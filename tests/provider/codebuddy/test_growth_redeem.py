"""CodeBuddy 成长 runner 兑换流程：档位重试、业务拒绝与异常归类。"""

from __future__ import annotations

import httpx

from src.provider.base import StepStatus
from src.provider.codebuddy.client import CodeBuddyCredential
from src.provider.codebuddy.growth import (
    EP_REDEEM,
    EP_REDEEM_SUMMARY,
    EP_STREAK,
    EP_TASKS,
    EP_TRAVEL_CLAIM,
    EP_TRAVEL_CONFIG,
    EP_TRAVEL_STATUS,
)
from tests.provider.codebuddy._growth_support import STREAK_BODY, TRAVEL_ARRIVED, TRAVEL_IDLE
from tests.provider.codebuddy._growth_support import runner as _runner


async def _run(handler, *, allow_irreversible: bool = True):
    runner = _runner(handler, allow_irreversible=allow_irreversible)
    return await runner.run(CodeBuddyCredential(bearer_token="t"))

async def test_runner_redeem_retries_with_tier_name_on_unknown_tier():
    """tier 传档位标识 "7d"；被判 unknown tier 时退回天数重试（参数校验阶段，安全）。"""
    tiers: list[object] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json=STREAK_BODY)
        if request.url.path == EP_REDEEM_SUMMARY:
            return httpx.Response(200, json={"code": 0, "data": {
                "starter_status": "unlocked"}})
        if request.url.path == EP_REDEEM:
            import json as _json

            tier = _json.loads(request.content)["tier"]
            tiers.append(tier)
            # 模拟「接口改回收天数」：档位标识被拒，退化成天数才成功
            if isinstance(tier, str):
                return httpx.Response(400, json={"code": 400, "msg": "unknown tier"})
            return httpx.Response(200, json={"code": 0, "data": {"credit_granted": 2}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(handler)
    assert tiers == ["7d", 7]                        # 先档位标识，失败后退天数
    assert any(step.name == "连登兑换" and step.status == StepStatus.DONE
               for step in result.steps)

async def test_runner_redeem_business_rejection_and_hard_failure():
    """未解锁的 400 不重试也不算硬失败；5xx 记失败。"""
    tiers: list[object] = []

    async def rejected(request: httpx.Request) -> httpx.Response:
        if request.url.path in (EP_TRAVEL_STATUS,):
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json=STREAK_BODY)
        if request.url.path == EP_REDEEM_SUMMARY:
            return httpx.Response(200, json={"code": 0, "data": {
                "starter_status": "unlocked"}})
        if request.url.path == EP_REDEEM:
            import json as _json

            tiers.append(_json.loads(request.content)["tier"])
            return httpx.Response(400, json={"code": 400, "msg": "invalid request"})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(rejected)
    assert tiers == ["7d"]                           # 业务拒绝不重试
    assert result.ok is True                         # 也不算硬失败

    async def broken(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json=STREAK_BODY)
        if request.url.path == EP_REDEEM_SUMMARY:
            return httpx.Response(200, json={"code": 0, "data": {
                "starter_status": "unlocked"}})
        if request.url.path == EP_REDEEM:
            return httpx.Response(503, content=b"boom")
        return httpx.Response(200, json={"code": 0, "data": {}})

    result2 = await _run(broken)
    assert result2.ok is False
    assert any("连登兑换" in step.name for step in result2.failed)

async def test_runner_travel_and_redeem_exception_paths():
    """非 HTTP 异常（协议违规/网络）与「领取失败也算硬失败」的分支。"""
    # 领旅行礼物失败：即使 4xx 也标 attention（礼物没领到会过期）
    async def claim_rejected(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_ARRIVED)
        if request.url.path == EP_TRAVEL_CLAIM:
            return httpx.Response(400, json={"code": 400, "msg": "记录已失效"})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(claim_rejected)
    assert result.ok is False
    failed = [step for step in result.steps if step.name == "领旅行礼物"]
    assert failed[0].status == StepStatus.FAILED and "记录已失效" in failed[0].detail

    # 协议违规（上游改版）→ 计为需要关注的失败
    async def protocol_violation(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json={"code": 0, "data": {"state": "idle"}})
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, content=b"<html>")
        return httpx.Response(200, json={"code": 0, "data": {}})

    result2 = await _run(protocol_violation)
    assert result2.ok is False
    hit = next(step for step in result2.steps if step.name == "查旅行地点")
    assert "响应结构异常" in hit.detail

    # 连登兑换：首次 400 非 tier 问题 → 不重试、算业务态
    async def redeem_non_tier(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json=STREAK_BODY)
        if request.url.path == EP_REDEEM_SUMMARY:
            return httpx.Response(200, json={"code": 0, "data": {"starter_status": "ready"}})
        if request.url.path == EP_REDEEM:
            return httpx.Response(400, json={"code": 400, "msg": "not enough days"})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result3 = await _run(redeem_non_tier)
    assert result3.ok is True
    assert any("连登兑换" in step.name and "not enough days" in step.detail
               for step in result3.steps)

    # 连登兑换：tier 重试后仍失败（如 401） → session_dead 并停止
    async def redeem_retry_unauthorized(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json=STREAK_BODY)
        if request.url.path == EP_REDEEM_SUMMARY:
            return httpx.Response(200, json={"code": 0, "data": {"starter_status": "ready"}})
        if request.url.path == EP_REDEEM:
            import json as _json

            tier = _json.loads(request.content)["tier"]
            if isinstance(tier, int):
                return httpx.Response(400, json={"code": 400, "msg": "unknown tier"})
            return httpx.Response(403, json={"code": 403, "msg": "过期"})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result4 = await _run(redeem_retry_unauthorized)
    assert result4.session_dead is True

    # 连登兑换：tier 重试成功 → 正常记奖励
    async def redeem_retry_ok(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json=STREAK_BODY)
        if request.url.path == EP_REDEEM_SUMMARY:
            return httpx.Response(200, json={"code": 0, "data": {"starter_status": "ready"}})
        if request.url.path == EP_REDEEM:
            import json as _json

            if isinstance(_json.loads(request.content)["tier"], int):
                return httpx.Response(400, json={"code": 400, "msg": "unknown tier"})
            return httpx.Response(200, json={"code": 0, "data": {"credit": 3, "energy": 1}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result5 = await _run(redeem_retry_ok)
    assert result5.ok is True and result5.credit == 3.0

async def test_runner_redeem_unexpected_exception_is_attention():
    """连登兑换遇到非 HTTP 异常（网络中断）按需要关注处理。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json=STREAK_BODY)
        if request.url.path == EP_REDEEM_SUMMARY:
            return httpx.Response(200, json={"code": 0, "data": {"starter_status": "ready"}})
        if request.url.path == EP_REDEEM:
            raise httpx.ConnectError("no route")
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(handler)
    assert result.ok is False
    assert any("连登兑换" in step.name and step.status == StepStatus.FAILED
               for step in result.steps)

async def test_runner_redeem_retry_non_session_failure_is_attention():
    """tier 换写法重试后仍是普通失败（500）→ 记失败但不断整轮。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json=STREAK_BODY)
        if request.url.path == EP_REDEEM_SUMMARY:
            return httpx.Response(200, json={"code": 0, "data": {
                "starter_status": "ready", "advanced_status": "ready"}})
        if request.url.path == EP_REDEEM:
            if isinstance(_json.loads(request.content)["tier"], int):
                return httpx.Response(400, json={"code": 400, "msg": "unknown tier"})
            return httpx.Response(500, content=b"boom")
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(handler)
    assert result.ok is False
    assert sum(1 for step in result.steps if "连登兑换" in step.name) == 2
    assert result.session_dead is False

async def test_runner_redeem_unexpected_error_continues_to_next_tier():
    """一个档位遇到网络异常后，下一档仍会被尝试（互不影响）。"""
    tiers: list[object] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json=STREAK_BODY)
        if request.url.path == EP_REDEEM_SUMMARY:
            return httpx.Response(200, json={"code": 0, "data": {
                "starter_status": "ready", "advanced_status": "ready"}})
        if request.url.path == EP_REDEEM:
            tier = _json.loads(request.content)["tier"]
            tiers.append(tier)
            if tier == "7d":
                raise httpx.ConnectError("no route")
            return httpx.Response(200, json={"code": 0, "data": {"credit_granted": 50}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(handler)
    assert tiers == ["7d", "14d"]                    # 第一档网络异常后仍尝试第二档
    assert result.credit == 50.0

async def test_runner_redeem_session_dead_stops_round():
    """连登兑换遇 401（非 tier 问题）→ 停止整轮，不再尝试下一档。"""
    tiers: list[object] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json=STREAK_BODY)
        if request.url.path == EP_REDEEM_SUMMARY:
            return httpx.Response(200, json={"code": 0, "data": {
                "starter_status": "ready", "advanced_status": "ready"}})
        if request.url.path == EP_REDEEM:
            tiers.append(_json.loads(request.content)["tier"])
            return httpx.Response(401, json={"code": 401, "msg": "过期"})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(handler)
    assert result.session_dead is True
    assert tiers == ["7d"]                           # 401 后不再尝试进阶档

async def test_runner_redeem_retry_session_dead_stops_round():
    """兜底重试（退天数）遇 401 → 置 session_dead 并停止整轮。"""
    tiers: list[object] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json=STREAK_BODY)
        if request.url.path == EP_REDEEM_SUMMARY:
            return httpx.Response(200, json={"code": 0, "data": {
                "starter_status": "available", "advanced_status": "available"}})
        if request.url.path == EP_REDEEM:
            tier = _json.loads(request.content)["tier"]
            tiers.append(tier)
            if isinstance(tier, str):
                return httpx.Response(400, json={"code": 400, "msg": "unknown tier"})
            return httpx.Response(401, json={"code": 401, "msg": "过期"})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(handler)
    assert result.session_dead is True
    assert tiers == ["7d", 7]        # 重试一次后判定失效，不再试进阶档

async def test_runner_redeem_non_tier_business_error_continues():
    """非 tier 的 400 业务拒绝（未解锁）→ 记 IDLE 不算失败，继续下一档。"""
    tiers: list[object] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json=STREAK_BODY)
        if request.url.path == EP_REDEEM_SUMMARY:
            return httpx.Response(200, json={"code": 0, "data": {
                "starter_status": "available", "advanced_status": "available"}})
        if request.url.path == EP_REDEEM:
            tier = _json.loads(request.content)["tier"]
            tiers.append(tier)
            if tier == "7d":
                return httpx.Response(400, json={"code": 400, "msg": "invalid request"})
            return httpx.Response(200, json={"code": 0, "data": {"credit_granted": 50}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(handler)
    assert tiers == ["7d", "14d"]        # 第一档业务拒绝后仍尝试第二档
    assert result.ok is True and result.credit == 50.0
    steps = [s for s in result.steps if s.name == "连登兑换"]
    assert steps[0].status == StepStatus.IDLE and "invalid request" in steps[0].detail
    assert steps[1].status == StepStatus.DONE and steps[1].credit == 50.0

async def test_runner_redeem_non_tier_error_is_idle_then_continues():
    """非 tier 的 400（未解锁）记 IDLE 不算失败，且继续尝试下一档。

    覆盖 elif 分支（GrowthRejected 但既非 tier_locked 也非 unknown_tier）。
    """
    tiers: list[object] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json=STREAK_BODY)
        if request.url.path == EP_REDEEM_SUMMARY:
            return httpx.Response(200, json={"code": 0, "data": {
                "starter_status": "available", "advanced_status": "available"}})
        if request.url.path == EP_REDEEM:
            tier = _json.loads(request.content)["tier"]
            tiers.append(tier)
            if tier == "7d":
                return httpx.Response(400, json={"code": 400, "msg": "未解锁"})
            return httpx.Response(200, json={"code": 0, "data": {"credit_granted": 50}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(handler)
    assert tiers == ["7d", "14d"]
    assert result.ok is True and result.credit == 50.0
    steps = [s for s in result.steps if s.name == "连登兑换"]
    assert steps[0].status == StepStatus.IDLE
    assert steps[0].detail == "「入门」未解锁（HTTP 400）"
    assert steps[1].status == StepStatus.DONE and steps[1].credit == 50.0

async def test_runner_redeem_direct_401_stops_round():
    """兑换首次就 401（未经兜底重试）→ 置 session_dead 并停止整轮。"""
    tiers: list[object] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json=STREAK_BODY)
        if request.url.path == EP_REDEEM_SUMMARY:
            return httpx.Response(200, json={"code": 0, "data": {
                "starter_status": "available", "advanced_status": "available"}})
        if request.url.path == EP_REDEEM:
            tiers.append(_json.loads(request.content)["tier"])
            return httpx.Response(401, json={"code": 401, "msg": "登录过期"})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(handler)
    assert result.session_dead is True
    assert tiers == ["7d"]           # 401 后不再尝试下一档

async def test_runner_redeem_retry_non_session_error_continues():
    """兜底重试（退天数）后遇普通失败 → 记失败并继续下一档，不中止整轮。"""
    tiers: list[object] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json=STREAK_BODY)
        if request.url.path == EP_REDEEM_SUMMARY:
            return httpx.Response(200, json={"code": 0, "data": {
                "starter_status": "available", "advanced_status": "available"}})
        if request.url.path == EP_REDEEM:
            tier = _json.loads(request.content)["tier"]
            tiers.append(tier)
            if isinstance(tier, str):
                return httpx.Response(400, json={"code": 400, "msg": "unknown tier"})
            if tier == 7:
                return httpx.Response(500, content=b"boom")   # 重试仍是普通失败
            return httpx.Response(200, json={"code": 0, "data": {"credit_granted": 50}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(handler)
    # 两档都先试档位标识、被判 unknown tier 后退天数（第二档重试成功）
    assert tiers == ["7d", 7, "14d", 14]
    assert result.session_dead is False
    assert result.credit == 50.0
    steps = [s for s in result.steps if s.name == "连登兑换"]
    assert steps[0].status == StepStatus.FAILED and "HTTP 500" in steps[0].detail
