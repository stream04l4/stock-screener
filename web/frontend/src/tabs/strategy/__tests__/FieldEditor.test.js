// FieldEditor 单测（brief D2）：各 FIELD_TYPE 分支渲染 + weights 归一化/和≠1 警告。
// happy-dom 环境 mount 组件；滑块 input 事件用 el.value=… + dispatchEvent 驱动。
import { describe, it, expect } from "vitest";
import { mount } from "@vue/test-utils";
import FieldEditor from "../FieldEditor.vue";

function m(props) {
  return mount(FieldEditor, { props });
}

describe("FieldEditor · FIELD_TYPE 分支渲染（移植 app.js editControl L832-890）", () => {
  it("int → number input step=1，值为 String(value)", () => {
    const w = m({ section: "technical", field: "ma_period", value: 60 });
    const input = w.find("input[type=number]");
    expect(input.exists()).toBe(true);
    expect(input.attributes("step")).toBe("1");
    expect(input.element.value).toBe("60");
  });

  it("float → number input step=any", () => {
    const w = m({ section: "technical", field: "min_return_pct", value: 2.5 });
    const input = w.find("input[type=number]");
    expect(input.attributes("step")).toBe("any");
    expect(input.element.value).toBe("2.5");
  });

  it("bool → select(true/false)，选中项跟随值", () => {
    let w = m({ section: "hard_filter", field: "st_enabled", value: true });
    expect(w.find("select").element.value).toBe("true");
    w = m({ section: "hard_filter", field: "st_enabled", value: false });
    expect(w.find("select").element.value).toBe("false");
  });

  it("enum2 → select(ENUM_OPTIONS)；当前值选中；无枚举定义时回退 [[value,value]]", () => {
    let w = m({ section: "scoring", field: "mode", value: "zscore" });
    const opts = w.findAll("option");
    expect(opts.map((o) => o.attributes("value"))).toEqual(["zscore", "legacy"]);
    expect(w.find("select").element.value).toBe("zscore");

    // 无 ENUM_OPTIONS 定义（如 industry.rank_by 之外的 enum2 字段）→ 回退当前值单选项
    w = m({ section: "industry", field: "rank_by", value: "roeAvg" });
    expect(w.findAll("option").map((o) => o.attributes("value"))).toEqual(["roeAvg"]);
  });

  it("list → text input，数组 join(', ')，placeholder 同旧版", () => {
    const w = m({ section: "universe", field: "a_share_prefixes", value: ["sh.60", "sz.00"] });
    const input = w.find("input[type=text]");
    expect(input.element.value).toBe("sh.60, sz.00");
    expect(input.attributes("placeholder")).toBe("sh.60, sh.68, sz.00, sz.30");
  });

  it("str → text input 原样", () => {
    const w = m({ section: "data", field: "cache_dir", value: "cache/" });
    expect(w.find("input[type=text]").element.value).toBe("cache/");
  });

  it("未知字段类型 → 回退 str（text input）", () => {
    const w = m({ section: "lake", field: "some_new_field", value: "x" });
    expect(w.find("input[type=text]").element.value).toBe("x");
  });
});

describe("FieldEditor · weights_dict 4 滑块（实时归一化 + 和≠1 警告）", () => {
  const base = { technical: 0.25, dividend: 0.25, industry: 0.25, fundamental: 0.25 };

  it("渲染 4 行（w-row），标签=WEIGHT_DIMS label，滑块 value=当前权重", () => {
    const w = m({ section: "scoring", field: "weights", value: base });
    expect(w.findAll(".w-row").length).toBe(4);
    expect(w.findAll(".w-label").map((e) => e.text())).toEqual(["技术面", "股息", "行业", "基本面"]);
    const sliders = w.findAll('input[type=range]');
    expect(sliders.map((s) => s.element.value)).toEqual(["0.25", "0.25", "0.25", "0.25"]);
  });

  it("和=1 → 归一化值显示、无警告", () => {
    const w = m({ section: "scoring", field: "weights", value: base });
    expect(w.findAll(".w-norm").map((e) => e.text())).toEqual([
      "归一化 0.250", "归一化 0.250", "归一化 0.250", "归一化 0.250",
    ]);
    expect(w.text()).not.toContain("保存将被拒绝");
  });

  it("拖动滑块 → emit update（{section,field,value:全4维}）+ 实时归一化重算 + 和≠1 警告", async () => {
    const w = m({ section: "scoring", field: "weights", value: base });
    const slider = w.findAll('input[type=range]')[0]; // 技术面
    await slider.setValue("0.5"); // 和=1.25 ≠ 1

    const emitted = w.emitted("update");
    expect(emitted).toBeTruthy();
    const last = emitted[emitted.length - 1][0];
    expect(last.section).toBe("scoring");
    expect(last.field).toBe("weights");
    expect(last.value.technical).toBe(0.5);
    expect(last.value.dividend).toBe(0.25); // 其余维保持

    await w.vm.$nextTick();
    const norms = w.findAll(".w-norm").map((e) => e.text());
    // 0.5/1.25=0.4, 0.25/1.25=0.2；和=1.25≠1 → 警告（旧版文案原样）
    expect(norms[0]).toBe("归一化 0.400（和=1.25≠1，保存将被拒绝）");
    expect(norms[1]).toBe("归一化 0.200（和=1.25≠1，保存将被拒绝）");
  });

  it("全 0 → 归一化显示 '—'（旧版 sum>0 判定）", async () => {
    const w = m({ section: "scoring", field: "weights", value: { technical: 0, dividend: 0, industry: 0, fundamental: 0 } });
    await w.vm.$nextTick();
    expect(w.findAll(".w-norm").map((e) => e.text())).toEqual(["归一化 —", "归一化 —", "归一化 —", "归一化 —"]);
  });
});

describe("FieldEditor · sub_weights_dict（每维度 key:值 文本，parseSubWeightsText 语义）", () => {
  const base = {
    technical: { ma_pos: 0.5, ret: 0.3, vol: 0.2 },
    dividend: { yield: 1 },
    industry: { roe: 1 },
    fundamental: { roe: 0.6, ocf: 0.4 },
  };

  it("渲染 4 行文本框，值=key:值,key:值（Object.entries 顺序）", () => {
    const w = m({ section: "scoring", field: "sub_weights", value: base });
    const inputs = w.findAll(".subweight-edits input[type=text]");
    expect(inputs.length).toBe(4);
    expect(inputs[0].element.value).toBe("ma_pos:0.5,ret:0.3,vol:0.2");
    expect(inputs[1].element.value).toBe("yield:1");
  });

  it("修改某维文本 → emit 完整 4 维解析对象（合法=对象，非法=null）", async () => {
    const w = m({ section: "scoring", field: "sub_weights", value: base });
    const inputs = w.findAll(".subweight-edits input[type=text]");
    await inputs[1].setValue("yield:0.8, payout:0.2"); // 合法

    let last = w.emitted("update").at(-1)[0];
    expect(last.value.dividend).toEqual({ yield: 0.8, payout: 0.2 });
    expect(last.value.technical).toEqual({ ma_pos: 0.5, ret: 0.3, vol: 0.2 }); // 其余维保持解析值

    await inputs[1].setValue("bad-format"); // 非法 → null（保存时统一报错）
    last = w.emitted("update").at(-1)[0];
    expect(last.value.dividend).toBeNull();
  });

  it("空维度文本 → 该行 null，其余维不受影响", async () => {
    const w = m({ section: "scoring", field: "sub_weights", value: base });
    const inputs = w.findAll(".subweight-edits input[type=text]");
    await inputs[2].setValue(""); // industry 清空
    const last = w.emitted("update").at(-1)[0];
    expect(last.value.industry).toBeNull();
    expect(last.value.fundamental).toEqual({ roe: 0.6, ocf: 0.4 });
  });
});
