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
from typing import Any, Dict, List, Optional

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
