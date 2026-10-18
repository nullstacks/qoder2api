import { screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AlertsPage } from "../AlertsPage";
import { renderPage, settle, userEvent } from "@/test-utils";

function stubFetch(body: unknown, status = 200) {
  const spy = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
    new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    }),
  );
  vi.stubGlobal("fetch", spy);
  return spy;
}

const ALERT = {
  id: "alert_1", ts: 1_700_000_000, rule: "pool_empty", severity: "critical",
  scope: "pool", message: "凭证池可用数为 0", detail: "{}", delivered: 1,
  delivery_error: null, resolved_at: null,
};

describe("AlertsPage", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("渲染告警行：规则标签、级别、推送状态", async () => {
    stubFetch({ alerts: [
      ALERT,
      { ...ALERT, id: "alert_2", rule: "task_failed", severity: "warning",
        scope: "growth", delivered: 0, delivery_error: null },
      { ...ALERT, id: "alert_3", rule: "error_rate", severity: "warning",
        scope: "pool", delivered: 0, delivery_error: "500" },
    ] });
    renderPage(<AlertsPage />, { username: "root", is_admin: true });
    await settle();

    expect(screen.getByTestId("alerts-table")).toBeInTheDocument();
    expect(screen.getByTestId("alert-row-alert_1")).toHaveTextContent("凭证池耗尽");
    expect(screen.getByTestId("alert-row-alert_1")).toHaveTextContent("严重");
    expect(screen.getByTestId("alert-row-alert_1")).toHaveTextContent("已推送");
    expect(screen.getByTestId("alert-row-alert_2")).toHaveTextContent("后台任务连续失败");
    expect(screen.getByTestId("alert-row-alert_2")).toHaveTextContent("站内");
    expect(screen.getByTestId("alert-row-alert_3")).toHaveTextContent("推送失败");
  });

  it("已恢复的告警显示「已恢复」而非严重度", async () => {
    stubFetch({ alerts: [
      { ...ALERT, id: "alert_r", resolved_at: 1_700_000_500 },
    ] });
    renderPage(<AlertsPage />, { username: "root", is_admin: true });
    await settle();
    const row = screen.getByTestId("alert-row-alert_r");
    expect(row).toHaveTextContent("已恢复");
    expect(row).not.toHaveTextContent("严重");
  });

  it("未知规则回落到原始 key，不崩", async () => {
    stubFetch({ alerts: [{ ...ALERT, id: "alert_x", rule: "brand_new_rule" }] });
    renderPage(<AlertsPage />, { username: "root", is_admin: true });
    await settle();
    expect(screen.getByTestId("alert-row-alert_x")).toHaveTextContent("brand_new_rule");
  });

  it("无记录时显示占位", async () => {
    stubFetch({ alerts: [] });
    renderPage(<AlertsPage />, { username: "root", is_admin: true });
    await settle();
    expect(screen.getByTestId("no-alerts")).toBeInTheDocument();
  });

  it("未恢复的行有「标记恢复」按钮，已恢复的没有", async () => {
    stubFetch({ alerts: [
      ALERT,
      { ...ALERT, id: "alert_done", resolved_at: 1_700_000_500 },
    ] });
    renderPage(<AlertsPage />, { username: "root", is_admin: true });
    await settle();
    expect(screen.getByTestId("alert-resolve-alert_1")).toBeInTheDocument();
    expect(screen.queryByTestId("alert-resolve-alert_done")).not.toBeInTheDocument();
  });

  it("点「标记恢复」POST 到对应端点并回读列表", async () => {
    const spy = stubFetch({ alerts: [ALERT] });
    renderPage(<AlertsPage />, { username: "root", is_admin: true });
    await settle();

    await userEvent.click(screen.getByTestId("alert-resolve-alert_1"));
    await settle();

    const call = spy.mock.calls.find(
      ([input, init]) => String(input) === "/api/alerts/alert_1/resolve"
        && (init as RequestInit | undefined)?.method === "POST",
    );
    expect(call).toBeDefined();
    // 成功后失效列表 → 重新拉取（至少两次 GET /api/alerts）
    const listCalls = spy.mock.calls.filter(
      ([input]) => String(input).startsWith("/api/alerts"),
    );
    expect(listCalls.length).toBeGreaterThan(1);
  });

  it("标记恢复失败时展示错误，不静默", async () => {
    const spy = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).includes("/resolve")) {
        return new Response(JSON.stringify({ error: { message: "already resolved" } }),
                            { status: 400, headers: { "Content-Type": "application/json" } });
      }
      return new Response(JSON.stringify({ alerts: [ALERT] }), {
        status: 200, headers: { "Content-Type": "application/json" },
      });
    });
    vi.stubGlobal("fetch", spy);
    renderPage(<AlertsPage />, { username: "root", is_admin: true });
    await settle();

    await userEvent.click(screen.getByTestId("alert-resolve-alert_1"));

    await waitFor(() =>
      expect(screen.getByTestId("resolve-error")).toHaveTextContent("标记恢复失败"));
  });
});