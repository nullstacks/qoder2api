"""运维告警记录回看与人工处置：GET /api/alerts + POST /api/alerts/{id}/resolve。

回看只读：告警由后台 AlertTask 写入，管理台「站内告警记录」按时间倒序看。
admin-only——告警详情含凭证 id / 渠道等运维信息，与任务与配置页同一门槛。

**人工处置**是状态机之外的出口。正常路径下 `AlertTask` 会在规则不再命中时
自己写 `resolved_at`（重启后也能自愈：构造时从库里未恢复的行重建「告警中」
集合）。但「规则已下线 / 误报 / 告警任务被关掉」这几类情况下，评估根本不会
再跑，那行就永远挂在未恢复里、侧栏角标永远亮。这里给一条显式出口，并记审计
——人工覆盖机器判定必须留痕。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..audit.actions import ACTION_ALERT_RESOLVE
from ..auth.rbac import require_admin
from ..compat.openai.request import InvalidRequest
from .deps import Services, csrf_protected, principal_from_request


def create_router(services: Services) -> APIRouter:
    router = APIRouter()
    alerts = services.alerts

    @router.get("/api/alerts")
    async def list_alerts(limit: int = 50, offset: int = 0,
                          sort: str | None = None,
                          order: str | None = None,
                          principal=Depends(principal_from_request)):
        """最近告警（默认倒序）。limit 由仓储收敛到 [1, 200]；total 供前端算页数。"""
        require_admin(principal)
        return {"alerts": alerts.recent(limit, sort=sort, order=order, offset=offset),
                "total": alerts.count_all()}

    @router.post("/api/alerts/{alert_id}/resolve")
    async def resolve_alert(alert_id: str,
                            _csrf: None = Depends(csrf_protected),
                            principal=Depends(principal_from_request)):
        """人工标记一条告警「已恢复」（admin + CSRF + 审计）。

        只标这一行，不做按 `(rule, scope)` 批量：同组里可能还有仍在告警的记录，
        手滑一次收掉整组会掩盖真问题。已恢复或不存在都回 400——静默成功会让
        运维以为点对了，实际什么都没改。
        """
        require_admin(principal)
        if alerts.resolve_by_id(alert_id) == 0:
            raise InvalidRequest("alert not found or already resolved")
        services.audit.record(actor=principal.username, action=ACTION_ALERT_RESOLVE,
                              target=alert_id)
        return {"ok": True}

    return router
