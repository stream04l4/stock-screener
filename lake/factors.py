# -*- coding: utf-8 -*-
"""lake.factors —— T8 因子计算（**纯本地，读 T1-T7，零网络**；报告 §3 T8）。

因子集（调研报告 §3 T8）：
- 行业排名：roe_rank_pct / yield_rank_pct / mv_rank_pct（行业内截面分位）
- 低波：ann_vol_3y/5y/10y、max_drawdown_5y、beta_vs_hs300
- 估值时机：yield_pctile_own_hist、pe_pctile / pb_pctile、drawdown_from_250d_high

写入 T8 ``factor_snapshot``（EAV：ts_code/as_of_date/factor_name/value/params_json）。
幂等：INSERT OR REPLACE（PK ts_code+as_of_date+factor_name）。

⚠️ 口径：波动率/回撤基于 **hfq close**（后复权，总收益口径）——raw close 跨除权日
不连续，直接算会引入假跳变。beta 基准 = sh000300（沪深300，T7）。
"""
from __future__ import annotations

import json
import logging
import math
from typing import Any, Dict, List, Optional, Sequence

from .ingest.common import DATA_VERSION, now_ts

log = logging.getLogger("lake.factors")


# ---------------------------------------------------------------------------
# 基础序列读取（T2 hfq close / T7 指数）
# ---------------------------------------------------------------------------
def _hfq_close_series(con, ts_code: str) -> List[tuple]:
    """[(date, hfq_close)] 升序；af 缺失段（NULL）跳过。"""
    rows = con.execute(
        'SELECT date, "close" FROM kline_daily_hfq '
        "WHERE ts_code=? AND \"close\" IS NOT NULL ORDER BY date", [ts_code]
    ).fetchall()
    return [(str(r[0]), float(r[1])) for r in rows if r[1] is not None]


def _index_close_series(con, index_code: str) -> List[tuple]:
    rows = con.execute(
        "SELECT date, close FROM index_daily WHERE index_code=? AND close IS NOT NULL "
        "ORDER BY date", [index_code]).fetchall()
    return [(str(r[0]), float(r[1])) for r in rows if r[1] is not None]


def _log_returns(series: List[tuple]) -> List[float]:
    """对数收益率序列（长度 = len(series)-1）。"""
    out = []
    for i in range(1, len(series)):
        prev, cur = series[i - 1][1], series[i][1]
        if prev > 0 and cur > 0:
            out.append(math.log(cur / prev))
    return out


def _annual_vol(rets: List[float], years: int) -> Optional[float]:
    """年化波动率（%）。窗口不足 → None。"""
    window = int(years * 252)
    if len(rets) < max(20, window // 2):
        return None
    seg = rets[-window:] if len(rets) >= window else rets
    n = len(seg)
    mean = sum(seg) / n
    var = sum((x - mean) ** 2 for x in seg) / max(1, n - 1)
    return math.sqrt(var) * math.sqrt(252) * 100.0


def _max_drawdown(series: List[tuple]) -> Optional[float]:
    """最大回撤（%，负值）。基于 hfq close。"""
    if len(series) < 2:
        return None
    peak = series[0][1]
    mdd = 0.0
    for _, price in series:
        if price > peak:
            peak = price
        dd = (price / peak - 1.0) * 100.0 if peak > 0 else 0.0
        if dd < mdd:
            mdd = dd
    return mdd


def _beta(stock_rets: List[float], bench_rets: List[float]) -> Optional[float]:
    """beta（对沪深300）。两序列按公共日期对齐后算协方差/方差。"""
    if len(stock_rets) < 60 or len(bench_rets) < 60:
        return None
    n = min(len(stock_rets), len(bench_rets))
    s, b = stock_rets[-n:], bench_rets[-n:]
    ms, mb = sum(s) / n, sum(b) / n
    cov = sum((s[i] - ms) * (b[i] - mb) for i in range(n)) / max(1, n - 1)
    var = sum((x - mb) ** 2 for x in b) / max(1, n - 1)
    if var == 0:
        return None
    return cov / var


# ---------------------------------------------------------------------------
# 因子计算（单股）
# ---------------------------------------------------------------------------
def compute_stock_factors(con, ts_code: str, as_of_date: str,
                          hs300_rets: Optional[List[float]] = None) -> Dict[str, float]:
    """算单股全部 T8 因子 → {factor_name: value}（None 值跳过）。

    :param hs300_rets: 沪深300 对数收益序列（可预取复用，避免每股重读 T7）。
    """
    series = _hfq_close_series(con, ts_code)
    out: Dict[str, float] = {}
    if len(series) >= 20:
        rets = _log_returns(series)
        for years in (3, 5, 10):
            v = _annual_vol(rets, years)
            if v is not None:
                out[f"ann_vol_{years}y"] = round(v, 4)
        dd5 = _max_drawdown(series[-(5 * 252):])
        if dd5 is not None:
            out["max_drawdown_5y"] = round(dd5, 4)
        # drawdown_from_250d_high：当前价 vs 近250日高点
        recent = series[-250:]
        if recent:
            high = max(p for _, p in recent)
            cur = recent[-1][1]
            if high > 0:
                out["drawdown_from_250d_high"] = round((cur / high - 1.0) * 100.0, 4)
        # beta vs hs300
        if hs300_rets is None:
            bench = _index_close_series(con, "sh000300")
            hs300_rets = _log_returns(bench)
        b = _beta(rets, hs300_rets)
        if b is not None:
            out["beta_vs_hs300"] = round(b, 4)
    # 估值时机：yield/pe/pb 自身历史分位（T3）
    out.update(_valuation_pctiles(con, ts_code))
    return out


def _valuation_pctiles(con, ts_code: str) -> Dict[str, float]:
    """T3 估值字段自身历史分位（yield_pctile_own_hist / pe_pctile / pb_pctile）。"""
    out: Dict[str, float] = {}
    for col, name in [("ttm_yield_pct", "yield_pctile_own_hist"),
                      ("pe_ttm", "pe_pctile"), ("pb", "pb_pctile")]:
        rows = con.execute(
            f"SELECT {col} FROM valuation_daily WHERE ts_code=? AND {col} IS NOT NULL "
            "ORDER BY date", [ts_code]).fetchall()
        vals = sorted(float(r[0]) for r in rows if r[0] is not None)
        if len(vals) >= 10:
            cur = vals[-1]
            below = sum(1 for v in vals if v <= cur)
            out[name] = round(below / len(vals) * 100.0, 4)
    return out


def compute_industry_ranks(con, as_of_date: str) -> Dict[str, Dict[str, float]]:
    """行业截面排名（roe_rank_pct / yield_rank_pct / mv_rank_pct）。

    读 T5 最近季 roe_avg + T3 最新 ttm_yield_pct/total_mv，按 industry_csric2 分组算分位。
    :return: {ts_code: {factor_name: value}}。
    """
    rows = con.execute(
        "SELECT m.ts_code, m.industry_csric2, v.ttm_yield_pct, v.total_mv, f.roe_avg "
        "FROM stock_master m "
        "LEFT JOIN valuation_daily v ON v.ts_code=m.ts_code "
        "  AND v.date=(SELECT MAX(date) FROM valuation_daily) "
        "LEFT JOIN fundamentals_quarterly f ON f.ts_code=m.ts_code "
        "  AND f.period=(SELECT MAX(period) FROM fundamentals_quarterly)"
    ).fetchall()
    # 按行业分组
    by_ind: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        ind = r[1] or "UNKNOWN"
        by_ind.setdefault(ind, []).append({
            "ts_code": r[0], "yield": r[2], "mv": r[3], "roe": r[4]})
    out: Dict[str, Dict[str, float]] = {}
    for ind, group in by_ind.items():
        if len(group) < 2:
            continue
        for metric, col in [("yield_rank_pct", "yield"), ("mv_rank_pct", "mv"),
                            ("roe_rank_pct", "roe")]:
            vals = [g[col] for g in group if g[col] is not None]
            if len(vals) < 2:
                continue
            sv = sorted(vals)
            for g in group:
                v = g[col]
                if v is None:
                    continue
                rank_pct = sum(1 for x in sv if x <= v) / len(sv) * 100.0
                out.setdefault(g["ts_code"], {})[metric] = round(rank_pct, 4)
    return out


# ---------------------------------------------------------------------------
# 写入 T8（幂等）
# ---------------------------------------------------------------------------
def write_factors(con, ts_code: str, as_of_date: str,
                  factors: Dict[str, float], source: str = "lake_factors") -> int:
    """T8 upsert：{factor_name: value} → factor_snapshot 行。None 值跳过。"""
    n = 0
    for name, val in factors.items():
        if val is None:
            continue
        con.execute(
            "INSERT OR REPLACE INTO factor_snapshot "
            "(ts_code, as_of_date, factor_name, value, params_json, source, fetched_at, "
            " data_version) VALUES (?,?,?,?,?,?,?,?)",
            [ts_code, as_of_date, name, float(val), json.dumps({"as_of": as_of_date}),
             source, now_ts(), DATA_VERSION])
        n += 1
    return n


def recompute_all(con, as_of_date: str, ts_codes: Optional[Sequence[str]] = None) -> int:
    """批量重算 T8（每日 P0 灌完后调一次；报告 §6-Q6）。

    :param ts_codes: 限定股票集；None=stock_master 全部。
    :return: 写入因子行数。
    """
    if ts_codes is None:
        ts_codes = [r[0] for r in con.execute(
            "SELECT ts_code FROM stock_master").fetchall()]
    # 预取 hs300 收益序列（复用，避免每股重读 T7）
    bench = _index_close_series(con, "sh000300")
    hs300_rets = _log_returns(bench)
    total = 0
    for ts in ts_codes:
        f = compute_stock_factors(con, ts, as_of_date, hs300_rets=hs300_rets)
        total += write_factors(con, ts, as_of_date, f)
    # 行业截面排名（全市场一次）
    ranks = compute_industry_ranks(con, as_of_date)
    for ts, rf in ranks.items():
        total += write_factors(con, ts, as_of_date, rf)
    log.info("T8 重算完成：%d 股，%d 因子行（as_of=%s）", len(ts_codes), total, as_of_date)
    return total
