"""上游状态页抓取后台任务：一轮 = 并发抓全部源 → 原子替换内存快照。

为什么是后台任务而不是「页面请求时现抓」：状态页聚合要打 8 个外部端点，
放在请求路径上等于让管理台页面等 8 个第三方（任一超时就把页面卡住）。周期
抓取 + 内存快照让页面恒为本地读取。

为什么周期是分钟级而不是秒级：官方状态页本身就是分钟级更新的，抓得比它更新
没有意义，只是白打别人的接口。

**永不抛异常**：全源失败也只落进 report（与 `QuotaProbeTask` 逐凭证失败同一
口径）。任务抛异常会触发「任务连续失败」告警，而「这台机器连不上外网」与
「上游真的挂了」是两件事，后者才值得半夜叫人。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

import httpx

from ..upstream_status import (
    LEVEL_OPERATIONAL,
    SOURCE_SPECS,
    SourceSpec,
    UpstreamStatusStore,
    fetch_all,
)

__all__ = ["UpstreamStatusTask"]


class UpstreamStatusTask:
    """一轮 = 抓全部源并写入快照。

    依赖全部可注入：`specs` 便于测试用假端点，`client_factory` 便于注入
    `httpx.MockTransport`，`enabled` 每轮现读（关掉再打开不必重启，与
    `AlertTask` 同一热更范式）。
    """

    def __init__(
        self,
        store: UpstreamStatusStore,
        *,
        specs: Iterable[SourceSpec] = SOURCE_SPECS,
        timeout: float = 10.0,
        enabled: Callable[[], bool] = lambda: True,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self._store = store
        self._specs = tuple(specs)
        self._timeout = timeout
        self._enabled = enabled
        self._client_factory = client_factory

    async def run_once(self) -> dict[str, Any] | None:
        """抓一轮；关闭时返回 None（no-op，不覆盖上次运行态）。"""
        if not self._enabled():
            return None

        if self._client_factory is None:
            statuses = await fetch_all(self._specs, timeout=self._timeout)
        else:
            async with self._client_factory() as client:
                statuses = await fetch_all(self._specs, timeout=self._timeout,
                                           client=client)

        self._store.update(statuses)
        failed = sum(1 for status in statuses if status.error is not None)
        return {
            "sources": len(statuses),
            # 「异常」= 除正常之外的一切（含抓取失败）：页面上这两类要分别
            # 渲染，但任务卡片只需要一个「有几个源不是绿的」。
            "abnormal": sum(1 for status in statuses
                            if status.level != LEVEL_OPERATIONAL),
            "failed": failed,
        }
