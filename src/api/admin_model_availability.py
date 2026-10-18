"""模型可用性只读视图：`GET /api/models/availability`。
**回答的问题**：「最近 N 天哪些 (渠道, 模型) 在真实请求里被判成『上游不认这个
模型』？」——此前只有首次请求才学到的 BLOCKED 负缓存（
`credential_model_cooldowns.reason='blocked'`），没有汇总视图，也就没有
「谁下线了」的看板与恢复入口。

口径：

* 数据源是 `usage_events` 的**明细**（小时汇总没有 error_type），窗口由
  `since` 绝对时刻限定，走 `idx_usage_ts`；`days` 默认 7、上限 30，`limit`
  上限 200——聚合必须限窗口与行数，否则一张跑了一年的库会被这一个只读端点
  拖垮。
* 只认 `error_type in ('upstream_blocked', 'invalid_request')`：前者是协议
  预留值（provider 直接上报时自动纳入），后者是当前 executor 对
  `ErrKind.BLOCKED` / `ErrKind.INVALID` 的统一归类——「上游不认这个模型」
  目前就落在 `invalid_request` 里。
* `blocked` 是**现算**的：拿 `MODEL_BLOCKLIST`（热更）与记录里的模型名
  做 glob 匹配，与 `/v1/models` 的过滤同口径（归一键 / 展示名任一命中即算）。
  不在服务端缓存，改完黑名单下一次请求立即反映。
* **不做主动探活**：真发请求耗额度，且必须与生产冷却 / 统计隔离，留作后续
  独立立项。

目录页（`admin_model_catalog`）复用同一份聚合结果给每行补 `suspected`
字段：两边都用 `model_match.match_keys` 的候选键求交集，OpenRouter 的
`z-ai/glm-5.3` 与网关侧的 `glm-5.3` 才能对上。
"""

from __future__ import annotations

import time
from fnmatch import fnmatch
from typing import Any

from fastapi import APIRouter, Depends

from ..auth.rbac import require_operator
from ..model_match import match_keys
from ..provider.naming import normalize_model_key
from .deps import Services, principal_from_request

# 窗口：默认 7 天、上限 30 天（`usage_events` 只保留 90 天，30 天足够判断
# 「最近是不是坏了」，再宽只是把早已恢复的历史失败翻出来）。
DEFAULT_WINDOW_DAYS = 7
MAX_WINDOW_DAYS = 30

# 返回行数上限：按失败次数降序，超出即截断并标记 `truncated`（前端提示
# 「只看前 N 条」，而不是静默给半份表）。
DEFAULT_LIMIT = 100
MAX_LIMIT = 200

# 计入「模型级失败」的 error_type（见模块 docstring）。
FAILURE_ERROR_TYPES = ("upstream_blocked", "invalid_request")


def _blocked(model: str, patterns: tuple[str, ...]) -> bool:
    """模型名是否命中当前黑名单（与 `/v1/models` 的过滤同口径）。

    记录里的 `model` 是请求用的对外名（归一键 / 展示名 / 别名），故同时拿
    原样与归一键去比；两边都忽略大小写（用户照展示名写 `Kimi K3` 与照
    归一键写 `kimi-k3` 应等价命中）。
    """
    names = {model, normalize_model_key(model)} - {""}
    return any(fnmatch(name.lower(), pattern.lower())
               for name in names for pattern in patterns)


def failure_rows(services: Services, *, days: int, limit: int = DEFAULT_LIMIT,
                 now: float | None = None) -> dict[str, Any]:
    """聚合窗口内的模型级失败；`days` / `limit` 已由调用方钳制。

    返回 `{rows, since, days, truncated}`。每行带 `keys`（`model_match` 候选键），
    供目录页求交集；对外端点会把 `keys` 去掉（内部匹配细节不外泄，也避免前端
    误把它当稳定契约）。
    """
    since = int((time.time() if now is None else now) - days * 86400)
    patterns = tuple(getattr(services.settings, "blocklist_patterns", ()) or ())
    raw = services.stats_query.model_failures(since=since, limit=limit + 1)
    truncated = len(raw) > limit
    rows: list[dict[str, Any]] = []
    for item in raw[:limit]:
        rows.append({
            "provider": item["provider"],
            "model": item["model"],
            "failures": item["failures"],
            "last_failed_at": item["last_failed_at"],
            "blocked": _blocked(item["model"], patterns),
            "keys": sorted(match_keys(item["model"])),
        })
    return {"rows": rows, "since": since, "days": days, "truncated": truncated}


def suspected_index(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """聚合行 → `{候选键: 疑似下线信息}`（同一键多条时取失败次数最多的一条）。

    目录页用候选键求交集，因此这里把每行的 `keys` 摊平成索引。同一键对应多条
    (渠道, 模型) 时只保留失败最多的一条：页面只需要「这条 OpenRouter 模型有
    问题、最坏是哪个渠道」，逐渠道明细留给 `/api/models/availability`。
    """
    index: dict[str, dict[str, Any]] = {}
    for row in rows:
        info = {"provider": row["provider"], "model": row["model"],
                "failures": row["failures"], "last_failed_at": row["last_failed_at"],
                "blocked": row["blocked"]}
        for key in row["keys"]:
            existing = index.get(key)
            if existing is None or info["failures"] > existing["failures"]:
                index[key] = info
    return index


def create_router(services: Services) -> APIRouter:
    router = APIRouter()

    @router.get("/api/models/availability")
    async def get_model_availability(days: int = DEFAULT_WINDOW_DAYS,
                                     limit: int = DEFAULT_LIMIT,
                                     principal=Depends(principal_from_request)):
        """最近 N 天模型级失败聚合（只读，operator 门槛）。

        `days` 钳到 `[1, 30]`、`limit` 钳到 `[1, 200]`：越界值不报错而是收窄
        ——这是看板端点，前端缓存里的旧参数不该把它打成 400。
        """
        require_operator(principal)
        window = max(1, min(MAX_WINDOW_DAYS, int(days)))
        capped = max(1, min(MAX_LIMIT, int(limit)))
        result = failure_rows(services, days=window, limit=capped)
        return {
            "window_days": result["days"],
            "since": result["since"],
            "rows": [{key: value for key, value in row.items() if key != "keys"}
                     for row in result["rows"]],
            "truncated": result["truncated"],
            "error_types": list(FAILURE_ERROR_TYPES),
        }

    return router
