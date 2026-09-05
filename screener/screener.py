# -*- coding: utf-8 -*-
"""筛选引擎：编排 股票池 → 技术面 → 股息率 → 行业排名 → 基本面 五层漏斗。

设计决策（已在 README 注明）：
- 技术面之后的三个维度（股息率/行业/基本面）对**全部技术面幸存者**独立计算并
  给出各自的通过与否；最终入选 = 四维全过。报告中的"漏斗"按累计交集展示，
  便于定位每只股票卡在哪一层。
- 行业排名在"技术面幸存者"集合内分组（组=证监会行业；空行业→"无行业"），
  组内按最近披露报告期 ROE 降序，保留前 top_pct；不足 min_group_size 的组跳过
  排名约束并在报告注明。
- BaoStock 是单 socket 串行接口（多进程并发登录实测会挂起）→ 全市场拉取为
  顺序循环 + 进度日志；首次运行约 1.5~2 小时属正常，之后全部命中本地缓存。
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
    TechnicalResult,
    build_industry_groups,
    compute_dividend_yield,
    compute_fundamental,
    compute_industry_rank,
    compute_technical,
    industry_pass,
)
from .universe import UniverseStats, build_universe

log = logging.getLogger("screener.engine")


@dataclass
class ScreenResult:
    """一次完整筛选的结果（供 report 层渲染）。"""

    run_day: str = ""                  # 实际筛选的交易日（定位后填充）
    requested_date: str = ""           # 用户传入的日期（可能与 run_day 不同=回退）
    date_fallback: bool = False       # 是否发生了"非交易日→最近交易日"回退
    universe_stats: Optional[UniverseStats] = None

    # 每只技术面幸存者的完整指标
    candidates: pd.DataFrame = field(default_factory=pd.DataFrame)

    # 各层数量（漏斗）
    funnel: Dict[str, int] = field(default_factory=dict)

    # 缺失/异常名单
    missing_fundamental: List[Dict[str, str]] = field(default_factory=list)  # [{code,name,missing}]
    no_industry_codes: List[str] = field(default_factory=list)
    small_groups_skipped: Dict[str, int] = field(default_factory=dict)       # {行业: 组大小}
    st_excluded_count: int = 0
    insufficient_kline_count: int = 0

    # 数据时间戳与来源
    data_notes: List[str] = field(default_factory=list)
    industry_update_date: str = ""
    fundamental_period: Optional[str] = None      # "2026Q2"
    fundamental_pub_dates: Dict[str, str] = field(default_factory=dict)  # code→pubDate
    dividend_window_start: str = ""               # 股息率窗口起点（ISO 日期）

    # 交叉验证（腾讯）
    crosscheck: List[Dict[str, Any]] = field(default_factory=list)

    # 耗时与请求统计
    elapsed_seconds: float = 0.0
    baostock_requests: int = 0
    cache_stats: Dict[str, int] = field(default_factory=dict)


class _NoCandidates(Exception):
    """内部哨兵：技术面无幸存者，跳过后续数据拉取（结果已在 result 中置空）。"""


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

    client = BaoStockClient(max_attempts=datac["retry_max_attempts"])
    cache = DiskCache(datac["cache_dir"])
    fetcher = DataFetcher(client, cache)
    result = ScreenResult(requested_date=requested_date.isoformat())

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

        # ---------- 2. 技术面 ----------
        # 2a. 运行日单根K线（af=3 不复权）：当前价 + isST + tradestatus
        log.info("阶段2a: 拉取 %d 只股票运行日K线(af=3) ...", len(codes))
        run_day_kl: Dict[str, KlineData] = {}
        for i, code in enumerate(codes):
            run_day_kl[code] = fetcher.kline_run_day(code, result.run_day)
            if (i + 1) % 200 == 0:
                log.info("  运行日K线进度 %d/%d", i + 1, len(codes))

        # ST 剔除（以日K isST=1 为准；名称含ST仅作辅助标记）
        st_set = {c for c in codes if (run_day_kl[c].is_st or 0) == 1}
        result.st_excluded_count = len(st_set)
        non_st = [c for c in codes if c not in st_set]
        log.info("阶段2a完成: ST剔除 %d 只（日K isST=1），剩余 %d", len(st_set), len(non_st))

        # 2b. 后复权窗口K线（af=1）：MA/收益/波动率/上市时长
        kline_start = (run_day - timedelta(days=datac["kline_calendar_days_back"])).isoformat()
        log.info(
            "阶段2b: 拉取 %d 只股票后复权窗口K线(%s ~ %s, af=1) ...",
            len(non_st), kline_start, result.run_day,
        )
        tech_results: Dict[str, TechnicalResult] = {}
        for i, code in enumerate(non_st):
            dates, closes, status = fetcher.kline_window(code, kline_start, result.run_day, "1")
            tr = compute_technical(
                code, dates, closes,
                ma_period=tc["ma_period"],
                return_window_days=tc["return_window_days"],
                min_return=tc["min_return"],
                max_return=tc["max_return"],
                max_vol=tc["max_vol"],
            )
            # 上市时长：窗口内K线行数（BaoStock 对停牌日也返回行）
            if tr.n_trading_days < uc["listing_min_trading_days"]:
                tr.fail_reasons.append(
                    f"上市不足{uc['listing_min_trading_days']}个交易日(窗口内仅{tr.n_trading_days}根)"
                )
            tech_results[code] = tr
            if (i + 1) % 200 == 0:
                log.info("  窗口K线进度 %d/%d", i + 1, len(non_st))

        tech_pass_codes = [c for c in non_st if not tech_results[c].fail_reasons]
        result.insufficient_kline_count = sum(
            1 for c in non_st
            if any("上市不足" in r or "K线不足" in r for r in tech_results[c].fail_reasons)
        )
        result.funnel["L2_技术面"] = len(tech_pass_codes)
        log.info("阶段2完成: 技术面通过 %d/%d", len(tech_pass_codes), len(non_st))

        # ---------- 3. 行业分类（全量，left-join） ----------
        ind_df = fetcher.industry()
        industry_map_all: Dict[str, str] = dict(zip(ind_df["code"], ind_df["industry"]))
        if "updateDate" in ind_df.columns and len(ind_df):
            result.industry_update_date = str(ind_df["updateDate"].iloc[0])

        # ---------- 4. 最近披露报告期探测 ----------
        period = _probe_latest_period(fetcher, run_day, fc["probe_quarters_back"])
        if period is None:
            raise RuntimeError("无法探测到任何已披露的财报季度（基准股 sh.601398）")
        py, pq = period
        result.fundamental_period = f"{py}Q{pq}"

        # ---------- 5. 技术面幸存者：分红 + 基本面数据 ----------
        cand_codes = tech_pass_codes
        years = sorted({run_day.year - 1, run_day.year})  # 窗口涉及的自然年（通常2个）
        log.info(
            "阶段3: 拉取 %d 只候选的分红(年份%s)与基本面(%s) ...",
            len(cand_codes), years, result.fundamental_period,
        )

        div_records: Dict[str, List[Dict[str, Any]]] = {}
        fund_rows: Dict[str, Dict[str, Optional[Dict[str, Any]]]] = {}
        for i, code in enumerate(cand_codes):
            recs: List[Dict[str, Any]] = []
            for y in years:
                recs.extend(fetcher.dividend(code, y))
            div_records[code] = recs
            fund_rows[code] = {
                "profit": fetcher.profit_data(code, py, pq),
                "growth": fetcher.growth_data(code, py, pq),
                "balance": fetcher.balance_data(code, py, pq),
            }
            if (i + 1) % 50 == 0:
                log.info("  候选数据进度 %d/%d", i + 1, len(cand_codes))

        # ---------- 6. 各维度计算（每只股票各算一次） ----------
        window_start = run_day - timedelta(days=dc["window_days"])
        result.dividend_window_start = window_start.isoformat()

        div_results: Dict[str, DividendResult] = {}
        fund_results: Dict[str, FundamentalResult] = {}
        rows: List[Dict[str, Any]] = []
        for code in cand_codes:
            kl = run_day_kl[code]
            tr = tech_results[code]

            dv = compute_dividend_yield(
                code, div_records.get(code, []), window_start, run_day,
                kl.current_price, dc["min_yield"],
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
                "close": kl.current_price,
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

        # 边界：技术面无幸存者 → 空结果直接输出（报告注明），不崩溃
        if cands.empty:
            empty_cols = ["code", "name", "close", "ma", "window_return_pct", "annual_vol_pct",
                          "cash_per_share", "dividend_yield_pct", "roe_pct", "yoy_net_profit_pct",
                          "liability_pct", "gross_margin_pct", "pass_technical", "pass_dividend",
                          "industry", "industry_rank", "industry_percentile", "industry_group_size",
                          "industry_group_skipped", "pass_industry", "pass_fundamental", "pass_all"]
            result.candidates = pd.DataFrame(columns=empty_cols)
            result.funnel.update({"L3_股息率": 0, "L4_行业排名": 0, "L5_最终入选": 0})
            log.warning("技术面无幸存者，输出空结果")
            raise _NoCandidates()  # 跳过后续拉取与计算

        # 行业排名（在技术面幸存者集合内分组）
        cand_industry_map: Dict[str, str] = {
            r["code"]: (industry_map_all.get(r["code"]) or "").strip() for _, r in cands.iterrows()
        }
        roe_map: Dict[str, Optional[float]] = {
            r["code"]: (None if pd.isna(r["roe_pct"]) else r["roe_pct"] / 100.0)
            for _, r in cands.iterrows()
        }
        groups = build_industry_groups(cand_industry_map)

        ind_results: Dict[str, IndustryResult] = {}
        for code, ind_name in cand_industry_map.items():
            group_codes = groups[ind_name or "无行业"]
            ind_results[code] = compute_industry_rank(
                code, group_codes, roe_map, ic["top_pct"], ic["min_group_size"], industry=ind_name,
            )

        # 缺失/异常名单
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

        # 行业维度结果合并进 cands
        cands["industry"] = [ind_results[c].industry for c in cands["code"]]
        cands["industry_rank"] = [ind_results[c].rank for c in cands["code"]]
        cands["industry_percentile"] = [ind_results[c].percentile for c in cands["code"]]
        cands["industry_group_size"] = [ind_results[c].group_size for c in cands["code"]]
        cands["industry_group_skipped"] = [ind_results[c].group_skipped for c in cands["code"]]
        cands["pass_industry"] = [industry_pass(ind_results[c], ic["top_pct"]) for c in cands["code"]]

        # 基本面维度：任一指标缺失或不达标 → 不通过
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

        # 最终入选 = 四维全过（技术面幸存者集合内）
        cands["pass_all"] = (
            cands["pass_dividend"] & cands["pass_industry"] & cands["pass_fundamental"]
        )

        # ---------- 7. 漏斗（累计交集口径） ----------
        n_after_div = int(cands["pass_dividend"].sum())
        n_after_ind = int((cands["pass_dividend"] & cands["pass_industry"]).sum())
        n_final = int(cands["pass_all"].sum())
        result.funnel.update({
            "L3_股息率": n_after_div,
            "L4_行业排名": n_after_ind,
            "L5_最终入选": n_final,
        })

        # 排序：入选优先，其次股息率降序，便于阅读
        cands = cands.sort_values(
            ["pass_all", "dividend_yield_pct"], ascending=[False, False]
        ).reset_index(drop=True)
        result.candidates = cands

        # ---------- 8. 数据说明 ----------
        result.data_notes = [
            f"主数据源: BaoStock（日K/分红/季报/行业/股票列表），筛选运行日 {result.run_day}",
            "技术面基于后复权日K(adjustflag=1)；当前价与股息率分母用不复权收盘价(adjustflag=3)",
            f"基本面报告期: {result.fundamental_period}（基准股逐季探测的最近披露期）；ROE 为报告期累计口径、未年化",
            f"行业分类: 证监会行业分类（query_stock_industry 全量快照 updateDate={result.industry_update_date or '未知'}），按 code left-join，空行业归入'无行业'",
            f"股息率窗口: [{window_start.isoformat()}, {result.run_day}]；已除权(dividOperateDate)分红按(code,除权日)去重后求和 ÷ 不复权收盘价",
        ]

        # ---------- 9. 腾讯交叉验证（可选，不进主计算路径） ----------
        if do_crosscheck and n_final > 0:
            from .data.tencent import TencentClient
            xc = cfgmod.crosscheck_cfg(cfg)
            if xc["enabled"]:
                final_codes = cands.loc[cands["pass_all"], "code"].tolist()
                sample = final_codes[: xc["sample_size"]]
                try:
                    tencent = TencentClient()
                    quotes = tencent.fetch(sample, batch_size=xc["batch_size"])
                    for code in sample:
                        row = cands.loc[cands["code"] == code].iloc[0]
                        q = quotes.get(code)
                        if q is None or q.get("price") is None:
                            result.crosscheck.append({
                                "code": code, "name": row["name"], "bs_close": row["close"],
                                "tencent_price": None, "diff_pct": None, "ok": False,
                                "note": "腾讯未返回",
                            })
                            continue
                        diff = abs(q["price"] - row["close"]) / row["close"] if row["close"] else None
                        ok = diff is not None and diff <= xc["price_tolerance_pct"]
                        result.crosscheck.append({
                            "code": code, "name": row["name"], "bs_close": row["close"],
                            "tencent_price": q["price"],
                            "diff_pct": None if diff is None else round(diff * 100, 3),
                            "ok": ok, "note": "",
                        })
                except Exception as exc:  # noqa: BLE001 - 交叉验证失败不影响主流程
                    log.warning("腾讯交叉验证失败（不影响主结果）: %s", exc)
                    result.data_notes.append(f"腾讯交叉验证失败: {exc}")

    except _NoCandidates:
        no_candidates = True

    finally:
        client.close()

    if no_candidates and not result.data_notes:
        result.data_notes = [
            f"主数据源: BaoStock，筛选运行日 {result.run_day}",
            "技术面（MA/收益区间/波动率/上市时长）无幸存者 → 输出空结果",
        ]

    result.elapsed_seconds = time.time() - t_start
    result.baostock_requests = client.request_count
    result.cache_stats = cache.stats()
    log.info(
        "筛选完成: 最终入选 %d 只, BaoStock请求 %d 次, 缓存文件 %s, 用时 %.0fs",
        result.funnel.get("L5_最终入选", 0), result.baostock_requests,
        result.cache_stats, result.elapsed_seconds,
    )
    return result
