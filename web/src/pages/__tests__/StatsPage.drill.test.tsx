import { screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { StatsPage } from "../StatsPage";
import { mockFetch, renderPage, settle, userEvent } from "@/test-utils";
import {
  ADMIN,
  CREDENTIALS,
  EVENTS_EMPTY,
  EVENTS_PAGE1,
  MODEL_TIMELINE,
  MODELS,
  OVERVIEW,
  PROVIDERS,
  READER,
  USERS,
} from "@/test-utils/stats";

/** 导出走 fetch → Blob → 触发下载：jsdom 没实现 createObjectURL，补桩。 */
function stubObjectUrl() {
  const create = vi.fn(() => "blob:mock");
  const revoke = vi.fn();
  vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: create, revokeObjectURL: revoke }));
  return { create, revoke };
}

function statsRoutes(events: unknown = EVENTS_EMPTY) {
  return {
    "/api/stats/overview": OVERVIEW,
    "/api/stats/by-provider": PROVIDERS,
    "/api/stats/by-model": MODELS,
    "/api/stats/by-user": USERS,
    "/api/stats/by-credential": CREDENTIALS,
    "/api/stats/model-timeline": MODEL_TIMELINE,
    "/api/stats/events": events,
  };
}

describe("StatsPage 钻取与导出", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("点分组行钻取：明细请求带上过滤参数并显示筛选标签，可清除", async () => {
    const fetchSpy = mockFetch(statsRoutes());
    renderPage(<StatsPage />, ADMIN);
    await settle();

    // 默认按渠道：点 TRAE 行 → 明细按 provider=trae 重新取数
    await userEvent.click(screen.getByTestId("group-row-trae"));
    await waitFor(() => expect(fetchSpy.mock.calls.some(
      ([url]) => String(url).includes("/api/stats/events") && String(url).includes("provider=trae"),
    )).toBe(true));
    const chips = screen.getByTestId("drill-filters");
    expect(chips).toHaveTextContent("渠道：TRAE");

    // 切模型维度再点行 → 追加 model 过滤（两个条件叠加）
    await userEvent.click(screen.getByTestId("group-tabs-model"));
    await waitFor(() => expect(screen.getByTestId("group-row-glm-5.2")).toBeInTheDocument());
    await userEvent.click(screen.getByTestId("group-row-glm-5.2"));
    await waitFor(() => expect(screen.getByTestId("drill-filters"))
      .toHaveTextContent("模型：glm-5.2"));
    expect(screen.getByTestId("drill-filters")).toHaveTextContent("渠道：TRAE");
    await waitFor(() => expect(fetchSpy.mock.calls.some(([url]) => {
      const text = String(url);
      return text.includes("provider=trae") && text.includes("model=glm-5.2");
    })).toBe(true));

    // 清除 → 回到无过滤
    await userEvent.click(screen.getByTestId("drill-clear"));
    await waitFor(() => expect(screen.queryByTestId("drill-filters")).not.toBeInTheDocument());
  });

  it("凭证维度钻取按 credential_id 过滤；用户维度走作用域参数", async () => {
    const fetchSpy = mockFetch(statsRoutes());
    renderPage(<StatsPage />, ADMIN);
    await settle();

    await userEvent.click(screen.getByTestId("group-tabs-credential"));
    await waitFor(() => expect(screen.getByTestId("group-row-cred_abc123")).toBeInTheDocument());
    await userEvent.click(screen.getByTestId("group-row-cred_abc123"));
    await waitFor(() => expect(fetchSpy.mock.calls.some(
      ([url]) => String(url).includes("credential_id=cred_abc123"),
    )).toBe(true));
    // 标签只显示 ID 前 12 位（完整 ID 太长）
    expect(screen.getByTestId("drill-filters")).toHaveTextContent("凭证：cred_abc123");

    await userEvent.click(screen.getByTestId("drill-clear"));
    await userEvent.click(screen.getByTestId("group-tabs-user"));
    await waitFor(() => expect(screen.getByTestId("group-row-alice")).toBeInTheDocument());
    await userEvent.click(screen.getByTestId("group-row-alice"));
    await waitFor(() => expect(fetchSpy.mock.calls.some(([url]) =>
      String(url).includes("/api/stats/events") && String(url).includes("username=alice"),
    )).toBe(true));
    expect(screen.getByTestId("drill-filters")).toHaveTextContent("用户：alice");
  });

  it("导出 CSV：带当前过滤请求文件、触发下载并提示行数", async () => {
    const { create, revoke } = stubObjectUrl();
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => {});
    const fetchSpy = mockFetch({
      ...statsRoutes(),
      "/api/stats/events": (url: string) => {
        if (url.includes("format=csv")) {
          return new Response("时间,用户\r\n2023-11-14T22:13:20+00:00,alice\r\n", {
            status: 200,
            headers: {
              "Content-Type": "text/csv; charset=utf-8",
              "X-Export-Row-Count": "1",
              "X-Export-Truncated": "0",
            },
          });
        }
        return new Response(JSON.stringify(EVENTS_EMPTY),
                            { headers: { "Content-Type": "application/json" } });
      },
    });
    renderPage(<StatsPage />, ADMIN);
    await settle();

    await userEvent.click(screen.getByTestId("group-row-trae"));
    await waitFor(() => expect(screen.getByTestId("drill-filters")).toBeInTheDocument());

    await userEvent.click(screen.getByTestId("events-export"));
    await waitFor(() => expect(screen.getByTestId("export-notice"))
      .toHaveTextContent("已导出 1 条明细"));
    expect(create).toHaveBeenCalled();
    expect(revoke).toHaveBeenCalledWith("blob:mock");
    expect(click).toHaveBeenCalled();
    const csvCall = fetchSpy.mock.calls.map(([url]) => String(url))
      .find((url) => url.includes("format=csv"));
    expect(csvCall).toContain("provider=trae");       // 导出沿用当前钻取过滤
    click.mockRestore();
  });

  it("导出被截断时提示收窄条件", async () => {
    stubObjectUrl();
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    mockFetch({
      ...statsRoutes(),
      "/api/stats/events": (url: string) => url.includes("format=csv")
        ? new Response("时间\r\n", {
            status: 200,
            headers: { "X-Export-Row-Count": "10000", "X-Export-Truncated": "1" },
          })
        : new Response(JSON.stringify(EVENTS_EMPTY),
                       { headers: { "Content-Type": "application/json" } }),
    });
    renderPage(<StatsPage />, ADMIN);
    await settle();

    await userEvent.click(screen.getByTestId("events-export"));
    await waitFor(() => expect(screen.getByTestId("export-notice"))
      .toHaveTextContent("达到上限被截断"));
  });

  it("导出失败展示错误文案（403 只读用户不可导出）", async () => {
    stubObjectUrl();
    mockFetch({
      ...statsRoutes(),
      "/api/stats/events": (url: string) => url.includes("format=csv")
        ? new Response(JSON.stringify({ error: { code: "forbidden", message: "权限不足" } }),
                       { status: 403, headers: { "Content-Type": "application/json" } })
        : new Response(JSON.stringify(EVENTS_EMPTY),
                       { headers: { "Content-Type": "application/json" } }),
    });
    renderPage(<StatsPage />, ADMIN);
    await settle();

    await userEvent.click(screen.getByTestId("events-export"));
    await waitFor(() => expect(screen.getByTestId("export-notice"))
      .toHaveTextContent("导出失败：权限不足"));
  });

  it("只读用户看不到导出按钮，也不展示用户/凭证钻取 tab", async () => {
    mockFetch(statsRoutes(EVENTS_PAGE1));
    renderPage(<StatsPage />, READER);
    await settle();

    expect(screen.queryByTestId("events-export")).not.toBeInTheDocument();
    expect(screen.queryByTestId("group-tabs-user")).not.toBeInTheDocument();
    expect(screen.queryByTestId("group-tabs-credential")).not.toBeInTheDocument();
    // 渠道维度仍可钻取
    expect(screen.getByTestId("group-row-trae")).toBeInTheDocument();
    expect(within(screen.getByTestId("events-panel")).queryByText("用户"))
      .not.toBeInTheDocument();
  });
});
