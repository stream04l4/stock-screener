# -*- coding: utf-8 -*-
"""回测输出（CSV / Markdown，调研报告 R5 输出物规格）。

产物（output/backtest/）：
- ``equity_curve.csv``   日频净值 + 各基准净值（列 date, strategy_nav, <bench...>）。
- ``monthly_holdings.csv`` 每期 date, code, name, weight, entry_price, total_score。
- ``report.md``          指标表(全窗口+1y/3y/5y切片) + 逐年分解 + 换手统计 +
                         基准对比 + **本期激活维度** + 行业口径标注 + 局限说明。

所有数值来自 metrics_bt（纯函数）；本模块只负责排版与落盘，不重算指标。
"""
from __future__ import annotations

import csv
import os
from typing import Dict, List, Optional, Sequence

from .metrics_bt import (MetricsSummary, slice_window, summarize, yearly_breakdown)
from .result import BacktestResult


def _fmt(v: Optional[float], nd: int = 2, suffix: str = "") -> str:
    if v is None:
        return "—"
    return f"{v:.{nd}f}{suffix}"


# ---------------------------------------------------------------------------
# CSV 落盘
# ---------------------------------------------------------------------------
def write_equity_curve(path: str, result: BacktestResult) -> None:
    bench_cols = [b.name for b in result.benchmarks]
    # 基准按日期对齐到策略日期（缺失 → 空）
    bench_by_date: Dict[str, Dict[str, Optional[float]]] = {}
    for b in result.benchmarks:
        m = dict(zip(b.dates, b.navs))
        for d in result.dates:
            bench_by_date.setdefault(d, {})[b.name] = m.get(d)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "strategy_nav"] + bench_cols)
        for i, d in enumerate(result.dates):
            row = [d, f"{result.navs[i]:.6f}"]
            bm = bench_by_date.get(d, {})
            for c in bench_cols:
                v = bm.get(c)
                row.append("" if v is None else f"{v:.6f}")
            w.writerow(row)


def write_monthly_holdings(path: str, result: BacktestResult) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "code", "name", "weight", "entry_price", "total_score"])
        for p in result.periods:
            for h in p.holdings:
                w.writerow([
                    p.decision_date, h.code, h.name, f"{h.weight:.6f}",
                    "" if h.entry_price is None else f"{h.entry_price:.4f}",
                    "" if h.total_score is None else f"{h.total_score:.4f}",
                ])


# ---------------------------------------------------------------------------
# Markdown 报告
# ---------------------------------------------------------------------------
def _metrics_table(rows: List[tuple[str, MetricsSummary]]) -> str:
    lines = [
        "| 窗口 | 年化% | 波动% | 夏普 | 最大回撤% | Calmar | beta | alpha%(年) | IR | 月度胜率% | 平均换手%(单边) |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    seen: set[tuple[str, str]] = set()
    for label, s in rows:
        key = (s.start, s.end)   # 短窗口下切片退化为全窗口 → 去重
        if key in seen:
            continue
        seen.add(key)
        lines.append(
            f"| {label}（{s.start}~{s.end}）"
            f" | {_fmt(s.annual_return_pct)} | {_fmt(s.annual_vol_pct)} "
            f"| {_fmt(s.sharpe)} | {_fmt(s.max_drawdown_pct)} "
            f"| {_fmt(s.calmar)} | {_fmt(s.beta)} | {_fmt(s.alpha_annual_pct)} "
            f"| {_fmt(s.info_ratio)} | {_fmt(s.monthly_win_rate_pct, 1)} "
            f"| {_fmt(s.avg_turnover_one_way)} |"
        )
    return "\n".join(lines)


def build_report(result: BacktestResult) -> str:
    rf = result.risk_free_pct
    bench = result.benchmarks[0] if result.benchmarks else None
    b_dates = bench.dates if bench else None
    b_navs = [v for v in (bench.navs if bench else []) if v is not None] \
        if bench else None

    def _sum(dates: Sequence[str], navs: Sequence[float]) -> MetricsSummary:
        return summarize(dates, navs, rf,
                         bench_dates=b_dates, bench_navs=b_navs,
                         turnover_by_day=result.turnover_by_day)

    full = _sum(result.dates, result.navs)
    s1y = _sum(*slice_window(result.dates, result.navs, 250))
    s3y = _sum(*slice_window(result.dates, result.navs, 750))
    s5y = _sum(*slice_window(result.dates, result.navs, 1250))

    yearly = yearly_breakdown(result.dates, result.navs)

    # 基准全窗口指标（对比用）
    bench_lines: List[str] = []
    for b in result.benchmarks:
        bn = [v for v in b.navs if v is not None]
        bd = [d for d, v in zip(b.dates, b.navs) if v is not None]
        if len(bn) < 2:
            continue
        bs = _sum(bd, bn)
        bench_lines.append(
            f"- **{b.name}**（{b.code or '自建'}）：年化 {_fmt(bs.annual_return_pct)}%，"
            f"最大回撤 {_fmt(bs.max_drawdown_pct)}%，夏普 {_fmt(bs.sharpe)}")

    # 本期激活维度统计（各期去重 + 出现频次）
    dim_freq: Dict[str, int] = {}
    for p in result.periods:
        for d in p.active_dims:
            dim_freq[d] = dim_freq.get(d, 0) + 1
    n_periods = len(result.periods)

    out: List[str] = []
    out.append("# 多因子策略历史回测报告")
    out.append("")
    out.append(f"- **回测窗口**：{result.start} ~ {result.end}（{full.n_days} 个交易日）")
    out.append(f"- **调仓**：月度，Top N = {result.top_n}（等权），成交口径 = `{result.execution_mode}`")
    out.append(f"- **权重来源**：`{result.weights_ref}`（strategy.yaml scoring 段单一事实来源）")
    out.append(f"- **无风险利率**：{rf}%（夏普/alpha 用）")
    out.append("")

    out.append("## 一、绩效指标（全窗口 + 1y/3y/5y 切片）")
    out.append("")
    out.append(_metrics_table([("全窗口", full), ("近1年", s1y), ("近3年", s3y), ("近5年", s5y)]))
    out.append("")
    out.append(f"> 累计交易成本 ≈ {result.cost_bps_cum:.0f} bp；"
               f"调仓 {result.n_rebalances} 次；open 缺失 close 兜底 {result.open_fallback_total} 笔。")
    out.append("")

    out.append("## 二、逐年分解")
    out.append("")
    out.append("| 年份 | 收益% | 最大回撤% |")
    out.append("|---|---|---|")
    for y in yearly:
        out.append(f"| {y['year']} | {_fmt(y['total_return_pct'])} | {_fmt(y['max_drawdown_pct'])} |")
    out.append("")

    if bench_lines:
        out.append("## 三、基准对比（全窗口）")
        out.append("")
        out.extend(bench_lines)
        out.append("")

    out.append("## 四、换手与成交统计")
    out.append("")
    to_vals = list(result.turnover_by_day.values())
    if to_vals:
        out.append(f"- 单边换手率：均值 {sum(to_vals)/len(to_vals)*100:.2f}%，"
                   f"最大 {max(to_vals)*100:.2f}%，最小 {min(to_vals)*100:.2f}%")
    else:
        out.append("- 无调仓记录。")
    out.append(f"- 退市退出 {result.n_delisted_exits} 次（合计拖累 {_fmt(result.delisting_drag_pct)}%）；"
               f"停牌超时转现金 {result.n_drop_to_cash} 次。")
    out.append("")

    out.append("## 五、本期激活维度（PIT 自适应，TL 硬性要求 6）")
    out.append("")
    if dim_freq:
        for d, c in sorted(dim_freq.items(), key=lambda x: -x[1]):
            out.append(f"- {d}：{c}/{n_periods} 期激活")
        out.append("")
        out.append("> 某维度在某期无 PIT 数据 → 该维得分置中性(0)并在此标注，"
                   "**不得用未来/缺失数据冒充**。当前缓存下技术面全量可用、"
                   "分红仅 2025/26、基本面仅近期 → 初版自然退化为以技术面为主（诚实结果）。")
    else:
        out.append("- 无调仓期。")
    out.append("")

    out.append("## 六、口径与局限（显式标注）")
    out.append("")
    if result.industry_update_date:
        out.append(f"- **行业分类** = 证监会行业**当前快照**（updateDate={result.industry_update_date}），"
                   "回测期按同一口径近似（BaoStock 无历史行业源，TL Q4 拍板接受+标注）。")
    if result.universe_note:
        out.append(f"- **股票池**：{result.universe_note}")
    for note in result.data_notes:
        out.append(f"- {note}")
    out.append("")

    out.append("## 七、逐期持仓明细（Top N）")
    out.append("")
    for p in result.periods:
        names = ", ".join(h.code for h in p.holdings[:10])
        more = f" …等{len(p.holdings)}只" if len(p.holdings) > 10 else ""
        out.append(f"- **{p.decision_date}**（成交 {p.exec_day}）激活维度="
                   f"{','.join(p.active_dims) or '—'}，池 {p.n_universe}/硬剔后 {p.n_hard_pass}："
                   f" {names}{more}")
    out.append("")
    return "\n".join(out)


def write_report(path: str, result: BacktestResult) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(build_report(result))


def write_all(output_dir: str, result: BacktestResult) -> Dict[str, str]:
    """落盘三件套，返回 {产物名: 路径}。"""
    os.makedirs(output_dir, exist_ok=True)
    eq = os.path.join(output_dir, "equity_curve.csv")
    mh = os.path.join(output_dir, "monthly_holdings.csv")
    rp = os.path.join(output_dir, "report.md")
    write_equity_curve(eq, result)
    write_monthly_holdings(mh, result)
    write_report(rp, result)
    return {"equity_curve": eq, "monthly_holdings": mh, "report": rp}
