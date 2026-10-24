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
    # 配置优先级：--config（web 层策略选择显式传入）> MARTINGALE_CONFIG 环境变量 > 默认 config/strategy.yaml
    sp, up, tp = load_config(args.config or _default_config_path())
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
    sp, up, tp = load_config(_default_config_path())
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
    sp, up, tp = load_config(_default_config_path())
    con = connect_readonly(None)
    try:
        from .selection import build_candidate_pool, load_manual_pool
        pool = build_candidate_pool(con, up, manual=load_manual_pool())
    finally:
        con.close()
    fu = pool.funnel
    print("候选池漏斗（基础 5 条件 + 选股门槛；候选池 ≠ 买入信号，买不买看价格）：")
    print(f"  全市场: {fu['total']}")
    print(f"  ∩行业白名单+存活+非ST: {fu['whitelist']}")
    print(f"  ∩央国企(SOE双规则): {fu['soe']}")
    print(f"  ∩总市值≥{up.min_total_mv_yi:g}亿: {fu['mv']}")
    print(f"  ∩上市≥{up.listing_min_trading_days}交易日: {fu['listing']}")
    print(f"  ∩连续分红≥{up.min_consecutive_div_years}年: {fu['div']}")
    print(f"  ∩{up.backtest_window_years}年回测为正: {fu['backtest']}")
    print(f"  ∩股息率≥{up.min_ttm_yield_pct:g}%: {fu['yield']}")
    print(f"  ∩价格{up.price_min_yuan:g}~{up.price_max_yuan:g}元: {fu['price']}")
    print(f"  候选池: {fu['final']}（含手动添加 {fu['manual']}）")
    print()
    print(f"{'ts_code':<12} {'name':<10} {'10y回测%':>9} {'股息%':>6} "
          f"{'价':>8} {'total_mv':>9} {'来源':<6}")
    for m in pool.members:
        bt = f"{m.bt_pnl_pct * 100:+.1f}" if m.bt_pnl_pct is not None else "未触发"
        src = "手动" if m.is_manual else "自动"
        print(f"{m.ts_code:<12} {m.name:<10} {bt:>9} "
              f"{(f'{m.ttm_yield_pct:.2f}' if m.ttm_yield_pct is not None else '-'):>6} "
              f"{(f'{m.price_latest:.2f}' if m.price_latest is not None else '-'):>8} "
              f"{(f'{m.total_mv:,.0f}' if m.total_mv is not None else '-'):>9} {src:<6}")
    return 0


def _cmd_selection(args) -> int:
    """候选池选股预计算：基础池逐股十年滚动回测 → 湖表 selection_backtest_10y。"""
    import duckdb

    from .data import default_db_path
    from .selection import refresh_selection

    sp, up, tp = load_config(_default_config_path())
    p = default_db_path()
    if not os.path.exists(p):
        print(f"数据湖不存在: {p}", file=sys.stderr)
        return 3
    con = duckdb.connect(p)  # 写连接：仅湖增量后/手动运维时调用（与 Web 读并发安全窗口外）
    try:
        out = refresh_selection(con, sp, up, verbose=True)
    finally:
        con.close()
    print(f"selection 完成 · as_of={out['as_of']} · 写入 {out['n_written']}/{out['n_codes']} "
          f"（回测为正 {out['n_positive']}）· 耗时 {out['elapsed_s']}s")
    print("提示：候选池立即生效（Web/CLI 读预计算表）")
    return 0


def _cmd_timing(args) -> int:
    """选股时机预计算 + 新触发微信提醒（候选池逐股 ①价格分位 ②股息率 ③年线）。"""
    import duckdb

    from .data import default_db_path
    from .timing import check_and_push, refresh_timing

    sp, up, tp = load_config(_default_config_path())
    p = default_db_path()
    if not os.path.exists(p):
        print(f"数据湖不存在: {p}", file=sys.stderr)
        return 3
    con = duckdb.connect(p)  # 写连接：仅湖增量后/手动运维时调用（同 selection 纪律）
    try:
        out = refresh_timing(con, up, tp, verbose=True)
        if args.no_push:
            res = {"n_new": None, "pushed": False, "error": None}
        else:
            res = check_and_push(con, tp, dry_run=args.dry_run, verbose=True)
    finally:
        con.close()
    new_s = f"新触发 {res['n_new']} 只" if res.get("n_new") is not None else "未检查推送（--no-push）"
    print(f"timing 完成 · as_of={out['as_of']} · 候选池 {out['n_codes']} 只 · "
          f"今日选股 {out['n_watchlist']} · 提醒口径 {out['n_push']} · "
          f"{new_s} · 耗时 {out['elapsed_s']}s")
    if res.get("pushed"):
        print("微信提醒已推送（仅新触发）")
    elif res.get("error"):
        print(f"推送未发生: {res['error']}", file=sys.stderr)
        return 4
    print("提示：页面「选股策略」卡片即时可读（湖表 selection_timing）")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m martingale",
                                 description="深回撤金字塔加仓策略 CLI (v" + __version__ + ")")
    sub = ap.add_subparsers(dest="cmd", required=True)

    bt = sub.add_parser("backtest", help="回测（默认股票池；--codes 指定子集）")
    bt.add_argument("--start", default=None, help="YYYY-MM-DD，默认数据起点")
    bt.add_argument("--end", default=None, help="YYYY-MM-DD，默认数据终点")
    bt.add_argument("--codes", default=None, help="逗号分隔 ts_code（默认=股票池）")
    bt.add_argument("--config", default=None,
                    help="策略 yaml 路径（默认 config/strategy.yaml，或 MARTINGALE_CONFIG 环境变量）")
    bt.add_argument("--out", default=DEFAULT_OUT, help="输出目录")
    bt.set_defaults(func=_cmd_backtest)

    st = sub.add_parser("status", help="当前持仓状态（逐股状态机投影）")
    st.add_argument("--start", default=None, help="YYYY-MM-DD，默认数据起点")
    st.add_argument("--codes", default=None, help="逗号分隔 ts_code（默认=股票池）")
    st.set_defaults(func=_cmd_status)

    po = sub.add_parser("pool", help="候选池（漏斗+成员；含选股门槛与手动添加）")
    po.set_defaults(func=_cmd_pool)

    se = sub.add_parser("selection",
                        help="候选池选股预计算（基础池十年滚动回测→湖表；湖增量后自动跑）")
    se.set_defaults(func=_cmd_selection)

    tm = sub.add_parser("timing",
                        help="选股时机预计算+新触发微信提醒（候选池 ①价格分位 ②股息率 ③年线→湖表 selection_timing）")
    tm.add_argument("--no-push", action="store_true", help="只算不推（不查边沿状态）")
    tm.add_argument("--dry-run", action="store_true",
                    help="算+打印新触发，不推送不落状态（手动验证用）")
    tm.set_defaults(func=_cmd_timing)

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
