# -*- coding: utf-8 -*-
"""martingale.report —— 回测产物写盘。

产物（out 目录）：
- per_stock.csv   与参考实现逐列/逐位对齐（f6 回归比对基准；pnl_pct 为比率）
- buys.csv        逐笔买入（ts_code,seq,date,price,amount,lots）—— v7 新增，
                  v7.0.28 加 lots（本笔手数）列
- equity_curve.csv 组合账本（与参考逐列对齐）
- summary.md      人读摘要（v7.0.35 起个股明细=年度汇总：外部投入/分红(盈余)/
                  盈亏；买入时间表不再重复——结果区 buys 表/buys.csv 已有）
- status.json     机器读运行元信息（Web 结果页 / 排障）
"""
from __future__ import annotations

import csv
import io
import json
import math
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import numpy as np

PER_STOCK_COLUMNS = [
    "ts_code", "n_bars", "n_rounds", "n_rearms", "n_buys", "total_invested",
    "realized_pnl", "final_assets", "dividends", "div_pool", "price_pnl", "total_pnl",
    "pnl_pct", "min_unreal_pct", "first_buy_date", "last_buy_date", "last_status",
    "end_date", "name",
]
# v8.0.10：div_pool（分红余额）从报告专用键进 CSV——Web per_stock 表
# 「分红占比 vs 上涨占比」拆解的数据来源（列追加，f6 按 fixture 键遍历不受影响）。

EQUITY_COLUMNS = ["date", "open_stocks", "open_cost", "open_value", "open_pnl", "open_pnl_pct"]


def _fmt(v) -> str:
    """与 pandas to_csv 一致的标量文本化：nan/None → 空串；float 用 repr
    （Python 3 最短 round-trip，与 numpy float64 的 str 一致）。"""
    if v is None:
        return ""
    if isinstance(v, float) or (hasattr(v, "item") and not isinstance(v, (int, str))):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return str(v)
        if math.isnan(f):
            return ""
        return repr(f)
    if isinstance(v, np.floating):
        f = float(v)
        return "" if math.isnan(f) else repr(f)
    return str(v)


def _sort_per_stock(rows: List[dict]) -> List[dict]:
    """pnl_pct 降序、nan 垫底（等价参考 pd.sort_values(ascending=False, na_position='last')）。"""
    ok = [r for r in rows if not (isinstance(r.get("pnl_pct"), float) and math.isnan(r["pnl_pct"]))]
    na = [r for r in rows if r not in ok]
    ok.sort(key=lambda r: float(r["pnl_pct"]), reverse=True)
    return ok + na


def _summary_lines(sp, db_path: str, gdates, n_dates: int, rows: List[dict],
                   buys_by_code: Dict[str, List], names: Dict[str, str],
                   bench_rets: List[float], curve_last, curve_worst, t0: float) -> List[str]:
    n_u = len(rows)
    triggered = [r for r in rows if r["last_status"] != "never"]
    n_t = len(triggered)
    tp = np.array([float(r["pnl_pct"]) for r in triggered if not math.isnan(r["pnl_pct"])])
    d0, d1 = str(gdates[0]) if n_dates else "", str(gdates[-1]) if n_dates else ""
    lines: List[str] = []
    A = lines.append
    A("# 马丁格尔深回撤加仓回测 summary")
    A("")
    A(f"- 参数: lot_size={sp.lot_size}, first_lots={sp.first_lots}, "
      f"add_lots_step={sp.add_lots_step}, first_drawdown_pct={sp.first_drawdown * 100:g}%, "
      f"add_step_pct={sp.add_step * 100:g}%, rearm_mode={sp.rearm_mode}, "
      f"rearm_pct={sp.rearm_pct * 100:g}%, accounting={sp.accounting}, "
      f"comm={sp.commission * 1e4:g}bps, stamp={sp.stamp * 1e4:g}bps")
    A(f"- 数据: {db_path}")
    A(f"- 窗口: {d0} ~ {d1} ({n_dates} 交易日), 股票数: {n_u}")
    A("- 口径: 信号=后复权; cash：原始价成交(人民币)+现金分红单列, 股价增长=总盈亏-分红")
    A("- 偏差: 湖内无退市股（幸存者偏差）；--start 若晚于上市日则高点为窗口内高点")
    A("")
    A("## 触发")
    A(f"- 触发过建仓的股票: {n_t}/{n_u} ({n_t / n_u:.1%})" if n_u else "- 触发过建仓的股票: 0/0")
    if n_t:
        nb = np.array([int(r["n_buys"]) for r in triggered], dtype=float)
        A(f"- 加仓次数分布: mean={nb.mean():.1f}, median={np.median(nb):.0f}, max={nb.max():.0f}")
        nr = np.array([int(r["n_rounds"]) for r in triggered], dtype=float)
        A(f"- 轮次: mean={nr.mean():.2f}, max={nr.max():.0f}")
    A("")
    A("## 收益（触发过的股票，pnl_pct = 总盈亏/外部净投入；v7.0.36 分红余额优先出资）")
    if len(tp):
        A(f"- 股票数: {len(tp)}")
        A(f"- 胜率(pnl>0): {(tp > 0).mean():.1%}")
        A(f"- mean={tp.mean():.1%}, median={np.median(tp):.1%}")
        for q in (1, 5, 25, 75, 95, 99):
            A(f"- p{q}={np.quantile(tp, q / 100):.1%}")
        A(f"- 总投入(外部净): {sum(float(r['total_invested']) for r in triggered):,.0f}")
        A(f"- 最终资产: {sum(float(r['final_assets']) for r in triggered):,.0f}")
        tv = sum(float(r["total_pnl"]) for r in triggered)
        ti = sum(float(r["total_invested"]) for r in triggered)
        A(f"- 总盈亏: {tv:,.0f} ({tv / ti:.1%})")
        td = sum(float(r.get("div_pool", 0.0)) for r in triggered)
        if tv > 0:
            A(f"- 其中分红余额: {td:,.0f} ({td / tv:.1%}) | 股价增长: {tv - td:,.0f} ({(tv - td) / tv:.1%})")
        else:
            A(f"- 其中分红余额: {td:,.0f}（总盈亏为负，拆分见个股）")
        mu = np.array([float(r["min_unreal_pct"]) for r in triggered])
        A(f"- 最差浮亏(min_unreal_pct): p1={np.quantile(mu, 0.01):.1%}, min={mu.min():.1%}")
    A("")
    A("## 组合账本（每日累计，equity_curve.csv）")
    if curve_last is not None:
        last_stocks, last_cost, last_value, last_pnl, last_pct = curve_last
        A(f"- 期末: 未平仓 {int(last_stocks)} 只, 成本 {last_cost:,.0f}, 市值 {last_value:,.0f}, "
          f"浮盈 {last_pnl:,.0f} ({last_pct})")
    if curve_worst is not None:
        A(f"- 账本最差浮亏日: {curve_worst[0]} ({curve_worst[1]:.1%})")
    A("")
    A("## 基准对照（各股窗口内 hfq 首末收益，等权平均，买持）")
    if bench_rets:
        br = np.array(bench_rets)
        A(f"- mean={br.mean():.1%}, median={np.median(br):.1%}, "
          f"p1={np.quantile(br, 0.01):.1%}, p99={np.quantile(br, 0.99):.1%}")
    A("")
    if n_t and n_u <= 20:
        A("## 个股明细")
        for r in _sort_per_stock(triggered):
            A("")
            A(f"### {r['name']} ({r['ts_code']}) — 总盈亏 {float(r['total_pnl']):+,.0f} "
              f"({float(r['pnl_pct']):+.1%})")
            pool = float(r.get("div_pool", 0.0))
            A(f"- 总投入(外部净): {float(r['total_invested']):,.0f} → 最终资产: "
              f"{float(r['final_assets']):,.0f} (持仓 {float(r['final_assets']) - pool:,.0f} "
              f"+ 分红余额 {pool:,.0f})")
            A(f"- 累计收到分红 {float(r['dividends']):,.0f}（v7.0.36：余额优先出资，"
              f"未投出部分留在余额）")
            if float(r["total_pnl"]) > 0:
                A(f"- 拆分: 股价增长 {float(r['price_pnl']):+,.0f} "
                  f"({float(r['price_pnl']) / float(r['total_pnl']):.1%})"
                  f" + 分红余额 {pool:+,.0f} "
                  f"({pool / float(r['total_pnl']):.1%})")
            A(f"- 轮次 {r['n_rounds']}, 买入 {r['n_buys']} 次, 状态 {r['last_status']}, "
              f"最差浮亏 {float(r['min_unreal_pct']):.1%}")
            # v7.0.35：年度汇总替代买入时间表（Joel 2026-11-10：回测结果区已有
            # 买入时间表 buys 表/buys.csv，明细不重复；列 = 投入(外部/分红)/
            # 分红盈余/股价盈亏/年底总资产）
            annual = r.get("annual") or []
            if annual:
                A("- 年度汇总（v7.0.36 分红优先出资：分红投入=当年买入中由分红余额"
                  "出资的部分；外部投入=剩余外部出资；分红盈余=分红余额净变动"
                  "（收到−投出，余额跨年结转）；股价盈亏=持仓市值变化（含新购股份）；"
                  "年底总资产=市值+分红余额。恒等式：年底总资产=年初总资产+股价盈亏"
                  "+分红盈余——投入（外部+分红）已转为市值含在股价盈亏内，不另加"
                  "（否则重复计算））:")
                A("")
                A("| 年份 | 外部投入 | 分红投入 | 分红盈余 | 股价盈亏 | 年底总资产 |")
                A("|---|---|---|---|---|---|")
                for a in annual:
                    A(f"| {a['year']} | {a['invested_ext']:,.0f} | "
                      f"{a['invested_div']:,.0f} | {a['div_surplus']:,.0f} | "
                      f"{a['price_pnl']:+,.0f} | {a['end_assets']:,.0f} |")
                A("")
    else:
        A("## Top / Bottom 10（按 pnl_pct，仅触发过的股票）")
        if len(tp):
            for label, sub in (("Top", _sort_per_stock(triggered)[:10]),
                               ("Bottom", list(reversed(_sort_per_stock(triggered)))[:10])):
                A(f"### {label}")
                A("")
                A("| code | name | buys | invested | pnl | pnl% | 分红/上涨 | status |")
                A("|---|---|---|---|---|---|---|---|")
                for r in sub:
                    tp = float(r["total_pnl"])
                    # v8.0.10：分红/上涨占比（与个股明细「拆分」行同口径：
                    # 分红余额 vs 价差盈亏；总盈亏≤0 不拆，显示 —）
                    if tp > 0:
                        split = (f"{float(r.get('div_pool', 0.0)) / tp:.0%} / "
                                 f"{float(r['price_pnl']) / tp:.0%}")
                    else:
                        split = "—"
                    A(f"| {r['ts_code']} | {r['name']} | {r['n_buys']} | "
                      f"{float(r['total_invested']):,.0f} | {float(r['total_pnl']):,.0f} | "
                      f"{float(r['pnl_pct']):.1%} | {split} | {r['last_status']} |")
                A("")
    A(f"耗时 {time.time() - t0:.1f}s")
    return lines


def write_outputs(out_dir: str, sp, db_path: str, gdates: np.ndarray,
                  g_value: np.ndarray, g_cost: np.ndarray, g_open: np.ndarray,
                  rows: List[dict], buys_by_code: Dict[str, List],
                  bench_rets: List[float], t0: float) -> None:
    """写 per_stock.csv / buys.csv / equity_curve.csv / summary.md / status.json。"""
    import os

    os.makedirs(out_dir, exist_ok=True)
    rows = _sort_per_stock(rows)

    # per_stock.csv
    with open(os.path.join(out_dir, "per_stock.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(PER_STOCK_COLUMNS)
        for r in rows:
            w.writerow([_fmt(r[c]) for c in PER_STOCK_COLUMNS])

    # buys.csv（逐笔；seq 从 1 起；v7.0.28 加 lots 列 = 本笔手数）
    with open(os.path.join(out_dir, "buys.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts_code", "seq", "date", "price", "amount", "lots"])
        for c in sorted(buys_by_code):
            for seq, (bdate, bprice, bamt, _bk, _bor, blots) in enumerate(buys_by_code[c], start=1):
                w.writerow([c, seq, bdate, repr(float(bprice)), repr(float(bamt)), int(blots)])

    # equity_curve.csv
    open_stocks = g_open
    open_cost = g_cost
    open_value = g_value
    open_pnl = g_value - g_cost
    open_pnl_pct = np.where(g_cost > 0, (g_value - g_cost) / np.where(g_cost > 0, g_cost, 1.0), np.nan)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(EQUITY_COLUMNS)
    for i in range(len(gdates)):
        w.writerow([
            str(np.datetime64(gdates[i], "D")),
            int(open_stocks[i]),
            _fmt(open_cost[i]),
            _fmt(open_value[i]),
            _fmt(open_pnl[i]),
            _fmt(open_pnl_pct[i]),
        ])
    with open(os.path.join(out_dir, "equity_curve.csv"), "w", encoding="utf-8") as f:
        f.write(buf.getvalue())

    # summary.md
    curve_last = None
    if len(gdates):
        last_pct = f"{open_pnl_pct[-1]:.1%}" if not math.isnan(open_pnl_pct[-1]) else "N/A"
        curve_last = (open_stocks[-1], open_cost[-1], open_value[-1], open_pnl[-1], last_pct)
        valid_idx = [i for i in range(len(gdates)) if not math.isnan(open_pnl_pct[i])]
        curve_worst = None
        if valid_idx:
            wi = min(valid_idx, key=lambda i: open_pnl_pct[i])
            curve_worst = (str(np.datetime64(gdates[wi], "D")), open_pnl_pct[wi])
        else:
            curve_worst = None
    else:
        curve_worst = None
    lines = _summary_lines(sp, db_path, gdates, len(gdates), rows,
                           buys_by_code, {r["ts_code"]: r["name"] for r in rows},
                           bench_rets, curve_last, curve_worst, t0)
    with open(os.path.join(out_dir, "summary.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    # status.json
    status = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window": {
            "start": str(np.datetime64(gdates[0], "D")) if len(gdates) else None,
            "end": str(np.datetime64(gdates[-1], "D")) if len(gdates) else None,
            "n_dates": int(len(gdates)),
        },
        "params": {
            "name": sp.name,
            "lot_size": sp.lot_size,
            "first_lots": sp.first_lots,
            "add_lots_step": sp.add_lots_step,
            "first_drawdown_pct": sp.first_drawdown * 100,
            "add_step_pct": sp.add_step * 100,
            "rearm_mode": sp.rearm_mode,
            "rearm_pct": sp.rearm_pct * 100,
            "accounting": sp.accounting,
            "commission_bps": sp.commission * 1e4,
            "stamp_bps": sp.stamp * 1e4,
        },
        "n_stocks": len(rows),
        "n_triggered": len([r for r in rows if r["last_status"] != "never"]),
        "n_buys_total": int(sum(int(r["n_buys"]) for r in rows)),
    }
    with open(os.path.join(out_dir, "status.json"), "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)
