"""OpenRouter 模型目录（管理台「模型列表」页）：刊例价 + 明细元数据 + 能力分，只读。

数据来自 `https://openrouter.ai/api/v1/models`，由后台 `openrouter_catalog`
任务周期抓取并落盘（`src/benchmarks.py`）。这里在同一次抓取里既取价格（统计页
**成本估算**的输入：`input` / `output` / `cache_read`，单位 USD / 百万 token），
也取更详细的元数据——展示名 / 上下文与输出上限 / 输入·输出模态 / 能力
（附件·推理·工具调用·结构化输出）/ 知识截止 / provider。目录缺失时回空表
（页面显示空态），不影响聊天与成本（成本另行读价表）。

**能力分**（Artificial Analysis 指数）与价格、元数据同一次抓取，挂在
`app.state.model_benchmarks`。这里按**同一套匹配口径**（`model_match.lookup`：
候选键等值、唯一命中才采用）给每行补 `benchmarks` 字段——上游 id
（`z-ai/glm-5.3`）与本项目归一键（`glm-5.3`）写法不同，靠的就是那层统一规则。
匹配不到的行**不带**该字段，页面显示 `—`；宁可不配也不错配。

目录挂在 `app.state.models_dev_catalog`（`main.lifespan` 同步回灌 + 后台刷新
就地为它换值），故这里从 request 取，而不是塞进 Services——与 model_catalog
快照同类：是运行数据，不是仓储/引擎依赖。

**`available`**（2026-10-13 起）：每行额外带一个布尔，表示该模型是否**当前渠道
真能提供**（口径与 `/v1/models` 一致：只算「有可用凭证」的渠道，且过当前黑名单）。
OpenRouter 收录数百条，其中多数我们根本没接；页面据此可一键只看「当前渠道获取
到的模型」。判定用与价表 / 能力分同一套候选键（`model_match.match_keys`），故
`z-ai/glm-5.3`（目录 id）能与渠道侧的 `glm-5.3`（对外归一键）对上。

**能力分排序**：`sort=intelligence` 按综合智能指数排序（缺分数 / 缺该项的行由
`sort_rows` 统一排最后）。只给这一个分项排序键——表头一列对应一个入口，三项各排
会把表头挤成一排按钮；综合智能也是 Playground 徽章用的主指数。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from ..auth.rbac import require_operator
from ..benchmarks import BenchmarkTable
from ..model_match import lookup, match_keys
from ..sorting import sort_rows
from .admin_model_availability import (
    DEFAULT_WINDOW_DAYS,
    MAX_LIMIT,
    MAX_WINDOW_DAYS,
    failure_rows,
    suspected_index,
)
from .deps import Services, principal_from_request
from .model_merge import _finalize
from .models import credential_providers, merged_entries

# 模型目录排序白名单：API 键 → 取值函数（缺失值排最后）。
MODEL_CATALOG_SORT_KEYS = {
    "id": lambda row: row["id"],
    "name": lambda row: row["name"],
    "provider": lambda row: row["provider"],
    "context": lambda row: row["context"],
    "max_output": lambda row: row["max_output"],
    "knowledge": lambda row: row["knowledge"],
    "release_date": lambda row: row["release_date"],
    "input": lambda row: row["input"],
    "output": lambda row: row["output"],
    "cache_read": lambda row: row["cache_read"],
    "cache_write": lambda row: row["cache_write"],
    # 能力分排序键只取**综合智能指数**（智），不取编程 / 智能体分项：表头一个
    # 「能力分」列对应一个排序入口，三项各排会让表头挤成一排按钮。与 Playground
    # 徽章用的主指数同一维度。缺失（无分数 / 无该项）由 `sort_rows` 统一排最后。
    "intelligence": lambda row: (row.get("benchmarks") or {}).get("intelligence_index"),
}


def _with_benchmarks(rows: list[dict[str, Any]],
                     table: BenchmarkTable | None) -> list[dict[str, Any]]:
    """给目录行补能力分（匹配不到就不带该字段；表为空时全部原样返回）。"""
    if not table:
        return rows
    enriched: list[dict[str, Any]] = []
    for row in rows:
        score = lookup(table, row.get("id"), row.get("name"))
        enriched.append({**row, "benchmarks": score} if score is not None else row)
    return enriched


def _available_keys(services: Services) -> set[str]:
    """当前渠道真能提供的模型 → 候选键集合（与 `/v1/models` 同口径）。

    只算「有可用凭证」的渠道（`credential_providers`），并过当前黑名单
    （`merged_entries` 内部已滤）。`merged_entries` 返回的是**未定稿**的分组
    （还没有对外 id），故先 `_finalize` 补出 id / 展示名，再展开成候选键。
    """
    entries = merged_entries(services, credential_providers(services))
    keys: set[str] = set()
    for entry in entries:
        _finalize(entry)
        keys |= match_keys(entry["id"], entry["meta"].get("name"))
    return keys


def _with_availability(rows: list[dict[str, Any]],
                       keys: set[str]) -> list[dict[str, Any]]:
    """给目录行补 `available`：该模型是否命中当前渠道提供的模型集合。

    用候选键**求交集**（不是 `lookup` 的「唯一命中」）：这里只问「有没有渠道
    提供」，命中多条渠道也是提供；键集为空（未接入任何渠道）时全部 false。
    """
    return [{**row, "available": bool(match_keys(row.get("id"), row.get("name")) & keys)}
            for row in rows]


def _with_suspected(rows: list[dict[str, Any]],
                    index: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """给目录行补 `suspected`：最近窗口内该模型是否有模型级失败（疑似下线）。

    与 `available` 同一套候选键：目录 id（`z-ai/glm-5.3`）与网关侧对外名
    （`glm-5.3`）靠 `model_match` 对上。无命中则**不带**该字段（不是 false），
    与 `benchmarks` 同口径——「没数据」和「确认健康」是两件事。
    """
    if not index:
        return rows
    enriched: list[dict[str, Any]] = []
    for row in rows:
        hit = next((index[key] for key in match_keys(row.get("id"), row.get("name"))
                    if key in index), None)
        enriched.append({**row, "suspected": hit} if hit is not None else row)
    return enriched


def _filter_catalog(rows: list[dict[str, Any]], q: str | None,
                    only_available: bool,
                    only_suspected: bool = False) -> list[dict[str, Any]]:
    """目录的服务端筛选：`q` 匹配 id / 名称，`only_available` 只留可用行。

    provider 不参与匹配：它恒为 id 的厂商前缀（id 的子串），匹配它等于匹配 id。
    与前端旧实现同口径，避免搜索行为在迁移后悄悄变化。
    `only_suspected` 只留带 `suspected` 字段的行（最近窗口内有模型级失败）。
    """
    needle = (q or "").strip().lower()
    out: list[dict[str, Any]] = []
    for row in rows:
        if only_available and not row.get("available"):
            continue
        if only_suspected and row.get("suspected") is None:
            continue
        if needle and not (
            str(row.get("id") or "").lower().find(needle) >= 0
            or str(row.get("name") or "").lower().find(needle) >= 0
        ):
            continue
        out.append(row)
    return out


def create_router(services: Services) -> APIRouter:
    router = APIRouter()

    @router.get("/api/model-catalog")
    async def get_model_catalog(request: Request, sort: str | None = None,
                                order: str | None = None,
                                q: str | None = None,
                                only_available: bool = False,
                                only_suspected: bool = False,
                                availability_days: int = DEFAULT_WINDOW_DAYS,
                                limit: int = 50, offset: int = 0,
                                principal=Depends(principal_from_request)):
        """当前生效模型目录 + 元信息；门槛 operator（admin / operator），与「模型列表」页一致。

        分页与筛选都在服务端：目录数百条，全量下发会拖慢首屏，而「搜索 id/名称」
        与「只看当前渠道」是两个常用收窄手段。`q` 大小写不敏感地匹配 id 与名称；
        `only_available` 只留 `available=true` 的行；`only_suspected` 只留最近
        `availability_days` 天有模型级失败的行（口径见
        `admin_model_availability`）。返回的 `total` 是**筛选后**的总条数（供
        前端算页数），`count` 仍是目录全量（页头展示「N / 总数」）。
        """
        require_operator(principal)
        state = request.app.state
        catalog: dict[str, dict] = state.models_dev_catalog
        # 先补能力分再排序：能力分排序键读 `row["benchmarks"]`，若在 enrich 之前
        # 排，所有行都拿不到分数、按能力分排会退化成不动。
        rows = _with_benchmarks([catalog[model] for model in sorted(catalog)],
                                getattr(state, "model_benchmarks", None))
        rows = _with_availability(rows, _available_keys(services))
        window = max(1, min(MAX_WINDOW_DAYS, int(availability_days)))
        failures = failure_rows(services, days=window, limit=MAX_LIMIT)
        rows = _with_suspected(rows, suspected_index(failures["rows"]))
        rows = _filter_catalog(rows, q, only_available, only_suspected)
        total = len(rows)
        # 默认按模型 id 升序：结果确定（前端无需再排），与历史行为一致。
        models = sort_rows(rows, sort, order,
                           MODEL_CATALOG_SORT_KEYS, default_key="id", default_desc=False)
        # 排序后切片：排序键含派生字段，SQL 排不了，故先排再分页。
        models = models[max(0, offset):max(0, offset) + max(1, min(500, limit))]
        return {
            "models": models,
            "count": len(catalog),
            "total": total,
            "currency": "USD",
            "usd_cny_rate": float(services.settings.usd_cny_rate),
            # 快照时刻：价格、明细与能力分同一次抓取，故只有一个时刻。缺失即
            # 「本次启动以来没拉到过」，页面显示 —。
            "saved_at": state.price_saved_at,
            # 疑似下线判定窗口（天）：前端据此显示「近 N 天」而不硬编码。
            "availability_days": window,
        }

    return router
