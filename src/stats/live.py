"""进程内逐请求事件广播（供 `GET /api/stats/events/stream` 的 SSE 实时流）。

为什么不轮询数据库：明细写入是**本进程**完成的（单进程部署，SQLite），写入点
最清楚「刚刚发生了什么」。从 `StatsCollector.record` 直接广播，事件在事务提交
后立刻到达订阅者，不必让每个打开统计页的浏览器每秒查一次 `usage_events`。

为什么不复用 `asyncio.Queue`：慢客户端（标签页被挂起、网络卡）不能拖住写入
路径，也不能让缓冲无界增长。这里用有界 deque + `Event` 唤醒：满了丢**最旧**
的一条——实时流看的是「现在」，丢历史比丢最新更可接受。

**只广播脱敏字段**：`UsageEvent` 本身就不含提示词/正文/请求头（见
`stats/collector.py` 顶部纪律），这里额外只补一个凭证昵称（且仅对
admin/operator 解析）。订阅者拿不到比 `/api/stats/events` 更多的东西。
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Any

# 单个订阅者的缓冲上限：正常浏览器每秒最多收到几条，200 足够吸收短暂卡顿；
# 再多说明客户端已经跟不上，保留最新的才有意义。
MAX_BUFFERED = 200

# 并发订阅上限：每个订阅者占一个长连接 + 一个 deque。统计页是低频入口，
# 8 个足够（同一用户开多个标签页也算在内），超出直接 429 而不是静默拖垮服务。
MAX_SUBSCRIBERS = 8


class TooManySubscribers(Exception):
    """订阅数已达上限（端点据此回 429，不排队、不丢弃已有订阅）。"""


@dataclass(slots=True)
class Subscription:
    """一个订阅者的缓冲与唤醒信号（由 `LiveEventHub` 创建与回收）。"""

    events: deque[dict[str, Any]] = field(default_factory=deque)
    wakeup: asyncio.Event = field(default_factory=asyncio.Event)

    def push(self, event: dict[str, Any]) -> None:
        self.events.append(event)
        if len(self.events) > MAX_BUFFERED:
            self.events.popleft()
        self.wakeup.set()

    def drain(self) -> list[dict[str, Any]]:
        """取走当前全部事件并清掉唤醒信号（调用方随后继续等待）。"""
        batch = list(self.events)
        self.events.clear()
        self.wakeup.clear()
        return batch


class LiveEventHub:
    """极简发布/订阅：`publish` 由写入路径调用，`subscribe` 由 SSE 端点调用。

    `publish` 在事件循环里同步调用（`StatsCollector.record` 就在请求协程里），
    因此不需要 `call_soon_threadsafe`；后台任务/脚本没有订阅者，直接空转。
    """

    def __init__(self, *, max_subscribers: int = MAX_SUBSCRIBERS) -> None:
        self._subscriptions: list[Subscription] = []
        self._max_subscribers = max_subscribers

    @property
    def subscriber_count(self) -> int:
        return len(self._subscriptions)

    def subscribe(self) -> Subscription:
        if len(self._subscriptions) >= self._max_subscribers:
            raise TooManySubscribers
        subscription = Subscription()
        self._subscriptions.append(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        """幂等注销：重复调用（生成器 finally 与显式清理都可能走到）无害。"""
        try:
            self._subscriptions.remove(subscription)
        except ValueError:
            return

    def publish(self, event: dict[str, Any]) -> None:
        """把一条事件推给所有订阅者；单个订阅者的问题不影响其它订阅者。"""
        for subscription in list(self._subscriptions):
            subscription.push(event)
