import { describe, expect, it } from "vitest";
import {
  buildSamplingParams,
  EFFORT_LEVELS,
  effortOptions,
  MAX_OUTPUT_TOKENS,
} from "../playgroundParams";

describe("buildSamplingParams", () => {
  it("三项留空 → 不发送任何键", () => {
    expect(buildSamplingParams({ temperature: "", maxTokens: "", effort: "" })).toEqual({
      params: {},
      error: null,
    });
    // 纯空白同样按「未指定」处理
    expect(buildSamplingParams({ temperature: "  ", maxTokens: " ", effort: "" }).params).toEqual({});
  });

  it("填了就按数值/字符串透传", () => {
    expect(buildSamplingParams({ temperature: "0.7", maxTokens: "1024", effort: "high" })).toEqual({
      params: { temperature: 0.7, max_tokens: 1024, reasoning_effort: "high" },
      error: null,
    });
  });

  it("温度可为 0（闭下界），并接受科学计数法", () => {
    expect(buildSamplingParams({ temperature: "0", maxTokens: "", effort: "" }).params)
      .toEqual({ temperature: 0 });
    expect(buildSamplingParams({ temperature: "1e3", maxTokens: "", effort: "" }).params)
      .toEqual({ temperature: 1000 });
  });

  it("温度非数/越界 → 报错且不带任何参数", () => {
    const bad = buildSamplingParams({ temperature: "abc", maxTokens: "10", effort: "low" });
    expect(bad.error).toBe("温度必须是数字");
    // 出错时整批作废，避免「温度错了但 max_tokens 静默生效」
    expect(bad.params).toEqual({});
    expect(buildSamplingParams({ temperature: "-0.1", maxTokens: "", effort: "" }).error)
      .toBe("温度需在 0–1000 之间");
    expect(buildSamplingParams({ temperature: "1000.1", maxTokens: "", effort: "" }).error)
      .toBe("温度需在 0–1000 之间");
  });

  it("最大输出必须是正整数且在上界内", () => {
    expect(buildSamplingParams({ temperature: "", maxTokens: "1.5", effort: "" }).error)
      .toBe("最大输出必须是整数");
    expect(buildSamplingParams({ temperature: "", maxTokens: "0", effort: "" }).error)
      .toBe(`最大输出需在 1–${MAX_OUTPUT_TOKENS} 之间`);
    expect(buildSamplingParams({ temperature: "", maxTokens: String(MAX_OUTPUT_TOKENS + 1), effort: "" }).error)
      .toBe(`最大输出需在 1–${MAX_OUTPUT_TOKENS} 之间`);
    expect(buildSamplingParams({ temperature: "", maxTokens: String(MAX_OUTPUT_TOKENS), effort: "" }).params)
      .toEqual({ max_tokens: MAX_OUTPUT_TOKENS });
  });
});

describe("effortOptions", () => {
  it("标了 supports_reasoning 才给标准档位", () => {
    expect(effortOptions({ supports_reasoning: true })).toEqual(EFFORT_LEVELS);
  });

  it("只要给了 default_effort 也展示（上游没显式标布尔）", () => {
    expect(effortOptions({ default_effort: "medium" })).toEqual(EFFORT_LEVELS);
  });

  it("不支持推理且无默认档 → 不渲染控件", () => {
    expect(effortOptions({ supports_reasoning: false })).toBeNull();
    expect(effortOptions({})).toBeNull();
    expect(effortOptions(undefined)).toBeNull();
  });

  it("上游默认档不在标准枚举里则补进候选，不吞掉", () => {
    expect(effortOptions({ supports_reasoning: true, default_effort: "minimal" }))
      .toEqual([...EFFORT_LEVELS, "minimal"]);
    // 已在枚举内不重复
    expect(effortOptions({ supports_reasoning: true, default_effort: "high" }))
      .toEqual(EFFORT_LEVELS);
  });
});
