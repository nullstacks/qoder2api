"""凭证级限额护栏的持久化：`credential_limits` 表 + 当日用量聚合。

判定逻辑在 `src/engine/limits.py`（纯函数）；本模块只负责存取：

- `LimitsRepository`：限额配置的 CRUD（缺行 = 不受限）
- `day_usage(db, since=...)`：按凭证聚合 `usage_events` 的当日用量

`per_model_tokens` 以 JSON 文本入库（模型名做键），读时对脏数据容错——一行
写坏不能让整个选号流程崩掉（与 quota_expiry_ladder 同一方针）。
"""

from __future__ import annotations

import json
import time
from typing import Any

from ..engine.limits import CredentialLimits, Usage


def _per_model_text(mapping: dict[str, int]) -> str:
    """按模型上限落库；空/非法一律写 '{}'（恒非 NULL，读取不必判空）。"""
    clean = {str(model): int(cap) for model, cap in mapping.items()
             if str(model) and cap}
    return json.dumps(clean, ensure_ascii=False)


def _per_model_value(text: Any) -> dict[str, int]:
    """读回按模型上限。缺失 / 脏数据当空表（不让坏行拖崩选号）。"""
    if not text:
        return {}
    try:
        items = json.loads(text)
    except (TypeError, ValueError):
        return {}
    if not isinstance(items, dict):
        return {}
    out: dict[str, int] = {}
    for model, cap in items.items():
        try:
            out[str(model)] = int(cap)
        except (TypeError, ValueError):
            continue
    return out


def _row_to_limits(row: Any) -> CredentialLimits:
    return CredentialLimits(
        credential_id=row["credential_id"],
        keep_credits=row["keep_credits"],
        daily_tokens=row["daily_tokens"],
        daily_credits=row["daily_credits"],
        per_model_tokens=_per_model_value(row["per_model_tokens"]),
        updated_at=row["updated_at"],
        updated_by=row["updated_by"],
    )


class LimitsRepository:
    """`credential_limits` 表的读写。"""

    def __init__(self, db) -> None:
        self._db = db

    def get(self, credential_id: str) -> CredentialLimits | None:
        row = self._db.connect().execute(
            "SELECT credential_id, keep_credits, daily_tokens, daily_credits, "
            "per_model_tokens, updated_at, updated_by "
            "FROM credential_limits WHERE credential_id = ?", (credential_id,)).fetchone()
        return _row_to_limits(row) if row is not None else None

    def map(self) -> dict[str, CredentialLimits]:
        """全表快照（选号护栏一轮读一次，避免逐候选查库）。表很小：只存
        「配过限额」的凭证。"""
        rows = self._db.connect().execute(
            "SELECT credential_id, keep_credits, daily_tokens, daily_credits, "
            "per_model_tokens, updated_at, updated_by FROM credential_limits").fetchall()
        return {row["credential_id"]: _row_to_limits(row) for row in rows}

    def set(self, credential_id: str, *, keep_credits: float | None = None,
            daily_tokens: int | None = None, daily_credits: float | None = None,
            per_model_tokens: dict[str, int] | None = None,
            updated_by: str | None = None, now: int | None = None) -> None:
        """写入 / 更新一条限额；四个护栏全关时删除该行（回到「不受限」）。"""
        limits = CredentialLimits(
            credential_id=credential_id, keep_credits=keep_credits,
            daily_tokens=daily_tokens, daily_credits=daily_credits,
            per_model_tokens=dict(per_model_tokens or {}),
            updated_at=int(now if now is not None else time.time()),
            updated_by=updated_by)
        if limits.is_empty():
            self.delete(credential_id)
            return
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO credential_limits (credential_id, keep_credits, daily_tokens, "
                "daily_credits, per_model_tokens, updated_at, updated_by) "
                "VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(credential_id) DO UPDATE SET "
                "keep_credits=excluded.keep_credits, daily_tokens=excluded.daily_tokens, "
                "daily_credits=excluded.daily_credits, "
                "per_model_tokens=excluded.per_model_tokens, "
                "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                (limits.credential_id, limits.keep_credits, limits.daily_tokens,
                 limits.daily_credits, _per_model_text(dict(limits.per_model_tokens)),
                 limits.updated_at, limits.updated_by))

    def delete(self, credential_id: str) -> None:
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM credential_limits WHERE credential_id = ?",
                         (credential_id,))


def day_usage(db, *, since: int) -> dict[str, Usage]:
    """按凭证聚合 `since`（含）以来的用量。

    数据源是明细表（`usage_events`）而非小时汇总：小时汇总的主键里没有
    credential_id，无法还原到凭证维度。代价是只覆盖明细保留期（90 天，
    远大于一天）——对「当日」聚合没有影响。

    失败请求也计入（它们同样消耗上游 token/积分）；credit 为 NULL 的行按 0
    累加（不猜）。
    """
    rows = db.connect().execute(
        "SELECT credential_id, model, "
        "COALESCE(SUM(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)), 0) AS tokens, "
        "COALESCE(SUM(credit), 0) AS credits "
        "FROM usage_events WHERE ts >= ? AND credential_id IS NOT NULL "
        "GROUP BY credential_id, model", (since,)).fetchall()
    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        bucket = buckets.setdefault(
            row["credential_id"], {"tokens": 0, "credits": 0.0, "per_model": {}})
        bucket["tokens"] += row["tokens"]
        bucket["credits"] += row["credits"] or 0.0
        bucket["per_model"][row["model"]] = (
            bucket["per_model"].get(row["model"], 0) + row["tokens"])
    return {
        credential_id: Usage(tokens=bucket["tokens"], credits=bucket["credits"],
                             per_model=bucket["per_model"])
        for credential_id, bucket in buckets.items()
    }
