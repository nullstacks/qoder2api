/**
 * 前端错误文案归一：只透出后端受控文案，其余一律回落固定串。
 *
 * 后端错误响应的 `message` 是受控的中文提示（`src/webapp/handlers.py` 保证），
 * 可以原样展示；而网络层异常（`TypeError: Failed to fetch`）、浏览器抛出的
 * 其它 `Error.message` 是英文实现细节，直接渲染给用户既看不懂也不可行动。
 */

import { ApiError } from "../api/client";

/** 取可展示的错误文案：ApiError 用后端 message，其余用 `fallback`。 */
export function errorText(caught: unknown, fallback: string): string {
  if (caught instanceof ApiError && caught.message) return caught.message;
  return fallback;
}
