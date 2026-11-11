# -*- coding: utf-8 -*-
"""crypto.backtest.paramsearch —— 参数网格寻优（v7.0.55；v7.0.56 资金预算）。

对 起始资金（base_amount_usdt）× 建仓幅度（first_drop_pct，高点回撤%）×
止盈倍数（take_profit_mult）× 加仓倍数（growth） 做网格回测，输出
markdown 对比表。

用法：
    python -m crypto.backtest.paramsearch \
        --start 2021-11-11 --end 2026-11-11 \
        --coins BTCUSDT,ETHUSDT \
        --base 10,30,100,300,1000 --drops 50,60,70 \
        --tps 3,5,8,10 --grs 1.02,1.05,1.08,1.1 \
        --budget 3000

口径：窗口 = [start, end]（UTC 日界，end 含当日）；窗口内账户从空开始，
一年高点用窗口前 lookback_h(8760h) 根历史做基线（v7.0.54 时间范围回测）。
预算（v7.0.56）：--budget = 外部投入硬预算（USDT，累计口径，0 = 不限）；
买不起的档位整档放弃（不做部分买入），余额（了结所得滚存）不受限。
指标：交易频率（了结轮次）、外部投入（/预算）、回报率 = 期末净值/外部−1
（含未了结浮盈）、资金周转效率 CAGR = (期末净值/外部)^(1/年数)−1、
均持仓天数、MDD；按 CAGR 降序。
"""
from __future__ import annotations

import argparse
import itertools
import math
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


def _cover_levels(base: float, gr: float, budget: float):
    """budget U 外部投入在 base/gr 下能完整买到的最大档位（0 起，L0=首仓）。

    解析式 + 浮点边界前向/回退校验：base·Σ_{j=0..k} gr^j ≤ budget。
    budget<=0（不限）→ None；base>budget（首仓都买不起）→ -1。
    """
    if not budget or budget <= 0 or base <= 0:
        return None
    if base > budget:
        return -1
    if abs(gr - 1.0) < 1e-12:
        return int(budget // base) - 1
    # base*(gr^(k+1)-1)/(gr-1) ≤ budget → gr^(k+1) ≤ 1 + budget*(gr-1)/base
    rhs = 1.0 + budget * (gr - 1.0) / base
    k = int(math.floor(math.log(rhs) / math.log(gr))) - 1
    while base * (gr ** (k + 2) - 1.0) / (gr - 1.0) <= budget + 1e-9:
        k += 1
    while k > -1 and base * (gr ** (k + 1) - 1.0) / (gr - 1.0) > budget + 1e-9:
        k -= 1
    return k


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
        "capped": (o and o.get("capped"))
                  or any(x.get("capped") for x in rounds),
    }


def _fmt_row(base, d: float, tp: float, gr: float, m: dict,
             budget: float) -> str:
    o = m["open"]
    op_s = "—"
    if o:
        op_s = f"{o['entry_date']} 入 L{o['k_level']}" \
               f"{'✂' if o.get('capped') else ''} 浮盈 {o['unrealized_usdt']:+.0f}U"
    roi_s = f"{m['roi']:+.0f}%" if m["roi"] is not None else "—"
    cagr_s = f"{m['cagr']:+.1f}%" if m["cagr"] is not None else "—"
    avg_s = f"{m['avg_days']:.0f}" if m["avg_days"] is not None else "—"
    cap_s = "✂" if m["capped"] else ""
    cov_s = (f"L{m['cover']}" if m.get("cover") is not None else "∞")
    return (f"| {base:g} | {d:g}% | {tp:g}x | {gr:g} | {m['rounds']} | {op_s} | "
            f"{m['ext']:.0f}/{budget:g}{cap_s} | {cov_s} | {m['eq']:.0f} | {roi_s} | "
            f"{cagr_s} | {avg_s} | {m['mdd']:.1f} |")


def run_search(drops: list, tps: list, grs: list, bases: list, coins: list,
               start: str, end: str, budget: float = 0.0) -> dict:
    """执行网格寻优 → {coin: [(base, drop, tp, growth, metrics), ...]}。"""
    with open(CFG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    base_p = StrategyParams.from_cfg(cfg)
    years = max((datetime.strptime(end, "%Y-%m-%d")
                 - datetime.strptime(start, "%Y-%m-%d")).days / 365.25, 1e-9)
    out: dict[str, list] = {}
    t0 = time.time()
    for coin in coins:
        rows = []
        for base, d, tp, gr in itertools.product(bases, drops, tps, grs):
            p = StrategyParams(first_drop_pct=float(d),
                               add_grid_pct=base_p.add_grid_pct,
                               base_amount_usdt=float(base),
                               growth=gr, take_profit_mult=tp,
                               fee_bps=base_p.fee_bps,
                               lookback_h=base_p.lookback_h)
            r = run_backtest([coin], p, start=start, end=end,
                             external_cap=budget or None)[0]
            m = _metrics(r, years)
            m["cover"] = _cover_levels(float(base), gr, budget)
            rows.append((base, d, tp, gr, m))
        rows.sort(key=lambda x: (x[4]["ext"] > 0, x[4]["cagr"] or -999),
                  reverse=True)
        out[coin] = rows
        print(f"[paramsearch] {coin}: {len(rows)} 组合，"
              f"{sum(1 for x in rows if x[4]['ext'] > 0)} 个有投入，"
              f"{time.time() - t0:.0f}s")
    return out


def render_report(rows_by_coin: dict, drops: list, tps: list, grs: list,
                  bases: list, start: str, end: str,
                  years: float, budget: float) -> str:
    hdr = ("| 起始资金(U) | 建仓幅度 | 止盈 | 加仓 | 轮次 | 持仓中 | "
           "外部投入/预算 | 预算可至 | 期末净值(U) | 回报率 | 周转CAGR | 均持仓(d) | MDD |")
    sep = "|---|---|---|---|---|---|---|---|---|---|---|---|---|"
    out = [
        "# 加密货币参数寻优",
        "",
        f"窗口：{start} → {end}（UTC 日界，end 含当日；窗口内账户从空开始，"
        "一年高点用窗口前 8760h 历史做基线）。fee 10bps 双向。",
        f"网格：起始资金 {bases} × 建仓幅度(高点回撤) {drops} × "
        f"止盈 {tps} × 加仓 {grs}，"
        f"每币 {len(bases) * len(drops) * len(tps) * len(grs)} 组合。",
    ]
    if budget:
        out.append(f"预算：外部投入硬上限 {budget:g} USDT（买不起的档位整档"
                   "放弃，不做部分买入；了结所得滚存不受限）。✂ = 被预算截断。")
        out += [
            "",
            f"### 预算覆盖深度：{budget:g} U 外部投入能完整买到的最大档位"
            "（L0 = 首仓，L80 = 价格较建仓价再跌 80%）",
            "",
            "| 起始 \\ 加仓 | " + " | ".join(f"{g:g}" for g in grs) + " |",
            "|" + "---|" * (len(grs) + 1),
        ]
        for b in bases:
            out.append("| " + f"{b:g} | " + " | ".join(
                f"L{_cover_levels(b, g, budget)}" for g in grs) + " |")
    out += [
        "",
        "指标口径：回报率 = 期末净值/总外部投入 − 1（含未了结浮盈）；"
        "周转 CAGR = (期末净值/总外部投入)^(1/年数) − 1；"
        "均持仓 = 已了结轮次平均持有天数；MDD = 净值曲线最大回撤。",
    ]
    for coin, rows in rows_by_coin.items():
        out += ["", f"## {coin}", "", hdr, sep]
        for base, d, tp, gr, m in rows:
            out.append(_fmt_row(base, d, tp, gr, m, budget))
        top = [x for x in rows if x[4]["ext"] > 0][:5]
        out += ["", f"### {coin} TOP5（按周转 CAGR）", ""]
        for i, (base, d, tp, gr, m) in enumerate(top, 1):
            o = m["open"]
            op_s = (f"{o['entry_date']} 入 L{o['k_level']}"
                    f"{'（预算截断）' if o.get('capped') else ''}"
                    f"（浮盈 {o['unrealized_usdt']:+.0f}U）" if o else "已空仓")
            cap_s = "，被预算截断" if m["capped"] else ""
            out.append(f"{i}. 起始{base:g} / 回撤{d:g}% / 止盈{tp:g}x / "
                       f"加仓{gr:g} — 轮次 {m['rounds']}（{op_s}），"
                       f"外部 {m['ext']:.0f} U{cap_s} → 净值 {m['eq']:.0f} U，"
                       f"回报率 {m['roi']:+.0f}%，周转 {m['cagr']:+.1f}%/年，"
                       f"MDD {m['mdd']:.1f}%")
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m crypto.backtest.paramsearch")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD（UTC）")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD（UTC，含当日）")
    ap.add_argument("--coins", default="", help="逗号分隔；缺省 = yaml coins")
    ap.add_argument("--base", default="10,30,100,300,1000",
                    help="起始资金 USDT，逗号分隔")
    ap.add_argument("--drops", default="50,60,70,80", help="高点回撤%%，逗号分隔")
    ap.add_argument("--tps", default="3,5,8,10", help="止盈倍数，逗号分隔")
    ap.add_argument("--grs", default="1.02,1.05,1.08,1.1", help="加仓倍数，逗号分隔")
    ap.add_argument("--budget", type=float, default=0.0,
                    help="外部投入硬预算 USDT（0 = 不限，缺省）")
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
    bases = _parse_list(args.base, float)
    drops = _parse_list(args.drops, float)
    tps = _parse_list(args.tps, float)
    grs = _parse_list(args.grs, float)
    years = max((datetime.strptime(args.end, "%Y-%m-%d")
                 - datetime.strptime(args.start, "%Y-%m-%d")).days / 365.25, 1e-9)

    rows_by_coin = run_search(drops, tps, grs, bases, coins,
                              args.start, args.end, budget=args.budget)
    report = render_report(rows_by_coin, drops, tps, grs, bases,
                           args.start, args.end, years, budget=args.budget)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"[paramsearch] 报告 → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
