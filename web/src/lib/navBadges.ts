/**
 * 侧栏角标口径：把 `GET /api/nav-badges` 的原始计数翻成「渲染什么」。
 *
 * 与 `credentialList.ts`（凭证状态判定）同一套路——取值、定色调、拼无障碍
 * 文案都收在这里，`Sidebar` 只负责渲染，测试也只测这个纯函数。
 *
 * 关键约定：**payload 里缺失的键表示「当前角色无权看」，不是 0**。所以每个
 * 分支都要先判 `undefined` 再判值——越权（缺键）与「无待办」（值为 0）都必须
 * 不渲染，两者对用户的含义完全不同。
 */

import { upstreamLevelTone } from "../api/display";
import type { NavBadgeKey, NavBadges } from "../api/types";
import type { Tone } from "../ui";

export interface NavBadgeView {
  /** 计数（数字）或文字形态（如「陈旧」） */
  value: number | string;
  tone: Tone;
  /** 无障碍全语义；可见数字本身是 `aria-hidden` 的 */
  label: string;
}

/**
 * 计数取值：非有限正数一律返回 null（不渲染角标）。
 *
 * 显式做类型/范围校验而不是只判 `> 0`：角标挂在全局侧栏上，一个脏 payload
 * （后端契约漂移 / 中间层改写）若被当成 React child 渲染，会把**每个页面**
 * 一起打崩。宁可静默不显示。
 */
function count(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : null;
}

/**
 * 一个导航项的角标视图；不需要显示时返回 `null`。
 *
 * `badges` 为 `undefined`（尚未加载 / 请求失败）时一律不渲染——宁可先不显示，
 * 也不要拿「0」冒充「一切正常」。
 */
export function navBadgeView(
  key: NavBadgeKey,
  badges: NavBadges | undefined,
): NavBadgeView | null {
  if (!badges) return null;
  switch (key) {
    case "credentials": {
      const n = count(badges.credentials);
      // 需重登 / 额度耗尽都是硬待办，用 warn 而非 danger：不阻塞服务，但要处理
      return n === null ? null
        : { value: n, tone: "warn", label: `${n} 个凭证需重新登录或额度耗尽` };
    }
    case "alerts": {
      const n = count(badges.alerts);
      return n === null ? null : { value: n, tone: "danger", label: `${n} 条告警尚未恢复` };
    }
    case "tasks": {
      const n = count(badges.tasks);
      return n === null ? null
        : { value: n, tone: "danger", label: `${n} 个后台任务上次执行失败` };
    }
    case "upstream": {
      const n = count(badges.upstream);
      if (n === null) return null;
      // 色调随整体档位：维护中（maintenance）是黄、部分/整体故障是红。
      // 缺 level（理论不可达）退化成 muted，而不是误报成故障色。
      const tone = badges.upstream_level ? upstreamLevelTone(badges.upstream_level) : "muted";
      return { value: n, tone, label: `${n} 个上游状态源异常` };
    }
    case "users": {
      const n = count(badges.users);
      return n === null ? null : { value: n, tone: "muted", label: `${n} 个账号待激活` };
    }
    case "api_keys": {
      const n = count(badges.api_keys);
      return n === null ? null
        : { value: n, tone: "warn", label: `${n} 个 API Key 已过期或 7 天内到期` };
    }
    case "model_catalog_stale":
      // 布尔标记，不是计数：陈旧时显示文字，让「有数据但旧」区别于「有几条」
      return badges.model_catalog_stale === true
        ? { value: "陈旧", tone: "warn", label: "模型目录快照陈旧，需等待后台刷新" }
        : null;
  }
}
