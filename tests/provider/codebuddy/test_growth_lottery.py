"""CodeBuddy 成长 runner 抽奖/buddy box 与任务装配接线。"""

from __future__ import annotations

import httpx

from src.config import Settings
from src.provider.base import StepStatus
from src.provider.codebuddy.client import CodeBuddyCredential
from src.provider.codebuddy.growth import (
    EP_BUDDY_OPEN,
    EP_BUDDY_QUOTA,
    EP_LOTTERY_CHANCES,
    EP_LOTTERY_DRAW,
    EP_REDEEM_SUMMARY,
    EP_STREAK,
    EP_TASKS,
    EP_TRAVEL_CONFIG,
    EP_TRAVEL_STATUS,
)
from src.tasks.growth import GrowthTask
from src.tasks.runner import TaskRunner, build_runner
from tests.helpers.constants import SECRET
from tests.provider.codebuddy._growth_support import REDEEM_LOCKED, STREAK_BODY, TRAVEL_IDLE
from tests.provider.codebuddy._growth_support import StubGrowthProvider as _StubGrowthProvider
from tests.provider.codebuddy._growth_support import runner as _runner


async def _run(handler, *, allow_irreversible: bool = True):
    runner = _runner(handler, allow_irreversible=allow_irreversible)
    return await runner.run(CodeBuddyCredential(bearer_token="t"))



async def _noop(*_args, **_kwargs):
    return None

async def test_runner_lottery_no_chance_and_failure_paths():
    """抽奖次数为 0 → 完全不请求 draw；draw 失败分业务态与硬失败。"""
    called: list[str] = []

    async def no_chance(request: httpx.Request) -> httpx.Response:
        called.append(request.url.path)
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json=STREAK_BODY)
        if request.url.path == EP_REDEEM_SUMMARY:
            return httpx.Response(200, json=REDEEM_LOCKED)
        if request.url.path == EP_LOTTERY_CHANCES:
            return httpx.Response(200, json={"code": 0, "data": {"balance": 0}})
        if request.url.path == EP_BUDDY_QUOTA:
            return httpx.Response(200, json={"code": 0, "data": {"affordable": 0}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(no_chance)
    assert EP_LOTTERY_DRAW not in called and EP_BUDDY_OPEN not in called
    assert result.ok is True

    async def draw_business_error(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json=STREAK_BODY)
        if request.url.path == EP_REDEEM_SUMMARY:
            return httpx.Response(200, json=REDEEM_LOCKED)
        if request.url.path == EP_LOTTERY_CHANCES:
            return httpx.Response(200, json={"code": 0, "data": {"balance": 1}})
        if request.url.path == EP_LOTTERY_DRAW:
            return httpx.Response(400, json={"code": 400,
                                             "msg": "insufficient lottery chance balance"})
        if request.url.path == EP_BUDDY_QUOTA:
            return httpx.Response(200, json={"code": 0, "data": {"affordable": 0}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result2 = await _run(draw_business_error)
    assert result2.ok is True                        # 次数为 0 是常态，不是故障
    assert result2.gained is False

    async def draw_broken(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json=STREAK_BODY)
        if request.url.path == EP_REDEEM_SUMMARY:
            return httpx.Response(200, json=REDEEM_LOCKED)
        if request.url.path == EP_LOTTERY_CHANCES:
            return httpx.Response(200, json={"code": 0, "data": {"balance": 1}})
        if request.url.path == EP_LOTTERY_DRAW:
            return httpx.Response(500, content=b"boom")
        if request.url.path == EP_BUDDY_QUOTA:
            return httpx.Response(200, json={"code": 0, "data": {"affordable": 0}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result3 = await _run(draw_broken)
    assert result3.ok is False

async def test_runner_lottery_prize_shapes_and_buddy_box_failure():
    """奖品名可能是对象/数字；Buddy 盲盒失败要记失败。"""
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
            return httpx.Response(200, json=REDEEM_LOCKED)
        if request.url.path == EP_LOTTERY_CHANCES:
            return httpx.Response(200, json={"code": 0, "data": {"balance": 1}})
        if request.url.path == EP_LOTTERY_DRAW:
            # prize 是数字 + require_address：不能因类型报错而丢掉整次中奖
            return httpx.Response(200, json={"code": 0, "data": {
                "prize": 3, "require_address": True}})
        if request.url.path == EP_BUDDY_QUOTA:
            return httpx.Response(200, json={"code": 0, "data": {
                "affordable": 9, "max_open_count": 3}})
        if request.url.path == EP_BUDDY_OPEN:
            return httpx.Response(400, json={"code": 400, "msg": "能量不足"})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(handler)
    draw = [step for step in result.steps if step.name == "开盲盒"
            and step.status == StepStatus.DONE]
    assert draw[0].detail == "3（实物奖，需到成长中心填写收件信息）"
    box = [step for step in result.steps if step.name == "Buddy 盲盒"]
    # 能量不足是业务规则（上限夹取后仍被上游拒）→ 记 IDLE 不算硬失败
    assert box[0].status == StepStatus.IDLE and result.ok is True
    assert "能量不足" in box[0].detail

    async def broken_box(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json=STREAK_BODY)
        if request.url.path == EP_REDEEM_SUMMARY:
            return httpx.Response(200, json=REDEEM_LOCKED)
        if request.url.path == EP_LOTTERY_CHANCES:
            return httpx.Response(200, json={"code": 0, "data": {"balance": 0}})
        if request.url.path == EP_BUDDY_QUOTA:
            return httpx.Response(200, json={"code": 0, "data": {"affordable": 1}})
        if request.url.path == EP_BUDDY_OPEN:
            return httpx.Response(500, content=b"boom")
        return httpx.Response(200, json={"code": 0, "data": {}})

    result2 = await _run(broken_box)
    assert result2.ok is False
    assert any(step.name == "Buddy 盲盒" and step.status == StepStatus.FAILED
               for step in result2.steps)

async def test_runner_buddy_box_reports_default_name_and_gain_flags():
    """Buddy 名字缺失时回落「新 Buddy」；gained/failed 属性口径稳定。"""
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
            return httpx.Response(200, json=REDEEM_LOCKED)
        if request.url.path == EP_LOTTERY_CHANCES:
            return httpx.Response(200, json={"code": 0, "data": {"balance": 0}})
        if request.url.path == EP_BUDDY_QUOTA:
            return httpx.Response(200, json={"code": 0, "data": {"affordable": 1}})
        if request.url.path == EP_BUDDY_OPEN:
            return httpx.Response(200, json={"code": 0, "data": {"buddies": [1, 2]}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(handler)
    assert result.gained is True and result.failed == []
    box = next(step for step in result.steps if step.name == "Buddy 盲盒")
    assert box.detail == "×1（新 Buddy）"        # bundles 是列表 → 不是 str → 回落

def test_build_runner_wires_growth_task(growth_repo):
    """build_runner 传入 events 仓库时装配成长中心；不传则不装配（无落库目标）。"""
    credentials, events = growth_repo
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR="./data",
                        GROWTH_INTERVAL_MINUTES=30, GROWTH_IRREVERSIBLE_ACTIONS=False)
    runner = build_runner(credentials, {}, None, settings, growth_events=events)
    assert runner._growth is not None
    assert runner._growth._allow_irreversible is False
    assert runner._growth_interval == 1800
    assert build_runner(credentials, {}, None, settings)._growth is None

def test_build_runner_clamps_growth_interval(growth_repo):
    """成长中心间隔下限 5 分钟：请求量比签到大一个量级，更密只会撞风控。"""
    credentials, events = growth_repo
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR="./data",
                        GROWTH_INTERVAL_MINUTES=0)
    runner = build_runner(credentials, {}, None, settings, growth_events=events)
    assert runner._growth_interval == 300

async def test_runner_sync_growth_and_loop_registration(growth_repo):
    """成长中心循环被注册进 asyncio 任务列表，_sync_growth 委托给任务对象。"""
    credentials, events = growth_repo
    provider = _StubGrowthProvider()
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    task = GrowthTask(credentials, {"codebuddy": provider}, events)
    runner = TaskRunner(quota_probe=type("Q", (), {"run_once": staticmethod(_noop)})(),
                        checkin=type("C", (), {"due": staticmethod(lambda: False)})(),
                        growth=task, refresh=type("R", (), {"run_once": _noop})(),
                        retention=type("T", (), {"run_once": staticmethod(lambda: None)})())
    await runner._sync_growth()
    assert provider.calls == 1

async def test_runner_lottery_prize_missing_and_no_address_flag():
    """奖品字段全缺失 → 显示「未知」；没有实物标志就不追加提醒。"""
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
            return httpx.Response(200, json=REDEEM_LOCKED)
        if request.url.path == EP_LOTTERY_CHANCES:
            return httpx.Response(200, json={"code": 0, "data": {"balance": 1}})
        if request.url.path == EP_LOTTERY_DRAW:
            return httpx.Response(200, json={"code": 0, "data": {"need_address": False}})
        return httpx.Response(200, json={"code": 0, "data": {"affordable": 0}})

    result = await _run(handler)
    draw = next(step for step in result.steps if step.name == "开盲盒")
    assert draw.detail == "未知"
    assert "实物奖" not in draw.detail
