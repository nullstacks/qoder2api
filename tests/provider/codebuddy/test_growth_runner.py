"""CodeBuddy 成长 runner 主流程：旅行/任务/makeup/出发与会话失效。"""

from __future__ import annotations

import httpx

from src.provider.base import GrowthStep, StepStatus
from src.provider.codebuddy.client import CodeBuddyCredential
from src.provider.codebuddy.growth import (
    EP_BUDDY_OPEN,
    EP_BUDDY_QUOTA,
    EP_ENERGY,
    EP_LOTTERY_CHANCES,
    EP_LOTTERY_DRAW,
    EP_MAKEUP_USE,
    EP_REDEEM,
    EP_REDEEM_SUMMARY,
    EP_STREAK,
    EP_TASK_ACCEPT,
    EP_TASKS,
    EP_TRAVEL_CLAIM,
    EP_TRAVEL_CONFIG,
    EP_TRAVEL_DEPART,
    EP_TRAVEL_STATUS,
)
from src.provider.codebuddy.growth_runner import MAKEUP_MAX_PER_RUN
from tests.provider.codebuddy._growth_support import STREAK_BODY, TRAVEL_ARRIVED, TRAVEL_IDLE
from tests.provider.codebuddy._growth_support import runner as _runner

TRAVEL_TRAVELING = {"code": 0, "data": {
    "state": "traveling", "location": {"id": 2, "name": "书店"},
    "arrive_at": 1000, "server_now": 100, "daily_limit_reached": False}}






async def _run(handler, *, allow_irreversible: bool = True):
    runner = _runner(handler, allow_irreversible=allow_irreversible)
    return await runner.run(CodeBuddyCredential(bearer_token="t"))

async def test_runner_full_happy_path_collects_every_reward():
    """一轮完整跑通：礼物 / 派 Buddy / 接单 / 领奖 / 连登兑换 / 抽奖 / Buddy 盲盒。"""
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        if path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_ARRIVED)
        if path == EP_TRAVEL_CLAIM:
            return httpx.Response(200, json={"code": 0, "data": {"credit": 8}})
        if path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {
                "locations": [{"id": 1, "name": "咖啡馆"}]}})
        if path == EP_TRAVEL_DEPART:
            # 真实响应把时长放在 location 内层
            return httpx.Response(200, json={"code": 0, "data": {
                "location": {"name": "咖啡馆", "duration_hours": 3}}})
        if path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": [
                {"task_code": "t_new", "title": "新任务", "accept_status": "not_accepted"},
                {"task_code": "t_done", "title": "已完成", "accept_status": "completed",
                 "reward_credit": 50}]}})
        if path == EP_TASK_ACCEPT:
            return httpx.Response(200, json={"code": 0, "data": {"results": [
                {"task_code": "t_new", "status": "ok"}]}})
        if path.endswith("/t_done/claim"):
            return httpx.Response(200, json={"code": 0, "data": {"credit": 50}})
        if path == EP_STREAK:
            return httpx.Response(200, json={"code": 0, "data": {
                "streak": {"days": 7, "makeup_dates": []},
                "makeup_cards": {"balance": 2}}})
        if path == EP_REDEEM_SUMMARY:
            return httpx.Response(200, json={"code": 0, "data": {
                "starter_status": "unlocked", "advanced_status": "locked"}})
        if path == EP_REDEEM:
            return httpx.Response(200, json={"code": 0, "data": {"credit": 0, "energy": 2}})
        if path == EP_MAKEUP_USE:
            return httpx.Response(200, json={"code": 0, "data": {"makeup_cards": 1}})
        if path == EP_LOTTERY_CHANCES:
            return httpx.Response(200, json={"code": 0, "data": {"balance": 2}})
        if path == EP_LOTTERY_DRAW:
            return httpx.Response(200, json={"code": 0, "data": {"prize_name": "冰箱贴",
                                                                 "need_address": True}})
        if path == EP_BUDDY_QUOTA:
            return httpx.Response(200, json={"code": 0, "data": {
                "affordable": 1, "cost_per_open": 10, "max_open_count": 5}})
        if path == EP_BUDDY_OPEN:
            return httpx.Response(200, json={"code": 0, "data": {"buddy": "小猫"}})
        if path == EP_ENERGY:
            return httpx.Response(200, json={"code": 0, "data": {"balance": 18}})
        return httpx.Response(404, json={"code": 404})

    result = await _run(handler)
    assert result.ok is True and result.session_dead is False
    details = {step.name: step for step in result.steps}

    def done(name: str) -> GrowthStep:
        return next(step for step in result.steps
                    if step.name == name and step.status == StepStatus.DONE)

    assert details["领旅行礼物"].detail == "咖啡馆 带回 8 积分"
    assert details["派 Buddy"].detail == "去咖啡馆（3 小时后回）"
    assert done("领取任务").detail == "「新任务」（进度开始计）"
    assert done("领任务奖").credit == 50.0
    assert details["连登兑换"].detail == "「入门」+0 积分 +2 能量"
    assert done("开盲盒").detail == "冰箱贴（实物奖，需到成长中心填写收件信息）"
    assert done("Buddy 盲盒").detail == "×1（小猫）"
    # 实际发放值优先：claim 回包 8 覆盖了 status 里的 10
    assert result.credit == 58.0 and result.energy == 18 and result.streak_days == 7
    assert result.gained is True and result.failed == []
    # 抽奖还剩 1 次 → 记一句下轮继续，但不影响成功
    assert any(step.detail.startswith("还剩 1 次") for step in result.steps)
    assert not any(step.name == "补登" for step in result.steps)   # 无卡可补

async def test_runner_daily_limit_and_traveling_are_idle_not_failure():
    """名额用完 / Buddy 在路上是日常状态：不算失败，也不该继续派出发。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json={"code": 0, "data": {
                "state": "idle", "daily_limit_reached": True}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(handler)
    assert result.ok is True and result.failed == []
    assert result.steps[0].status == StepStatus.IDLE
    assert result.steps[0].detail == "今日旅行名额已用完"
    assert result.gained is False
    assert "成长中心无可领取项" in result.report

    async def traveling(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_TRAVELING)
        return httpx.Response(200, json={"code": 0, "data": {}})

    result2 = await _run(traveling)
    assert result2.ok is True
    assert result2.steps[0].name == "Buddy 旅行中"
    assert "约 15 分钟后回" in result2.steps[0].detail

async def test_runner_depart_failure_and_empty_locations():
    """派 Buddy 失败：4xx 业务规则不算硬失败；没有目的地则跳过。"""
    async def rejected(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {
                "locations": [{"id": 1, "name": "咖啡馆"}]}})
        if request.url.path == EP_TRAVEL_DEPART:
            return httpx.Response(400, json={"code": 400, "msg": "今日名额已用完"})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(rejected)
    failed = [step for step in result.steps if step.name == "派 Buddy"]
    assert failed[0].status == StepStatus.IDLE         # 业务规则不算失败
    assert "今日名额已用完" in failed[0].detail
    assert result.ok is True

    async def no_location(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result2 = await _run(no_location)
    assert result2.ok is True and result2.steps[0].detail == "无可选目的地"

async def test_runner_task_failure_is_recorded_per_task():
    """单条任务失败只记该条，不影响其余任务与整轮结论。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": [
                {"task_code": "t1", "title": "坏任务", "accept_status": "not_accepted"},
                {"task_code": "t2", "title": "好任务", "accept_status": "not_accepted"}]}})
        if request.url.path == EP_TASK_ACCEPT:
            # 上游逐条报错：一条前置条件未满足（常态）、一条 ok
            return httpx.Response(200, json={"code": 0, "data": {"results": [
                {"task_code": "t1", "status": "error",
                 "message": "prerequisite not met: first_buddy"},
                {"task_code": "t2", "status": "ok"}]}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(handler)
    assert "接单受阻" in result.report and "好任务" in result.report
    # 前置条件未满足是常态（不记 FAILED），同一轮「好任务」接单成功
    assert result.failed == []
    assert result.ok is True and result.gained is True
    blocked = next(s for s in result.steps if s.name == "接单受阻")
    assert blocked.status == StepStatus.IDLE
    assert "领取一只 Buddy" in blocked.detail   # 前置条件给了可读说明

async def test_runner_makeup_uses_one_card_and_reports_leftover():
    """补登：一轮只花一张卡；还有可补天数时留到下轮。"""
    used: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json={"code": 0, "data": {
                "streak": {"days": 3, "makeup_dates": ["2026-09-01", "2026-09-02"]},
                "makeup_cards": {"balance": 3}}})
        if request.url.path == EP_MAKEUP_USE:
            import json as _json

            used.append(_json.loads(request.content)["target_date"])
            return httpx.Response(200, json={"code": 0, "data": {"makeup_cards": 2}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(handler)
    assert used == ["2026-09-01"]                     # 一轮只补一天
    assert MAKEUP_MAX_PER_RUN == 1
    details = [step.detail for step in result.steps if step.name == "补登"]
    assert "剩 2 张卡" in details[0]
    assert any("另有 1 天可补" in detail for detail in details)

async def test_runner_makeup_failure_stops_and_card_balance_zero_skips():
    """补登失败 → 记失败并停止补登；没有卡/没有可补日期 → 完全不请求。"""
    called: list[str] = []

    async def failing(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json={"code": 0, "data": {
                "streak": {"days": 3, "makeup_dates": ["2026-09-01"]},
                "makeup_cards": {"balance": 1}}})
        if request.url.path == EP_MAKEUP_USE:
            return httpx.Response(500, content=b"boom")
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(failing)
    assert result.ok is False
    assert any(step.name == "补登" and step.status == StepStatus.FAILED for step in result.steps)
    assert "2026-09-01" not in [step.name for step in result.steps]   # 步骤名稳定，不带日期

    async def no_card(request: httpx.Request) -> httpx.Response:
        called.append(request.url.path)
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json=STREAK_BODY)
        return httpx.Response(200, json={"code": 0, "data": {}})

    await _run(no_card)
    assert EP_MAKEUP_USE not in called

async def test_runner_stops_immediately_on_session_dead():
    """401/403 → 立即停止后续请求（再打只会一路 401）并标记 session_dead。"""
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(401, json={"code": 401, "msg": "登录过期"})

    result = await _run(handler)
    assert result.session_dead is True and result.ok is False
    assert calls == [EP_TRAVEL_STATUS]              # 一个 401 之后不再打任何请求
    assert "重新登录" in result.report

    # 任务阶段失败同理停止
    async def tasks_unauthorized(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        return httpx.Response(403, json={"code": 403, "msg": "过期"})

    calls.clear()
    result2 = await _run(tasks_unauthorized)
    assert result2.session_dead is True
    assert calls == [EP_TRAVEL_STATUS, EP_TRAVEL_CONFIG, EP_TASKS]

async def test_runner_irreversible_switch_skips_dangerous_actions():
    """关闭不可逆动作：仍领礼物，但抽奖/兑换/开盲盒/补登一律不发请求。"""
    called: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        called.append(request.url.path)
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_ARRIVED)
        if request.url.path == EP_TRAVEL_CLAIM:
            return httpx.Response(200, json={"code": 0, "data": {"credit": 10}})
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": [
                {"task_code": "t", "title": "任务", "accept_status": "completed",
                 "reward_credit": 5}]}})
        if request.url.path.endswith("/t/claim"):
            return httpx.Response(200, json={"code": 0, "data": {"credit": 5}})
        return httpx.Response(200, json={"code": 0, "data": {"balance": 5}})

    result = await _run(handler, allow_irreversible=False)
    assert EP_LOTTERY_CHANCES not in called and EP_LOTTERY_DRAW not in called
    assert EP_REDEEM_SUMMARY not in called and EP_REDEEM not in called
    assert EP_BUDDY_QUOTA not in called and EP_BUDDY_OPEN not in called
    # /streak 是只读查询，必须照打（否则关闭不可逆动作的部署静默丢掉连签展示）
    assert EP_MAKEUP_USE not in called and EP_STREAK in called
    # 可逆的仍要做：礼物 + 任务奖
    assert result.credit == 15.0
    skipped = {step.name for step in result.steps if step.status == StepStatus.SKIPPED}
    assert {"补登", "连登兑换", "开盲盒", "Buddy 盲盒"} <= skipped
    assert result.ok is True

async def test_runner_energy_and_streak_display_failures_are_silent():
    """展示字段（能量/连签）取不到时静默：不能因此把成功的领取判成失败。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_ARRIVED)
        if request.url.path == EP_TRAVEL_CLAIM:
            return httpx.Response(200, json={"code": 0, "data": {"credit": 10}})
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(500, content=b"boom")
        if request.url.path == EP_ENERGY:
            return httpx.Response(500, content=b"boom")
        return httpx.Response(200, json={"code": 0, "data": {}})

    # 能量查询失败必须静默；连登状态 500 记失败，但已有成功的领取 → 整体仍成功
    for allow in (True, False):
        result = await _run(handler, allow_irreversible=allow)
        assert result.energy is None
        assert result.credit == 10.0
        assert result.ok is True and result.gained is True
        assert any(step.name == "查连登状态" and step.status == StepStatus.FAILED
                   for step in result.steps)

async def test_runner_unknown_provider_errors_are_attention():
    """非 HTTP 异常（网络中断等）按需要关注处理。"""
    async def broken(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    result = await _run(broken)
    assert result.ok is False
    assert any(step.status == StepStatus.FAILED for step in result.steps)

async def test_runner_reads_streak_even_with_irreversible_disabled():
    """回归：关闭不可逆动作时仍要拿回连签天数（只读查询不该被开关挡住）。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json={"code": 0, "data": {
                "streak": {"days": 5, "makeup_dates": ["2026-09-01"]},
                "makeup_cards": {"balance": 2}}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(handler, allow_irreversible=False)
    assert result.streak_days == 5
    # 用官方术语「连登」：与签到接口的 streak_days 是两个数（同一天 1 vs 5）
    assert "连登 5 天" in result.report
    # 补登仍被开关挡住，只是展示值不再丢
    assert any(step.name == "补登" and step.status == StepStatus.SKIPPED
               for step in result.steps)
    assert not any(step.name == "补登" and step.status == StepStatus.DONE
                   for step in result.steps)

async def test_runner_makeup_leftover_branch_and_missing_card_balance():
    """补登剩余天数提示只在卡还够时输出；回包没给剩余卡数时用本地递减兜底。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json={"code": 0, "data": {
                "streak": {"days": 1, "makeup_dates": ["2026-09-01", "2026-09-02"]},
                "makeup_cards": {"balance": 1}}})       # 只有 1 张卡
        if request.url.path == EP_MAKEUP_USE:
            return httpx.Response(200, json={"code": 0, "data": {}})   # 回包没给卡数
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(handler)
    details = [step.detail for step in result.steps if step.name == "补登"]
    assert details[0] == "2026-09-01（剩 0 张卡）"     # 本地递减兜底
    # 卡已用完（1 - 1 = 0 不大于 MAKEUP_MAX_PER_RUN）→ 不提示还有可补天数
    assert not any("另有" in detail for detail in details)

async def test_runner_skips_claimed_and_unfinished_tasks():
    """已领奖/锁定/已接单未完成三类任务都必须跳过（不重复请求上游）。"""
    accepts: list[object] = []
    claims: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": [
                {"task_code": "t_claimed", "title": "已领", "accept_status": "claimed"},
                {"task_code": "t_locked", "title": "锁定", "accept_status": "", "locked": True},
                {"task_code": "t_doing", "title": "进行中", "accept_status": "accepted",
                 "progress": {"current": 1, "target": 3}},
                {"task_code": "t_progress", "title": "进行中2",
                 "accept_status": "in_progress"},
                {"task_code": "t_new", "title": "新任务", "accept_status": "not_accepted"},
                {"task_code": "t_done", "title": "已完成", "accept_status": "completed"}]}})
        if request.url.path == EP_TASK_ACCEPT:
            accepts.extend(_json.loads(request.content)["task_codes"])
            return httpx.Response(200, json={"code": 0, "data": {"results": []}})
        if request.url.path.endswith("/claim"):
            claims.append(request.url.path)
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(handler)
    # 只有 not_accepted 被接单；accepted/in_progress/claimed/locked 都不碰
    assert accepts == ["t_new"]
    assert claims == ["/v2/activity/growth/tasks/t_done/claim"]   # 只有 completed 领奖
    assert result.ok is True

async def test_runner_depart_without_duration_omits_eta():
    """时长拿不到时不编造数字：只说「已出发」，不显示「? 小时后回」。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {
                "locations": [{"id": 1, "name": "咖啡馆"}]}})
        if request.url.path == EP_TRAVEL_DEPART:
            return httpx.Response(200, json={"code": 0, "data": {"ok": True}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(handler)
    step = next(step for step in result.steps if step.name == "派 Buddy")
    assert step.detail == "去咖啡馆"
    assert "?" not in step.detail

async def test_runner_tier_locked_is_idle_not_session_dead():
    """未解锁档位 403 必须当常态：不能误报「登录态已失效」也不能中止整轮。

    回归：401/403 一律当 session 失效的话，未解锁档会让整轮成长中心被判死。
    """
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_ARRIVED)
        if request.url.path == EP_TRAVEL_CLAIM:
            return httpx.Response(200, json={"code": 0, "data": {"credit": 10}})
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json=STREAK_BODY)
        if request.url.path == EP_REDEEM_SUMMARY:
            return httpx.Response(200, json={"code": 0, "data": {
                "starter_status": "available"}})
        if request.url.path == EP_REDEEM:
            return httpx.Response(403, json={"code": 403, "msg": "连续登录天数不足"})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(handler)
    assert result.session_dead is False
    assert result.ok is True and result.gained is True       # 礼物已领到，仍是成功
    locked = [s for s in result.steps if s.name == "连登兑换"]
    assert locked[0].status == StepStatus.IDLE
    assert "连登天数不足" in locked[0].detail
    # 未解锁不该中止：后续的抽奖查询仍被执行
    assert EP_LOTTERY_CHANCES in calls

async def test_runner_depart_without_buddy_is_idle_and_reportable():
    """还没有 Buddy 时派出失败 → 记为 IDLE 且进汇报（账号状态，不是故障）。

    回归：此前记成 FAILED 并带上「（HTTP 400）」，让新账号的报告里混进一条
    并不需要处理的"失败"，而它与其他任务的 first_buddy 是同一个根因。
    """
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {
                "locations": [{"id": 1, "name": "咖啡馆"}]}})
        if request.url.path == EP_TRAVEL_DEPART:
            return httpx.Response(400, json={"code": 400, "msg": "no active buddy"})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(handler)
    step = next(s for s in result.steps if s.name == "派 Buddy")
    assert step.status == StepStatus.IDLE
    assert "尚未领取 Buddy" in step.detail and "HTTP" not in step.detail
    assert "派 Buddy" in result.report        # 用户需要知道去客户端领 Buddy
    assert result.ok is True

async def test_runner_depart_other_business_rejection_still_idle_not_failure():
    """其他 4xx（如每日名额用完）仍按业务态处理，不误报成故障。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {
                "locations": [{"id": 1, "name": "咖啡馆"}]}})
        if request.url.path == EP_TRAVEL_DEPART:
            return httpx.Response(400, json={"code": 400, "msg": "daily limit reached"})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(handler)
    step = next(s for s in result.steps if s.name == "派 Buddy")
    assert step.status == StepStatus.IDLE and "daily limit reached" in step.detail
    assert result.ok is True

async def test_runner_depart_unexpected_exception_is_recorded():
    """派出遇到非 HTTP 异常（网络中断）→ 记失败，不被 no_buddy 分支吞掉。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {
                "locations": [{"id": 1, "name": "咖啡馆"}]}})
        if request.url.path == EP_TRAVEL_DEPART:
            raise httpx.ConnectError("no route to host")
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(handler)
    step = next(s for s in result.steps if s.name == "派 Buddy")
    assert step.status == StepStatus.FAILED and "ConnectError" in step.detail
    assert result.ok is False
