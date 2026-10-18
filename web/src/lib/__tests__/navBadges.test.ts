import { describe, expect, it } from "vitest";
import { navBadgeView } from "@/lib/navBadges";
import type { NavBadgeKey, NavBadges } from "@/api/types";

const ALL_KEYS: NavBadgeKey[] = [
  "credentials", "alerts", "tasks", "upstream", "users", "api_keys",
  "model_catalog_stale",
];

describe("navBadgeView", () => {
  it("未加载 / 请求失败（undefined）时一律不渲染", () => {
    for (const key of ALL_KEYS) expect(navBadgeView(key, undefined)).toBeNull();
  });

  it("缺失的键（无权看）不渲染：不能拿 0 冒充「一切正常」", () => {
    for (const key of ALL_KEYS) expect(navBadgeView(key, {})).toBeNull();
  });

  it("计数为 0 或负数不渲染", () => {
    const zero: NavBadges = {
      credentials: 0, alerts: 0, tasks: 0, upstream: 0, users: 0, api_keys: 0,
    };
    for (const key of ALL_KEYS) expect(navBadgeView(key, zero)).toBeNull();
    expect(navBadgeView("alerts", { alerts: -1 })).toBeNull();
  });

  it("脏 payload（非数字）静默不渲染：角标挂在全局侧栏，渲染错类型会打崩每个页面", () => {
    // 页面测试的 catch-all stub 曾把告警数组塞进 credentials 字段，触发过 React
    // 「Objects are not valid as a React child」并连带打崩整棵树
    const dirty = {
      credentials: [1, 2] as unknown as number,
      alerts: "3" as unknown as number,
      tasks: Number.NaN,
      upstream: null as unknown as number,
      users: true as unknown as number,
      api_keys: {} as unknown as number,
    };
    for (const key of ALL_KEYS) expect(navBadgeView(key, dirty)).toBeNull();
  });

  it("模型目录字段非布尔 true 时不渲染", () => {
    const dirty = { model_catalog_stale: 1 } as unknown as NavBadges;
    expect(navBadgeView("model_catalog_stale", dirty)).toBeNull();
  });

  it("凭证：warn 色调 + 说明「需重登或耗尽」", () => {
    expect(navBadgeView("credentials", { credentials: 3 })).toEqual({
      value: 3, tone: "warn", label: "3 个凭证需重新登录或额度耗尽",
    });
  });

  it("告警与任务：danger 色调", () => {
    expect(navBadgeView("alerts", { alerts: 2 })).toEqual({
      value: 2, tone: "danger", label: "2 条告警尚未恢复",
    });
    expect(navBadgeView("tasks", { tasks: 1 })).toEqual({
      value: 1, tone: "danger", label: "1 个后台任务上次执行失败",
    });
  });

  it("用户：muted 色调（待激活是流程，不是故障）", () => {
    expect(navBadgeView("users", { users: 4 })).toEqual({
      value: 4, tone: "muted", label: "4 个账号待激活",
    });
  });

  it("API Key：warn 色调 + 说明窗口", () => {
    expect(navBadgeView("api_keys", { api_keys: 5 })).toEqual({
      value: 5, tone: "warn", label: "5 个 API Key 已过期或 7 天内到期",
    });
  });

  it("上游：色调随整体档位——维护中是黄、部分/整体故障是红", () => {
    const at = (level: NavBadges["upstream_level"]) =>
      navBadgeView("upstream", { upstream: 1, upstream_level: level });
    expect(at("maintenance")).toEqual({
      value: 1, tone: "warn", label: "1 个上游状态源异常",
    });
    expect(at("degraded")?.tone).toBe("warn");
    expect(at("partial")?.tone).toBe("danger");
    expect(at("major")?.tone).toBe("danger");
  });

  it("上游：缺 level（理论不可达）退化成 muted，不误报成故障色", () => {
    expect(navBadgeView("upstream", { upstream: 2 })?.tone).toBe("muted");
  });

  it("模型目录：布尔标记渲染成文字而非计数", () => {
    expect(navBadgeView("model_catalog_stale", { model_catalog_stale: true })).toEqual({
      value: "陈旧", tone: "warn", label: "模型目录快照陈旧，需等待后台刷新",
    });
    expect(navBadgeView("model_catalog_stale", { model_catalog_stale: false })).toBeNull();
  });
});
