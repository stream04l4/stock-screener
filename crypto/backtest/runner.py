# -*- coding: utf-8 -*-
"""crypto.backtest —— 回测执行器：湖数据 → 引擎 → 报告（v7.0.47）。

数据：data/crypto/crypto.duckdb 湖内 1h K 线（read_only 连接，3×0.5s 重试，
与 models.stats 同锁语义——cron 同步持写锁期间短重试）。
产物：data/crypto/backtest_report.md（每次覆盖）+ stdout 摘要。
边界纪律（设计文档 §7）：回测 = 策略验证工具，不接自动交易。
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Tuple

import duckdb
import numpy as np

from crypto.strategy.engine import StrategyParams, run_coin

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LAKE_PATH = os.path.join(ROOT, "data", "crypto", "crypto.duckdb")
REPORT_PATH = os.path.join(ROOT, "data", "crypto", "backtest_report.md")


def connect_readonly(lake_path: str = LAKE_PATH):
    """read_only 连接 + 3×0.5s 写锁竞争重试（与 crypto.models.stats 同语义）。"""
    con = None
    for _ in range(3):
        try:
            con = duckdb.connect(lake_path, read_only=True)
            return con
        except Exception:  # noqa: BLE001 - 写锁竞争
            time.sleep(0.5)
    raise RuntimeError("数据湖写入占用中，稍后重试（cron 同步？）")


def load_bars(
    lake_path: str,
    symbol: str,
    lo_ts: int | None = None,
    hi_ts: int | None = None,
) -> Tuple[np.ndarray, np.ndarray]:
    """单币 1h K 线：(ts[int64 秒], closes[float])，按 ts 升序。

    :param lo_ts: 可选下界（含）；:param hi_ts: 可选上界（不含）。
    """
    con = connect_readonly(lake_path)
    try:
        sql = ("SELECT ts, close FROM kline_1h "
               "WHERE exchange='binance' AND symbol=? AND close > 0")
        args: list = [symbol]
        if lo_ts is not None:
            sql += " AND ts >= ?"
            args.append(lo_ts)
        if hi_ts is not None:
            sql += " AND ts < ?"
            args.append(hi_ts)
        sql += " ORDER BY ts"
        rows = con.execute(sql, args).fetchall()
    finally:
        con.close()
    if not rows:
        return np.array([], dtype="int64"), np.array([], dtype=float)
    ts = np.array([r[0] for r in rows], dtype="int64")      # SQL 已按 ts 升序
    closes = np.array([r[1] for r in rows], dtype=float)
    return ts, closes


def _date_to_ts(date_str: str, end_of_day: bool = False) -> int:
    """'YYYY-MM-DD' → UTC epoch 秒（end_of_day = 该日 24:00，即次日 00:00）。"""
    import datetime as dt
    d = dt.datetime.strptime(date_str, "%Y-%m-%d").replace(
        tzinfo=dt.timezone.utc)
    if end_of_day:
        d += dt.timedelta(days=1)
    return int(d.timestamp())


def run_backtest(
    symbols: List[str],
    p: StrategyParams,
    lake_path: str = LAKE_PATH,
    start: str | None = None,
    end: str | None = None,
    external_cap: float | None = None,
) -> List[Dict[str, Any]]:
    """逐币跑引擎 → 结果列表（含 equity 曲线，供报告/绘图）。

    时间范围（v7.0.54）：start/end = 'YYYY-MM-DD'（UTC 日界，end 含当日）。
    窗口内账户从空开始（了结余额/外部投入均为窗口内口径）；但一年高点
    用**窗口前 lookback_h 根 bar 的前置历史**参与计算（只用于高点，不产生
    交易）——保证窗口起点第一根 bar 的触发阈值与全史一致。

    预算（v7.0.56）：external_cap = 外部投入硬预算（USDT，累计口径）；
    买不起的档位整档放弃（不做部分买入），余额（了结所得）不受限。
    """
    from crypto.strategy.engine import rolling_high_1y
    start_ts = _date_to_ts(start) if start else None
    end_ts = _date_to_ts(end, end_of_day=True) if end else None
    out = []
    for sym in symbols:
        lo = (start_ts - p.lookback_h * 3600) if start_ts else None
        ts, closes = load_bars(lake_path, sym, lo_ts=lo, hi_ts=end_ts)
        if start_ts is not None:
            # 切出窗口内部分（前置历史只用于算高点）
            keep = np.where(ts >= start_ts)[0]
            if not len(keep):
                ts, closes, h1y = ts[:0], closes[:0], None  # 空窗口：引擎直接返回空结果
            else:
                h1y_full = rolling_high_1y(closes, p.lookback_h)
                ts, closes, h1y = ts[keep], closes[keep], h1y_full[keep]
        else:
            h1y = None
        dates = (ts // 86400).astype("datetime64[D]")
        out.append(run_coin(sym, dates, closes, p, h1y=h1y,
                            external_cap=external_cap))
    return out


def _fmt_open(r: Dict[str, Any]) -> str:
    cap_s = "，预算截断（该档买不起）" if r.get("capped") else ""
    tp_s = (f"止盈线 {r['tp_price']}" if r.get("tp_price") is not None
            else "无止盈线")
    trail_s = ""
    if r.get("trail_on"):
        trail_s = (f"，追踪止损已激活（峰值 {r['trail_peak']}，"
                   f"平仓线 {r['trail_stop']}）")
    return (f"{r['entry_date']} 建仓 {r['entry']} → 现 {r['last_price']}，"
            f"已加仓 {r['k_level']} 级，投入 {r['spent_usdt']} USDT{cap_s}，"
            f"浮盈 {r['unrealized_usdt']} USDT（{r['unrealized_pct']}%），"
            f"{tp_s}{trail_s}（持有 {r['bars_holding']}h）")


def render_report(
    results: List[Dict[str, Any]],
    p: StrategyParams,
    start: str | None = None,
    end: str | None = None,
) -> str:
    """markdown 报告：参数 + 范围 + 逐币轮次/未了结/净值回撤。"""
    import datetime as dt
    lines = [
        "# 加密货币马丁格尔回测报告",
        "",
        f"生成时间：{dt.datetime.now().strftime('%Y-%m-%d %H:%M')} UTC",
    ]
    if start or end:
        lines.append(
            f"回测范围：{start or '数据起点'} → {end or '最新'}（UTC 日界，end 含当日；"
            f"窗口内账户从空开始，一年高点用窗口前 {p.lookback_h}h 历史做基线）")
    lines += [
        "",
        "## 策略参数",
        "",
        f"- 建仓：价 ≤ 一年高点 × {p.trigger_frac():.0%}（跌 {p.first_drop_pct:.0f}% 触发），"
        f"首仓 {p.base_amount_usdt:g} USDT",
        f"- 加仓：固定网格 建仓价 × (1 − {p.add_grid_pct:g}% × k)，第 k 级 "
        f"{p.base_amount_usdt:g} × {p.growth:g}^k USDT，不设上限",
        (f"- 固定止盈：价 ≥ 建仓价 × {p.take_profit_mult:g} → 全卖"
         if p.take_profit_mult is not None else "- 固定止盈：关闭"),
    ]
    if p.trail_activation_mult is not None and p.trail_drawdown_pct is not None:
        lines.append(
            f"- 追踪止损：价 ≥ 建仓价 × {p.trail_activation_mult:g} 激活，"
            f"自激活后最高收盘回撤 {p.trail_drawdown_pct:.0%} 平仓"
            "（与固定止盈先到先了结）")
    lines += [
        f"- 持有：上涨不加仓，新高不刷新建仓价",
        f"- 手续费：{p.fee_bps:g} bps（买卖双向）；执行：1h 收盘价",
        f"- 一年高点：滚动 {p.lookback_h}h 收盘价最大（数据头不足一年用部分窗口）",
        "",
    ]
    for r in results:
        lines += [f"## {r['symbol']}", ""]
        if r["n_bars"] == 0:
            lines += ["无数据。", ""]
            continue
        lines.append(f"数据：{r['start']} ~ {r['end']}（{r['n_bars']} 根 1h）")
        lines.append("")
        if r["rounds"]:
            lines += [
                f"### 已了结轮次（{len(r['rounds'])}）",
                "",
                "| 建仓日 | 建仓价 | 了结日 | 了结价 | 了结方式 | 持有(h) | 最深层 | 投入 | 所得 | 盈亏 | 收益率 |",
                "|---|---|---|---|---|---|---|---|---|---|---|",
            ]
            for rd in r["rounds"]:
                cap_mark = "✂截断" if rd.get("capped") else ""
                reason = "止盈" if rd.get("reason") == "tp" else "追踪止损"
                peak_s = f"（峰 {rd['peak_price']}）" if rd.get("peak_price") else ""
                lines.append(
                    f"| {rd['entry_date']} | {rd['entry']} | {rd['exit_date']} "
                    f"| {rd['exit_price']} | {reason}{peak_s} | {rd['bars']} "
                    f"| {rd['max_level']}{cap_mark} "
                    f"| {rd['spent_usdt']} | {rd['proceeds_usdt']} "
                    f"| {rd['pnl_usdt']} | {rd['pnl_pct']}% |")
            lines.append("")
        else:
            lines += ["### 已了结轮次", "", "无（出场条件从未触发）。", ""]
        o = r["open_round"]
        if o:
            lines += ["### 未了结轮次（持仓中）", "", _fmt_open(o), ""]
        else:
            lines += ["### 未了结轮次", "", "无（当前空仓）。", ""]
        lines += [
            f"- 已了结净盈亏：{r['realized_cash']} USDT",
            f"- 总外部投入：{r['external_invested']} USDT（首次建仓全为外部；"
            f"了结后余额滚入下一轮，余额耗尽才追加）",
            f"- 当前净值（持仓市值 + 期末余额 {r['wallet']} USDT）：{r['final_equity']} USDT",
            f"- 净值曲线最大回撤：{r['mdd_pct']}%",
            "",
        ]
    lines += [
        "---",
        "边界纪律：回测 = 策略验证工具，不接自动交易（设计文档 §7）。",
    ]
    return "\n".join(lines)


def save_report(results: List[Dict[str, Any]], p: StrategyParams,
                out_path: str = "", start: str | None = None,
                end: str | None = None) -> str:
    out_path = out_path or REPORT_PATH
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(render_report(results, p, start=start, end=end))
    return out_path
