/**
 * 自定义时间范围的解析（`datetime-local` → epoch 秒）。
 *
 * 抽成独立模块而不是塞进 `StatsPage`：它有两个非显然的约束需要单测覆盖，
 * 而 `StatsPage` 自带一堆取数副作用。
 *
 * - **本地时区**：`datetime-local` 的 value 是 `YYYY-MM-DDTHH:mm`，无时区信息。
 *   `new Date(value)` 按**浏览器本地时区**解析（与 `parseExpiresAt` 同口径），
 *   这正是用户填「我这边 10 点」时的预期，不能当 UTC。
 * - **半开区间**：`from` 是闭下界、`to` 是开上界（后端 `[since, until)`）。
 *   用户填的「结束时间」是瞬时右端点，前一次「到 12:00」与后一次「从 12:00」
 *   不重叠——否则同一分钟的数据会被两个窗口都算进去。
 */

export interface CustomRange {
  /** 起始 epoch 秒（闭下界）；空 = 不限 */
  since?: number;
  /** 结束 epoch 秒（开上界）；空 = 不限 */
  until?: number;
}

/** `datetime-local` 值 → epoch 秒；空串 → null；非法 → 抛错。 */
export function parseMoment(value: string): number | null {
  const raw = value.trim();
  if (!raw) return null;
  const ms = new Date(raw).getTime();
  if (!Number.isFinite(ms)) throw new Error("invalid moment");
  return Math.floor(ms / 1000);
}

/**
 * Date → `datetime-local` 的 value（本地时区，精确到分钟）。
 *
 * 不能用 `toISOString().slice(0,16)`：那是 UTC，切到自定义范围时预填的时间
 * 会与用户本地时钟差一个时区（东八区看就是差 8 小时）。这里手工按本地
 * 年月日时分拼字符串，绕开 `toISOString` 的 UTC 归一。
 */
export function toLocalInput(date: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}`
  );
}

/**
 * 两个 `datetime-local` 输入 → 查询参数。
 *
 * 起点晚于终点时**整体作废**（回 undefined）而不是只丢掉一头：
 * 一个「从 8 点看到 6 点」的窗口在任何一边单独生效都会给出与用户直觉相反的
 * 数据；作废后前端显示提示，用户改对了再查。任一端为空即单边不限。
 */
export function buildCustomRange(from: string, to: string): CustomRange | null {
  let since: number | null;
  let until: number | null;
  try {
    since = parseMoment(from);
    until = parseMoment(to);
  } catch {
    return null;
  }
  if (since !== null && until !== null && since >= until) return null;
  if (since === null && until === null) return null;
  return { since: since ?? undefined, until: until ?? undefined };
}
