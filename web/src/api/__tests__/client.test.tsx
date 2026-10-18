import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "../client";

describe("api client", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("解析错误响应为 ApiError 并保留 code", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(JSON.stringify({ error: { code: "no_healthy_credential", message: "全挂了" } }), {
          status: 503,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    await expect(api.credentials()).rejects.toMatchObject({
      name: "ApiError",
      status: 503,
      code: "no_healthy_credential",
      message: "全挂了",
    });
  });

  it("错误响应缺少 error 字段时回退到 statusText", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("", { status: 502 })));
    const error = await api.credentials().catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).code).toBe("unknown");
  });

  it("空响应体返回 null 而不抛解析错误", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(null, { status: 200 })));
    await expect(api.session()).resolves.toBeNull();
  });

  it("query 构造：跳过空值，保留数字", async () => {
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        calls.push(String(input));
        return new Response(JSON.stringify({}), { status: 200 });
      }),
    );
    await api.statsOverview("alice", 123);
    expect(calls[0]).toBe("/api/stats/overview?username=alice&since=123");
  });

  it("query 构造：无参数时不留问号", async () => {
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        calls.push(String(input));
        return new Response(JSON.stringify({}), { status: 200 });
      }),
    );
    await api.statsOverview();
    expect(calls[0]).toBe("/api/stats/overview");
  });

  it("写操作使用正确的 HTTP 方法与 JSON 头", async () => {
    const spy = vi.fn(async () => new Response(JSON.stringify({ ok: true }), { status: 200 }));
    vi.stubGlobal("fetch", spy);

    await api.toggleCredential("c1", false);
    expect(spy).toHaveBeenCalledWith(
      "/api/credentials/c1/toggle",
      expect.objectContaining({ method: "POST" }),
    );

    await api.deleteCredential("c1");
    expect(spy).toHaveBeenCalledWith("/api/credentials/c1", expect.objectContaining({ method: "DELETE" }));

    await api.pinCredential(null);
    expect(spy).toHaveBeenCalledWith(
      "/api/credentials/pin",
      expect.objectContaining({ method: "POST", body: JSON.stringify({ credential_id: null }) }),
    );

    await api.importCredential("codebuddy", { token: "t" }, "昵称");
    expect(spy).toHaveBeenCalledWith(
      "/api/credentials",
      expect.objectContaining({ method: "POST" }),
    );

    await api.upstreamStart("codebuddy");
    await api.upstreamPoll("codebuddy", "s");
    await api.upstreamCancel("codebuddy", "s");
    await api.probeCredential("c1");
    await api.checkinCredential("c1");
    await api.apiKeys();
    await api.createApiKey({ name: "n", provider_binding: "trae", allowed_ips: "10.0.0.0/8" });
    await api.deleteApiKey("k1");
    await api.statsByProvider("alice", 5);
    await api.logout();
  });

  it("上游状态快照：GET /api/upstream-status，无查询参数", async () => {
    const spy = vi.fn(async () => new Response(JSON.stringify({ sources: [] }), { status: 200 }));
    vi.stubGlobal("fetch", spy);

    await api.upstreamStatus();

    expect(spy).toHaveBeenCalledWith(
      "/api/upstream-status",
      expect.objectContaining({ credentials: "same-origin" }),
    );
  });
});
