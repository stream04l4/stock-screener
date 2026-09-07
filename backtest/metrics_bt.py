# -*- coding: utf-8 -*-
"""回测绩效指标（纯函数层，离线可测；口径与 v2 一致：250 交易日年化）。

输入统一为**日频净值序列**（date → nav）与可选基准序列。所有指标都是
已知答案数据集可验证的纯函数（tests/test_metrics_bt.py 手工算答案）。

口径约定：
- 日收益 r_t = nav_t/nav_{t-1} − 1（简单收益，与模拟器现金记账一致）。
- 年化 = (末/初)^(250/n) − 1；波动 = std(日收益, n-1) × √250。
- 夏普 = (年化 − rf) / 年化波动；rf 来自 config backtest.risk_free_pct（%）。
- 最大回撤 = min(nav_t / max_{s<=t} nav_s − 1)。
- Calmar = 年化 / |最大回撤|（回撤=0 → None）。
- alpha/beta：对基准日收益做 OLS（beta=cov/var，alpha=(r̄−β·b̄)×250 年化%）。
- 信息比率 = mean(r_s − r_b) / std(r_s − r_b, n-1) × √250。
- 月度胜率：按自然月聚合收益 > 0 的月份占比（%）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

TRADING_DAYS_PER_YEAR = 250  # A股年化因子（指标定义，与 v2 metrics 一致）


def daily_returns(navs: Sequence[float]) -> List[float]:
    """简单日收益序列（长度 len(navs)-1）。"""
    out: List[float] = []
    for i in range(1, len(navs)):
        prev = navs[i - 1]
        if prev and prev > 0:
            out.append(navs[i] / prev - 1.0)
    return out


def _mean_std(xs: Sequence[float]) -> Tuple[float, float]:
    n = len(xs)
    if n == 0:
        return 0.0, 0.0
    mean = sum(xs) / n
    if n < 2:
        return mean, 0.0
    var = sum((x - mean) ** 2 for x in xs) / (n - 1)
    return mean, math.sqrt(var)


def annualized_return(navs: Sequence[float]) -> Optional[float]:
    """年化收益率（小数）。<2 个点 → None。"""
    if len(navs) < 2 or navs[0] <= 0 or navs[-1] <= 0:
        return None
    n = len(navs) - 1
    return (navs[-1] / navs[0]) ** (TRADING_DAYS_PER_YEAR / n) - 1.0


def annualized_volatility(navs: Sequence[float]) -> Optional[float]:
    """年化波动率（小数）。<2 个收益 → None。"""
    rets = daily_returns(navs)
    if len(rets) < 2:
        return None
    _, std = _mean_std(rets)
    return std * math.sqrt(TRADING_DAYS_PER_YEAR)


def max_drawdown(navs: Sequence[float]) -> float:
    """最大回撤（负数小数；无回撤 → 0.0）。"""
    peak = -math.inf
    mdd = 0.0
    for v in navs:
        if v > peak:
            peak = v
        if peak > 0:
            dd = v / peak - 1.0
            if dd < mdd:
                mdd = dd
    return mdd


def sharpe_ratio(navs: Sequence[float], risk_free_pct: float) -> Optional[float]:
    """夏普比率（rf 为年化百分数，如 2.0 = 2%）。"""
    ar = annualized_return(navs)
    av = annualized_volatility(navs)
    if ar is None or av is None or av <= 0:
        return None
    return (ar - risk_free_pct / 100.0) / av


def calmar_ratio(navs: Sequence[float]) -> Optional[float]:
    ar = annualized_return(navs)
    mdd = max_drawdown(navs)
    if ar is None or mdd >= 0:
        return None
    return ar / abs(mdd)


def beta_alpha(
    navs: Sequence[float], bench_navs: Sequence[float]
) -> Tuple[Optional[float], Optional[float]]:
    """对基准日收益 OLS：返回 (beta, alpha 年化%)。

    alpha = (mean(r_s) − beta·mean(r_b)) × 250（百分数）。样本 <2 → (None, None)。
    """
    rs = daily_returns(navs)
    rb = daily_returns(bench_navs)
    n = min(len(rs), len(rb))
    if n < 2:
        return None, None
    rs, rb = rs[-n:], rb[-n:]
    mb, sb = _mean_std(rb)
    ms, _ = _mean_std(rs)
    var_b = sb * sb   # beta 分母是**方差**（_mean_std 返回 std）
    if var_b <= 0:
        return None, None
    cov = sum((rs[i] - ms) * (rb[i] - mb) for i in range(n)) / (n - 1)
    beta = cov / var_b
    alpha_pct = (ms - beta * mb) * TRADING_DAYS_PER_YEAR * 100.0
    return beta, alpha_pct


def information_ratio(
    navs: Sequence[float], bench_navs: Sequence[float]
) -> Optional[float]:
    """信息比率：主动收益均值/标准差 × √250。"""
    rs = daily_returns(navs)
    rb = daily_returns(bench_navs)
    n = min(len(rs), len(rb))
    if n < 2:
        return None
    active = [rs[-n + i] - rb[-n + i] for i in range(n)]
    m, s = _mean_std(active)
    if s <= 0:
        return None
    return m / s * math.sqrt(TRADING_DAYS_PER_YEAR)


def monthly_win_rate(navs: Sequence[float], dates: Sequence[str]) -> Optional[float]:
    """月度胜率（%）：按自然月聚合收益 >0 的月份占比。"""
    if len(navs) < 2 or len(dates) != len(navs):
        return None
    monthly: Dict[str, float] = {}
    order: List[str] = []
    for i in range(1, len(navs)):
        key = dates[i][:7]  # YYYY-MM
        if key not in monthly:
            monthly[key] = navs[i - 1]
            order.append(key)
        monthly[key] = navs[i]
    wins = 0
    total = 0
    for key in order:
        first = None
        for i in range(1, len(navs)):
            if dates[i][:7] == key:
                first = i - 1
                break
        last_i = max(i for i in range(len(navs)) if dates[i][:7] == key)
        if first is None or navs[first] <= 0:
            continue
        ret = navs[last_i] / navs[first] - 1.0
        total += 1
        if ret > 0:
            wins += 1
    if total == 0:
        return None
    return wins / total * 100.0


@dataclass
class MetricsSummary:
    """单窗口指标集（report 层渲染）。"""

    start: str = ""
    end: str = ""
    n_days: int = 0
    total_return_pct: Optional[float] = None
    annual_return_pct: Optional[float] = None
    annual_vol_pct: Optional[float] = None
    sharpe: Optional[float] = None
    max_drawdown_pct: Optional[float] = None
    calmar: Optional[float] = None
    beta: Optional[float] = None
    alpha_annual_pct: Optional[float] = None
    info_ratio: Optional[float] = None
    monthly_win_rate_pct: Optional[float] = None
    avg_turnover_one_way: Optional[float] = None   # 平均单边换手率（%）


def summarize(
    dates: Sequence[str],
    navs: Sequence[float],
    risk_free_pct: float,
    bench_dates: Optional[Sequence[str]] = None,
    bench_navs: Optional[Sequence[float]] = None,
    turnover_by_day: Optional[Dict[str, float]] = None,
) -> MetricsSummary:
    """计算一个窗口的全部指标。基准缺失 → 对应字段 None。"""
    s = MetricsSummary(
        start=dates[0] if dates else "",
        end=dates[-1] if dates else "",
        n_days=len(navs),
    )
    if len(navs) >= 2 and navs[0] > 0:
        s.total_return_pct = (navs[-1] / navs[0] - 1.0) * 100.0
        s.annual_return_pct = _pct(annualized_return(navs))
        s.annual_vol_pct = _pct(annualized_volatility(navs))
        s.sharpe = sharpe_ratio(navs, risk_free_pct)
        s.max_drawdown_pct = max_drawdown(navs) * 100.0
        s.calmar = calmar_ratio(navs)
    if bench_navs and bench_dates and len(bench_navs) >= 2:
        beta, alpha = beta_alpha(navs, bench_navs)
        s.beta = beta
        s.alpha_annual_pct = alpha
        s.info_ratio = information_ratio(navs, bench_navs)
    s.monthly_win_rate_pct = monthly_win_rate(navs, dates)
    if turnover_by_day:
        # 只统计窗口内的调仓日
        in_window = {d for d in dates}
        vals = [v for d, v in turnover_by_day.items() if d in in_window]
        if vals:
            s.avg_turnover_one_way = sum(vals) / len(vals) * 100.0
    return s


def slice_window(
    dates: Sequence[str], navs: Sequence[float], days: int
) -> Tuple[List[str], List[float]]:
    """取最近 ``days`` 个交易日的窗口（1y/3y/5y 切片用；不足则全量）。"""
    if len(dates) <= days:
        return list(dates), list(navs)
    return list(dates[-days:]), list(navs[-days:])


def yearly_breakdown(
    dates: Sequence[str], navs: Sequence[float]
) -> List[Dict[str, Optional[float]]]:
    """逐年收益分解：[{year, total_return_pct, max_drawdown_pct}]。"""
    by_year: Dict[int, Dict[str, float]] = {}
    for i, d in enumerate(dates):
        y = int(d[:4])
        if y not in by_year:
            by_year[y] = {"base": navs[i - 1] if i > 0 else navs[0]}
        by_year[y]["last"] = navs[i]
    out: List[Dict[str, Optional[float]]] = []
    for y in sorted(by_year):
        base = by_year[y]["base"]
        last = by_year[y]["last"]
        # 年内净值序列（含上年末基点，用于回撤）
        idx = [i for i, d in enumerate(dates) if int(d[:4]) == y]
        seg_navs = ([navs[idx[0] - 1]] if idx[0] > 0 else []) + [navs[i] for i in idx]
        out.append({
            "year": y,
            "total_return_pct": (last / base - 1.0) * 100.0 if base > 0 else None,
            "max_drawdown_pct": max_drawdown(seg_navs) * 100.0,
        })
    return out


def _pct(v: Optional[float]) -> Optional[float]:
    return None if v is None else v * 100.0
