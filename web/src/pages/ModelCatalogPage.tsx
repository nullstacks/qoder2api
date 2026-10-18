import { useEffect, useState } from "react";
import {
  Ban,
  Boxes,
  CircleDollarSign,
  RefreshCw,
  Search,
  Trophy,
} from "lucide-react";
import { useSessionContext } from "../Layout";
import { useModelCatalogPage, useBlockModel } from "../api/modelExtrasHooks";
import type { ModelCatalogRow } from "../api/modelExtras";
import {
  formatAgo,
  formatCompact,
  formatNumber,
  formatTime,
} from "../api/display";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import { Pagination } from "../components/Pagination";
import { SortableHead } from "../components/SortableHead";
import { ColumnHint } from "../components/Tip";
import { ToastViewport, useToasts } from "../components/Toast";
import { useSort } from "../hooks/useSort";
import { DEFAULT_PAGE_SIZE } from "../lib/constants";
import {
  Badge,
  Button,
  Checkbox,
  Empty,
  Input,
  Label,
  Metric,
  Notice,
  Panel,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  Tabs,
} from "../ui";
import type { ModelCatalogEntry } from "../api/types";

/** 币种切换：价格原始单位是 USD / 百万 token，成本展示以人民币为主，两者都提供。 */
type Currency = "USD" | "CNY";

const CURRENCIES = [
  { value: "USD", label: "美元" },
  { value: "CNY", label: "人民币" },
];

/** 单价格式化：刊例价量级跨度大（0.02 ~ 数十），保留最多 4 位小数。 */
function formatUnitPrice(
  value: number | null,
  currency: Currency,
  rate: number,
): string {
  if (value === null) return "—";
  const converted = currency === "CNY" ? value * rate : value;
  const symbol = currency === "CNY" ? "¥" : "$";
  return `${symbol}${converted.toLocaleString("zh-CN", { maximumFractionDigits: 4 })}`;
}

/** 上游模态取值 → 中文（未知取值原样回显，上游加值前端不炸）。 */
const MODALITY_LABEL: Record<string, string> = {
  text: "文本",
  image: "图片",
  pdf: "PDF",
  audio: "音频",
  video: "视频",
  file: "文件",
};

function modalityText(items: string[]): string {
  if (items.length === 0) return "—";
  return items.map((item) => MODALITY_LABEL[item] ?? item).join(" · ");
}

/** 能力标签：只列具备的能力；一个都没有时返回空数组（页面显示 —）。 */
function capabilities(row: ModelCatalogEntry): string[] {
  const items: string[] = [];
  if (row.reasoning) items.push("推理");
  if (row.tool_call) items.push("工具调用");
  if (row.attachment) items.push("附件");
  if (row.structured_output) items.push("结构化输出");
  if (row.open_weights) items.push("开放权重");
  return items;
}

/** 上下文 / 输出上限：两者都缺显示 —，各占一行（表头「上下文 / 输出」对应上行 / 下行）。 */
function limitLine(value: number | null): string {
  return value === null ? "—" : formatCompact(value);
}

/**
 * 能力分一行：智 / 编 / 智体三项各占一行，缺失的那项显示 —。
 *
 * 与表头「能力分」对应（三行结构同「上下文 / 输出」）。整块没有分时只显示一个
 * 居中的 —：OpenRouter 收录数百条目，其中多数没有第三方成绩，逐个渲染
 * 三个 — 会让这一列显得很吵。
 */
function benchmarkLine(row: ModelCatalogEntry) {
  const b = row.benchmarks;
  if (!b) return <div className="text-muted-foreground">—</div>;
  const score = (value: number | undefined): string =>
    value === undefined ? "—" : value.toLocaleString("zh-CN");
  return (
    <div title="Artificial Analysis 指数（智能 / 编程 / 智能体），非本服务实测">
      <div>智 {score(b.intelligence_index)}</div>
      <div className="text-muted-foreground">编 {score(b.coding_index)}</div>
      <div className="text-muted-foreground">体 {score(b.agentic_index)}</div>
    </div>
  );
}

/**
 * 可用性单元格：窗口内失败次数 + 最后失败时刻 + 「加入黑名单」按钮。
 *
 * 没有失败记录时显示 —（「没数据」不等于「确认健康」，与后端 `suspected`
 * 字段同口径）。已命中当前黑名单的行只显示「已屏蔽」，按钮置灰——重复点击
 * 不会产生新的审计记录，只会让用户误以为操作没生效。
 */
function AvailabilityCell({
  row,
  windowDays,
  busy,
  onBlock,
}: {
  row: ModelCatalogRow;
  windowDays: number;
  busy: boolean;
  onBlock: () => void;
}) {
  const suspected = row.suspected;
  if (!suspected) return <span className="text-muted-foreground">—</span>;
  return (
    <div className="space-y-1" data-testid={`model-availability-${row.id}`}>
      <div className="flex flex-wrap items-center gap-1">
        <Badge tone="warn" dot>
          近 {windowDays} 天失败 {suspected.failures} 次
        </Badge>
        {suspected.blocked && <Badge tone="muted">已屏蔽</Badge>}
      </div>
      <div className="text-muted-foreground" title="最后一次失败的请求时刻">
        {suspected.provider} · {formatTime(suspected.last_failed_at)}
      </div>
      <Button
        variant="default"
        size="sm"
        data-testid={`model-block-${row.id}`}
        disabled={busy || suspected.blocked}
        title={suspected.blocked
          ? "该模型已命中当前黑名单"
          : `把 ${suspected.model} 追加进 MODEL_BLOCKLIST（走设置写接口，记审计）`}
        onClick={onBlock}
      >
        <Ban className="size-3" />
        加入黑名单
      </Button>
    </div>
  );
}

/**
 * 模型列表页（管理）：只读展示 OpenRouter 的模型目录。
 *
 * 这份目录既是统计页「成本（估算）」的价格来源（输入 / 输出 / 缓存读，USD /
 * 百万 token），也带出上游更详细的元数据（上下文、模态、能力、知识截止等）。
 * 目录缺失（首次部署尚未拉取）时显示空态而非报错。
 *
 * 门槛与「上游状态」一致：operator（admin / operator）可见，与路由守卫同口径。
 */
export function ModelCatalogPage() {
  const session = useSessionContext();
  const { sort, order, toggle } = useSort("id", "asc",
    { context: "desc", max_output: "desc", input: "desc", output: "desc",
      cache_read: "desc", cache_write: "desc", intelligence: "desc" });
  const [query, setQuery] = useState("");
  // 搜索词去抖：服务端分页后每次击键都会打接口，逐字符发请求既费流量又让结果闪烁。
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const [currency, setCurrency] = useState<Currency>("USD");
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
  // 只看「当前渠道获取到的模型」：默认关闭（OpenRouter 收录全量，用户按需收窄）。
  const [onlyAvailable, setOnlyAvailable] = useState(false);
  // 只看「疑似下线」：最近 N 天有模型级失败（口径见后端 /api/models/availability）。
  const [onlySuspected, setOnlySuspected] = useState(false);
  const { toasts, push, dismiss } = useToasts();
  const block = useBlockModel(session.username);

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedQuery(query), 250);
    return () => clearTimeout(timer);
  }, [query]);

  const { data, isLoading, isError, isFetching } = useModelCatalogPage(
    session.username,
    {
      sort, order,
      q: debouncedQuery || undefined,
      onlyAvailable,
      onlySuspected,
      limit: pageSize,
      offset: page * pageSize,
    });

  const rate = data?.usd_cny_rate ?? 1;
  const models = data?.models ?? [];
  const catalogCount = data?.count ?? 0;
  const total = data?.total ?? 0;
  const windowDays = data?.availability_days ?? 7;

  // 搜索 / 改每页条数 / 换排序 / 切过滤后回到第一页：否则会停在一个已不存在的页码上（空白）
  useEffect(() => {
    setPage(0);
  }, [debouncedQuery, pageSize, sort, order, onlyAvailable, onlySuspected]);

  if (isLoading) {
    return (
      <div data-testid="model-catalog-page">
        <PageSkeleton variant="table" rows={8} />
      </div>
    );
  }

  const addToBlocklist = async (row: ModelCatalogRow) => {
    const pattern = row.id;
    try {
      await block.mutateAsync(pattern);
      push({
        tone: "ok",
        testId: "model-blocklist-toast",
        content: `已把 ${pattern} 加入黑名单，下一次 /v1/models 起不再下发。`,
      });
    } catch (caught) {
      push({
        tone: "danger",
        testId: "model-blocklist-toast",
        content: `加入黑名单失败：${caught instanceof Error ? caught.message : String(caught)}`,
      });
    }
  };

  const priceUnit = `${currency === "CNY" ? "¥" : "$"} / 百万 token`;

  const toolbar = (
    <div className="flex flex-wrap items-center gap-2">
      <div className="relative">
        <Search className="pointer-events-none absolute top-1/2 left-2 size-3.5 -translate-y-1/2 text-muted-foreground" />
        <Input
          value={query}
          data-testid="model-search"
          placeholder="搜索 id / 名称"
          className="h-8 w-full pl-7 text-xs sm:w-56"
          onChange={(event) => setQuery(event.target.value)}
        />
      </div>
      <Tabs
        value={currency}
        options={CURRENCIES}
        onChange={(value) => setCurrency(value as Currency)}
        testId="model-currency-tabs"
      />
      <Label className="w-fit cursor-pointer gap-1.5 text-xs font-normal text-muted-foreground">
        <Checkbox
          checked={onlyAvailable}
          data-testid="model-available-toggle"
          onCheckedChange={(value) => setOnlyAvailable(value === true)}
        />
        只看当前渠道
      </Label>
      <Label className="w-fit cursor-pointer gap-1.5 text-xs font-normal text-muted-foreground">
        <Checkbox
          checked={onlySuspected}
          data-testid="model-suspected-toggle"
          onCheckedChange={(value) => setOnlySuspected(value === true)}
        />
        只看疑似下线
      </Label>
    </div>
  );

  return (
    <div className="space-y-6" data-testid="model-catalog-page">
      <ToastViewport toasts={toasts} onDismiss={dismiss} />
      <PageHeader
        eyebrow="管理"
        title="模型列表"
        description="OpenRouter 模型目录：统计页「成本（估算）」的刊例价来源（输入 / 输出 / 缓存读，USD / 百万 token）与上下文、模态、知识截止等元数据；「能力分」来自 Artificial Analysis。"
        icon={<Boxes className="size-5" />}
      />

      <section className="grid grid-cols-2 gap-3 md:grid-cols-3">
        <Metric
          label="目录更新"
          value={data?.saved_at ? formatAgo(data.saved_at) : "—"}
          hint={data?.saved_at
            ? `${formatTime(data.saved_at)} · 价格 / 明细 / 能力分同一次抓取`
            : "尚无目录快照，成本与能力分将显示 —"}
          icon={<RefreshCw className="size-4" />}
        />
        <Metric
          label="能力分来源"
          value="Artificial Analysis"
          hint="经 OpenRouter 公开接口分发，非本服务实测"
          icon={<Trophy className="size-4" />}
        />
        <Metric
          label="汇率"
          value={`1 USD = ${formatNumber(rate)} CNY`}
          hint="可在「任务与配置」热更，仅影响之后写入的请求"
          icon={<CircleDollarSign className="size-4" />}
        />
      </section>

      {isError ? (
        <div data-testid="model-catalog-error">
          <Notice tone="danger">
            模型列表加载失败，请刷新重试；若刚升级服务，请确认后端已重启。
          </Notice>
        </div>
      ) : catalogCount === 0 ? (
        <Panel title="模型列表">
          <Empty data-testid="no-model-catalog">
            尚无模型目录快照（后台拉取 OpenRouter 后自动出现），统计里的成本与能力分会显示 —
          </Empty>
        </Panel>
      ) : (
        <Panel title={`模型列表（${total} / ${catalogCount}）`}>
          {/* 搜索与币种切换独立成行：放进 Panel 标题行会在窄视口挤压标题成竖排 */}
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            {toolbar}
          </div>

          {total === 0 ? (
            <Empty data-testid="no-model-match">
              {query.trim()
                ? `没有匹配「${query}」的模型`
                : onlySuspected
                  ? `最近 ${windowDays} 天没有模型级失败（疑似下线为空）`
                  : "当前渠道没有可用模型（未接入或全部暂停）"}
            </Empty>
          ) : (
            <div className="mt-3">
              <Table
                data-testid="model-catalog-table"
                cards
                containerClassName="max-h-[70vh] overflow-auto"
              >
                {/* [&_th]:bg-muted：吸顶表头必须不透明，否则滚动时行内容会从
                    半透明的表头底色（基类 tr 的 bg-muted/40）里透出来。 */}
                <TableHeader className="sticky top-0 z-10 [&_th]:bg-muted">
                  <TableRow>
                    <SortableHead label="模型" columnKey="id" active={sort === "id"}
                                  direction={order} onToggle={toggle} testId="sort-id" />
                    <SortableHead label={`价格（${priceUnit}）`} columnKey="input"
                                  active={sort === "input"} direction={order}
                                  onToggle={toggle} align="right" testId="sort-input" />
                    <SortableHead label="能力分" columnKey="intelligence"
                                  active={sort === "intelligence"} direction={order}
                                  onToggle={toggle} testId="sort-intelligence"
                                  hint={<ColumnHint text="Artificial Analysis 三项指数（智 / 编 / 体），非本服务实测。点列头按「智」排序；无分数行排末尾。" />} />
                    <SortableHead label="上下文 / 输出" columnKey="context"
                                  active={sort === "context"} direction={order}
                                  onToggle={toggle} testId="sort-context" />
                    <TableHead>输入 → 输出模态</TableHead>
                    <TableHead>能力</TableHead>
                    <SortableHead label="知识截止" columnKey="knowledge"
                                  active={sort === "knowledge"} direction={order}
                                  onToggle={toggle} testId="sort-knowledge" />
                    <TableHead>可用性</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {models.map((row) => {
                    const caps = capabilities(row);
                    return (
                      <TableRow key={row.id} data-testid={`model-row-${row.id}`}>
                        <TableCell label="模型" className="max-w-[17rem]">
                          <div className="font-medium break-words">{row.name ?? row.id}</div>
                          <div className="font-mono text-xs break-all text-muted-foreground">
                            {row.id}
                          </div>
                        </TableCell>
                        <TableCell label="价格" className="text-right tabular-nums">
                          <div title="输入 单价 · 输出 单价">
                            <span className="text-muted-foreground">输入 </span>
                            {formatUnitPrice(row.input, currency, rate)}
                            <span className="text-muted-foreground"> · 输出 </span>
                            {formatUnitPrice(row.output, currency, rate)}
                          </div>
                          <div className="text-xs text-muted-foreground" title="缓存读 单价 · 缓存写 单价">
                            <span>缓存读 </span>
                            {formatUnitPrice(row.cache_read, currency, rate)}
                            <span> · 缓存写 </span>
                            {formatUnitPrice(row.cache_write, currency, rate)}
                          </div>
                        </TableCell>
                        <TableCell label="能力分" className="text-xs tabular-nums">
                          {benchmarkLine(row)}
                        </TableCell>
                        <TableCell label="上下文 / 输出" className="text-xs tabular-nums">
                          <div title="上下文窗口上限">{limitLine(row.context)}</div>
                          <div className="text-muted-foreground" title="单次输出上限">
                            {limitLine(row.max_output)}
                          </div>
                        </TableCell>
                        <TableCell label="模态" className="max-w-[9rem] text-xs whitespace-normal break-keep">
                          {modalityText(row.input_modalities)} →{" "}
                          {modalityText(row.output_modalities)}
                        </TableCell>
                        <TableCell label="能力" className="max-w-[12rem] text-xs whitespace-normal break-keep">
                          {caps.length === 0 ? "—" : caps.join(" · ")}
                        </TableCell>
                        <TableCell label="知识截止" className="text-xs">
                          {row.knowledge ?? "—"}
                        </TableCell>
                        <TableCell label="可用性" className="text-xs">
                          <AvailabilityCell
                            row={row}
                            windowDays={windowDays}
                            busy={block.isPending}
                            onBlock={() => void addToBlocklist(row)}
                          />
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
              <Pagination
                testId="model"
                page={page}
                pageSize={pageSize}
                total={total}
                busy={isFetching}
                onPageChange={setPage}
                onPageSizeChange={setPageSize}
              />
            </div>
          )}
        </Panel>
      )}

      <Notice tone="muted" className="items-start gap-1 px-3 py-2.5 text-xs">
        <p>
          模型 id 为 OpenRouter 的 id（含厂商前缀，如 z-ai/glm-5.3），与本服务对外模型名（归一键）不总是逐个相同；未匹配到定价的模型成本显示 —。价格为「按 token × 公开刊例价」的估算，不是上游真实扣费；匹配口径见「用量统计」页。
        </p>
        <p>
          本表是 OpenRouter 公开收录的范围（数百个模型）。价格列上行是「输入 / 输出」单价、下行是「缓存读 / 缓存写」单价（未声明缓存价时显示 —）。原始单位为 USD / 百万 token；切到人民币按当前汇率（1 USD = {formatNumber(rate)} CNY）折算，历史成本按写入时汇率定值、不重算。OpenRouter 的 `created` 是收录时间、不是发布日，故本表只列知识截止。
        </p>
      </Notice>
    </div>
  );
}
