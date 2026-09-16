// SURV_COLS —— 通用数据表列定义，逐字移植 web/static/app.js（31 数据列 + Badge 列）。
// [key, label, isNum?]；pass_* / top_n_selected / ma_bullish / macd_golden_cross 有专门 cellText。
export const SURV_COLS = [
  ["code", "代码"], ["name", "名称"], ["industry", "行业"],
  ["close", "收盘", true],
  ["ma_bullish", "MA多头", true], ["window_return_pct", "250日收益%", true],
  ["annual_vol_pct", "年化波动%", true], ["rsi14", "RSI14", true],
  ["macd_golden_cross", "MACD金叉", true],
  ["ttm_dividend_yield_pct", "TTM息率%", true], ["payout_ratio_pct", "支付率%", true],
  ["industry_roe_rank_pct", "行业ROE分位", true], ["industry_yoy_pni_rank_pct", "行业YOY分位", true],
  ["roe_pct", "ROE%", true], ["roe_3y_mean_pct", "ROE3年均值%", true],
  ["liability_pct", "负债率%", true], ["gross_margin_pct", "毛利率%", true],
  ["piotroski_fscore", "F-Score", true],
  ["z_technical", "z技术", true], ["z_dividend", "z股息", true],
  ["z_industry", "z行业", true], ["z_fundamental", "z基本面", true],
  ["score_technical", "分技术", true], ["score_dividend", "分股息", true],
  ["score_industry", "分行业", true], ["score_fundamental", "分基本面", true],
  ["total_score", "综合得分", true], ["rank", "排名", true],
  ["top_n_selected", "入选", true],
  // legacy 兼容列（v1 运行可见；v2 行中为空）
  ["dividend_yield_pct", "息率%(旧)", true], ["yoy_net_profit_pct", "净利同比%", true],
  ["industry_percentile", "行业百分位(旧)", true], ["pass_all", "全过"],
];
