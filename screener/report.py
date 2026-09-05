# -*- coding: utf-8 -*-
"""输出层：result_YYYYMMDD.csv + report_YYYYMMDD.md。

CSV 列（brief §3.6）：代码、名称、行业、收盘价、股息率、ROE、行业百分位、各维度是否通过。
报告内容：每层过滤漏斗、最终入选列表（含关键指标）、数据时间戳与来源说明、缺失/异常名单。
"""
from __future__ import annotations

import csv
import os
from datetime import datetime, timezone
from typing import Any, Dict

import pandas as pd

# CSV 列顺序（brief 要求的字段 + 少量补充指标，便于复核）
CSV_COLUMNS = [
    "code",            # 代码
    "name",            # 名称
    "industry",        # 行业
    "close",           # 收盘价（不复权 af=3）
    "dividend_yield_pct",  # 股息率 %
    "roe_pct",         # ROE %（最近披露报告期，累计口径）
    "industry_percentile",  # 行业百分位（越小越好；空=未排名/跳过组）
    "pass_technical",  # 技术面是否通过
    "pass_dividend",   # 股息率维度是否通过
    "pass_industry",   # 行业排名维度是否通过
    "pass_fundamental",  # 基本面维度是否通过
    "pass_all",        # 最终入选
    # --- 补充指标（复核用） ---
    "window_return_pct",
    "annual_vol_pct",
    "cash_per_share",
    "yoy_net_profit_pct",
    "liability_pct",
    "gross_margin_pct",
    "industry_rank",
    "industry_group_size",
]


def _fmt(v: Any, nd: int = 2) -> str:
    """None/NaN → ''，否则格式化。"""
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except (TypeError, ValueError):
        pass
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def _bool_str(v: Any) -> str:
    try:
        if pd.isna(v):
            return ""
    except (TypeError, ValueError):
        pass
    return "是" if v else "否"


def write_csv(result, path: str) -> int:
    """写 result_YYYYMMDD.csv，返回行数（不含表头）。"""
    cands = result.candidates
    if cands.empty:
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(CSV_COLUMNS)
        return 0

    out = cands.copy()
    for col in CSV_COLUMNS:
        if col not in out.columns:
            out[col] = None
    out = out[CSV_COLUMNS]

    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_COLUMNS)
        for _, r in out.iterrows():
            row = []
            for col in CSV_COLUMNS:
                v = r[col]
                if col in ("pass_technical", "pass_dividend", "pass_industry",
                           "pass_fundamental", "pass_all"):
                    row.append(_bool_str(v))
                elif col in ("code", "name", "industry"):
                    row.append("" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v))
                elif col == "close":
                    row.append(_fmt(v, 3))
                elif col in ("dividend_yield_pct", "roe_pct", "yoy_net_profit_pct",
                             "liability_pct", "gross_margin_pct", "window_return_pct",
                             "annual_vol_pct"):
                    row.append(_fmt(v, 2))
                elif col == "cash_per_share":
                    row.append(_fmt(v, 4))
                else:
                    row.append("" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v))
            writer.writerow(row)
    return len(out)


def write_report(result, cfg: Dict[str, Any], path: str) -> None:
    """写 report_YYYYMMDD.md。"""
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    cands = result.candidates
    final = cands[cands["pass_all"]] if not cands.empty else cands

    lines: list[str] = []
    ap = lines.append

    ap(f"# A股选股报告 · {result.run_day}")
    ap("")
    ap(f"- 生成时间: {now_utc}")
    ap(f"- 请求日期: {result.requested_date}"
       + ("（非交易日，已回退到最近交易日）" if result.date_fallback else ""))
    ap(f"- 筛选运行日: **{result.run_day}**")
    ap(f"- 基本面报告期: {result.fundamental_period or '未知'}（ROE 为报告期累计口径，未年化；"
       f"净利同比字段={cfg['fundamental']['net_profit_yoy_field']}）")
    ap(f"- 总耗时: {result.elapsed_seconds:.0f}s；BaoStock 请求 {result.baostock_requests} 次；"
       f"本地缓存文件 {result.cache_stats.get('files', 0)} 个")
    ap("")

    # ---------- 漏斗 ----------
    ap("## 一、过滤漏斗")
    ap("")
    ap("| 层级 | 说明 | 剩余数量 |")
    ap("|---|---|---:|")
    us = result.universe_stats
    if us:
        ap(f"| L0 全部证券 | query_all_stock({result.run_day}) | {us.total_securities} |")
        ap(f"| L1 股票池 | A股前缀过滤 + 当日正常交易(tradeStatus=1) | {us.a_share_count} → **{us.trading_count}** |")
    ap(f"| (ST剔除) | 日K isST=1（名称含ST辅助标记 {us.st_name_count if us else 0} 只） | -{result.st_excluded_count} |")
    ap(f"| L2 技术面 | 收盘>MA{cfg['technical']['ma_period']}、近{cfg['technical']['return_window_days']}日收益∈"
       f"[{cfg['technical']['min_return_pct']}%,{cfg['technical']['max_return_pct']}%]、年化波动率<"
       f"{cfg['technical']['max_annual_volatility_pct']}%、上市满{cfg['universe']['listing_min_trading_days']}交易日 | "
       f"**{result.funnel.get('L2_技术面', 0)}** |")
    ap(f"| L3 股息率 | 窗口[{result.dividend_window_start}, {result.run_day}]已除权分红 ÷ 不复权收盘价 ≥ "
       f"{cfg['dividend']['min_yield_pct']}%（累计交集口径） | **{result.funnel.get('L3_股息率', 0)}** |")
    ap(f"| L4 行业排名 | 证监会行业内 ROE 前 {cfg['industry']['top_pct']:.0f}%（累计交集口径） | "
       f"**{result.funnel.get('L4_行业排名', 0)}** |")
    ap(f"| L5 最终入选 | 四维全过 | **{result.funnel.get('L5_最终入选', 0)}** |")
    ap("")
    ap("> 漏斗说明：L3/L4 为**累计交集**口径（在上一层幸存者中继续过滤），便于定位每只股票卡在哪层；"
       "各维度的独立通过率见 CSV 的 pass_* 列。")
    if result.insufficient_kline_count:
        ap(f"> 其中上市/数据不足被技术面剔除 {result.insufficient_kline_count} 只。")
    ap("")

    # ---------- 最终入选 ----------
    ap("## 二、最终入选列表")
    ap("")
    if final.empty:
        ap("**无股票通过全部四个维度。**")
    else:
        ap(f"共 **{len(final)}** 只（按股息率降序）：")
        ap("")
        ap("| 代码 | 名称 | 行业 | 收盘价 | 股息率% | ROE% | 行业百分位 | 近250日收益% | 年化波动% | 净利同比% | 负债率% | 毛利率% |")
        ap("|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for _, r in final.iterrows():
            pct = "" if (r["industry_percentile"] is None or pd.isna(r["industry_percentile"])) else f"{r['industry_percentile']:.1f}"
            ap(
                f"| {r['code']} | {r['name']} | {r['industry']} | {_fmt(r['close'],3)} "
                f"| {_fmt(r['dividend_yield_pct'])} | {_fmt(r['roe_pct'])} | {pct} "
                f"| {_fmt(r['window_return_pct'])} | {_fmt(r['annual_vol_pct'])} "
                f"| {_fmt(r['yoy_net_profit_pct'])} | {_fmt(r['liability_pct'])} | {_fmt(r['gross_margin_pct'])} |"
            )
    ap("")

    # ---------- 交叉验证 ----------
    if result.crosscheck:
        ap("## 三、腾讯实时接口交叉验证（收盘价）")
        ap("")
        ap("| 代码 | 名称 | BaoStock收盘(af=3) | 腾讯最新价 | 偏差% | 通过 |")
        ap("|---|---|---:|---:|---:|---|")
        for c in result.crosscheck:
            ok = "✓" if c["ok"] else "✗"
            ap(
                f"| {c['code']} | {c['name']} | {_fmt(c['bs_close'],3)} "
                f"| {_fmt(c['tencent_price'],3)} | {_fmt(c['diff_pct'],3)} | {ok} |"
            )
        ap("")
        ap("> 腾讯接口仅作交叉验证，不进主计算路径（股息率分母固定用 BaoStock 不复权收盘价）。")
        ap("")

    # ---------- 数据缺失/异常 ----------
    ap("## 四、数据缺失与异常名单")
    ap("")
    if result.missing_fundamental:
        ap(f"### 基本面数据缺失（{len(result.missing_fundamental)} 只，维度判不通过）")
        ap("")
        ap("| 代码 | 名称 | 缺失字段 |")
        ap("|---|---|---|")
        for m in result.missing_fundamental[:200]:
            ap(f"| {m['code']} | {m['name']} | {m['missing']} |")
        if len(result.missing_fundamental) > 200:
            ap(f"| … | 其余 {len(result.missing_fundamental) - 200} 只略 | |")
        ap("")
    else:
        ap("无基本面数据缺失。")
        ap("")

    if result.no_industry_codes:
        ap(f"### 无行业分类（{len(result.no_industry_codes)} 只，归入'无行业'组）")
        ap("")
        ap(", ".join(result.no_industry_codes[:100]) + (" …" if len(result.no_industry_codes) > 100 else ""))
        ap("")

    if result.small_groups_skipped:
        skipped = sorted(result.small_groups_skipped.items(), key=lambda kv: -kv[1])
        ap(f"### 行业组不足 {cfg['industry']['min_group_size']} 只、跳过排名约束（{len(skipped)} 个组）")
        ap("")
        for ind, n in skipped[:50]:
            ap(f"- {ind}: {n} 只")
        if len(skipped) > 50:
            ap(f"- …其余 {len(skipped) - 50} 个组略")
        ap("")

    # ---------- 数据说明 ----------
    ap("## 五、数据时间戳与来源说明")
    ap("")
    for note in result.data_notes:
        ap(f"- {note}")
    if result.fundamental_pub_dates:
        pub = sorted(set(result.fundamental_pub_dates.values()))
        ap(f"- 入选/候选股财报发布日(pubDate)范围: {min(pub)} ~ {max(pub)}")
    ap("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
