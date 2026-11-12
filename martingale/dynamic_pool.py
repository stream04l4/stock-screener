"""v8.0.13 动态池回测（Joel 2026-11-12 定案）：股票池随时间重算 + 交易门控。

背景：旧「按股票池回测」= 拿**当前静态池**固定回测全窗口——今天的池 ≠
历史上的池（近年才达标的股从窗口第一天就交易了，Joel：「怎么出来一堆
我没见过的股票」）。本模块把池筛选加进回测时间轴：

- **重算频率 = 每月**（每月第一个交易日重算全漏斗；120 次/十年）。
- **漏斗口径 = 生产候选池**（build_pool_asof 基础 6 门 ∩ G7 滚动十年
  策略回测 ∩ G8 TTM ∩ G9 价格区间，全部 point-in-time）；不含手动池
  （历史回测无「我的自选」概念）。
- **出池语义 = 冻结开仓+加仓、不强制平仓**（策略无卖出信号；经引擎
  run_stock(trade_mask) 门控买入块实现，状态机其余照常）。
- **PIT 近似**（README §动态池 有载）：退市/ST 按 as_of 从严；holders 取
  ≤ as_of 最近季度快照（缺期沿用更早快照）。

产物（经 report.write_outputs pool_schedule 参数）：per_stock.csv +
pool_months/pool_first/pool_last 三列、pool_timeline.csv（月度入池/出池）、
status.json mode=dynamic_pool。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional

import numpy as np

from .config import TimingParams
from .engine import aggregate_into, extend_start, first_ok_series, load, run_stock
from .report import write_outputs
from .universe import build_pool_asof

# 滚动回测门 G7 的窗口长度（与 selection 十年滚动口径一致）
ROLLING_DAYS = 3650
# 仓库根（子进程 cwd + -m martingale 可导入）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 月度重算子进程并发数（子进程峰值 RSS ~1.3GB，本机 11GB 留 3 路余量）
MONTH_WORKERS = 3


def month_starts(dates: np.ndarray) -> np.ndarray:
    """每月第一个交易日（dates = 排序后的全局交易日历，datetime64[ns]）。"""
    if len(dates) == 0:
        return dates
    ym = np.datetime_as_string(dates, unit="M").astype("U7")
    first_idx = np.flatnonzero(np.concatenate(([True], ym[1:] != ym[:-1])))
    return dates[first_idx]


def _rolling_g7(con, sp, codes: List[str], end_d: str,
                min_return_pct: float,
                tp: Optional[TimingParams] = None) -> Dict[str, dict]:
    """滚动十年策略回测（窗口 [end-3650d, end]），返回 {code: stat}。

    G7 过滤在调用方做（与 selection.build_candidate_pool 同语义：
    n_buys≥1 且 pnl_pct×100 ≥ 门槛）。verbose=False 避免 400 股×120 次
    的日志洪水；进度由上层按重算日期粒度打 [PROGRESS]。
    tp=v8.1.0 首仓双条件②（timing 段）——G7 回测与生产同口径（PIT）；
    None = ②关闭（仅单元测试兼容）。
    """
    start10 = str(np.datetime64(end_d, "D") - np.timedelta64(ROLLING_DAYS, "D"))[:10]
    # v8.1.0：分位窗口 → load 起点前移 N 年，算完切回原窗口
    ext_start = (extend_start(start10, tp.price_window_years)
                 if tp is not None else start10)
    _names, data, divs = load(con, codes=codes, start=ext_start, end=end_d)
    out: Dict[str, dict] = {}
    for c in codes:
        d, s, e = data[c]
        if len(d) < 2:
            continue
        ok = None
        if tp is not None:
            ok = first_ok_series(d, s, tp.price_window_years,
                                 tp.price_pctile_max)
            i0 = int(np.searchsorted(d, np.datetime64(start10)))
            d, s, e, ok = d[i0:], s[i0:], e[i0:], ok[i0:]
        stat, _state, _v, _cc, _fl, _buys = run_stock(
            c, d, s, e, divs.get(c), sp, verbose=False, first_ok=ok)
        out[c] = stat
    return out


def _month_pool(con, sp, up, rd: str,
                tp: Optional[TimingParams] = None) -> set:
    """单月全漏斗（PIT）：基础 6 门 ∩ G7 滚动十年 ∩ G8 TTM ∩ G9 价格区间。"""
    base = build_pool_asof(con, up, rd)
    members = base.members
    # G7（滚动十年策略回测；up.backtest_positive=False 时关闭 = 基础池直通）
    if up.backtest_positive and members:
        g7 = _rolling_g7(con, sp, [m.ts_code for m in members], rd,
                         up.backtest_min_return_pct, tp=tp)
        codes_g7 = [
            m.ts_code for m in members
            if (b := g7.get(m.ts_code)) is not None
            and int(b.get("n_buys") or 0) >= 1
            and b.get("pnl_pct") is not None
            and float(b["pnl_pct"]) * 100.0 >= up.backtest_min_return_pct
        ]
    else:
        codes_g7 = [m.ts_code for m in members]
    # G8（TTM PIT，build_pool_asof 已按 as_of 取）
    by_code = {m.ts_code: m for m in members}
    codes_g8 = [c for c in codes_g7
                if (by_code[c].ttm_yield_pct is not None
                    and by_code[c].ttm_yield_pct >= up.min_ttm_yield_pct)]
    # G9（最新收盘价 ≤ as_of，区间 [price_min, price_max]）
    price_map: Dict[str, float] = {}
    if codes_g8:
        fmt = ", ".join(["?"] * len(codes_g8))
        con.execute(f"""
            SELECT k.ts_code, k.close FROM kline_daily k
            JOIN (SELECT ts_code, MAX(date) md FROM kline_daily
                  WHERE ts_code IN ({fmt}) AND date <= ? GROUP BY ts_code) x
              ON k.ts_code = x.ts_code AND k.date = x.md
            WHERE k.ts_code IN ({fmt})
        """, codes_g8 + [rd] + codes_g8)
        price_map = {c: float(cl) for c, cl in con.fetchall() if cl is not None}
    codes_g9 = [c for c in codes_g8
                if c in price_map
                and up.price_min_yuan <= price_map[c] <= up.price_max_yuan]
    return set(codes_g9)


def compute_month_pool(as_of: str, config_path: Optional[str] = None) -> List[str]:
    """单月池的独立进程入口（`martingale dynpool-month --as-of …` 消费）。

    每次重算一个进程：DuckDB/mimalloc 的 C 层 allocator 滞留 ~100MB/月且不
    归还 OS（gc/重连均无效，2026-11-12 三次 OOM 实踩）——进程退出即归零。
    """
    from .config import load_config
    from .data import connect_readonly
    # v8.1.0：G7 回测首仓双条件② → 子进程同样取 timing 段（生产同口径）
    sp, up, tp = load_config(config_path or os.path.join(_ROOT, "config", "strategy.yaml"))
    con = connect_readonly(None)
    try:
        con.execute("SET memory_limit='2GB'")
        return sorted(_month_pool(con, sp, up, as_of, tp=tp))
    finally:
        con.close()


def _subprocess_month_pool(rd: str, config_path: Optional[str]) -> set:
    """父进程侧：跑一个一次性子进程算单月池（超时 10 分钟，失败 fail-closed）。"""
    cmd = [sys.executable, "-m", "martingale", "dynpool-month", "--as-of", rd]
    if config_path:
        cmd += ["--config", config_path]
    proc = subprocess.run(cmd, cwd=_ROOT, capture_output=True, text=True,
                          timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(
            f"dynpool-month {rd} 失败 rc={proc.returncode}: "
            f"{(proc.stderr or '').strip()[-300:]}")
    return set(json.loads(proc.stdout.strip().splitlines()[-1])["codes"])


def pool_schedule(con, sp, up, start: str, end: str,
                  progress: Callable[[str, int, int], None] = None,
                  use_subprocess: Optional[bool] = None,
                  config_path: Optional[str] = None,
                  tp: Optional[TimingParams] = None) -> Dict[str, set]:
    """计算 [start, end] 内的月度池重算表 {as_of_date: set(ts_code)}。

    每次重算 = build_pool_asof（基础 6 门 PIT）∩ G7（滚动十年回测）∩ G8
    （TTM PIT）∩ G9（价格区间 PIT）。:param progress: (label, done, total)。
    :param use_subprocess: None=自动——文件湖走一次性子进程（内存归零，见
      compute_month_pool docstring）；内存湖（测试）走进程内。
    :param tp: v8.1.0 首仓双条件②（timing 段）——G7 同口径（子进程路径
      经 config_path 自行加载，此参数只影响进程内路径）。
    """
    con.execute(
        "SELECT DISTINCT date FROM kline_daily WHERE date >= ? AND date <= ? "
        "ORDER BY date", [start, end])
    cal = np.array([r[0] for r in con.fetchall()], dtype="datetime64[ns]")
    if len(cal) == 0:
        return {}
    rdates = month_starts(cal)
    rdates = [str(d)[:10] for d in rdates]

    if use_subprocess is None:
        # 文件湖（path 非空）→ 子进程；内存湖（测试，path=None）→ 进程内
        try:
            use_subprocess = any(
                r[1] for r in con.execute(
                    "SELECT database_name, path FROM duckdb_databases()").fetchall())
        except Exception:
            use_subprocess = False

    schedule: Dict[str, set] = {}
    total = len(rdates)
    done = 0
    if use_subprocess:
        # 一次性子进程 × 3 路并行（子进程退出即释放 allocator 滞留内存）
        with ThreadPoolExecutor(max_workers=MONTH_WORKERS) as ex:
            futs = {ex.submit(_subprocess_month_pool, rd, config_path): rd
                    for rd in rdates}
            for fut in futs:
                rd = futs[fut]
                schedule[rd] = fut.result()   # 子进程异常 → 直接抛出（fail-closed）
                done += 1
                if progress:
                    progress("pool", done, total)
    else:
        for k, rd in enumerate(rdates, start=1):
            schedule[rd] = _month_pool(con, sp, up, rd, tp=tp)
            if progress:
                progress("pool", k, total)
    return schedule


def build_code_mask(code: str, code_dates: np.ndarray, rdates: List[str],
                    schedule: Dict[str, set]) -> np.ndarray:
    """单股的在池掩码（build_trade_mask 的正确实现入口）。"""
    mask = np.zeros(len(code_dates), dtype=bool)
    if not rdates or len(code_dates) == 0:
        return mask
    rd_ns = np.array(rdates, dtype="datetime64[ns]")
    pos = np.searchsorted(rd_ns, code_dates, side="right") - 1
    for p in np.unique(pos):
        if p < 0:
            continue
        members = schedule[rdates[int(p)]]
        mask[pos == p] = code in members
    return mask


def run_dynamic_pool_backtest(con, sp, up, start: str, end: str, out_dir: str,
                              t0: float, db_label: str,
                              config_path: Optional[str] = None,
                              tp: Optional[TimingParams] = None) -> int:
    """动态池回测主流程：池重算 → 并集 → 带掩码逐股回测 → 产物。

    :param start/: end: None = 数据起点/终点（解析后全程非空）。
    :param tp: v8.1.0 首仓双条件②（timing 段）——G7 重算 + 主回测同口径；
        None = ②关闭（仅单元测试兼容）。
    """
    # v8.0.13 内存纪律：DuckDB 默认 memory_limit=物理内存 80%（本机 8.8GB），
    # 月度重算的整湖扫描查询会把缓冲池顶满且不归还 OS → 全局 OOM（10.2GB 实踩）。
    # 回测读连接限 2GB，超额自动落盘 spill。
    con.execute("SET memory_limit='2GB'")
    # 默认窗口：end 缺省 = 湖 K线终点；start 缺省 = **end − ROLLING_DAYS**（动态池
    # 围绕滚动十年 G7 门设计，全跨度默认会 35 年 430+ 次重算 → 内存/时长爆炸，
    # 且 2016 前 SOE/分红 PIT 数据稀疏、池无意义；Joel 定案口径 = 十年动态池）
    if end is None or start is None:
        hi = con.execute("SELECT MAX(date) FROM kline_daily").fetchone()[0]
        end = end or str(hi)[:10]
    if start is None:
        start = str(np.datetime64(end, "D") - np.timedelta64(ROLLING_DAYS, "D"))[:10]
    schedule = pool_schedule(con, sp, up, start, end,
                             progress=lambda s, d, t: print(
                                 f"[PROGRESS] stage={s} done={d} total={t}",
                                 flush=True),
                             config_path=config_path, tp=tp)
    if not schedule:
        print("无交易日数据（窗口内湖无 K线）")
        return 0
    rdates = sorted(schedule)
    union: set = set()
    for s in schedule.values():
        union |= s
    if not union:
        print("动态池全程为空（漏斗过严或数据不足）", flush=True)
        return 2
    union = sorted(union)
    print(f"[dynamic-pool] 重算 {len(rdates)} 次（{rdates[0]} ~ {rdates[-1]}）· "
          f"池规模 min={min(len(s) for s in schedule.values())} "
          f"max={max(len(s) for s in schedule.values())} · 并集 {len(union)} 只",
          flush=True)

    # v8.1.0：首仓双条件②（5 年分位）窗口 → load 起点前移 N 年，算完分位
    # 切回原窗口（扩展段只喂分位窗口，不进回测日历/掩码）
    ext_start = (extend_start(start, tp.price_window_years)
                 if tp is not None else start)
    names, data, divs = load(con, codes=union, start=ext_start, end=end)
    prepared: Dict[str, tuple] = {}
    for c in union:
        d, s, e = data[c]
        if len(d) < 2:
            continue
        ok = (first_ok_series(d, s, tp.price_window_years,
                              tp.price_pctile_max)
              if tp is not None else None)
        i0 = int(np.searchsorted(d, np.datetime64(start)))
        prepared[c] = (d[i0:], s[i0:], e[i0:], ok[i0:] if ok is not None else None)
    codes = [c for c in union if c in prepared]
    if not codes:
        print("no data")
        return 0

    con.execute(
        "SELECT DISTINCT date FROM kline_daily WHERE date >= ? AND date <= ? "
        "ORDER BY date", [start, end])
    gdates = np.array([r[0] for r in con.fetchall()], dtype="datetime64[ns]")
    g_value = np.zeros(len(gdates))
    g_cost = np.zeros(len(gdates))
    g_open = np.zeros(len(gdates), dtype=np.int64)

    rows = []
    buys_by = {}
    bench_rets = []
    total = len(codes)
    for i, c in enumerate(codes, start=1):
        dates, sig, exec_c, ok = prepared[c]
        mask = build_code_mask(c, dates, rdates, schedule)
        stat, _state, v_arr, c_arr, flag, buys = run_stock(
            c, dates, sig, exec_c, divs.get(c), sp, verbose=False,
            trade_mask=mask, first_ok=ok)
        row = dict(stat)
        row["name"] = names.get(c, "")
        # 池履历（重算粒度 = 月）
        in_months = [d for d in rdates if c in schedule[d]]
        row["pool_months"] = len(in_months) if in_months else 0
        row["pool_first"] = in_months[0] if in_months else ""
        row["pool_last"] = in_months[-1] if in_months else ""
        rows.append(row)
        buys_by[c] = buys
        bench_rets.append(float(sig[-1] / sig[0] - 1.0))
        aggregate_into(gdates, g_value, g_cost, g_open, dates, v_arr, c_arr, flag)
        print(f"[PROGRESS] stage=bt done={i} total={total}", flush=True)

    write_outputs(out_dir, sp, db_label, gdates, g_value, g_cost, g_open,
                  rows, buys_by, bench_rets, t0,
                  pool_schedule=schedule, mode="dynamic_pool")
    d0 = str(np.datetime64(gdates[0], "D")) if len(gdates) else "?"
    d1 = str(np.datetime64(gdates[-1], "D")) if len(gdates) else "?"
    n_trig = sum(1 for r in rows if r["last_status"] != "never")
    print(f"回测完成 · 动态池 {d0} ~ {d1} · {len(rows)} 只（触发 {n_trig}）· "
          f"重算 {len(rdates)} 次 · 耗时 {time.time() - t0:.1f}s", flush=True)
    print(f"产物: {out_dir}/{{per_stock.csv, buys.csv, equity_curve.csv, "
          f"pool_timeline.csv, summary.md, status.json}}", flush=True)
    return 0
