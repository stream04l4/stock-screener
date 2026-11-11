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


def load_bars(lake_path: str, symbol: str) -> Tuple[np.ndarray, np.ndarray]:
    """单币 1h K 线：(dates[datetime64[D]], closes[float])，按 ts 升序。"""
    con = connect_readonly(lake_path)
    try:
        rows = con.execute(
            "SELECT ts, close FROM kline_1h "
            "WHERE exchange='binance' AND symbol=? AND close > 0 ORDER BY ts",
            [symbol],
        ).fetchall()
    finally:
        con.close()
    if not rows:
        return np.array([], dtype="datetime64[D]"), np.array([], dtype=float)
    ts = np.array([r[0] for r in rows], dtype="int64")      # SQL 已按 ts 升序
    closes = np.array([r[1] for r in rows], dtype=float)
    dates = (ts // 86400).astype("datetime64[D]")
    return dates, closes


def run_backtest(
    symbols: List[str],
    p: StrategyParams,
    lake_path: str = LAKE_PATH,
) -> List[Dict[str, Any]]:
    """逐币跑引擎 → 结果列表（含 equity 曲线，供报告/绘图）。"""
    out = []
    for sym in symbols:
        dates, closes = load_bars(lake_path, sym)
        out.append(run_coin(sym, dates, closes, p))
    return out


def _fmt_open(r: Dict[str, Any]) -> str:
    return (f"{r['entry_date']} 建仓 {r['entry']} → 现 {r['last_price']}，"
            f"已加仓 {r['k_level']} 级，投入 {r['spent_usdt']} USDT，"
            f"浮盈 {r['unrealized_usdt']} USDT（{r['unrealized_pct']}%），"
            f"止盈线 {r['tp_price']}（持有 {r['bars_holding']}h）")


def render_report(results: List[Dict[str, Any]], p: StrategyParams) -> str:
    """markdown 报告：参数 + 逐币轮次/未了结/净值回撤。"""
    import datetime as dt
    lines = [
        "# 加密货币马丁格尔回测报告",
        "",
        f"生成时间：{dt.datetime.now().strftime('%Y-%m-%d %H:%M')} UTC",
        "",
        "## 策略参数",
        "",
        f"- 建仓：价 ≤ 一年高点 × {p.trigger_frac():.0%}（跌 {p.first_drop_pct:.0f}% 触发），"
        f"首仓 {p.base_amount_usdt:g} USDT",
        f"- 加仓：固定网格 建仓价 × (1 − {p.add_grid_pct:g}% × k)，第 k 级 "
        f"{p.base_amount_usdt:g} × {p.growth:g}^k USDT，不设上限",
        f"- 止盈：价 ≥ 建仓价 × {p.take_profit_mult:g} → 全卖，entry 重置",
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
                "| 建仓日 | 建仓价 | 止盈日 | 止盈价 | 持有(h) | 最深层 | 投入 | 所得 | 盈亏 | 收益率 |",
                "|---|---|---|---|---|---|---|---|---|---|",
            ]
            for rd in r["rounds"]:
                lines.append(
                    f"| {rd['entry_date']} | {rd['entry']} | {rd['exit_date']} "
                    f"| {rd['exit_price']} | {rd['bars']} | {rd['max_level']} "
                    f"| {rd['spent_usdt']} | {rd['proceeds_usdt']} "
                    f"| {rd['pnl_usdt']} | {rd['pnl_pct']}% |")
            lines.append("")
        else:
            lines += ["### 已了结轮次", "", "无（10x 止盈从未触发）。", ""]
        o = r["open_round"]
        if o:
            lines += ["### 未了结轮次（持仓中）", "", _fmt_open(o), ""]
        else:
            lines += ["### 未了结轮次", "", "无（当前空仓）。", ""]
        lines += [
            f"- 已了结净盈亏：{r['realized_cash']} USDT",
            f"- 当前净值（持仓市值 + 已了结净盈亏）：{r['final_equity']} USDT",
            f"- 净值曲线最大回撤：{r['mdd_pct']}%",
            "",
        ]
    lines += [
        "---",
        "边界纪律：回测 = 策略验证工具，不接自动交易（设计文档 §7）。",
    ]
    return "\n".join(lines)


def save_report(results: List[Dict[str, Any]], p: StrategyParams,
                out_path: str = "") -> str:
    out_path = out_path or REPORT_PATH
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(render_report(results, p))
    return out_path
