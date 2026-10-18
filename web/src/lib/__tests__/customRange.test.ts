import { describe, expect, it } from "vitest";
import { buildCustomRange, parseMoment, toLocalInput } from "../customRange";

describe("parseMoment", () => {
  it("空串 → null（单边不限）", () => {
    expect(parseMoment("")).toBeNull();
    expect(parseMoment("   ")).toBeNull();
  });

  it("按本地时区解析为 epoch 秒", () => {
    // 2026-01-02T03:04 本地时区：任意时区下都解析成同一个绝对时刻
    const expected = Math.floor(new Date("2026-01-02T03:04").getTime() / 1000);
    expect(parseMoment("2026-01-02T03:04")).toBe(expected);
  });

  it("非法值抛错", () => {
    expect(() => parseMoment("not-a-date")).toThrow();
  });
});

describe("toLocalInput", () => {
  it("按本地年月日时分拼串（不走 UTC）", () => {
    const date = new Date(2026, 0, 2, 3, 4); // 本地 2026-01-02 03:04
    expect(toLocalInput(date)).toBe("2026-01-02T03:04");
  });

  it("个位数月/日/时/分补零", () => {
    const date = new Date(2026, 8, 9, 5, 6); // 本地 2026-09-09 05:06
    expect(toLocalInput(date)).toBe("2026-09-09T05:06");
  });
});

describe("buildCustomRange", () => {
  const from = "2026-01-02T03:04";
  const to = "2026-01-03T03:04";

  it("两端都填 → 半开区间（since 闭、until 开）", () => {
    const range = buildCustomRange(from, to);
    expect(range).toEqual({
      since: Math.floor(new Date(from).getTime() / 1000),
      until: Math.floor(new Date(to).getTime() / 1000),
    });
  });

  it("只填一端 → 单边不限", () => {
    expect(buildCustomRange(from, "")).toEqual({
      since: Math.floor(new Date(from).getTime() / 1000),
    });
    expect(buildCustomRange("", to)).toEqual({
      until: Math.floor(new Date(to).getTime() / 1000),
    });
  });

  it("两端都空 → null（等同全部，走预设逻辑）", () => {
    expect(buildCustomRange("", "")).toBeNull();
  });

  it("起点不早于终点 → null（整体作废，不单边生效）", () => {
    expect(buildCustomRange(to, from)).toBeNull();
    expect(buildCustomRange(from, from)).toBeNull();
  });

  it("非法输入 → null", () => {
    expect(buildCustomRange("bad", to)).toBeNull();
  });
});
