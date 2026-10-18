import type { ModelCatalogEntry, ModelCatalogResponse, ModelInfo } from "./types";
import { ApiError } from "./client";

/**
 * 模型面的**新增字段与端点**（批次 C）。
 *
 * 刻意单独成文件而不改 `types.ts` / `client.ts`：这两个文件是全站共享契约，
 * 新增模型面字段时先在这里落地，等接口稳定后再并入（届时只需搬移，不必重写）。
 */

/** `/v1/models` 条目的刊例价（USD / 百万 token）。只有三项：上游的 cache_write 我们不采集。 */
export interface ModelCost {
  input: number;
  output: number;
  cache_read: number;
}

/** 后端在 `ModelInfo` 上追加的模型面字段（旧后端不带，全部可选）。 */
export type ModelInfoExtras = ModelInfo & {
  /** 用户自定义别名（`MODEL_ALIASES`）条目：前端据此打「自定义别名」徽章 */
  custom_alias?: boolean;
  /** 别名指向的真实对外 id（排障用） */
  alias_target?: string;
  /** 刊例价；缺价时整个字段缺失（不是 0） */
  cost?: ModelCost;
  /** 生效中的推理档位；未配置时整个字段缺失 */
  reasoning_efforts?: string[];
};

/** 该条目是否用户自定义别名（老后端 / 真实条目恒为 false）。 */
export function isCustomAlias(item: ModelInfo): boolean {
  return (item as ModelInfoExtras).custom_alias === true;
}

/** 条目刊例价；缺失返回 undefined（调用方显示 —，不要当成免费）。 */
export function modelCost(item: ModelInfo): ModelCost | undefined {
  return (item as ModelInfoExtras).cost;
}

/** 条目生效中的推理档位；未配置返回空数组。 */
export function reasoningEfforts(item: ModelInfo): string[] {
  return (item as ModelInfoExtras).reasoning_efforts ?? [];
}

/**
 * 别名徽章的 tooltip：带上目标模型，排障时能直接看出「这个名字是谁的别名」。
 */
export function aliasTitle(item: ModelInfo): string {
  const target = (item as ModelInfoExtras).alias_target;
  return target
    ? `用户自定义别名，指向 ${target}（MODEL_ALIASES）`
    : "用户自定义别名（MODEL_ALIASES）";
}

// ------------------------------------------------- 模型可用性（疑似下线）

/** 一条 (渠道, 模型) 的失败聚合（`GET /api/models/availability`）。 */
export interface ModelAvailabilityRow {
  provider: string;
  model: string;
  /** 窗口内的失败次数 */
  failures: number;
  /** 最后一次失败时刻（epoch 秒） */
  last_failed_at: number;
  /** 是否已被当前 `MODEL_BLOCKLIST` 命中（现算，热更后立即反映） */
  blocked: boolean;
}

export interface ModelAvailabilityResponse {
  window_days: number;
  since: number;
  rows: ModelAvailabilityRow[];
  /** 命中行数超过 limit：前端提示「只看前 N 条」 */
  truncated: boolean;
  /** 计入口径的 error_type（后端下发，前端不硬编码） */
  error_types: string[];
}

/** 目录页每行附带的「疑似下线」信息（与 `ModelAvailabilityRow` 同形，去掉内部匹配键）。 */
export type ModelSuspected = ModelAvailabilityRow;

/** 目录行：OpenRouter 条目 + 疑似下线标记。 */
export type ModelCatalogRow = ModelCatalogEntry & { suspected?: ModelSuspected };

export interface ModelCatalogPageResponse
  extends Omit<ModelCatalogResponse, "models"> {
  models: ModelCatalogRow[];
  /** 疑似下线判定窗口（天），后端钳到 [1, 30] */
  availability_days: number;
}

/** 目录页的请求参数（比既有 `api.modelCatalog` 多两项筛选）。 */
export interface ModelCatalogParams {
  sort?: string;
  order?: string;
  q?: string;
  onlyAvailable?: boolean;
  onlySuspected?: boolean;
  availabilityDays?: number;
  limit?: number;
  offset?: number;
}

async function request<T>(path: string): Promise<T> {
  const response = await fetch(path, {
    headers: { "X-Requested-With": "XMLHttpRequest" },
    credentials: "same-origin",
  });
  const text = await response.text();
  const body = text ? (JSON.parse(text) as unknown) : null;
  if (!response.ok) {
    const error = (body as { error?: { code?: string; message?: string } } | null)?.error;
    throw new ApiError(response.status, error?.code ?? "unknown",
      error?.message ?? response.statusText);
  }
  return body as T;
}

function query(params: Record<string, string | number | boolean | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

/** 模型目录（带「疑似下线」筛选）。与 `api.modelCatalog` 同一端点，仅多一个参数。 */
export function fetchModelCatalog(params: ModelCatalogParams = {}): Promise<ModelCatalogPageResponse> {
  return request<ModelCatalogPageResponse>(`/api/model-catalog${query({
    sort: params.sort,
    order: params.order,
    q: params.q,
    only_available: params.onlyAvailable,
    only_suspected: params.onlySuspected,
    availability_days: params.availabilityDays,
    limit: params.limit,
    offset: params.offset,
  })}`);
}

/** 模型可用性只读视图（operator）：最近 N 天按 (渠道, 模型) 的模型级失败。 */
export function fetchModelAvailability(days?: number, limit?: number) {
  return request<ModelAvailabilityResponse>(
    `/api/models/availability${query({ days, limit })}`);
}

// ------------------------------------------------------- 黑名单（一键下线）

/**
 * `MODEL_BLOCKLIST` 文本 → 模式数组（与后端 `blocklist_patterns` 同口径：逗号分隔、
 * 去首尾空白、丢弃空项）。
 */
export function blocklistPatterns(raw: string): string[] {
  return raw.split(",").map((item) => item.trim()).filter((item) => item !== "");
}

/**
 * 把一条模型名并入黑名单文本；已存在（大小写不敏感）则原样返回。
 *
 * 只做「追加一条精确名」：用户要通配可以随后去设置页改。追加而不是覆盖，
 * 否则点一次按钮会静默清掉已有的黑名单（那是安全相关的配置）。
 */
export function withBlocklistPattern(raw: string, pattern: string): string {
  const target = pattern.trim();
  if (target === "") return raw;
  const patterns = blocklistPatterns(raw);
  if (patterns.some((item) => item.toLowerCase() === target.toLowerCase())) {
    return patterns.join(",");
  }
  return [...patterns, target].join(",");
}
