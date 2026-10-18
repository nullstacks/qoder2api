/**
 * CC Switch 一键导入深链（`ccswitch://`）。
 *
 * 协议与参数以官方文档为准（cc-switch 用户手册 5.3「Deep Link Protocol」+ 站点
 * 接入指南）：`ccswitch://v1/import?resource=provider&app=..&name=..&endpoint=..`。
 *
 * 三条容易搞错的地方（都是官方文档明确的）：
 * - **scheme 是 `ccswitch://`**，不是 `cc-switch://`（曾按后者写，实为错误）。
 * - **endpoint 形态随 `apiFormat` 而变**：Anthropic 格式（Claude Code）填到根、
 *   **不带 `/v1`**（客户端自己拼 `/v1/messages`）；两个 OpenAI 格式填到 `/v1`
 *   （客户端在 base 上拼 `/chat/completions`、`/responses`）。
 * - **深链只能由用户点击触发**：浏览器会拦截页面加载时的自动跳转，故这里只
 *   生成链接，由 `<a href>` 交给用户点，不做 `location.href` 跳转。
 *
 * 需要 CC Switch v4.0.10 及以上（该版本起支持站点自建导入按钮与 `apiFormat`）。
 */

/** 深链目标 app 与对应的 `apiFormat` / endpoint 形态。 */
export interface CcSwitchTarget {
  /** 官方 `app` 取值。 */
  app: string;
  label: string;
  /** 官方 `apiFormat` 取值。 */
  apiFormat: string;
  /** endpoint 是否带 `/v1` 后缀（Anthropic 格式不带）。 */
  withV1: boolean;
}

/**
 * 本服务支持的三个目标：覆盖三个出口协议，各给一个入口。
 *
 * 刻意**不含** `gemini` / `grokbuild` —— 本服务没有对应出口，生成过去也接不通。
 * OpenCode 走 `openai_chat`，是最通用的一个。
 */
export const CCSWITCH_TARGETS: CcSwitchTarget[] = [
  { app: "claude", label: "Claude Code", apiFormat: "anthropic", withV1: false },
  { app: "codex", label: "Codex CLI", apiFormat: "openai_responses", withV1: true },
  { app: "opencode", label: "OpenCode", apiFormat: "openai_chat", withV1: true },
];

/** 去掉结尾斜杠，避免和 `/v1` 拼出 `//v1`。 */
function trimSlash(url: string): string {
  return url.replace(/\/+$/, "");
}

/** 按目标形态给出该 app 应填的 endpoint。 */
export function endpointFor(origin: string, target: CcSwitchTarget): string {
  const base = trimSlash(origin);
  return target.withV1 ? `${base}/v1` : base;
}

export interface BuildOptions {
  /** 本服务地址（`window.location.origin`）。 */
  origin: string;
  /** 一次性的明文 API Key；为空（未创建 / 已关闭弹窗）时无法生成。 */
  apiKey: string;
  /** 默认模型名；留空则不写入 `model`（Claude Code 会走内置模型列表）。 */
  model?: string;
  /** 卡片显示名，默认「coding2api」。 */
  name?: string;
  /** 供应商主页，默认取 origin。 */
  homepage?: string;
}

/**
 * 生成一条 provider 导入深链。
 *
 * 用 `URLSearchParams` 编码（官方指南强调的第二层编码）：手写拼接最容易漏掉
 * 「Base64 / 特殊字符再 URL 编码」这一步，而 `URLSearchParams` 会正确处理。
 * 空串参数（如未填 `model`）不写入——写了空值等于显式覆盖为空。
 */
export function buildCcSwitchLink(target: CcSwitchTarget, options: BuildOptions): string {
  const name = options.name?.trim() || "coding2api";
  const params = new URLSearchParams({
    resource: "provider",
    app: target.app,
    name,
    endpoint: endpointFor(options.origin, target),
    apiKey: options.apiKey,
    apiFormat: target.apiFormat,
    homepage: options.homepage?.trim() || trimSlash(options.origin),
  });
  const model = options.model?.trim();
  if (model) params.set("model", model);
  return `ccswitch://v1/import?${params.toString()}`;
}
