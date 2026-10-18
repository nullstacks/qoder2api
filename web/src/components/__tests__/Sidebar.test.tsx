import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { SidebarNav } from "@/components/Sidebar";
import { NavBadge } from "@/ui";
import { fullSession } from "@/test-utils";

function renderNav(session: Parameters<typeof fullSession>[0], badges?: Parameters<
  typeof SidebarNav
>[0]["badges"]) {
  return render(
    <MemoryRouter>
      <SidebarNav session={fullSession(session)} badges={badges} />
    </MemoryRouter>,
  );
}

describe("NavBadge", () => {
  it("渲染计数与全语义文案（可见数字 aria-hidden）", () => {
    render(<NavBadge value={3} tone="warn" label="3 条告警尚未恢复" />);
    expect(screen.getByText("3")).toHaveAttribute("aria-hidden");
    expect(screen.getByText("3 条告警尚未恢复")).toHaveClass("sr-only");
  });

  it("> 99 收敛成 99+，避免侧栏宽度被撑开", () => {
    render(<NavBadge value={1234} label="很多" />);
    expect(screen.getByText("99+")).toBeInTheDocument();
  });

  it("0 / 空串不渲染（不占位）", () => {
    const { container } = render(<NavBadge value={0} label="无" />);
    expect(container).toBeEmptyDOMElement();
    expect(render(<NavBadge value="" label="无" />).container).toBeEmptyDOMElement();
  });

  it("文字形态原样渲染（模型目录的「陈旧」）", () => {
    render(<NavBadge value="陈旧" label="模型目录快照陈旧" />);
    expect(screen.getByText("陈旧")).toBeInTheDocument();
  });
});

describe("SidebarNav 角标接线", () => {
  it("按 badgeKey 把对应计数挂到导航项上（admin 全量）", () => {
    renderNav({ username: "root", is_admin: true }, {
      credentials: 2, alerts: 1, tasks: 3, users: 4,
    });
    // 每项的无障碍文案可被读屏定位到，且互不串台
    expect(screen.getByText("2 个凭证需重新登录或额度耗尽")).toBeInTheDocument();
    expect(screen.getByText("1 条告警尚未恢复")).toBeInTheDocument();
    expect(screen.getByText("3 个后台任务上次执行失败")).toBeInTheDocument();
    expect(screen.getByText("4 个账号待激活")).toBeInTheDocument();
  });

  it("badges 未加载（undefined）时导航照常渲染、无角标", () => {
    renderNav({ username: "root", is_admin: true });
    expect(screen.getByText("运维告警")).toBeInTheDocument();
    expect(screen.queryByText(/条告警尚未恢复/)).not.toBeInTheDocument();
  });

  it("后端裁剪（viewer 只有 api_keys）时其余角标不渲染", () => {
    renderNav({ username: "guest", is_admin: false }, { api_keys: 1 });
    expect(screen.getByText("1 个 API Key 已过期或 7 天内到期")).toBeInTheDocument();
    expect(screen.queryByText(/个凭证需重新登录/)).not.toBeInTheDocument();
  });
});
