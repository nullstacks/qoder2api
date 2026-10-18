"""CodeBuddy 签到：响应解析、状态查询与领取。"""

from __future__ import annotations

import httpx
import pytest

from src.provider.codebuddy.checkin import (
    CheckinResult,
    CodeBuddyCheckin,
    checkin_scope_key,
    parse_checkin_response,
    parse_checkin_status,
)
from src.provider.codebuddy.client import CodeBuddyCredential
from src.provider.codebuddy.events import UpstreamProtocolViolation
from tests.helpers.http import mock_async_client as _refresh_client


@pytest.mark.parametrize(("body", "ok"), [
    ({"code": 0, "data": {"credit": 100}}, True),
    ({"code": 0, "data": {"credit": 0}}, True),          # 0 也是合法数值
    ({"code": 0, "data": {}}, False),                    # 已签过
    ({"code": 0, "data": {"credit": True}}, False),      # 布尔不算数值
    ({"code": 0, "data": {"credit": "x"}}, False),
    ({"code": 9999, "data": {"credit": 5}}, False),
    ({"code": None, "data": {}}, False),                 # code=null 不阻断后续补偿
])
def test_parse_checkin_response(body, ok):
    result = parse_checkin_response(body)
    assert result.ok is ok

def test_parse_checkin_response_marks_already_checked_in():
    """上游把「已签到」返回成 HTTP 400 + code=10001，必须视为成功。"""
    real = parse_checkin_response(
        {"code": 10001, "msg": "今天已签到，请明天再来",
         "requestId": "595bc3bc-a294-44dc-90dc-4cc3e3756018"})
    assert real.ok is True and real.already_checked_in is True
    assert parse_checkin_response({"code": 0, "msg": "OK", "data": {}}).ok is False
    assert parse_checkin_response({"code": 5, "msg": "boom"}).message == "boom"

@pytest.mark.parametrize("body", ["junk", [1]])
def test_parse_checkin_response_rejects_non_object(body):
    with pytest.raises(UpstreamProtocolViolation):
        parse_checkin_response(body)

def test_checkin_scope_key_normalizes():
    assert checkin_scope_key("https://e/", "u") == "https://e|u"
    # 身份未知时必须返回空串：返回 "endpoint|" 会让所有该渠道凭证算出同一个
    # scope，签到任务只签第一个账号（实测两个 CB 凭证 account_uid/user_id 都是空）
    assert checkin_scope_key("https://e", "") == ""
    assert checkin_scope_key("https://e", "   ") == ""

def test_parse_checkin_status_extracts_fields():
    """状态解析：字段齐全时逐项取出，类型归一。"""
    status = parse_checkin_status({"code": 0, "msg": "OK", "data": {
        "active": True, "today_checked_in": True, "streak_days": 4,
        "today_credit": 100, "total_credits": 400, "is_streak_day": False,
        "activity_name": "高校新生攻略"}})
    assert status.active is True and status.today_checked_in is True
    assert status.streak_days == 4 and status.today_credit == 100.0
    assert status.total_credits == 400.0 and status.activity_name == "高校新生攻略"
    assert status.is_streak_day is False
    assert status.to_dict()["streak_days"] == 4

@pytest.mark.parametrize("field", ["streak_days", "today_credit", "total_credits"])
@pytest.mark.parametrize("value", [None, "x", True, float("inf")])
def test_parse_checkin_status_tolerates_bad_optional_fields(field, value):
    """可选数值字段缺失/非法/非有限 → None，绝不抛异常（展示字段不能拖垮签到）。"""
    status = parse_checkin_status({"code": 0, "data": {"active": True, field: value}})
    assert getattr(status, field) is None

def test_parse_checkin_status_rejects_bad_envelope():
    """code 非 0、缺 data、非对象：一律显式报错，不静默当「未签到」。"""
    for body in ("junk", [1], {"code": 1, "msg": "boom"}, {"code": 0},
                 {"code": 0, "data": []}, {"data": {"active": True}}):
        with pytest.raises(UpstreamProtocolViolation):
            parse_checkin_status(body)

async def test_checkin_fetch_status_error_paths():
    """状态接口：401/403 与 5xx 抛异常；非 JSON 也抛（不能当空状态吞掉）。"""
    async def unauthorized(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": 401})

    client = CodeBuddyCheckin("https://e", client=_refresh_client(unauthorized))
    with pytest.raises(UpstreamProtocolViolation):
        await client.fetch_status(CodeBuddyCredential(bearer_token="t"))
    await client.aclose()

    async def server_error(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"boom")

    client2 = CodeBuddyCheckin("https://e", client=_refresh_client(server_error))
    with pytest.raises(UpstreamProtocolViolation):
        await client2.fetch_status(CodeBuddyCredential(bearer_token="t"))
    await client2.aclose()

    async def html(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>")

    client3 = CodeBuddyCheckin("https://e", client=_refresh_client(html))
    with pytest.raises(UpstreamProtocolViolation):
        await client3.fetch_status(CodeBuddyCredential(bearer_token="t"))
    await client3.aclose()

async def test_checkin_claim_failure_skips_status_lookup():
    """领取失败（业务码非 0）时不做状态回查：失败结论已完整，多余请求只会拖时间。"""
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"code": 5, "msg": "boom"})

    client = CodeBuddyCheckin("https://e", client=_refresh_client(handler))
    result = await client.claim(CodeBuddyCredential(bearer_token="t"))
    assert result.ok is False and result.status is None
    assert len(calls) == 1                              # 只有 daily-checkin 一次请求
    await client.aclose()

async def test_checkin_claim_attaches_status_and_ignores_status_failure():
    """领取成功后回查状态；回查失败不能推翻「已领取」的既成事实。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("checkin-activity-status"):
            return httpx.Response(200, json={"code": 0, "data": {
                "active": True, "today_checked_in": True, "streak_days": 5}})
        return httpx.Response(200, json={"code": 0, "data": {"credit": 100}})

    client = CodeBuddyCheckin("https://e", client=_refresh_client(handler))
    result = await client.claim(CodeBuddyCredential(bearer_token="t"))
    assert result.ok and result.status is not None
    assert result.status.streak_days == 5
    await client.aclose()

    async def status_boom(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("checkin-activity-status"):
            return httpx.Response(500, content=b"boom")
        return httpx.Response(200, json={"code": 0, "data": {"credit": 100}})

    client2 = CodeBuddyCheckin("https://e", client=_refresh_client(status_boom))
    result2 = await client2.claim(CodeBuddyCredential(bearer_token="t"))
    assert result2.ok and result2.credit == 100 and result2.status is None
    await client2.aclose()

async def test_checkin_claim():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/billing/meter/daily-checkin"
        return httpx.Response(200, json={"code": 0, "data": {"credit": 50}})

    client = CodeBuddyCheckin("https://e", client=_refresh_client(handler))
    result = await client.claim(CodeBuddyCredential(bearer_token="t"))
    assert result.ok and result.credit == 50
    await client.aclose()

async def test_checkin_claim_error_paths():
    async def failing(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    client = CodeBuddyCheckin("https://e", client=_refresh_client(failing))
    with pytest.raises(UpstreamProtocolViolation):
        await client.claim(CodeBuddyCredential(bearer_token="t"))
    await client.aclose()

    async def rejected(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, content=b"nope")

    client2 = CodeBuddyCheckin("https://e", client=_refresh_client(rejected))
    with pytest.raises(UpstreamProtocolViolation):
        await client2.claim(CodeBuddyCredential(bearer_token="t"))
    await client2.aclose()

    async def not_json(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>")

    client3 = CodeBuddyCheckin("https://e", client=_refresh_client(not_json))
    with pytest.raises(UpstreamProtocolViolation):
        await client3.claim(CodeBuddyCredential(bearer_token="t"))
    await client3.aclose()

async def test_checkin_lazy_client_and_close():
    client = CodeBuddyCheckin("https://e")
    assert client._http is client._http
    await client.aclose()

async def test_checkin_claim_http_error_paths(repo):
    """签到 HTTP 4xx/5xx → 协议违规（checkin.py 70 出口）。"""
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, content=b"slow down")

    client = CodeBuddyCheckin("https://e", client=_refresh_client(handler))
    with pytest.raises(UpstreamProtocolViolation):
        await client.claim(CodeBuddyCredential(bearer_token="t"))
    await client.aclose()

async def test_checkin_result_defaults():
    assert CheckinResult(ok=True).credit is None
    assert CheckinResult(ok=False, code=7).already_checked_in is False
