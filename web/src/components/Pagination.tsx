import { ChevronLeft, ChevronRight } from "lucide-react";
import { Button, Select } from "../ui";
import { DEFAULT_PAGE_SIZE, PAGE_SIZE_OPTIONS } from "../lib/constants";

/**
 * 全站统一的分页控件。
 *
 * 所有列表页共用同一个「每页 N 条 + 第 X / Y 页 + 上一页 / 下一页」布局，
 * 避免同一产品里出现两套分页样式与三套页码文案（见 `PAGE_SIZE_OPTIONS`）。
 *
 * 两种数据源都能用：
 *
 * - **后端分页**：传 `total`（后端返回的总条数），组件据此算页数；
 * - **客户端分页**：传 `pageCount`（前端切片的页数）；
 * - **游标分页**（如请求明细）：两者都不传，只给 `hasNext`，页码退化成
 *   「第 X 页」（不知道总数，不猜）。
 *
 * 受控组件：翻页 / 改每页条数都由调用方持有状态（后端分页要据此重新取数）。
 * 始终渲染（含单页列表）：翻页按钮按 `page` / 总页数自动禁用，每页选择器在单页
 * 时仍可用——空表由调用方的空态分支兜住，不在这里隐藏。
 */
export function Pagination({
  testId = "pagination",
  page,
  pageSize,
  pageCount,
  total,
  hasNext,
  onPageChange,
  onPageSizeChange,
  pageSizeOptions = PAGE_SIZE_OPTIONS,
  busy = false,
  className,
}: {
  /** testid 前缀：产出 `${testId}-page-info` / `-page-size` / `-prev` / `-next`。 */
  testId?: string;
  /** 当前页（0 起）。 */
  page: number;
  pageSize: number;
  /** 总页数（客户端分页直接给）。与 `total` 二选一。 */
  pageCount?: number | null;
  /** 总条数（后端分页给）。与 `pageCount` 二选一。 */
  total?: number | null;
  /** 是否还有下一页（游标分页用；给了 `pageCount`/`total` 时可省）。 */
  hasNext?: boolean;
  onPageChange: (page: number) => void;
  onPageSizeChange?: (size: number) => void;
  pageSizeOptions?: readonly number[];
  /** 取数中：禁用翻页，避免连点翻页发出竞态请求。 */
  busy?: boolean;
  className?: string;
}) {
  // 页数优先用显式 pageCount；否则由 total 现算；都没有 = 未知（游标模式）
  const resolvedCount =
    pageCount ?? (total !== null && total !== undefined
      ? Math.max(1, Math.ceil(total / pageSize))
      : null);
  const next = hasNext ?? (resolvedCount !== null ? page < resolvedCount - 1 : false);

  const info = resolvedCount !== null
    ? `第 ${page + 1} / ${resolvedCount} 页`
    : `第 ${page + 1} 页`;

  return (
    <div
      data-testid={testId}
      className={className ?? "mt-3 flex flex-wrap items-center justify-between gap-3"}
    >
      <div className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
        <span data-testid={`${testId}-page-info`}>{info}</span>
        {onPageSizeChange && (
          <label className="flex items-center gap-1.5">
            每页
            <Select
              value={String(pageSize)}
              data-testid={`${testId}-page-size`}
              className="h-7 w-20 text-xs"
              onChange={(event) => onPageSizeChange(Number(event.target.value))}
            >
              {pageSizeOptions.map((size) => (
                <option key={size} value={size}>{size} 条</option>
              ))}
            </Select>
          </label>
        )}
      </div>
      <div className="flex gap-2">
        <Button
          size="sm"
          variant="ghost"
          data-testid={`${testId}-prev`}
          disabled={page === 0 || busy}
          onClick={() => onPageChange(Math.max(0, page - 1))}
        >
          <ChevronLeft className="size-3.5" />
          上一页
        </Button>
        <Button
          size="sm"
          variant="ghost"
          data-testid={`${testId}-next`}
          disabled={!next || busy}
          onClick={() => onPageChange(page + 1)}
        >
          下一页
          <ChevronRight className="size-3.5" />
        </Button>
      </div>
    </div>
  );
}

/** 分页组件的默认每页条数（与 `DEFAULT_PAGE_SIZE` 同源，导出便于调用点复用）。 */
export { DEFAULT_PAGE_SIZE };
