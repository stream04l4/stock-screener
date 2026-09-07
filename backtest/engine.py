# -*- coding: utf-8 -*-
"""回测引擎编排（Phase A 核心：月末 PIT 快照跑打分 → 目标组合 → 驱动模拟器）。

流程（报告 R5 + TL 拍板）：
1. 调仓日序列 = [start, end] 内每 rebalance 周期的**最后一个交易日**。
2. 每个调仓日 T（全部用"截至 T"的 PIT 数据，data_pit 层保证无未来泄漏）：
   - PIT 股票池（K线跨度近似 + 前缀；停牌 tradestatus=0 剔除，与 v2 live 口径一致）；
   - 硬剔除（ST / 上市未满 N 个交易日，n_bars=count(date<=T)——PIT 修正点）；
   - 四维因子（复用 v2 ``screener.screener._tech_factors`` + ``screener.metrics``
     纯函数 + ``scoring.score_cross_section``，**不重写**）；
   - **维度自适应（TL 硬性要求 6）**：某维度本期无任何 PIT 数据 → 该维得分置中性
     （因子全 None → z=0）并在报告标注"本期激活维度=[...]"。
3. Top N 等权 → ``RebalanceOrder``；模拟器 T+1 成交（open 缺失 close 兜底）。
4. 基准：自建全市场等权指数（离线合成，口径同调研 R3 留样）；配置里的指数代码
   （sh.000300/sh.000905）K线待 Phase C 补拉 → 缺失时报告标注"未纳入"。
5. 产物三件套落盘 output/backtest/（report_bt）。

性能：全缓存预热解析 ~80s + 每期打分 ~40s（researcher 实测 42s/期）→ 61 期 ≈ 45min。
"""
from __future__ import annotations

import bisect
import logging
import math
import time
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence

from screener.config import (backtest_cfg, data_cfg, hard_filter_cfg,
                             industry_cfg, scoring_cfg, universe_cfg)
from screener.metrics import (build_industry_groups, dedup_dividends, payout_ratio,
                              piotroski_fscore, rank_percentile, roe_stability,
                              ttm_dividend_yield)
from screener.screener import _tech_factors
from screener.scoring import DIMENSIONS, score_cross_section

from .data_pit import PitData
from .result import BacktestResult, BenchmarkSeries, PeriodHolding, PeriodInfo
from .report_bt import write_all
from .simulator import (CostsConfig, PortfolioSimulator, RebalanceOrder,
                        SuspensionConfig)

log = logging.getLogger("backtest.engine")

# 基准探测股（银行披露最早最齐——v2 _resolve_annual_year/_probe_latest_period 同款约定，
# 数据源选择而非策略阈值）
BASE_PROBE_CODE = "sh.601398"


def rebalance_dates(pit: PitData, start: date, end: date,
                    period: str) -> List[str]:
    """[start, end] 内每周期（monthly/quarterly）的最后一个交易日。"""
    cal = pit.trade_calendar(start, end)
    if not cal:
        return []
    # 按月分组：month_key -> 该月最后一个交易日
    by_month: Dict[str, str] = {}
    for d in cal:
        by_month[d[:7]] = d
    months = sorted(by_month)
    if period == "quarterly":
        months = [m for m in months if int(m[5:7]) % 3 == 0]
    out = [by_month[m] for m in months]
    return [d for d in out if start.isoformat() <= d <= end.isoformat()]


class _FundMemo:
    """基本面行按 (kind,code,year,quarter) 记忆化（pubDate 校验仍逐期做）。"""

    def __init__(self, pit: PitData) -> None:
        self._pit = pit
        self._cache: Dict[tuple, Optional[Dict[str, Any]]] = {}

    def row(self, kind: str, code: str, year: int, quarter: int,
            T: date) -> Optional[Dict[str, Any]]:
        key = (kind, code, year, quarter)
        if key not in self._cache:
            self._cache[key] = self._pit._fundamental_row(kind, code, year, quarter, date.max)
        raw = self._cache[key]
        if raw is None:
            return None
        # PIT 校验：pubDate > T → 视为未披露（date.max 预读时跳过该校验）
        pub_s = str(raw.get("pubDate") or "").strip()
        if pub_s:
            try:
                from datetime import date as _d
                if _d.fromisoformat(pub_s) > T:
                    return None
            except ValueError:
                pass
        return raw


def _dim_has_data(stocks: List[Dict[str, Any]], dim: str) -> bool:
    """维度本期是否有 PIT 数据（任一候选股该维至少一个非 None 因子）。"""
    for s in stocks:
        f = (s.get("factors") or {}).get(dim) or {}
        if any(v is not None for v in f.values()):
            return True
    return False


def compute_period_factors(
    pit: PitData, codes: List[str], T: date, annual_year: int,
    industry_map: Dict[str, str], max_bars: int, window_days: int,
    min_group_size: int, memo: Optional["_FundMemo"] = None,
) -> tuple[List[Dict[str, Any]], Dict[str, str]]:
    """单期四维因子计算（全部 PIT；复用 v2 纯函数）。

    :param memo: 基本面行记忆化（跨期复用，避免每重读缓存文件）；None → 直读。
    :return: (stocks 列表[含 factors/_aux], {code: pubDate})
    """
    from datetime import timedelta as _td
    window_start = T - _td(days=window_days)
    stocks: List[Dict[str, Any]] = []
    pub_dates: Dict[str, str] = {}

    def _fund_row(kind: str, dy: int) -> Optional[Dict[str, Any]]:
        if memo is not None:
            return memo.row(kind, code, annual_year + dy, 4, T)
        return pit._fundamental_row(kind, code, annual_year + dy, 4, T)

    # 行业分组（全体候选）
    cand_industry = {c: (industry_map.get(c) or "").strip() for c in codes}
    groups = build_industry_groups(cand_industry)

    for code in codes:
        # ---- 技术面（af1 窗口截至 T；v2 _tech_factors 原样复用）----
        _, closes = pit.af1_window(code, T, max_bars)
        tech = _tech_factors(closes)

        # ---- 快照价（股息率分母，af3 不复权）----
        snap = pit.kline_snapshot(code, T)
        close_af3 = snap.close_af3 if snap else None

        # ---- 分红（ex_date<=T；TTM 窗口 + 年度支付率）----
        div_recs = pit.dividend_records(code, T)
        ttm_y = ttm_dividend_yield(div_recs, window_start, T, close_af3)
        cash_annual, _ = dedup_dividends(
            div_recs, date(annual_year, 1, 1), date(annual_year, 12, 31))

        # ---- 基本面（pubDate<=T；7 键访问模式同 v2）----
        p_cur = _fund_row("profit", 0) or {}
        payout = payout_ratio(
            cash_annual if cash_annual > 0 else None,
            _f(p_cur.get("totalShare")), _f(p_cur.get("netProfit")))
        roe_level = _f(p_cur.get("roeAvg"))
        yoy_pni = _f((_fund_row("growth", 0) or {}).get("YOYPNI"))
        liability = _f((_fund_row("balance", 0) or {}).get("liabilityToAsset"))
        gross_margin = _f(p_cur.get("gpMargin"))
        roe_vals = [
            _f((_fund_row("profit", -2) or {}).get("roeAvg")),
            _f((_fund_row("profit", -1) or {}).get("roeAvg")),
            roe_level,
        ]
        _, roe_std = roe_stability(roe_vals)
        piot = piotroski_fscore(
            code,
            profit_cur=_fund_row("profit", 0),
            profit_prior=_fund_row("profit", -1),
            balance_cur=_fund_row("balance", 0),
            balance_prior=_fund_row("balance", -1),
            growth_cur=_fund_row("growth", 0),
            cashflow_cur=_fund_row("cashflow", 0))
        if p_cur.get("pubDate"):
            pub_dates[code] = str(p_cur["pubDate"])

        stocks.append({
            "code": code,
            "factors": {
                "technical": tech,
                "dividend": {"ttm_yield": ttm_y, "payout_ratio": payout},
                "industry": {},  # 下方统一填分位
                "fundamental": {
                    "roe_level": roe_level,
                    "roe_stability": None if roe_std is None else -roe_std,
                    "low_liability": None if liability is None else -liability,
                    "gross_margin": gross_margin,
                    "piotroski": piot.ratio,
                },
            },
            "_aux": {"close_af3": close_af3, "industry": cand_industry[code],
                     "roe_level": roe_level, "yoy_pni": yoy_pni},
        })

    # ---- 行业组内分位（min_group_size 语义沿用 v2：小组跳过→None）----
    roe_map = {s["code"]: s["_aux"]["roe_level"] for s in stocks}
    yoy_map = {s["code"]: s["_aux"]["yoy_pni"] for s in stocks}
    for s in stocks:
        code = s["code"]
        ind_name = s["_aux"]["industry"] or "无行业"
        group_codes = groups[ind_name]
        if len(group_codes) < min_group_size:
            s["factors"]["industry"] = {"roe_rank_pct": None, "yoy_pni_rank_pct": None}
        else:
            _, roe_pct = rank_percentile(code, group_codes, roe_map)
            _, yoy_pct = rank_percentile(code, group_codes, yoy_map)
            s["factors"]["industry"] = {
                "roe_rank_pct": None if roe_pct is None else 100.0 - roe_pct,
                "yoy_pni_rank_pct": None if yoy_pct is None else 100.0 - yoy_pct,
            }
    return stocks, pub_dates


def _f(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        s = str(v).strip()
        return float(s) if s else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 自建全市场等权基准（口径同调研 R3：每月末存活池等权、af1 日收益合成、无成本）
# ---------------------------------------------------------------------------
def compute_ew_benchmark(
    pit: PitData, cal: List[str], pools_by_day: Dict[str, List[str]],
) -> BenchmarkSeries:
    """全市场等权指数净值（与策略同日历对齐）。

    :param pools_by_day: {调仓日ISO: 该期股票池}（段内持有到下一调仓日）。
    """
    if not cal:
        return BenchmarkSeries(name="ew_allmarket")
    n = len(cal)
    # 每只股票：日历日 → 当日 af1 日收益（按日期对齐，不假设 K线起点=日历起点）
    rets_of: Dict[str, Dict[str, float]] = {}
    for code in set(c for pool in pools_by_day.values() for c in pool):
        kl = pit.kline(code)
        if not kl or not kl.dates:
            continue
        prev = math.nan
        m: Dict[str, float] = {}
        for j, d in enumerate(kl.dates):
            v = kl.af1[j]
            if math.isnan(v):
                r = 0.0                      # 停牌日：收益 0（前值冻结）
            elif not math.isnan(prev) and prev > 0:
                r = v / prev - 1.0
            else:
                r = 0.0
            if not math.isnan(v):
                prev = v
            m[d] = r
        rets_of[code] = m

    # 调仓日 → 日历下标（段内持有到下一调仓日）
    reb_idx = [bisect.bisect_left(cal, d) for d in sorted(pools_by_day)]
    nav = 1.0
    navs: List[float] = [1.0]
    seg_pool: List[str] = []
    pending_pool: Optional[List[str]] = None
    seg_i = 0
    if reb_idx and reb_idx[0] == 0:
        # 首个调仓日=日历首日：其池在首日收盘选出 → 第 2 天起直接计收益（PIT）
        seg_pool = pools_by_day[cal[0]]
        seg_i = 1
    for g in range(1, n):
        day = cal[g]
        while seg_i < len(reb_idx) and reb_idx[seg_i] <= g:
            # 新池在**当日收盘**选出 → PIT：当日收益仍按旧池计，次日才生效
            pending_pool = pools_by_day[cal[reb_idx[seg_i]]]
            seg_i += 1
        s = 0.0
        cnt = 0
        if seg_pool:
            for c in seg_pool:
                m = rets_of.get(c)
                if m is None or day not in m:
                    continue   # 该日无K线（未上市/已退市）→ 不计入当日等权
                s += m[day]
                cnt += 1
        day_ret = (s / cnt) if cnt else 0.0
        nav *= (1.0 + day_ret)
        navs.append(nav)
        if pending_pool is not None:
            seg_pool = pending_pool
            pending_pool = None
    return BenchmarkSeries(name="ew_allmarket", code="", dates=list(cal), navs=navs)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def run_backtest(
    cfg: Dict[str, Any],
    output_dir: str = "output/backtest",
    on_progress: Optional[Callable[[str, int, int], None]] = None,
) -> BacktestResult:
    """执行完整离线回测（零 live 请求）。返回结果并落盘三件套。"""
    t0 = time.time()
    bt = backtest_cfg(cfg)
    scfg = scoring_cfg(cfg)
    uc = universe_cfg(cfg)
    hf = hard_filter_cfg(cfg)
    ic = industry_cfg(cfg)
    dc_win = int(cfg["dividend"]["window_days"])
    max_bars = data_cfg(cfg)["kline_calendar_days_back"] + 50

    start = date.fromisoformat(bt["start"])
    end = date.fromisoformat(bt["end"])
    pit = PitData(data_cfg(cfg)["cache_dir"])

    def _prog(stage: str, done: int, total: int) -> None:
        log.info("[PROGRESS] stage=%s done=%d total=%d", stage, done, total)
        if on_progress:
            on_progress(stage, done, total)

    # ---- 0. 预热：全缓存解析（~80s；多期复用）----
    _prog("warmup_parse", 0, 1)
    codes_all = pit.all_kline_codes()
    for c in codes_all:
        pit.kline(c)
    industry_map = pit.industry_map()
    name_map = pit.name_map()
    ind_update = pit.industry_update_date()
    _prog("warmup_parse", 1, 1)

    # ---- 1. 调仓日序列 ----
    rdates = rebalance_dates(pit, start, end, bt["rebalance"])
    if not rdates:
        raise RuntimeError(f"回测窗口 {bt['start']}~{bt['end']} 内无交易日")
    log.info("调仓期 %d 个（%s ~ %s，%s）", len(rdates), rdates[0], rdates[-1], bt["rebalance"])

    # ---- 2. 逐期打分 → 目标组合 ----
    use_open = bt["execution"] == "t1_open"
    targets_by_day: Dict[str, Dict[str, float]] = {}
    periods: List[PeriodInfo] = []
    n_periods = len(rdates)
    memo = _FundMemo(pit)   # 跨期记忆化：同一 (kind,code,year,q) 只读一次缓存文件
    for pi_, T_iso in enumerate(rdates):
        T = date.fromisoformat(T_iso)
        _prog("score_period", pi_, n_periods)
        t_p = time.time()

        # PIT 股票池 + 硬剔除（ST / 上市天数 / 决策日停牌）
        pool = pit.universe(T, uc["prefixes"])
        hard_pass: List[str] = []
        for code in pool:
            snap = pit.kline_snapshot(code, T)
            if snap is None:
                continue
            if hf["st_enabled"] and (snap.is_st or 0) == 1:
                continue
            if snap.tradestatus == 0:      # 决策日停牌 → 不入候选（v2 live 口径）
                continue
            if snap.n_bars < hf["listing_min_trading_days"]:
                continue
            hard_pass.append(code)

        # 基准年度（PIT：pubDate<=T 校验）+ 四维因子
        ay = PitData.resolve_annual_year(
            lambda y, q: memo.row("profit", BASE_PROBE_CODE, y, q, T), T)
        stocks, pub_dates = compute_period_factors(
            pit, hard_pass, T, ay, industry_map, max_bars, dc_win, ic["min_group_size"],
            memo=memo)

        # 维度自适应（TL 硬性要求 6）：无 PIT 数据的维度 → 因子全 None（中性 z=0）
        active: List[str] = []
        for dim in DIMENSIONS:
            if _dim_has_data(stocks, dim):
                active.append(dim)
            else:
                for s in stocks:
                    s["factors"][dim] = {k: None for k in (scfg["sub_weights"].get(dim) or {})}

        # 截面打分（复用 v2 score_cross_section；权重来自 scoring.weights）
        scored = score_cross_section(
            stocks, scfg["weights"], scfg["sub_weights"], bt["top_n"],
            scfg["missing_policy"])
        top = [s for s in scored if s.top_n_selected]
        w = 1.0 / len(top) if top else 0.0
        targets_by_day[T_iso] = {s.code: w for s in top}

        periods.append(PeriodInfo(
            decision_date=T_iso,
            active_dims=active,
            n_universe=len(pool),
            n_hard_pass=len(hard_pass),
            holdings=[
                PeriodHolding(code=s.code, name=name_map.get(s.code, ""),
                              weight=w, total_score=s.total_score)
                for s in top
            ],
        ))
        log.info("期 %d/%d %s: 池 %d → 硬剔后 %d → Top%d（激活维度=%s）%.1fs",
                 pi_ + 1, n_periods, T_iso, len(pool), len(hard_pass), len(top),
                 ",".join(active) or "—", time.time() - t_p)

    # ---- 3. 组合模拟（T+1 成交；日历须覆盖最后一个调仓日的执行日，否则末单无法成交）----
    _prog("simulate", 0, 1)
    costs = CostsConfig(**bt["costs"])
    susp = SuspensionConfig(**bt["suspension"])
    sim = PortfolioSimulator(pit, costs, susp, use_open=use_open)
    sim_cal_end = end
    if rdates:
        last_exec = pit.next_trade_date(date.fromisoformat(rdates[-1]))
        if last_exec:
            sim_cal_end = max(sim_cal_end, date.fromisoformat(last_exec))
    cal = [str(d) for d in pit.trade_calendar(start, sim_cal_end)]   # trade_calendar 返回 ISO str 列表
    decision_set = set(targets_by_day)
    skipped_last: List[str] = []
    for day in cal:
        sim.step(day)
        if day in decision_set:
            nxt = pit.next_trade_date(date.fromisoformat(day))
            if nxt is None or nxt > cal[-1]:
                # 缓存末尾边界：调仓日=最后交易日 → T+1 不存在，订单无法执行
                # （诚实处理：跳过并标注，不伪造成交）
                skipped_last.append(day)
                continue
            sim.submit_order(RebalanceOrder(decision_date=day,
                                            targets=targets_by_day[day]))
    _prog("simulate", 1, 1)

    # ---- 4. 基准：自建全市场等权 + 配置指数（K线已缓存时纳入；否则标注缺失）----
    pools_by_day = {p.decision_date: pit.universe(
        date.fromisoformat(p.decision_date), uc["prefixes"]) for p in periods}
    benchmarks: List[BenchmarkSeries] = [compute_ew_benchmark(pit, cal, pools_by_day)]
    missing_benchmarks: List[str] = []
    name_of: Dict[str, str] = {"sh.000300": "hs300", "sh.000905": "zz500"}
    for bcode in bt["benchmarks"]:
        kl = pit.kline(bcode)  # 指数K线缓存（Phase C 补拉后自动纳入）
        if kl is None or not kl.dates:
            missing_benchmarks.append(bcode)
            continue
        navs: List[float] = []
        dates: List[str] = []
        base_px: Optional[float] = None
        for j, d in enumerate(kl.dates):
            if d < cal[0] or d > cal[-1]:
                continue
            v = kl.af1[j]
            if math.isnan(v) or v <= 0:
                continue
            if base_px is None:
                base_px = v
            navs.append(v / base_px)
            dates.append(d)
        if len(navs) >= 2:
            benchmarks.append(BenchmarkSeries(name=name_of.get(bcode, bcode),
                                              code=bcode, dates=dates, navs=navs))

    # ---- 5. 组装结果 + entry_price（成交价 af1）----
    res = BacktestResult(
        start=bt["start"], end=bt["end"], top_n=bt["top_n"],
        execution_mode=bt["execution"], weights_ref=bt["weights_ref"],
        risk_free_pct=bt["risk_free_pct"],
        dates=list(sim.result.dates), navs=list(sim.result.nav),
        benchmarks=benchmarks, periods=periods,
        turnover_by_day=dict(sim.result.turnover),
        cost_bps_cum=sim.result.cost_bps_cum,
        n_rebalances=sim.result.n_rebalances,
        n_delisted_exits=sim.result.n_delisted_exits,
        delisting_drag_pct=sim.result.delisting_drag_pct,
        n_drop_to_cash=sim.result.n_drop_to_cash,
        open_fallback_total=sum(sim.result.open_fallback_days.values()),
        industry_update_date=ind_update,
    )
    # exec_day 由模拟器确定（决策日的下一交易日）；fills 按执行日查成交价
    exec_of = {str(r["decision_date"]): str(r["exec_day"]) for r in sim.result.rebalances}
    for p in periods:
        p.exec_day = exec_of.get(p.decision_date, "")
        fills = sim.result.fills.get(p.exec_day or "", {})
        for h in p.holdings:
            h.entry_price = fills.get(h.code)

    res.universe_note = (
        "K线跨度近似池（first_date<=T<=last_date，前缀过滤）；当前缓存仅含现存股，"
        "缺窗口内退市股（~60–120 只，幸存者偏差偏乐观）——Phase B/C 补拉 allstock(day=T) "
        "与退市股数据后切换严格 PIT 池")
    res.data_notes = [
        f"收益口径=方案B（af1 后复权价，分红已隐含进复权台阶；调研 R2 实测与 A 等价 ≤18bp）",
        f"成交口径={bt['execution']}：决策日 T 收盘 PIT 打分 → 次日执行；open 缺失 "
        f"{res.open_fallback_total} 笔按 close 兜底（缓存仅 23/5216 只K线含 open 列）",
        f"印花税卖出单边分段（≥2023-08-28=0.05% / 之前=0.1%）、佣金双边万2.5下限5元、"
        f"过户费双边0.001%、滑点 {costs.slippage_bp}bp —— 全部来自 config backtest.costs",
    ]
    if missing_benchmarks:
        res.data_notes.append(
            "基准 " + "/".join(missing_benchmarks) + " 的指数K线未缓存（Phase C 补拉）→ 本轮未纳入，"
            "仅含自建全市场等权基准")
    if skipped_last:
        res.data_notes.append(
            f"调仓日 {skipped_last[-1]} = 缓存最后交易日 → T+1 不存在，该期订单未执行"
            f"（诚实跳过；其选股结果仍计入 monthly_holdings/report）")

    # ---- 6. 落盘三件套 ----
    paths = write_all(output_dir, res)
    res.data_notes.append(f"耗时 {time.time() - t0:.0f}s；产物: {', '.join(paths.values())}")
    log.info("回测完成: %d 期 / %d 交易日 / 最终净值 %.4f（%.0fs）",
             len(rdates), len(cal), res.navs[-1] if res.navs else float("nan"),
             time.time() - t0)
    return res


if __name__ == "__main__":
    import argparse

    from screener.config import load_config as _load_cfg

    ap = argparse.ArgumentParser(description="多因子策略历史回测（全离线，零 live 请求）")
    ap.add_argument("--config", default="config/strategy.yaml", help="strategy.yaml 路径")
    ap.add_argument("--start", default=None, help="覆盖 backtest.start（ISO 日期）")
    ap.add_argument("--end", default=None, help="覆盖 backtest.end（ISO 日期）")
    ap.add_argument("--output-dir", default="output/backtest", help="产物目录")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    _cfg = _load_cfg(args.config)
    if args.start:
        _cfg["backtest"]["start"] = args.start
    if args.end:
        _cfg["backtest"]["end"] = args.end
    run_backtest(_cfg, output_dir=args.output_dir)
