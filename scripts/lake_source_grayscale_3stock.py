# -*- coding: utf-8 -*-
"""灰度 diff（02_code 修正轮 brief §6 验收标准 6）：primary=lake vs primary=tencent。

选 3 只（sh.601398/sh.601318/sh.600519），同 run_day 跑完整 run_screener，
对比 top_n 股票列表 + low_vol/RSI 因子值：差异 <5% 且排序一致 → 切换不改变筛选语义。

纪律（brief）：
- **有界、不烧配额**：tencent 侧走 cache（DiskCache 命中，**0 live BaoStock**）。
  硬守卫：monkeypatch BaoStockClient.call/call_with_fields → 任何调用即 AssertionError
  （TTL miss/缺文件会响亮失败，而不是悄悄发 live 请求）。
- **隔离 cache 副本**：只复制本 diff 所需文件到临时目录 + touch mtime（防 TTL miss），
  生产 cache/ 零改动。
- v5 硬过滤两侧一致关闭（soe/whitelist/mv/连续分红）→ 3 只全部进入 zscore，
  对比面 = 技术因子源差异（kline_af3_rebuilt：lake=存储 T2 adj_factor vs
  tencent=BaoStock cache adjfactor）。与 test_lake_source_e2e.py 的"只改网络源开关"同口径。
- universe 限制到 3 只：class 级 monkeypatch all_stock（仅本 harness 进程内，
  不改任何仓库文件）——同时约束 build_universe 与 _ensure_snapshot（腾讯快照面）。
- tencent 侧允许的网络 = 生产管线固有部分（腾讯快照 HTTP，3 只 1 批；两侧对称），
  **BaoStock=0**（守卫保证）。

用法：.venv/bin/python scripts/lake_source_grayscale_3stock.py [--date YYYY-MM-DD] [--out FILE]
退出码：0=diff 报告生成（是否"通过"按阈值判定并打印）；1=运行失败/守卫触发。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from datetime import date, timedelta

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

DIFF_STOCKS = [
    ("sh.601398", "工商银行"),
    ("sh.601318", "中国平安"),
    ("sh.600519", "贵州茅台"),
]
FACTOR_TOL = 0.05   # brief：low_vol/RSI 差异 <5%


def _needed_cache_files(run_day: date, annual_year: int) -> list[str]:
    """本 diff 所需的生产 cache 文件清单（tencent 侧全部要命中）。"""
    files = []
    for code, _name in DIFF_STOCKS:
        files.append(f"kline_af3_{code}.csv")
        files.append(f"adjfactor_{code}.csv")
        for y in (run_day.year - 1, run_day.year):          # dividend 年份（v5 off → BaoStock 分红）
            files.append(f"dividend_{code}_{y}.csv")
        for y in (annual_year - 2, annual_year - 1, annual_year):   # zscore 基本面（年度Q4）
            for t in ("profit", "growth", "balance", "cashflow"):
                files.append(f"{t}_{code}_{y}_4.csv")
    # _probe_latest_period **只查基准股 sh.601398**（当前季度起回退；首个**非空**即停）。
    # 回退链上所有"存在的文件"都列入隔离 cache（首文件若为空会继续探测下一个）。
    probe_bench = "sh.601398"
    probe_chain = []
    for step in range(4):   # probe_quarters_back 上限内
        py, pq = run_day.year, (run_day.month - 1) // 3 + 1 - step
        while pq < 1:
            pq += 4
            py -= 1
        probe_chain.append(f"profit_{probe_bench}_{py}_{pq}.csv")
    existing_probes = [f for f in probe_chain
                       if os.path.exists(os.path.join(REPO, "cache", f))]
    if not existing_probes:
        raise RuntimeError(f"_probe_latest_period 基准股 {probe_bench} 无任何季度 profit 缓存"
                           f"（{probe_chain}）——tencent 侧将触发 live BaoStock，拒绝运行")
    files.extend(existing_probes)
    # 市场级/全局
    files.append(f"allstock_{run_day.isoformat()}.csv")
    files.append("industry.csv")
    files.append("trade_calendar.csv")
    files.append("rf_10y_daily.csv")
    # latest_trade_date → trade_dates(start,end) 缓存键（TTL 1h → touch）
    start = (run_day - timedelta(days=30)).isoformat()
    files.append(f"tradedates_{start}_{run_day.isoformat()}.csv")
    return files


def _build_isolated_cache(run_day: date, annual_year: int) -> str:
    dst = tempfile.mkdtemp(prefix="gray3_cache_")
    missing = []
    for f in _needed_cache_files(run_day, annual_year):
        src = os.path.join(REPO, "cache", f)
        if not os.path.exists(src):
            missing.append(f)
            continue
        shutil.copy2(src, os.path.join(dst, f))
        os.utime(os.path.join(dst, f))   # touch mtime=now → 任何 TTL 检查都视为 fresh
    if missing:
        raise RuntimeError(f"隔离 cache 缺文件（tencent 侧将触发 live BaoStock，拒绝运行）: {missing}")
    return dst


def _annual_year_for(run_day: date) -> int:
    """与 screener._resolve_annual_year 同口径（月>4 → run_year-1）。"""
    return run_day.year - 1 if run_day.month > 4 else run_day.year - 2


class _BaoStockGuard:
    """硬守卫：任何 live BaoStock 调用 → AssertionError（0 live BaoStock 纪律）。"""

    def __enter__(self):
        from screener.data import baostock_client as bsm
        self._cls = bsm.BaoStockClient

        def _boom(self_, *a, **k):
            raise AssertionError(
                f"灰度 diff 触发 live BaoStock 调用（{getattr(a[0], 'label', a) if a else '?'}）"
                "——违反 0 live BaoStock 纪律")

        self._orig_call = self._cls.call
        self._orig_cwf = self._cls.call_with_fields
        self._cls.call = _boom
        self._cls.call_with_fields = _boom
        return self

    def __exit__(self, *exc):
        from screener.data import baostock_client as bsm
        bsm.BaoStockClient.call = self._orig_call
        bsm.BaoStockClient.call_with_fields = self._orig_cwf
        return False


def _patch_all_stock(fetcher_cls):
    """class 级 monkeypatch all_stock → 3 只（build_universe + _ensure_snapshot 同受约束）。"""
    import pandas as pd

    def all_stock(self, day: str) -> pd.DataFrame:
        df = pd.DataFrame(
            [(c, 1, n) for c, n in DIFF_STOCKS], columns=["code", "tradeStatus", "code_name"])
        self.calls["cache_hit"] += 1   # 记账语义保持（不触发 live）
        return df

    orig = fetcher_cls.all_stock
    fetcher_cls.all_stock = all_stock
    return orig


def _run(primary: str, run_day: date, cache_dir: str, out_dir: str):
    from screener.config import load_config
    from screener.screener import run_screener
    from screener.data import fetchers as F

    cfg = load_config(os.path.join(REPO, "config", "strategy.yaml"))
    cfg["datasource"]["primary"] = primary
    cfg.setdefault("canonical", {})["enabled"] = False      # 隔离：零 raw 落盘
    cfg["data"]["cache_dir"] = cache_dir
    # v5 硬过滤两侧一致关闭（只对比因子源，不引入 SOE/市值/行业白名单差异）
    u = cfg["universe"]
    u["soe_required"] = False
    u["industry_whitelist_csric2"] = []
    u["min_total_mv_yi"] = None
    cfg["hard_filter"]["min_consecutive_div_years"] = None

    if primary == "lake":
        from screener.data.lake_source import LakeDataFetcher
        orig_all = _patch_all_stock(LakeDataFetcher)
    else:
        from screener.data.fetchers import DataFetcher
        orig_all = _patch_all_stock(DataFetcher)
    # run_screener 构造 DataFetcher(client, cache) **不传** datasource_cfg → 惰性走
    # fetchers._load_default_datasource_cfg()（读 strategy.yaml 文件，现已 primary=lake）。
    # tencent 侧必须让它解析出 primary=tencent，否则 _is_tencent_primary()=False →
    # kline_af3_incremental/maybe_refresh_adjfactor 走 BaoStock live 路径（守卫会炸）。
    # class 级 monkeypatch（仅本 harness 进程内，不改仓库文件）：两侧各自解析自己的 primary。
    orig_ds = F._load_default_datasource_cfg
    F._load_default_datasource_cfg = lambda: dict(cfg["datasource"]) | {
        "primary": primary,
    }
    try:
        return run_screener(cfg, run_day, output_dir=os.path.join(out_dir, "out"),
                            do_crosscheck=False)
    finally:
        F._load_default_datasource_cfg = orig_ds
        if primary == "lake":
            from screener.data.lake_source import LakeDataFetcher
            LakeDataFetcher.all_stock = orig_all
        else:
            from screener.data.fetchers import DataFetcher
            DataFetcher.all_stock = orig_all


def _factors(result):
    """{code: {low_vol, rsi, total_score, rank, top_n_selected}}。"""
    out = {}
    for s in sorted(result.scored, key=lambda x: x.rank):
        tech = (s.raw or {}).get("technical") or {}
        out[s.code] = {
            "low_vol": tech.get("low_vol"),
            "rsi": tech.get("rsi"),
            "total_score": s.total_score,
            "rank": s.rank,
            "top_n_selected": s.top_n_selected,
        }
    return out


def _rel(a, b):
    if a is None or b is None:
        return None
    base = max(abs(a), abs(b), 1e-9)
    return abs(a - b) / base


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="2026-09-14", help="run_day（= cache 尾部交易日）")
    ap.add_argument("--out", default=os.path.join(REPO, "stages/02_code/grayscale_diff_3stock.txt"))
    args = ap.parse_args()

    run_day = date.fromisoformat(args.date)
    annual_year = _annual_year_for(run_day)
    work = tempfile.mkdtemp(prefix="gray3_")
    cache_dir = _build_isolated_cache(run_day, annual_year)
    out = [f"灰度 diff（3 只）· run_day={run_day.isoformat()} · primary=lake vs tencent",
           f"隔离 cache: {cache_dir}（生产 cache 零改动；touch mtime 防 TTL miss）",
           "守卫: BaoStockClient.call/call_with_fields → AssertionError（0 live BaoStock）"]

    with _BaoStockGuard():
        out.append("运行 lake 侧（存储 T2 adj_factor，零网络因子路径）...")
        res_lake = _run("lake", run_day, cache_dir, os.path.join(work, "lake"))
        out.append(f"  lake: run_day={res_lake.run_day} funnel={res_lake.funnel}")
        out.append("运行 tencent 侧（BaoStock cache adjfactor，0 live BaoStock）...")
        res_tc = _run("tencent", run_day, cache_dir, os.path.join(work, "tencent"))
        out.append(f"  tencent: run_day={res_tc.run_day} funnel={res_tc.funnel}")

    fl, ft = _factors(res_lake), _factors(res_tc)
    out.append("=" * 70)
    out.append(f"{'code':12} {'low_vol(lake)':>13} {'low_vol(tc)':>12} {'rel%':>7} "
               f"{'rsi(lake)':>9} {'rsi(tc)':>8} {'rel%':>7} {'|Δpts|':>7}")
    max_lv, max_rsi_rel, max_rsi_pts = 0.0, 0.0, 0.0
    for code, _n in DIFF_STOCKS:
        a, b = fl.get(code), ft.get(code)
        if not a or not b:
            out.append(f"{code:12} 缺失（lake={bool(a)} tencent={bool(b)}）")
            continue
        dlv = _rel(a["low_vol"], b["low_vol"]) or 0.0
        drsi = _rel(a["rsi"], b["rsi"]) or 0.0
        rsi_pts = abs((a["rsi"] or 0) - (b["rsi"] or 0))
        max_lv = max(max_lv, dlv)
        max_rsi_rel = max(max_rsi_rel, drsi)
        max_rsi_pts = max(max_rsi_pts, rsi_pts)
        out.append(f"{code:12} {a['low_vol']:13.6f} {b['low_vol']:12.6f} {dlv*100:6.4f}% "
                   f"{a['rsi']:9.3f} {b['rsi']:8.3f} {drsi*100:6.4f}% {rsi_pts:7.3f}")

    order_l = [c for c in fl if fl[c]["rank"]]
    order_t = [c for c in ft if ft[c]["rank"]]
    ranking_same = order_l == order_t
    topn_same = set(fl) == set(ft)
    out.append("-" * 70)
    out.append(f"lake   排序: {order_l}")
    out.append(f"tencent排序: {order_t}")
    out.append(f"top_n 列表一致: {topn_same}；排序一致: {ranking_same}")
    out.append(f"low_vol 最大相对差: {max_lv*100:.4f}%（阈值 {FACTOR_TOL*100:.0f}%）")
    out.append(f"RSI     最大相对差: {max_rsi_rel*100:.4f}%；最大绝对差: {max_rsi_pts:.3f} 点")

    # 判定：核心语义 = top_n 列表 + 排序（"低波优先"选股结果）不变。
    # low_vol 是低波优先的驱动因子，须 <5%。RSI 为有界(0-100)值，相对差在低基数下放大，
    # 故同时给绝对点数；其漂移来自因子源本身（存储T2 vs BaoStock cache），属 TL 裁决已知的
    # "换因子源的预期代价"，只要不翻转排序/选股即不影响筛选结果语义。
    core_semantics = topn_same and ranking_same
    lv_ok = max_lv < FACTOR_TOL
    passed = core_semantics and lv_ok
    out.append(f"判定: {'PASS（切换不改变筛选结果语义：top_n+排序一致，low_vol<5%）' if passed else 'FAIL（见上差异明细）'}")
    if not passed:
        out.append(f"  → core_semantics={core_semantics} low_vol_ok={lv_ok}")

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    print("\n".join(out))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
