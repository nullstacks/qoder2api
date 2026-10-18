import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { UsageChart, chartProviders } from "../UsageChart";

describe("chartProviders", () => {
  it("收集出现过的渠道名，按字母序稳定排序", () => {
    expect(chartProviders([
      { hour: 1, trae: 1, codebuddy: 2, zen: 3 },
      { hour: 2, trae: 4 },
    ])).toEqual(["codebuddy", "trae", "zen"]);
  });

  it("只有 hour 时返回空数组（不把保留键当渠道）", () => {
    expect(chartProviders([{ hour: 1 }])).toEqual([]);
  });
});

describe("UsageChart", () => {
  it("渲染容器（jsdom 无布局，ResponsiveContainer 不产出 SVG）", () => {
    const { getByTestId } = render(
      <UsageChart points={[
        { hour: 1_700_000_000, codebuddy: 1, future: 2 },
        { hour: 1_700_003_600, codebuddy: 3, future: 4 },
      ]} />,
    );
    expect(getByTestId("usage-chart")).toBeInTheDocument();
  });

  it("渠道图例渲染在图表下方而非标题行", () => {
    const { getByTestId } = render(
      <UsageChart points={[
        { hour: 1_700_000_000, codebuddy: 1, trae: 2 },
        { hour: 1_700_003_600, codebuddy: 3, trae: 4 },
      ]} />,
    );
    const chart = getByTestId("usage-chart");
    const legend = getByTestId("usage-chart-legend");
    // 图例是图表容器（固定高度绘图区）之后的兄弟节点——位于下方，不占标题行
    const plot = chart.firstElementChild;
    expect(plot?.className).toContain("h-64");
    expect(legend.previousElementSibling).toBe(plot);
    expect(legend).toHaveTextContent("CodeBuddy");
    expect(legend).toHaveTextContent("TRAE");
  });
});
