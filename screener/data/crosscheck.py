# -*- coding: utf-8 -*-
"""v5.2 Phase 1：多源交叉校验 v1（报告 §4，阈值按实测校准）。

纯函数层——**零网络、零 IO**，输入两源的已解析值，输出冲突判定。可离线单测
（fixture=stages/01_research/evidence 留样）。所有阈值由调用方从 config ``health:``
段传入（零硬编码纪律）；本模块不读 yaml。

三类字段校验（TL 拍板 Q1 字段级多源并存）：
- **close**：腾讯快照(高频主) vs 新浪 akshare daily(校验)。非除权日 |Δ|>tol% 告警；
  除权日**不校验 close 绝对值**（腾讯 idx4=交易所调整后参考价，系统性口径差非偏差），
  改由调用方走 r_event 一致性（见 :func:`check_r_event`）。
- **dps**：em_dividend_all(静态底表) vs akshare fhps_detail_em(增量，同源东财=一致性)。
  |Δ|≥warn_at 告警、>stop_at 停算标"待复核"。⚠️akshare "现金分红比例"是每10股口径，
  须 /10（evidence/boundary_probe_round5.json）——换算在 adapter 侧完成，本函数收每股值。
- **roe**：BaoStock roeAvg(小数,平均) vs 新浪 ROEWEIGHTED(百分数,加权)。换算后
  |Δ|>tol_pp 告警（实测基线差 0.48pp=口径差非错误，阈值勿收紧到 <0.5pp）。

**EmptyPayloadGuard**（报告 §4 静默空数据检测，最高级冲突）：
- (a) 调用成功且 rows==0 且该源同类接口历史非空 → ``suspected_gap``（不写 canonical、
  不覆盖静态底表）；
- (b) 市场级接口(all_stock)交易日 rows<min_market_rows → ``source_anomaly``。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional


# ===========================================================================
# 通用
# ===========================================================================
def pct_diff(a: Optional[float], b: Optional[float]) -> Optional[float]:
    """相对偏差百分比 |a-b|/b*100。任一为 None/0 → None（无法比较）。"""
    if a is None or b is None or b == 0:
        return None
    return abs(a - b) / abs(b) * 100.0


# ===========================================================================
# close：腾讯 vs 新浪
# ===========================================================================
def check_close(
    code: str,
    tencent_close: Optional[float],
    sina_close: Optional[float],
    tol_pct: float,
) -> Dict[str, Any]:
    """非除权日收盘价一致性。返回 {code, tencent, sina, diff_pct, ok}。

    :param tol_pct: 告警阈值（百分数，如 0.1）。实测基线=0（52/52 零偏差），0.1% 已留余量。
    :return: ok=None 表示无法比较（缺值）；ok=True/False 为判定结果。
    """
    diff = pct_diff(tencent_close, sina_close)
    return {
        "code": code,
        "tencent": tencent_close,
        "sina": sina_close,
        "diff_pct": None if diff is None else round(diff, 4),
        "ok": None if diff is None else (diff <= tol_pct),
    }


def check_r_event(
    code: str,
    r_event_from_tencent: Optional[float],
    r_event_from_cache: Optional[float],
    tol_pct: float,
) -> Dict[str, Any]:
    """除权日改校验 r_event 一致性（close_prevday/preclose_today）。

    腾讯侧 r_event 由 ExdateDetector 推导；缓存侧由 adjfactor 事件行反推。两者应一致
    （同一除权事件）。|Δ|>tol_pct → 冲突。
    """
    diff = pct_diff(r_event_from_tencent, r_event_from_cache)
    return {
        "code": code,
        "r_event_tencent": r_event_from_tencent,
        "r_event_cache": r_event_from_cache,
        "diff_pct": None if diff is None else round(diff, 4),
        "ok": None if diff is None else (diff <= tol_pct),
    }


# ===========================================================================
# dps：em 静态底表 vs akshare-em 增量（同源=一致性检查）
# ===========================================================================
def check_dps(
    code: str,
    em_dps: Optional[float],
    akshare_dps_per_share: Optional[float],
    warn_at: float,
    stop_at: float,
) -> Dict[str, Any]:
    """每股 DPS 一致性。返回 {code, em, akshare, diff, level}。

    :param em_dps: em_dividend_all.dps_pretax（已 /10，元/股）。
    :param akshare_dps_per_share: akshare "现金分红比例"/10（元/股，adapter 侧换算）。
    :param warn_at: |Δ|≥此值 → level=warn（告警）。
    :param stop_at: |Δ|>此值 → level=review（停算标"待复核"）。
    :return: level ∈ {ok, warn, review, skip}；skip=缺值无法比较。
    """
    if em_dps is None or akshare_dps_per_share is None:
        return {"code": code, "em": em_dps, "akshare": akshare_dps_per_share,
                "diff": None, "level": "skip"}
    diff = round(abs(em_dps - akshare_dps_per_share), 6)
    if diff > stop_at:
        level = "review"
    elif diff >= warn_at:
        level = "warn"
    else:
        level = "ok"
    return {"code": code, "em": em_dps, "akshare": akshare_dps_per_share,
            "diff": diff, "level": level}


def fhps_bonus_to_dps(bonus_per_10: Optional[float]) -> Optional[float]:
    """akshare "现金分红比例"（每10股口径）→ 每股 DPS。None/非数 → None。

    evidence/boundary_probe_round5.json：PRETAX_BONUS_RMB=每10股，须 /10。
    """
    if bonus_per_10 is None:
        return None
    try:
        return float(bonus_per_10) / 10.0
    except (TypeError, ValueError):
        return None


# ===========================================================================
# roe：BaoStock roeAvg(小数,平均) vs 新浪 ROEWEIGHTED(百分数,加权)
# ===========================================================================
def check_roe(
    code: str,
    bs_roe_avg: Optional[float],
    sina_roe_weighted_pct: Optional[float],
    tol_pp: float,
) -> Dict[str, Any]:
    """ROE 口径换算后一致性。返回 {code, bs_pct, sina_pct, diff_pp, ok}。

    :param bs_roe_avg: BaoStock roeAvg（小数，如 0.0897=8.97%；平均口径）。
    :param sina_roe_weighted_pct: 新浪 ROEWEIGHTED（百分数，如 9.45；加权口径）。
    :param tol_pp: 告警阈值（百分点，pp）。实测基线差 0.48pp=口径差非错误 → 默认 1.0，
        **勿收紧到 <0.5pp**（会误报全部正常股）。
    """
    bs_pct = None if bs_roe_avg is None else bs_roe_avg * 100.0
    diff = None
    if bs_pct is not None and sina_roe_weighted_pct is not None:
        diff = abs(bs_pct - float(sina_roe_weighted_pct))
    return {
        "code": code,
        "bs_pct": None if bs_pct is None else round(bs_pct, 4),
        "sina_pct": sina_roe_weighted_pct,
        "diff_pp": None if diff is None else round(diff, 4),
        "ok": None if diff is None else (diff <= tol_pp),
    }


# ===========================================================================
# EmptyPayloadGuard（静默空数据检测，最高级冲突）
# ===========================================================================
def empty_payload_guard(
    source: str,
    endpoint: str,
    rows: int,
    history_nonempty: bool,
    market_level: bool = False,
    min_market_rows: int = 5000,
) -> Dict[str, Any]:
    """空/异常 payload 守卫。返回 {source, endpoint, rows, status}。

    :param rows: 本次调用成功取回的行数。
    :param history_nonempty: 该源同类接口历史是否曾有非空响应（rawstore.history_nonempty）。
    :param market_level: 是否市场级接口(all_stock)——交易日恒有数千只，rows<阈值=数据源异常。
    :param min_market_rows: 市场级接口交易日最小行数（config health.market_min_rows）。

    status ∈ {ok, suspected_gap, source_anomaly}：
    - ``suspected_gap``：调用成功但 rows==0 且历史非空 → 疑似静默缺数据。调用方据此
      **不写 canonical、不覆盖静态底表**。
    - ``source_anomaly``：市场级接口交易日 rows<min_market_rows → 直接判数据源异常
      （复用 universe.py 现有守卫语义）。
    - ``ok``：其余（含历史也为空的真新股/无分红股——rows==0 但历史空=正常，不告警）。
    """
    if market_level and rows < min_market_rows:
        return {"source": source, "endpoint": endpoint, "rows": rows,
                "status": "source_anomaly"}
    if rows == 0 and history_nonempty:
        return {"source": source, "endpoint": endpoint, "rows": rows,
                "status": "suspected_gap"}
    return {"source": source, "endpoint": endpoint, "rows": rows, "status": "ok"}
