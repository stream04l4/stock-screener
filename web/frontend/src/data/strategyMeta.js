// 策略页元数据 —— v7.0（策略核心替换：深回撤金字塔加仓，多因子已删）。
//
// 只描述**结构**（字段类型/选项/说明文案），不含业务阈值——数值一律来自 /api/strategy。
//
// 文案口径（2026-10-23 Joel："无用提示太多，字段名不通俗易懂"）：
// - FIELD_LABELS = 通俗字段名（页面主显示）；原始 yaml 字段名保留为 hover 提示，
//   只给排查用，不占视觉。
// - FIELD_DESC 一律一行口语说明，删掉公式堆砌/内部附注（k=0 占档等**行为差异**保留，
//   因为直接影响首仓后第一个加仓的金额）。
//
// 页面渲染 23 个字段行：strategy 8（name 为策略标识，隐藏于 HIDDEN_FIELDS）
// + universe 10（v7.0.12：基础 5 条件 + 候选池选股门槛 5 键）
// + timing 5（v7.0.13：选股时机 3 条件开关/参数）。
// accounting 固定 cash（只读，FIELD_TYPE="readonly"）。
//
// FIELD_TYPE 约定（沿用旧版）：**按裸字段名**索引（非 section.field）；
// FieldEditor 用 FIELD_TYPE[field] 取类型，未注册 → 回退 "str"。

export const SECTION_META = {
  strategy: { title: "持仓策略（深回撤金字塔加仓）", icon: "📉" },
  universe: { title: "股票池（候选池：基础 5 条件 + 选股 3 门槛）", icon: "🗂️" },
  timing: { title: "选股策略（买入时机筛选）", icon: "🎯" },
};

// 分组（有序）。group.id 用于 localStorage 折叠记忆键。
export const SECTION_GROUPS = [
  { id: "strategy", title: "持仓策略", icon: "📉", defaultOpen: true, sections: ["strategy"] },
  { id: "universe", title: "股票池", icon: "🗂️", defaultOpen: true, sections: ["universe"] },
  { id: "timing", title: "选股策略", icon: "🎯", defaultOpen: true, sections: ["timing"] },
];

// 不渲染的段（lake/datasource = yaml-only，lake-sync 专用，Web 只读不改）。
export const EXCLUDED_SECTIONS = ["lake", "datasource"];

// section 级说明（卡片头显示）：只说"这个策略在干什么"，公式细节下沉到字段行。
export const SECTION_DESC = {
  strategy:
    "每只股票独立运作：从高点跌够「首次建仓回撤」建首仓，之后每再跌「加仓步长」加一档，" +
    "金额按 base_add_amount×growth_ratio 阶梯递增，上不封顶、不卖出、无熔断。" +
    "注意：首轮首仓占 k=0 档，所以首轮第一个加仓 = base_add_amount×growth_ratio；再武装后的新轮从 base_add_amount 起。",
  universe:
    "候选池门槛 = 基础 5 条件（行业白名单、央国企、总市值下限、上市天数、连续分红）" +
    "叠加 3 道选股门槛（十年回测为正、股息率下限、股价区间）；入选 ≠ 买入信号，" +
    "买不买看价格是否到策略触发线（「我的持仓」页信号 / 微信提醒）。",
  timing:
    "在候选池之上筛「何时买」：价格低位（近 N 年分位）+ 高股息率（+ 可选年线之下）三条件。" +
    "下方「今日选股」= 命中任意已启用条件（OR，观察名单）；" +
    "微信提醒 = 价格低位**且**高股息率同时命中且是新触发（每日湖更新后自动检查，只推新进入买区的）。",
};

// ---------------------------------------------------------------------------
// 通俗字段名（页面主显示；原始 yaml 字段名保留为 hover 提示，见 StrategyCards）。
// ---------------------------------------------------------------------------
export const FIELD_LABELS = {
  "strategy.name": "策略标识",
  "strategy.initial_amount": "首仓金额（元）",
  "strategy.base_add_amount": "加仓基准金额（元）",
  "strategy.first_drawdown_pct": "首次建仓回撤（%）",
  "strategy.add_step_pct": "加仓步长（%）",
  "strategy.growth_ratio": "加仓递增比率",
  "strategy.rearm_mode": "高点再武装方式",
  "strategy.rearm_pct": "再武装反弹幅度（%）",
  "strategy.accounting": "成交口径（固定）",
  "universe.industry_whitelist_csric2": "行业白名单（证监会二级代码）",
  "universe.soe_keywords": "央国企识别关键词",
  "universe.min_total_mv_yi": "总市值下限（亿元）",
  "universe.min_consecutive_div_years": "连续分红年数下限",
  "universe.listing_min_trading_days": "最少上市交易日",
  "universe.backtest_positive": "十年回测必须为正",
  "universe.backtest_window_years": "回测窗口（年）",
  "universe.min_ttm_yield_pct": "股息率下限 TTM（%）",
  "universe.price_min_yuan": "股价下限（元）",
  "universe.price_max_yuan": "股价上限（元）",
  // v7.0.13 timing（选股时机）
  "timing.price_window_years": "价格分位窗口（年）",
  "timing.price_pctile_max": "价格低位分位上限（%）",
  "timing.yield_min_pct": "股息率下限 TTM（%）",
  "timing.below_ma250": "叠加「年线之下」条件",
  "timing.ma_period": "年线周期（交易日）",
};

// ---------------------------------------------------------------------------
// 字段说明：一行口语，只讲"填了之后会发生什么"。
// ---------------------------------------------------------------------------
export const FIELD_DESC = {
  // ---- strategy ----
  "strategy.name": "固定标识，不可改。",
  "strategy.initial_amount":
    "首仓一次性投入的金额；它占掉 k=0 档，所以首轮第一个加仓是 基准×比率（不是基准本身）",
  "strategy.base_add_amount":
    "加仓的基准金额：第 k 档 = 基准×比率^k；再武装后的新轮从它开始加",
  "strategy.first_drawdown_pct":
    "股价从高点回落这个比例，就建首仓（按前复权收盘价判断）",
  "strategy.add_step_pct":
    "建首仓后，每再跌这个比例加一档；跳空缺口一天可以连穿多档",
  "strategy.growth_ratio":
    "每档金额乘这个数：1.1 就是 1万 → 1.1万 → 1.21万……上不封顶",
  "strategy.rearm_mode":
    "什么时候把「高点」刷新、档位从头数：反弹重置=低点弹够幅度就刷新（新高可低于旧高）；仅创新高=必须创出新高才刷新",
  "strategy.rearm_pct":
    "较低点反弹这个比例就算「再武装」，之后重新记高点、档位归零；仅「仅创新高」方式下不生效",
  "strategy.accounting": "只读固定：按前复权收盘价成交、现金分红单列、只买不卖",
  // ---- universe ----
  "universe.industry_whitelist_csric2":
    "只保留白名单内的行业（代码如 B06/C25/D44）；留空 = 不按行业过滤",
  "universe.soe_keywords":
    "前十大股东含「国有股」性质、或股东名称含这些关键词 → 判定为央国企（该条件恒强制，无开关）",
  "universe.min_total_mv_yi": "总市值低于这个数（取数据湖最新一日）→ 剔除",
  "universe.min_consecutive_div_years":
    "要连续这么多年的完整年度都有分红：上市满 7 年 → 看最近 N 年；不满 7 年 → 上市以来每个完整年度都要有",
  "universe.listing_min_trading_days": "上市交易天数没满这个数 → 剔除",
  // v7.0.12 候选池选股门槛
  "universe.backtest_positive":
    "开关：十年滚动回测（含分红、扣佣金）净利为正且至少触发过一次策略的股票才有资格进候选池；关掉则跳过这道门槛",
  "universe.backtest_window_years":
    "从最新 K线日往回数 N 年作为回测窗口（滚动，每日刷新）",
  "universe.min_ttm_yield_pct": "最新估值日 TTM 股息率低于这个数 → 剔除",
  "universe.price_min_yuan": "最新收盘价低于这个价 → 剔除（远离退市风险区）",
  "universe.price_max_yuan": "最新收盘价高于这个价 → 剔除（保证加仓档位整手可行）",
  // v7.0.13 timing（选股时机）
  "timing.price_window_years":
    "条件①的「近年」= 从最新 K线日往回数 N 年的前复权收盘价；当前价在其中的分位决定「是否低位」",
  "timing.price_pctile_max":
    "条件①：当前价处于近 N 年价格分位 ≤ 这个值 → 算「价格低位」（如 40 = 比近 5 年 6 成以上交易日都便宜）",
  "timing.yield_min_pct": "条件②：最新 TTM 股息率 ≥ 这个值 → 算「高股息」（分红兜底）",
  "timing.below_ma250":
    "条件③（可选，默认关）：当前价低于年线才算低位；开启后微信提醒需三条件同时满足",
  "timing.ma_period": "年线 = 最近 N 个交易日收盘价均值（默认 250 = 一年线）",
};

// ---------------------------------------------------------------------------
// 字段类型（结构性元数据，非业务阈值）：int/float/enum2/list/readonly。
// readonly = 固定值只读展示（accounting）；改值只能走 yaml 手工编辑。
// ---------------------------------------------------------------------------
export const FIELD_TYPE = {
  // strategy
  name: "str",              // 固定标识（页面不渲染，见 HIDDEN_FIELDS）
  initial_amount: "int",
  base_add_amount: "int",
  first_drawdown_pct: "float",
  add_step_pct: "float",
  growth_ratio: "float",
  rearm_mode: "enum2",
  rearm_pct: "float",
  accounting: "readonly",
  // universe
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
  // v7.0.13 timing（选股时机）
  price_window_years: "int",
  price_pctile_max: "float",
  yield_min_pct: "float",
  below_ma250: "bool",
  ma_period: "int",
};

// 隐藏字段（策略标识 name：卡片标题已体现，不作为可编辑字段行渲染）。
export const HIDDEN_FIELDS = new Set(["strategy.name"]);

// enum2 选项（按 section.field）：选项文案通俗化（2026-10-23）。
export const ENUM_OPTIONS = {
  "strategy.rearm_mode": [
    ["bounce", "反弹重置（较低点弹够幅度即重新记高点，默认）"],
    ["newhigh", "仅创新高（必须创出新高才重新记高点）"],
  ],
};

// ---------------------------------------------------------------------------
// 只读展示值（沿用旧版 displayValue 语义，去掉 weights 分支）：
// list → join(", ")（>50 项截断）；bool → true/false；其余 → readableValue。
// ---------------------------------------------------------------------------
export function displayValue(section, field, v) {
  const t = FIELD_TYPE[field];
  if (t === "list") return Array.isArray(v) ? v.join(", ") : String(v);
  if (t === "bool") return v ? "true" : "false";
  // readonly / str / int / float / enum2 / 未注册 → readableValue（dict/list 可读摘要，标量 String）
  return readableValue(v);
}

// 可读摘要：dict/list → 紧凑可读文本；标量 → String(v)。
export function readableValue(v) {
  if (Array.isArray(v)) {
    if (!v.length) return "[]";
    const head = v.slice(0, 50).map((x) =>
      x !== null && typeof x === "object" ? JSON.stringify(x) : String(x)).join(", ");
    return v.length > 50 ? `[${head}, …共${v.length}项]` : `[${head}]`;
  }
  if (v !== null && typeof v === "object") {
    return compactJson(v, 2);
  }
  return String(v);
}

// 紧凑 JSON（深度 >maxDepth 层 → "…"）。
export function compactJson(v, maxDepth) {
  if (Array.isArray(v)) {
    if (maxDepth <= 0) return "…";
    return "[" + v.map((x) => compactJson(x, maxDepth - 1)).join(",") + "]";
  }
  if (v !== null && typeof v === "object") {
    if (maxDepth <= 0) return "…";
    const keys = Object.keys(v);
    if (!keys.length) return "{}";
    return "{" + keys.map((k) => `${JSON.stringify(k)}:${compactJson(v[k], maxDepth - 1)}`).join(",") + "}";
  }
  return JSON.stringify(v);
}
