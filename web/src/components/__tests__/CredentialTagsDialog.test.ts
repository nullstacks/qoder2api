import { describe, expect, it } from "vitest";
import {
  MAX_TAG_LENGTH,
  MAX_TAGS,
  parseTagInput,
  validateTags,
} from "../CredentialTagsDialog";

describe("parseTagInput", () => {
  it("半角/全角逗号与分号都当分隔符，去空白", () => {
    expect(parseTagInput("生产, 备用")).toEqual(["生产", "备用"]);
    expect(parseTagInput("生产，备用；测试;prod")).toEqual(["生产", "备用", "测试", "prod"]);
  });

  it("空串与连续分隔符不产生空标签", () => {
    expect(parseTagInput("")).toEqual([]);
    expect(parseTagInput("  ,  ，; ")).toEqual([]);
  });

  it("去重大小写不敏感，保留首次写法", () => {
    expect(parseTagInput("Prod, prod, PROD")).toEqual(["Prod"]);
  });

  it("不 trim 标签内部空格（「低 倍率」是一个标签）", () => {
    expect(parseTagInput("低 倍率, 备用")).toEqual(["低 倍率", "备用"]);
  });
});

describe("validateTags", () => {
  it("合法输入通过", () => {
    expect(validateTags([])).toBeNull();
    expect(validateTags(["生产", "备用"])).toBeNull();
    expect(validateTags(["x".repeat(MAX_TAG_LENGTH)])).toBeNull();
  });

  it("条数超限报错（与后端 MAX_TAGS 对齐）", () => {
    const tags = Array.from({ length: MAX_TAGS + 1 }, (_, index) => `t${index}`);
    expect(validateTags(tags)).toBe(`标签最多 ${MAX_TAGS} 条`);
  });

  it("单条超长报错并截断回显", () => {
    const message = validateTags(["x".repeat(MAX_TAG_LENGTH + 1)]);
    expect(message).toContain(`最长 ${MAX_TAG_LENGTH} 个字符`);
    expect(message).toContain("x".repeat(MAX_TAG_LENGTH));
  });
});
