"""上游官方状态页聚合：GET /api/upstream-status。

只读端点：数据由后台 UpstreamStatusTask 周期抓取并存在内存里，这里只是把它
连同「这个任务当前是否启用 / 多久抓一次」一起回给管理台。

operator-only（admin 与 operator 都放行）——页面把「我们自己的入口」与「模型
上游」的状态摆在一起看，属于运维视角，与「模型列表」同一门槛。

`server_time` 供前端算「上次抓取距今多久」：浏览器时钟偏移会把刚抓完的
快照显示成几小时前（与 /api/tasks 同一理由）。
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Request

from ..auth.rbac import require_operator
from .deps import Services, principal_from_request


def create_router(services: Services) -> APIRouter:
    router = APIRouter()
    runtime = services.settings

    @router.get("/api/upstream-status")
    async def upstream_status(request: Request,
                              principal=Depends(principal_from_request)):
        """各上游状态源的当前快照（进程内，不落盘）。

        未装配（测试 / 一次性构造）时返回空快照而不是 500：页面显示「尚未抓取」。
        """
        require_operator(principal)
        store = getattr(request.app.state, "upstream_status", None)
        payload = store.payload() if store is not None else {
            "summary": {"level": "unknown", "operational": 0, "abnormal": 0,
                        "unknown": 0, "total": 0},
            "fetched_at": None,
            "sources": [],
        }
        runner = getattr(request.app.state, "task_runner", None)
        # 周期与开关取「当前生效值」：走 task_status()（公开接口）而不是复制
        # 一份周期计算逻辑，热更后页面立刻反映。
        task = next((item for item in runner.task_status()
                     if item["key"] == "upstream_status"), None) if runner else None
        return {
            **payload,
            "enabled": task["enabled"] if task is not None
                       else bool(runtime.upstream_status_enabled),
            "interval_seconds": task["interval_seconds"] if task is not None
                                else float(runtime.upstream_status_minutes) * 60,
            "server_time": int(time.time()),
        }

    return router
