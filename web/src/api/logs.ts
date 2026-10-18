import { ApiError } from "./client";

/** 一行服务端日志（已脱敏；`request_id` 仅在有请求上下文时下发）。 */
export interface LogLine {
  seq: number;
  ts: number;
  level: string;
  logger: string;
  message: string;
  request_id?: string;
}

export interface LogsResponse {
  /** 旧 → 新。 */
  lines: LogLine[];
  /** 端点当前生效的缓冲容量（热更后变化）。 */
  capacity: number;
  /** 本次实际生效的 limit（已夹到 1..capacity）。 */
  limit: number;
}

/** 日志级别筛选：只影响客户端展示，过滤不重新请求。 */
export type LogLevel = "DEBUG" | "INFO" | "WARNING" | "ERROR" | "CRITICAL";

export const LOG_LEVELS: LogLevel[] = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"];

/** `GET /api/logs?limit=`：页面首次加载 / 不支持 SSE 时的兜底。 */
export async function fetchLogs(limit?: number, signal?: AbortSignal): Promise<LogsResponse> {
  const search = new URLSearchParams();
  if (limit !== undefined) search.set("limit", String(limit));
  const text = search.toString();
  const response = await fetch(`/api/logs${text ? `?${text}` : ""}`, {
    credentials: "same-origin",
    signal,
  });
  const body = (await response.json()) as unknown;
  if (!response.ok) {
    const error = (body as { error?: { code?: string; message?: string } } | null)?.error;
    throw new ApiError(response.status, error?.code ?? "unknown",
                       error?.message ?? response.statusText);
  }
  return body as LogsResponse;
}

/** SSE 地址：交给 `EventSource` 订阅（自动带同源 Cookie、断线重连）。 */
export function logsStreamUrl(limit?: number): string {
  return limit === undefined ? "/api/logs/stream" : `/api/logs/stream?limit=${limit}`;
}

/**
 * 订阅日志实时流。
 *
 * `EventSource` 打开即由后端回放缓冲里的最近 N 行，随后实时推送；本函数只做
 * 「解析 + 回调 + 生命周期」，去重与排序由调用方按 `seq` 处理。返回 `close`
 * 供页面暂停/卸载时断开（暂停 = 断开连接，不占后端订阅槽位）。
 */
export function subscribeLogs(options: {
  limit?: number;
  onLine: (line: LogLine) => void;
  onStatus?: (status: "live" | "error") => void;
}): () => void {
  const { limit, onLine, onStatus } = options;
  if (typeof EventSource === "undefined") {
    // 老浏览器 / 测试环境未提供：不假装已连上，页面按「不支持」提示
    onStatus?.("error");
    return () => undefined;
  }
  const source = new EventSource(logsStreamUrl(limit));
  onStatus?.("live");
  source.onmessage = (event: MessageEvent<string>) => {
    let parsed: LogLine;
    try {
      parsed = JSON.parse(event.data) as LogLine;
    } catch {
      return; // 半截帧 / 非 JSON：丢弃这一条，连接继续
    }
    if (typeof parsed?.seq !== "number" || typeof parsed?.message !== "string") return;
    onLine(parsed);
  };
  source.onerror = () => {
    // readyState=CLOSED：浏览器放弃重连（401/403/404/429），页面显示断开
    onStatus?.(source.readyState === EventSource.CLOSED ? "error" : "live");
  };
  return () => {
    source.close();
  };
}
