import { describe, expect, it } from "vitest";
import { ApiError } from "../../api/client";
import { errorText } from "../errors";

describe("errorText", () => {
  it("ApiError 透出后端受控文案", () => {
    expect(errorText(new ApiError(400, "invalid_request", "不能大于 30"), "兜底")).toBe(
      "不能大于 30",
    );
  });

  it("网络层异常回落固定串（不泄漏英文实现细节）", () => {
    expect(errorText(new TypeError("Failed to fetch"), "请求失败，请稍后重试。")).toBe(
      "请求失败，请稍后重试。",
    );
  });

  it("ApiError 但 message 为空时也回落", () => {
    expect(errorText(new ApiError(500, "internal_error", ""), "兜底")).toBe("兜底");
  });

  it("非 Error 值回落", () => {
    expect(errorText("boom", "兜底")).toBe("兜底");
  });
});
