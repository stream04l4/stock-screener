// FieldEditor 单测（v7.0）：各 FIELD_TYPE 分支渲染（int/float/bool/enum2/list/str/readonly）。
// v7.0：weights_dict/sub_weights_dict 分支已删（多因子权重随策略核心移除）。
import { describe, it, expect } from "vitest";
import { mount } from "@vue/test-utils";
import FieldEditor from "../FieldEditor.vue";

function m(props) {
  return mount(FieldEditor, { props });
}

describe("FieldEditor · FIELD_TYPE 分支渲染（v7 slim）", () => {
  it("int → number input step=1，值为 String(value)", () => {
    const w = m({ section: "strategy", field: "initial_amount", value: 100000 });
    const input = w.find("input[type=number]");
    expect(input.exists()).toBe(true);
    expect(input.attributes("step")).toBe("1");
    expect(input.element.value).toBe("100000");
  });

  it("float → number input step=any", () => {
    const w = m({ section: "strategy", field: "first_drawdown_pct", value: 15 });
    const input = w.find("input[type=number]");
    expect(input.attributes("step")).toBe("any");
    expect(input.element.value).toBe("15");
    const w2 = m({ section: "strategy", field: "growth_ratio", value: 1.1 });
    expect(w2.find("input[type=number]").element.value).toBe("1.1");
  });

  it("enum2 → select(ENUM_OPTIONS)；当前值选中；值不在选项内时仍渲染选项列表", () => {
    let w = m({ section: "strategy", field: "rearm_mode", value: "bounce" });
    const opts = w.findAll("option");
    expect(opts.map((o) => o.attributes("value"))).toEqual(["bounce", "newhigh"]);
    expect(w.find("select").element.value).toBe("bounce");

    // 值不在选项内（外部改过）→ 选项列表不变
    w = m({ section: "strategy", field: "rearm_mode", value: "other" });
    expect(w.findAll("option").map((o) => o.attributes("value"))).toEqual(["bounce", "newhigh"]);
  });

  it("list → text input，数组 join(', ')，placeholder = 行业码示例", () => {
    const w = m({ section: "universe", field: "industry_whitelist_csric2", value: ["B06", "C25"] });
    const input = w.find("input[type=text]");
    expect(input.element.value).toBe("B06, C25");
    expect(input.attributes("placeholder")).toBe("B06, B07, C25, D44, …");
  });

  it("str → text input 原样（name 固定标识）", () => {
    const w = m({ section: "strategy", field: "name", value: "drawdown_martingale" });
    expect(w.find("input[type=text]").element.value).toBe("drawdown_martingale");
  });

  it("readonly（accounting）→ 只读展示值，不渲染 input", () => {
    const w = m({ section: "strategy", field: "accounting", value: "cash" });
    expect(w.find("input").exists()).toBe(false);
    expect(w.find(".s-readonly").exists()).toBe(true);
    expect(w.find(".s-readonly").text()).toBe("cash");
  });

  it("未知字段类型 → 回退 str（text input）", () => {
    const w = m({ section: "lake", field: "some_new_field", value: "x" });
    expect(w.find("input[type=text]").element.value).toBe("x");
  });

  it("标量 input 变更 → emit update {section, field, value:原始字符串}", async () => {
    const w = m({ section: "strategy", field: "initial_amount", value: 100000 });
    const input = w.find("input[type=number]");
    input.element.value = "200000";
    await input.trigger("input");
    const last = w.emitted("update").at(-1)[0];
    expect(last).toEqual({ section: "strategy", field: "initial_amount", value: "200000" });
  });
});
