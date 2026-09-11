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
import re
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

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
    annual_dps_from_em,
    build_industry_groups,
    compute_dividend_yield,
    compute_fundamental,
    compute_industry_rank,
    compute_macd,
    compute_technical,
    consecutive_div_years,
    dedup_dividends,
    dividend_yield_percentile,
    div_stability_cv,
    em_dividend_records,
    fcf_coverage,
    fcf_coverage_proxy,
    industry_pass,
    macd_golden_cross,
    new_stock_div_ok,
    payout_ratio,
    piotroski_fscore,
    rank_percentile,
    roe_stability,
    rsi_wilder,
    soe_basis,
    soe_flag,
    ttm_dividend_yield,
    yield_spread,
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

    # ---- v5（TL D1/D3/D8，报告层消费；v4 路径保持空，零回归）----
    soe_flag_map: Dict[str, Optional[str]] = field(default_factory=dict)   # code → 'soe'/None（Round-2 D3'：新浪双规则）
    soe_basis_map: Dict[str, str] = field(default_factory=dict)            # code → 判定依据（国有股本性质/关键词命中(x)/''；报告列，人工复核）
    soe_review_list: List[Dict[str, str]] = field(default_factory=list)    # SOE 剔除股复核清单 [{code,name}]（D3'：双规则皆无 → 剔除+单列）
    reinvest_cols: Dict[str, Dict[str, Optional[float]]] = field(default_factory=dict)
    # code → {ref_price_4pct(DPS/4%), ttm_yield_pctile(0-100), ttm_yield_pctile_n(样本年数)}
    ttm_crosscheck: List[Dict[str, Any]] = field(default_factory=list)     # ttm_yield vs 腾讯 idx64 抽样 [{code, ours, tencent, diff_pct, ok}]
    holders_end_date: str = ""                                              # 前十大股东报告期（展示用）
    total_mv_yi_map: Dict[str, float] = field(default_factory=dict)        # code → 总市值(亿元)（v5 市值过滤时填充）

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

        # ---------- 3b. v5 硬过滤（TL D1/D2/D3；config 未启用时原样返回，v4 零回归） ----------
        set_div_fetcher(fetcher)  # _div_history_ok 读 kline_af3 首行（IPO 年）用
        hard_pass = _v5_hard_filter(
            cfg, fetcher, result, hard_pass, name_by_code, industry_map_all,
            uc, hfc, datac, run_day,
        )
        if not hard_pass:
            log.warning("v5 硬过滤后无候选，输出空结果")
            raise _NoCandidates()

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
    except Exception as exc:  # noqa: BLE001 - 失败守卫：附运行日后原样上抛（不吞异常）
        # 把已解析的交易日附到异常对象上，供 CLI 写 failed sidecar 时命名
        # （与 result_*.csv 命名口径一致；定位前失败 → run_day 为空 → 用请求日期）。
        if not getattr(exc, "run_day", None):
            setattr(exc, "run_day", result.run_day or None)
        raise
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


def _kline_first_date(fetcher: DataFetcher, code: str) -> Optional[str]:
    """kline_af3 缓存**首行**日期（IPO 年代理；头部字节读，不加载全历史）。"""
    path = fetcher.cache._path(fetcher._kline_af3_key(code))
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096).decode("utf-8", errors="replace")
    except OSError:
        return None
    for ln in head.splitlines():
        s = ln.strip()
        if not s or s.startswith("stock-screener-cache") or s.startswith("date,"):
            continue  # 哨兵 / 表头
        d = s.split(",")[0].strip()
        return d[:10] if len(d) >= 10 else None
    return None


def _v5_hard_filter(
    cfg: Dict[str, Any], fetcher: DataFetcher, result: ScreenResult,
    hard_pass: List[str], name_by_code: Dict[str, str], industry_map_all: Dict[str, str],
    uc: Dict[str, Any], hfc: Dict[str, Any], datac: Dict[str, Any], run_day: date,
) -> List[str]:
    """v5 硬过滤（TL D1/D2/D3'，brief §4 + Round-2 数据源切换）。

    **v4 零回归**：config 未启用任何 v5 键（soe_required=False、白名单空、
    min_total_mv_yi=None、min_consecutive_div_years=None）→ 原样返回，零新浪请求。

    Round-2 数据源（东财停用，TL D-EM/D1'/D3'）：
    - 分红历史 = **本地静态** cache/em_dividend_all.csv（sina.load_local_dividends，
      零网络；progress=="实施分配" 过滤在 metrics 层）；
    - SOE 输入 = **新浪 F10 前十大股东**（逐只抓，串行限速 D9'；双规则判定）。

    管线顺序（brief §"运行管线顺序"）：universe → 行业白名单 + SOE + 市值 + 连续分红
    → （候选股 OCF 取数在 _run_zscore 内、打分前执行）。
    PIT：分红事件锚 ex_date<=run_day；股东表截止日期/公告日期 <= run_day。

    **D-EM 守卫**：本函数及 _run_zscore 的 v5 路径均不 import screener.data.em——
    东财客户端只在 config em.enabled=true 时才可能被构造（当前生产配置 false）。
    """
    min_years = hfc.get("min_consecutive_div_years")
    whitelist = uc.get("industry_whitelist_csric2") or []
    min_mv = uc.get("min_total_mv_yi")
    soe_required = bool(uc.get("soe_required"))
    if not (soe_required or whitelist or min_mv is not None or min_years is not None):
        return hard_pass  # v4 行为（零回归）

    from .data import sina as sinamod
    scfg = cfgmod.sina_cfg(cfg)
    keywords = cfgmod.soe_keywords_cfg(cfg)
    cache_dir = datac["cache_dir"]
    run_day_s = run_day.isoformat()
    run_year = run_day.year

    # ---- 1. 本地静态分红全表（D1'：零网络；东财封禁前落盘快照，截至 2026-09-10）----
    div_rows, div_meta = sinamod.load_local_dividends(cache_dir)
    result.data_notes.append(
        f"v5 分红数据源: 本地静态 em_dividend_all.csv（{div_meta['rows']} 行，实施分配 "
        f"{div_meta['implemented_rows']} 行；快照截至 {div_meta['as_of']}，Phase 1 不做每日增量）")
    div_by_code: Dict[str, List[Dict[str, Any]]] = {}
    for r in div_rows:
        div_by_code.setdefault(r["code"], []).append(r)

    # ---- 2. 白名单 + 市值先过滤（缩小 SOE 抓取面：brief_round2 预估候选 ~300-500 只，
    #      全量 hard_pass 可达上千只 → 新浪请求超 D9' 预算；SOE/连续分红在子集上执行）----
    def _industry_code(ind: str) -> str:
        m = re.match(r"^([A-Z]\d{2})", (ind or "").strip())
        return m.group(1) if m else ""

    kept = list(hard_pass)
    n0 = len(kept)
    if whitelist:
        wl = set(whitelist)
        kept = [c for c in kept if _industry_code(industry_map_all.get(c, "")) in wl]
    result.funnel["L2b_行业白名单"] = len(kept)
    mv_map: Dict[str, float] = {}
    if min_mv is not None:
        kept, mv_map = _filter_min_market_cap(cfg, kept, min_mv)
        result.total_mv_yi_map = mv_map
    result.funnel["L2d_市值下限"] = len(kept)

    # ---- 3. SOE：新浪 F10 前十大股东（D3'；白名单+市值子集逐只抓、串行限速 D9'、熔断）----
    # soe_flag_map/soe_basis_map 供报告列 + 复核清单（TL D3'）。
    # **按总市值降序抓取**：WAF 若在中途截断序列（实测 ~10-30 次后 HTTP 456），先保证
    # 高市值候选（最可能进 Top50）完成 SOE 核验——降级运行时 Top 列表仍尽量完整。
    # （排序只影响抓取顺序；最终入选由 total_score 决定，与输入顺序无关。）
    if mv_map:
        kept = sorted(kept, key=lambda c: mv_map.get(c, 0.0), reverse=True)
    sina_client = sinamod.SinaClient(scfg)
    soe_fetched: set = set()   # 成功取到 PIT 可见报告期的 code（区分"真非SOE"与"取数失败"）
    n_consec_fail = 0
    breaker_tripped = False
    n_ok = 0
    for i, c in enumerate(kept):
        c6 = c.split(".")[1]
        if breaker_tripped:
            continue  # 熔断后剩余保持 soe_flag=None（取数失败，不进"非SOE复核清单"）
        try:
            periods = sina_client.fetch_holders(c6)
            picked = sinamod.pick_holders_asof(periods, run_day_s)
            n_consec_fail = 0
            if picked is None:
                log.warning("v5 SOE %s: 无 PIT 可见报告期（截止日期/公告日期均 > run_day）", c)
                continue
            soe_fetched.add(c)
            result.soe_flag_map[c] = soe_flag(picked["holders"], keywords)
            result.soe_basis_map[c] = soe_basis(picked["holders"], keywords)
            n_ok += 1
        except sinamod.SinaDataError as exc:
            if "熔断" in str(exc):
                breaker_tripped = True
                log.warning("v5 SOE 新浪 F10 熔断：剩余 %d 只保持 soe=None（取数失败，不进复核清单）",
                            len(kept) - i - 1)
                continue
            n_consec_fail += 1
            if n_consec_fail >= scfg["consecutive_fail_breaker"]:
                breaker_tripped = True
                log.warning(
                    "v5 SOE 新浪 F10 连续失败 %d 只 → 熔断：该类因子整体降级 None + 报告告警（D9'）",
                    n_consec_fail)
        if (i + 1) % 100 == 0 or i + 1 == len(kept):
            _progress("L2b_sina_holders", i + 1, len(kept))
    result.data_notes.append(
        f"v5 SOE 数据源: 新浪 F10 前十大股东（{n_ok}/{len(kept)} 只取数成功，"
        f"HTTP {sina_client.request_count} 次；双规则=股本性质'国有股' OR 名称关键词）")
    if breaker_tripped:
        result.data_notes.append(
            "⚠️ v5 SOE 新浪 F10 接口连续失败熔断（D9'）：部分候选 soe_flag=None"
            "（取数失败，非'判定为非SOE'），已剔除——人工核对后重跑可补齐（幂等）")

    # D3'：soe_flag=None 且**成功取数**的候选 → 剔除 + 进报告复核清单（供人工复核）。
    # 取数失败/熔断的股不进此清单（语义=数据缺失，已在 data_notes 告警）。
    for c in kept:
        if c in soe_fetched and result.soe_flag_map.get(c) is None:
            result.soe_review_list.append({"code": c, "name": name_by_code.get(c, "")})

    # ---- 4. SOE + 连续分红过滤（漏斗计数）----
    if soe_required:
        kept = [c for c in kept if result.soe_flag_map.get(c) is not None]
    result.funnel["L2c_SOE央国企"] = len(kept)
    if min_years is not None:
        kept = [c for c in kept if _div_history_ok(c, div_by_code.get(c.split(".")[1], []),
                                                   run_day_s, run_year, min_years)]
        result.funnel["L2e_连续分红D1"] = len(kept)

    log.info(
        "v5硬过滤: %d → 白名单%d → 市值%d → SOE%d → 连续分红%d（新浪F10成功 %d/%d 只）",
        n0, result.funnel.get("L2b_行业白名单", n0), result.funnel.get("L2d_市值下限", n0),
        result.funnel.get("L2c_SOE央国企", n0), len(kept), n_ok, len(hard_pass),
    )
    return kept


def _filter_min_market_cap(cfg: Dict[str, Any], codes: List[str], min_mv_yi: float) -> Tuple[List[str], Dict[str, float]]:
    """总市值 ≥ min_mv_yi（腾讯快照 idx45，现有批量通道 tencent.py）。

    快照缺失/未返回的 code → 剔除（保守：无市值证据不通过硬门槛）+ 告警。
    :return: (kept, mv_map)——mv_map 覆盖全部请求 codes 的总市值（亿元），供报告列。
    """
    from .data.tencent import TencentClient
    tc = cfg.get("datasource", {}).get("tencent", {}) or {}
    batch = int(tc.get("snapshot_batch_size", 200))
    client = TencentClient(
        timeout=float(tc.get("timeout_s", 15)), max_attempts=int(tc.get("max_attempts", 3)),
    )
    quotes = client.fetch(codes, batch_size=batch)
    kept: List[str] = []
    mv_map: Dict[str, float] = {}
    n_missing = 0
    for c in codes:
        q = quotes.get(c)
        mv = (q or {}).get("total_mv_yi")
        if mv is None:
            n_missing += 1
            continue
        mv_map[c] = float(mv)
        if mv >= min_mv_yi:
            kept.append(c)
    if n_missing:
        log.warning("v5市值过滤: %d 只腾讯快照缺失/无总市值 → 剔除（保守方向）", n_missing)
    return kept, mv_map


def _div_history_ok(
    code: str, div_rows: List[Dict[str, Any]], run_day_s: str, run_year: int, min_years: int
) -> bool:
    """TL D1 连续分红判定（新股规则精确化）。

    - IPO 满 7 年（run_year - ipo_year >= 7）→ consecutive_div_years >= min_years；
    - IPO 不满 7 年 → new_stock_div_ok（IPO 年份之后每个完整年度都有分红，
      IPO 当年不要求——上市不足整年）。
    IPO 年 = kline_af3 缓存首行日期年份（全史K线自 IPO 起，v2 稳定键保证）。
    """
    if _DIV_FETCHER is None:
        return False  # 未注入 fetcher（不应发生）→ 保守剔除
    first = _kline_first_date(_DIV_FETCHER, code)
    if not first:
        return False  # 无K线缓存 → 无法判定 IPO 年（保守剔除；正常不会发生：已过上市天数硬过滤）
    ipo_year = int(first[:4])
    annual = annual_dps_from_em(div_rows, code.split(".")[1], run_day_s)
    if run_year - ipo_year >= 7:
        n = consecutive_div_years(annual, run_year)
        return n is not None and n >= min_years
    return new_stock_div_ok(annual, ipo_year, run_year)


# _DIV_FETCHER：_div_history_ok 需要读 kline_af3 首行（模块级注入，避免参数穿透）
_DIV_FETCHER: Optional[DataFetcher] = None


def set_div_fetcher(fetcher: DataFetcher) -> None:
    """由 run_screener 在 v5 硬过滤前注入（_div_history_ok 读 IPO 年用）。"""
    global _DIV_FETCHER
    _DIV_FETCHER = fetcher


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

    # ---- v5 数据准备（Round-2：本地静态分红 + TE rf + 新浪 OCF；东财停用）----
    from .data import sina as sinamod
    from .data import rf as rfmod
    uc5 = cfgmod.universe_cfg(cfg)
    hf5 = cfgmod.hard_filter_cfg(cfg)
    v5_on = bool(
        uc5.get("soe_required") or uc5.get("industry_whitelist_csric2")
        or uc5.get("min_total_mv_yi") is not None
        or hf5.get("min_consecutive_div_years") is not None
    )
    em_div_records: Dict[str, List[Dict[str, Any]]] = {}
    annual_dps_map: Dict[str, Dict[int, float]] = {}
    rf_10y: Optional[float] = None
    rf_meta: Dict[str, Any] = {}
    ocf_annual_map: Dict[str, Optional[Dict[str, Any]]] = {}  # D6'：code → 最近已披露年报 OCF（新浪）
    code6_to_bs: Dict[str, str] = {c.split(".")[1]: c for c in hard_pass}
    if v5_on:
        sina_c = cfgmod.sina_cfg(cfg)   # ⚠️不得命名 scfg——会遮蔽 _run_zscore 的打分配置参数（scoring_cfg）
        cache_dir = cfg["data"]["cache_dir"]
        run_day_s = run_day.isoformat()
        # 分红全表：本地静态（D1'，零网络；硬过滤阶段已读过 → 此处再读一次纯 IO）
        div_rows_all, _dm = sinamod.load_local_dividends(cache_dir)
        for r in div_rows_all:
            em_div_records.setdefault(r["code"], []).append(r)
        # 10Y 国债（TL D4'）：TE 现值，每日运行抓取一次 → rf_10y_daily.csv 落盘；
        # 解析失败 → config fallback + 告警（fetch_rf_10y 内部处理，不抛异常）。
        rfc = cfgmod.rf_cfg(cfg)
        rf_10y, rf_meta = rfmod.fetch_rf_10y(rfc, cache_dir, run_day_s)
        if rf_meta.get("source") == "fallback":
            result.data_notes.append(
                f"⚠️ v5 rf 数据源: TE 解析失败 → 回退 config fallback={rfc['fallback_pct']}%（告警不静默，D4'）")
        else:
            result.data_notes.append(
                f"v5 rf 数据源: TradingEconomics 10Y={rf_meta['yield_pct']}%（{rf_meta['date']}，"
                f"落盘 cache/{rfmod.RF_CACHE_FILE}）")
        # 逐年 DPS（D1/D5 因子输入；PIT ex_date<=run_day + progress=="实施分配"）
        for c in hard_pass:
            annual_dps_map[c] = annual_dps_from_em(
                em_div_records.get(c.split(".")[1], []), c.split(".")[1], run_day_s
            )
        # ---- D6'：候选股 OCF 逐只取数（新浪财务 JSON；硬过滤后、打分前）----
        log.info("阶段4b(v5): 候选股 %d 只 OCF 逐只取数（新浪 getFinanceReport2022，串行限速 >=1s）...",
                 len(hard_pass))
        # 单客户端复用：全局限速锚点 + 连续失败熔断（D9'）跨逐只调用生效。
        cf_client = sinamod.SinaClient(sina_c)
        _cf_consec_fail = 0
        _cf_breaker_tripped = False
        for i, code in enumerate(hard_pass):
            c6 = code.split(".")[1]
            if _cf_breaker_tripped:
                ocf_annual_map[code] = None  # 熔断后剩余全部降级代理
                continue
            try:
                ann = cf_client.fetch_annual_ocf(c6, run_day_s)
                ocf_annual_map[code] = ann  # None=无可见年报 → 该股降级代理（非接口失败）
                _cf_consec_fail = 0  # 成功（含"无可见年报"）→ 重置连续失败计数
            except sinamod.SinaDataError as exc:
                if "熔断" in str(exc):
                    _cf_breaker_tripped = True
                    log.warning("v5 OCF 新浪财务JSON 熔断：剩余 %d 只全部降级代理（D9'）",
                                len(hard_pass) - i - 1)
                    ocf_annual_map[code] = None
                    continue
                # 单只失败 → 该股 fcf_coverage 降级代理（TL D6），不炸全市场
                log.warning("v5 OCF 取数失败 %s: %s（该因子降级代理 cfo_to_np/payout）", code, exc)
                ocf_annual_map[code] = None
                _cf_consec_fail += 1
                if _cf_consec_fail >= sina_c["consecutive_fail_breaker"]:
                    _cf_breaker_tripped = True
                    log.warning(
                        "v5 OCF 新浪财务JSON 连续失败 %d 只 → 熔断：剩余 %d 只全部降级代理（D9'）",
                        _cf_consec_fail, len(hard_pass) - i - 1)
            if (i + 1) % 100 == 0 or i + 1 == len(hard_pass):
                _progress("L3b_sina_cashflow", i + 1, len(hard_pass))
        result.data_notes.append(
            f"v5 OCF 数据源: 新浪财务JSON getFinanceReport2022（MANANETR 经营现金流量净额，年报行；"
            f"HTTP {cf_client.request_count} 次）")
        if _cf_breaker_tripped:
            result.data_notes.append(
                "⚠️ v5 OCF 新浪财务JSON 接口连续失败熔断（D9'）：部分候选 fcf_coverage 降级代理"
                "（CFOToNP/payout）——重跑可补齐（幂等）")

    reinvest_c = cfgmod.reinvest_cfg(cfg)  # D8：target_ttm_yield_pct / 分位回看年数

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
        if v5_on:
            # v5：分红走东财全表（BaoStock 零请求；单位已 /10、口径同 BaoStock）
            recs = em_dividend_records(em_div_records.get(code.split(".")[1], []), code6_to_bs)
        else:
            recs = []
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

        # ---- v5 新因子（TL D1/D4/D6/D8；v4 路径下 annual_dps_map/rf_10y 为空/None → 全 None）----
        cons_y = (consecutive_div_years(annual_dps_map.get(code, {}), run_day.year)
                  if v5_on else None)
        # div_stability：近5年 DPS 变异系数（越低越稳 → 打分取负；D5 dividend 子因子）
        stab_cv = (div_stability_cv(annual_dps_map.get(code, {}), n=5, end_year=run_day.year - 1)
                   if v5_on else None)
        div_stab_score = None if stab_cv is None else -stab_cv
        # fcf_coverage（Round-2 TL D6'：OCF-based 真值）= OCF(年报) / (年度DPS×总股本)；
        # 接口失败/无可见年报/缺输入 → 降级代理 CFOToNP/payout。
        cfo_to_np_v = _f((fr_["cashflow_cur"] or {}).get("CFOToNP"))
        ocf_row = ocf_annual_map.get(code)
        fcf_val: Optional[float] = None
        if v5_on:
            if ocf_row is not None and ocf_row.get("ocf") is not None \
                    and annual_dps_map.get(code, {}).get(annual_year) is not None \
                    and _f(p_cur.get("totalShare")) is not None:
                # capex=None → 纯 OCF 覆盖口径（D6'：新浪无 capex 字段）
                fcf_val = fcf_coverage(
                    ocf_row["ocf"], None,
                    annual_dps_map[code][annual_year], _f(p_cur.get("totalShare")),
                )
            if fcf_val is None:
                # 降级代理（TL D6：接口失败/缺输入时）= CFOToNP / payout
                fcf_val = fcf_coverage_proxy(cfo_to_np_v, payout)
        # technical v5 子因子（TL D5/D9：估值因子并入 technical 维度——引擎固定 4 维的务实选择）
        div_yield_pct: Optional[float] = None
        ttm_pctile: Optional[float] = None
        ttm_pctile_n: int = 0
        yld_spread: Optional[float] = None
        if v5_on:
            rebuilt_ = fetcher.kline_af3_rebuilt(code)
            closes_map: Dict[str, float] = {}
            if rebuilt_:
                closes_map = {d: c for d, c in zip(rebuilt_["dates"], rebuilt_["af3_close"])
                              if c is not None}
            ttm_pctile, ttm_pctile_n = dividend_yield_percentile(
                annual_dps_map.get(code, {}), closes_map, run_day.isoformat(),
                reinvest_c["yield_pctile_lookback_years"], ttm_y,
            )
            yld_spread = yield_spread(ttm_y, rf_10y)

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
        if v5_on:
            for fname, val in (
                ("technical.div_yield_pctile", ttm_pctile),
                ("technical.yield_spread", yld_spread),
                ("dividend.consecutive_div_years", cons_y),
                ("dividend.fcf_coverage", fcf_val),
                ("dividend.div_stability", div_stab_score),
            ):
                if val is None:
                    missing.append(fname)
        if piot.n_na:
            missing.append(f"piotroski_NA({';'.join(piot.na_signals)})")
        missing_by_code[code] = missing

        stocks.append({
            "code": code,
            "factors": {
                # v5（TL D5/D9）：technical 子因子按 config sub_weights 供给——
                # v4 配置只取 ma_bullish/low_vol；v5 配置追加 div_yield_pctile/yield_spread。
                # 引擎按 sub_weights 键取值，多余键（v4 路径的 window_return/rsi/macd）被忽略，
                # 故两代配置共用同一 raw dict，零回归。
                "technical": {
                    **tech_factors[code],
                    "div_yield_pctile": ttm_pctile,
                    "yield_spread": yld_spread,
                },
                "dividend": {
                    "ttm_yield": ttm_y,
                    "payout_ratio": payout,
                    "consecutive_div_years": cons_y,
                    "fcf_coverage": fcf_val,
                    "div_stability": div_stab_score,
                },
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
                # v5 报告列输入（D8/D1）
                "cons_y": cons_y, "ttm_pctile": ttm_pctile, "ttm_pctile_n": ttm_pctile_n,
                "annual_dps": annual_dps_map.get(code, {}),
            },
        })
        if (i + 1) % 500 == 0 or i + 1 == len(hard_pass):
            _progress("L4_factor_compute", i + 1, len(hard_pass))

    # ---- v5 再投资参考列（TL D8）：参考价 = 年度DPS / 目标TTM股息率(4%) + TTM 历史分位 ----
    if v5_on:
        target = reinvest_c["target_ttm_yield_pct"] / 100.0
        for s in stocks:
            c = s["code"]
            a = s["_aux"]
            dps_annual = (a.get("annual_dps") or {}).get(annual_year)
            ref_price = None if (dps_annual is None or target <= 0) else dps_annual / target
            result.reinvest_cols[c] = {
                "ref_price": ref_price,
                "ttm_yield_pctile": a.get("ttm_pctile"),
                "ttm_yield_pctile_n": a.get("ttm_pctile_n") or 0,
            }

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
            # ---- v5 报告列（TL D1/D3'/D8；v4 路径下为空，零回归）----
            "soe_flag": (result.soe_flag_map.get(code) if v5_on else None),
            "soe_basis": (result.soe_basis_map.get(code, "") if v5_on else ""),
            "total_mv_yi": (round(result.total_mv_yi_map[code], 2)
                            if v5_on and code in result.total_mv_yi_map else None),
            "consecutive_div_years": a.get("cons_y") if v5_on else None,
            "fcf_coverage": _f4(sc.raw["dividend"].get("fcf_coverage")) if v5_on else None,
            "div_stability_cv": (None if sc.raw["dividend"].get("div_stability") is None
                                 else round(-float(sc.raw["dividend"]["div_stability"]), 4)) if v5_on else None,
            "reinvest_ref_price_4pct": (
                _f3(result.reinvest_cols[code]["ref_price"])
                if v5_on and code in result.reinvest_cols else None),
            "ttm_yield_pctile": (
                result.reinvest_cols[code]["ttm_yield_pctile"]
                if v5_on and code in result.reinvest_cols else None),
            "yield_spread_pct": (None if sc.raw["technical"].get("yield_spread") is None
                                 else round(float(sc.raw["technical"]["yield_spread"]) * 100.0, 3)) if v5_on else None,
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

    # v5（验收④）：ttm_yield vs 腾讯 idx64 抽样交叉校验（偏差 <0.1pct，超差告警）
    if v5_on and do_crosscheck and result.funnel["L4_TopN入选"] > 0:
        _ttm_crosscheck(cfg, result)


def _ttm_crosscheck(cfg, result: ScreenResult) -> None:
    """v5（TL D8/验收④）：自算 ttm_yield vs 腾讯 idx64 TTM 股息率% 抽样对比。

    口径说明：我方 ttm_yield = 窗口内已除权分红和 ÷ **运行日** af3 收盘（PIT）；
    腾讯 idx64 是实时价口径 → 两者差异含当日价格波动。验收阈值 <0.1pct（config
    crosscheck.ttm_tolerance_pct）；超差逐只告警不静默，全部结果留档 result.ttm_crosscheck。
    """
    from .data.tencent import TencentClient
    xc = cfgmod.crosscheck_cfg(cfg)
    tol = float((cfg.get("crosscheck") or {}).get("ttm_tolerance_pct", 0.1))
    top = [s.code for s in result.scored if s.top_n_selected]
    sample = top[: int(xc["sample_size"])]
    if not sample:
        return
    try:
        tencent = TencentClient()
        quotes = tencent.fetch(sample, batch_size=int(xc["batch_size"]))
        for code in sample:
            row = result.candidates.loc[result.candidates["code"] == code].iloc[0]
            ours = _f(row.get("ttm_dividend_yield_pct"))  # 百分数口径（CSV 列）
            q = quotes.get(code) or {}
            theirs = q.get("ttm_yield_pct")
            diff = None if (ours is None or theirs is None) else abs(ours - float(theirs))
            result.ttm_crosscheck.append({
                "code": code, "name": row["name"],
                "ours_pct": ours, "tencent_pct": theirs,
                "diff_pct": None if diff is None else round(diff, 3),
                "ok": diff is not None and diff <= tol,
            })
        n_bad = sum(1 for c in result.ttm_crosscheck if not c["ok"])
        log.info("v5 ttm_yield 交叉校验: %d/%d 只偏差 ≤%.2fpct（%d 只超差）",
                 len(result.ttm_crosscheck) - n_bad, len(result.ttm_crosscheck), tol, n_bad)
        for c in result.ttm_crosscheck:
            if not c["ok"]:
                log.warning("v5 ttm_yield 交叉校验超差 %s: 我方=%s%% 腾讯=%s%% diff=%s（>%.2fpct）",
                            c["code"], c["ours_pct"], c["tencent_pct"], c["diff_pct"], tol)
    except Exception as exc:  # noqa: BLE001 - 交叉校验失败不影响主流程
        log.warning("v5 ttm_yield 交叉校验失败（不影响主结果）: %s", exc)


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
    # 保留前序阶段已 append 的 data_notes（v5：本地分红源/SOE F10 取数成功数/熔断告警等）——
    # 原实现 `result.data_notes = [...]` 会整体覆盖，丢失 _v5_hard_filter 的 SOE 取数与 WAF
    # 熔断记录（TL D9'：降级必须可追溯）。改为前置插入标准段。
    standard_notes = [
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
    # 标准段在前 + 前序阶段 notes 在后（SOE F10 取数成功数/熔断告警等不丢失）
    result.data_notes = standard_notes + list(result.data_notes)
    # ---- v5 数据源/因子说明（TL D1-D9 + Round-2；config 未启用 v5 键时不追加，v4 报告零回归）----
    uc5 = cfgmod.universe_cfg(cfg)
    hf5 = cfgmod.hard_filter_cfg(cfg)
    if (uc5.get("soe_required") or uc5.get("industry_whitelist_csric2")
            or uc5.get("min_total_mv_yi") is not None
            or hf5.get("min_consecutive_div_years") is not None):
        result.data_notes += [
            "v5 硬过滤: 行业白名单(证监会二级) + SOE央国企(新浪F10双规则: 股本性质'国有股' OR 名称关键词) + "
            f"总市值≥{uc5.get('min_total_mv_yi')}亿(腾讯idx45) + 连续分红(D1, min={hf5.get('min_consecutive_div_years')})",
            "v5 东财停用（TL D-EM：海外访问被禁）——em.enabled=false，本运行零东财请求；"
            "本地静态缓存 em_dividend_all.csv 正常读取（D1' 唯一例外=本地文件）",
            "v5 新因子: consecutive_div_years / div_stability(近5年DPS CV) / fcf_coverage(OCF-based: 新浪年报OCF/年度分红总额, D6'; "
            "失败降级CFOToNP/payout代理) / div_yield_pctile(TTM股息率历史分位) / yield_spread(TTM-10Y国债)",
            "v5 technical 维度含估值因子(div_yield_pctile/yield_spread)——TL D9：引擎固定4维的务实选择，非语义归类",
            f"v5 再投资参考(TL D8): 参考价=年度DPS/目标TTM股息率{cfgmod.reinvest_cfg(cfg)['target_ttm_yield_pct']}% + TTM历史分位展示",
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
    return None if v is None else round(float(v), 2)


def _f3(v: Optional[float]) -> Optional[float]:
    return None if v is None else round(float(v), 3)


def _f4(v: Optional[float]) -> Optional[float]:
    return None if v is None else round(float(v), 4)


def _pct2(v: Optional[float]) -> Optional[float]:
    """小数 → 百分数（2位）。"""
    return None if v is None else round(v * 100.0, 2)


def _pct3(v: Optional[float]) -> Optional[float]:
    return None if v is None else round(v * 100.0, 3)


def _i01(v: Optional[float]) -> Optional[int]:
    return None if v is None else int(v)
