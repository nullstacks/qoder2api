"""侧栏「数据提醒」角标聚合：GET /api/nav-badges。

管理台侧栏在每个页面都在，但多数待办态（告警未恢复 / 任务失败 / 凭证需重登）
**不点进去就发现不了**。这里用一个聚合端点把各页面的「待办计数」一次算完，
侧栏轮询它即可——比让 Layout 去挂 7 个页面级 hook 划算得多（后者会反复拉
全量列表）。

设计要点：

- **按角色裁剪 payload**：admin-only 的 key（告警 / 任务 / 用户）不下发给
  operator / viewer，operator 的 key（上游 / 模型列表）不下发给 viewer。
  缺失 = 无权看，前端据此不渲染角标。与侧栏可见性、路由守卫同一口径。
- **不做「越权先 403」**：这是聚合端点，viewer 也有要看的部分（API Key），
  整体 403 会把能看的一起挡掉。
- **未装配时回 0 / false 而不是 500**：测试与一次性构造常不挂 task_runner /
  upstream_status（与 `admin_upstream.py` 的空快照同范式）。
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Request

from .deps import Services, principal_from_request

# API Key「即将到期」预警窗口（天）。计数在后端算，故常量放这里；
# 刻意不进 HOT_SETTINGS——改窗口的频率远低于改告警阈值，先不做热更。
API_KEY_EXPIRY_WINDOW_DAYS = 7

# 模型目录陈旧判定：超过这么多倍刷新周期没更新就算陈旧。取 2 倍 = 漏跑
# 整整一轮才算，避免后台任务的正常抖动抢注意力。
STALE_CATALOG_INTERVALS = 2


def _task_failure_count(runner) -> int:
    """上次真跑失败、且当前启用的任务数。

    用 `last_ok is False` 而不是告警规则的「连续失败 ≥ 阈值」：首次失败就该
    让人知道，不必等阈值轮（默认 15 分钟）。`last_ok is None`（从未运行）不计
    ——进程刚重启不是故障；`enabled=False`（用户关掉的任务）也不算待办。
    """
    if runner is None:
        return 0
    return sum(1 for item in runner.task_status()
               if item["enabled"] and item["last_ok"] is False)


def _upstream_summary(request: Request) -> tuple[int, str]:
    """上游状态快照的 `(abnormal, level)`；未装配时回 `(0, "unknown")`。

    计数复用后端 `summary.abnormal`（含 maintenance），不另立第四套「异常」
    口径；`level` 是 `aggregate_level` 的最差档，前端用它区分维护与真故障的
    色调（维护 → 黄，部分/整体故障 → 红）。
    """
    store = getattr(request.app.state, "upstream_status", None)
    if store is None:
        return 0, "unknown"
    summary = store.payload()["summary"]
    return int(summary["abnormal"]), str(summary["level"])


def _catalog_stale(request: Request, interval_minutes: int) -> bool:
    """模型目录快照是否陈旧（漏跑一整轮以上）。

    从未拉取过（`price_saved_at is None` 且目录为空）**不算陈旧**：新装部署
    一上来就报警是噪声。只有目录已有内容、却长时间没刷新才提示。
    """
    saved_at = getattr(request.app.state, "price_saved_at", None)
    if saved_at is None:
        return False
    age = time.time() - float(saved_at)
    return age > STALE_CATALOG_INTERVALS * int(interval_minutes) * 60


def create_router(services: Services) -> APIRouter:
    router = APIRouter()
    runtime = services.settings

    @router.get("/api/nav-badges")
    async def nav_badges(request: Request,
                         principal=Depends(principal_from_request)):
        """侧栏角标计数（按角色裁剪，缺失 = 无权看）。"""
        payload: dict[str, object] = {}
        now = int(time.time())

        # 凭证待办：仅 operator+（viewer 无写权限，露出角标只会误导；
        # 注意这与 /api/credentials 的可见性不同——viewer 能看列表）。
        if principal.is_operator:
            attention = services.credentials.attention_counts()
            payload["credentials"] = attention["disabled"] + attention["exhausted"]

        # API Key 即将到期：全员，但只数自己的。
        payload["api_keys"] = services.api_keys.count_expiring(
            principal.username, now + API_KEY_EXPIRY_WINDOW_DAYS * 86400)

        if principal.is_admin:
            payload["alerts"] = services.alerts.count_unresolved()
            payload["tasks"] = _task_failure_count(
                getattr(request.app.state, "task_runner", None))
            payload["users"] = services.user_repo.count_pending_activation()

        if principal.is_operator:
            abnormal, level = _upstream_summary(request)
            payload["upstream"] = abnormal
            payload["upstream_level"] = level
            payload["model_catalog_stale"] = _catalog_stale(
                request, runtime.openrouter_catalog_minutes)

        return payload

    return router
