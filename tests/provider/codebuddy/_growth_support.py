"""CodeBuddy 成长中心测试共享响应样本与装配助手。

`TRAVEL_ARRIVED`(4) / `TRAVEL_IDLE`(5) / `STREAK_BODY`(5) / `REDEEM_LOCKED`(3) /
`TASKS_BODY`(2) 曾在该渠道及 API/任务测试里各写一份逐字节等价的定义；
`_client`(6) / `_scripted`(4) / `_runner`(4) 同样各写多份，收敛到这里。
"""

from __future__ import annotations

import httpx

from src.provider.base import GrowthResult, GrowthStep, StepStatus
from src.provider.codebuddy.growth import CodeBuddyGrowth
from src.provider.codebuddy.growth_runner import GrowthRunner
from tests.helpers.http import mock_async_client

__all__ = [
    "REDEEM_LOCKED",
    "STREAK_BODY",
    "TASKS_BODY",
    "TRAVEL_ARRIVED",
    "TRAVEL_IDLE",
    "StubGrowthProvider",
    "client",
    "runner",
    "scripted",
]

TRAVEL_ARRIVED = {"code": 0, "msg": "OK", "data": {
    "state": "arrived", "buddy_id": 7485932, "record_id": 277990,
    "location": {"id": 1, "code": "coffee", "name": "咖啡馆", "duration_hours": 3},
    "depart_at": 1785255159, "arrive_at": 1785265959, "server_now": 1789820066,
    "daily_limit_reached": False, "duration_hours": 3, "reward_credit": 10}}

TRAVEL_IDLE = {"code": 0, "data": {
    "state": "idle", "location": {"id": 1, "name": "咖啡馆"},
    "daily_limit_reached": False}}

TASKS_BODY = {"code": 0, "data": {"tasks": [
    # 未领取：必须先 accept 一次（进度从领取才计）
    {"task_code": "t_new", "title": "新任务", "accept_status": "",
     "progress": {"current": 0, "target": 1}, "reward_credit": 300, "locked": False},
    # 已接单未完成：跳过，不重复领取
    {"task_code": "t_doing", "title": "进行中", "accept_status": "accepted",
     "progress": {"current": 0, "target": 3}},
    # 已接单且完成：领奖
    {"task_code": "t_done", "title": "已完成", "accept_status": "accepted",
     "progress": {"current": 1, "target": 1}, "reward_credit": 50, "reward_energy": 5},
    # 已领奖 / 锁定：跳过
    {"task_code": "t_claimed", "title": "已领", "accept_status": "claimed"},
    {"task_code": "t_locked", "title": "锁定", "accept_status": "", "locked": True},
    # progress 缺失：target 按 1，current 0 → 未完成
    {"task_code": "t_bare", "title": "无进度", "accept_status": ""},
]}}

STREAK_BODY = {"code": 0, "data": {
    "streak": {"days": 0, "month_total_days": 3, "makeup_dates": []},
    "makeup_cards": {"balance": 0, "max": 4},
    "redemption_status": {"tier_7d_status": "locked", "tier_14d_status": "locked",
                          "tier_28d_status": "locked"}}}

REDEEM_LOCKED = {"code": 0, "data": {
    "starter_status": "locked", "advanced_status": "locked", "legendary_status": "locked",
    "starter_count": 0, "total_consumed": 0}}


def client(handler) -> httpx.AsyncClient:
    """把 handler 包成无超时的 `AsyncClient`。"""
    return mock_async_client(handler)


def scripted(handler) -> CodeBuddyGrowth:
    """用 handler 造一个 `CodeBuddyGrowth`（固定测试主机）。"""
    return CodeBuddyGrowth("https://e", client=client(handler))


def runner(handler, *, allow_irreversible: bool = True) -> GrowthRunner:
    """用 handler 造一个 `GrowthRunner`。"""
    return GrowthRunner(scripted(handler), allow_irreversible=allow_irreversible)


class StubGrowthProvider:
    """可控的成长中心 provider：记录调用、可注入结果或异常。"""

    id = "codebuddy"

    def __init__(self, *, result=None, error=None, scope="acct") -> None:
        self.calls = 0
        self._result = result or GrowthResult(
            ok=True, report="领旅行礼物：+10 积分", credit=10.0,
            steps=[GrowthStep("领旅行礼物", StepStatus.DONE, "+10 积分")])
        self._error = error
        self._scope = scope
        self.allow_flags: list[bool] = []

    async def growth(self, _data, *, allow_irreversible: bool = True):
        self.calls += 1
        self.allow_flags.append(allow_irreversible)
        if self._error is not None:
            raise self._error
        return self._result

    def checkin_scope(self, data):
        # 与真实 provider 同款语义：身份未知（含空 scope）时返回空串，
        # 由任务回落到 credential_id（绝不共享）
        identity = data.get("account_uid", "")
        if not self._scope or not identity:
            return ""
        return f"{self._scope}|{identity}"
