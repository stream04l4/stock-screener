#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""martingale CLI（python -m martingale）。

子命令：
  backtest  全池/指定代码回测（默认股票池；参数取 config/strategy.yaml）
  status    当前持仓状态（逐股状态机投影：股数/均价/市值/分红/浮亏/下一触发价）
  pool      股票池（5 条件漏斗 + 成员）

进度标记（Web SSE 分类器解析）：
  [PROGRESS] stage=bt done=k total=N
完成标记（Web 结束判定，勿改词）：
  回测完成 · 窗口 ...
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import numpy as np

from . import __version__
from .config import ConfigError, load_config
from .data import connect_readonly, default_db_path
from .engine import aggregate_into, load, run_stock
from .report import write_outputs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG = os.path.join(ROOT, "config", "strategy.yaml")
DEFAULT_OUT = os.path.join(ROOT, "output", "backtest_martingale")


def _default_config_path() -> str:
    return os.environ.get("MARTINGALE_CONFIG") or DEFAULT_CONFIG


def _cmd_backtest(args) -> int:
    t0 = time.time()
    sp, up = load_config(_default_config_path())
    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    else:
        from .universe import build_pool
        con = connect_readonly(None)
        try:
            codes = build_pool(con, up).codes
        finally:
            con.close()
        if not codes:
            print("股票池为空（检查 strategy.yaml universe 段与数据湖）", file=sys.stderr)
            return 2
    con = connect_readonly(None)
    try:
        names, data, divs = load(con, codes=codes, start=args.start, end=args.end)
    finally:
        con.close()

    codes = sorted(codes)
    # 与参考一致：跳过 bar 数 < 2 的股
    codes = [c for c in codes if len(data[c][0]) >= 2]
    if not codes:
        print("no data")
        return 0

    gdates = np.unique(np.concatenate([data[c][0] for c in codes]))
    g_value = np.zeros(len(gdates))
    g_cost = np.zeros(len(gdates))
    g_open = np.zeros(len(gdates), dtype=np.int64)

    rows = []
    buys_by = {}
    bench_rets = []
    total = len(codes)
    for i, c in enumerate(codes, start=1):
        dates, sig, exec_c = data[c]
        stat, state, v_arr, c_arr, flag, buys = run_stock(
            c, dates, sig, exec_c, divs.get(c), sp)
        row = dict(stat)
        row["name"] = names.get(c, "")
        rows.append(row)
        buys_by[c] = buys
        bench_rets.append(float(sig[-1] / sig[0] - 1.0))
        aggregate_into(gdates, g_value, g_cost, g_open, dates, v_arr, c_arr, flag)
        print(f"[PROGRESS] stage=bt done={i} total={total}", flush=True)

    out_dir = args.out
    write_outputs(out_dir, sp, _db_label(), gdates, g_value, g_cost, g_open,
                  rows, buys_by, bench_rets, t0)
    d0 = str(np.datetime64(gdates[0], "D")) if len(gdates) else "?"
    d1 = str(np.datetime64(gdates[-1], "D")) if len(gdates) else "?"
    n_trig = sum(1 for r in rows if r["last_status"] != "never")
    print(f"回测完成 · 窗口 {d0} ~ {d1} · {len(rows)} 只（触发 {n_trig}）· 耗时 {time.time() - t0:.1f}s")
    print(f"产物: {out_dir}/{{per_stock.csv, buys.csv, equity_curve.csv, summary.md, status.json}}")
    return 0


def _db_label() -> str:
    return default_db_path()


def _cmd_status(args) -> int:
    t0 = time.time()
    sp, up = load_config(_default_config_path())
    con = connect_readonly(None)
    try:
        if args.codes:
            codes = [c.strip() for c in args.codes.split(",") if c.strip()]
        else:
            from .universe import build_pool
            codes = build_pool(con, up).codes
        names, data, divs = load(con, codes=codes, start=args.start, end=None)
    finally:
        con.close()
    codes = sorted(c for c in codes if len(data[c][0]) >= 2)
    if not codes:
        print("no data")
        return 0
    states = []
    total = len(codes)
    for i, c in enumerate(codes, start=1):
        dates, sig, exec_c = data[c]
        _, state, *_ = run_stock(c, dates, sig, exec_c, divs.get(c), sp)
        state["name"] = names.get(c, "")
        states.append(state)
        print(f"[PROGRESS] stage=st done={i} total={total}", flush=True)
    states.sort(key=lambda s: (s["ts_code"]))
    hdr = ["ts_code", "name", "in_pos", "shares", "avg_cost", "market_value",
           "cash_div", "unreal_pct", "k_level", "next_add_price", "next_rearm_price",
           "entry_high", "low_since_high", "last_price"]
    print("\n".join(["\t".join(hdr)]) )
    for s in states:
        def f(v, spec=".4f"):
            return ("-" if v is None else format(v, spec))
        print("\t".join([
            s["ts_code"], s["name"], str(s["in_pos"]),
            f(s["shares"]), f(s["avg_cost"]), f(s["market_value"]),
            f(s["cash_div"]), f(s["unreal_pct"], ".4f"),
            str(s["k_level"]), f(s["next_add_price"]), f(s["next_rearm_price"]),
            f(s["entry_high"]), f(s["low_since_high"]), f(s["last_price"]),
        ]))
    print(f"status 完成 · {len(states)} 只 · 耗时 {time.time() - t0:.1f}s")
    return 0


def _cmd_pool(args) -> int:
    sp, up = load_config(_default_config_path())
    con = connect_readonly(None)
    try:
        from .universe import build_pool
        pool = build_pool(con, up)
    finally:
        con.close()
    fu = pool.funnel
    print("股票池漏斗（全链 5 条件）：")
    print(f"  全市场: {fu['total']}")
    print(f"  ∩行业白名单+存活+非ST: {fu['whitelist']}")
    print(f"  ∩央国企(SOE双规则): {fu['soe']}")
    print(f"  ∩总市值≥{up.min_total_mv_yi:g}亿: {fu['mv']}")
    print(f"  ∩上市≥{up.listing_min_trading_days}交易日: {fu['listing']}")
    print(f"  ∩连续分红≥{up.min_consecutive_div_years}年: {fu['div']}")
    print(f"  最终池: {fu['final']}")
    print()
    print(f"{'ts_code':<12} {'name':<10} {'industry':<8} {'soe_basis':<8} "
          f"{'total_mv':>10} {'ttm_yield%':>10} {'n_bars':>7} {'list_date':<12}")
    for m in sorted(pool.members, key=lambda x: x.ts_code):
        print(f"{m.ts_code:<12} {m.name:<10} {m.industry_csric2:<8} {m.soe_basis:<8} "
              f"{(f'{m.total_mv:,.0f}' if m.total_mv is not None else '-'):>10} "
              f"{(f'{m.ttm_yield_pct:.2f}' if m.ttm_yield_pct is not None else '-'):>10} "
              f"{m.n_bars:>7} {(m.list_date or '-'):<12}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m martingale",
                                 description="深回撤金字塔加仓策略 CLI (v" + __version__ + ")")
    sub = ap.add_subparsers(dest="cmd", required=True)

    bt = sub.add_parser("backtest", help="回测（默认股票池；--codes 指定子集）")
    bt.add_argument("--start", default=None, help="YYYY-MM-DD，默认数据起点")
    bt.add_argument("--end", default=None, help="YYYY-MM-DD，默认数据终点")
    bt.add_argument("--codes", default=None, help="逗号分隔 ts_code（默认=股票池）")
    bt.add_argument("--out", default=DEFAULT_OUT, help="输出目录")
    bt.set_defaults(func=_cmd_backtest)

    st = sub.add_parser("status", help="当前持仓状态（逐股状态机投影）")
    st.add_argument("--start", default=None, help="YYYY-MM-DD，默认数据起点")
    st.add_argument("--codes", default=None, help="逗号分隔 ts_code（默认=股票池）")
    st.set_defaults(func=_cmd_status)

    po = sub.add_parser("pool", help="股票池（漏斗+成员）")
    po.set_defaults(func=_cmd_pool)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as e:
        print(f"配置错误: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001 - CLI 顶层归一（lake 错误已归一为子类）
        import traceback
        traceback.print_exc()
        print(f"错误: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
