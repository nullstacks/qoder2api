import { useQuery } from "@tanstack/react-query";
import { adminKey } from "./hooks";

/**
 * 失败归因（`GET /api/stats/errors`，operator+）。
 *
 * 数据源是逐请求明细（`usage_hourly` 没有 `error_type` 维度），故只覆盖明细
 * 保留期（90 天）；后端默认窗口 24 小时。这里单独成文件而不是塞进 `client.ts`：
 * 统计页的这批新接口与既有请求函数没有共用逻辑，独立文件让改动面最小。
 */
export interface StatsErrorRow {
  provider: string;
  model: string;
  /** 受控错误类型；NULL = 老库历史行 / 未上报（前端显示「失败」） */
  error_type: string | null;
  /** 该 (渠道, 模型, 错误类型) 组合的失败次数 */
  failures: number;
  /** 最近一次失败的时刻（UTC 秒） */
  last_ts: number;
}

export interface StatsErrorsResponse {
  errors: StatsErrorRow[];
}

function queryString(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

async function fetchStatsErrors(
  username?: string,
  since?: number,
  until?: number,
  limit?: number,
): Promise<StatsErrorsResponse> {
  const response = await fetch(
    `/api/stats/errors${queryString({ username, since, until, limit })}`,
    { credentials: "same-origin" },
  );
  if (!response.ok) {
    throw new Error(`失败归因加载失败（HTTP ${response.status}）`);
  }
  return (await response.json()) as StatsErrorsResponse;
}

/** 失败归因排行（失败数降序）。窗口/limit 进 query key：换范围即重新取数。
 *
 * `enabled=false` 时不发请求：端点仅 operator+，viewer 打开会拿到 403，
 * 前端干脆不渲染该区块（页面按角色隐藏）。 */
export function useStatsErrors(
  username?: string,
  since?: number,
  until?: number,
  limit?: number,
  enabled = true,
) {
  return useQuery({
    queryKey: adminKey(username, "stats-errors", since, until, limit),
    queryFn: () => fetchStatsErrors(username, since, until, limit),
    enabled,
  });
}
