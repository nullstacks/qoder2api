import { screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiKeysPage } from "../ApiKeysPage";
import { jsonResponse, mockFetch, renderPage, settle, userEvent } from "@/test-utils";

const KEY = {
  id: "key_1",
  username: "root",
  name: "laptop",
  preview: "sk-…Ab3d",
  created_at: 1_700_000_000,
  last_used_at: null,
  provider_binding: "",
  allowed_ips: "",
};

describe("OpenAI 客户端接入面板", () => {
  it("显示 Base URL 与端点，未创建 Key 时示例用占位符", async () => {
    mockFetch({ "/api/api-keys": { api_keys: [] } });
    renderPage(<ApiKeysPage />);
    await settle();
    await openOpenAIEntry();

    const baseUrl = screen.getByTestId("openai-base-url").textContent;
    expect(baseUrl).toBe(`${window.location.origin}/v1`);
    expect(screen.getByTestId("openai-entry")).toHaveTextContent("/chat/completions");
    expect(screen.getByTestId("openai-entry")).toHaveTextContent("/models");
    expect(screen.getByTestId("openai-entry")).toHaveTextContent("/user/balance");
    expect(screen.getByTestId("example-curl")).toHaveTextContent("sk-…");
    expect(screen.getByTestId("example-python")).toHaveTextContent(
      `base_url="${window.location.origin}/v1"`,
    );
    expect(screen.getByTestId("example-balance-curl")).toHaveTextContent(
      `curl ${window.location.origin}/v1/user/balance`,
    );
    expect(screen.getByTestId("example-balance-curl")).toHaveTextContent("sk-…");
    // Responses 端点在此列出，示例默认折叠
    expect(screen.getByTestId("example-details-responses")).toHaveTextContent("/responses");
    expect(screen.getByTestId("openai-entry")).toHaveTextContent("Codex CLI");
    // Anthropic 端点已拆到独立面板，OpenAI 面板不再包含
    expect(screen.queryByTestId("example-details-messages")).not.toBeInTheDocument();
  });
  it("创建 Key 后示例自动带入真实 Key", async () => {
    const fetchSpy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/api-keys" && init?.method === "POST") {
        return jsonResponse({ ...KEY, api_key: "sk-real-key" });
      }
      if (url === "/api/api-keys") return jsonResponse({ api_keys: [] });
      throw new Error(`未 mock: ${url}`);
    });
    vi.stubGlobal("fetch", fetchSpy);
    renderPage(<ApiKeysPage />);
    await settle();
    await openOpenAIEntry();
    await openCreateDialog();
    await userEvent.type(screen.getByTestId("key-name"), "test");
    await userEvent.click(screen.getByRole("button", { name: "创建" }));
    await waitFor(() => {
      expect(screen.getByTestId("example-curl")).toHaveTextContent("sk-real-key");
    });
    expect(screen.getByTestId("example-python")).toHaveTextContent('api_key="sk-real-key"');
    expect(screen.getByTestId("example-balance-curl")).toHaveTextContent("sk-real-key");
    expect(screen.getByTestId("example-responses")).toHaveTextContent(
      "export CODING2API_KEY=sk-real-key",
    );
  });});

describe("Anthropic 客户端接入面板", () => {
  it("Base URL 填到根（SDK 自拼 /v1/messages），Claude Code 示例默认折叠", async () => {
    mockFetch({ "/api/api-keys": { api_keys: [] } });
    renderPage(<ApiKeysPage />);
    await settle();
    await userEvent.click(screen.getByTestId("anthropic-entry-toggle"));

    expect(screen.getByTestId("anthropic-base-url")).toHaveTextContent(
      String(window.location.origin),
    );
    expect(screen.getByTestId("example-details-messages")).toHaveTextContent("/v1/messages");
    expect(screen.getByTestId("example-messages")).toHaveTextContent(
      `export ANTHROPIC_BASE_URL=${window.location.origin}`,
    );
    expect(screen.getByTestId("example-messages")).toHaveTextContent("ANTHROPIC_AUTH_TOKEN=sk-…");
  });

  it("创建 Key 后 Claude Code 示例自动带入真实 Key", async () => {
    const fetchSpy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/api-keys" && init?.method === "POST") {
        return jsonResponse({ ...KEY, api_key: "sk-real-key" });
      }
      if (url === "/api/api-keys") return jsonResponse({ api_keys: [] });
      throw new Error(`未 mock: ${url}`);
    });
    vi.stubGlobal("fetch", fetchSpy);
    renderPage(<ApiKeysPage />);
    await settle();
    await userEvent.click(screen.getByTestId("anthropic-entry-toggle"));
    await openCreateDialog();
    await userEvent.type(screen.getByTestId("key-name"), "test");
    await userEvent.click(screen.getByRole("button", { name: "创建" }));
    await waitFor(() => {
      expect(screen.getByTestId("example-messages")).toHaveTextContent(
        "export ANTHROPIC_AUTH_TOKEN=sk-real-key",
      );
    });
  });
});


describe("CC Switch 一键导入", () => {
  it("页面不再有常驻说明面板，未创建 Key 时没有可点的导入按钮", async () => {
    mockFetch({ "/api/api-keys": { api_keys: [] } });
    renderPage(<ApiKeysPage />);
    await settle();

    expect(screen.queryByTestId("ccswitch-entry")).not.toBeInTheDocument();
    expect(screen.queryByTestId("ccswitch-entry-toggle")).not.toBeInTheDocument();
    expect(screen.queryByTestId("ccswitch-claude")).not.toBeInTheDocument();
    expect(screen.queryByTestId("ccswitch-import")).not.toBeInTheDocument();
  });

  it("创建 Key 后三个目标各自生成 ccswitch:// 深链", async () => {
    await createKey();
    await waitFor(() => expect(screen.getByTestId("ccswitch-claude")).toHaveAttribute("href"));

    const href = (testid: string) => screen.getByTestId(testid).getAttribute("href") ?? "";
    const claude = new URLSearchParams(href("ccswitch-claude").split("?")[1]);
    expect(href("ccswitch-claude").startsWith("ccswitch://v1/import?")).toBe(true);
    expect(claude.get("app")).toBe("claude");
    expect(claude.get("apiFormat")).toBe("anthropic");
    expect(claude.get("endpoint")).toBe(window.location.origin);   // Anthropic 不带 /v1
    expect(claude.get("apiKey")).toBe("sk-real-key");

    const codex = new URLSearchParams(href("ccswitch-codex").split("?")[1]);
    expect(codex.get("apiFormat")).toBe("openai_responses");
    expect(codex.get("endpoint")).toBe(`${window.location.origin}/v1`);

    const opencode = new URLSearchParams(href("ccswitch-opencode").split("?")[1]);
    expect(opencode.get("apiFormat")).toBe("openai_chat");
  });

  it("关闭创建对话框后导入按钮随之消失（与明文 Key 同生命周期）", async () => {
    await createKey();
    expect(await screen.findByTestId("ccswitch-claude")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "我已保存" }));

    expect(screen.queryByTestId("create-key-dialog")).not.toBeInTheDocument();
    expect(screen.queryByTestId("ccswitch-claude")).not.toBeInTheDocument();
    expect(screen.queryByTestId("ccswitch-import")).not.toBeInTheDocument();
  });

  it("默认模型下拉来自 Playground 模型列表，选中后写进深链", async () => {
    await createKey();
    await waitFor(() => expect(screen.getByTestId("ccswitch-claude")).toHaveAttribute("href"));

    // 下拉分组展示模型（多渠道自动调度 / 仅 XX），值为 Playground 口径
    await userEvent.selectOptions(screen.getByTestId("ccswitch-model"), "glm-5.2");
    const link = screen.getByTestId("ccswitch-claude").getAttribute("href") ?? "";
    expect(new URLSearchParams(link.split("?")[1]).get("model")).toBe("glm-5.2");

    // 留空（不写入）时深链不带 model 参数
    await userEvent.selectOptions(screen.getByTestId("ccswitch-model"), "");
    const empty = screen.getByTestId("ccswitch-claude").getAttribute("href") ?? "";
    expect(new URLSearchParams(empty.split("?")[1]).get("model")).toBeNull();
  });
});


describe("API Key 列表「导入 CC Switch」", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("行内按钮打开对话框：未粘贴 Key 时导入按钮禁用，粘贴后生成深链", async () => {
    mockFetch({
      "/api/api-keys": { api_keys: [KEY] },
      "/api/playground/models": PLAYGROUND_MODELS,
    });
    renderPage(<ApiKeysPage />);
    await settle();

    await userEvent.click(screen.getByTestId("import-ccswitch-key_1"));
    expect(await screen.findByTestId("ccswitch-row-dialog")).toBeInTheDocument();
    // 明文未知：三个导入按钮先禁用，且没有可点的深链
    expect(screen.getByTestId("ccswitch-row-claude")).toBeDisabled();
    expect(screen.getByTestId("ccswitch-row-claude").getAttribute("href")).toBeNull();

    await userEvent.type(screen.getByTestId("ccswitch-row-key"), "sk-pasted-key");
    const href = screen.getByTestId("ccswitch-row-claude").getAttribute("href") ?? "";
    expect(href.startsWith("ccswitch://v1/import?")).toBe(true);
    expect(new URLSearchParams(href.split("?")[1]).get("apiKey")).toBe("sk-pasted-key");
  });

  it("关闭对话框后清除粘贴的 Key 并卸载按钮", async () => {
    mockFetch({
      "/api/api-keys": { api_keys: [KEY] },
      "/api/playground/models": PLAYGROUND_MODELS,
    });
    renderPage(<ApiKeysPage />);
    await settle();

    await userEvent.click(screen.getByTestId("import-ccswitch-key_1"));
    await userEvent.type(await screen.findByTestId("ccswitch-row-key"), "sk-pasted-key");
    await userEvent.click(screen.getByTestId("ccswitch-row-close"));

    expect(screen.queryByTestId("ccswitch-row-dialog")).not.toBeInTheDocument();
    expect(screen.queryByTestId("ccswitch-row-claude")).not.toBeInTheDocument();
    expect(screen.queryByText(/sk-pasted-key/)).not.toBeInTheDocument();

    // 重新打开：输入框是空的（明文不残留）
    await userEvent.click(screen.getByTestId("import-ccswitch-key_1"));
    expect(await screen.findByTestId("ccswitch-row-key")).toHaveValue("");
  });
});


it("调用示例默认折叠，点击端点行展开", async () => {
  mockFetch({ "/api/api-keys": { api_keys: [] } });
  renderPage(<ApiKeysPage />);
  await settle();
  await openOpenAIEntry();

  const trigger = screen.getByTestId("example-details-trigger");
  expect(trigger).toHaveAttribute("data-state", "closed");
  await userEvent.click(trigger);
  expect(trigger).toHaveAttribute("data-state", "open");
  expect(screen.getByTestId("example-curl")).toBeVisible();
});

it("余额端点示例同样默认折叠，展开后可复制 curl", async () => {
  const writeText = vi.fn(async () => undefined);
  vi.stubGlobal("navigator", { clipboard: { writeText } });
  mockFetch({ "/api/api-keys": { api_keys: [] } });
  renderPage(<ApiKeysPage />);
  await settle();
  await openOpenAIEntry();

  const trigger = screen.getByTestId("example-details-balance-trigger");
  expect(trigger).toHaveAttribute("data-state", "closed");
  await userEvent.click(trigger);
  expect(trigger).toHaveAttribute("data-state", "open");

  const copy = screen.getByTestId("copy-balance-curl");
  await userEvent.click(copy);
  expect(writeText).toHaveBeenCalledWith(
    `curl ${window.location.origin}/v1/user/balance \\
  -H "Authorization: Bearer sk-…"`,
  );
});

it("Responses 端点示例默认折叠，展开后可复制 Codex CLI 配置", async () => {
  const writeText = vi.fn(async () => undefined);
  vi.stubGlobal("navigator", { clipboard: { writeText } });
  mockFetch({ "/api/api-keys": { api_keys: [] } });
  renderPage(<ApiKeysPage />);
  await settle();
  await openOpenAIEntry();

  const trigger = screen.getByTestId("example-details-responses-trigger");
  expect(trigger).toHaveAttribute("data-state", "closed");
  // 折叠时子节点仍在 DOM 里（forceMount），靠 data-state 控制可见性
  expect(screen.getByTestId("example-responses").closest("[data-state]")).toHaveAttribute(
    "data-state",
    "closed",
  );

  await userEvent.click(trigger);
  expect(trigger).toHaveAttribute("data-state", "open");
  expect(screen.getByTestId("example-responses")).toBeVisible();
  expect(screen.getByTestId("example-responses")).toHaveTextContent("wire_api='responses'");
  expect(screen.getByTestId("example-responses")).toHaveTextContent(
    `base_url='${window.location.origin}/v1'`,
  );

  await userEvent.click(screen.getByTestId("copy-responses"));
  expect(writeText).toHaveBeenCalledWith(
    expect.stringContaining("export CODING2API_KEY=sk-…"),
  );
});


/** 展开「OpenAI 客户端接入」面板（默认收起）。 */
async function openOpenAIEntry() {
  await userEvent.click(screen.getByTestId("openai-entry-toggle"));
}

/** 打开「创建 API Key」对话框（由「我的 API Key」右上角按钮触发）。 */
async function openCreateDialog() {
  await userEvent.click(screen.getByTestId("open-create-key-dialog"));
  expect(await screen.findByTestId("create-key-dialog")).toBeInTheDocument();
}

/** 走完「创建 API Key」流程，停在显示一次性明文（含 CC Switch 导入按钮）的对话框内。 */
async function createKey() {
  const fetchSpy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url === "/api/api-keys" && init?.method === "POST") {
      return jsonResponse({ ...KEY, api_key: "sk-real-key" });
    }
    if (url === "/api/api-keys") return jsonResponse({ api_keys: [] });
    if (url.includes("/api/playground/models")) return jsonResponse(PLAYGROUND_MODELS);
    throw new Error(`未 mock: ${url}`);
  });
  vi.stubGlobal("fetch", fetchSpy);
  renderPage(<ApiKeysPage />);
  await settle();
  await openCreateDialog();
  await userEvent.type(screen.getByTestId("key-name"), "test");
  await userEvent.click(screen.getByRole("button", { name: "创建" }));
  expect(await screen.findByTestId("new-key-plaintext")).toHaveTextContent("sk-real-key");
}

/** Playground 模型列表（`/api/playground/models`）：一个多渠道 + 两个单渠道。 */
const PLAYGROUND_MODELS = {
  object: "list",
  data: [
    { id: "glm-5.2", object: "model", owned_by: "Coding2API", providers: ["codebuddy", "trae"] },
    { id: "kimi-k3", object: "model", owned_by: "Coding2API", providers: ["zen"] },
    { id: "glm-5.2-turbo", object: "model", owned_by: "Coding2API", providers: ["trae"] },
  ],
};
