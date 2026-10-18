import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Field, Input, Select } from "../../ui";

/**
 * Field 的 label 关联。
 *
 * 曾经的写法是 `<Label>{label}</Label>`：label 与控件之间没有任何关系，
 * 读屏只会念「编辑文本」而不会念字段名（如「额度探测周期 生效值」属于哪一项），
 * 点 label 也不聚焦控件。这里锁住 htmlFor 必须真的绑到控件 id 上。
 */
describe("Field label 关联", () => {
  it("传 htmlFor 时 label 的 for 指向控件，且控件 id 一致", () => {
    render(
      <Field label="用户名" htmlFor="u">
        <Input id="u" />
      </Field>,
    );
    const label = screen.getByText("用户名");
    const input = screen.getByLabelText("用户名");
    expect(label).toHaveAttribute("for", "u");
    expect(input).toHaveAttribute("id", "u");
  });

  it("Select 同样可被 label 关联（布尔型配置项走 Select）", () => {
    render(
      <Field label="开关" htmlFor="s">
        <Select id="s">
          <option value="true">开启</option>
          <option value="false">关闭</option>
        </Select>
      </Field>,
    );
    expect(screen.getByLabelText("开关").tagName).toBe("SELECT");
  });

  it("不传 htmlFor 时不产生 for，也不伪造指向", () => {
    render(<Field label="裸字段">
      <Input />
    </Field>);
    const label = screen.getByText("裸字段");
    expect(label).not.toHaveAttribute("for");
    // label 仍渲染（文案不能因为不关联就消失）
    expect(label.tagName).toBe("LABEL");
  });
});
