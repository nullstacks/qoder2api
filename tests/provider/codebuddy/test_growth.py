"""CodeBuddy 成长中心解析层：信封/数值宽松转换、任务/连续/兑换/旅行解析与展示文案。"""

from __future__ import annotations

import httpx

from src.provider.base import GrowthResult, GrowthStep, StepStatus
from src.provider.codebuddy.growth import (
    GrowthRejected,
    GrowthTaskItem,
    as_int,
    client_token,
    dig,
    is_tier_locked,
    is_unknown_tier,
    parse_granted,
    parse_redeem_summary,
    parse_reward,
    parse_streak,
    parse_tasks,
    parse_travel_locations,
    parse_travel_status,
)
from src.provider.codebuddy.growth_runner import _eta, _fmt, _report
from tests.provider.codebuddy._growth_support import (
    REDEEM_LOCKED,
    STREAK_BODY,
    TASKS_BODY,
    TRAVEL_ARRIVED,
)


def test_dig_and_as_int_tolerate_junk():
    """信封查找与宽松数值转换：坏数据不能抛异常（上游会返回 Infinity）。"""
    assert dig({"data": {"a": 1}}, "a") == 1
    assert dig({"result": {"resp": {"a": 2}}}, "a") == 2
    assert dig({"a": None}, "a") is None
    assert dig([1, 2], "a") is None
    assert as_int("1.5") == 1
    assert as_int(float("inf")) == 0
    assert as_int(True) == 1
    assert as_int(None, default=7) == 7

def test_client_token_is_prefixed_uuid():
    """抽奖/兑换必须带 u-<uuid>：缺了上游直接 400（实测）。"""
    token = client_token()
    assert token.startswith("u-") and len(token) > 20
    assert token != client_token()

def test_parse_travel_status_variants():
    arrived = parse_travel_status(TRAVEL_ARRIVED["data"])
    assert arrived.state == "arrived" and arrived.record_id == 277990
    assert arrived.reward_credit == 10.0 and arrived.location_name == "咖啡馆"
    # state 缺失/非法 → idle（不能因为字段异常就以为要派 Buddy）
    assert parse_travel_status({}).state == "idle"
    assert parse_travel_status({"state": 5}).state == "idle"
    assert parse_travel_status({"state": "idle", "daily_limit_reached": True}) \
        .daily_limit_reached is True
    # location 非 dict 时不能炸
    assert parse_travel_status({"state": "traveling", "location": "x"}).location_name == ""

def test_parse_travel_locations_filters_non_dicts():
    locations = parse_travel_locations({"locations": [
        {"id": 1, "name": "咖啡馆", "duration_hours_min": 1}, "junk"]})
    assert len(locations) == 1 and locations[0].name == "咖啡馆"
    assert parse_travel_locations({"locations": "x"}) == []

def test_parse_tasks_three_state_semantics():
    """accept_status 三态与进度语义：决定「接单」还是「领奖」。"""
    tasks = parse_tasks(TASKS_BODY["data"])
    assert len(tasks) == 6
    by_code = {task.task_code: task for task in tasks}
    assert by_code["t_new"].completed is False and by_code["t_new"].accept_status == ""
    assert by_code["t_doing"].accept_status == "accepted"
    assert by_code["t_done"].completed is True
    assert by_code["t_locked"].locked is True
    # target 缺失按 1：0 >= 0 会被误判成已完成
    assert by_code["t_bare"].progress_target == 1
    assert by_code["t_bare"].completed is False
    assert parse_tasks({"tasks": "junk"}) == []

def test_parse_streak_card_shapes_and_dates():
    """makeup_cards 兼容对象与裸数字；makeup_dates 兼容 streak 内层与顶层。"""
    inner = parse_streak(STREAK_BODY["data"])
    assert inner.makeup_cards == 0 and inner.days == 0 and inner.makeup_dates == []
    bare = parse_streak({"makeup_cards": 2, "streak": {"days": 5,
                                                       "makeup_dates": ["2026-09-01", 7]}})
    assert bare.makeup_cards == 2 and bare.days == 5
    assert bare.makeup_dates == ["2026-09-01"]        # 非字符串日期被过滤
    top = parse_streak({"makeup_dates": ["2026-09-02"]})
    assert top.makeup_dates == ["2026-09-02"]
    assert parse_streak({"streak": {"days": "5"}}).days is None

def test_parse_redeem_summary_skips_missing_tiers():
    assert parse_redeem_summary(REDEEM_LOCKED["data"]) == {
        "starter": "locked", "advanced": "locked", "legendary": "locked"}
    # 字段缺失不猜"可兑换"：接口改版时不该对三档无脑 POST
    assert parse_redeem_summary({"starter_status": "unlocked"}) == {"starter": "unlocked"}

def test_parse_reward_reads_actual_values():
    credit, energy = parse_reward({"data": {"credit": 10, "energy": 5}})
    assert credit == 10.0 and energy == 5.0
    assert parse_reward({}) == (None, None)
    assert parse_reward({"credit": True}) == (None, None)

def test_growth_rejected_needs_attention_boundary():
    """4xx 是业务规则（不算故障），5xx 与"没拿到响应"才需要人看。"""
    assert GrowthRejected(400, "x").needs_attention is False
    assert GrowthRejected(429, "x").needs_attention is False
    assert GrowthRejected(500, "x").needs_attention is True
    assert GrowthRejected(-1, "network").needs_attention is True
    assert "400" in str(GrowthRejected(400))

def test_is_unknown_tier_only_for_param_rejections():
    """只有「参数里 tier 不认识」的 400 才允许换写法重试。"""
    assert is_unknown_tier(GrowthRejected(400, "unknown tier")) is True
    assert is_unknown_tier(GrowthRejected(400, "invalid tier value")) is True
    assert is_unknown_tier(GrowthRejected(400, "unsupported tier")) is True
    assert is_unknown_tier(GrowthRejected(400, "invalid request")) is False
    assert is_unknown_tier(GrowthRejected(500, "unknown tier")) is False

def test_eta_and_fmt_are_display_only():
    """展示函数遇到坏数据必须返回空串/原样，绝不能抛异常。"""
    assert _eta(1000, 100) == "，约 15 分钟后回"
    assert _eta(10000, 100) == "，约 2.8 小时后回"
    assert _eta(100, 1000) == "，已到达待领取"
    assert _eta(None, 1) == "" and _eta("x", 1) == "" and _eta(float("inf"), 0) == ""
    assert _fmt(10.0) == "10" and _fmt(1.5) == "1.5" and _fmt("x") == "x"

def test_report_summarizes_steps_and_tail():
    result = GrowthResult(steps=[
        GrowthStep("领旅行礼物", StepStatus.DONE, "咖啡馆 带回 10 积分"),
        GrowthStep("Buddy 旅行中", StepStatus.IDLE, "书店，约 1 小时后回"),
        GrowthStep("开盲盒", StepStatus.FAILED, "HTTP 500"),
    ], credit=10.0, energy=18, streak_days=4)
    report = _report(result)
    assert "领旅行礼物" in report and "开盲盒" in report
    assert "Buddy 旅行中" not in report          # idle 不进汇报（无事发生）
    assert "能量 18" in report and "连登 4 天" in report and "+共 10 积分" in report
    assert _report(GrowthResult()) == "成长中心无可领取项"

def test_report_omits_tail_when_only_steps():
    """没有能量/连签/积分时汇报只有步骤，不留空括号。"""
    report = _report(GrowthResult(steps=[GrowthStep("领旅行礼物", StepStatus.DONE, "咖啡馆")]))
    assert report == "领旅行礼物：咖啡馆"
    assert _report(GrowthResult(steps=[GrowthStep("步", StepStatus.DONE, "")])) == "步"

def test_dig_skips_none_and_non_dict_wrappers():
    """信封查找：值为 None 视为未命中继续找；非 dict 的包裹层跳过。"""
    assert dig({"a": None, "data": {"a": 5}}, "a") == 5
    assert dig({"data": [1, 2], "result": {"a": 7}}, "a") == 7
    assert dig({"data": {"a": None}}, "a") is None

def test_parse_tasks_skips_non_dict_entries():
    """列表中混入非对象条目时跳过，不能因此丢掉整份任务列表。"""
    tasks = parse_tasks({"tasks": ["junk", {"task_code": "t1", "title": "任务"}]})
    assert len(tasks) == 1 and tasks[0].task_code == "t1"

def test_streak_status_post_init_defaults_dates():
    """直接构造 StreakStatus 时 makeup_dates 默认空列表（不是 None）。"""
    from src.provider.codebuddy.growth import StreakStatus

    assert StreakStatus().makeup_dates == []
    assert StreakStatus(makeup_dates=["2026-09-01"]).makeup_dates == ["2026-09-01"]

async def test_message_of_ignores_non_string_and_non_dict_bodies():
    """失败消息提取：非字符串值/非对象体都跳过，最终回落空串。"""
    from src.provider.codebuddy.growth import _message_of

    assert _message_of(httpx.Response(500, json={"msg": 5})) == ""
    assert _message_of(httpx.Response(500, json=["x"])) == ""
    assert _message_of(httpx.Response(500, json={"message": "第二优先"})) == "第二优先"
    assert _message_of(httpx.Response(500, json={"error": "第三优先"})) == "第三优先"

def test_task_item_five_state_semantics():
    """accept_status 五态：not_accepted 必须接单，completed 才能领奖。

    回归：三态假设下 `not_accepted` 被当成「已接单」跳过，实测有账号积压
    5 个任务共 650 积分既没接单也没领奖。
    """
    def item(status: str, locked: bool = False) -> GrowthTaskItem:
        return GrowthTaskItem(task_code="t", accept_status=status, locked=locked)

    assert item("not_accepted").needs_accept is True
    assert item("").needs_accept is True                 # 字段缺失同样当未接单
    assert item("accepted").needs_accept is False
    assert item("in_progress").needs_accept is False
    assert item("completed").needs_accept is False
    assert item("claimed").needs_accept is False
    assert item("not_accepted", locked=True).needs_accept is False

    assert item("completed").needs_claim is True
    assert item("claimed").needs_claim is False
    assert item("accepted").needs_claim is False
    assert item("completed", locked=True).needs_claim is False

def test_parse_tasks_reads_five_states_from_fixture():
    """真实五态夹具：19 个任务的形状（含 not_accepted 与 accepted 并存）。"""
    tasks = parse_tasks({"tasks": [
        {"task_code": "a", "title": "未接单", "accept_status": "not_accepted",
         "progress": None, "reward_credit": 300},
        {"task_code": "b", "title": "进行中", "accept_status": "accepted",
         "progress": {"current": 0, "target": 1}},
        {"task_code": "c", "title": "已完成", "accept_status": "completed"},
        {"task_code": "d", "title": "已领", "accept_status": "claimed"},
    ]})
    assert [t.accept_status for t in tasks] == [
        "not_accepted", "accepted", "completed", "claimed"]
    assert tasks[0].needs_accept is True and tasks[0].reward_credit == 300.0
    assert tasks[2].needs_claim is True

def test_parse_granted_prefers_granted_fields():
    """兑换实发字段是 *_granted；读裸 credit 会把兑换所得全部漏计。"""
    assert parse_granted({"credit_granted": 50, "energy_granted": 3}) == (50.0, 3.0)
    # 只给一个 *_granted 也照用（不为空的那个回落裸字段）
    assert parse_granted({"credit_granted": 50}) == (50.0, None)
    # 两个都缺 → 回落裸字段（接口改版方向未知，两边都兜）
    assert parse_granted({"credit": 7, "energy": 2}) == (7.0, 2.0)
    assert parse_granted({}) == (None, None)

def test_is_tier_locked_only_for_403_with_message():
    """403 + 「天数不足」= 未解锁（常态）；其他 403/401 仍按登录失效处理。"""
    assert is_tier_locked(GrowthRejected(403, "连续登录天数不足")) is True
    assert is_tier_locked(GrowthRejected(403, "连登天数不足")) is True
    assert is_tier_locked(GrowthRejected(403, "")) is False
    assert is_tier_locked(GrowthRejected(403, "forbidden")) is False
    assert is_tier_locked(GrowthRejected(401, "天数不足")) is False
    assert is_tier_locked(GrowthRejected(400, "天数不足")) is False

def test_prerequisite_of_handles_empty_and_unknown_reasons():
    """前置条件解析的边界：消息截断、未知 code、大小写。"""
    from src.provider.codebuddy.growth_runner import _prerequisite_label, _prerequisite_of

    assert _prerequisite_of("prerequisite not met: first_buddy") == "first_buddy"
    assert _prerequisite_of("PREREQUISITE NOT MET: some_task.") == "some_task"
    assert _prerequisite_of("prerequisite not met:") == ""      # 有标记无原因
    assert _prerequisite_of("task does not require acceptance") is None
    assert _prerequisite_of("") is None
    # 未知前置条件回落成 code 本身（不编造说明）
    assert _prerequisite_label("mystery_task") == "mystery_task"
    assert "客户端" in _prerequisite_label("first_buddy")

def test_is_no_buddy_matches_only_buddy_rejections():
    """「还没有 Buddy」的识别：只看 400 + buddy 关键字。"""
    from src.provider.codebuddy.growth_runner import _is_no_buddy

    assert _is_no_buddy(GrowthRejected(400, "no active buddy")) is True
    assert _is_no_buddy(GrowthRejected(400, "No Active Buddy")) is True
    assert _is_no_buddy(GrowthRejected(400, "daily limit reached")) is False
    assert _is_no_buddy(GrowthRejected(500, "no active buddy")) is False
