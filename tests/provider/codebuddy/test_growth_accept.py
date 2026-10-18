"""CodeBuddy 成长 runner 接单与领奖：批量结果分类、领取失败与会话失效。"""

from __future__ import annotations

import httpx

from src.provider.base import StepStatus
from src.provider.codebuddy.client import CodeBuddyCredential
from src.provider.codebuddy.growth import (
    EP_TASK_ACCEPT,
    EP_TASKS,
    EP_TRAVEL_CONFIG,
    EP_TRAVEL_STATUS,
)
from src.provider.codebuddy.growth_runner import ACCEPT_BATCH_SIZE
from tests.provider.codebuddy._growth_support import TRAVEL_IDLE
from tests.provider.codebuddy._growth_support import runner as _runner


async def _run(handler, *, allow_irreversible: bool = True):
    runner = _runner(handler, allow_irreversible=allow_irreversible)
    return await runner.run(CodeBuddyCredential(bearer_token="t"))

async def test_runner_task_accept_failure_stops_on_session_dead():
    """任务 accept 返回 401 → 立即停止该轮（不再处理后续任务）。"""
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": [
                {"task_code": "t1", "title": "任务一", "accept_status": ""},
                {"task_code": "t2", "title": "任务二", "accept_status": ""}]}})
        return httpx.Response(401, json={"code": 401, "msg": "过期"})

    result = await _run(handler)
    assert result.session_dead is True
    assert calls.count(EP_TASK_ACCEPT) == 1          # 第一个任务 401 后不再打第二个

async def test_runner_accept_batches_and_reports_per_item_errors():
    """接单分批提交 + 逐条读 results（失败必须报出来，不静默）。"""
    batches: list[list] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": [
                {"task_code": f"t{i}", "title": f"任务{i}",
                 "accept_status": "not_accepted"} for i in range(25)]}})
        if request.url.path == EP_TASK_ACCEPT:
            batch = _json.loads(request.content)["task_codes"]
            batches.append(batch)
            return httpx.Response(200, json={"code": 0, "data": {"results": [
                {"task_code": code,
                 "status": "error" if code == "t0" else "ok",
                 "message": "prerequisite not met: first_buddy" if code == "t0" else None}
                for code in batch]}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(handler)
    assert [len(b) for b in batches] == [ACCEPT_BATCH_SIZE, 25 - ACCEPT_BATCH_SIZE]
    # t0 是前置条件未满足：归并成一条汇总，不是逐条 FAILED 刷屏
    assert result.failed == []
    blocked = [s for s in result.steps if s.name == "接单受阻"]
    assert len(blocked) == 1 and "1 个任务" in blocked[0].detail
    # 24 个任务接单成功：每条各一条 DONE + 一条「接单完成」汇总
    assert sum(1 for s in result.steps if s.name == "领取任务") == 24
    assert any(s.name == "接单完成" and "24 个" in s.detail for s in result.steps)

async def test_runner_accept_falls_back_when_results_missing():
    """上游没给 results 时按整体状态判断，不编造具体错误。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": [
                {"task_code": "t1", "title": "任务1", "accept_status": "not_accepted"}]}})
        if request.url.path == EP_TASK_ACCEPT:
            return httpx.Response(200, json={"code": 0, "data": {}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(handler)
    assert any(s.name == "领取任务" and s.status == StepStatus.DONE for s in result.steps)

async def test_runner_accept_http_failure_is_recorded():
    """接单整体失败（5xx）记失败，但不阻塞独立的领奖操作。

    接单与领奖是两个独立端点，且领奖走 accept_status==completed（与接单无关）；
    一次接单故障不该让已经能领的奖励烂在账上。
    """
    claims: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": [
                {"task_code": "t1", "title": "任务1", "accept_status": "not_accepted"},
                {"task_code": "t2", "title": "任务2", "accept_status": "completed"}]}})
        if request.url.path == EP_TASK_ACCEPT:
            return httpx.Response(500, content=b"boom")
        if request.url.path.endswith("/claim"):
            claims.append(request.url.path)
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(handler)
    assert any(s.name == "接单" and s.status == StepStatus.FAILED for s in result.steps)
    assert claims == ["/v2/activity/growth/tasks/t2/claim"]   # 领奖照常进行

async def test_runner_claim_already_claimed_is_idle_and_not_counted():
    """已领过（already_claimed）不能重复计分。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": [
                {"task_code": "t1", "title": "已完成", "accept_status": "completed",
                 "reward_credit": 300}]}})
        if request.url.path.endswith("/t1/claim"):
            return httpx.Response(200, json={"code": 0, "data": {
                "already_claimed": True, "credit": 300}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(handler)
    assert result.credit is None                 # 没重复计分
    step = next(s for s in result.steps if s.name == "领任务奖")
    assert step.status == StepStatus.IDLE and "已领过" in step.detail

async def test_runner_claim_failure_is_recorded_and_continues():
    """单条领奖失败记失败，不影响后续任务。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": [
                {"task_code": "bad", "title": "坏", "accept_status": "completed"},
                {"task_code": "good", "title": "好", "accept_status": "completed",
                 "reward_credit": 100}]}})
        if request.url.path.endswith("/bad/claim"):
            return httpx.Response(500, content=b"boom")
        if request.url.path.endswith("/good/claim"):
            return httpx.Response(200, json={"code": 0, "data": {"credit": 100}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(handler)
    failed = [s for s in result.steps if s.status == StepStatus.FAILED]
    # 步骤名保持稳定（"领任务奖"），任务名在 detail 里 —— 前端按 name 归组渲染
    assert len(failed) == 1 and failed[0].name == "领任务奖"
    assert "「坏」" in failed[0].detail and "HTTP 500" in failed[0].detail
    assert result.credit == 100.0                # 好的那条照常计分

async def test_runner_claim_session_dead_stops_round():
    """领奖遇 401 → 停止整轮（后续任务不再尝试）。"""
    claims: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": [
                {"task_code": "t1", "title": "一", "accept_status": "completed"},
                {"task_code": "t2", "title": "二", "accept_status": "completed"}]}})
        if request.url.path.endswith("/claim"):
            claims.append(request.url.path)
            return httpx.Response(401, json={"code": 401, "msg": "过期"})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(handler)
    assert result.session_dead is True
    assert len(claims) == 1          # 第一个 401 后不再打第二个

async def test_runner_accept_classifies_three_kinds_of_results():
    """接单结果分三类：正常应答（不需要接单）/ 前置条件受阻 / 真失败。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": [
                {"task_code": "ok1", "title": "好任务", "accept_status": "not_accepted"},
                {"task_code": "no_accept", "title": "不需要接单",
                 "accept_status": "not_accepted"},
                {"task_code": "gated1", "title": "受门控甲", "accept_status": "not_accepted"},
                {"task_code": "gated2", "title": "受门控乙", "accept_status": "not_accepted"},
                {"task_code": "broken", "title": "真坏了", "accept_status": "not_accepted"}]}})
        if request.url.path == EP_TASK_ACCEPT:
            return httpx.Response(200, json={"code": 0, "data": {"results": [
                {"task_code": "ok1", "status": "ok"},
                # 正常应答：该任务不需要接单，既不算成功也不算失败（不刷屏）
                {"task_code": "no_accept", "status": "error",
                 "message": "task does not require acceptance"},
                # 两个任务共享同一前置条件 → 归并成一条
                {"task_code": "gated1", "status": "error",
                 "message": "prerequisite not met: first_buddy"},
                {"task_code": "gated2", "status": "error",
                 "message": "prerequisite not met: first_buddy"},
                # 真正需要人看的失败
                {"task_code": "broken", "status": "error", "message": "服务端拒绝"}]}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(handler)
    names = [s.name for s in result.steps]
    # 「不需要接单」不产生任何步骤（不当失败、不当成功）
    assert "「不需要接单」" not in result.report
    # 「领取任务」只出现 1 次（ok1）；「真坏了」走的是 FAILED 且 detail 带原文，
    # 「不需要接单」不产生步骤
    done_tasks = [s for s in result.steps
                  if s.name == "领取任务" and s.status == StepStatus.DONE]
    assert len(done_tasks) == 1 and "好任务" in done_tasks[0].detail
    # 同一前置条件的两个任务归并成一条，且进汇报
    blocked = [s for s in result.steps if s.name == "接单受阻"]
    assert len(blocked) == 1 and "2 个任务" in blocked[0].detail
    assert blocked[0].reportable is True
    assert "接单受阻" in result.report
    # 真失败逐条报，并让整轮失败（无成功领取时不伪装成功）
    assert any(s.name == "领取任务" and s.status == StepStatus.FAILED
               and "服务端拒绝" in s.detail for s in result.steps)
    assert result.ok is True            # ok1 接单成功 → 部分成功仍算成功
    assert "接单完成" in names           # 有接单成功的汇总行
