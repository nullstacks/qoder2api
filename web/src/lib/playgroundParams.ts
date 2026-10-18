/**
 * Playground 高级参数（采样/输出上限/推理档位）的解析与组装。
 *
 * 抽成独立模块：`PlaygroundPage` 里这三个控件是纯字符串 state，组装成请求体
 * 前要过一遍校验，而校验边界必须与后端 `compat/openai/request.py` 一致，
 * 否则前端放过、后端 400，用户看到的是一句上游风格的英文错误。
 *
 * 三条不显然的约束：
 * - **空值 = 不发送该键**（不是发 `null`）：三个参数都不是本服务必填，
 *   缺省时各渠道有各自的默认（CodeBuddy 缺 `reasoning_effort` 时补 `medium`），
 *   发 `null` 反而会被上游当成显式覆盖。
 * - **上界跟随后端**：`temperature` 到 1000、`max_tokens` 到 4_000_000，
 *   不做比 API 更严的限制——Playground 是用来探渠道行为的，卡在「OpenAI 惯例
 *   0–2」会让用户没法复现 API 能发出的请求。
 * - **推理档位枚举不由上游给出**：`/v1/models` 只透出 `supports_reasoning`
 *   与单个 `default_effort`，没有完整枚举，故用 OpenAI 标准档位作候选，
 *   并把上游声明的默认档补进列表。
 */

/** OpenAI 标准推理档位（CodeBuddy 实测只用 high/medium，其余渠道未实测）。 */
export const EFFORT_LEVELS = ["low", "medium", "high"];

/** 与后端 `MAX_TEMPERATURE` / `MAX_OUTPUT_TOKENS` 对齐。 */
export const MAX_TEMPERATURE = 1000;
export const MAX_OUTPUT_TOKENS = 4_000_000;

export interface SamplingParams {
  temperature?: number;
  max_tokens?: number;
  reasoning_effort?: string;
}

export interface BuildResult {
  /** 要并入请求体的键；空对象表示三个参数都未指定 */
  params: SamplingParams;
  /** 首个校验失败的中文提示；非空时不应发送请求 */
  error: string | null;
}

/**
 * 三个输入框的字符串值 → 请求体片段（含校验）。
 *
 * 数值解析用 `Number`：`"1e3"` 这类科学计数法是合法数字，一并接受；
 * `""` 走「未指定」分支，不报错。
 */
export function buildSamplingParams(input: {
  temperature: string;
  maxTokens: string;
  effort: string;
}): BuildResult {
  const params: SamplingParams = {};

  const temperature = input.temperature.trim();
  if (temperature !== "") {
    const value = Number(temperature);
    if (!Number.isFinite(value)) return { params: {}, error: "温度必须是数字" };
    if (value < 0 || value > MAX_TEMPERATURE) {
      return { params: {}, error: `温度需在 0–${MAX_TEMPERATURE} 之间` };
    }
    params.temperature = value;
  }

  const maxTokens = input.maxTokens.trim();
  if (maxTokens !== "") {
    const value = Number(maxTokens);
    if (!Number.isInteger(value)) return { params: {}, error: "最大输出必须是整数" };
    if (value <= 0 || value > MAX_OUTPUT_TOKENS) {
      return { params: {}, error: `最大输出需在 1–${MAX_OUTPUT_TOKENS} 之间` };
    }
    params.max_tokens = value;
  }

  const effort = input.effort.trim();
  if (effort !== "") params.reasoning_effort = effort;

  return { params, error: null };
}

/**
 * 该模型可选的推理档位；不适用时返回 `null`（不渲染控件）。
 *
 * 只有当上游明确标注 `supports_reasoning` 或给出 `default_effort` 时才展示：
 * 两者都缺说明该模型没透出推理信息，控件给了也无参照。上游声明的默认档
 * 若不在标准枚举里（如将来出现 `minimal`），补进候选而不是吞掉。
 */
export function effortOptions(
  info?: { supports_reasoning?: boolean; default_effort?: string },
): string[] | null {
  if (!info) return null;
  const preset = (info.default_effort ?? "").trim();
  if (info.supports_reasoning !== true && preset === "") return null;
  const options = [...EFFORT_LEVELS];
  if (preset !== "" && !options.includes(preset)) options.push(preset);
  return options;
}
