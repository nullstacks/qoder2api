import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { formatAxisValue, formatChartValue, METRIC_AXIS_UNIT } from "../api/display";
import { providerChartColor, providerLabel } from "../api/providers";
import type { TimelinePoint } from "../api/types";
import { ProviderIcon } from "./ProviderIcon";

/** Y 轴宽度：容下紧凑刻度（如「1500万」「850 ms」）。 */
const Y_AXIS_WIDTH = 56;

/** 时间序列点的保留键（不是渠道名），其余键都当作渠道序列。 */
const NON_PROVIDER_KEYS = new Set(["hour", "time"]);

/**
 * 从数据点里收集出现过的渠道名（稳定排序）。
 *
 * 渠道集合动态取自后端返回的键：新增渠道时后端数据一变，图例与曲线自动
 * 跟上，不需要在前端再维护一份渠道清单。
 */
export function chartProviders(points: TimelinePoint[]): string[] {
  const seen = new Set<string>();
  for (const point of points) {
    for (const key of Object.keys(point)) {
      if (!NON_PROVIDER_KEYS.has(key)) seen.add(key);
    }
  }
  return [...seen].sort();
}

/** 请求量时间序列曲线（recharts）。点少时仍画满可用宽度，不加插值。
 *
 *  渠道图例放在图表**下方**（与「按模型趋势」同口径）：渠道数会随上游增减
 *  （国际站接入后一条模型最多挂 5 个渠道），图例放面板右上角会把标题挤掉，
 *  放图下方则按需折行、不占用标题行。
 */
export function UsageChart({ points, metric }: { points: TimelinePoint[]; metric?: string }) {
  const data = points.map((point) => ({
    ...point,
    time: new Date(point.hour * 1000).toLocaleString("zh-CN", {
      hour12: false,
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
    }),
  }));
  // 单位标签只对不自带单位的指标（计数 / token）显示；耗时/首字的刻度已含 ms/s
  const axisUnit = METRIC_AXIS_UNIT[metric ?? "requests"];
  const providers = chartProviders(points);

  return (
    <div data-testid="usage-chart">
      <div className="h-64 w-full">
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={data} margin={{ top: 32, right: 12, left: 0, bottom: 0 }}>
            <defs>
              {providers.map((provider) => (
                <linearGradient key={provider} id={`g-${provider}`} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={providerChartColor(provider)} stopOpacity={0.25} />
                  <stop offset="100%" stopColor={providerChartColor(provider)} stopOpacity={0} />
                </linearGradient>
              ))}
            </defs>
            <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" vertical={false} />
            <XAxis
              dataKey="time"
              tick={{ fontSize: 11, fill: "var(--muted-foreground)" }}
              tickLine={false}
              axisLine={{ stroke: "var(--border)" }}
              interval="preserveStartEnd"
              minTickGap={40}
            />
            <YAxis
              allowDecimals={false}
              width={Y_AXIS_WIDTH}
              tick={{ fontSize: 11, fill: "var(--muted-foreground)" }}
              tickLine={false}
              axisLine={false}
              tickFormatter={(value) => formatAxisValue(Number(value), metric ?? "requests")}
              label={
                axisUnit
                  ? {
                      value: axisUnit,
                      position: "top",
                      angle: 0,
                      offset: 13,
                      style: { fontSize: 11, fill: "var(--muted-foreground)", textAnchor: "middle" },
                    }
                  : undefined
              }
            />
            <Tooltip
              contentStyle={{
                background: "var(--popover)",
                border: "1px solid var(--border)",
                borderRadius: 8,
                fontSize: 12,
                color: "var(--popover-foreground)",
              }}
              labelStyle={{ fontWeight: 600 }}
              formatter={(value, name) => [formatChartValue(Number(value), metric ?? "requests"), name]}
            />
            {providers.map((provider) => (
              <Area
                key={provider}
                type="monotone"
                dataKey={provider}
                name={providerLabel(provider)}
                stroke={providerChartColor(provider)}
                strokeWidth={2}
                fill={`url(#g-${provider})`}
                isAnimationActive={false}
              />
            ))}
          </AreaChart>
        </ResponsiveContainer>
      </div>
      {/* 图例自绘而非 recharts <Legend/>：渠道图标与名称要成组、按需折行，
          且复用与列表行一致的 ProviderIcon。 */}
      <div
        className="mt-3 flex flex-wrap items-center justify-center gap-x-4 gap-y-1.5 text-xs text-muted-foreground"
        data-testid="usage-chart-legend"
      >
        {providers.map((provider) => (
          <span key={provider} className="inline-flex items-center gap-1.5">
            <ProviderIcon provider={provider} size={12} />
            {providerLabel(provider)}
          </span>
        ))}
      </div>
    </div>
  );
}
