// strategyMeta 单测（brief D2）：元数据完整性 + parseSubWeightsText 边界 + displayValue 分支。
import { describe, it, expect } from "vitest";
import {
  SECTION_META, FIELD_DESC, FIELD_TYPE, ENUM_OPTIONS, WEIGHT_DIMS,
  parseSubWeightsText, displayValue,
} from "../strategyMeta.js";

describe("strategyMeta · 元数据完整性（与旧 app.js L687-769 对齐）", () => {
  it("SECTION_META 覆盖 10 个已知 section，title/icon 齐全", () => {
    const keys = ["technical", "dividend", "industry", "fundamental", "universe",
      "scoring", "badges", "hard_filter", "data", "crosscheck"];
    for (const k of keys) {
      expect(SECTION_META[k]).toBeTruthy();
      expect(typeof SECTION_META[k].title).toBe("string");
      expect(typeof SECTION_META[k].icon).toBe("string");
    }
  });

  it("FIELD_TYPE 覆盖旧版全部字段类型（int/float/bool/str/list/enum2/weights_dict/sub_weights_dict）", () => {
    const expected = {
      ma_period: "int", return_window_days: "int", min_return_pct: "float",
      max_return_pct: "float", max_annual_volatility_pct: "float",
      window_days: "int", min_yield_pct: "float",
      rank_by: "enum2", top_pct: "float", min_group_size: "int",
      roe_min_pct: "float", net_profit_yoy_field: "enum2", liability_max_pct: "float",
      gross_margin_min_pct: "float", probe_quarters_back: "int",
      a_share_prefixes: "list", listing_min_trading_days: "int", st_name_keyword: "str",
      mode: "enum2", top_n: "int", missing_policy: "enum2",
      weights: "weights_dict", sub_weights: "sub_weights_dict",
      industry_top_pct: "float", fscore_min: "int",
      st_enabled: "bool",
      kline_calendar_days_back: "int", retry_max_attempts: "int", cache_dir: "str",
      enabled: "bool", sample_size: "int", price_tolerance_pct: "float", batch_size: "int",
    };
    expect(FIELD_TYPE).toEqual(expected);
  });

  it("ENUM_OPTIONS 三个枚举字段的选项与旧版一致（含 scoring.missing_policy 3 项）", () => {
    expect(ENUM_OPTIONS["industry.rank_by"]).toEqual([["roeAvg", "roeAvg（最近披露报告期 ROE）"]]);
    expect(ENUM_OPTIONS["fundamental.net_profit_yoy_field"].map(([v]) => v)).toEqual(["YOYPNI", "YOYNI"]);
    expect(ENUM_OPTIONS["scoring.mode"].map(([v]) => v)).toEqual(["zscore", "legacy"]);
    expect(ENUM_OPTIONS["scoring.missing_policy"].map(([v]) => v)).toEqual(["neutral_renorm", "neutral", "drop"]);
  });

  it("WEIGHT_DIMS = 四维（key/label 成对）", () => {
    expect(WEIGHT_DIMS).toEqual([
      ["technical", "技术面"], ["dividend", "股息"],
      ["industry", "行业"], ["fundamental", "基本面"],
    ]);
  });

  it("FIELD_DESC 覆盖旧版全部说明条目（数量与抽样文案）", () => {
    expect(Object.keys(FIELD_DESC).length).toBe(34); // 旧版 L700-735 共 34 条
    expect(FIELD_DESC["scoring.weights"]).toBe("四维权重（和必须为1；滑块 0–1，实时显示归一化值）");
    expect(FIELD_DESC["universe.a_share_prefixes"]).toContain("sh.60,sh.68,sz.00,sz.30");
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
    expect(v).toBe("[a, {\"b\":1}]");
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
