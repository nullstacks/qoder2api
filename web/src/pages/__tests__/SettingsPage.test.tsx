import { screen, waitFor, within } from "@testing-library/react";

import { beforeEach, describe, expect, it, vi } from "vitest";
import { SettingsPage } from "../SettingsPage";
import { jsonResponse, mockFetch, renderPage, settle, userEvent } from "@/test-utils";
import { SETTINGS, SERVER_TIME, body, mockAll, runBody, switchTab, tasksBody, withSettings } from "@/test-utils/settings";

describe("SettingsPage", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("渲染全部配置项，并标出来源与 .env 默认值", async () => {
    mockAll();
    renderPage(<SettingsPage />);
    await settle();

    // 默认落在第一个任务 tab（额度探测）
    expect(screen.getByTestId("setting-quota_probe_minutes")).toBeInTheDocument();
    expect(screen.getByTestId("source-quota_probe_minutes")).toHaveTextContent("来自 .env");

    await switchTab("settings-tabs-gateway-pacer");
    expect(screen.getByTestId("setting-pacer_max_seconds")).toBeInTheDocument();
    expect(screen.getByTestId("source-pacer_max_seconds")).toHaveTextContent("DB 覆盖");
    expect(screen.getByTestId("default-pacer_max_seconds")).toHaveTextContent("20");

    await switchTab("settings-tabs-activity");
    expect(screen.getByTestId("setting-activity_report_enabled")).toBeInTheDocument();
  });

  it("每个任务一个 tab，配置按后端下发的归属进各自 tab，无归属的按网关卡组进 tab", async () => {
    mockAll();
    renderPage(<SettingsPage />);
    await settle();

    // 额度探测 tab：只有它自己的配置
    const probe = screen.getByTestId("task-quota_probe");
    expect(within(probe).getByTestId("setting-quota_probe_minutes")).toBeInTheDocument();
    expect(within(probe).queryByTestId("setting-pacer_max_seconds")).not.toBeInTheDocument();

    await switchTab("settings-tabs-activity");
    expect(
      within(screen.getByTestId("task-activity")).getByTestId("setting-activity_report_enabled"),
    ).toBeInTheDocument();

    // 节流项不属于任何任务：只在「后台任务节流」这个网关 tab 出现
    await switchTab("settings-tabs-gateway-pacer");
    expect(screen.getByTestId("setting-pacer_max_seconds")).toBeInTheDocument();
    expect(screen.queryByTestId("task-quota_probe")).not.toBeInTheDocument();
  });

  it("网关卡组按后端下发拆成多个一级 tab，空分组不出 tab", async () => {
    mockAll();
    renderPage(<SettingsPage />);
    await settle();

    // 有配置项的 pacer 分组有 tab；routing / affinity / channels 当前无配置项，不出现
    expect(screen.getByRole("tab", { name: "后台任务节流" })).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "模型路由" })).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "渠道节流" })).not.toBeInTheDocument();
  });

  it("tab 文案去掉任务名的括号补充、过长的用简称；卡片标题保留完整名", async () => {
    const spy = vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.includes("/api/tasks")) {
        return jsonResponse({
          tasks: [
            {
              key: "model_catalog",
              name: "渠道模型列表刷新",
              description: "兜底重拉各渠道模型表。",
              interval_seconds: 1800,
              enabled: true,
              running: false,
              runs: 0,
              last_started_at: null,
              last_finished_at: null,
              last_ok: null,
              last_report: null,
              last_error: null,
            },
            {
              key: "openrouter_catalog",
              name: "模型目录刷新（OpenRouter）",
              description: "拉取 OpenRouter 模型目录。",
              interval_seconds: 86400,
              enabled: true,
              running: false,
              runs: 0,
              last_started_at: null,
              last_finished_at: null,
              last_ok: null,
              last_report: null,
              last_error: null,
            },
          ],
          server_time: SERVER_TIME,
        });
      }
      return jsonResponse(body());
    });
    vi.stubGlobal("fetch", spy);

    renderPage(<SettingsPage />);
    await settle();

    // 括号补充被去掉、过长任务名用简称
    expect(screen.getByRole("tab", { name: "模型目录刷新" })).toBeInTheDocument();
    expect(
      screen.queryByRole("tab", { name: "模型目录刷新（OpenRouter）" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "渠道模型刷新" })).toBeInTheDocument();
    // 卡片标题仍用完整名（默认落在第一个任务 model_catalog）
    expect(screen.getByTestId("task-model_catalog")).toHaveTextContent("渠道模型列表刷新");

    await switchTab("settings-tabs-openrouter_catalog");
    expect(screen.getByTestId("task-openrouter_catalog")).toHaveTextContent(
      "模型目录刷新（OpenRouter）",
    );
  });

  it("任务卡片展示周期、上次执行相对时间与最近结果", async () => {
    mockAll();
    renderPage(<SettingsPage />);
    await settle();

    // 默认落在第一个任务 tab（额度探测）
    expect(screen.getByTestId("task-interval-quota_probe")).toHaveTextContent("1.0 小时");
    expect(screen.getByTestId("task-last-quota_probe")).toHaveTextContent("2 分钟前");
    expect(screen.getByTestId("task-report-quota_probe")).toHaveTextContent(
      "尝试 2 · 成功 2 · 失败 0 · 跳过 0",
    );
  });

  it("失败任务标出「上次失败」并展示错误原文", async () => {
    mockAll();
    renderPage(<SettingsPage />);
    await settle();
    await switchTab("settings-tabs-checkin");

    const checkin = screen.getByTestId("task-checkin");
    expect(checkin).toHaveTextContent("上次失败");
    expect(screen.getByTestId("task-error-checkin")).toHaveTextContent("上游 500");
  });

  it("关闭的任务显示「已关闭」，未执行过的显示尚未执行", async () => {
    mockAll();
    renderPage(<SettingsPage />);
    await settle();
    await switchTab("settings-tabs-activity");

    const activity = screen.getByTestId("task-activity");
    expect(activity).toHaveTextContent("已关闭");
    expect(screen.getByTestId("task-last-activity")).toHaveTextContent("本进程内尚未执行");
  });

  it("无热更配置的任务给出提示而不是空白卡片", async () => {
    mockAll();
    renderPage(<SettingsPage />);
    await settle();
    await switchTab("settings-tabs-checkin");

    expect(screen.getByTestId("task-noconfig-checkin")).toHaveTextContent("无可热更配置");
  });

  it("拿不到任务运行态时列出提示，配置项仍留在页面上", async () => {
    const spy = vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.includes("/api/tasks")) return jsonResponse({ tasks: [], server_time: SERVER_TIME });
      return jsonResponse(body());
    });
    vi.stubGlobal("fetch", spy);

    renderPage(<SettingsPage />);
    await settle();

    expect(screen.getByTestId("no-tasks")).toBeInTheDocument();
    // 有内容的网关 tab 仍在
    expect(screen.getByTestId("setting-pacer_max_seconds")).toBeInTheDocument();
    // 任务归属项因任务清单不可用落到「其他」，不会被吞掉
    await switchTab("settings-tabs-settings-other");
    expect(screen.getByTestId("setting-quota_probe_minutes")).toBeInTheDocument();
  });

  it("归属对不上的配置项落进「其他」tab，不会被吞掉", async () => {
    // task 指向一个未装配的任务、且无有效分组 → 兜底进「其他」
    withSettings([
      ...SETTINGS,
      {
        key: "orphan_setting",
        env_name: "ORPHAN_SETTING",
        label: "孤儿配置",
        description: "归属既非任务也非网关卡组。",
        kind: "int",
        value: 1,
        default: 1,
        overridden: false,
        task: "ghost_task",
        group: null,
      },
    ]);
    renderPage(<SettingsPage />);
    await settle();

    await switchTab("settings-tabs-settings-other");
    expect(screen.getByTestId("setting-orphan_setting")).toBeInTheDocument();
    expect(screen.queryByTestId("task-quota_probe")).not.toBeInTheDocument();
  });

  it("布尔项用下拉选择，数字项用输入框", async () => {
    mockAll();
    renderPage(<SettingsPage />);
    await settle();
    expect(screen.getByTestId("input-quota_probe_minutes").tagName).toBe("INPUT");

    await switchTab("settings-tabs-activity");
    expect(screen.getByTestId("input-activity_report_enabled").tagName).toBe("SELECT");
  });

  it("空列表给出提示", async () => {
    mockFetch({
      "/api/settings": () => jsonResponse({ settings: [], overridden: 0 }),
      "/api/tasks": () => jsonResponse(tasksBody()),
    });
    renderPage(<SettingsPage />);
    await settle();
    expect(screen.getByTestId("no-settings")).toBeInTheDocument();
  });

  it("点「立即运行」触发后端并展示本轮结果", async () => {
    const spy = mockAll();
    renderPage(<SettingsPage />);
    await settle();

    await userEvent.click(screen.getByTestId("task-run-quota_probe"));

    await waitFor(() => {
      expect(screen.getByTestId("task-run-result-quota_probe")).toHaveTextContent("已运行一轮");
    });
    // 打到了正确的端点，且带了写请求所需的 CSRF 头
    const call = spy.mock.calls.find(([, init]) => init?.method === "POST");
    expect(String(call?.[0])).toContain("/api/tasks/quota_probe/run");
    expect((call?.[1]?.headers as Record<string, string>)["X-Requested-With"]).toBe("XMLHttpRequest");
  });

  it("已在跑的任务：按钮禁用并显示运行中，不重复触发", async () => {
    mockAll(undefined, () => jsonResponse(runBody("quota_probe", { busy: true, ran: false })));
    renderPage(<SettingsPage />);
    await settle();

    // 默认数据里 running=false，按钮可点；点后拿到 busy 返回
    await userEvent.click(screen.getByTestId("task-run-quota_probe"));
    await waitFor(() => {
      expect(screen.getByTestId("task-run-result-quota_probe")).toHaveTextContent(
        "正在运行，本次未重复触发",
      );
    });
  });

  it("被开关跳过的任务：按钮禁用且不谎报已运行", async () => {
    mockAll();
    renderPage(<SettingsPage />);
    await settle();
    await switchTab("settings-tabs-activity");

    // activity 在样例数据里 enabled=false
    expect(screen.getByTestId("task-run-activity")).toBeDisabled();
    expect(screen.getByTestId("task-run-activity")).toHaveAttribute(
      "title",
      "任务已关闭，先打开对应开关",
    );
  });

  it("后端回 ran=false（no-op）时如实说「本轮未执行」", async () => {
    mockAll(undefined, () => jsonResponse(runBody("quota_probe", { ran: false, busy: false })));
    renderPage(<SettingsPage />);
    await settle();

    await userEvent.click(screen.getByTestId("task-run-quota_probe"));

    await waitFor(() => {
      expect(screen.getByTestId("task-run-result-quota_probe")).toHaveTextContent("本轮未执行");
    });
  });

  it("本轮失败时展示错误原文", async () => {
    mockAll(undefined, () => jsonResponse(runBody("quota_probe", {
      last_ok: false,
      last_error: "上游 500",
    })));
    renderPage(<SettingsPage />);
    await settle();

    await userEvent.click(screen.getByTestId("task-run-quota_probe"));

    await waitFor(() => {
      expect(screen.getByTestId("task-run-result-quota_probe")).toHaveTextContent("上游 500");
    });
  });

  it("触发请求本身失败时给出兜底文案", async () => {
    mockAll(undefined, () => jsonResponse({ error: { code: "invalid", message: "任务不存在" } }, 400));
    renderPage(<SettingsPage />);
    await settle();

    await userEvent.click(screen.getByTestId("task-run-quota_probe"));

    await waitFor(() => {
      expect(screen.getByTestId("task-run-error")).toHaveTextContent("任务不存在");
    });
  });

  it("管理员可见导航入口，非管理员不可见", async () => {
    mockAll();
    const view = renderPage(<SettingsPage />);
    await settle();
    // 管理类入口直接展示在侧边栏「管理」分组下（不再收进下拉）
    const nav = screen.getByRole("navigation", { name: "主导航" });
    expect(within(nav).getByText("任务与配置")).toBeInTheDocument();

    view.unmount();
    renderPage(<SettingsPage />, { username: "guest", is_admin: false });
    // 非管理员不渲染管理分组
    await waitFor(() => {
      const nav2 = screen.getByRole("navigation", { name: "主导航" });
      expect(within(nav2).queryByText("任务与配置")).not.toBeInTheDocument();
    });
  });
});
