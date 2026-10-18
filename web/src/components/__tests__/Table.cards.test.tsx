import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../../ui";

/** data-testid 透传到 <table> 本体（外层滚动容器只带 data-slot）。 */
function tableEl() {
  return screen.getByTestId("t");
}

function renderTable(cards: boolean) {
  return render(
    <Table data-testid="t" cards={cards}>
      <TableHeader>
        <TableRow>
          <TableHead>名称</TableHead>
          <TableHead className="text-right">数值</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        <TableRow data-testid="row">
          <TableCell label="名称">甲</TableCell>
          <TableCell label="数值" className="text-right">
            1
          </TableCell>
        </TableRow>
      </TableBody>
    </Table>,
  );
}

describe("Table 窄屏卡片模式", () => {
  it("cards 打开时表体带 data-table-cards，窄屏 CSS 据此转卡片", () => {
    renderTable(true);
    expect(tableEl()).toHaveAttribute("data-table-cards");
  });

  it("默认不开：宽屏表格不被打上卡片标记", () => {
    renderTable(false);
    expect(tableEl()).not.toHaveAttribute("data-table-cards");
  });

  it("label 落到单元格的 data-label，供 CSS ::before 取字段名", () => {
    renderTable(true);
    const cells = screen.getByTestId("row").querySelectorAll("td");
    expect(cells[0]).toHaveAttribute("data-label", "名称");
    expect(cells[1]).toHaveAttribute("data-label", "数值");
  });

  it("不给 label 时不产生 data-label（空占位格不出字段名）", () => {
    render(
      <Table data-testid="t" cards>
        <TableBody>
          <TableRow data-testid="row">
            <TableCell aria-hidden />
          </TableRow>
        </TableBody>
      </Table>,
    );
    expect(screen.getByTestId("row").querySelector("td")).not.toHaveAttribute(
      "data-label",
    );
  });

  it("DOM 结构仍是标准表：宽屏与读屏拿到的 row / columnheader 不因卡片模式改变", () => {
    renderTable(true);
    // 卡片模式只改样式，不改结构——否则页面里按行断言的语义会被破坏
    expect(screen.getAllByRole("row")).toHaveLength(2);
    expect(screen.getAllByRole("columnheader")).toHaveLength(2);
    expect(screen.getByRole("columnheader", { name: "名称" })).toBeInTheDocument();
  });

  it("卡片模式补显式表格角色（display:block 会让浏览器丢掉隐式语义）", () => {
    renderTable(true);
    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(screen.getAllByRole("rowgroup")).toHaveLength(2);
    expect(screen.getAllByRole("cell")).toHaveLength(2);
  });

  it("不开卡片模式时不加任何显式 role（沿用浏览器隐式语义）", () => {
    renderTable(false);
    const table = tableEl();
    expect(table).not.toHaveAttribute("role");
    expect(table.querySelector("th")).not.toHaveAttribute("role");
    expect(table.querySelector("td")).not.toHaveAttribute("role");
  });
});
