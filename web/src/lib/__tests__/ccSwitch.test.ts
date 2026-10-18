import { describe, expect, it } from "vitest";
import {
  buildCcSwitchLink,
  CCSWITCH_TARGETS,
  endpointFor,
} from "../ccSwitch";

const target = (app: string) => {
  const found = CCSWITCH_TARGETS.find((item) => item.app === app);
  if (!found) throw new Error(`unknown target ${app}`);
  return found;
};

describe("endpointFor", () => {
  it("Anthropic 格式填到根（客户端自拼 /v1/messages）", () => {
    expect(endpointFor("https://gateway.example.com", target("claude")))
      .toBe("https://gateway.example.com");
  });

  it("两个 OpenAI 格式填到 /v1（客户端在 base 上拼路径）", () => {
    expect(endpointFor("https://gateway.example.com", target("codex")))
      .toBe("https://gateway.example.com/v1");
    expect(endpointFor("https://gateway.example.com", target("opencode")))
      .toBe("https://gateway.example.com/v1");
  });

  it("结尾斜杠不会拼出 //v1", () => {
    expect(endpointFor("https://gateway.example.com/", target("codex")))
      .toBe("https://gateway.example.com/v1");
    expect(endpointFor("https://gateway.example.com//", target("claude")))
      .toBe("https://gateway.example.com");
  });
});

describe("buildCcSwitchLink", () => {
  it("生成官方 ccswitch:// 深链与必备参数", () => {
    const link = buildCcSwitchLink(target("claude"), {
      origin: "https://gateway.example.com",
      apiKey: "sk-abc",
      model: "glm-5.2",
    });
    expect(link.startsWith("ccswitch://v1/import?")).toBe(true);
    const params = new URLSearchParams(link.slice("ccswitch://v1/import?".length));
    expect(params.get("resource")).toBe("provider");
    expect(params.get("app")).toBe("claude");
    expect(params.get("name")).toBe("coding2api");
    expect(params.get("endpoint")).toBe("https://gateway.example.com");
    expect(params.get("apiKey")).toBe("sk-abc");
    expect(params.get("apiFormat")).toBe("anthropic");
    expect(params.get("model")).toBe("glm-5.2");
    expect(params.get("homepage")).toBe("https://gateway.example.com");
  });

  it("model 留空则不写入该参数", () => {
    const link = buildCcSwitchLink(target("opencode"), {
      origin: "https://gateway.example.com",
      apiKey: "sk-abc",
      model: "   ",
    });
    const params = new URLSearchParams(link.slice("ccswitch://v1/import?".length));
    expect(params.has("model")).toBe(false);
    expect(params.get("apiFormat")).toBe("openai_chat");
    expect(params.get("endpoint")).toBe("https://gateway.example.com/v1");
  });

  it("scheme 是 ccswitch 而非 cc-switch（曾照报告写错）", () => {
    const link = buildCcSwitchLink(target("codex"), {
      origin: "https://gateway.example.com",
      apiKey: "sk-abc",
    });
    expect(link.startsWith("ccswitch://")).toBe(true);
    expect(link.startsWith("cc-switch://")).toBe(false);
  });

  it("特殊字符经 URL 编码（Key / 名称里的 & 与空格不破坏参数表）", () => {
    const link = buildCcSwitchLink(target("claude"), {
      origin: "https://gateway.example.com",
      apiKey: "sk-a&b=c d",
      name: "我的 网关",
    });
    const params = new URLSearchParams(link.slice("ccswitch://v1/import?".length));
    expect(params.get("apiKey")).toBe("sk-a&b=c d");
    expect(params.get("name")).toBe("我的 网关");
  });
});
