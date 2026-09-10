# -*- coding: utf-8 -*-
"""指标计算（纯函数层，不联网、可单测）。

口径说明（与调研报告/TL 拍板一致）：
- 技术面基于**后复权**日K（af=1）：MA200、区间收益率、年化波动率。
- 股息率 = 窗口内已除权每股税前现金分红之和 ÷ 当前价（BaoStock 不复权 close af=3）。
  分红数据求和前必须按 (code, dividOperateDate) 去重——同一除权日可能同时存在
  "预案记录"与"正式记录"两行，不去重股息率翻倍（调研报告 §1 高危坑）。
- 基本面字段（roeAvg/gpMargin/liabilityToAsset/YOYNI/YOYPNI）在 BaoStock 里是
  **小数**（0.10 = 10%），本层原样透传；阈值比较在 screener 层用小数做。
  展示 ×100 由 report 层负责。缺失值统一为 None（BaoStock 空串 '' → None）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# 技术面
# ---------------------------------------------------------------------------

TRADING_DAYS_PER_YEAR = 250  # A股年化波动率的年化因子（行业惯例）


@dataclass
class TechnicalResult:
    code: str
    ma: Optional[float] = None          # MA{n}（后复权收盘价均值）
    close_last: Optional[float] = None  # 窗口最后一根K线收盘价（后复权）
    above_ma: Optional[bool] = None     # close > MA？数据不足 → None
    window_return: Optional[float] = None   # 近 N 日区间收益率（小数，0.1=10%）
    annual_volatility: Optional[float] = None  # 年化波动率（小数）
    n_trading_days: int = 0             # 窗口内K线行数（上市时长代理）
    last_date: str = ""
    fail_reasons: List[str] = field(default_factory=list)


def compute_technical(
    code: str,
    dates: List[str],
    closes: List[float],
    ma_period: int,
    return_window_days: int,
    min_return: float,
    max_return: float,
    max_vol: float,
) -> TechnicalResult:
    """计算单只股票的技术面指标并判定通过与否。

    规则：
    - 上市时长：窗口内K线行数 >= ma_period（保证 MA 可算；"上市满250个交易日"
      由窗口长度+该检查共同保证，见 screener._fetch_klines）。
    - 收盘价 > MA{ma_period}。
    - 近 return_window_days 日区间收益率 ∈ [min_return, max_return]（小数）。
      收益 = close[-1] / close[-(N+1)] - 1，即 N 个交易日前的收盘到最后一根。
    - 年化波动率 < max_vol：日收益率（ln 差分）标准差 × sqrt(250)。

    数据不足时相应指标为 None、fail_reasons 记录原因（不抛异常）。
    """
    res = TechnicalResult(code=code, n_trading_days=len(closes))
    if dates:
        res.last_date = dates[-1]
    if closes:
        res.close_last = closes[-1]

    if len(closes) < ma_period:
        res.fail_reasons.append(f"K线不足{ma_period}根(仅{len(closes)})")
        return res

    # --- MA ---
    ma = sum(closes[-ma_period:]) / ma_period
    res.ma = ma
    res.above_ma = closes[-1] > ma
    if not res.above_ma:
        res.fail_reasons.append(f"收盘价{closes[-1]:.3f}≤MA{ma_period}({ma:.3f})")

    # --- 区间收益率 ---
    n = return_window_days
    if len(closes) >= n + 1:
        base = closes[-(n + 1)]
        if base > 0:
            ret = closes[-1] / base - 1.0
            res.window_return = ret
            if not (min_return <= ret <= max_return):
                res.fail_reasons.append(
                    f"近{n}日收益{ret*100:.1f}%∉[{min_return*100:.0f}%,{max_return*100:.0f}%]"
                )
    else:
        res.fail_reasons.append(f"K线不足{n+1}根，无法算近{n}日收益")

    # --- 年化波动率 ---
    if len(closes) >= ma_period + 1:
        rets = [
            math.log(closes[i] / closes[i - 1])
            for i in range(len(closes) - ma_period, len(closes))
            if closes[i - 1] > 0 and closes[i] > 0
        ]
        if len(rets) >= 2:
            mean = sum(rets) / len(rets)
            var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
            vol = math.sqrt(var) * math.sqrt(TRADING_DAYS_PER_YEAR)
            res.annual_volatility = vol
            if not (vol < max_vol):
                res.fail_reasons.append(f"年化波动率{vol*100:.1f}%≥{max_vol*100:.0f}%")
    else:
        res.fail_reasons.append("K线不足，无法算年化波动率")

    return res


# ---------------------------------------------------------------------------
# 股息率
# ---------------------------------------------------------------------------

@dataclass
class DividendResult:
    code: str
    has_dividend: bool = False          # 窗口内是否有已除权分红记录
    dividends_in_window: List[Dict[str, Any]] = field(default_factory=list)
    cash_per_share: Optional[float] = None   # 窗口内每股税前现金分红之和（元/股）
    current_price: Optional[float] = None
    yield_pct: Optional[float] = None   # 股息率（小数）
    fail_reasons: List[str] = field(default_factory=list)


def compute_dividend_yield(
    code: str,
    dividend_records: List[Dict[str, Any]],
    window_start: date,
    run_day: date,
    current_price: Optional[float],
    min_yield: float,
) -> DividendResult:
    """计算股息率并判定。

    步骤（调研报告 §1 明确口径）：
    1. 筛选 ``window_start <= dividOperateDate <= run_day`` —— 这一条同时实现
       "已实施/已除权"（dividOperateDate 非空且≤运行日）与"除权日在窗口内"。
    2. 按 (code, dividOperateDate) 去重（同一除权日的预案+正式两行只算一次）。
    3. 求和 ``dividCashPsBeforeTax``（已是元/股，**不要再除10**）。
    4. 股息率 = 每股分红和 / 当前价；无记录 → 不通过（不是报错）。
    """
    res = DividendResult(code=code, current_price=current_price)

    in_window: List[Dict[str, Any]] = []
    for d in dividend_records:
        op_date_s = (d.get("dividOperateDate") or "").strip()
        if not op_date_s:
            continue  # 无除权日 = 未实施（仅预案/股东大会通过）
        try:
            op_date = date.fromisoformat(op_date_s)
        except ValueError:
            continue
        if window_start <= op_date <= run_day:
            in_window.append(d)

    # 去重：一个除权日 = 一次事件（预案记录与正式记录可能并存）
    seen = set()
    deduped: List[Dict[str, Any]] = []
    for d in sorted(in_window, key=lambda x: x["dividOperateDate"]):
        key = (d.get("code"), d["dividOperateDate"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(d)

    res.dividends_in_window = deduped
    if not deduped:
        res.fail_reasons.append("窗口内无已除权分红记录")
        return res

    res.has_dividend = True
    cash_sum = 0.0
    for d in deduped:
        cash = d.get("dividCashPsBeforeTax")
        if cash is None:
            continue
        cash_sum += cash
    res.cash_per_share = cash_sum

    if current_price is None or current_price <= 0:
        res.fail_reasons.append("当前价缺失，无法计算股息率")
        return res

    res.yield_pct = cash_sum / current_price
    if not (res.yield_pct >= min_yield):
        res.fail_reasons.append(f"股息率{res.yield_pct*100:.2f}%<{min_yield*100:.1f}%")
    return res


# ---------------------------------------------------------------------------
# 基本面
# ---------------------------------------------------------------------------

@dataclass
class FundamentalResult:
    code: str
    period: Optional[str] = None        # "2026Q2"（最近披露报告期）
    pub_date: str = ""                  # 财报发布日
    roe_avg: Optional[float] = None     # 小数
    yoy_net_profit: Optional[float] = None  # 小数（字段名可配 YOYPNI/YOYNI）
    liability_to_asset: Optional[float] = None  # 小数
    gross_margin: Optional[float] = None        # 小数
    missing: List[str] = field(default_factory=list)   # 缺失的指标名
    fail_reasons: List[str] = field(default_factory=list)


def compute_fundamental(
    code: str,
    profit: Optional[Dict[str, Any]],
    growth: Optional[Dict[str, Any]],
    balance: Optional[Dict[str, Any]],
    period: Optional[str],
    roe_min: float,
    yoy_field: str,
    liability_max: float,
    gross_margin_min: float,
) -> FundamentalResult:
    """基本面四维判定（阈值均为小数口径）。

    任一指标缺失 → 记入 missing 且该维度不通过（brief §3.5，报告单列名单）。
    注意：金融业 gpMargin 为空属正常现象（无毛利概念），同样落"数据缺失"。
    """
    res = FundamentalResult(code=code, period=period)
    if profit:
        res.pub_date = profit.get("pubDate") or ""

    checks = [
        ("roeAvg", (profit or {}).get("roeAvg"), roe_min, ">=（ROE下限）"),
        (yoy_field, (growth or {}).get(yoy_field), 0.0, ">（净利同比须为正）"),
        ("liabilityToAsset", (balance or {}).get("liabilityToAsset"), liability_max, "<=（负债率上限）"),
        ("gpMargin", (profit or {}).get("gpMargin"), gross_margin_min, ">（毛利率下限）"),
    ]

    for name, value, threshold, rule in checks:
        if value is None:
            res.missing.append(name)
            continue
        setattr(res, _attr_for(name), value)
        # 按语义比较：ROE/同比/毛利率是"下限"，负债率是"上限"
        if name == "liabilityToAsset":
            ok = value <= threshold
            if not ok:
                res.fail_reasons.append(f"{name}={value*100:.2f}%>{threshold*100:.0f}%")
        elif name in ("roeAvg", yoy_field, "gpMargin"):
            op = ">" if name in (yoy_field, "gpMargin") else ">="
            ok = value > threshold if op == ">" else value >= threshold
            if not ok:
                res.fail_reasons.append(f"{name}={value*100:.2f}% 不满足 {op}{threshold*100:.0f}%")

    return res


def _attr_for(field_name: str) -> str:
    """字段名 → FundamentalResult 属性名。"""
    return {
        "roeAvg": "roe_avg",
        "YOYPNI": "yoy_net_profit",
        "YOYNI": "yoy_net_profit",
        "liabilityToAsset": "liability_to_asset",
        "gpMargin": "gross_margin",
    }[field_name]


# ---------------------------------------------------------------------------
# 行业排名
# ---------------------------------------------------------------------------

@dataclass
class IndustryResult:
    code: str
    industry: str = ""                  # 证监会行业（如 J66货币金融服务）
    group_size: int = 0                 # 行业内候选股票数（含自身）
    rank: Optional[int] = None          # 组内名次（1=最好），无排名时为 None
    percentile: Optional[float] = None  # 百分位（0-100，越小越好；None=未排名）
    group_skipped: bool = False         # 组不足 min_group_size → 跳过排名约束
    rank_by: str = "roeAvg"


def build_industry_groups(industry_map: Dict[str, str]) -> Dict[str, List[str]]:
    """按行业分组（空行业统一为 "无行业"）。返回 {行业名: [codes...]}。"""
    groups: Dict[str, List[str]] = {}
    for code, ind in industry_map.items():
        key = (ind or "").strip() or "无行业"
        groups.setdefault(key, []).append(code)
    return groups


def compute_industry_rank(
    code: str,
    group_codes: List[str],
    roe_map: Dict[str, Optional[float]],
    top_pct: float,
    min_group_size: int,
    industry: str = "",
) -> IndustryResult:
    """行业排名判定（组内按 ROE 百分位保留前 top_pct）。

    :param group_codes: 该股票所在行业的**全部候选**代码列表（由 screener 层用
        build_industry_groups 预计算，避免 O(N²)）。
    - industry 为空 → 归入 "无行业" 组，不崩溃（调研报告 §2）。
    - 组内不足 min_group_size 只 → 跳过排名约束（group_skipped=True），报告注明。
    - ROE 缺失的股票排在组内最后。
    - percentile = rank/size*100（越小越好）；pass 条件 percentile <= top_pct*100。
    """
    res = IndustryResult(code=code, industry=industry or "无行业")
    res.group_size = len(group_codes)

    if len(group_codes) < min_group_size:
        res.group_skipped = True
        return res

    # 组内按 ROE 降序排名；ROE 缺失排最后（稳定排序，code 升序兜底）
    def sort_key(c: str):
        roe = roe_map.get(c)
        return (roe is None, -(roe or 0.0), c)

    ordered = sorted(group_codes, key=sort_key)
    rank_of = {c: i + 1 for i, c in enumerate(ordered)}
    res.rank = rank_of[code]
    res.percentile = round(rank_of[code] / len(group_codes) * 100.0, 2)
    return res


def industry_pass(res: IndustryResult, top_pct: float) -> bool:
    """行业维度是否通过：跳过组视为通过；否则要求 percentile <= top_pct*100。"""
    if res.group_skipped:
        return True
    if res.percentile is None:
        return False
    return res.percentile <= top_pct * 100.0 + 1e-9


# ---------------------------------------------------------------------------
# v2 新因子（纯函数，离线可测；调研报告 R3 口径）
# ---------------------------------------------------------------------------
# 说明：RSI(14)/MACD(12,26,9) 的周期是指标**定义**的一部分（同 TRADING_DAYS_PER_YEAR），
# 不是策略阈值 → 以命名常量给出，不参与 pass/fail 判定。

RSI_PERIOD = 14          # RSI Wilder 平滑周期（指标定义）
MACD_FAST = 12           # MACD EMA 快线（指标定义）
MACD_SLOW = 26           # MACD EMA 慢线（指标定义）
MACD_SIGNAL = 9          # MACD DEA 信号线（指标定义）
MACD_CROSS_LOOKBACK = 5  # "金叉"判定回看根数（近 N 根内 DIF 上穿 DEA）


def rsi_wilder(closes: Sequence[float], period: int = RSI_PERIOD) -> Optional[float]:
    """Wilder RSI：RSI = 100 - 100/(1+avgGain/avgLoss)，首值用简单均值、其后 Wilder 平滑。

    < period+1 根 → None（数据不足）。全涨（avgLoss=0）→ 100.0；全跌 → 0.0。
    """
    n = len(closes)
    if n < period + 1:
        return None
    gains: List[float] = []
    losses: List[float] = []
    for i in range(1, n):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def ema_series(values: Sequence[float], span: int) -> List[float]:
    """指数移动平均（首值=首个数据点，k=2/(span+1)）。"""
    k = 2.0 / (span + 1)
    out: List[float] = []
    e = values[0]
    for v in values:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def compute_macd(
    closes: Sequence[float],
    fast: int = MACD_FAST,
    slow: int = MACD_SLOW,
    signal: int = MACD_SIGNAL,
) -> Optional[Dict[str, List[float]]]:
    """MACD：DIF=EMA(fast)-EMA(slow)，DEA=EMA(DIF,signal)，bar=(DIF-DEA)×2（A股惯例）。

    < slow+signal 根 → None。
    """
    if len(closes) < slow + signal:
        return None
    e_fast = ema_series(list(closes), fast)
    e_slow = ema_series(list(closes), slow)
    dif = [a - b for a, b in zip(e_fast, e_slow)]
    dea = ema_series(dif, signal)
    bar = [(a - b) * 2.0 for a, b in zip(dif, dea)]
    return {"dif": dif, "dea": dea, "bar": bar}


def macd_golden_cross(closes: Sequence[float], lookback: int = MACD_CROSS_LOOKBACK) -> Optional[bool]:
    """近 lookback 根内 DIF 上穿 DEA（金叉）→ True/False；数据不足 → None。"""
    m = compute_macd(closes)
    if m is None:
        return None
    dif, dea = m["dif"], m["dea"]
    lo = max(1, len(dif) - lookback)
    for i in range(lo, len(dif)):
        if dif[i] > dea[i] and dif[i - 1] <= dea[i - 1]:
            return True
    return False


def dedup_dividends(
    dividend_records: Sequence[Dict[str, Any]],
    window_start: date,
    run_day: date,
) -> Tuple[float, List[str]]:
    """窗口内已除权分红：按 (code, dividOperateDate) 去重后求和。

    返回 (每股税前现金分红之和, 除权日列表升序)。与 compute_dividend_yield 同口径，
    但**不含** pass/fail 语义（v2 打分因子用）。
    """
    in_window: List[Dict[str, Any]] = []
    for d in dividend_records:
        op_s = (d.get("dividOperateDate") or "").strip()
        if not op_s:
            continue
        try:
            op = date.fromisoformat(op_s)
        except ValueError:
            continue
        if window_start <= op <= run_day:
            in_window.append(d)
    seen = set()
    cash_sum = 0.0
    ex_dates: List[str] = []
    for d in sorted(in_window, key=lambda x: x["dividOperateDate"]):
        key = (d.get("code"), d["dividOperateDate"])
        if key in seen:
            continue
        seen.add(key)
        ex_dates.append(d["dividOperateDate"])
        cash = d.get("dividCashPsBeforeTax")
        if cash is not None:
            cash_sum += cash
    return cash_sum, ex_dates


def ttm_dividend_yield(
    dividend_records: Sequence[Dict[str, Any]],
    window_start: date,
    run_day: date,
    current_price: Optional[float],
) -> Optional[float]:
    """TTM 滚动股息率（小数）= 窗口内去重每股分红和 ÷ 当前价(af3)。

    窗口内无已除权分红或当前价缺失 → None（非报错）。
    """
    cash_sum, _ = dedup_dividends(dividend_records, window_start, run_day)
    if current_price is None or current_price <= 0:
        return None
    if cash_sum <= 0:
        return None
    return cash_sum / current_price


def payout_ratio(
    cash_per_share_annual: Optional[float],
    total_share: Optional[float],
    net_profit: Optional[float],
) -> Optional[float]:
    """股利支付率（小数）= (每股分红Σ × totalShare) ÷ netProfit。

    无分红 / 缺股本 / netProfit<=0 → None。
    """
    if cash_per_share_annual is None or total_share is None or net_profit is None:
        return None
    if net_profit <= 0:
        return None
    return (cash_per_share_annual * total_share) / net_profit


def roe_stability(roe_values: Sequence[Optional[float]]) -> Tuple[Optional[float], Optional[float]]:
    """ROE 近3年（年度Q4）稳定性：返回 (mean, std)。

    - 用可得 n（上市<3年自然成立）；
    - n < 2 → std=None（均值仍给，n>=1）；
    - std 用总体标准差 pstdev（与调研报告 r3_piotroski.py 同口径）。
    """
    vals = [v for v in roe_values if v is not None]
    if not vals:
        return None, None
    mean = sum(vals) / len(vals)
    if len(vals) < 2:
        return mean, None
    var = sum((v - mean) ** 2 for v in vals) / len(vals)
    return mean, math.sqrt(var)


@dataclass
class PiotroskiResult:
    """Piotroski F-Score（9 信号，金融业 S6/S8 N/A）。

    F = 有效信号和 / 有效信号数；N/A 信号不计入分母（不得当 0 分）。
    """
    code: str
    signals: Dict[str, Optional[int]] = field(default_factory=dict)  # {S1..S9: 0/1/None}
    fscore: int = 0            # 有效信号之和
    n_valid: int = 0           # 有效信号数（分母）
    n_na: int = 0              # N/A 信号数
    na_signals: List[str] = field(default_factory=list)

    @property
    def ratio(self) -> Optional[float]:
        """F/有效数（0~1），打分因子用；无有效信号 → None。"""
        if self.n_valid == 0:
            return None
        return self.fscore / self.n_valid


def piotroski_fscore(
    code: str,
    profit_cur: Optional[Dict[str, Any]],
    profit_prior: Optional[Dict[str, Any]],
    balance_cur: Optional[Dict[str, Any]],
    balance_prior: Optional[Dict[str, Any]],
    growth_cur: Optional[Dict[str, Any]],
    cashflow_cur: Optional[Dict[str, Any]],
) -> PiotroskiResult:
    """Piotroski 9 信号 → BaoStock 字段映射（调研报告 R3 表，年度 Q4 口径）。

    - S1 ROA>0        == netProfit > 0                          （精确）
    - S2 CFO>0        ~ sign(netProfit × CFOToNP) > 0            （代理：无 OCF 绝对额）
    - S3 ΔROA>0       ~ roeAvg(cur) > roeAvg(prior)              （代理：用 ΔROE）
    - S4 CFO>NI       == CFOToNP > 1 且 netProfit > 0            （精确，经比率）
    - S5 去杠杆        ~ YOYLiability < 0                        （代理：总负债同比）
    - S6 流动性升      == currentRatio(cur) > (prior)             （精确；金融业空→N/A）
    - S7 未增发       == totalShare(cur) <= (prior)               （精确）
    - S8 毛利率升     == gpMargin(cur) > (prior)                  （精确；金融业空→N/A）
    - S9 资产周转升   ~ MBRevenue 增速 > YOYAsset                 （代理：无绝对营收/资产）

    信号所需字段缺失 → 该信号 N/A（不计入分母）。
    """
    def g(d: Optional[Dict[str, Any]], key: str) -> Optional[float]:
        if not d:
            return None
        v = d.get(key)
        try:
            return float(v) if v is not None and str(v).strip() != "" else None
        except (TypeError, ValueError):
            return None

    ni_c = g(profit_cur, "netProfit")
    cfo_np = g(cashflow_cur, "CFOToNP")
    roe_c = g(profit_cur, "roeAvg")
    roe_p = g(profit_prior, "roeAvg")
    yoy_liab = g(balance_cur, "YOYLiability")
    cr_c = g(balance_cur, "currentRatio")
    cr_p = g(balance_prior, "currentRatio")
    ts_c = g(profit_cur, "totalShare")
    ts_p = g(profit_prior, "totalShare")
    gm_c = g(profit_cur, "gpMargin")
    gm_p = g(profit_prior, "gpMargin")
    mb_c = g(profit_cur, "MBRevenue")
    mb_p = g(profit_prior, "MBRevenue")
    yoy_asset = g(growth_cur, "YOYAsset")

    sig: Dict[str, Optional[int]] = {}
    # S1 ROA>0（totalAssets 恒>0，netProfit>0 等价）
    sig["S1_ROA_pos"] = None if ni_c is None else (1 if ni_c > 0 else 0)
    # S2 CFO>0 ~ sign(netProfit × CFOToNP) > 0（代理）
    if ni_c is not None and cfo_np is not None:
        sig["S2_CFO_pos"] = 1 if (ni_c * cfo_np) > 0 else 0
    else:
        sig["S2_CFO_pos"] = None
    # S3 ΔROA>0 ~ ΔROE（代理）
    if roe_c is not None and roe_p is not None:
        sig["S3_dROA_up"] = 1 if roe_c > roe_p else 0
    else:
        sig["S3_dROA_up"] = None
    # S4 CFO>NI == CFOToNP>1（需 NI>0）
    if ni_c is not None and cfo_np is not None and ni_c > 0:
        sig["S4_CFO_gt_NI"] = 1 if cfo_np > 1 else 0
    else:
        sig["S4_CFO_gt_NI"] = None
    # S5 去杠杆 ~ YOYLiability<0（代理）
    sig["S5_deleveraging"] = None if yoy_liab is None else (1 if yoy_liab < 0 else 0)
    # S6 流动性升 == currentRatio 升（金融业空 → N/A）
    if cr_c is not None and cr_p is not None:
        sig["S6_liquidity_up"] = 1 if cr_c > cr_p else 0
    else:
        sig["S6_liquidity_up"] = None
    # S7 未增发 == totalShare 未增
    if ts_c is not None and ts_p is not None:
        sig["S7_no_new_shares"] = 1 if ts_c <= ts_p else 0
    else:
        sig["S7_no_new_shares"] = None
    # S8 毛利率升 == gpMargin 升（金融业空 → N/A）
    if gm_c is not None and gm_p is not None:
        sig["S8_gm_up"] = 1 if gm_c > gm_p else 0
    else:
        sig["S8_gm_up"] = None
    # S9 资产周转升 ~ 营收增速 > 资产增速（代理）
    if mb_c and mb_p and yoy_asset is not None:
        rev_g = mb_c / mb_p - 1.0
        sig["S9_turnover_up"] = 1 if rev_g > yoy_asset else 0
    else:
        sig["S9_turnover_up"] = None

    res = PiotroskiResult(code=code, signals=sig)
    res.na_signals = [k for k, v in sig.items() if v is None]
    valid = {k: v for k, v in sig.items() if v is not None}
    res.n_valid = len(valid)
    res.n_na = len(res.na_signals)
    res.fscore = sum(valid.values())
    return res


def rank_percentile(
    code: str,
    group_codes: Sequence[str],
    value_map: Dict[str, Optional[float]],
) -> Tuple[Optional[int], Optional[float]]:
    """组内按值降序排名 → (rank, percentile=rank/size×100)。

    值缺失的股票排组末（稳定排序，code 升序兜底）。通用版：ROE/YOYPNI 等任意
    "越大越好"的因子都可用（v2 行业维度两个分位因子共用）。

    **全组无信号**（组内所有 value 均为 None）→ 返回 (None, None)：没有基本面
    数据就不产生排名，避免按 code 升序派生噪声分位冒充行业信号（D-01 修复；
    TL 硬性要求6"不得用缺失数据冒充"）。v2 live 路径不受影响——当前季度财报
    总是已披露，正常组不会全 None。
    """
    if all(value_map.get(c) is None for c in group_codes):
        return None, None

    def sort_key(c: str):
        v = value_map.get(c)
        return (v is None, -(v or 0.0), c)

    ordered = sorted(group_codes, key=sort_key)
    rank_of = {c: i + 1 for i, c in enumerate(ordered)}
    r = rank_of[code]
    return r, round(r / len(group_codes) * 100.0, 2)


# ===========================================================================
# v5 新因子（TL D1/D3/D4/D6/D8，报告 §4 草案签名；纯函数、离线可测）
# ===========================================================================
# PIT 纪律（brief §3）：分红事件锚 = ex_date <= run_day（与 v4 一致）；
# 股东/现金流表按 NOTICE_DATE <= run_day 取报告期。
# 同除权日"预案+正式"去重沿用 v4 逻辑（dedup_dividends，上方 metrics.py:417-450）。

def em_dividend_records(
    em_rows: Sequence[Dict[str, Any]], code_map: Dict[str, str]
) -> List[Dict[str, Any]]:
    """东财分红全表行 → BaoStock 口径记录（复用 v4 dedup/ttm 函数，零口径漂移）。

    :param em_rows: em_dividend_all.csv 行 {code(6位), ex_date, dps_pretax(元/股), ...}
        —— dps_pretax 已在入库时 /10（每10股→每股），此处**不再除10**。
    :param code_map: {6位代码: BaoStock格式代码 sh.601398}；未收录的 6 位代码跳过。
    EX_DIVIDEND_DATE=null 的未实施预案行（ex_date=''）保留在输出里，
    v4 的 dedup_dividends/ttm_dividend_yield 会自动按"无除权日=未实施"过滤。
    """
    out: List[Dict[str, Any]] = []
    for r in em_rows:
        c6 = str(r.get("code") or "")
        bs_code = code_map.get(c6)
        if not bs_code:
            continue
        out.append({
            "code": bs_code,
            "dividOperateDate": str(r.get("ex_date") or ""),
            "dividCashPsBeforeTax": r.get("dps_pretax"),
        })
    # 同除权日"预案+正式"并存时，让**有现金（实施）的行排在前面**——v4 dedup_dividends
    # 按 dividOperateDate 稳定排序后取每组首行（first-wins），此排序保证首行=实施行
    # （cash=None 的预案行被跳过）。ex_date=''（未实施预案）排最前，dedup 会因无除权日过滤。
    out.sort(key=lambda x: (x["dividOperateDate"], x["dividCashPsBeforeTax"] is None))
    return out


def annual_dps_from_em(
    em_rows: Sequence[Dict[str, Any]], code6: str, run_day: str
) -> Dict[int, float]:
    """单只股票的逐年每股税前现金分红 {year: dps}（PIT：ex_date <= run_day）。

    去重口径（沿用 v4"一个除权日=一次事件"）：同一 (code, ex_date) 多行
    （预案+正式并存）只取一行——优先 ASSIGN_PROGRESS 含"实施"的行，其次
    plan_notice_date 最新者；dps 为 null（纯送转/未填）的事件不计现金。
    """
    by_ex: Dict[str, List[Dict[str, Any]]] = {}
    for r in em_rows:
        if str(r.get("code") or "") != code6:
            continue
        ex = str(r.get("ex_date") or "")
        if not ex or ex > run_day:  # PIT：未实施（null）或未来除权事件不可见
            continue
        by_ex.setdefault(ex, []).append(r)

    def pick(rows_same_ex: List[Dict[str, Any]]) -> Dict[str, Any]:
        impl = [x for x in rows_same_ex if "实施" in str(x.get("progress") or "")]
        pool = impl or rows_same_ex
        return max(pool, key=lambda x: str(x.get("plan_notice_date") or ""))

    annual: Dict[int, float] = {}
    for ex, rows_same_ex in by_ex.items():
        rec = pick(rows_same_ex)
        dps = rec.get("dps_pretax")
        if dps is None or dps <= 0:
            continue
        y = int(ex[:4])
        annual[y] = annual.get(y, 0.0) + float(dps)
    return annual


def consecutive_div_years(annual_dps: Dict[int, float], run_year: int) -> Optional[int]:
    """从 run_year-1 向前数 dps>0 的连续自然年数（TL D1）。

    - 无任何记录 → None（区分"从没分过红"与"断档=0 年"）；
    - run_year-1 当年无分红 → 0（连续性从最近一年起算，中间断档即终止）。
    """
    if not annual_dps:
        return None
    n = 0
    y = run_year - 1
    while annual_dps.get(y, 0.0) > 0:
        n += 1
        y -= 1
    return n


def new_stock_div_ok(
    annual_dps: Dict[int, float], ipo_year: int, run_year: int
) -> bool:
    """TL D1 新股规则：IPO 不满 7 年 → IPO 年份之后**每个完整年度**都有分红。

    要求区间 = [ipo_year+1, run_year-1]（IPO 当年不要求——上市不足整年，
    非"完整年度"；报告 §4 Q1 + 单测边界用例）。区间为空（IPO 次年即运行年）
    → 视为满足（无完整年度可断档）。
    """
    for y in range(ipo_year + 1, run_year):
        if annual_dps.get(y, 0.0) <= 0:
            return False
    return True


def div_stability_cv(annual_dps: Dict[int, float], n: int = 5,
                    end_year: Optional[int] = None) -> Optional[float]:
    """近 n 年 DPS 变异系数 std/mean（越低越稳定；打分取负）。

    窗口 = [end_year-n+1, end_year]（默认 end_year=调用方传 run_year-1）；
    有效样本（dps>0 的年份）<3 → None（n<3 同样 None）。std 用总体标准差
    pstdev（与 roe_stability 同口径）。
    """
    if n < 3:
        return None
    end = end_year if end_year is not None else max(annual_dps, default=0)
    vals = [annual_dps[y] for y in range(end - n + 1, end + 1)
            if annual_dps.get(y, 0.0) > 0]
    if len(vals) < 3:
        return None
    mean = sum(vals) / len(vals)
    if mean <= 0:
        return None
    var = sum((v - mean) ** 2 for v in vals) / len(vals)
    return math.sqrt(var) / mean


def fcf_coverage(
    ocf: Optional[float], capex: Optional[float],
    dps_annual: Optional[float], total_share: Optional[float],
) -> Optional[float]:
    """FCF 分红覆盖倍数 = (OCF - capex) / (年度DPS × 总股本)（TL D6 真值口径）。

    分母 <=0（无分红/缺股本）或任一输入缺失 → None。capex 缺失时按 0 处理
    （保守方向由调用方决定是否改用代理 fcf_coverage_proxy）。
    """
    if ocf is None or dps_annual is None or total_share is None:
        return None
    cap = capex or 0.0
    denom = float(dps_annual) * float(total_share)
    if denom <= 0:
        return None
    return (float(ocf) - cap) / denom


def fcf_coverage_proxy(
    cfo_to_np: Optional[float], payout: Optional[float]
) -> Optional[float]:
    """兜底代理（TL D6：仅东财现金流接口失败时降级）= CFOToNP / payout。

    推导：CFOToNP=CFO/净利，payout=分红/净利 → 比值 = CFO/分红 ≈ FCF 覆盖
    （忽略 capex）。payout<=0 或缺失 → None。
    """
    if cfo_to_np is None or payout is None or payout <= 0:
        return None
    return float(cfo_to_np) / float(payout)


def dividend_yield_percentile(
    dps_history: Dict[int, float],
    closes_af3_by_date: Dict[str, float],
    run_day: str,
    lookback_years: int,
    current_ttm_yield: Optional[float] = None,
) -> Tuple[Optional[float], int]:
    """当前 TTM 股息率在自身历史年度股息率序列中的分位（0-100）+ 样本年数。

    各历史年度 yield_Y = DPS(Y) / Y 年末参考日 close（af3 不复权，PIT：只用
    ex_date<=run_day 的历史事件与 <=Y 年末的价格）。参考日 = Y-12-31 或之前
    最近一个有 close 的交易日（停牌/退市兜底）。

    :param dps_history: {year: 年度每股DPS}（annual_dps_from_em 输出，已 PIT 过滤）
    :param closes_af3_by_date: {date: close} 全史不复权价（kline_af3 缓存）
    :param current_ttm_yield: 当前 TTM 股息率（小数）；None → 只返回样本数、分位 None
    :return: (分位0-100 或 None, 有效样本年数)。分位 = 历史 yield <= 当前值的占比×100。
    """
    run_year = int(run_day[:4])
    yields: List[float] = []
    for y in range(run_year - lookback_years, run_year):
        dps = dps_history.get(y)
        if not dps or dps <= 0:
            continue
        ref = _year_ref_close(closes_af3_by_date, y)
        if ref is None or ref <= 0:
            continue
        yields.append(dps / ref)
    if current_ttm_yield is None or not yields:
        return None, len(yields)
    n_le = sum(1 for v in yields if v <= current_ttm_yield)
    return round(n_le / len(yields) * 100.0, 2), len(yields)


def _year_ref_close(closes_af3_by_date: Dict[str, float], year: int) -> Optional[float]:
    """year-12-31 或之前最近一个有 close 的交易日（二分查找，O(log n)）。"""
    if not closes_af3_by_date:
        return None
    target = f"{year}-12-31"
    keys = sorted(closes_af3_by_date)
    lo, hi = 0, len(keys) - 1
    best: Optional[str] = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if keys[mid] <= target:
            best = keys[mid]
            lo = mid + 1
        else:
            hi = mid - 1
    return closes_af3_by_date.get(best) if best else None


def yield_spread(ttm_yield: Optional[float], rf_10y: Optional[float]) -> Optional[float]:
    """股息率 − 10Y 国债收益率（小数差值；TL D4）。任一缺失 → None。"""
    if ttm_yield is None or rf_10y is None:
        return None
    return float(ttm_yield) - float(rf_10y)


def soe_flag(holders: Sequence[Dict[str, Any]], keywords: Optional[Sequence[str]] = None) -> Optional[str]:
    """央国企识别（TL D3，关键词 config 驱动）。

    规则：前十大股东名称命中任一关键词 → 'soe'；且存在 IS_SJKZR=1 → 'soe_confirmed'。
    **仅** IS_SJKZR=1 未命中关键词 → None（报告单列清单供人工复核，不判 soe）。

    ⚠️实测结论（evidence/probe20_summary.json）：银行类前十大 IS_SJKZR 全为 0
    （601398 汇金+财政部并列无单一实控人）→ 纯标记规则会漏掉全部国有大行，必须靠关键词。

    :param holders: 单只股票的前十大股东 [{holder_name, is_sjkzr('0'/'1'), ...}]
        （PIT：调用方已按 NOTICE_DATE <= run_day 过滤）
    :param keywords: 关键词清单（config universe.soe_keywords）；None/空 → 永不命中
    """
    kws = [k for k in (keywords or []) if k]
    kw_hit = False
    sjkzr_hit = False
    for h in holders:
        name = str(h.get("holder_name") or "")
        if any(k in name for k in kws):
            kw_hit = True
        if str(h.get("is_sjkzr") or "0").strip() == "1":
            sjkzr_hit = True
    if not kw_hit:
        return None  # 含"仅 IS_SJKZR=1"情形 → 调用方另行列复核清单
    return "soe_confirmed" if sjkzr_hit else "soe"


def is_sjkzr_only(holders: Sequence[Dict[str, Any]], keywords: Optional[Sequence[str]] = None) -> bool:
    """'仅 IS_SJKZR=1 未命中关键词'标记（TL D3：报告单列清单供人工复核）。"""
    if soe_flag(holders, keywords) is not None:
        return False
    kws = [k for k in (keywords or []) if k]
    for h in holders:
        if str(h.get("is_sjkzr") or "0").strip() != "1":
            continue
        name = str(h.get("holder_name") or "")
        if not any(k in name for k in kws):
            return True
    return False
