// strategyMeta 单测（v6.2 O1/O2）：元数据完整性 + 分组/section说明覆盖 + FIELD_DESC/FIELD_TYPE
// 全字段守卫（与 config/strategy.yaml 对齐，防未来加字段漏说明）+ parseSubWeightsText/displayValue 边界。
import { describe, it, expect } from "vitest";
import fs from "node:fs";
import path from "node:path";
import {
  SECTION_META, SECTION_GROUPS, SECTION_DESC, FIELD_DESC, FIELD_TYPE, ENUM_OPTIONS, WEIGHT_DIMS,
  parseSubWeightsText, displayValue,
} from "../strategyMeta.js";

// config/strategy.yaml 顶层字段结构（仓库根 = web/frontend 上两级）。
// 守卫直接读**生产 yaml**（真·防未来加字段漏说明），用最小结构解析器只取「顶层 section + 其
// 2 空格缩进的直接子字段键」——不解析值、不引入 js-yaml 依赖（npm install 在本环境被安全扫描拦截）。
// 规则：列 0 `name:` = section 头；恰好 2 空格缩进 + 标识符 + `:` = 该 section 的顶层字段
// （含 baostock/tencent/costs 这类嵌套 dict 头——它们是 datasource/backtest 的顶层字段）；
// 4+ 空格缩进的子键与 `- item` 列表项均排除（= 只读展示、本期不做深编辑的子层）。
const REPO_ROOT = path.resolve(__dirname, "../../../../..");
const STRATEGY_YAML = path.join(REPO_ROOT, "config", "strategy.yaml");
function loadStrategyFields() {
  const lines = fs.readFileSync(STRATEGY_YAML, "utf-8").split(/\r?\n/);
  const out = {}; // {section: [field, ...]}
  let section = null;
  for (const line of lines) {
    if (/^[A-Za-z_]\w*:\s*(#.*)?$/.test(line)) {          // 列 0 section 头（无缩进）
      section = line.slice(0, line.indexOf(":"));
      out[section] = [];
    } else if (section && /^ {2}[A-Za-z_]\w*:/.test(line)) {
      const name = line.slice(2).split(/[:\s]/)[0];       // 恰好 2 空格缩进的顶层字段键（含内联值/纯键）
      out[section].push(name);
    }
  }
  return out;
}

describe("strategyMeta · SECTION_META / SECTION_GROUPS / SECTION_DESC（v6.2 O1）", () => {
  it("SECTION_META 覆盖全部 16 段，title/icon 齐全", () => {
    const keys = ["technical", "dividend", "industry", "fundamental", "universe",
      "scoring", "badges", "hard_filter", "backtest", "reinvest", "datasource",
      "data", "crosscheck", "health", "canonical", "lake"];
    for (const k of keys) {
      expect(SECTION_META[k]).toBeTruthy();
      expect(typeof SECTION_META[k].title).toBe("string");
      expect(typeof SECTION_META[k].icon).toBe("string");
    }
  });

  it("SECTION_GROUPS = 三段有序分组，section 无重叠且覆盖全部 16 段", () => {
    expect(SECTION_GROUPS.map((g) => g.id)).toEqual(["core", "backtest", "infra"]);
    const all = SECTION_GROUPS.flatMap((g) => g.sections);
    // 无重叠
    expect(new Set(all).size).toBe(all.length);
    // 覆盖全部 16 段（与 SECTION_META 键集一致）
    expect([...all].sort()).toEqual(Object.keys(SECTION_META).sort());
    // 策略核心默认展开，其余默认折叠
    expect(SECTION_GROUPS[0].defaultOpen).toBe(true);
    expect(SECTION_GROUPS[1].defaultOpen).toBe(false);
    expect(SECTION_GROUPS[2].defaultOpen).toBe(false);
  });

  it("SECTION_DESC 覆盖全部 16 段（每段一行说明）", () => {
    for (const k of Object.keys(SECTION_META)) {
      expect(typeof SECTION_DESC[k]).toBe("string");
      expect(SECTION_DESC[k].length).toBeGreaterThan(4);
    }
  });
});

describe("strategyMeta · FIELD_TYPE / ENUM_OPTIONS / WEIGHT_DIMS", () => {
  it("FIELD_TYPE 含全部旧版字段类型 + v6.2 新增（readonly/bool/float/int/str/list/enum2）", () => {
    const expected = {
      ma_period: "int", return_window_days: "int", min_return_pct: "float",
      max_return_pct: "float", max_annual_volatility_pct: "float",
      window_days: "int", min_yield_pct: "float",
      payout_band_pct: "readonly", payout_out_of_band_decay: "float",
      rank_by: "enum2", top_pct: "float", min_group_size: "int",
      roe_min_pct: "float", net_profit_yoy_field: "enum2", liability_max_pct: "float",
      gross_margin_min_pct: "float", probe_quarters_back: "int",
      a_share_prefixes: "list", listing_min_trading_days: "int", st_name_keyword: "str",
      soe_required: "bool", industry_whitelist_csric2: "list", min_total_mv_yi: "float",
      soe_keywords: "list",
      mode: "enum2", top_n: "int", missing_policy: "enum2", normalize: "str",
      weights: "weights_dict", sub_weights: "sub_weights_dict",
      industry_top_pct: "float", fscore_min: "int",
      st_enabled: "bool", min_consecutive_div_years: "int",
      start: "str", end: "str", rebalance: "enum2", weights_ref: "str", execution: "enum2",
      costs: "readonly", suspension: "readonly", benchmarks: "readonly", risk_free_pct: "float",
      kline_calendar_days_back: "int", retry_max_attempts: "int", cache_dir: "str",
      primary: "enum2", fallback: "enum2",
      baostock: "readonly", tencent: "readonly", exdate_detector: "readonly",
      universe: "readonly", factor_reconcile: "readonly", contract: "readonly",
      em: "readonly", sina: "readonly", rf: "readonly",
      target_ttm_yield_pct: "float", yield_pctile_lookback_years: "int",
      dps_smooth_years: "int", dps_growth_years: "int",
      enabled: "bool", sample_size: "int", price_tolerance_pct: "float", batch_size: "int",
      ttm_tolerance_pct: "float",
      dir: "str", data_version: "str",
      close_tolerance_pct: "float", r_event_tolerance_pct: "float", dps_warn_at: "float",
      dps_stop_at: "float", roe_tolerance_pp: "float", market_min_rows: "int",
      akshare_interval_s: "float", akshare_max_attempts: "int", akshare_breaker: "int",
      alerts: "readonly",
      baostock_daily_budget: "int",
    };
    expect(FIELD_TYPE).toEqual(expected);
  });

  it("ENUM_OPTIONS 含旧版 + v6.2 新增枚举（backtest.rebalance/execution、datasource.primary/fallback）", () => {
    expect(ENUM_OPTIONS["industry.rank_by"]).toEqual([["roeAvg", "roeAvg（最近披露报告期 ROE）"]]);
    expect(ENUM_OPTIONS["fundamental.net_profit_yoy_field"].map(([v]) => v)).toEqual(["YOYPNI", "YOYNI"]);
    expect(ENUM_OPTIONS["scoring.mode"].map(([v]) => v)).toEqual(["zscore", "legacy"]);
    expect(ENUM_OPTIONS["scoring.missing_policy"].map(([v]) => v)).toEqual(["neutral_renorm", "neutral", "drop"]);
    expect(ENUM_OPTIONS["backtest.rebalance"].map(([v]) => v)).toEqual(["monthly", "quarterly"]);
    expect(ENUM_OPTIONS["backtest.execution"].map(([v]) => v)).toEqual(["t1_open", "t1_close", "t_close"]);
    expect(ENUM_OPTIONS["datasource.primary"].map(([v]) => v)).toEqual(["tencent", "baostock"]);
    expect(ENUM_OPTIONS["datasource.fallback"].map(([v]) => v)).toEqual(["fail_fast", "baostock"]);
  });

  it("WEIGHT_DIMS = 四维（key/label 成对）", () => {
    expect(WEIGHT_DIMS).toEqual([
      ["technical", "技术面"], ["dividend", "股息"],
      ["industry", "行业"], ["fundamental", "基本面"],
    ]);
  });
});

describe("strategyMeta · FIELD_DESC / FIELD_TYPE 全字段守卫（v6.2 O2，防未来加字段漏说明）", () => {
  // 纪律：config/strategy.yaml **每个顶层 section.field** 必须有 desc + type。
  // 嵌套 dict/list-of-dict（costs/tencent/em/alerts...）标 readonly = 只读展示、本期不做深编辑，
  // 其子键不在守卫范围（与 brief O2「嵌套 dict 保持只读展示」一致）。
  it("strategy.yaml 每个顶层字段都有 FIELD_DESC 说明", () => {
    const cfg = loadStrategyFields();
    const missing = [];
    for (const [section, fields] of Object.entries(cfg)) {
      for (const field of fields) {
        if (!FIELD_DESC[`${section}.${field}`]) missing.push(`${section}.${field}`);
      }
    }
    expect(missing).toEqual([]);
  });

  it("strategy.yaml 每个顶层字段都有 FIELD_TYPE 类型（缺类型会落 str 兜底）", () => {
    const cfg = loadStrategyFields();
    const missing = [];
    for (const [section, fields] of Object.entries(cfg)) {
      for (const field of fields) {
        if (!(field in FIELD_TYPE)) missing.push(`${section}.${field}`);
      }
    }
    expect(missing).toEqual([]);
  });

  it("FIELD_DESC / FIELD_TYPE 覆盖率 = 100%（yaml 顶层字段数 vs desc/type 条数）", () => {
    const cfg = loadStrategyFields();
    let topFields = 0;
    for (const fields of Object.values(cfg)) topFields += fields.length;
    // FIELD_DESC 按 section.field 全限定键索引 → 条数应 == yaml 顶层字段数（无多余/缺失）
    expect(Object.keys(FIELD_DESC).length).toBe(topFields);
    // 抽样：关键新增字段 desc 非空
    expect(FIELD_DESC["dividend.payout_band_pct"]).toBeTruthy();
    expect(FIELD_DESC["universe.soe_required"]).toBeTruthy();
    expect(FIELD_DESC["backtest.weights_ref"]).toBeTruthy();
    expect(FIELD_DESC["reinvest.dps_smooth_years"]).toBeTruthy();
    // 嵌套容器字段（datasource.em / backtest.costs）是顶层字段，desc 非空即可
    expect(FIELD_DESC["datasource.em"]).toBeTruthy();
    expect(FIELD_DESC["backtest.costs"]).toBeTruthy();
  });
});

describe("parseSubWeightsText · 边界（语义同旧版 app.js L913-926）", () => {
  it("合法：'k:0.5,j:0.5' → {k:0.5, j:0.5}", () => {
    expect(parseSubWeightsText("k:0.5,j:0.5")).toEqual({ k: 0.5, j: 0.5 });
  });

  it("合法：整数值/空格容忍 ' a : 1 , b:2 '", () => {
    expect(parseSubWeightsText(" a : 1 , b:2 ")).toEqual({ a: 1, b: 2 });
  });

  it("空串 → null", () => {
    expect(parseSubWeightsText("")).toBeNull();
  });

  it("纯逗号/空白项 → null（无有效项）", () => {
    expect(parseSubWeightsText(", ,")).toBeNull();
    expect(parseSubWeightsText("   ")).toBeNull();
  });

  it("非法：缺冒号 'abc' → null", () => {
    expect(parseSubWeightsText("abc")).toBeNull();
  });

  it("非法：空 key ':0.5'（lastIndexOf(':')=0 → i<=0）→ null", () => {
    expect(parseSubWeightsText(":0.5")).toBeNull();
  });

  it("非法：值非数值 'k:abc' → null", () => {
    expect(parseSubWeightsText("k:abc")).toBeNull();
  });

  it("非法：负值 'k:-1' → null", () => {
    expect(parseSubWeightsText("k:-1")).toBeNull();
  });

  it("混合项任一非法 → 整条 null（旧版 return null 语义）", () => {
    expect(parseSubWeightsText("k:0.5,bad")).toBeNull();
  });

  it("key 含冒号取 lastIndexOf：'a:b:0.3' → {'a:b': 0.3}", () => {
    expect(parseSubWeightsText("a:b:0.3")).toEqual({ "a:b": 0.3 });
  });
});

describe("displayValue · 按 FIELD_TYPE 分支（移植 app.js L818-830）", () => {
  it("list → join(', ')；非数组原样字符串化", () => {
    expect(displayValue("universe", "a_share_prefixes", ["sh.60", "sz.00"])).toBe("sh.60, sz.00");
    expect(displayValue("x", "a_share_prefixes", "oops")).toBe("oops");
  });

  it("bool → 'true'/'false'", () => {
    expect(displayValue("hard_filter", "st_enabled", true)).toBe("true");
    expect(displayValue("hard_filter", "st_enabled", false)).toBe("false");
  });

  it("weights_dict → 按 WEIGHT_DIMS 顺序过滤存在的 key（'标签 值 / …'）", () => {
    const v = { technical: 0.3, dividend: 0.2, industry: 0.1, fundamental: 0.4 };
    expect(displayValue("scoring", "weights", v)).toBe("技术面 0.3 / 股息 0.2 / 行业 0.1 / 基本面 0.4");
    // 缺 key → 过滤（旧版 filter(k in v)）
    expect(displayValue("scoring", "weights", { technical: 1 })).toBe("技术面 1");
  });

  it("sub_weights_dict → 'dim{k:v,k2:v2}  dim2{...}'（双空格连接）", () => {
    const v = { technical: { ma_pos: 0.5, ret: 0.5 }, dividend: { yield: 1 } };
    expect(displayValue("scoring", "sub_weights", v)).toBe("technical{ma_pos:0.5,ret:0.5}  dividend{yield:1}");
  });

  it("默认分支 → String(v)", () => {
    expect(displayValue("data", "cache_dir", "cache/")).toBe("cache/");
    expect(displayValue("scoring", "top_n", 50)).toBe("50");
  });

  it("readonly 类型（嵌套 dict）→ 紧凑 JSON 摘要，不再是 [object Object]（v6.2 O2）", () => {
    const v = displayValue("backtest", "costs", { commission_bp: 2.5, min_commission_cny: 5 });
    expect(v).not.toContain("[object Object]");
    expect(v).toBe('{"commission_bp":2.5,"min_commission_cny":5}');
  });

  it("readonly 类型（list-of-dict）→ 紧凑 JSON 摘要", () => {
    const v = displayValue("backtest", "suspension", { max_defer_days: 5, on_timeout: "drop_to_cash" });
    expect(v).toBe('{"max_defer_days":5,"on_timeout":"drop_to_cash"}');
  });
});

describe("displayValue · [object Object] 修复（v6.1 D3，Joel 拍板）", () => {
  // 根因：dict/list 值落到 FIELD_TYPE 未注册字段 → 旧兜底 String(v) = "[object Object]"
  it("dict 值（未注册字段）→ 紧凑 JSON 摘要，不再是 [object Object]", () => {
    const v = displayValue("dividend", "payout_band_pct", { min: 30, max: 80 });
    expect(v).not.toContain("[object Object]");
    expect(v).toBe('{"min":30,"max":80}');
  });

  it("list 值（未注册字段）→ join(', ')，不再是 [object Object]", () => {
    const v = displayValue("backtest", "benchmarks", ["sh.000300", "sh.000905"]);
    expect(v).not.toContain("[object Object]");
    expect(v).toBe("[sh.000300, sh.000905]");   // 字符串/数字元素原样（join ", " 语义）
  });

  it("list >50 项 → 前 50 + '…共N项' 截断", () => {
    const items = Array.from({ length: 60 }, (_, i) => `s${i}`);
    const v = displayValue("x", "y", items);
    expect(v).toContain("s49");
    expect(v).not.toContain("s50,");
    expect(v).toContain("…共60项");
  });

  it("list 含对象元素 → 对象元素 JSON.stringify（防 [object Object]）", () => {
    const v = displayValue("x", "y", ["a", { b: 1 }]);
    expect(v).toBe('[a, {"b":1}]');
  });

  it("深嵌套 dict → 深度 >2 层截断为 '…'", () => {
    const v = displayValue("x", "y", { a: { b: { c: 1 } }, d: [1, { e: 2 }] });
    expect(v).not.toContain("[object Object]");
    // 第 1 层 key、第 2 层值展开；第 3 层（b.c / e）→ "…"（裸省略号，非 JSON 字符串）
    expect(v).toBe('{"a":{"b":…},"d":[1,…]}');
  });

  it("空值/边界：null → 'null'、{} → '{}'、[] → '[]'", () => {
    expect(displayValue("x", "y", null)).toBe("null");
    expect(displayValue("x", "y", {})).toBe("{}");
    expect(displayValue("x", "y", [])).toBe("[]");
  });

  it("标量保持 String(v)（int/float/str 行为不变）", () => {
    expect(displayValue("scoring", "top_n", 50)).toBe("50");
    expect(displayValue("technical", "min_return_pct", 3.5)).toBe("3.5");
    expect(displayValue("data", "cache_dir", "cache/")).toBe("cache/");
  });

  it("已注册类型分支不受影响（list/bool/weights_dict/sub_weights_dict 原语义）", () => {
    expect(displayValue("universe", "a_share_prefixes", ["sh.60", "sz.00"])).toBe("sh.60, sz.00");
    expect(displayValue("hard_filter", "st_enabled", true)).toBe("true");
    expect(displayValue("scoring", "weights", { technical: 1 })).toBe("技术面 1");
  });
});
