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


# 状态机终态 → 中文（报告展示用；未知值原样透传）
_STATUS_CN = {"open": "持仓中", "closed": "已了结", "never": "未建仓"}


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
    A("# 马丁格尔深回撤加仓回测报告")
    A("")
    A(f"- 参数: 每手 {sp.lot_size} 股, 首仓 {sp.first_lots} 手, 加仓手数步长 {sp.add_lots_step} 手/档, "
      f"首仓回撤线 {sp.first_drawdown * 100:g}%, 加仓价格步长 {sp.add_step * 100:g}%/档, "
      f"再武装模式 {sp.rearm_mode}({'低点反弹' if sp.rearm_mode == 'bounce' else '新高'}, "
      f"{'反弹幅度' if sp.rearm_mode == 'bounce' else '高点刷新幅度'} {sp.rearm_pct * 100:g}%), "
      f"结算 cash(现金: 原始价成交+分红单列), 佣金 {sp.commission * 1e4:g}bp/边, "
      f"印花税 {sp.stamp * 1e4:g}bp(卖)")
    A(f"- 数据: {db_path}")
    A(f"- 窗口: {d0[:10]} ~ {d1[:10]} ({n_dates} 交易日), 股票数: {n_u}")
    A("- 口径: 信号按后复权价(hfq)判定；成交按原始价(人民币)，现金分红单列——"
      "总盈亏 = 股价增长 + 分红余额；v7.0.36 起分红余额优先出资买股")
    A("- 买入口径（v8.2.x 对齐实操）: **盘中挂单**——当日最低价触及档位线即"
      "成交（对齐实盘提前按理论加仓价挂单）；成交价 = 挂单撮合价：渐进触线"
      "按档位价成交，跳空低开按开盘价成交；首仓时机门 = 触线时刻**实际成交"
      "价**处于近 N 年收盘 ≤ 分位上限（窗口止于前一交易日收盘，零前视）")
    A("- 偏差: 湖内无退市股（幸存者偏差）；--start 若晚于上市日则高点为窗口内高点")
    A("")
    A("## 触发")
    A(f"- 触发过建仓的股票: {n_t}/{n_u} ({n_t / n_u:.1%})" if n_u else "- 触发过建仓的股票: 0/0")
    if n_t:
        nb = np.array([int(r["n_buys"]) for r in triggered], dtype=float)
        A(f"- 买入次数分布: 平均={nb.mean():.1f}, 中位={np.median(nb):.0f}, 最多={nb.max():.0f}")
        nr = np.array([int(r["n_rounds"]) for r in triggered], dtype=float)
        A(f"- 轮次（一次再武装→下一次再武装=一轮）: 平均={nr.mean():.2f}, 最多={nr.max():.0f}")
    A("")
    A("## 收益（仅触发过建仓的股票；收益率 = 总盈亏 ÷ 外部净投入）")
    if len(tp):
        A(f"- 股票数: {len(tp)}")
        A(f"- 胜率（总盈亏>0）: {(tp > 0).mean():.1%}")
        A(f"- 收益率: 平均={tp.mean():.1%}, 中位={np.median(tp):.1%}")
        A(f"- 分位（pN=第 N 百分位，比它低的股票占 N%）: "
          + ", ".join(f"p{q}={np.quantile(tp, q / 100):.1%}" for q in (1, 5, 25, 75, 95, 99)))
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
        A(f"- 最差浮亏（持仓期间最低 浮亏/投入）: p1={np.quantile(mu, 0.01):.1%}（最深 1% 的股票低于此值）, "
          f"最深={mu.min():.1%}")
    A("")
    A("## 组合账本（每日累计，equity_curve.csv）")
    if curve_last is not None:
        last_stocks, last_cost, last_value, last_pnl, last_pct = curve_last
        A(f"- 期末: 未平仓 {int(last_stocks)} 只, 成本 {last_cost:,.0f}, 市值 {last_value:,.0f}, "
          f"浮盈 {last_pnl:,.0f} ({last_pct})")
    if curve_worst is not None:
        A(f"- 账本最差浮亏日: {curve_worst[0]} ({curve_worst[1]:.1%})")
    A("")
    A("## 基准对照（各股窗口内 后复权首末收益，等权平均，买入持有——同窗口不做策略、直接持有的参照）")
    if bench_rets:
        br = np.array(bench_rets)
        A(f"- 平均={br.mean():.1%}, 中位={np.median(br):.1%}, "
          f"p1={np.quantile(br, 0.01):.1%}（最差 1%）, p99={np.quantile(br, 0.99):.1%}（最好 1%）")
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
            A(f"- 轮次 {r['n_rounds']}, 买入 {r['n_buys']} 次, 状态 {_STATUS_CN.get(r['last_status'], r['last_status'])}, "
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
    elif n_t:
        A("## 前 10 / 后 10（按收益率排序，仅触发过建仓的股票）")
        if len(tp):
            for label, sub in (("前 10", _sort_per_stock(triggered)[:10]),
                               ("后 10", list(reversed(_sort_per_stock(triggered)))[:10])):
                A(f"### {label}")
                A("")
                A("| 代码 | 名称 | 买入次数 | 总投入 | 总盈亏 | 收益率 | 分红/上涨占比 | 状态 |")
                A("|---|---|---|---|---|---|---|---|")
                for r in sub:
                    r_pnl = float(r["total_pnl"])
                    if r_pnl > 0:
                        split = (f"{float(r.get('div_pool', 0.0)) / r_pnl:.0%} / "
                                 f"{float(r['price_pnl']) / r_pnl:.0%}")
                    else:
                        split = "—"
                    A(f"| {r['ts_code']} | {r['name']} | {r['n_buys']} | "
                      f"{float(r['total_invested']):,.0f} | {r_pnl:,.0f} | "
                      f"{float(r['pnl_pct']):.1%} | {split} | {_STATUS_CN.get(r['last_status'], r['last_status'])} |")
                A("")
    A("")
    A("## 术语表")
    A("- 首仓（k=0）：一轮里的第一次买入，须同时满足双条件——① 当日最低价触及首仓线（较本轮高点回撤 ≥ 首仓回撤线，盘中挂单触线即成交），且 ② 触线时刻**实际成交价**（挂单撮合价：渐进触线=线价、跳空低开=开盘价）处于近 N 年收盘分位 ≤ 时机分位上限（窗口止于前一交易日）；首仓实际成交价记为本轮锚点 P0")
    A("- 档位线（v8.1.0）：首仓线 = 本轮高点 H × (1 − 首仓回撤线)；后续第 k 档加仓线 = 首仓锚点 P0 × (1 − k × 加仓价格步长)，当日最低价触及加仓线即成交，每次加仓无分位限制，手数按 首仓手数 + k × 加仓手数步长 递增")
    A("- 再武装（rearm）：一轮买满后，价格从低点反弹 再武装幅度（或创出新高，视模式）→ "
      "状态机复位，刷新本轮高点，重新起新一轮的档位线")
    A("- 轮次：一次再武装到下一次再武装之间为一轮")
    A("- 收益率 = 总盈亏 ÷ 外部净投入；总盈亏 = 最终资产 − 总投入（最终资产含分红余额）")
    A("- 分红余额：收到但尚未再投入买股的现金分红（v7.0.36 起余额优先出资，未投出部分留在余额）")
    A("- 最差浮亏：持仓期间 浮亏÷投入 的最低点（不卖出的账面最大回撤）")
    A("- 分位 pN：第 N 百分位——有 N% 的股票比它差")
    A("- 后复权(hfq)：分红/送转折算进价格的复权方式，信号判定用它避免除权假信号")
    A(f"耗时 {time.time() - t0:.1f}s")
    return lines


def write_outputs(out_dir: str, sp, db_path: str, gdates: np.ndarray,
                  g_value: np.ndarray, g_cost: np.ndarray, g_open: np.ndarray,
                  rows: List[dict], buys_by_code: Dict[str, List],
                  bench_rets: List[float], t0: float,
                  pool_schedule: Optional[Dict[str, set]] = None,
                  mode: str = "static") -> None:
    """写 per_stock.csv / buys.csv / equity_curve.csv / summary.md / status.json。

    v8.0.13：pool_schedule 非空（动态池回测）时——per_stock.csv +
    pool_months/pool_first/pool_last 三列、另写 pool_timeline.csv（月度
    池规模+入池/出池名单）、status.json +mode/pool 键。静态回测产物不变。
    """
    import os

    os.makedirs(out_dir, exist_ok=True)
    rows = _sort_per_stock(rows)

    # per_stock.csv（v8.0.13：动态池行带池履历键 → 追加三列）
    has_pool = pool_schedule is not None and any("pool_months" in r for r in rows)
    pcols = list(PER_STOCK_COLUMNS)
    if has_pool:
        pcols += ["pool_months", "pool_first", "pool_last"]
    with open(os.path.join(out_dir, "per_stock.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(pcols)
        for r in rows:
            w.writerow([_fmt(r[c]) if c in r else None for c in pcols])

    # v8.0.13：pool_timeline.csv（月度重算的池规模 + 入池/出池名单）
    if pool_schedule:
        with open(os.path.join(out_dir, "pool_timeline.csv"), "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["as_of_date", "n_pool", "n_entered", "n_exited",
                        "entered", "exited"])
            prev: Optional[set] = None
            for d in sorted(pool_schedule):
                cur = pool_schedule[d]
                entered = sorted(cur - prev) if prev is not None else []
                exited = sorted(prev - cur) if prev is not None else []
                w.writerow([d, len(cur), len(entered), len(exited),
                            ",".join(entered), ",".join(exited)])
                prev = cur

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
        # v8.0.13：回测模式 + 动态池重算元信息
        "mode": mode,
        **({"pool": {"n_recomputes": len(pool_schedule),
                     "recompute_dates": sorted(pool_schedule),
                     "min_pool": int(min(len(s) for s in pool_schedule.values())),
                     "max_pool": int(max(len(s) for s in pool_schedule.values()))}}
           if pool_schedule else {}),
    }
    with open(os.path.join(out_dir, "status.json"), "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)
