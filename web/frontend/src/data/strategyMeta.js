// 策略页元数据 —— SECTION_META / FIELD_DESC / FIELD_TYPE / ENUM_OPTIONS / WEIGHT_DIMS
// 逐字移植 web/static/app.js（L687-769，只读参照基准）。纯数据 + 展示/解析辅助函数。
// 注意：这些是结构性元数据（字段类型、选项、说明文案），不是业务阈值——数值一律来自 /api/strategy。

export const SECTION_META = {
  technical: { title: "技术面", icon: "📉" },
  dividend: { title: "股息率", icon: "💰" },
  industry: { title: "行业排名", icon: "🏭" },
  fundamental: { title: "基本面", icon: "📊" },
  universe: { title: "股票池", icon: "🗂️" },
  scoring: { title: "打分模型（v2）", icon: "🧮" },
  badges: { title: "Badge 阈值（v2 Web）", icon: "🏷️" },
  hard_filter: { title: "硬性剔除（v2）", icon: "🚫" },
  data: { title: "数据获取", icon: "⚙️" },
  crosscheck: { title: "交叉验证", icon: "🔍" },
};

// 字段说明（只描述含义，不含阈值；数值一律来自 /api/strategy）
export const FIELD_DESC = {
  "technical.ma_period": "收盘价须高于 MA{n}（后复权日K），均线周期（交易日）",
  "technical.return_window_days": "区间收益率回看窗口（交易日）",
  "technical.min_return_pct": "近 N 日区间收益率下限（%，含边界）",
  "technical.max_return_pct": "近 N 日区间收益率上限（%，含边界）",
  "technical.max_annual_volatility_pct": "年化波动率上限（%，不含边界）",
  "dividend.window_days": "股息率统计窗口（日历天）：[运行日-N, 运行日] 内已除权分红",
  "dividend.min_yield_pct": "股息率下限（%，含边界）= 窗口分红合计 ÷ 不复权收盘价",
  "industry.rank_by": "组内排名依据（目前仅支持最近披露报告期 ROE：roeAvg）",
  "industry.top_pct": "保留行业内前 N%（按 ROE 降序）",
  "industry.min_group_size": "行业组不足该数量 → 跳过排名约束并在报告注明",
  "fundamental.roe_min_pct": "ROE 下限（%，报告期累计口径、未年化，含边界）",
  "fundamental.net_profit_yoy_field": "净利同比字段：YOYPNI(归母) / YOYNI(净利润)，须 > 0",
  "fundamental.liability_max_pct": "资产负债率上限（%，含边界）",
  "fundamental.gross_margin_min_pct": "毛利率下限（%，不含边界）；金融业该字段为空 → 落缺失名单",
  "fundamental.probe_quarters_back": "「最近披露报告期」从当前季度最多回退探测的季数",
  "universe.a_share_prefixes": "沪深A股代码前缀（排除指数/ETF/B股），逗号分隔如 sh.60,sh.68,sz.00,sz.30",
  "universe.listing_min_trading_days": "上市满 N 个交易日（窗口内K线行数判断）",
  "universe.st_name_keyword": "名称辅助标记（剔除以日K isST=1 为准）",
  "scoring.mode": "打分模式：zscore（截面Z-Score多因子，v2 默认）/ legacy（旧四维AND硬过滤，可回退）",
  "scoring.top_n": "榜单输出前 N 名",
  "scoring.missing_policy": "缺失因子处理：neutral_renorm（z=0+按可用权重归一化）/ neutral（z=0不重归一化）/ drop（维度缺失不参与合成）",
  "scoring.weights": "四维权重（和必须为1；滑块 0–1，实时显示归一化值）",
  "scoring.sub_weights": "维度内子因子权重（每维度和必须为1；格式 key:值,key:值）",
  "badges.industry_top_pct": "行业TopN% badge：industry_roe_rank_pct ≤ 该值",
  "badges.fscore_min": "F-Score badge：piotroski_fscore ≥ 该值",
  "hard_filter.st_enabled": "ST 剔除（日K isST=1）",
  "hard_filter.listing_min_trading_days": "上市未满 N 个交易日剔除（全历史K线行数判断）",
  "data.kline_calendar_days_back": "后复权窗口K线回溯日历天（需覆盖均线/收益窗口交易日+节假日余量）",
  "data.retry_max_attempts": "BaoStock 单次查询失败重试次数（指数退避）",
  "data.cache_dir": "本地缓存目录（相对路径基于项目根）",
  "crosscheck.enabled": "启用腾讯 qt.gtimg.cn 交叉验证（不进主计算路径）",
  "crosscheck.sample_size": "从最终入选股中抽样 N 只验证收盘价",
  "crosscheck.price_tolerance_pct": "收盘价偏差容忍度（%）",
  "crosscheck.batch_size": "腾讯接口单批请求股票数",
};

// 字段类型（结构性元数据，非业务阈值）：int/float/bool/str/list/enum2/weights_dict/sub_weights_dict
export const FIELD_TYPE = {
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

// enum2 选项（按 section.field；缺省回退通用）
export const ENUM_OPTIONS = {
  "industry.rank_by": [["roeAvg", "roeAvg（最近披露报告期 ROE）"]],
  "fundamental.net_profit_yoy_field": [
    ["YOYPNI", "YOYPNI（归母净利同比）"], ["YOYNI", "YOYNI（净利润同比）"]],
  "scoring.mode": [
    ["zscore", "zscore（截面Z-Score多因子，v2 默认）"],
    ["legacy", "legacy（旧四维AND硬过滤，可回退）"]],
  "scoring.missing_policy": [
    ["neutral_renorm", "neutral_renorm（缺失 z=0 + 按可用权重归一化）"],
    ["neutral", "neutral（缺失 z=0，不重归一化）"],
    ["drop", "drop（维度缺失 → 不参与合成）"]],
};

export const WEIGHT_DIMS = [
  ["technical", "技术面"], ["dividend", "股息"],
  ["industry", "行业"], ["fundamental", "基本面"],
];

// ---------------------------------------------------------------------------
// 只读展示值（移植 app.js displayValue：按 FIELD_TYPE 分支格式化）
// ---------------------------------------------------------------------------
export function displayValue(section, field, v) {
  const t = FIELD_TYPE[field];
  if (t === "list") return Array.isArray(v) ? v.join(", ") : String(v);
  if (t === "bool") return v ? "true" : "false";
  if (t === "weights_dict") {
    return WEIGHT_DIMS.filter(([k]) => k in v).map(([k, label]) => `${label} ${v[k]}`).join(" / ");
  }
  if (t === "sub_weights_dict") {
    return Object.entries(v || {}).map(([dim, m]) =>
      dim + "{" + Object.entries(m).map(([k, x]) => `${k}:${x}`).join(",") + "}").join("  ");
  }
  return String(v);
}

// ---------------------------------------------------------------------------
// sub_weights_dict 文本解析（移植 app.js parseSubWeightsText，语义原样）：
// "key:值,key:值" → {key: 数值}。空串/无有效项 → null；
// 任一项缺冒号(i<=0)、空 key、非数值、负值 → null（整条拒绝，调用方报格式错误）。
// ---------------------------------------------------------------------------
export function parseSubWeightsText(txt) {
  const out = {};
  for (const part of String(txt).split(",")) {
    const s = part.trim();
    if (!s) continue;
    const i = s.lastIndexOf(":");
    if (i <= 0) return null;
    const k = s.slice(0, i).trim();
    const v = parseFloat(s.slice(i + 1));
    if (!k || isNaN(v) || v < 0) return null;
    out[k] = v;
  }
  return Object.keys(out).length ? out : null;
}
