# -*- coding: utf-8 -*-
"""crypto.backtest.paramsearch —— 参数网格寻优（v7.0.55）。

对 建仓幅度（first_drop_pct，高点回撤%）× 止盈倍数（take_profit_mult）×
加仓倍数（growth） 做网格回测，输出 markdown 对比表。

用法：
    python -m crypto.backtest.paramsearch \
        --start 2021-11-11 --end 2026-11-11 \
        --coins BTCUSDT,ETHUSDT \
        --drops 50,60,70,80 --tps 3,5,8,10 --grs 1.02,1.05,1.08,1.1

口径：窗口 = [start, end]（UTC 日界，end 含当日）；窗口内账户从空开始，
一年高点用窗口前 lookback_h(8760h) 根历史做基线（v7.0.54 时间范围回测）。
指标：交易频率（了结轮次/窗口年数）、总外部投入、回报率 = 期末净值/外部−1
（含未了结浮盈）、资金周转效率 CAGR = (期末净值/外部)^(1/年数)−1、
已了结轮次平均持仓天数、MDD。
"""
from __future__ import annotations

import argparse
import itertools
import os
import re
import time
from datetime import datetime

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CFG_PATH = os.path.join(ROOT, "crypto", "config", "crypto.yaml")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

from crypto.strategy.engine import StrategyParams      # noqa: E402
from crypto.backtest.runner import run_backtest        # noqa: E402


def _parse_list(s: str, cast) -> list:
    return [cast(x.strip()) for x in s.split(",") if x.strip()]


def _metrics(r: dict, years: float) -> dict:
    rounds, o = r["rounds"], r["open_round"]
    ext, eq = r["external_invested"], r["final_equity"]
    kmax_list = [x["max_level"] for x in rounds] + ([o["k_level"]] if o else [])
    return {
        "rounds": len(rounds),
        "open": o,
        "ext": ext,
        "eq": eq,
        "roi": (eq / ext - 1.0) * 100.0 if ext > 0 else None,
        "cagr": ((eq / ext) ** (1.0 / years) - 1.0) * 100.0
                if (ext > 0 and eq > 0) else (-100.0 if ext > 0 else None),
        "avg_days": (sum(x["bars"] for x in rounds) / len(rounds) / 24.0)
                    if rounds else None,
        "mdd": r["mdd_pct"],
        "k_max": max(kmax_list) if kmax_list else None,
    }


def _fmt_row(d: float, tp: float, gr: float, m: dict) -> str:
    o = m["open"]
    op_s = (f"{o['entry_date']} 入 L{o['k_level']} 浮盈 {o['unrealized_usdt']:+.0f}U"
            if o else "—")
    roi_s = f"{m['roi']:+.0f}%" if m["roi"] is not None else "—"
    cagr_s = f"{m['cagr']:+.1f}%" if m["cagr"] is not None else "—"
    avg_s = f"{m['avg_days']:.0f}" if m["avg_days"] is not None else "—"
    return (f"| {d:g}% | {tp:g}x | {gr:g} | {m['rounds']} | {op_s} | "
            f"{m['ext']:.0f} | {m['eq']:.0f} | {roi_s} | {cagr_s} | {avg_s} | "
            f"{m['mdd']:.1f} |")


def run_search(drops: list, tps: list, grs: list, coins: list,
               start: str, end: str) -> dict:
    """执行网格寻优 → {coin: [(drop, tp, growth, metrics), ...]}（按 CAGR 降序）。"""
    with open(CFG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    base = StrategyParams.from_cfg(cfg)
    years = max((datetime.strptime(end, "%Y-%m-%d")
                 - datetime.strptime(start, "%Y-%m-%d")).days / 365.25, 1e-9)
    out: dict[str, list] = {}
    t0 = time.time()
    for coin in coins:
        rows = []
        for d, tp, gr in itertools.product(drops, tps, grs):
            p = StrategyParams(first_drop_pct=float(d),
                               add_grid_pct=base.add_grid_pct,
                               base_amount_usdt=base.base_amount_usdt,
                               growth=gr, take_profit_mult=tp,
                               fee_bps=base.fee_bps, lookback_h=base.lookback_h)
            r = run_backtest([coin], p, start=start, end=end)[0]
            rows.append((d, tp, gr, _metrics(r, years)))
        rows.sort(key=lambda x: (x[3]["ext"] > 0, x[3]["cagr"] or -999),
                  reverse=True)
        out[coin] = rows
        print(f"[paramsearch] {coin}: {len(rows)} 组合，"
              f"{sum(1 for x in rows if x[3]['ext'] > 0)} 个有投入，"
              f"{time.time() - t0:.0f}s")
    return out


def render_report(rows_by_coin: dict, drops: list, tps: list, grs: list,
                  start: str, end: str, years: float) -> str:
    hdr = ("| 建仓幅度 | 止盈 | 加仓 | 轮次 | 持仓中 | 外部投入(U) | "
           "期末净值(U) | 回报率 | 周转CAGR | 均持仓(d) | MDD |")
    sep = "|---|---|---|---|---|---|---|---|---|---|---|"
    out = [
        "# 加密货币参数寻优",
        "",
        f"窗口：{start} → {end}（UTC 日界，end 含当日；窗口内账户从空开始，"
        "一年高点用窗口前 8760h 历史做基线）。fee 10bps 双向。",
        f"网格：建仓幅度(高点回撤) {drops} × 止盈 {tps} × 加仓 {grs}，"
        f"每币 {len(drops) * len(tps) * len(grs)} 组合。",
        "",
        "指标口径：回报率 = 期末净值/总外部投入 − 1（含未了结浮盈）；"
        "周转 CAGR = (期末净值/总外部投入)^(1/年数) − 1；"
        "均持仓 = 已了结轮次平均持有天数；MDD = 净值曲线最大回撤。",
    ]
    for coin, rows in rows_by_coin.items():
        out += ["", f"## {coin}", "", hdr, sep]
        for d, tp, gr, m in rows:
            out.append(_fmt_row(d, tp, gr, m))
        top = [x for x in rows if x[3]["ext"] > 0][:5]
        out += ["", f"### {coin} TOP5（按周转 CAGR）", ""]
        for i, (d, tp, gr, m) in enumerate(top, 1):
            o = m["open"]
            op_s = (f"{o['entry_date']} 入 L{o['k_level']}（浮盈 "
                    f"{o['unrealized_usdt']:+.0f}U）" if o else "已空仓")
            out.append(f"{i}. 回撤{d:g}% / 止盈{tp:g}x / 加仓{gr:g} — "
                       f"轮次 {m['rounds']}（{op_s}），外部 {m['ext']:.0f} U → "
                       f"净值 {m['eq']:.0f} U，回报率 {m['roi']:+.0f}%，"
                       f"周转 {m['cagr']:+.1f}%/年，MDD {m['mdd']:.1f}%")
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m crypto.backtest.paramsearch")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD（UTC）")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD（UTC，含当日）")
    ap.add_argument("--coins", default="", help="逗号分隔；缺省 = yaml coins")
    ap.add_argument("--drops", default="50,60,70,80", help="高点回撤%%，逗号分隔")
    ap.add_argument("--tps", default="3,5,8,10", help="止盈倍数，逗号分隔")
    ap.add_argument("--grs", default="1.02,1.05,1.08,1.1", help="加仓倍数，逗号分隔")
    ap.add_argument("--out", default=os.path.join(
        ROOT, "data", "crypto", "param_search.md"), help="报告输出路径")
    args = ap.parse_args(argv)
    for v in (args.start, args.end):
        if not _DATE_RE.match(v):
            raise SystemExit(f"用法错误：日期需 YYYY-MM-DD：{v!r}")
    if args.start >= args.end:
        raise SystemExit("用法错误：start 需早于 end")

    with open(CFG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    coins = _parse_list(args.coins, str) or cfg["coins"]
    drops = _parse_list(args.drops, float)
    tps = _parse_list(args.tps, float)
    grs = _parse_list(args.grs, float)
    years = max((datetime.strptime(args.end, "%Y-%m-%d")
                 - datetime.strptime(args.start, "%Y-%m-%d")).days / 365.25, 1e-9)

    rows_by_coin = run_search(drops, tps, grs, coins, args.start, args.end)
    report = render_report(rows_by_coin, drops, tps, grs,
                           args.start, args.end, years)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"[paramsearch] 报告 → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
