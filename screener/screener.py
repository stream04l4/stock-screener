# -*- coding: utf-8 -*-
"""筛选引擎 v2：稳定键增量数据层 + 双模式打分（zscore 多因子 / legacy 四维AND）。

v2 架构（调研报告 R1/R4 + TL 拍板）：
- **数据层**：全市场统一走稳定键缓存 ``kline_af3_{code}``（不复权全历史，尾部追加）
  + ``adjfactor_{code}``（复权因子全历史，事件驱动追加）。后复权 af=1 全部本地重建
  （``reconstruct.py``，实测 0 误差），旧漂移键 ``kline_*_af1*`` 不再读写。
- **硬性剔除**（可配置 hard_filter）：ST（日K isST=1）、上市未满 N 个交易日
  （全历史K线行数）。不加"重大违规"（BaoStock 无数据源，R4 明确不做）。
- **打分模式**：
  - ``zscore``（默认）：硬剔除后全体候选做截面 Z-Score 多因子打分 → Top N 榜单。
  - ``legacy``：旧四维 AND 硬过滤（原 metrics 函数不动），可一键回退/对照。
- **BaoStock 单 socket 串行**：全市场拉取为顺序循环 + [PROGRESS] 结构化进度日志
  （供 Web SSE 解析；向后兼容，旧行仍当普通日志）。

每日稳态成本（R1-c）：af=3 尾部追加 ~5010 次小查询（~15-25min）+ 事件驱动因子刷新
（绝大多数股票 0 次）+ 候选基本面/分红（首跑全量，之后命中缓存）。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd

from . import config as cfgmod
from .data.baostock_client import BaoStockClient
from .data.cache import DiskCache
from .data.fetchers import DataFetcher, KlineData
from .metrics import (
    DividendResult,
    FundamentalResult,
    IndustryResult,
    PiotroskiResult,
    TechnicalResult,
    build_industry_groups,
    compute_dividend_yield,
    compute_fundamental,
    compute_industry_rank,
    compute_macd,
    compute_technical,
    dedup_dividends,
    industry_pass,
    macd_golden_cross,
    payout_ratio,
    piotroski_fscore,
    rank_percentile,
    roe_stability,
    rsi_wilder,
    ttm_dividend_yield,
)
from .reconstruct import moving_average, rebuild_kline_series
from .scoring import DIMENSIONS, ScoredStock, dimension_means, score_cross_section
from .universe import UniverseStats, build_universe

log = logging.getLogger("screener.engine")


def _progress(stage: str, done: int, total: int) -> None:
    """结构化进度标记（供 Web SSE classify 解析；普通日志行向后兼容）。"""
    log.info("[PROGRESS] stage=%s done=%d total=%d", stage, done, total)


@dataclass
class ScreenResult:
    """一次完整筛选的结果（供 report 层渲染）。"""

    run_day: str = ""                  # 实际筛选的交易日（定位后填充）
    requested_date: str = ""           # 用户传入的日期（可能与 run_day 不同=回退）
    date_fallback: bool = False       # 是否发生了"非交易日→最近交易日"回退
    mode: str = "zscore"              # zscore | legacy
    universe_stats: Optional[UniverseStats] = None

    # 每只候选的完整指标（zscore=硬剔除后全体；legacy=技术面幸存者）
    candidates: pd.DataFrame = field(default_factory=pd.DataFrame)

    # 各层数量（漏斗/阶段统计）
    funnel: Dict[str, int] = field(default_factory=dict)

    # v2 打分产物
    scored: List[ScoredStock] = field(default_factory=list)
    factor_means: Dict[str, Dict[str, Optional[float]]] = field(default_factory=dict)
    top_n: int = 0
    annual_year: int = 0               # v2 基本面基准年度（最近年报年度，如 2025）

    # 缺失/异常名单
    missing_fundamental: List[Dict[str, str]] = field(default_factory=list)  # [{code,name,missing}]
    no_industry_codes: List[str] = field(default_factory=list)
    small_groups_skipped: Dict[str, int] = field(default_factory=dict)       # {行业: 组大小}
    st_excluded_count: int = 0
    insufficient_kline_count: int = 0

    # 数据时间戳与来源
    data_notes: List[str] = field(default_factory=list)
    industry_update_date: str = ""
    fundamental_period: Optional[str] = None      # "2026Q2"（探测到的最近披露期，legacy/展示用）
    fundamental_pub_dates: Dict[str, str] = field(default_factory=dict)  # code→pubDate
    dividend_window_start: str = ""               # 股息率窗口起点（ISO 日期）

    # 交叉验证（腾讯）
    crosscheck: List[Dict[str, Any]] = field(default_factory=list)

    # 耗时与请求统计
    elapsed_seconds: float = 0.0
    baostock_requests: int = 0
    kline_requests: int = 0          # K线(af3)查询次数（增量方案生效的直接证据）
    cache_stats: Dict[str, int] = field(default_factory=dict)


class _NoCandidates(Exception):
    """内部哨兵：硬剔除后无候选，跳过后续数据拉取（结果已在 result 中置空）。"""


def _resolve_run_day(
    fetcher: DataFetcher, requested: date, today: Optional[date] = None
) -> tuple[date, bool]:
    """把请求日期解析为实际筛选的交易日。

    请求日非交易日 → 回退到最近一个交易日（warning，报告注明）。
    未来日期守卫（D-01 修复）：请求日晚于今天直接拒绝——"尚未发生的交易日"
    没有任何数据，若放行会把空股票列表按不可变历史数据永久缓存，污染该日
    真实运行。latest_trade_date 恒返回 ≤ requested 的日期，因此检查
    requested > today 与检查"解析出的运行日 > today"等价，且可在任何网络
    请求（交易日历拉取）之前抛出。
    """
    if today is None:
        today = date.today()
    if requested > today:
        raise RuntimeError(
            f"请求日期晚于当前日期，拒绝运行 (requested={requested.isoformat()}, "
            f"today={today.isoformat()})"
        )
    latest = fetcher.latest_trade_date(requested)
    if latest is None:
        raise RuntimeError(f"无法在 {requested} 之前找到任何交易日")
    return latest, latest != requested


def _probe_latest_period(
    fetcher: DataFetcher, run_day: date, probe_back: int
) -> Optional[tuple[int, int]]:
    """从当前季度往前逐季探测，找到最近一个"有披露数据"的报告期。

    探测方式：对基准样本股（sh.601398 工商银行）调 query_profit_data(year, quarter)，
    非空即视为该报告期已披露（银行披露最早最齐）。最多回退 probe_back 季。
    不可硬编码季度——季报有披露滞后（如 2026Q3 要到 10 月才出）。
    """
    y, m = run_day.year, run_day.month
    q = (m - 1) // 3 + 1
    for step in range(probe_back):
        py, pq = y, q - step
        while pq < 1:
            pq += 4
            py -= 1
        row = fetcher.profit_data("sh.601398", py, pq)
        if row is not None and (row.get("roeAvg") is not None or row.get("pubDate")):
            log.info("最近披露报告期探测命中: %dQ%d (pubDate=%s)", py, pq, row.get("pubDate"))
            return py, pq
    return None


def _resolve_annual_year(fetcher: DataFetcher, run_day: date) -> int:
    """v2 基本面基准年度 = 最近一个"年报已披露完毕"的年度（Q4 报告截止 4/30）。

    5 月及以后 → run_year-1 年报齐；1-4 月 → run_year-2。防御：若基准股该年 Q4
    仍无数据（极端延迟），回退一年。
    """
    y = run_day.year - 1 if run_day.month > 4 else run_day.year - 2
    row = fetcher.profit_data("sh.601398", y, 4)
    if row is None or not (row.get("roeAvg") is not None or row.get("pubDate")):
        log.warning("基准年度 %dQ4 无数据，回退到 %dQ4", y, y - 1)
        return y - 1
    return y


def _kline_snapshot(fetcher: DataFetcher, code: str, run_day: str) -> Optional[KlineData]:
    """稳定键增量更新 + 运行日快照（K线查询计数由调用方通过 client.request_count 差值统计）。"""
    return fetcher.kline_af3_incremental(code, run_day)


def _rebuild_window(
    fetcher: DataFetcher, code: str, max_bars: int
) -> tuple[List[str], List[float]]:
    """本地重建后复权收盘价窗口（取最近 max_bars 根，离线、0 网络请求）。

    返回 (dates, af1_closes)。无缓存 → ([], [])。
    """
    rebuilt = fetcher.kline_af3_rebuilt(code)
    if not rebuilt or not rebuilt["dates"]:
        return [], []
    dates = rebuilt["dates"][-max_bars:]
    closes: List[float] = [c for c in rebuilt["af1_close"][-max_bars:] if c is not None]
    # 对齐：只保留 close 非空的日期（停牌空 close 行不参与均线计算）
    pairs = [(d, c) for d, c in zip(rebuilt["dates"], rebuilt["af1_close"]) if c is not None]
    pairs = pairs[-max_bars:]
    return [p[0] for p in pairs], [p[1] for p in pairs]


def _tech_factors(closes: List[float]) -> Dict[str, Optional[float]]:
    """技术面子因子（纯本地计算）。方向约定：值越大越好（low_vol 已取负）。"""
    f: Dict[str, Optional[float]] = {
        "ma_bullish": None, "window_return": None, "low_vol": None, "rsi": None, "macd": None,
    }
    if len(closes) >= 60:
        ma20 = sum(closes[-20:]) / 20.0
        ma60 = sum(closes[-60:]) / 60.0
        f["ma_bullish"] = 1.0 if ma20 > ma60 else 0.0
    if len(closes) >= 251:
        base = closes[-251]
        if base > 0:
            f["window_return"] = closes[-1] / base - 1.0
    if len(closes) >= 21:
        rets = [
            (closes[i] - closes[i - 1]) / closes[i - 1]
            for i in range(len(closes) - 20, len(closes))
            if closes[i - 1] > 0
        ]
        if len(rets) >= 2:
            mean = sum(rets) / len(rets)
            var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
            import math
            f["low_vol"] = -(math.sqrt(var) * math.sqrt(250.0))  # 低波动率越好 → 取负
    r = rsi_wilder(closes)
    if r is not None:
        f["rsi"] = r
    gc = macd_golden_cross(closes)
    if gc is not None:
        f["macd"] = 1.0 if gc else 0.0
    return f


def run_screener(
    cfg: Dict[str, Any],
    requested_date: date,
    output_dir: str = "output",
    do_crosscheck: bool = True,
) -> ScreenResult:
    """执行完整筛选流程。所有阈值来自 cfg（strategy.yaml），本函数不硬编码任何阈值。"""
    t_start = time.time()
    tc = cfgmod.tech(cfg)
    dc = cfgmod.dividend_cfg(cfg)
    fc = cfgmod.fundamental_cfg(cfg)
    ic = cfgmod.industry_cfg(cfg)
    uc = cfgmod.universe_cfg(cfg)
    datac = cfgmod.data_cfg(cfg)
    scfg = cfgmod.scoring_cfg(cfg)
    hfc = cfgmod.hard_filter_cfg(cfg)
    mode = scfg["mode"]

    client = BaoStockClient(max_attempts=datac["retry_max_attempts"])
    cache = DiskCache(datac["cache_dir"])
    fetcher = DataFetcher(client, cache)
    result = ScreenResult(requested_date=requested_date.isoformat(), mode=mode, top_n=scfg["top_n"])

    no_candidates = False
    try:
        # ---------- 0. 交易日定位 ----------
        run_day, fallback = _resolve_run_day(fetcher, requested_date)
        fetcher.set_run_day(run_day)  # 缓存 TTL 依赖运行日（分红/季报的可变窗口）
        result.run_day = run_day.isoformat()
        result.date_fallback = fallback
        if fallback:
            log.warning(
                "请求日期 %s 非交易日，回退到最近交易日 %s", result.requested_date, result.run_day
            )

        # ---------- 1. 股票池 ----------
        pool, uni_stats = build_universe(fetcher, result.run_day, uc["prefixes"], uc["st_name_keyword"])
        result.universe_stats = uni_stats
        result.funnel["L1_股票池"] = len(pool)
        codes: List[str] = pool["code"].tolist()
        name_by_code: Dict[str, str] = dict(zip(pool["code"], pool["name"]))

        # ---------- 2. 稳定键K线（全市场，af=3 尾部追加）+ 硬性剔除 ----------
        log.info("阶段2: 稳定键增量更新 %d 只股票K线(kline_af3, af=3) ...", len(codes))
        req_before_kline = client.request_count
        snapshots: Dict[str, Optional[KlineData]] = {}
        for i, code in enumerate(codes):
            snapshots[code] = _kline_snapshot(fetcher, code, result.run_day)
            if (i + 1) % 200 == 0 or i + 1 == len(codes):
                _progress("L1_kline_af3", i + 1, len(codes))

        # ST 剔除（以日K isST=1 为准；可配置开关）
        st_set = set()
        if hfc["st_enabled"]:
            for c in codes:
                kl = snapshots[c]
                if kl is not None and (kl.is_st or 0) == 1:
                    st_set.add(c)
        result.st_excluded_count = len(st_set)

        # 上市时长剔除（全历史K线行数 >= N；可配置开关）
        short_list: set = set()
        for c in codes:
            if c in st_set:
                continue
            kl = snapshots[c]
            if kl is None or kl.n_rows < hfc["listing_min_trading_days"]:
                short_list.add(c)
        result.insufficient_kline_count = len(short_list)
        hard_pass = [c for c in codes if c not in st_set and c not in short_list]
        result.kline_requests = client.request_count - req_before_kline
        result.funnel["L2_硬剔除后"] = len(hard_pass)
        log.info(
            "阶段2完成: ST剔除 %d、上市不足%d日剔除 %d，剩余 %d（K线 BaoStock 请求 %d 次）",
            len(st_set), hfc["listing_min_trading_days"], len(short_list),
            len(hard_pass), result.kline_requests,
        )

        if not hard_pass:
            log.warning("硬剔除后无候选，输出空结果")
            raise _NoCandidates()

        # ---------- 3. 行业分类（全量，left-join） ----------
        ind_df = fetcher.industry()
        industry_map_all: Dict[str, str] = dict(zip(ind_df["code"], ind_df["industry"]))
        if "updateDate" in ind_df.columns and len(ind_df):
            result.industry_update_date = str(ind_df["updateDate"].iloc[0])

        # ---------- 4. 报告期 ----------
        period = _probe_latest_period(fetcher, run_day, fc["probe_quarters_back"])
        if period is None:
            raise RuntimeError("无法探测到任何已披露的财报季度（基准股 sh.601398）")
        py, pq = period
        result.fundamental_period = f"{py}Q{pq}"

        window_start = run_day - timedelta(days=dc["window_days"])
        result.dividend_window_start = window_start.isoformat()

        if mode == "legacy":
            _run_legacy(
                cfg, fetcher, client, result, hard_pass, name_by_code, industry_map_all,
                snapshots, tc, dc, fc, ic, uc, window_start, run_day, do_crosscheck, period,
            )
        else:
            _run_zscore(
                cfg, fetcher, client, result, hard_pass, name_by_code, industry_map_all,
                snapshots, scfg, ic, window_start, run_day, do_crosscheck,
            )

    except _NoCandidates:
        no_candidates = True

    finally:
        client.close()

    if no_candidates and not result.data_notes:
        result.data_notes = [
            f"主数据源: BaoStock，筛选运行日 {result.run_day}",
            "硬性剔除（ST/上市天数）后无候选 → 输出空结果",
        ]

    result.elapsed_seconds = time.time() - t_start
    result.baostock_requests = client.request_count
    result.cache_stats = cache.stats()
    log.info(
        "筛选完成: mode=%s 入选 %d 只, BaoStock请求 %d 次（其中K线 %d 次）, 缓存文件 %s, 用时 %.0fs",
        result.mode, _final_count(result), result.baostock_requests,
        result.kline_requests, result.cache_stats, result.elapsed_seconds,
    )
    return result


def _final_count(result: ScreenResult) -> int:
    if result.mode == "zscore":
        return sum(1 for s in result.scored if s.top_n_selected)
    try:
        return int(result.candidates["pass_all"].sum())
    except Exception:  # noqa: BLE001
        return 0


# ===========================================================================
# legacy 模式（旧四维 AND 硬过滤，原 metrics 函数不动）
# ===========================================================================

def _run_legacy(
    cfg, fetcher, client, result, hard_pass, name_by_code, industry_map_all,
    snapshots, tc, dc, fc, ic, uc, window_start, run_day, do_crosscheck, period,
) -> None:
    """旧模式：技术面幸存者 → 分红/基本面 → 四维 AND。af=1 由稳定键缓存本地重建。"""
    log.info("阶段3(legacy): 计算 %d 只候选的技术面（本地重建 af=1）...", len(hard_pass))
    max_bars = cfg["data"]["kline_calendar_days_back"] + 50  # 覆盖 MA/收益窗口的K线根数上限

    tech_results: Dict[str, TechnicalResult] = {}
    for i, code in enumerate(hard_pass):
        dates, closes = _rebuild_window(fetcher, code, max_bars)
        tr = compute_technical(
            code, dates, closes,
            ma_period=tc["ma_period"],
            return_window_days=tc["return_window_days"],
            min_return=tc["min_return"],
            max_return=tc["max_return"],
            max_vol=tc["max_vol"],
        )
        tech_results[code] = tr
        if (i + 1) % 200 == 0 or i + 1 == len(hard_pass):
            _progress("L2_technical", i + 1, len(hard_pass))

    tech_pass_codes = [c for c in hard_pass if not tech_results[c].fail_reasons]
    result.funnel["L3_技术面"] = len(tech_pass_codes)
    log.info("阶段3完成: 技术面通过 %d/%d", len(tech_pass_codes), len(hard_pass))

    cand_codes = tech_pass_codes
    years = sorted({run_day.year - 1, run_day.year})
    log.info(
        "阶段4(legacy): 拉取 %d 只候选的分红(年份%s)与基本面(%s) ...",
        len(cand_codes), years, result.fundamental_period,
    )

    div_records: Dict[str, List[Dict[str, Any]]] = {}
    fund_rows: Dict[str, Dict[str, Optional[Dict[str, Any]]]] = {}
    n_factor_refresh = 0
    for i, code in enumerate(cand_codes):
        recs: List[Dict[str, Any]] = []
        for y in years:
            recs.extend(fetcher.dividend(code, y))
        div_records[code] = recs
        if fetcher.maybe_refresh_adjfactor(code, recs):
            n_factor_refresh += 1
        fund_rows[code] = {
            "profit": fetcher.profit_data(code, period[0], period[1]),
            "growth": fetcher.growth_data(code, period[0], period[1]),
            "balance": fetcher.balance_data(code, period[0], period[1]),
        }
        if (i + 1) % 50 == 0 or i + 1 == len(cand_codes):
            _progress("L3_candidate_data", i + 1, len(cand_codes))
    log.info("候选数据拉取完成(legacy): 分红+基本面（事件驱动因子刷新触发 %d 只）", n_factor_refresh)

    div_results: Dict[str, DividendResult] = {}
    fund_results: Dict[str, FundamentalResult] = {}
    rows: List[Dict[str, Any]] = []
    for code in cand_codes:
        kl = snapshots[code]
        tr = tech_results[code]

        dv = compute_dividend_yield(
            code, div_records.get(code, []), window_start, run_day,
            kl.current_price if kl else None, dc["min_yield"],
        )
        fr = compute_fundamental(
            code, fund_rows[code]["profit"], fund_rows[code]["growth"], fund_rows[code]["balance"],
            period=result.fundamental_period,
            roe_min=fc["roe_min"], yoy_field=fc["yoy_field"],
            liability_max=fc["liability_max"], gross_margin_min=fc["gross_margin_min"],
        )
        div_results[code] = dv
        fund_results[code] = fr

        rows.append({
            "code": code,
            "name": name_by_code.get(code, ""),
            "close": kl.current_price if kl else None,
            "ma": None if tr.ma is None else round(tr.ma, 3),
            "window_return_pct": None if tr.window_return is None else round(tr.window_return * 100, 2),
            "annual_vol_pct": None if tr.annual_volatility is None else round(tr.annual_volatility * 100, 2),
            "cash_per_share": dv.cash_per_share,
            "dividend_yield_pct": None if dv.yield_pct is None else round(dv.yield_pct * 100, 3),
            "roe_pct": None if fr.roe_avg is None else round(fr.roe_avg * 100, 2),
            "yoy_net_profit_pct": None if fr.yoy_net_profit is None else round(fr.yoy_net_profit * 100, 2),
            "liability_pct": None if fr.liability_to_asset is None else round(fr.liability_to_asset * 100, 2),
            "gross_margin_pct": None if fr.gross_margin is None else round(fr.gross_margin * 100, 2),
            "pass_technical": True,
            "pass_dividend": not dv.fail_reasons,
        })

    cands = pd.DataFrame(rows)

    if cands.empty:
        raise _NoCandidates()

    cand_industry_map = {
        row["code"]: (industry_map_all.get(row["code"]) or "").strip() for row in rows
    }
    roe_map: Dict[str, Optional[float]] = {}
    for row in rows:
        v = row["roe_pct"]
        roe_map[row["code"]] = None if v is None else float(v) / 100.0
    groups = build_industry_groups(cand_industry_map)

    ind_results: Dict[str, IndustryResult] = {}
    for code, ind_name in cand_industry_map.items():
        group_codes = groups[ind_name or "无行业"]
        ind_results[code] = compute_industry_rank(
            code, group_codes, roe_map, ic["top_pct"], ic["min_group_size"], industry=ind_name,
        )

    for code in cand_codes:
        fr = fund_results[code]
        if fr.missing:
            result.missing_fundamental.append({
                "code": code,
                "name": name_by_code.get(code, ""),
                "missing": ",".join(fr.missing),
            })
    result.no_industry_codes = [c for c in cand_codes if not cand_industry_map[c]]
    result.small_groups_skipped = {
        ind: len(g) for ind, g in groups.items() if len(g) < ic["min_group_size"]
    }

    cands["industry"] = [ind_results[c].industry for c in cands["code"]]
    cands["industry_rank"] = [ind_results[c].rank for c in cands["code"]]
    cands["industry_percentile"] = [ind_results[c].percentile for c in cands["code"]]
    cands["industry_group_size"] = [ind_results[c].group_size for c in cands["code"]]
    cands["industry_group_skipped"] = [ind_results[c].group_skipped for c in cands["code"]]
    cands["pass_industry"] = [industry_pass(ind_results[c], ic["top_pct"]) for c in cands["code"]]

    fund_pass_list: List[bool] = []
    pub_dates: Dict[str, str] = {}
    for code in cands["code"]:
        fr = fund_results[code]
        fund_pass_list.append(not fr.fail_reasons and not fr.missing)
        p = fund_rows[code]["profit"]
        if p and p.get("pubDate"):
            pub_dates[code] = p["pubDate"]
    cands["pass_fundamental"] = fund_pass_list
    result.fundamental_pub_dates = pub_dates

    cands["pass_all"] = (
        cands["pass_dividend"] & cands["pass_industry"] & cands["pass_fundamental"]
    )

    n_after_div = int(cands["pass_dividend"].sum())
    n_after_ind = int((cands["pass_dividend"] & cands["pass_industry"]).sum())
    n_final = int(cands["pass_all"].sum())
    result.funnel.update({
        "L4_股息率": n_after_div,
        "L5_行业排名": n_after_ind,
        "L6_最终入选": n_final,
    })

    cands = cands.sort_values(
        ["pass_all", "dividend_yield_pct"], ascending=[False, False]
    ).reset_index(drop=True)
    result.candidates = cands

    _legacy_data_notes(cfg, result, window_start)

    if do_crosscheck and n_final > 0:
        _crosscheck(cfg, fetcher, result, cands.loc[cands["pass_all"], "code"].tolist())


def _legacy_data_notes(cfg, result, window_start) -> None:
    result.data_notes = [
        f"主数据源: BaoStock（日K/分红/季报/行业/股票列表），筛选运行日 {result.run_day}",
        "模式=legacy（旧四维AND硬过滤）；技术面基于后复权日K（稳定键缓存本地重建 af=1）",
        f"基本面报告期: {result.fundamental_period}（基准股逐季探测的最近披露期）；ROE 为报告期累计口径、未年化",
        f"行业分类: 证监会行业分类（query_stock_industry 全量快照 updateDate={result.industry_update_date or '未知'}），按 code left-join，空行业归入'无行业'",
        f"股息率窗口: [{window_start.isoformat()}, {result.run_day}]；已除权(dividOperateDate)分红按(code,除权日)去重后求和 ÷ 不复权收盘价",
    ]


# ===========================================================================
# zscore 模式（v2 多因子打分）
# ===========================================================================

def _run_zscore(
    cfg, fetcher, client, result, hard_pass, name_by_code, industry_map_all,
    snapshots, scfg, ic, window_start, run_day, do_crosscheck,
) -> None:
    """v2：硬剔除后全体候选 → 因子计算 → 截面 Z-Score → Top N。"""
    annual_year = _resolve_annual_year(fetcher, run_day)
    result.annual_year = annual_year
    roe_years = [annual_year - 2, annual_year - 1, annual_year]  # ROE 近3年（年度Q4）

    log.info(
        "阶段3(zscore): 计算 %d 只候选的技术面因子（本地重建 af=1，0 网络请求）...", len(hard_pass)
    )
    max_bars = cfg["data"]["kline_calendar_days_back"] + 50
    tech_factors: Dict[str, Dict[str, Optional[float]]] = {}
    for i, code in enumerate(hard_pass):
        _, closes = _rebuild_window(fetcher, code, max_bars)
        tech_factors[code] = _tech_factors(closes)
        if (i + 1) % 500 == 0 or i + 1 == len(hard_pass):
            _progress("L2_tech_factors", i + 1, len(hard_pass))

    # ---- 行业分组（全体候选，证监会口径）----
    cand_industry_map: Dict[str, str] = {
        c: (industry_map_all.get(c) or "").strip() for c in hard_pass
    }
    groups = build_industry_groups(cand_industry_map)
    result.no_industry_codes = [c for c in hard_pass if not cand_industry_map[c]]
    result.small_groups_skipped = {
        ind: len(g) for ind, g in groups.items() if len(g) < ic["min_group_size"]
    }

    # ---- 候选数据拉取（分红 + 基本面，年度Q4口径）----
    div_years = sorted({run_day.year - 1, run_day.year, annual_year})
    log.info(
        "阶段4(zscore): 拉取 %d 只候选的分红(年份%s)与基本面(%dQ4基准) ...",
        len(hard_pass), div_years, annual_year,
    )

    div_records: Dict[str, List[Dict[str, Any]]] = {}
    fund_rows: Dict[str, Dict[str, Optional[Dict[str, Any]]]] = {}
    n_factor_refresh = 0
    for i, code in enumerate(hard_pass):
        recs: List[Dict[str, Any]] = []
        for y in div_years:
            recs.extend(fetcher.dividend(code, y))
        div_records[code] = recs
        # 事件驱动因子刷新：仅新除权事件触发（绝大多数股票 0 次查询）
        if fetcher.maybe_refresh_adjfactor(code, recs):
            n_factor_refresh += 1
        fund_rows[code] = {
            "profit_cur": fetcher.profit_data(code, annual_year, 4),
            "profit_prior": fetcher.profit_data(code, annual_year - 1, 4),
            "profit_oldest": fetcher.profit_data(code, annual_year - 2, 4),
            "growth_cur": fetcher.growth_data(code, annual_year, 4),
            "balance_cur": fetcher.balance_data(code, annual_year, 4),
            "balance_prior": fetcher.balance_data(code, annual_year - 1, 4),
            "cashflow_cur": fetcher.cashflow_data(code, annual_year, 4),
        }
        if (i + 1) % 100 == 0 or i + 1 == len(hard_pass):
            _progress("L3_candidate_data", i + 1, len(hard_pass))
    log.info("候选数据拉取完成: 分红+基本面（事件驱动因子刷新触发 %d 只）", n_factor_refresh)

    # ---- 逐股因子计算（纯函数）----
    log.info("阶段5(zscore): 计算四维因子 ...")
    stocks: List[Dict[str, Any]] = []
    missing_by_code: Dict[str, List[str]] = {}
    pub_dates: Dict[str, str] = {}

    for i, code in enumerate(hard_pass):
        fr_ = fund_rows[code]
        kl = snapshots[code]
        close_af3 = kl.current_price if kl else None

        # 股息维度
        ttm_y = ttm_dividend_yield(
            div_records.get(code, []), window_start, run_day, close_af3
        )
        cash_annual, _ = dedup_dividends(
            div_records.get(code, []),
            date(annual_year, 1, 1), date(annual_year, 12, 31),
        )
        p_cur = fr_["profit_cur"] or {}
        payout = payout_ratio(
            cash_annual if cash_annual > 0 else None,
            _f(p_cur.get("totalShare")), _f(p_cur.get("netProfit")),
        )

        # 行业维度（证监会二级；min_group_size 语义沿用：小组跳过排名→None）
        roe_level = _f((fr_["profit_cur"] or {}).get("roeAvg"))
        yoy_pni = _f((fr_["growth_cur"] or {}).get("YOYPNI"))

        # 基本面维度
        roe_vals = [
            _f((fr_["profit_oldest"] or {}).get("roeAvg")),
            _f((fr_["profit_prior"] or {}).get("roeAvg")),
            roe_level,
        ]
        roe_mean, roe_std = roe_stability(roe_vals)
        piot = piotroski_fscore(
            code,
            profit_cur=fr_["profit_cur"], profit_prior=fr_["profit_prior"],
            balance_cur=fr_["balance_cur"], balance_prior=fr_["balance_prior"],
            growth_cur=fr_["growth_cur"], cashflow_cur=fr_["cashflow_cur"],
        )
        liability = _f((fr_["balance_cur"] or {}).get("liabilityToAsset"))
        gross_margin = _f(p_cur.get("gpMargin"))

        if p_cur.get("pubDate"):
            pub_dates[code] = str(p_cur["pubDate"])

        # 缺失因子记录（金融业 gpMargin/currentRatio 空属正常 → N/A）
        missing: List[str] = []
        for fname, val in (
            ("fundamental.gross_margin", gross_margin),
            ("fundamental.roe_level", roe_level),
            ("fundamental.liability", liability),
            ("dividend.ttm_yield", ttm_y),
            ("dividend.payout_ratio", payout),
        ):
            if val is None:
                missing.append(fname)
        if piot.n_na:
            missing.append(f"piotroski_NA({';'.join(piot.na_signals)})")
        missing_by_code[code] = missing

        stocks.append({
            "code": code,
            "factors": {
                "technical": tech_factors[code],
                "dividend": {"ttm_yield": ttm_y, "payout_ratio": payout},
                # 行业分位在下面统一填充（需要全体候选的组内映射）
                "industry": {},
                "fundamental": {
                    "roe_level": roe_level,
                    "roe_stability": None if roe_std is None else -roe_std,  # 低波动(稳定)越好→取负
                    "low_liability": None if liability is None else -liability,  # 低负债越好→取负
                    "gross_margin": gross_margin,
                    "piotroski": piot.ratio,
                },
            },
            "_aux": {
                "close_af3": close_af3,
                "ttm_y": ttm_y, "payout": payout,
                "roe_level": roe_level, "roe_mean": roe_mean, "roe_std": roe_std,
                "yoy_pni": yoy_pni, "liability": liability, "gross_margin": gross_margin,
                "piot": piot, "industry": cand_industry_map[code],
            },
        })
        if (i + 1) % 500 == 0 or i + 1 == len(hard_pass):
            _progress("L4_factor_compute", i + 1, len(hard_pass))

    # ---- 行业组内分位（全体候选，O(N)）----
    roe_map_all: Dict[str, Optional[float]] = {s["code"]: s["_aux"]["roe_level"] for s in stocks}
    yoy_map_all: Dict[str, Optional[float]] = {s["code"]: s["_aux"]["yoy_pni"] for s in stocks}
    for s in stocks:
        code = s["code"]
        ind_name = s["_aux"]["industry"] or "无行业"
        group_codes = groups[ind_name]
        if len(group_codes) < ic["min_group_size"]:
            # 小组跳过排名（min_group_size 语义沿用）→ 因子缺失
            s["factors"]["industry"] = {"roe_rank_pct": None, "yoy_pni_rank_pct": None}
        else:
            _, roe_pct = rank_percentile(code, group_codes, roe_map_all)
            _, yoy_pct = rank_percentile(code, group_codes, yoy_map_all)
            # 分位越小越好 → 打分值取 100-pct（越大越好）；CSV 保留原始 pct 列
            s["factors"]["industry"] = {
                "roe_rank_pct": None if roe_pct is None else 100.0 - roe_pct,
                "yoy_pni_rank_pct": None if yoy_pct is None else 100.0 - yoy_pct,
            }
        s["_aux"]["roe_rank_pct_raw"] = (
            None if len(groups[ind_name]) < ic["min_group_size"]
            else rank_percentile(code, group_codes, roe_map_all)[1]
        )
        s["_aux"]["yoy_rank_pct_raw"] = (
            None if len(groups[ind_name]) < ic["min_group_size"]
            else rank_percentile(code, group_codes, yoy_map_all)[1]
        )

    # ---- 截面打分 ----
    log.info("阶段6(zscore): 截面 Z-Score 打分（%d 只候选）...", len(stocks))
    scored = score_cross_section(
        stocks, scfg["weights"], scfg["sub_weights"], scfg["top_n"], scfg["missing_policy"]
    )
    result.scored = scored
    result.factor_means = dimension_means(stocks)

    # ---- 组装 candidates DataFrame（v2 列 + legacy 兼容列）----
    aux_by_code = {s["code"]: s["_aux"] for s in stocks}
    rows: List[Dict[str, Any]] = []
    for sc in scored:
        code = sc.code
        a = aux_by_code[code]
        piot: PiotroskiResult = a["piot"]
        rows.append({
            "code": code,
            "name": name_by_code.get(code, ""),
            "industry": a["industry"] or "",
            "close": a["close_af3"],
            # 四维原始因子值（badge/复核）
            "ma_bullish": _i01(sc.raw["technical"].get("ma_bullish")),
            "window_return_pct": _pct2(sc.raw["technical"].get("window_return")),
            "annual_vol_pct": (
                None if sc.raw["technical"].get("low_vol") is None
                else round(-float(sc.raw["technical"]["low_vol"]) * 100.0, 2)
            ),
            "rsi14": _f2(sc.raw["technical"].get("rsi")),
            "macd_golden_cross": (
                None if sc.raw["technical"].get("macd") is None
                else int(sc.raw["technical"]["macd"])
            ),
            "ttm_dividend_yield_pct": _pct3(a["ttm_y"]),
            "payout_ratio_pct": _pct2(a["payout"]),
            "industry_roe_rank_pct": a.get("roe_rank_pct_raw"),
            "industry_yoy_pni_rank_pct": a.get("yoy_rank_pct_raw"),
            "roe_pct": _pct2(a["roe_level"]),
            "roe_3y_mean_pct": _pct2(a["roe_mean"]),
            "roe_3y_std_pct": _pct2(a["roe_std"]),
            "liability_pct": _pct2(a["liability"]),
            "gross_margin_pct": _pct2(a["gross_margin"]),
            "piotroski_fscore": piot.fscore,
            "piotroski_valid": piot.n_valid,
            # 四维 Z-Score 与维度分
            "z_technical": _f4(sc.z_dims.get("technical")),
            "z_dividend": _f4(sc.z_dims.get("dividend")),
            "z_industry": _f4(sc.z_dims.get("industry")),
            "z_fundamental": _f4(sc.z_dims.get("fundamental")),
            "score_technical": _f4(sc.scores.get("technical")),
            "score_dividend": _f4(sc.scores.get("dividend")),
            "score_industry": _f4(sc.scores.get("industry")),
            "score_fundamental": _f4(sc.scores.get("fundamental")),
            "total_score": round(sc.total_score, 4),
            "rank": sc.rank,
            "top_n_selected": sc.top_n_selected,
            "na_factors": ",".join(sc.na_factors),
            # legacy 兼容列（zscore 模式下 pass_* 按旧硬规则评估，仅供对照）
            "pass_technical": _legacy_pass_tech(sc.raw["technical"], cfg),
            "pass_dividend": (a["ttm_y"] is not None and a["ttm_y"] >= cfg["dividend"]["min_yield_pct"] / 100.0),
            "pass_industry": (a.get("roe_rank_pct_raw") is not None
                              and a["roe_rank_pct_raw"] <= cfg["industry"]["top_pct"]),
            "pass_fundamental": _legacy_pass_fund(a, cfg),
            "pass_all": sc.top_n_selected,  # v2 入选 = Top N（web 兼容字段）
        })

    cands = pd.DataFrame(rows)
    result.candidates = cands
    result.funnel["L3_打分候选"] = len(stocks)
    result.funnel["L4_TopN入选"] = sum(1 for s in scored if s.top_n_selected)
    result.fundamental_pub_dates = pub_dates

    # 缺失名单（金融业 N/A 因子标注）
    for code in hard_pass:
        if missing_by_code.get(code):
            result.missing_fundamental.append({
                "code": code,
                "name": name_by_code.get(code, ""),
                "missing": ",".join(missing_by_code[code]),
            })

    _zscore_data_notes(cfg, result, window_start)

    if do_crosscheck and result.funnel["L4_TopN入选"] > 0:
        top_codes = [s.code for s in scored if s.top_n_selected]
        _crosscheck(cfg, fetcher, result, top_codes)


def _legacy_pass_tech(tech_raw: Dict[str, Optional[float]], cfg) -> bool:
    """zscore 模式下 legacy 兼容列：按旧硬规则评估技术面（MA20>MA60 近似 + 收益/波动区间）。"""
    if tech_raw.get("ma_bullish") is None:
        return False
    wr = tech_raw.get("window_return")
    lv = tech_raw.get("low_vol")
    vol = None if lv is None else -float(lv)
    t = cfgmod.tech(cfg)
    if wr is None or not (t["min_return"] <= wr <= t["max_return"]):
        return False
    if vol is None or not (vol < t["max_vol"]):
        return False
    return tech_raw["ma_bullish"] == 1.0


def _legacy_pass_fund(a: Dict[str, Any], cfg) -> bool:
    f = cfgmod.fundamental_cfg(cfg)
    checks = [
        a.get("roe_level") is not None and a["roe_level"] >= f["roe_min"],
        (a.get("yoy_pni") or -1.0) > 0,
        a.get("liability") is not None and a["liability"] <= f["liability_max"],
        a.get("gross_margin") is not None and a["gross_margin"] > f["gross_margin_min"],
    ]
    return all(checks)


def _zscore_data_notes(cfg, result, window_start) -> None:
    sc = cfg["scoring"]
    w = sc["weights"]
    result.data_notes = [
        f"主数据源: BaoStock（日K/分红/季报/行业/股票列表），筛选运行日 {result.run_day}",
        "模式=zscore（截面Z-Score多因子打分）；后复权af=1由稳定键缓存(kline_af3×adjfactor)本地重建，"
        "历史不可变+尾部追加（新除权事件不改变事件日之前的值）",
        f"打分: 硬剔除后全体候选上 z=(x-mean)/std（样本标准差n-1, std=0→z=0）；"
        f"缺失因子 {sc['missing_policy']}（z=0且按可用权重归一化）；total_score=Σweight×z_dim",
        f"四维权重: technical={w['technical']} / dividend={w['dividend']} / "
        f"industry={w['industry']} / fundamental={w['fundamental']}（来自 strategy.yaml scoring 段）",
        f"行业=证监会二级分类（query_stock_industry，updateDate={result.industry_update_date or '未知'}，83组）；"
        "行业内排名分位在证监会二级组内计算，min_group_size 不足的小组跳过排名（因子记缺失）",
        f"Piotroski F-Score: S2/S3/S5/S9 为比率代理口径（BaoStock 只给比率不给绝对值）；"
        "金融业 S6/S8 字段为空 → N/A 不计入分母，F=有效信号和/有效数",
        f"基本面基准年度: {result.annual_year}Q4（年报）；ROE稳定性=近3个年度Q4 roeAvg 的 mean/std（总体标准差）",
        f"股息率窗口(TTM): [{window_start.isoformat()}, {result.run_day}]；已除权分红按(code,除权日)去重 ÷ 不复权收盘价(af=3)",
        "因子方向约定: low_vol/roe_stability/low_liability 取负值（越低越好）、行业分位取100-pct（越小越好）"
        "→ 打分值统一为越大越好；CSV 中 *_pct 列为原始口径",
        "pass_* 兼容列按 legacy v1 硬规则评估（仅供对照）；v2 入选 = total_score Top N（pass_all=top_n_selected）",
    ]


def _crosscheck(cfg, fetcher, result, final_codes: List[str]) -> None:
    """腾讯交叉验证（可选，不进主计算路径）。"""
    from .data.tencent import TencentClient
    xc = cfgmod.crosscheck_cfg(cfg)
    if not xc["enabled"]:
        return
    sample = final_codes[: xc["sample_size"]]
    try:
        tencent = TencentClient()
        quotes = tencent.fetch(sample, batch_size=xc["batch_size"])
        for code in sample:
            row = result.candidates.loc[result.candidates["code"] == code].iloc[0]
            q = quotes.get(code)
            bs_close = row["close"]
            if q is None or q.get("price") is None:
                result.crosscheck.append({
                    "code": code, "name": row["name"], "bs_close": bs_close,
                    "tencent_price": None, "diff_pct": None, "ok": False,
                    "note": "腾讯未返回",
                })
                continue
            diff = abs(q["price"] - bs_close) / bs_close if bs_close else None
            ok = diff is not None and diff <= xc["price_tolerance_pct"]
            result.crosscheck.append({
                "code": code, "name": row["name"], "bs_close": bs_close,
                "tencent_price": q["price"],
                "diff_pct": None if diff is None else round(diff * 100, 3),
                "ok": ok, "note": "",
            })
    except Exception as exc:  # noqa: BLE001 - 交叉验证失败不影响主流程
        log.warning("腾讯交叉验证失败（不影响主结果）: %s", exc)
        result.data_notes.append(f"腾讯交叉验证失败: {exc}")


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _f(v: Any) -> Optional[float]:
    """任意值 → float（空串/None/非法 → None）。"""
    if v is None:
        return None
    try:
        s = str(v).strip()
        return float(s) if s else None
    except (TypeError, ValueError):
        return None


def _f2(v: Optional[float]) -> Optional[float]:
    return None if v is None else round(v, 2)


def _f4(v: Optional[float]) -> Optional[float]:
    return None if v is None else round(v, 4)


def _pct2(v: Optional[float]) -> Optional[float]:
    """小数 → 百分数（2位）。"""
    return None if v is None else round(v * 100.0, 2)


def _pct3(v: Optional[float]) -> Optional[float]:
    return None if v is None else round(v * 100.0, 3)


def _i01(v: Optional[float]) -> Optional[int]:
    return None if v is None else int(v)
