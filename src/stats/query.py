"""用量统计查询（TECHNICAL §2 query.py）：overview / by-provider / timeline / events。"""

from __future__ import annotations

import time
from typing import Any

from ..sorting import parse_sort_order, sql_order

METRIC_COLUMNS: dict[str, str] = {
    # metric 参数 → usage_hourly 上的取值表达式
    "requests": "requests",
    "tokens": "input_tokens + output_tokens",
    "latency": "latency_sum",      # 均值由调用方除以 ok_count
    "ttfb": "ttfb_sum",            # 同上
    # 成本默认口径是人民币（与本模块展示一致）；仅含可定价明细的小时才有值。
    "cost": "cost_cny_sum",
}

# 分组统计下发的分位档：P50 / P90 / P95 / P99（latency 与 ttfb 各一组列）。
# **不加进 METRIC_COLUMNS**：图表指标读小时汇总，只存 sum/count，数学上无法
# 还原分布；分位只能从明细表算（见 `_percentile_join`）。
PERCENTILES: tuple[int, ...] = (50, 90, 95, 99)


# 分组统计排序白名单：API 键 → 聚合列别名（`group` 指分组键列 `group_key`）。
# 默认按请求数降序（「哪家/哪个模型用得最多」才是分组表的用途），并列时按
# 分组键升序——次序稳定且与旧行为一致（旧代码就是按分组键升序）。
#
# 延迟类键的两种来源：
# - 均值（avg_*）：小时汇总上的 `*_sum ÷ ok_count`，SQLite 允许 ORDER BY 引用
#   SELECT 别名并做表达式；`ok_count = 0` 时除零得 NULL，降序自然排最后（与
#   其它可空列同语义）。
# - 分位（p50_* / p90_* / p95_* / p99_*）：由 `_percentile_join` 的 LEFT JOIN
#   子查询算出，排序直接走该列别名。
GROUP_SORT_COLUMNS: dict[str, str] = {
    "group": "group_key",
    "requests": "requests",
    "ok_count": "ok_count",
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cached_tokens": "cached_tokens",
    "credit": "credit_sum",
    "cost_cny": "cost_cny_sum",
    "avg_latency_ms": "CAST(latency_sum AS REAL) / ok_count",
    "avg_ttfb_ms": "CAST(ttfb_sum AS REAL) / ok_count",
    "p50_latency_ms": "p50_latency_ms",
    "p50_ttfb_ms": "p50_ttfb_ms",
    "p90_latency_ms": "p90_latency_ms",
    "p90_ttfb_ms": "p90_ttfb_ms",
    "p95_latency_ms": "p95_latency_ms",
    "p95_ttfb_ms": "p95_ttfb_ms",
    "p99_latency_ms": "p99_latency_ms",
    "p99_ttfb_ms": "p99_ttfb_ms",
}


# 逐请求明细排序白名单：API 键 → 列（带 `e.` 前缀，因查询有 JOIN）。
# `time` 是 rowid（插入序）；它保持降序时走游标分页，其余情况走 offset 分页。
EVENT_SORT_COLUMNS: dict[str, str] = {
    "time": "e.rowid",
    "ts": "e.ts",
    "username": "e.username",
    "provider": "e.provider",
    "credential": "e.credential_id",
    "model": "e.model",
    "ok": "e.ok",
    "input_tokens": "e.input_tokens",
    "output_tokens": "e.output_tokens",
    "cached_tokens": "e.cached_tokens",
    # 命中率是派生列：命中 ÷ 输入。输入为 0 / 未上报（NULL）时给 NULL——与其它
    # 可空列同例（降序时 NULL 自然排最后），不拿「未上报」冒充 0%。CAST 成 REAL
    # 走浮点除法（整数除法会截断成 0）。
    "cache_rate": ("CASE WHEN e.input_tokens > 0 "
                   "THEN CAST(e.cached_tokens AS REAL) / e.input_tokens ELSE NULL END"),
    "credit": "e.credit",
    "cost_cny": "e.cost_cny",
    "ttfb_ms": "e.ttfb_ms",
    "latency_ms": "e.latency_ms",
}


# 分组键白名单：`_group_hourly` / `_group_events` 把 key 直接拼进 SQL（列名无法
# 参数绑定），调用点全是本模块内的字面量。留一份显式白名单并在入口断言，未来
# 若有人把外部输入当 key 传进来，会当场炸而不是拼出注入语句。
_GROUP_KEYS = frozenset({"provider", "model", "username", "credential_id"})

# CSV 导出单次上限：明细只留 90 天，但全量导出（几万行）会一次性拉进内存并
# 撑爆响应。超上限时明确告知「被截断、请收窄条件」，而不是静默给半份文件。
EXPORT_MAX_ROWS = 10000

# 失败归因（`/api/stats/errors`）：默认窗口 24 小时、单次最多 5000 组。
# 窗口是硬要求——没有 `ts` 下界就是全表扫（明细留 90 天，几十万行）。
ERROR_WINDOW_SECONDS = 86400
ERROR_MAX_GROUPS = 5000

# 分位值的 nearest-rank 名次：第 p 百分位 = 第 ⌈p·n/100⌉ 小的值。用整数运算写
# `(p * n + 99) / 100`（SQLite 整数除法即向下取整，加分母−1 后等价于对分子
# 向上取整），避免浮点误差把名次算偏。与原 P95 的 `(95*n+99)/100` 逐字节一致。
_PERCENT_DENOM = 100

# 分位 JOIN 子查询的别名：`_percentile_join` 与两个分组路径共用，别名固定。
_PERCENTILE_ALIAS = "lat"

# 每档分位在结果行里下发的列名（latency 与 ttfb 各一列）。`_group_row` 按此
# 生成返回 dict——加档只需改 `PERCENTILES`，不用再改三处 SQL/映射。
_PERCENTILE_FIELDS: tuple[str, ...] = tuple(
    f"p{pct}_{kind}" for pct in PERCENTILES for kind in ("latency_ms", "ttfb_ms"))


def _percentile_rank(n_expr: str, pct: int) -> str:
    """`⌈pct/100·n⌉` 的 SQL 表达式（整数运算，避免浮点误差）。"""
    return f"({pct} * {n_expr} + {_PERCENT_DENOM - 1}) / {_PERCENT_DENOM}"


def _percentile_select(pct: int, *, indent: str = "                   ") -> str:
    """子查询里某一档分位的两列（latency / ttfb）。"""
    return (
        f"{indent}MAX(CASE WHEN ranked.lat_rn = {_percentile_rank('ranked.lat_n', pct)}\n"
        f"{indent}         THEN ranked.latency_ms END) AS p{pct}_latency_ms,\n"
        f"{indent}MAX(CASE WHEN ranked.ttfb_rn = {_percentile_rank('ranked.ttfb_n', pct)}\n"
        f"{indent}         THEN ranked.ttfb_ms END) AS p{pct}_ttfb_ms"
    )


def _percentile_columns(outer: str = _PERCENTILE_ALIAS) -> str:
    """主查询里取各档分位列的 SELECT 片段（逗号分隔，不含前导逗号）。"""
    return ",\n                   ".join(
        f"{outer}.{name} AS {name}" for name in _PERCENTILE_FIELDS)


def _percentile_join(key: str, *, where: str = "", params: list[Any] | None = None,
                     suffix: str = "e2", outer: str = "") -> tuple[str, list[Any]]:
    """按 `key` 分组算 P50/P90/P95/P99 延迟的 LEFT JOIN 子查询（挂到主聚合查询上）。

    四档分位在**同一次**窗口函数扫描里算完：明细表按分组排一次序、按 latency 与
    ttfb 各生成一套 row_number，再用条件聚合按名次挑出各档的值。拆成四个 JOIN
    会把同一批明细扫四遍。

    分位值**只能从明细表算**：小时汇总只存 `latency_sum` / `ttfb_sum`（和与
    成功计数），数学上无法还原分布。代价是只覆盖明细保留期（90 天），均值列
    仍读小时汇总（永久）——表尾文案把这个差异写明，不假装全覆盖。

    口径与均值一致：**只算成功请求**（`ok = 1`，TECHNICAL §9.7）。`ttfb_ms`
    对非流式 / 未产出首字的成功请求为 NULL，故 latency 与 ttfb 各自独立排名
    （`COUNT(col)` 忽略 NULL，天然只数有值样本）。`ORDER BY (col IS NULL), col`
    把 NULL 排在有值样本之后，配合 `COUNT(col)` 的名次不会错位。

    `where` / `params`：子查询**独立**应用作用域与时间窗（取值列用 `ts`，
    与主查询的 `hour_utc` 口径区分）。参数由调用方按子查询内的占位符顺序给出，
    返回时原样带回，主查询拼在自己的参数之后。

    `suffix`：子查询内部表别名，避免与主查询的别名撞名（明细路径主查询用
    `e`，故子查询用 `e2`）。
    `outer`：**主查询**那侧的分组列前缀（小时路径无别名 = ""，明细路径 = "e."），
    JOIN 的 ON 必须指向主查询的列而不是子查询里的同名列。
    """
    if key not in _GROUP_KEYS:
        raise ValueError(f"非法分组键: {key!r}")
    inner = f"{suffix}.{key}"
    pct_selects = ",\n".join(_percentile_select(pct) for pct in PERCENTILES)
    return f"""
        LEFT JOIN (
            SELECT ranked.group_key AS group_key,
{pct_selects}
            FROM (
                SELECT {inner} AS group_key, {suffix}.latency_ms AS latency_ms,
                       {suffix}.ttfb_ms AS ttfb_ms,
                       ROW_NUMBER() OVER (
                           PARTITION BY {inner}
                           ORDER BY ({suffix}.latency_ms IS NULL), {suffix}.latency_ms
                       ) AS lat_rn,
                       COUNT({suffix}.latency_ms) OVER (PARTITION BY {inner}) AS lat_n,
                       ROW_NUMBER() OVER (
                           PARTITION BY {inner}
                           ORDER BY ({suffix}.ttfb_ms IS NULL), {suffix}.ttfb_ms
                       ) AS ttfb_rn,
                       COUNT({suffix}.ttfb_ms) OVER (PARTITION BY {inner}) AS ttfb_n
                FROM usage_events {suffix}
                WHERE {suffix}.ok = 1 AND {inner} IS NOT NULL {where}
            ) AS ranked
            GROUP BY ranked.group_key
        ) AS {_PERCENTILE_ALIAS} ON {_PERCENTILE_ALIAS}.group_key = {outer}{key}
    """, list(params or [])


def _group_order(sort: str | None, order: str | None) -> str:
    """分组统计的 `ORDER BY`：主列随 sort，并列时分组键升序（稳定次序）。"""
    key, desc = parse_sort_order(sort, order, GROUP_SORT_COLUMNS,
                                 default_key="requests", default_desc=True)
    column = GROUP_SORT_COLUMNS[key]
    if column == "group_key":
        return "group_key ASC"
    direction = "DESC" if desc else "ASC"
    return f"{column} {direction}, group_key ASC"


class StatsQuery:
    def __init__(self, db) -> None:
        self._db = db

    def _metric_sql(self, metric: str) -> tuple[str, bool]:
        """返回 (取值表达式, 是否均值语义)。非法 metric 回退 requests。"""
        expr = METRIC_COLUMNS.get(metric, "requests")
        return expr, metric in ("latency", "ttfb")

    def _metric_value(self, metric: str, expr_value, ok_count, cost_known: int = 0) -> Any:
        """把 SQL 原始值按 metric 归一：均值类除以 ok_count，无成功则 0。

        成本是**部分可定价**的：某小时/模型若一条明细都没匹配到价表
        （`cost_known=0`），聚合值只是 0，直接展示会被读成「免费」；这里回 None
        让前端显示 —。只要有一条可定价，就按可定价部分求和（宁可少算不虚报）。
        """
        if metric in ("latency", "ttfb"):
            return round(expr_value / ok_count) if ok_count else 0
        if metric == "cost" and not cost_known:
            return None
        return expr_value

    @staticmethod
    def _where(*, username: str | None = None, provider: str | None = None,
               model: str | None = None, credential_id: str | None = None,
               ok: int | None = None,
               since: int | None = None, since_col: str = "ts",
               until: int | None = None, until_col: str = "ts",
               before: int | None = None, before_col: str = "rowid",
               alias: str = "") -> tuple[str, list[Any]]:
        """拼 WHERE 子句与参数。alias 为 JOIN 时的表前缀（如 "e."）。

        `since` 与 `until` 是**同一个时间窗的两端**（半开区间 `[since, until)`），
        区别只在取值列：明细表用 `ts`，小时汇总用 `hour_utc`。两者可单独给
        （只给一端 = 单边不限），也可都不给（全部，与历史行为一致）。

        `before` 是**游标**不是时间：它按 `rowid`（插入序）取「更小的一页」，
        与 90 天时间窗无关，故与 `until` 分开、各用一列。

        `ok`：只看成功（1）或只看失败（0）的明细。`None` = 不限制（历史行为）。
        """
        clauses: list[str] = []
        params: list[Any] = []
        if username is not None:
            clauses.append(f"{alias}username = ?")
            params.append(username)
        if provider is not None:
            clauses.append(f"{alias}provider = ?")
            params.append(provider)
        if model is not None:
            clauses.append(f"{alias}model = ?")
            params.append(model)
        if credential_id is not None:
            clauses.append(f"{alias}credential_id = ?")
            params.append(credential_id)
        if ok is not None:
            clauses.append(f"{alias}ok = ?")
            params.append(ok)
        if since is not None:
            clauses.append(f"{alias}{since_col} >= ?")
            params.append(since)
        if until is not None:
            # 半开区间上界：`until` 本身不计入（与 since 的闭下界区分），
            # 前端按此把「结束时间」当作瞬时右端点，前后两次查询不重叠
            clauses.append(f"{alias}{until_col} < ?")
            params.append(until)
        if before is not None:
            clauses.append(f"{alias}{before_col} < ?")
            params.append(before)
        return (f"WHERE {' AND '.join(clauses)}" if clauses else ""), params

    def overview(self, *, username: str | None = None, provider: str | None = None,
                 since: int | None = None, until: int | None = None) -> dict[str, Any]:
        """总览。admin 传 username=None 看全局；普通用户只看自己。

        数据源是 `usage_hourly`（与 timeline/model-timeline 同源）：
        明细只留 90 天，读明细会让选「全部」时总览小于图表；小时汇总永久保留。
        代价：最近 ≤5 分钟未进汇总的请求不计入（retention 每 5 分钟 rollup），
        刷新一次即可。延迟类均值统一按 `ok_count` 归一（与图表同口径）。

        `since` / `until`：自定义时间窗两端（半开 `[since, until)`），按小时桶
        过滤，与 `timeline` 同口径。
        """
        where, params = self._where(username=username, provider=provider,
                                    since=since, since_col="hour_utc",
                                    until=until, until_col="hour_utc")

        row = self._db.connect().execute(
            f"""
            SELECT COALESCE(SUM(requests), 0) AS requests,
                   COALESCE(SUM(ok_count), 0) AS ok_count,
                   COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens,
                   COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
                   SUM(cached_tokens) AS cached_tokens,
                   SUM(cached_known) AS cached_known,
                   SUM(credit_sum) AS credit_sum,
                   SUM(credit_known) AS credit_known,
                   SUM(credit_estimated_known) AS credit_estimated_known,
                   SUM(cost_usd_sum) AS cost_usd_sum,
                   SUM(cost_cny_sum) AS cost_cny_sum,
                   SUM(cost_known) AS cost_known,
                   SUM(latency_sum) AS latency_sum,
                   SUM(ttfb_sum) AS ttfb_sum
            FROM usage_hourly {where}
            """, params).fetchone()
        requests = row["requests"] or 0
        ok_count = row["ok_count"] or 0
        # 均值口径：只除成功请求（失败请求的 latency 会拉偏"典型耗时"，
        # 且与图表 latency_sum/ok_count 同一致），无成功则 None
        return {
            "requests": requests,
            "ok_count": ok_count,
            "success_rate": (ok_count / requests) if requests else None,
            "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"],
            "reasoning_tokens": row["reasoning_tokens"],
            # 缓存命中：任一明细上报过才可信，否则 None（前端显示 —）
            "cached_tokens": row["cached_tokens"] if row["cached_known"] else None,
            "credit": row["credit_sum"] if row["credit_known"] else None,
            # 汇总 credit 中含推算值时标 ≈（全部渠道任一为推算即标，保守）
            "credit_estimated": bool(row["credit_estimated_known"]),
            # 成本：只统计匹配到定价的明细（cost_known 条数>0 才可信）
            "cost_usd": row["cost_usd_sum"] if row["cost_known"] else None,
            "cost_cny": row["cost_cny_sum"] if row["cost_known"] else None,
            "avg_latency_ms": round(row["latency_sum"] / ok_count) if ok_count else None,
            "avg_ttfb_ms": round(row["ttfb_sum"] / ok_count) if ok_count else None,
        }

    def metrics_provider_model(self) -> list[tuple[str, str, int, int]]:
        """按 (渠道, 模型) 汇总请求数 / 成功数（/metrics 的 requests_total）。

        读**永久保留**的 `usage_hourly`（与总览同源）：明细只留 90 天，
        用它算累计计数器会随裁剪下降。返回 (provider, model, requests, ok_count)，
        由调用方拆成 status=ok / error 两条样本。排序稳定（provider, model）。
        """
        rows = self._db.connect().execute(
            "SELECT provider, model, COALESCE(SUM(requests), 0) AS requests, "
            "COALESCE(SUM(ok_count), 0) AS ok_count "
            "FROM usage_hourly GROUP BY provider, model "
            "ORDER BY provider, model").fetchall()
        return [(row["provider"], row["model"], int(row["requests"]),
                 int(row["ok_count"])) for row in rows]

    def metrics_provider_tokens(self) -> list[tuple[str, int, int, int, int]]:
        """按渠道汇总各 token 类别（/metrics 的 tokens_total）。

        返回 (provider, input, output, reasoning, cached)，排序稳定（provider）。
        与 requests 同为 `usage_hourly`（永久）上的累计值。
        """
        rows = self._db.connect().execute(
            "SELECT provider, "
            "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
            "COALESCE(SUM(output_tokens), 0) AS output_tokens, "
            "COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens, "
            "COALESCE(SUM(cached_tokens), 0) AS cached_tokens "
            "FROM usage_hourly GROUP BY provider ORDER BY provider").fetchall()
        return [(row["provider"], int(row["input_tokens"]), int(row["output_tokens"]),
                 int(row["reasoning_tokens"]), int(row["cached_tokens"]))
                for row in rows]

    def model_failures(self, *, since: int, limit: int = 200) -> list[dict[str, Any]]:
        """按 (provider, model) 聚合窗口内的模型级失败（可用性视图的数据源）。

        只认两类错误：`invalid_request`（当前 executor 把上游「无此模型」的
        `ErrKind.BLOCKED` 也归到这一类）与 `upstream_blocked`（协议预留值，
        未来 provider 直接上报时自动纳入）。窗口由调用方给绝对时刻 `since`，
        SQL 走 `idx_usage_ts`；`limit` 由调用方收敛，多取一条供判断截断。

        `last_failed_at` 取该组内最大 `ts`；排序按失败次数降序，同次数按
        (provider, model) 升序，保证分页 / 快照稳定。
        """
        rows = self._db.connect().execute(
            """
            SELECT provider, model, COUNT(*) AS failures, MAX(ts) AS last_failed_at
            FROM usage_events
            WHERE ts >= ? AND error_type IN ('upstream_blocked', 'invalid_request')
            GROUP BY provider, model
            ORDER BY failures DESC, provider ASC, model ASC
            LIMIT ?
            """, (since, max(1, limit))).fetchall()
        return [{"provider": row["provider"], "model": row["model"],
                 "failures": int(row["failures"]),
                 "last_failed_at": int(row["last_failed_at"])} for row in rows]

    @staticmethod
    def _group_row(row: Any, key: str, extra: tuple[str, ...] = ()) -> dict[str, Any]:
        """把一行聚合结果转成 {分组键: ...} + 公共指标的 dict（各分组维度共用）。

        `extra`：维度特有的展示列（如凭证维度的 provider / credential_name），
        SQL 已算好，这里原样带出。

        延迟指标两种来源、两套口径（表尾文案对用户说明）：
        - 均值读小时汇总（永久保留），与 `overview()` 逐字节同口径：
          `round(sum / ok_count)`，无成功请求给 None（前端显示 —）；
        - 分位读明细表（只覆盖 90 天），SQL 里已按成功请求算好，NULL =
          该组无成功样本（全失败 / 未上报延迟）。
        """
        ok_count = row["ok_count"] or 0
        return {
            key: row["group_key"],
            "requests": row["requests"],
            "ok_count": ok_count,
            "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"],
            # 缓存命中：任一明细上报过才可信，否则 None（前端显示 —）
            "cached_tokens": row["cached_tokens"] if row["cached_known"] else None,
            "credit": row["credit_sum"] if row["credit_known"] else None,
            "credit_estimated": bool(row["credit_estimated_known"]),
            "cost_usd": row["cost_usd_sum"] if row["cost_known"] else None,
            "cost_cny": row["cost_cny_sum"] if row["cost_known"] else None,
            "avg_latency_ms": round(row["latency_sum"] / ok_count) if ok_count else None,
            "avg_ttfb_ms": round(row["ttfb_sum"] / ok_count) if ok_count else None,
            **{name: row[name] for name in _PERCENTILE_FIELDS},
            **{name: row[name] for name in extra},
        }

    def _group_hourly(self, key: str, *, username: str | None = None,
                      since: int | None = None, until: int | None = None,
                      sort: str | None = None,
                      order: str | None = None) -> list[dict[str, Any]]:
        """在小时汇总上按 `key` 列聚合（渠道 / 模型 / 用户三个维度共用）。"""
        if key not in _GROUP_KEYS:
            raise ValueError(f"非法分组键: {key!r}")
        where, params = self._where(username=username, since=since, since_col="hour_utc",
                                    until=until, until_col="hour_utc")
        # 分位值另起一条明细过滤：同一作用域与时间窗，但取值列用 `ts`（明细表），
        # 与主查询的 `hour_utc`（小时表）区分——否则时间窗对不上，分位会算错范围
        pct_where, pct_params = self._where(username=username, since=since, until=until,
                                            alias="e2.")
        # 分位子查询的 WHERE 由 _where 生成（无条件时是空串），这里补 AND 接后续条件
        pct_clause = f"AND {pct_where[6:]}" if pct_where else ""
        join, join_params = _percentile_join(key, where=pct_clause, params=pct_params)
        rows = self._db.connect().execute(
            f"""
            SELECT {key} AS group_key, COALESCE(SUM(requests), 0) AS requests,
                   COALESCE(SUM(ok_count), 0) AS ok_count,
                   COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens,
                   SUM(cached_tokens) AS cached_tokens,
                   SUM(cached_known) AS cached_known,
                   SUM(credit_sum) AS credit_sum,
                   SUM(credit_known) AS credit_known,
                   SUM(credit_estimated_known) AS credit_estimated_known,
                   SUM(cost_usd_sum) AS cost_usd_sum,
                   SUM(cost_cny_sum) AS cost_cny_sum,
                   SUM(cost_known) AS cost_known,
                   COALESCE(SUM(latency_sum), 0) AS latency_sum,
                   COALESCE(SUM(ttfb_sum), 0) AS ttfb_sum,
                   {_percentile_columns()}
            FROM usage_hourly
            {join}
            {where} GROUP BY {key} ORDER BY {_group_order(sort, order)}
            """, [*join_params, *params]).fetchall()
        return [self._group_row(row, key) for row in rows]

    def _group_events(self, key: str, *, username: str | None = None,
                      since: int | None = None, until: int | None = None,
                      sort: str | None = None,
                      order: str | None = None, include_credential_name: bool = False,
                      ) -> list[dict[str, Any]]:
        """在明细上按 `key` 列聚合。

        仅凭证维度需要：小时汇总的主键里没有 credential_id，无法还原。代价是
        数据源只保留 90 天（选「全部」时凭证分组比其它维度少），且无凭证的
        预热失败等请求被排除（它们没有可归属的凭证）。

        `include_credential_name`：随行下发凭证昵称与渠道，前端据此在凭证名前
        画渠道 icon 并与请求明细显示同一个名字。昵称取自**全局共享池**（常含
        邮箱/手机），只对 admin/operator 返回（与 `events()` 的 约束一致）。
        """
        if key not in _GROUP_KEYS:
            raise ValueError(f"非法分组键: {key!r}")
        where, params = self._where(username=username, since=since, since_col="ts",
                                    until=until, until_col="ts", alias="e.")
        # 分组键为空的行（无凭证的预热失败等）没有可归属对象，不参与分组；
        # _where 无条件时返回空串，这里补 WHERE 关键词
        clause = f"{where} AND e.{key} IS NOT NULL" if where else f"WHERE e.{key} IS NOT NULL"
        # 分位子查询：与主查询同表同过滤（明细路径两条路都读 usage_events），
        # 内部别名用 e2 避免与主查询的 e 相撞
        pct_where, pct_params = self._where(username=username, since=since, until=until,
                                            alias="e2.")
        pct_clause = f"AND {pct_where[6:]}" if pct_where else ""
        join, join_params = _percentile_join(key, where=pct_clause, params=pct_params,
                                             suffix="e2", outer="e.")
        # 渠道优先取凭证当前所属渠道；凭证已被硬删除则回退该组明细里的渠道。
        # 同组跨渠道（凭证改过 provider）时 MAX 取字典序最大的那个，仅用于画
        # icon，不参与统计口径。
        name_expr = "NULLIF(c.nickname, '')" if include_credential_name else "NULL"
        rows = self._db.connect().execute(
            f"""
            SELECT e.{key} AS group_key, COUNT(*) AS requests,
                   SUM(e.ok) AS ok_count,
                   COALESCE(SUM(e.input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(e.output_tokens), 0) AS output_tokens,
                   SUM(e.cached_tokens) AS cached_tokens,
                   SUM(CASE WHEN e.cached_tokens IS NULL THEN 0 ELSE 1 END) AS cached_known,
                   COALESCE(SUM(e.credit), 0) AS credit_sum,
                   SUM(CASE WHEN e.credit IS NULL THEN 0 ELSE 1 END) AS credit_known,
                   SUM(CASE WHEN e.credit_estimated = 1 AND e.credit IS NOT NULL
                            THEN 1 ELSE 0 END) AS credit_estimated_known,
                   COALESCE(SUM(e.cost_usd), 0) AS cost_usd_sum,
                   COALESCE(SUM(e.cost_cny), 0) AS cost_cny_sum,
                   SUM(CASE WHEN e.cost_usd IS NULL THEN 0 ELSE 1 END) AS cost_known,
                   COALESCE(SUM(CASE WHEN e.ok = 1 THEN e.latency_ms END), 0) AS latency_sum,
                   COALESCE(SUM(CASE WHEN e.ok = 1 THEN e.ttfb_ms END), 0) AS ttfb_sum,
                   COALESCE(c.provider, MAX(e.provider)) AS provider,
                   {name_expr} AS credential_name,
                   {_percentile_columns()}
            FROM usage_events e
            LEFT JOIN credentials c ON c.id = e.credential_id
            {join}
            {clause}
            GROUP BY e.{key} ORDER BY {_group_order(sort, order)}
            """, [*join_params, *params]).fetchall()
        return [self._group_row(row, key, ("provider", "credential_name")) for row in rows]

    def by_provider(self, *, username: str | None = None,
                    since: int | None = None, until: int | None = None,
                    sort: str | None = None,
                    order: str | None = None) -> list[dict[str, Any]]:
        """按渠道聚合（读小时汇总，与总览/图表同源）。"""
        return self._group_hourly("provider", username=username, since=since, until=until,
                                  sort=sort, order=order)

    def by_model(self, *, username: str | None = None,
                 since: int | None = None, until: int | None = None,
                 sort: str | None = None,
                 order: str | None = None) -> list[dict[str, Any]]:
        """按模型聚合（读小时汇总，与总览/图表同源）。"""
        return self._group_hourly("model", username=username, since=since, until=until,
                                  sort=sort, order=order)

    def by_user(self, *, username: str | None = None,
                since: int | None = None, until: int | None = None,
                sort: str | None = None,
                order: str | None = None) -> list[dict[str, Any]]:
        """按用户聚合（读小时汇总，与总览/图表同源）。"""
        return self._group_hourly("username", username=username, since=since, until=until,
                                  sort=sort, order=order)

    def by_credential(self, *, username: str | None = None,
                      since: int | None = None, until: int | None = None,
                      sort: str | None = None,
                      order: str | None = None,
                      include_credential_name: bool = False) -> list[dict[str, Any]]:
        """按凭证聚合（读明细表：小时汇总主键里没有 credential_id）。

        行里带 `provider`（画渠道 icon）与 `credential_name`（与请求明细同口径
        显示凭证名；未授权/无昵称/凭证已删除时为 None，前端回退显示 ID 前 12 位）。
        """
        return self._group_events("credential_id", username=username, since=since,
                                  until=until, sort=sort, order=order,
                                  include_credential_name=include_credential_name)

    def timeline(self, *, username: str | None = None,
                 since: int | None = None, until: int | None = None,
                 metric: str = "requests") -> list[dict[str, Any]]:
        """按小时的指标时间序列（usage_hourly 聚合，跨 model 汇总）。

        每个点含当日出现过的各渠道指标值（按 metric 变化：请求数 / 总 token /
        平均耗时 ms / 平均首字延迟 ms），供前端绘制曲线。渠道集合动态取自
        数据（新增渠道无需改 SQL），返回结构形如 {hour, codebuddy, trae, qoder}；
        某渠道在该小时没有数据时补 0，保证同一批点的键集合一致，前端画线不
        会因缺键断线。
        """
        expr, _as_mean = self._metric_sql(metric)
        where, params = self._where(username=username, since=since, since_col="hour_utc",
                                    until=until, until_col="hour_utc")
        rows = self._db.connect().execute(
            f"""
            SELECT hour_utc, provider,
                   SUM({expr}) AS value, SUM(ok_count) AS ok_count,
                   SUM(cost_known) AS cost_known
            FROM usage_hourly {where}
            GROUP BY hour_utc, provider
            ORDER BY hour_utc
            """, params).fetchall()
        raw: dict[int, dict[str, Any]] = {}
        providers: set[str] = set()
        for row in rows:
            hour = row["hour_utc"]
            provider = row["provider"]
            providers.add(provider)
            raw.setdefault(hour, {})[provider] = self._metric_value(
                metric, row["value"], row["ok_count"], row["cost_known"])
        # 渠道按名排序：同一批点的键顺序稳定（与渠道注册顺序无关）
        ordered = sorted(providers)
        return [
            {"hour": hour, **{p: raw[hour].get(p, 0) for p in ordered}}
            for hour in sorted(raw)
        ]

    def events(self, *, username: str | None = None, since: int | None = None,
               until: int | None = None,
               before: int | None = None, limit: int = 50,
               include_credential_name: bool = False, sort: str | None = None,
               order: str | None = None, offset: int = 0,
               provider: str | None = None, model: str | None = None,
               credential_id: str | None = None,
               ok: int | None = None) -> dict[str, Any]:
        """逐请求明细（默认新→旧，rowid 游标分页）。明细仅保留 90 天。

        **两种分页模式**：

        - 默认（`sort=time` 降序、`offset=0`）：rowid 即插入序，稳定且可比大小，
          游标翻页不漏不重；返回 `next_before` 供下一页取「rowid 更小」的记录，
          null 表示到底。
        - 按其它列排序、或时间升序、或显式给 `offset`：改用 `LIMIT/OFFSET` 分页，
          额外返回 `total`（总条数）供前端算页数；`next_before` 恒为 null。

        之所以分两套：任意列排序与「rowid 游标」不兼容（游标只在按 rowid 走时
        成立）。明细只留 90 天，offset 的深翻页成本可接受。

        `provider` / `model` / `credential_id`：钻取过滤（分组表点行 → 只看该
        渠道/模型/凭证的明细）。三者独立、可叠加，`AND` 语义。

        `ok`：只看成功（1）/ 只看失败（0）的明细。失败归因卡片点行即带 `ok=0`
        下钻——否则「失败 12 次」点进去会看到同一组的成功记录，对不上账。

        `include_credential_name`：凭证昵称取自**全局共享池**（常含邮箱/手机），
        只对 admin/operator 返回；viewer 即便只看自己的记录也不该看到池里他人
        凭证的昵称。
        """
        key, desc = parse_sort_order(sort, order, EVENT_SORT_COLUMNS,
                                     default_key="time", default_desc=True)
        cursor_mode = key == "time" and desc and offset == 0
        clause = sql_order(sort, order, EVENT_SORT_COLUMNS, default_key="time",
                           default_desc=True, tiebreak="e.rowid")
        name_expr = ("NULLIF(c.nickname, '')" if include_credential_name else "NULL")
        columns = (
            "e.rowid, e.ts, e.username, e.provider, e.credential_id, e.model, "
            "e.ok, e.error_type, e.input_tokens, e.output_tokens, "
            "e.reasoning_tokens, e.cached_tokens, e.credit, e.credit_estimated, "
            "e.cost_usd, e.cost_cny, e.latency_ms, "
            f"e.ttfb_ms, e.request_id, {name_expr} AS credential_name"
        )
        filters = {"provider": provider, "model": model, "credential_id": credential_id,
                   "ok": ok}
        if cursor_mode:
            where, params = self._where(username=username, since=since, until=until,
                                        before=before, alias="e.", **filters)
            rows = self._db.connect().execute(
                f"""
                SELECT {columns}
                FROM usage_events e
                LEFT JOIN credentials c ON c.id = e.credential_id
                {where}
                ORDER BY {clause} LIMIT ?
                """, [*params, limit + 1]).fetchall()
            has_more = len(rows) > limit
            page = rows[:limit]
            return {
                "events": [dict(row) for row in page],
                "next_before": page[-1]["rowid"] if has_more and page else None,
                "total": None,
            }
        # offset 模式：before 不参与（游标语义仅对 rowid 降序成立）
        where, params = self._where(username=username, since=since, until=until,
                                    alias="e.", **filters)
        total = self._db.connect().execute(
            f"SELECT COUNT(*) AS n FROM usage_events e {where}", params).fetchone()["n"]
        rows = self._db.connect().execute(
            f"""
            SELECT {columns}
            FROM usage_events e
            LEFT JOIN credentials c ON c.id = e.credential_id
            {where}
            ORDER BY {clause} LIMIT ? OFFSET ?
            """, [*params, limit, max(0, offset)]).fetchall()
        return {
            "events": [dict(row) for row in rows],
            "next_before": None,
            "total": int(total),
        }

    def export_events(self, *, username: str | None = None, since: int | None = None,
                      until: int | None = None,
                      include_credential_name: bool = False,
                      provider: str | None = None, model: str | None = None,
                      credential_id: str | None = None,
                      ok: int | None = None,
                      limit: int = EXPORT_MAX_ROWS) -> list[dict[str, Any]]:
        """导出用明细（旧→新，按 rowid 升序），最多 `limit + 1` 条。

        与 `events()` 的差异：固定**升序**（导出文件从头读起更符合直觉）、不做
        分页、不带 `rowid` 语义。多取一条供调用方判断是否被截断（超上限时明确
        告知用户「收窄条件」，而不是静默给一份不全的文件）。

        `ok`：与 `events()` 同义（失败归因下钻后导出，导出的就该是同一批失败）。
        """
        where, params = self._where(
            username=username, since=since, until=until, alias="e.",
            provider=provider, model=model, credential_id=credential_id, ok=ok)
        name_expr = ("NULLIF(c.nickname, '')" if include_credential_name else "NULL")
        rows = self._db.connect().execute(
            f"""
            SELECT e.ts, e.username, e.provider, e.credential_id, e.model,
                   e.ok, e.error_type, e.input_tokens, e.output_tokens,
                   e.reasoning_tokens, e.cached_tokens, e.credit, e.credit_estimated,
                   e.cost_usd, e.cost_cny, e.ttfb_ms, e.latency_ms, e.request_id,
                   {name_expr} AS credential_name
            FROM usage_events e
            LEFT JOIN credentials c ON c.id = e.credential_id
            {where}
            ORDER BY e.rowid ASC LIMIT ?
            """, [*params, limit + 1]).fetchall()
        return [dict(row) for row in rows]

    def errors(self, *, username: str | None = None, since: int | None = None,
               until: int | None = None, limit: int = ERROR_MAX_GROUPS,
               now: int | None = None) -> list[dict[str, Any]]:
        """失败归因：按 `(provider, model, error_type)` 分组计数 + 最近失败时刻。

        数据源是**明细表**（`usage_hourly` 没有 `error_type` 维度），故只覆盖明细
        保留期（90 天）；调用方必须给窗口，未给 `since` 时默认「最近 24 小时」，
        避免一次扫全表。

        `error_type` 为 NULL 的历史行（老库 / 未上报）单独成组：它们也是失败，
        藏起来会让「失败总数」对不上明细——前端把它显示成「失败」。

        `limit` 按失败数降序取前 N 组：归因卡片是「谁最该修」的排行榜，长尾
        组合（同渠道同模型的不同错误类型各算一组）容易把响应撑大。

        `now` 可注入（测试确定性）：只在 `since` 缺省时用于推算默认窗口。
        """
        if since is None:
            since = (now if now is not None else int(time.time())) - ERROR_WINDOW_SECONDS
        where, params = self._where(username=username, since=since, since_col="ts",
                                    until=until, until_col="ts", ok=0)
        rows = self._db.connect().execute(
            f"""
            SELECT provider, model, error_type, COUNT(*) AS failures,
                   MAX(ts) AS last_ts
            FROM usage_events {where}
            GROUP BY provider, model, error_type
            ORDER BY failures DESC, provider, model, error_type
            LIMIT ?
            """, [*params, max(1, limit)]).fetchall()
        return [{"provider": row["provider"], "model": row["model"],
                 "error_type": row["error_type"], "failures": int(row["failures"]),
                 "last_ts": int(row["last_ts"])} for row in rows]

    def model_timeline(self, *, username: str | None = None,
                       since: int | None = None, until: int | None = None,
                       top: int = 6,
                       metric: str = "requests") -> dict[str, Any]:
        """按小时的各模型指标趋势（usage_hourly 聚合）。

        Top N 模型按「请求数」排序（排序口径固定，与图表指标解耦）；
        曲线值按 metric 变化（请求数 / 总 token / 平均耗时 / 平均首字延迟）。
        返回结构 {models, points} 不变。
        """
        expr, as_mean = self._metric_sql(metric)
        where, params = self._where(username=username, since=since, since_col="hour_utc",
                                    until=until, until_col="hour_utc")
        rows = self._db.connect().execute(
            f"""
            SELECT hour_utc, model, SUM(requests) AS requests,
                   SUM({expr}) AS value, SUM(ok_count) AS ok_count,
                   SUM(cost_known) AS cost_known
            FROM usage_hourly {where}
            GROUP BY hour_utc, model
            ORDER BY hour_utc
            """, params).fetchall()
        totals: dict[str, int] = {}
        series: dict[str, dict[int, int]] = {}
        hours: list[int] = []
        for row in rows:
            hour, model = row["hour_utc"], row["model"]
            if not hours or hours[-1] != hour:
                hours.append(hour)
            totals[model] = totals.get(model, 0) + row["requests"]
            series.setdefault(model, {})[hour] = (
                self._metric_value(metric, row["value"], row["ok_count"], row["cost_known"]))
        # Top N 模型：按总量降序；总量相同按名字稳定排序
        top_models = sorted(totals, key=lambda m: (-totals[m], m))[:top]
        points = [
            {"hour": hour, **{m: series.get(m, {}).get(hour, 0) for m in top_models}}
            for hour in hours
        ]
        return {"models": top_models, "points": points}
