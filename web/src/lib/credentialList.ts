/**
 * 凭证池列表的筛选与分组（纯函数，页面与测试共用）。
 *
 * 池子一大，页面就从「看得完的列表」变成「翻不到的长表」：没有搜索只能靠肉眼扫，
 * 没有分组看不出渠道结构，每行还复读同样的渠道名与周期语义。这里把三件事抽成
 * 可单测的纯函数，`CredentialsPage` 只负责渲染：
 *
 * - 筛选：搜索词（昵称 / 渠道展示名 / 凭证 id）× 渠道 × 状态 × 标签；
 * - 分组：按渠道聚合，组顺序与全站渠道顺序一致（`providerRank`）；
 * - 计数：给筛选控件提供「各项各有多少条」，让零计数的筛选项不占位。
 *
 * 状态判定复用 `credentialState`（与表格里那个 Badge 同一口径），
 * 不在这里另起一套「可用」定义，否则筛选结果会和徽章自相矛盾。
 */

import { credentialState, STATE_LABEL } from "../api/display";
import type { CredentialState } from "../api/display";
import { providerLabel, providerRank } from "../api/providers";
import type { Credential } from "../api/types";

/** 筛选控件里代表「全部 / 不限」的哨兵值。 */
export const ANY = "all";

export interface CredentialFilters {
  /** 搜索词；去空白后大小写不敏感，空串 = 不限 */
  query: string;
  /** 渠道筛选：`ANY` 或渠道 id */
  provider: string;
  /** 状态筛选：`ANY` 或 `CredentialState` */
  state: string;
  /** 标签筛选：`ANY` 或某个标签（**精确匹配**，大小写不敏感） */
  tag: string;
}

/** 默认筛选：全部可见（页面首屏即池子全貌）。 */
export const DEFAULT_FILTERS: CredentialFilters = {
  query: "", provider: ANY, state: ANY, tag: ANY,
};

/**
 * 标签比对用的大小写不敏感键。
 *
 * 后端按大小写不敏感去重但保留首次写法，故同一标签在库里只会有一种写法；
 * 这里再归一一次是为了前端筛选下拉与行内徽章在任何写法下都能对上。
 */
function tagKey(tag: string): string {
  return tag.trim().toLowerCase();
}

/**
 * 单条凭证是否通过筛选。
 *
 * 搜索同时匹配昵称、渠道展示名与凭证 id：用户可能记得「主账号」「TRAE」或
 * 后端给的 id，任一种都该能找到。渠道名参与匹配是有意的——按渠道筛选之外，
 * 直接打字「codebuddy」也应当收敛。**标签不参与搜索**：它是结构化维度，
 * 已经单列一个下拉；混进搜索词会让「查一个恰好叫『生产』的昵称」变得不可能。
 */
export function matchesFilters(
  credential: Credential,
  filters: CredentialFilters,
  now: number = Date.now() / 1000,
): boolean {
  if (filters.provider !== ANY && credential.provider !== filters.provider) return false;
  if (filters.state !== ANY && credentialState(credential, now) !== filters.state) return false;
  if (filters.tag !== ANY && !(credential.tags ?? []).some(
    (tag) => tagKey(tag) === tagKey(filters.tag))) {
    return false;
  }
  const needle = filters.query.trim().toLowerCase();
  if (!needle) return true;
  const fields = [credential.nickname, credential.id, providerLabel(credential.provider)];
  return fields.some((field) => (field ?? "").toLowerCase().includes(needle));
}

export function filterCredentials(
  credentials: Credential[],
  filters: CredentialFilters,
  now: number = Date.now() / 1000,
): Credential[] {
  return credentials.filter((credential) => matchesFilters(credential, filters, now));
}

/**
 * CodeArts 在凭证页拆成两个**虚拟渠道**。
 *
 * 为什么不在后端真拆 provider：CodeArts 的 `refresh_token` 是一次性的（与
 * `client_id=codearts-agent` + DPoP 私钥三者绑定，刷后即作废），两条凭证行会
 * 各刷一次、第二条必报 `the refresh token has been used` 并烧掉票据。所以拆分
 * 只发生在展示层：同一批账号在两个组各出现一次，**福利**只读展示每日 token 池，
 * **内置**承载全部操作与积分账（两本账的区分轴是模型，不是渠道，见 PROPOSAL §2.4）。
 */
export const CODEARTS_INTERNAL_KEY = "codearts-internal";
export const CODEARTS_BENEFIT_KEY = "codearts-benefit";

/** 虚拟渠道在组内的展示次序：内置（承载全部操作）在前，福利（只读）在后。 */
const CHANNEL_ORDER: Record<string, number> = {
  [CODEARTS_INTERNAL_KEY]: 0,
  [CODEARTS_BENEFIT_KEY]: 1,
};

/**
 * 一组展示的是哪本账。
 *
 * `pool`＝每日 token 池（福利模型计费），`credits`＝积分账（内置模型计费）。
 * 两本账在上游是两个独立接口、互不通气，数值量级也差一个数量级，故**一个组
 * 只讲一本账**：组头额度合计、行内额度数字、变动流水都按它取。
 */
export type Ledger = "pool" | "credits";

/** 展示渠道的定义：一个真实渠道一组，CodeArts 拆成两组。 */
interface ChannelDef {
  key: string;
  provider: string;
  label: string;
  readOnly: boolean;
  ledger: Ledger;
}

/**
 * 一条凭证归属的展示渠道（可多个）。
 *
 * CodeArts 同时进「内置」与「福利」两组；其余渠道只有自己一组。`key` 与
 * `provider` 分开：筛选 / 行内语义仍按真实 `provider` 判断（`quotaSemantics`、
 * `hasQuotaProbe` 等），只有分组与 testid 用 `key`。
 */
function channelDefs(provider: string): ChannelDef[] {
  const label = providerLabel(provider);
  if (provider !== "codearts") {
    // 其余渠道只有一本账（主账＝每日 token 池口径的余额），积分账恒为 null。
    return [{ key: provider, provider, label, readOnly: false, ledger: "pool" }];
  }
  return [
    { key: CODEARTS_INTERNAL_KEY, provider, label: `${label} 内置`,
      readOnly: false, ledger: "credits" },
    { key: CODEARTS_BENEFIT_KEY, provider, label: `${label} 福利`,
      readOnly: true, ledger: "pool" },
  ];
}

/** 组间顺序：先按全站渠道顺序，再按虚拟渠道次序（内置前、福利后），最后按 key 定序。 */
function compareGroups(left: ProviderGroup, right: ProviderGroup): number {
  return providerRank(left.provider) - providerRank(right.provider)
    || (CHANNEL_ORDER[left.key] ?? 0) - (CHANNEL_ORDER[right.key] ?? 0)
    || left.key.localeCompare(right.key);
}

export interface ProviderGroup {
  /** 分组唯一键：真实渠道＝provider；CodeArts 虚拟渠道＝`codearts-internal` / `codearts-benefit` */
  key: string;
  /** 真实渠道 id（行内渠道语义仍按它判断） */
  provider: string;
  /** 组头展示名（虚拟渠道带「内置 / 福利」后缀） */
  label: string;
  /** 只读组（福利渠道）：只展示额度，不渲染勾选与任何操作 */
  readOnly: boolean;
  /** 本组讲哪一本账：pool＝每日 token 池 / credits＝积分账（额度合计与流水都按它取） */
  ledger: Ledger;
  /** 组内凭证，保持传入顺序（即后端排序），分组不改变排序语义 */
  items: Credential[];
  /** 组内各状态计数（只含出现过的态），供组头展示状态分布 */
  states: Partial<Record<CredentialState, number>>;
  /** 组内额度剩余合计；全部为 null（免费层 / 无该账）时为 null，展示成 — */
  remaining: number | null;
  /** 组内额度总量合计；与 remaining 同口径 */
  total: number | null;
  /** 组内 1.5 天（主窗口）内将过期的积分合计；全为 null 时为 null */
  expiring: number | null;
  /** 组内 7 天（次窗口）内将过期的积分合计；全为 null 时为 null */
  expiringSecondary: number | null;
}

/**
 * 按渠道分组，并在组上聚合计几个「一眼看池子」的数。
 *
 * 组顺序用全站统一的 `providerRank`（CodeBuddy / TRAE 在前），而不是「谁先出现」：
 * 30s 轮询会让列表顺序随排序键变化，若按出现顺序排组，组头会在页面上乱跳。
 * 未知渠道排在已知渠道之后，与模型列表口径一致。
 *
 * 聚合口径与行内展示同源：状态走 `credentialState`，额度直接对**本组那本账**的
 * 字段求和（内置组＝`quota_credits_*`，其余＝`quota_remaining/total`）。
 * **null 语义必须保住**：免费层（zen / kilo）的 `quota_remaining` 与内置组的
 * 未探测积分账恒为 null，若当 0 求和会把「无额度接口 / 未探测」显示成「额度 0」。
 * 故只对非 null 求和，一个都没有时返回 null（调用方显示 —）。
 */
export function groupByProvider(
  credentials: Credential[],
  now: number = Date.now() / 1000,
): ProviderGroup[] {
  const buckets = new Map<string, { channel: ChannelDef; items: Credential[] }>();
  for (const credential of credentials) {
    for (const channel of channelDefs(credential.provider)) {
      const bucket = buckets.get(channel.key);
      if (bucket) bucket.items.push(credential);
      else buckets.set(channel.key, { channel, items: [credential] });
    }
  }
  return [...buckets.values()]
    .map(({ channel, items }) => {
      const states: Partial<Record<CredentialState, number>> = {};
      for (const item of items) {
        const state = credentialState(item, now);
        states[state] = (states[state] ?? 0) + 1;
      }
      const credits = channel.ledger === "credits";
      return {
        key: channel.key,
        provider: channel.provider,
        label: channel.label,
        readOnly: channel.readOnly,
        ledger: channel.ledger,
        items,
        states,
        remaining: sumDefined(
          items, (item) => credits ? item.quota_credits_remaining : item.quota_remaining),
        total: sumDefined(
          items, (item) => credits ? item.quota_credits_total : item.quota_total),
        expiring: sumDefined(
          items,
          (item) => credits
            ? item.quota_credits_expiring
            : item.quota_expiring_credits,
        ),
        expiringSecondary: sumDefined(
          items,
          (item) => credits
            ? item.quota_credits_expiring_secondary
            : item.quota_expiring_credits_secondary,
        ),
      };
    })
    .sort(compareGroups);
}

/**
 * 组的排序口径。
 *
 * `quota` 是默认：按组内额度剩余合计从高到低——「哪个渠道还能打得多」是池子
 * 健康度的第一问题，数字大的排前面最省眼。`provider` 是全站渠道顺序
 * （CodeBuddy / TRAE 在前），顺序固定，适合想看稳定结构时用。
 */
export type GroupSort = "provider" | "quota";

export const DEFAULT_GROUP_SORT: GroupSort = "quota";

/**
 * 按口径重排分组。`provider` 直接原样返回（`groupByProvider` 已排好，不复制）。
 *
 * 额度排序的两个边界：
 * - **null 垫底**，不当 0：免费层（zen / kilo）没有额度接口，若按 0 参与比较
 *   会插在「真的只剩 0 积分」的渠道中间，语义反了；
 * - 额度相同（含都为 null）时回落到渠道顺序，避免 `sort` 稳定性之外的抖动。
 */
export function sortGroups(groups: ProviderGroup[], sort: GroupSort): ProviderGroup[] {
  if (sort === "provider") return groups;
  return [...groups].sort((left, right) => {
    if (left.remaining !== right.remaining) {
      if (left.remaining === null) return 1;
      if (right.remaining === null) return -1;
      return right.remaining - left.remaining;
    }
    return compareGroups(left, right);
  });
}

/** 对非 null 值求和；一个都没有时返回 null（区别于「合计为 0」）。 */
function sumDefined(
  items: Credential[],
  pick: (item: Credential) => number | null,
): number | null {
  let sum: number | null = null;
  for (const item of items) {
    const value = pick(item);
    if (value === null || value === undefined) continue;
    sum = (sum ?? 0) + value;
  }
  return sum;
}

export interface CountedOption {
  value: string;
  label: string;
  count: number;
}

/** 渠道筛选项：只列列表里真实出现过的渠道（带数量），按全站渠道顺序。 */
export function providerOptions(credentials: Credential[]): CountedOption[] {
  const counts = new Map<string, number>();
  for (const credential of credentials) {
    counts.set(credential.provider, (counts.get(credential.provider) ?? 0) + 1);
  }
  return [...counts.entries()]
    .sort(
      ([left], [right]) =>
        providerRank(left) - providerRank(right) || left.localeCompare(right),
    )
    .map(([provider, count]) => ({
      value: provider,
      label: providerLabel(provider),
      count,
    }));
}

/** 状态筛选的展示顺序：可用 → 冷却 → 硬禁用 → 软暂停 → 耗尽，与池概况的语义顺序一致。 */
export const STATE_ORDER: CredentialState[] = [
  "ready",
  "cooling",
  "disabled",
  "off",
  "exhausted",
];

/**
 * 标签筛选项：列表里出现过的标签（带计数），按**首次出现顺序**排。
 *
 * 不像渠道那样有全站固定顺序（标签是用户自由起的），按出现顺序排能让常用标签
 * 稳定地留在原位；同一条凭证上的多个标签合计只算一次。
 */
export function tagOptions(credentials: Credential[]): CountedOption[] {
  const counts = new Map<string, { label: string; count: number }>();
  for (const credential of credentials) {
    // 同一条凭证内的重复标签（老数据/大小写不一）只算一次——下拉里的数字是
    // 「携带该标签的凭证数」，不是标签出现次数。
    const seenHere = new Set<string>();
    for (const tag of credential.tags ?? []) {
      const key = tagKey(tag);
      if (seenHere.has(key)) continue;
      seenHere.add(key);
      const seen = counts.get(key);
      if (seen) seen.count += 1;
      else counts.set(key, { label: tag.trim(), count: 1 });
    }
  }
  return [...counts.entries()].map(([value, item]) => ({
    value,
    label: item.label,
    count: item.count,
  }));
}

/**
 * 状态筛选项：五态带计数。
 *
 * 零计数的态默认不占位——一排「冷却中 0 / 已禁用 0 / 已暂停 0」本身就是噪声，
 * 而池概况已经把四态讲清楚了。但**当前选中的那一项必须保留**：否则用户选完
 * 最后一条恰好被删/被恢复时，筛选控件会突然少一项、选中态无处可归。
 */
export function stateOptions(
  credentials: Credential[],
  now: number = Date.now() / 1000,
  selected: string = ANY,
): CountedOption[] {
  const counts = new Map<CredentialState, number>();
  for (const credential of credentials) {
    const state = credentialState(credential, now);
    counts.set(state, (counts.get(state) ?? 0) + 1);
  }
  return STATE_ORDER.filter(
    (state) => (counts.get(state) ?? 0) > 0 || state === selected,
  ).map((state) => ({
    value: state,
    label: STATE_LABEL[state],
    count: counts.get(state) ?? 0,
  }));
}
