// 策略页元数据 —— SECTION_META / SECTION_GROUPS / SECTION_DESC / FIELD_DESC / FIELD_TYPE / ENUM_OPTIONS / WEIGHT_DIMS
// v6.2（TL brief O1/O2）：
//   - SECTION_META 补齐全部 16 段（新增 backtest/reinvest/datasource/health/canonical/lake）；
//   - 新增 SECTION_GROUPS（三段分组，有序）+ SECTION_DESC（每段一行"这组管什么"，16 段全覆盖）；
//   - FIELD_DESC 补全至 strategy.yaml **全部顶层字段**（84 条），每条读 screener/backtest/lake
//     实际消费代码核实语义后撰写（含单位/边界：日历天 vs 交易日、含不含边界、只读嵌套 dict 标注）；
//   - FIELD_TYPE 同步补全缺失字段类型（新增 "readonly" 类型 = 嵌套 dict/list-of-dict，编辑态只读展示）。
// 注意：这些是结构性元数据（字段类型、选项、说明文案），不是业务阈值——数值一律来自 /api/strategy。
//
// FIELD_TYPE 约定（沿用旧版）：**按裸字段名**索引（非 section.field），FieldEditor 用 FIELD_TYPE[field] 取类型；
// 跨段同名字段（enabled/sample_size/top_n/listing_min_trading_days）类型一致，无冲突。

export const SECTION_META = {
  technical: { title: "技术面", icon: "📉" },
  dividend: { title: "股息率", icon: "💰" },
  industry: { title: "行业排名", icon: "🏭" },
  fundamental: { title: "基本面", icon: "📊" },
  universe: { title: "股票池", icon: "🗂️" },
  scoring: { title: "打分模型（v2）", icon: "🧮" },
  badges: { title: "Badge 阈值（v2 Web）", icon: "🏷️" },
  hard_filter: { title: "硬性剔除（v2）", icon: "🚫" },
  backtest: { title: "历史回测", icon: "🧪" },
  reinvest: { title: "再投资参考", icon: "💵" },
  datasource: { title: "数据源架构", icon: "🔌" },
  data: { title: "数据获取", icon: "⚙️" },
  crosscheck: { title: "交叉验证", icon: "🔍" },
  health: { title: "数据质量校验", icon: "🩺" },
  canonical: { title: "Canonical 派生层", icon: "🗄️" },
  lake: { title: "数据湖（v6）", icon: "🌊" },
};

// ---------------------------------------------------------------------------
// O1 — 分组（有序）。group.id 用于 localStorage 折叠记忆键；defaultOpen 为首次访问默认态。
// v6.2.1 S2（页面归属调整）：
//   - backtest/reinvest 两段移出 StrategyTab → BacktestTab（"回测参数"编辑区，复用
//     StrategyCards/FieldEditor + "load full json → 编辑子集 → PUT full" 保存模式）；
//   - lake 段移除（TL 拍板：数据湖页不新增设置表单，高级键保持 yaml-only、很少改）；
//   - 本表 = StrategyTab 渲染的分组：📌策略核心 8 段（默认展开）+ ⚙️高级配置 5 段
//     （screener 基础设施、很少调 → 默认折叠，仍可编辑）。
//   编辑态强制全部展开（防漏改），由 StrategyCards 的 editing prop 驱动，不读这里的 defaultOpen。
// ---------------------------------------------------------------------------
export const SECTION_GROUPS = [
  {
    id: "core", title: "策略核心", icon: "📌", defaultOpen: true,
    sections: ["technical", "dividend", "industry", "fundamental", "universe", "scoring", "badges", "hard_filter"],
  },
  {
    id: "advanced", title: "高级配置", icon: "⚙️", defaultOpen: false,
    sections: ["datasource", "data", "crosscheck", "health", "canonical"],
  },
];

// v6.2.1 S2：BacktestTab"回测参数"区渲染的分组（backtest + reinvest 两段，默认展开——
// 该页签的主内容就是回测；StrategyCards 按传入 groups 渲染，组件本身不区分页面）。
export const BACKTEST_GROUPS = [
  {
    id: "bt_params", title: "回测参数", icon: "🧪", defaultOpen: true,
    sections: ["backtest", "reinvest"],
  },
];

// O1 — section 级说明（每段一行"这组管什么"，16 段全覆盖；显示在每张卡片头）。
export const SECTION_DESC = {
  technical: "技术面筛选：均线位置、区间收益、波动率上限",
  dividend: "股息率与分红质量：TTM 股息率下限 + 支付率软约束",
  industry: "行业内 ROE 排名：保留组内前 N%",
  fundamental: "基本面门槛：ROE / 净利同比 / 负债率 / 毛利率",
  universe: "股票池范围：A股前缀、上市天数、央国企/行业白名单/市值硬过滤",
  scoring: "打分模型：四维 Z-Score 权重与缺失因子策略",
  badges: "Web 主表格 badge 阈值（高股息 / 行业TopN% / F-Score）",
  hard_filter: "硬性剔除开关：ST / 上市天数 / 连续分红年数",
  backtest: "历史回测参数：窗口、调仓频率、成交口径、成本与停牌规则",
  reinvest: "再投资参考输出：目标股息率参考价 + DPS 平滑/CAGR",
  datasource: "数据源架构：腾讯/BaoStock/东财/新浪/10Y国债 各客户端限速与降级",
  data: "K线数据获取：回溯窗口、重试、缓存目录",
  crosscheck: "腾讯收盘价交叉验证（不进主计算路径）",
  health: "数据质量交叉校验阈值 + EmptyPayloadGuard + 健康度告警",
  canonical: "raw/canonical 统一 Schema 派生层开关与溯源",
  lake: "v6 数据湖（DuckDB 独立分析层）BaoStock 日预算",
};

// ---------------------------------------------------------------------------
// O2 — 字段说明（只描述含义，不含阈值；数值一律来自 /api/strategy）。
// 覆盖 strategy.yaml **全部顶层字段**（84 条），按 section.field 索引。
// 纪律：每条读 screener/backtest/lake 实际消费代码核实语义后撰写，不猜；含单位/边界语义。
// 嵌套 dict（costs/suspension/tencent/em/...）保持只读展示（readableValue），本期不做深编辑——
// 说明里标注"只读展示"并在 FIELD_TYPE 标 "readonly"。
// ---------------------------------------------------------------------------
export const FIELD_DESC = {
  // ---- technical（技术面）----
  "technical.ma_period": "收盘价须高于 MA{n}（后复权日K），均线周期（交易日）",
  "technical.return_window_days": "区间收益率回看窗口（交易日）",
  "technical.min_return_pct": "近 N 日区间收益率下限（%，含边界）",
  "technical.max_return_pct": "近 N 日区间收益率上限（%，含边界）",
  "technical.max_annual_volatility_pct": "年化波动率上限（%，不含边界）",
  // ---- dividend（股息率）----
  "dividend.window_days": "股息率统计窗口（日历天）：[运行日-N, 运行日] 内已除权分红",
  "dividend.min_yield_pct": "股息率下限（%，含边界）= 窗口分红合计 ÷ 不复权收盘价",
  "dividend.payout_band_pct": "支付率软约束区间 {min,max}（%）：区间内原值打分；>max 按 hi−(p−hi)×decay、<min 按 p×decay 降分但不剔除（只读展示，本期不做深编辑）",
  "dividend.payout_out_of_band_decay": "支付率区间外衰减系数（0–1）：>max 时 hi−(p−hi)×decay、<min 时 p×decay；config 驱动、禁硬编码",
  // ---- industry（行业排名）----
  "industry.rank_by": "组内排名依据（目前仅支持最近披露报告期 ROE：roeAvg）",
  "industry.top_pct": "保留行业内前 N%（按 ROE 降序）",
  "industry.min_group_size": "行业组不足该数量 → 跳过排名约束并在报告注明",
  // ---- fundamental（基本面）----
  "fundamental.roe_min_pct": "ROE 下限（%，报告期累计口径、未年化，含边界）",
  "fundamental.net_profit_yoy_field": "净利同比字段：YOYPNI(归母) / YOYNI(净利润)，须 > 0",
  "fundamental.liability_max_pct": "资产负债率上限（%，含边界）",
  "fundamental.gross_margin_min_pct": "毛利率下限（%，不含边界）；金融业该字段为空 → 落缺失名单",
  "fundamental.probe_quarters_back": "「最近披露报告期」从当前季度最多回退探测的季数",
  // ---- universe（股票池）----
  "universe.a_share_prefixes": "沪深A股代码前缀（排除指数/ETF/B股），逗号分隔如 sh.60,sh.68,sz.00,sz.30",
  "universe.listing_min_trading_days": "上市满 N 个交易日（窗口内K线行数判断）",
  "universe.st_name_keyword": "名称辅助标记（剔除以日K isST=1 为准）",
  "universe.soe_required": "央国企硬过滤开关：前十大股东任一 share_nature=国有股 或名称命中 soe_keywords → 保留，否则剔除并进复核清单",
  "universe.industry_whitelist_csric2": "证监会二级行业白名单（代码如 B06/C25/D44）：仅保留白名单内行业；空列表=不过滤",
  "universe.min_total_mv_yi": "总市值下限（亿元，腾讯快照 idx45）：低于剔除；null=不启用",
  "universe.soe_keywords": "央国企识别关键词（config 驱动初值）：股东名称含任一关键词 → soe；空=该规则不命中",
  // ---- scoring（打分模型）----
  "scoring.mode": "打分模式：zscore（截面Z-Score多因子，v2 默认）/ legacy（旧四维AND硬过滤，可回退）",
  "scoring.top_n": "榜单输出前 N 名",
  "scoring.missing_policy": "缺失因子处理：neutral_renorm（z=0+按可用权重归一化）/ neutral（z=0不重归一化）/ drop（维度缺失不参与合成）",
  "scoring.normalize": "截面标准化集合声明（当前引擎恒为硬剔除后全体候选做截面 Z-Score；该键为配置占位，暂未驱动行为）",
  "scoring.weights": "四维权重（和必须为1；滑块 0–1，实时显示归一化值）",
  "scoring.sub_weights": "维度内子因子权重（每维度和必须为1；格式 key:值,key:值）",
  // ---- badges（Badge 阈值）----
  "badges.industry_top_pct": "行业TopN% badge：industry_roe_rank_pct ≤ 该值",
  "badges.fscore_min": "F-Score badge：piotroski_fscore ≥ 该值",
  // ---- hard_filter（硬性剔除）----
  "hard_filter.st_enabled": "ST 剔除（日K isST=1）",
  "hard_filter.listing_min_trading_days": "上市未满 N 个交易日剔除（全历史K线行数判断）",
  "hard_filter.min_consecutive_div_years": "连续分红年数下限：IPO 满7年→连续≥N个自然年有分红；不满7年→IPO后每个完整年度都有分红，否则剔除",
  // ---- backtest（历史回测）----
  "backtest.start": "回测首调仓月（月末交易日，YYYY-MM-DD）",
  "backtest.end": "回测末调仓月（月末交易日，YYYY-MM-DD）",
  "backtest.rebalance": "调仓频率：monthly（月度）/ quarterly（季度）",
  "backtest.top_n": "等权持仓数（每期持有前 N 名）",
  "backtest.weights_ref": "回测权重引用（单一事实来源，目前仅支持 'scoring.weights'）",
  "backtest.execution": "成交口径：t1_open（T+1开盘，open缺失自动close兜底、PIT安全）/ t1_close / t_close",
  "backtest.costs": "交易成本参数 {commission_bp/min_commission_cny/initial_capital_cny/stamp_tax_sell/transfer_fee_bp/slippage_bp/delisting_haircut_pct}（只读展示，本期不做深编辑）",
  "backtest.suspension": "停牌规则 {max_defer_days/on_timeout}：逐日重试顺延，超期按 on_timeout 处置 drop_to_cash/hold（只读展示）",
  "backtest.benchmarks": "基准指数代码列表（另自动加自建全市场等权；只读展示，本期不做深编辑）",
  "backtest.risk_free_pct": "夏普/alpha 无风险利率（年化%）",
  // ---- data（数据获取）----
  "data.kline_calendar_days_back": "后复权窗口K线回溯日历天（需覆盖均线/收益窗口交易日+节假日余量）",
  "data.retry_max_attempts": "BaoStock 单次查询失败重试次数（指数退避）",
  "data.cache_dir": "本地缓存目录（相对路径基于项目根）",
  // ---- datasource（数据源架构）----
  "datasource.primary": "主数据源：tencent（批量快照/K线）/ baostock（回退旧行为）",
  "datasource.fallback": "全批失败兜底：fail_fast（推荐，不自动回退 BaoStock）/ baostock",
  "datasource.baostock": "BaoStock 每日配额守卫 {daily_quota}：官网限 50000/日/IP，到顶当日停、次日续（只读展示）",
  "datasource.tencent": "腾讯批量客户端参数 {snapshot_batch_size/snapshot_interval_s/timeout_s/max_attempts/kline_bars/kline_interval_s}（只读展示）",
  "datasource.exdate_detector": "preclose 信号除权检测参数 {preclose_dev_threshold_pct/factor_sanity_cap_pct/max_candidates_per_day/cutover_max_candidates/cutover_max_gap_backfill}（只读展示）",
  "datasource.universe": "股票池/行业 BaoStock 降级 {stale_max_days}：当日缓存 miss 时可用 ≤N 天前陈旧 all_stock 池（只读展示）",
  "datasource.factor_reconcile": "BaoStock 低频因子对账 {weekly_baostock_scan/scan_days_spread/skip_during_disclosure}（本期默认关，只读展示）",
  "datasource.contract": "契约监控 {pct_sample_size/pct_tolerance_pct}：抽样校验 pct 一致性（离线可算、零额外请求；只读展示）",
  "datasource.em": "东财 datacenter-web 客户端 {enabled/page_size/interval_s/cooldown_*/full_table_gap_seconds/cgb10y_sanity_pct}：非官方接口、串行小步防限流（只读展示）",
  "datasource.sina": "新浪非官方接口客户端 {interval_s/timeout_s/max_attempts/consecutive_fail_breaker/cf_reports_num/cooldown_*/waf_backoff_s}：F10股东+财务JSON，限速防WAF（只读展示）",
  "datasource.rf": "10Y国债收益率源（TradingEconomics）{url/fallback_pct/sanity_pct}：解析失败回退+告警不静默（只读展示）",
  // ---- reinvest（再投资参考）----
  "reinvest.target_ttm_yield_pct": "目标 TTM 股息率（%）：参考价 = 近N年平均DPS ÷ (该值/100) + TTM分位展示",
  "reinvest.yield_pctile_lookback_years": "历史股息率分位回看年数",
  "reinvest.dps_smooth_years": "参考价平滑窗口年数：窗口[run_year-N, run_year-1]所有自然年平均DPS（无分红年按0）；<1=单年原行为",
  "reinvest.dps_growth_years": "DPS CAGR 回看年数（新增展示列 dps_cagr_5y_pct）",
  // ---- crosscheck（交叉验证）----
  "crosscheck.enabled": "启用腾讯 qt.gtimg.cn 交叉验证（不进主计算路径）",
  "crosscheck.sample_size": "从最终入选股中抽样 N 只验证收盘价",
  "crosscheck.price_tolerance_pct": "收盘价偏差容忍度（%）",
  "crosscheck.batch_size": "腾讯接口单批请求股票数",
  "crosscheck.ttm_tolerance_pct": "ttm_yield vs 腾讯 idx64 偏差容忍度（百分点）",
  // ---- canonical（Canonical 派生层）----
  "canonical.enabled": "raw/canonical 派生层开关：false → 全 no-op、主路径逐字节不变（回滚点）",
  "canonical.dir": "canonical 输出目录（相对项目根，与 cache/ 平级的独立数据层）",
  "canonical.data_version": "溯源字段：canonical 每条记录的 schema 版本",
  // ---- health（数据质量校验）----
  "health.enabled": "三字段交叉校验开关：false → 跳过 close/dps/roe 校验（raw/canonical 落盘不受影响）",
  "health.sample_size": "从最终入选股抽样 N 只做 close/dps/roe 三字段校验（串行限速）",
  "health.close_tolerance_pct": "收盘价：腾讯 vs 新浪非除权日 |Δ|>此值(%) 告警",
  "health.r_event_tolerance_pct": "除权日改校验 r_event 一致性：|Δ|>此值(%) 告警（不校验 close 绝对值）",
  "health.dps_warn_at": "DPS：em静态 vs akshare-em |Δ|≥此值(元/股) 告警（同源一致性检查）",
  "health.dps_stop_at": "DPS：|Δ|>此值(元/股) 停算标'待复核'（不进计算、不覆盖静态底表）",
  "health.roe_tolerance_pp": "ROE：BaoStock roeAvg vs 新浪 ROEWEIGHTED 换算后 |Δ|>此值(pp) 告警（口径差非错误，勿收紧<0.5pp）",
  "health.market_min_rows": "EmptyPayloadGuard：市场级接口(all_stock)交易日 rows<此值 → 数据源异常",
  "health.akshare_interval_s": "akshare 校验源限速（串行 >=1s；仅校验用途，失败不阻塞）",
  "health.akshare_max_attempts": "akshare 重试 <=N 次（含首次共 <=3 次尝试）",
  "health.akshare_breaker": "akshare 某类接口连续失败 >=N → 该类当日整体降级（不得死磕）",
  "health.alerts": "健康度阈值告警 {rf_fallback_max_per_run/f10_degraded_max/crosscheck_conflicts_max}：全部 yaml 驱动、零硬编码（只读展示）",
  // ---- lake（数据湖 v6）----
  "lake.baostock_daily_budget": "数据湖 BaoStock 日预算上限（次/日，config 可调；到顶当日停、次日续）",
};

// ---------------------------------------------------------------------------
// O2 — 字段类型（结构性元数据，非业务阈值）。
// 按**裸字段名**索引：int/float/bool/str/list/enum2/weights_dict/sub_weights_dict/readonly。
// "readonly" = 嵌套 dict / list-of-dict（costs/suspension/tencent/em/alerts/...）——编辑态只读展示
// （readableValue），本期不做深编辑（brief O2 范围外，已注明）。
// ---------------------------------------------------------------------------
export const FIELD_TYPE = {
  // technical
  ma_period: "int", return_window_days: "int", min_return_pct: "float",
  max_return_pct: "float", max_annual_volatility_pct: "float",
  // dividend
  window_days: "int", min_yield_pct: "float",
  payout_band_pct: "readonly", payout_out_of_band_decay: "float",
  // industry
  rank_by: "enum2", top_pct: "float", min_group_size: "int",
  // fundamental
  roe_min_pct: "float", net_profit_yoy_field: "enum2", liability_max_pct: "float",
  gross_margin_min_pct: "float", probe_quarters_back: "int",
  // universe
  a_share_prefixes: "list", listing_min_trading_days: "int", st_name_keyword: "str",
  soe_required: "bool", industry_whitelist_csric2: "list", min_total_mv_yi: "float",
  soe_keywords: "list",
  // scoring
  mode: "enum2", top_n: "int", missing_policy: "enum2", normalize: "str",
  weights: "weights_dict", sub_weights: "sub_weights_dict",
  // badges
  industry_top_pct: "float", fscore_min: "int",
  // hard_filter
  st_enabled: "bool", min_consecutive_div_years: "int",
  // backtest
  start: "str", end: "str", rebalance: "enum2", weights_ref: "str", execution: "enum2",
  costs: "readonly", suspension: "readonly", benchmarks: "readonly", risk_free_pct: "float",
  // data
  kline_calendar_days_back: "int", retry_max_attempts: "int", cache_dir: "str",
  // datasource
  primary: "enum2", fallback: "enum2",
  baostock: "readonly", tencent: "readonly", exdate_detector: "readonly",
  universe: "readonly", factor_reconcile: "readonly", contract: "readonly",
  em: "readonly", sina: "readonly", rf: "readonly",
  // reinvest
  target_ttm_yield_pct: "float", yield_pctile_lookback_years: "int",
  dps_smooth_years: "int", dps_growth_years: "int",
  // crosscheck
  enabled: "bool", sample_size: "int", price_tolerance_pct: "float", batch_size: "int",
  ttm_tolerance_pct: "float",
  // canonical
  dir: "str", data_version: "str",
  // health
  close_tolerance_pct: "float", r_event_tolerance_pct: "float", dps_warn_at: "float",
  dps_stop_at: "float", roe_tolerance_pp: "float", market_min_rows: "int",
  akshare_interval_s: "float", akshare_max_attempts: "int", akshare_breaker: "int",
  alerts: "readonly",
  // lake
  baostock_daily_budget: "int",
};

// enum2 选项（按 section.field；缺省回退通用）。v6.2 补 backtest.rebalance/execution + datasource.primary/fallback。
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
  "backtest.rebalance": [
    ["monthly", "monthly（月度调仓）"], ["quarterly", "quarterly（季度调仓）"]],
  "backtest.execution": [
    ["t1_open", "t1_open（T+1开盘，open缺失close兜底）"],
    ["t1_close", "t1_close（T+1收盘）"],
    ["t_close", "t_close（当日收盘）"]],
  "datasource.primary": [
    ["tencent", "tencent（批量快照/K线）"], ["baostock", "baostock（回退旧行为）"]],
  "datasource.fallback": [
    ["fail_fast", "fail_fast（推荐，不自动回退 BaoStock）"], ["baostock", "baostock（自动回退）"]],
};

export const WEIGHT_DIMS = [
  ["technical", "技术面"], ["dividend", "股息"],
  ["industry", "行业"], ["fundamental", "基本面"],
];

// ---------------------------------------------------------------------------
// 只读展示值（移植 app.js displayValue：按 FIELD_TYPE 分支格式化）
// v6.1 D3（Joel 拍板）：[object Object] 修复——dict/list 值落到 FIELD_TYPE 未注册
// 字段时，兜底 String(v) 渲染成 "[object Object]"。改为**可读摘要**（与 vanilla
// app.js readableValue/compactJson 逐字节同步）：
// - list → join(", ")；>50 项截断显示前 50 + "…共N项"；
// - dict → {k:v, ...} 紧凑 JSON（JSON.stringify，深度 >2 层截断为 "…"）；
// - 标量保持 String(v) 不变。
// v6.2：readonly 类型走 readableValue（与未注册字段同路径），嵌套 dict 不再 [object Object]。
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
  // readonly / str / int / float / enum2 / 未注册 → readableValue（dict/list 可读摘要，标量 String）
  return readableValue(v);
}

// 可读摘要（v6.1 D3）：dict/list → 紧凑可读文本；标量 → String(v)。
export function readableValue(v) {
  if (Array.isArray(v)) {
    if (!v.length) return "[]";
    // 元素：字符串/数字原样（join(", ") 语义）；对象/数组 → JSON.stringify（防 [object Object]）
    const head = v.slice(0, 50).map((x) =>
      x !== null && typeof x === "object" ? JSON.stringify(x) : String(x)).join(", ");
    return v.length > 50 ? `[${head}, …共${v.length}项]` : `[${head}]`;
  }
  if (v !== null && typeof v === "object") {
    return compactJson(v, 2);   // compactJson: "{}" / "{k:v,…}" / "…"（自带头尾括号）
  }
  return String(v);
}

// 紧凑 JSON（深度 >maxDepth 层 → "…"；JSON.stringify 语义：字符串带引号、null/数字原样）。
export function compactJson(v, maxDepth) {
  if (Array.isArray(v)) {
    if (maxDepth <= 0) return "…";
    const parts = v.map((x) => compactJson(x, maxDepth - 1));
    return "[" + parts.join(",") + "]";
  }
  if (v !== null && typeof v === "object") {
    if (maxDepth <= 0) return "…";
    const keys = Object.keys(v);
    if (!keys.length) return "{}";
    const parts = keys.map((k) => `${JSON.stringify(k)}:${compactJson(v[k], maxDepth - 1)}`);
    return "{" + parts.join(",") + "}";   // 自带头尾括号（空对象上面已返回 "{}"）
  }
  return JSON.stringify(v);
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
