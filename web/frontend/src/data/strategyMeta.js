// 策略页元数据 —— v7.0（策略核心替换：深回撤金字塔加仓，多因子已删）。
//
// 只描述**结构**（字段类型/选项/说明文案），不含业务阈值——数值一律来自 /api/strategy。
// 字段说明文案 = 调研报告 ③ 参数说明文档草稿原文（含"首轮 k=0 占档"必附注）。
//
// 页面渲染 13 个字段行：strategy 8（name 为策略标识，隐藏于 HIDDEN_FIELDS）
// + universe 5。accounting 固定 cash（只读，FIELD_TYPE="readonly"）。
//
// FIELD_TYPE 约定（沿用旧版）：**按裸字段名**索引（非 section.field）；
// FieldEditor 用 FIELD_TYPE[field] 取类型，未注册 → 回退 "str"。

export const SECTION_META = {
  strategy: { title: "策略参数（深回撤金字塔加仓）", icon: "📉" },
  universe: { title: "股票池（5 条件全链）", icon: "🗂️" },
};

// 分组（有序）。group.id 用于 localStorage 折叠记忆键。
export const SECTION_GROUPS = [
  { id: "strategy", title: "策略参数", icon: "📉", defaultOpen: true, sections: ["strategy"] },
  { id: "universe", title: "股票池", icon: "🗂️", defaultOpen: true, sections: ["universe"] },
];

// 不渲染的段（lake/datasource = yaml-only，lake-sync 专用，Web 只读不改）。
export const EXCLUDED_SECTIONS = ["lake", "datasource"];

// section 级说明（卡片头显示）。strategy 段附"首轮 k=0 占档"必附注 + Joel 口语映射。
export const SECTION_DESC = {
  strategy:
    "逐股独立状态机：高点回撤触发首仓，之后每再跌一档加仓一档，金额按 growth_ratio 递增，上不封顶、不卖出、无熔断。" +
    "【必附注】首轮初始仓占用 k=0 档 → 首轮第一个实际加仓 = base_add_amount×growth_ratio（11,000 元）；" +
    "再武装后的新轮从 base_add_amount（10,000 元）起。" +
    "（口语映射：初始投入金额→initial_amount；首次回撤加仓金额→首轮首个加仓（派生值）；后续加仓金额→base_add_amount。）",
  universe:
    "全链 5 条件（白名单 ∩ 央国企 ∩ 市值≥200亿 ∩ 上市≥250交易日 ∩ 连续分红），SOE 恒强制不设开关；全离线读 lake 单库。",
};

// ---------------------------------------------------------------------------
// 字段说明（调研 ③ 草稿原文）。
// ---------------------------------------------------------------------------
export const FIELD_DESC = {
  // ---- strategy ----
  "strategy.name": "策略标识（固定 drawdown_martingale，不可改）",
  "strategy.initial_amount":
    "初始投入金额（元）：首仓一次性金额；**每股**仅第一轮首买（k=0 档）使用，此后所有轮次首买用 base_add_amount",
  "strategy.base_add_amount":
    "加仓基准金额（元）：阶梯首档（k=0）金额；第 k 档 = 基准×growth_ratio^k",
  "strategy.first_drawdown_pct":
    "首次回撤比例（%）：hfq 收盘 ≤ 高点×(1−比例/100) 时建仓",
  "strategy.add_step_pct":
    "后续加仓回撤比例（%）：以高点为基准每再跌 比例% 加一档（档线=H×(1−first_drawdown_pct/100−k×比例/100)），跳空可一日穿多档",
  "strategy.growth_ratio":
    "加仓提升比率：第 k 档 = base_add_amount×比率^k（1.1 → 1万/1.1万/1.21万…上不封顶）",
  "strategy.rearm_mode":
    "高点刷新方式：bounce=较本周期低点反弹 rearm_pct 即重新记录高点（新高可低于旧高）并重置档位 k=0；newhigh=仅创新高刷新",
  "strategy.rearm_pct":
    "高点刷新比例（%）：bounce 模式下较低点反弹该比例触发再武装（newhigh 模式不生效）",
  "strategy.accounting": "会计口径固定 cash（原始收盘成交+现金分红单列，不卖出）——只读，不可改",
  // ---- universe（沿用现 84 字段 desc 对应 5 键）----
  "universe.industry_whitelist_csric2":
    "证监会二级行业白名单（代码如 B06/C25/D44）：仅保留白名单内行业；空列表=不过滤",
  "universe.soe_keywords":
    "央国企识别关键词（config 驱动初值）：前十大股东任一 share_nature=国有股 或股东名称含任一关键词 → SOE；SOE 恒强制",
  "universe.min_total_mv_yi": "总市值下限（亿元，lake valuation_daily 最新 date）：低于剔除",
  "universe.min_consecutive_div_years":
    "连续分红年数下限：IPO 满7年→连续≥N个自然年有分红；不满7年→IPO后每个完整年度都有分红，否则剔除",
  "universe.listing_min_trading_days": "上市满 N 个交易日（lake kline_daily 行数判断）",
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
};

// 隐藏字段（策略标识 name：卡片标题已体现，不作为可编辑字段行渲染）。
export const HIDDEN_FIELDS = new Set(["strategy.name"]);

// enum2 选项（按 section.field）。
export const ENUM_OPTIONS = {
  "strategy.rearm_mode": [
    ["bounce", "bounce（较低点反弹 rearm_pct 重记高点，默认）"],
    ["newhigh", "newhigh（仅创新高刷新）"],
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
