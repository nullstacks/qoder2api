"""CodeBuddy 客户端：个人/企业额度探测与周期解析。"""

from __future__ import annotations

import json
import time

import httpx
import pytest

from src.provider.codebuddy.client import CodeBuddyCredential, _cycle_end_epoch
from src.provider.codebuddy.events import UpstreamProtocolViolation
from src.provider.codebuddy.headers import QUOTA_RANGE_END
from tests.provider.codebuddy._support import client as _client
from tests.provider.codebuddy._support import fixture


def _quota_body(accounts: object) -> dict:
    """包出上游真实的个人版额度响应结构（data.Response.Data.Accounts）。"""
    return {"code": 0, "msg": "OK",
            "data": {"Response": {"Data": {"Accounts": accounts},
                                  "RequestId": "<redacted>"}}}


@pytest.fixture(autouse=True)
def _clear_model_cache():
    """模块级模型缓存必须按测试清空，否则用例间互相污染。"""
    from src.provider.codebuddy.client import _MODEL_CACHE

    _MODEL_CACHE.clear()
    # `_days()` 的基准时刻同理：按测试重置，避免整套跑时基准越来越旧
    global _DAYS_BASE
    _DAYS_BASE = None
    yield
    _MODEL_CACHE.clear()


def _days(offset: int) -> str:
    """相对基准时刻的偏移，返回上游格式的时间串（有符号，负数=过去）。

    基准时刻在**首次调用时固定**（`_days` 会被调用多次：构造 payload 一次、
    断言里再算一次）。若每次现取 `time.time()`，两者跨过整秒就会差 1 秒，
    `quota.expiry_ladder == [(_cycle_end_epoch(_days(3)), 40.0)]` 这类等值
    断言会随机翻红——带覆盖率跑（更慢）时尤其容易命中。
    偏移量都是小时级，基准固定后与真实 now 的漂移可忽略。
    """
    global _DAYS_BASE
    if _DAYS_BASE is None:
        _DAYS_BASE = time.time()
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_DAYS_BASE + offset * 3600))

async def test_fetch_personal_quota_parses_real_nested_response():
    """真实响应是 data.Response.Data.Accounts，且 *Precise 是字符串。

    用抓取的真实结构做契约测试：层级读错或忽略字符串都会让有效凭证
    被误判成「未探测到额度」。
    """
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=fixture("quota-personal.json"))

    quota = await _client(handler).fetch_quota(CodeBuddyCredential(bearer_token="t"))
    # fixture 是脱敏后的真实响应：两个 Status=0 的套餐（累加 total 5500），
    # remaining 取逐包 CycleCapacityRemainPrecise 之和（500 + 4767.50000158）。
    # 响应顶层另有 TotalDosage=9070，**必须忽略**：它大于按 size 累加的 total，
    # 是「上周期」口径、不随本周期消耗变化，采用它会让 remaining == total、
    # 健康度恒 100（2026-09-28 线上事故）。
    assert quota.total == 5500.0
    assert quota.remaining == pytest.approx(5267.50000158)
    assert quota.cycle_end is not None
    assert quota.probe_failed is False

async def test_total_dosage_is_ignored_in_favor_of_per_package_remaining():
    """回归：响应里的 TotalDosage 是陈旧的上周期口径，绝不能覆盖逐包剩余。

    构造一个 TotalDosage 与逐包和明显不同的响应，断言 remaining 取逐包口径。
    """
    body = {"code": 0, "msg": "OK", "data": {"Response": {"Data": {
        "TotalDosage": 9070,
        "Accounts": [
            {"Status": 0, "CycleCapacitySizePrecise": "500",
             "CycleCapacityRemainPrecise": "480"},
            {"Status": 0, "CycleCapacitySizePrecise": "100",
             "CycleCapacityRemainPrecise": "60"},
        ],
    }}}}

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    quota = await _client(handler).fetch_quota(CodeBuddyCredential(bearer_token="t"))
    assert quota.total == 600.0
    assert quota.remaining == 540.0            # 480 + 60，而非 TotalDosage=9070
    assert quota.remaining < quota.total       # 消耗可见，健康度不再恒 100

async def test_fetch_personal_quota_prefers_precise_over_plain():
    """Precise 优先，且能解析字符串；被禁用的套餐（Status!=0）要跳过。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        accounts = [
            {"Status": 0, "CycleCapacitySizePrecise": "100",
             "CycleCapacityRemainPrecise": "60", "CycleCapacitySize": 999,
             "CycleEndTime": "2026-12-31 23:59:59"},
            {"Status": 0, "CycleCapacitySize": 50, "CycleCapacityRemain": 10},
            {"Status": 1, "CycleCapacitySizePrecise": "777",
             "CycleCapacityRemainPrecise": "777"},
            "junk",
        ]
        return httpx.Response(200, json=_quota_body(accounts))

    quota = await _client(handler).fetch_quota(CodeBuddyCredential(bearer_token="t"))
    assert quota.total == 150.0      # 100(Precise 字符串) + 50(回退非 Precise)
    assert quota.remaining == 70.0
    assert quota.cycle_end is not None

async def test_fetch_personal_quota_cycle_end_is_earliest_not_expired():
    """账号常有几十个套餐各自独立到期（每日 100 积分 × N），cycle_end 取最早的那个。

    上游会把已过期套餐一起返回（EndTimeRangeBegin 过滤的是套餐有效期，不是积分周期），
    直接取最小值会得到过去的时间，调度器的到期偏好就永远不触发。
    """
    def handler(_request: httpx.Request) -> httpx.Response:
        accounts = [
            {"Status": 0, "CycleCapacitySizePrecise": "100",
             "CycleCapacityRemainPrecise": "100", "CycleEndTime": _days(400)},
            {"Status": 0, "CycleCapacitySizePrecise": "100",
             "CycleCapacityRemainPrecise": "100", "CycleEndTime": _days(2)},
            {"Status": 0, "CycleCapacitySizePrecise": "100",
             "CycleCapacityRemainPrecise": "64", "CycleEndTime": _days(-1)},
        ]
        return httpx.Response(200, json=_quota_body(accounts))

    quota = await _client(handler).fetch_quota(CodeBuddyCredential(bearer_token="t"))
    assert quota.cycle_end == _cycle_end_epoch(_days(2))
    assert quota.cycle_end < _cycle_end_epoch(_days(400))
    assert quota.cycle_end > time.time()          # 已过期套餐不参与
    assert quota.total == 300.0
    # 到期阶梯保留套餐顺序，只收未过期的包
    assert quota.expiry_ladder == [(_cycle_end_epoch(_days(400)), 100.0),
                                   (_cycle_end_epoch(_days(2)), 100.0)]

async def test_fetch_personal_quota_expiry_ladder_filters_packages():
    """到期阶梯只收「未过期 + 有余额」的包：状态异常、零容量、已用完、
    时间格式不合法的套餐都排除；total/remaining 仍按原口径累加。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        accounts = [
            {"Status": 1, "CycleCapacitySizePrecise": "100",
             "CycleCapacityRemainPrecise": "100", "CycleEndTime": _days(1)},
            {"Status": 0, "CycleCapacitySizePrecise": "0",
             "CycleCapacityRemainPrecise": "0", "CycleEndTime": _days(1)},
            {"Status": 0, "CycleCapacitySizePrecise": "100",
             "CycleCapacityRemainPrecise": "0", "CycleEndTime": _days(1)},
            {"Status": 0, "CycleCapacitySizePrecise": "100",
             "CycleCapacityRemainPrecise": "100", "CycleEndTime": "not-a-date"},
            {"Status": 0, "CycleCapacitySizePrecise": "100",
             "CycleCapacityRemainPrecise": "40", "CycleEndTime": _days(3)},
        ]
        return httpx.Response(200, json=_quota_body(accounts))

    quota = await _client(handler).fetch_quota(CodeBuddyCredential(bearer_token="t"))
    assert quota.expiry_ladder == [(_cycle_end_epoch(_days(3)), 40.0)]
    assert quota.total == 300.0
    assert quota.remaining == 140.0
    assert quota.cycle_end == _cycle_end_epoch(_days(3))

async def test_fetch_personal_quota_packages_include_names_and_usage():
    """展示明细比调度阶梯宽：已用完但仍有效的包要显示（管理员想知道哪包空了），
    但「已过期且已用完」的历史残留不堆进列表。与 ladder 互不影响。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        accounts = [
            # 未过期 + 有余额 → 进明细，也进阶梯
            {"Status": 0, "PackageName": "CodeBuddy个人体验版",
             "CycleCapacitySizePrecise": "500", "CycleCapacityRemainPrecise": "100",
             "CycleEndTime": _days(2)},
            # 未过期但已用完 → 只进明细（展示“已用完”），不进阶梯
            {"Status": 0, "PackageName": "已用完的包",
             "CycleCapacitySizePrecise": "100", "CycleCapacityRemainPrecise": "0",
             "CycleEndTime": _days(3)},
            # 已过期但还有余额 → 只进明细（展示“浪费了”），不进阶梯
            {"Status": 0, "PackageName": "已过期有余额",
             "CycleCapacitySizePrecise": "100", "CycleCapacityRemainPrecise": "50",
             "CycleEndTime": _days(-1)},
            # 已过期 + 已用完 → 两边都不进（历史残留）
            {"Status": 0, "PackageName": "历史残留",
             "CycleCapacitySizePrecise": "100", "CycleCapacityRemainPrecise": "0",
             "CycleEndTime": _days(-2)},
            # 无到期时间但还有余额 → 进明细，end 为 None
            {"Status": 0, "PackageName": "无到期",
             "CycleCapacitySizePrecise": "100", "CycleCapacityRemainPrecise": "10"},
        ]
        return httpx.Response(200, json=_quota_body(accounts))

    quota = await _client(handler).fetch_quota(CodeBuddyCredential(bearer_token="t"))
    assert [item["name"] for item in quota.packages] == [
        "CodeBuddy个人体验版", "已用完的包", "已过期有余额", "无到期"]
    assert quota.packages[0] == {"name": "CodeBuddy个人体验版", "total": 500.0,
                                 "used": 400.0, "end": _cycle_end_epoch(_days(2))}
    assert quota.packages[1]["used"] == 100.0      # 已用完
    assert quota.packages[3]["end"] is None        # 无到期信息
    # 调度阶梯不受展示口径影响：已过期的包不进阶梯（只收未过期 + 有余额）
    assert quota.expiry_ladder == [(_cycle_end_epoch(_days(2)), 100.0)]

async def test_fetch_personal_quota_cycle_end_none_when_all_packages_expired():
    """套餐全部过期（积分已作废）时不给出过去的到期点。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        accounts = [
            {"Status": 0, "CycleCapacitySizePrecise": "100",
             "CycleCapacityRemainPrecise": "100", "CycleEndTime": _days(-2)},
            {"Status": 0, "CycleCapacitySizePrecise": "100",
             "CycleCapacityRemainPrecise": "100", "CycleEndTime": _days(-48)},
        ]
        return httpx.Response(200, json=_quota_body(accounts))

    quota = await _client(handler).fetch_quota(CodeBuddyCredential(bearer_token="t"))
    assert quota.cycle_end is None
    assert quota.total == 200.0
    assert quota.expiry_ladder == []

@pytest.mark.parametrize("accounts", [None, []])
async def test_fetch_quota_accepts_null_accounts_as_no_personal_quota(accounts):
    """个人版 Accounts: null / [] 表示探测成功但没有额度（AGENTS.md 约束）。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_quota_body(accounts))

    quota = await _client(handler).fetch_quota(CodeBuddyCredential(bearer_token="t"))
    assert quota.total == 0 and quota.remaining == 0
    assert quota.probe_failed is False

async def test_fetch_personal_quota_omits_product_code():
    """回归：探测请求不带 ProductCode。

    上游对部分账号已拒绝 "codebuddy" 产品码（InvalidParameterValue:
    productCode:param format error），缺省时才返回全部套餐。
    """
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.read()))
        return httpx.Response(200, json=_quota_body([]))

    await _client(handler).fetch_quota(CodeBuddyCredential(bearer_token="t"))
    assert bodies == [{"PageNumber": 1, "PageSize": 200, "Status": [0, 3],
                       "PackageEndTimeRangeBegin": bodies[0]["PackageEndTimeRangeBegin"],
                       "PackageEndTimeRangeEnd": QUOTA_RANGE_END}]

@pytest.mark.parametrize("message", ["[productCode:param format error] ", None])
async def test_fetch_quota_response_error_is_probe_failure(message):
    """上游业务层错误（Response.Error）必须抛探测失败，不能当成 0 额度
    把还有积分的凭证误标成「已耗尽」。"""
    error = {"Code": "InvalidParameterValue", "Message": message}
    body = {"code": 0, "msg": "OK",
            "data": {"Response": {"Data": {"Accounts": None,
                                          "TotalDosage": 0},
                                  "Error": error}}}

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    with pytest.raises(UpstreamProtocolViolation, match="InvalidParameterValue"):
        await _client(handler).fetch_quota(CodeBuddyCredential(bearer_token="t"))

@pytest.mark.parametrize("body", [
    {},                                     # 完全没有 data
    {"data": None},
    {"data": {}},                           # data 里没有 Accounts
    {"data": {"Response": {}}},
    {"data": {"Response": {"Data": {}}}},
    {"data": {"Accounts": "no"}},           # 类型错误
])
async def test_fetch_quota_missing_accounts_fails(body):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    with pytest.raises(UpstreamProtocolViolation):
        await _client(handler).fetch_quota(CodeBuddyCredential(bearer_token="t"))

async def test_fetch_enterprise_quota_requires_oauth():
    """企业额度接口只对 OAuth 凭证开放；手动凭证走个人版。"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("get-enterprise-user-usage"):
            return httpx.Response(200, json={"credit": 30, "limitNum": 100})
        return httpx.Response(200, json=_quota_body([]))

    oauth = CodeBuddyCredential(bearer_token="t", auth_source="oauth",
                                enterprise_id="e", quota_probe_mode="enterprise")
    quota = await _client(handler).fetch_quota(oauth)
    assert quota.total == 100 and quota.remaining == 70
    assert calls[-1].endswith("get-enterprise-user-usage")

    manual = CodeBuddyCredential(bearer_token="t", quota_probe_mode="enterprise")
    await _client(handler).fetch_quota(manual)
    assert calls[-1].endswith("get-user-resource")

async def test_fetch_enterprise_quota_rejects_missing_limit():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"credit": 5})

    oauth = CodeBuddyCredential(bearer_token="t", auth_source="oauth",
                                quota_probe_mode="enterprise")
    with pytest.raises(UpstreamProtocolViolation):
        await _client(handler).fetch_quota(oauth)

async def test_fetch_quota_skips_packages_with_zero_total():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_quota_body([
            {"Status": 0, "CycleCapacitySizePrecise": "0", "CycleCapacityRemainPrecise": "5"},
            {"Status": 0, "CycleCapacitySizePrecise": "10",
             "CycleCapacityRemainPrecise": "10"},
        ]))

    quota = await _client(handler).fetch_quota(CodeBuddyCredential(bearer_token="t"))
    assert quota.total == 10 and quota.remaining == 10

async def test_fetch_quota_tolerates_bad_cycle_end_format():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_quota_body([
            {"Status": 0, "CycleCapacitySizePrecise": "5",
             "CycleCapacityRemainPrecise": "5", "CycleEndTime": "not-a-date"},
        ]))

    quota = await _client(handler).fetch_quota(CodeBuddyCredential(bearer_token="t"))
    assert quota.cycle_end is None

def test_cycle_end_accepts_iso_separator():
    from src.provider.codebuddy.client import _cycle_end_epoch

    assert _cycle_end_epoch("2026-12-31T23:59:59") is not None
    assert _cycle_end_epoch(None) is None
    assert _cycle_end_epoch("") is None

@pytest.mark.parametrize(("value", "expected"), [
    (500, 500.0),
    (500.5, 500.5),
    ("500", 500.0),          # 上游 Precise 字段是字符串
    (" 4767.50000158 ", 4767.50000158),
    ("", None),              # 空字符串
    ("   ", None),
    ("not-a-number", None),  # 无法解析
    (None, None),
    ([], None),
    (True, None),            # 布尔不算数字
    (False, None),
])
def test_number_parses_strings_and_rejects_junk(value, expected):
    """额度解析必须能处理字符串型 Precise，否则整批额度会被算成 0。"""
    from src.provider.codebuddy.client import _number

    result = _number(value)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)
