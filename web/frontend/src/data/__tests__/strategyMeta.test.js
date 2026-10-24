// strategyMeta 单测（v7.0）：元数据完整性 + 与 config/strategy.yaml 对齐守卫。
// - SECTION_META/SECTION_GROUPS/SECTION_DESC 覆盖渲染的 2 段（strategy/universe）；
// - FIELD_DESC/FIELD_TYPE 全字段守卫：yaml 中 strategy/universe 段每个顶层字段
//   必须有 desc + type（防未来加字段漏说明）；lake/datasource = yaml-only 不渲染、不进守卫；
// - displayValue/readableValue/compactJson 边界。
import { describe, it, expect } from "vitest";
import fs from "node:fs";
import path from "node:path";
import {
  SECTION_META, SECTION_GROUPS, EXCLUDED_SECTIONS, SECTION_DESC, FIELD_DESC, FIELD_LABELS,
  FIELD_TYPE, ENUM_OPTIONS, HIDDEN_FIELDS, displayValue, readableValue, compactJson,
} from "../strategyMeta.js";

// config/strategy.yaml 顶层字段结构（仓库根 = web/frontend 上两级）。
// 守卫直接读**生产 yaml**（真·防未来加字段漏说明），用最小结构解析器只取
// 「顶层 section + 其 2 空格缩进的直接子字段键」——不解析值、不引入 js-yaml 依赖。
// 规则：列 0 `name:` = section 头；恰好 2 空格缩进 + 标识符 + `:` = 顶层字段键；
// 4+ 空格缩进的子键与 `- item` 列表项均排除。
const REPO_ROOT = path.resolve(__dirname, "../../../../..");
const STRATEGY_YAML = path.join(REPO_ROOT, "config", "strategy.yaml");
function loadStrategyFields() {
  const lines = fs.readFileSync(STRATEGY_YAML, "utf-8").split(/\r?\n/);
  const out = {};
  let section = null;
  for (const line of lines) {
    if (/^[A-Za-z_]\w*:\s*(#.*)?$/.test(line)) {
      section = line.slice(0, line.indexOf(":"));
      out[section] = [];
    } else if (section && /^ {2}[A-Za-z_]\w*:/.test(line)) {
      out[section].push(line.slice(2).split(/[:\s]/)[0]);
    }
  }
  return out;
}

describe("strategyMeta · v7 结构", () => {
  it("SECTION_META = 3 段（strategy/universe/timing），title/icon 齐全", () => {
    expect(Object.keys(SECTION_META).sort()).toEqual(["strategy", "timing", "universe"]);
    for (const k of Object.keys(SECTION_META)) {
      expect(typeof SECTION_META[k].title).toBe("string");
      expect(typeof SECTION_META[k].icon).toBe("string");
    }
  });

  it("SECTION_GROUPS = 3 组（持仓策略/股票池/选股策略），无重叠，均默认展开", () => {
    expect(SECTION_GROUPS.map((g) => g.id)).toEqual(["strategy", "universe", "timing"]);
    const all = SECTION_GROUPS.flatMap((g) => g.sections);
    expect(new Set(all).size).toBe(all.length);
    expect(all).toEqual(["strategy", "universe", "timing"]);
    for (const g of SECTION_GROUPS) expect(g.defaultOpen).toBe(true);
  });

  it("EXCLUDED_SECTIONS = lake/datasource（yaml-only，页面不渲染）", () => {
    expect(EXCLUDED_SECTIONS.sort()).toEqual(["datasource", "lake"]);
    const all = SECTION_GROUPS.flatMap((g) => g.sections);
    for (const s of EXCLUDED_SECTIONS) expect(all).not.toContain(s);
  });

  it("SECTION_DESC 覆盖渲染的 3 段；strategy 段含 k=0 占档必附注", () => {
    for (const k of Object.keys(SECTION_META)) {
      expect(typeof SECTION_DESC[k]).toBe("string");
      expect(SECTION_DESC[k].length).toBeGreaterThan(10);
    }
    expect(SECTION_DESC.strategy).toContain("k=0");
    expect(SECTION_DESC.strategy).toContain("base_add_amount×growth_ratio");
  });

  it("HIDDEN_FIELDS = {strategy.name}（策略标识不作为字段行渲染）", () => {
    expect(HIDDEN_FIELDS.has("strategy.name")).toBe(true);
    expect(HIDDEN_FIELDS.size).toBe(1);
  });
});

describe("strategyMeta · FIELD_TYPE / ENUM_OPTIONS（v7 slim）", () => {
  it("FIELD_TYPE = 24 键（strategy 9 + universe 10 + timing 5），类型逐一断言", () => {
    const expected = {
      name: "str",
      initial_amount: "int",
      base_add_amount: "int",
      first_drawdown_pct: "float",
      add_step_pct: "float",
      growth_ratio: "float",
      rearm_mode: "enum2",
      rearm_pct: "float",
      accounting: "readonly",
      industry_whitelist_csric2: "list",
      soe_keywords: "list",
      min_total_mv_yi: "float",
      min_consecutive_div_years: "int",
      listing_min_trading_days: "int",
      backtest_positive: "bool",
      backtest_window_years: "int",
      min_ttm_yield_pct: "float",
      price_min_yuan: "float",
      price_max_yuan: "float",
      // v7.0.13 timing
      price_window_years: "int",
      price_pctile_max: "float",
      yield_min_pct: "float",
      below_ma250: "bool",
      ma_period: "int",
    };
    expect(FIELD_TYPE).toEqual(expected);
  });

  it("ENUM_OPTIONS 仅 strategy.rearm_mode（bounce/newhigh）", () => {
    expect(Object.keys(ENUM_OPTIONS)).toEqual(["strategy.rearm_mode"]);
    expect(ENUM_OPTIONS["strategy.rearm_mode"].map(([v]) => v)).toEqual(["bounce", "newhigh"]);
  });
});

describe("strategyMeta · FIELD_DESC/FIELD_TYPE 全字段守卫（防未来加字段漏说明）", () => {
  it("yaml strategy/universe 段每个顶层字段都有 FIELD_DESC + FIELD_TYPE", () => {
    const cfg = loadStrategyFields();
    const rendered = new Set(SECTION_GROUPS.flatMap((g) => g.sections));
    const missingDesc = [];
    const missingType = [];
    for (const [section, fields] of Object.entries(cfg)) {
      if (!rendered.has(section)) continue; // lake/datasource = yaml-only，不进守卫
      for (const field of fields) {
        if (!FIELD_DESC[`${section}.${field}`]) missingDesc.push(`${section}.${field}`);
        if (!(field in FIELD_TYPE)) missingType.push(`${section}.${field}`);
      }
    }
    expect(missingDesc).toEqual([]);
    expect(missingType).toEqual([]);
  });

  it("FIELD_DESC/FIELD_TYPE 覆盖率 = 100%（渲染段 yaml 字段数 vs 条数，无多余/缺失）", () => {
    const cfg = loadStrategyFields();
    const rendered = new Set(SECTION_GROUPS.flatMap((g) => g.sections));
    let topFields = 0;
    for (const [s, fields] of Object.entries(cfg)) if (rendered.has(s)) topFields += fields.length;
    // FIELD_DESC 的键全部落在渲染段
    for (const k of Object.keys(FIELD_DESC)) expect(rendered.has(k.split(".")[0])).toBe(true);
    expect(Object.keys(FIELD_DESC).length).toBe(topFields);
    expect(Object.keys(FIELD_TYPE).length).toBe(topFields);
    // 关键 desc 非空（调研 ③ 草稿原文要点）
    expect(FIELD_DESC["strategy.initial_amount"]).toContain("k=0");
    expect(FIELD_DESC["strategy.accounting"]).toContain("只读");
    expect(FIELD_DESC["universe.min_total_mv_yi"]).toContain("总市值");
    // v7.0.13 timing desc 关键点
    expect(FIELD_DESC["timing.price_pctile_max"]).toContain("分位");
    expect(FIELD_DESC["timing.yield_min_pct"]).toContain("股息率");
  });

  it("FIELD_LABELS 覆盖渲染段 yaml 全部字段（通俗名非空；条数=FIELD_TYPE；防未来加字段漏改名）", () => {
    const cfg = loadStrategyFields();
    const rendered = new Set(SECTION_GROUPS.flatMap((g) => g.sections));
    for (const [section, fields] of Object.entries(cfg)) {
      if (!rendered.has(section)) continue;
      for (const field of fields) {
        expect(FIELD_LABELS[`${section}.${field}`], `${section}.${field} 缺通俗名`).toBeTruthy();
      }
    }
    for (const k of Object.keys(FIELD_LABELS)) expect(rendered.has(k.split(".")[0])).toBe(true);
    expect(Object.keys(FIELD_LABELS).length).toBe(Object.keys(FIELD_TYPE).length);
  });
});

describe("displayValue · 按 FIELD_TYPE 分支（v7）", () => {
  it("list → join(', ')；非数组原样字符串化", () => {
    expect(displayValue("universe", "soe_keywords", ["国务院", "汇金"])).toBe("国务院, 汇金");
    expect(displayValue("x", "soe_keywords", "oops")).toBe("oops");
  });

  it("readonly（accounting）→ 标量 String(v)", () => {
    expect(displayValue("strategy", "accounting", "cash")).toBe("cash");
  });

  it("int/float/enum2/str → String(v)", () => {
    expect(displayValue("strategy", "initial_amount", 100000)).toBe("100000");
    expect(displayValue("strategy", "first_drawdown_pct", 15)).toBe("15");
    expect(displayValue("strategy", "rearm_mode", "bounce")).toBe("bounce");
  });

  it("dict 值（未注册字段）→ 紧凑 JSON 摘要，不是 [object Object]", () => {
    const v = displayValue("x", "y", { a: 1 });
    expect(v).not.toContain("[object Object]");
    expect(v).toBe('{"a":1}');
  });

  it("list >50 项 → 前 50 + '…共N项' 截断", () => {
    const items = Array.from({ length: 60 }, (_, i) => `s${i}`);
    const v = displayValue("x", "y", items);
    expect(v).toContain("s49");
    expect(v).toContain("…共60项");
  });

  it("深嵌套 dict → 深度 >2 层截断为 '…'", () => {
    const v = displayValue("x", "y", { a: { b: { c: 1 } } });
    expect(v).toBe('{"a":{"b":…}}');
  });

  it("空值边界：null → 'null'、{} → '{}'、[] → '[]'", () => {
    expect(displayValue("x", "y", null)).toBe("null");
    expect(displayValue("x", "y", {})).toBe("{}");
    expect(displayValue("x", "y", [])).toBe("[]");
  });
});

describe("readableValue/compactJson · 边界", () => {
  it("标量 → String；数字保持 JSON 字面", () => {
    expect(readableValue(50)).toBe("50");
    expect(readableValue(3.5)).toBe("3.5");
  });

  it("list 含对象元素 → JSON.stringify（防 [object Object]）", () => {
    expect(readableValue(["a", { b: 1 }])).toBe('[a, {"b":1}]');
  });

  it("compactJson 深度截断", () => {
    expect(compactJson({ a: { b: { c: 1 } } }, 2)).toBe('{"a":{"b":…}}');
    expect(compactJson([1, { e: 2 }], 1)).toBe('[1,…]');
  });
});
