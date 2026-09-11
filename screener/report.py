# -*- coding: utf-8 -*-
"""输出层：result_YYYYMMDD.csv + report_YYYYMMDD.md（v2 双模式）。

CSV 列契约（R4）：
- v2(zscore)：code/name/industry/close + 四维原始因子值 + z_*/score_*/total_score/
  rank/top_n_selected/na_factors + legacy 兼容列 pass_*。
- legacy：v1 原有列 + v2 列以空值补齐（保持单一 CSV_COLUMNS，web 解析兼容）。

报告章节：
- v2(zscore)：一、KPI概览 / 二、综合得分榜单(Top N) / 三、四维得分分解 /
  四、行业分布统计 / 五、数据缺失与异常名单 / 六、数据时间戳与来源说明。
  必须标注"行业=证监会二级分类""S2/S3/S5/S9 为比率代理口径"（TL 拍板）。
- legacy：v1 章节结构（过滤漏斗/最终入选列表/交叉验证/缺失名单/数据说明）。
"""
from __future__ import annotations

import csv
import math
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pandas as pd

# CSV 列顺序（v2 全量；legacy 模式下 v2 专属列为空）
CSV_COLUMNS = [
    "code",            # 代码
    "name",            # 名称
    "industry",        # 行业（证监会二级分类）
    "close",           # 收盘价（不复权 af=3）
    # --- 四维原始因子值（badge/复核） ---
    "ma_bullish",                  # MA20>MA60 多头排列 (0/1)
    "window_return_pct",           # 近250日区间收益 %
    "annual_vol_pct",              # 年化波动率 %
    "rsi14",                       # RSI(14) Wilder
    "macd_golden_cross",           # MACD 金叉 (0/1)
    "ttm_dividend_yield_pct",      # TTM 滚动股息率 %
    "payout_ratio_pct",            # 股利支付率 %
    "industry_roe_rank_pct",       # 行业内 ROE 排名分位（越小越好；空=跳过组）
    "industry_yoy_pni_rank_pct",   # 行业内 YOYPNI 排名分位（越小越好；空=跳过组）
    "roe_pct",                     # ROE %（最近年报 Q4，累计口径）
    "roe_3y_mean_pct",             # ROE 近3年Q4均值 %
    "roe_3y_std_pct",              # ROE 近3年Q4标准差 %
    "liability_pct",               # 资产负债率 %
    "gross_margin_pct",            # 毛利率 %（金融业为空）
    "piotroski_fscore",            # Piotroski F-Score（有效信号和）
    "piotroski_valid",             # 有效信号数（分母；金融业=7）
    # --- 四维 Z-Score 与维度分 ---
    "z_technical", "z_dividend", "z_industry", "z_fundamental",
    "score_technical", "score_dividend", "score_industry", "score_fundamental",
    "total_score",                 # Σ weight × z_dim
    "rank",                        # 综合得分排名（1=最高）
    "top_n_selected",              # 是否 Top N 入选 (0/1)
    "na_factors",                  # 缺失因子名（逗号分隔）
    # --- legacy 兼容列（v1 四维AND语义；zscore 模式下按旧硬规则对照评估） ---
    "pass_technical", "pass_dividend", "pass_industry", "pass_fundamental",
    "pass_all",
    # --- v5（TL D1/D3'/D8；v4 模式/配置下为空，零回归） ---
    "soe_flag",                    # 央国企标记 soe / ''（空=非SOE或v4；Round-2 D3'：新浪双规则）
    "soe_basis",                   # SOE 判定依据（国有股本性质/关键词命中(x)；供人工复核，TL Round-2 验收③）
    "total_mv_yi",                 # 总市值（亿元，腾讯快照 idx45）
    "consecutive_div_years",       # 连续分红年数（TL D1；从 run_year-1 向前数）
    "fcf_coverage",                # FCF 分红覆盖倍数（TL D6 真值/代理）
    "div_stability_cv",            # 近5年 DPS 变异系数（越低越稳）
    "reinvest_ref_price_4pct",     # 再投资参考价 = 近N年平均DPS / 目标TTM股息率(4%)（TL D8；v5.1 V1-5 多期平滑）
    "dps_cagr_5y_pct",             # 近 N 年 DPS CAGR %（v5.1 V1-5 展示列；None→空）
    "ttm_yield_pctile",            # 当前 TTM 股息率历史分位（0-100，TL D8）
    "yield_spread_pct",            # TTM 股息率 − 10Y国债（百分点，TL D4）
    # --- v1 补充指标（复核用，legacy 模式填充） ---
    "ma", "cash_per_share", "dividend_yield_pct", "yoy_net_profit_pct",
    "industry_rank", "industry_percentile", "industry_group_size",
]

_BOOL_COLS = {"pass_technical", "pass_dividend", "pass_industry",
              "pass_fundamental", "pass_all"}
_INT_COLS = {"ma_bullish", "macd_golden_cross", "piotroski_fscore", "piotroski_valid",
             "rank", "top_n_selected", "industry_rank", "industry_group_size",
             "consecutive_div_years"}
_F3_COLS = {"close", "rsi14", "ttm_dividend_yield_pct", "cash_per_share",
            "dividend_yield_pct", "reinvest_ref_price_4pct", "yield_spread_pct"}
_F2_COLS = {"window_return_pct", "annual_vol_pct", "payout_ratio_pct",
            "industry_roe_rank_pct", "industry_yoy_pni_rank_pct", "roe_pct",
            "roe_3y_mean_pct", "roe_3y_std_pct", "liability_pct", "gross_margin_pct",
            "yoy_net_profit_pct", "industry_percentile", "total_mv_yi", "ttm_yield_pctile",
            "dps_cagr_5y_pct"}
_F4_COLS = {"z_technical", "z_dividend", "z_industry", "z_fundamental",
            "score_technical", "score_dividend", "score_industry",
            "score_fundamental", "total_score", "ma", "fcf_coverage", "div_stability_cv"}


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
        if v is None or pd.isna(v):
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
                if col in _BOOL_COLS:
                    row.append(_bool_str(v))
                elif col in _INT_COLS:
                    if v is None or (isinstance(v, float) and math.isnan(v)):
                        row.append("")
                    else:
                        try:
                            row.append(str(int(float(v))))
                        except (TypeError, ValueError):
                            row.append("")
                elif col in ("code", "name", "industry", "na_factors"):
                    row.append("" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v))
                elif col in _F3_COLS:
                    row.append(_fmt(v, 3))
                elif col in _F2_COLS:
                    row.append(_fmt(v, 2))
                elif col in _F4_COLS:
                    row.append(_fmt(v, 4))
                else:
                    row.append("" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v))
            writer.writerow(row)
    return len(out)


# ---------------------------------------------------------------------------
# 报告渲染
# ---------------------------------------------------------------------------

def _hhi(counts: Dict[str, int]) -> float:
    """行业集中度 HHI = Σ(占比²)，counts={行业:数量}。"""
    total = sum(counts.values())
    if total == 0:
        return 0.0
    return sum((n / total) ** 2 for n in counts.values())


def _industry_counts(cands: pd.DataFrame, selected_mask=None) -> Dict[str, int]:
    df = cands[selected_mask] if selected_mask is not None else cands
    out: Dict[str, int] = {}
    for v in df["industry"].fillna("").astype(str):
        k = v.strip() or "无行业"
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def _kpi_table(result) -> List[str]:
    """KPI 概览表（parse_report 按 | 指标 | 值 | 解析）。"""
    cands = result.candidates
    if result.mode == "zscore":
        sel = cands[cands["top_n_selected"].astype(str).isin(["1", "True"])] \
            if not cands.empty else cands
        n_sel = int(result.funnel.get("L4_TopN入选", 0))
    else:
        sel = cands[cands["pass_all"]] if not cands.empty else cands
        n_sel = int(result.funnel.get("L6_最终入选", result.funnel.get("L5_最终入选", 0)))

    def _avg(col: str) -> Optional[float]:
        if sel.empty or col not in sel.columns:
            return None
        vals = pd.to_numeric(sel[col], errors="coerce").dropna()
        return float(vals.mean()) if len(vals) else None

    avg_yield = _avg("ttm_dividend_yield_pct") if result.mode == "zscore" else _avg("dividend_yield_pct")
    avg_roe = _avg("roe_pct")
    counts = _industry_counts(sel) if not sel.empty else {}
    top1 = (f"{next(iter(counts))} {counts[next(iter(counts))]}/{len(sel)}"
            f"（{counts[next(iter(counts))] / len(sel) * 100:.1f}%）") if counts and len(sel) else "—"
    hhi = _hhi(counts)

    lines = [
        "| 指标 | 值 |",
        "|---|---:|",
        f"| 入选数（Top {result.top_n}） | {n_sel} |",
        f"| 平均TTM股息率% | {_fmt(avg_yield, 3) if avg_yield is not None else '—'} |",
        f"| 平均ROE% | {_fmt(avg_roe, 2) if avg_roe is not None else '—'} |",
        f"| 行业集中度（Top1行业 / HHI） | {top1} / {_fmt(hhi * 100, 1)} |",
    ]
    return lines


def _write_report_zscore(result, cfg: Dict[str, Any], path: str) -> None:
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    cands = result.candidates
    sc = cfg["scoring"]
    w = sc["weights"]

    lines: List[str] = []
    ap = lines.append

    ap(f"# A股选股报告 · {result.run_day}（v2 多因子打分）")
    ap("")
    ap(f"- 生成时间: {now_utc}")
    ap(f"- 请求日期: {result.requested_date}"
       + ("（非交易日，已回退到最近交易日）" if result.date_fallback else ""))
    ap(f"- 筛选运行日: **{result.run_day}**")
    ap(f"- 打分模式: **zscore**（截面Z-Score多因子；权重 technical={w['technical']} / "
       f"dividend={w['dividend']} / industry={w['industry']} / fundamental={w['fundamental']}）")
    ap(f"- 基本面基准年度: {result.annual_year}Q4（年报）")
    ap(f"- 总耗时: {result.elapsed_seconds:.0f}s；BaoStock 请求 {result.baostock_requests} 次"
       f"（其中K线 {result.kline_requests} 次，稳定键缓存命中则≈0）；"
       f"本地缓存文件 {result.cache_stats.get('files', 0)} 个")
    ap("")

    # ---------- 一、KPI 概览 ----------
    ap("## 一、KPI 概览")
    ap("")
    lines.extend(_kpi_table(result))
    ap("")

    # ---------- 二、综合得分榜单（Top N） ----------
    ap(f"## 二、综合得分榜单（Top {result.top_n}）")
    ap("")
    if cands.empty:
        ap("**无候选股票。**")
    else:
        sel = cands[cands["top_n_selected"].astype(str).isin(["1", "True"])]
        v5_cols = ("soe_flag" in cands.columns) and (cands["soe_flag"].notna().any())
        ap(f"共 **{len(sel)}** 只（按 total_score 降序）：")
        ap("")
        if v5_cols:
            # v5（TL D1/D3'/D8）：榜单追加 SOE/判定依据/市值/连续分红/再投资参考列
            ap("| 排名 | 代码 | 名称 | 行业 | 收盘 | 技术分 | 股息分 | 行业分 | 基本面分 | "
               "综合得分 | TTM股息率% | ROE% | F-Score | SOE | SOE判定依据 | 市值(亿) | 连续分红年 | 再投资参考价 | DPS CAGR5y% | TTM分位% |")
            ap("|---:|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|---|---:|---:|---:|---:|---:|")
            for _, r in sel.iterrows():
                fs = f"{_fmt(r['piotroski_fscore'], 0)}/{_fmt(r['piotroski_valid'], 0)}" \
                    if pd.notna(r.get("piotroski_fscore")) else "—"
                ap(
                    f"| {r['rank']} | {r['code']} | {r['name']} | {r['industry']} "
                    f"| {_fmt(r['close'], 3)} | {_fmt(r['score_technical'])} | {_fmt(r['score_dividend'])} "
                    f"| {_fmt(r['score_industry'])} | {_fmt(r['score_fundamental'])} "
                    f"| **{_fmt(r['total_score'], 4)}** | {_fmt(r['ttm_dividend_yield_pct'], 3)} "
                    f"| {_fmt(r['roe_pct'])} | {fs} | {_fmt(r.get('soe_flag'))} | {_fmt(r.get('soe_basis'))} "
                    f"| {_fmt(r.get('total_mv_yi'))} "
                    f"| {_fmt(r.get('consecutive_div_years'), 0)} | {_fmt(r.get('reinvest_ref_price_4pct'), 2)} "
                    f"| {_fmt(r.get('dps_cagr_5y_pct'))} | {_fmt(r.get('ttm_yield_pctile'))} |"
                )
        else:
            ap("| 排名 | 代码 | 名称 | 行业 | 收盘 | 技术分 | 股息分 | 行业分 | 基本面分 | "
               "综合得分 | TTM股息率% | ROE% | F-Score |")
            ap("|---:|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
            for _, r in sel.iterrows():
                fs = f"{_fmt(r['piotroski_fscore'], 0)}/{_fmt(r['piotroski_valid'], 0)}" \
                    if pd.notna(r.get("piotroski_fscore")) else "—"
                ap(
                    f"| {r['rank']} | {r['code']} | {r['name']} | {r['industry']} "
                    f"| {_fmt(r['close'], 3)} | {_fmt(r['score_technical'])} | {_fmt(r['score_dividend'])} "
                    f"| {_fmt(r['score_industry'])} | {_fmt(r['score_fundamental'])} "
                    f"| **{_fmt(r['total_score'], 4)}** | {_fmt(r['ttm_dividend_yield_pct'], 3)} "
                    f"| {_fmt(r['roe_pct'])} | {fs} |"
                )
    ap("")

    # ---------- 二-bis. v5 SOE 剔除复核清单（Round-2 TL D3'：双规则皆无 → 剔除+单列）----------
    soe_review = getattr(result, "soe_review_list", None) or []
    if soe_review:
        ap(f"## 二-bis、SOE 剔除复核清单（新浪F10双规则皆未命中，{len(soe_review)} 只，已剔除，供人工复核）")
        ap("")
        ap("> 规则（Round-2 TL D3'）：任一股东 股本性质=='国有股' → soe；或名称命中关键词"
           "（国务院/国资委/汇金/财政部/国资）→ soe；**两者皆无** → 剔除并单列于此。")
        ap("")
        ap("| 代码 | 名称 |")
        ap("|---|---|")
        for m in soe_review[:200]:
            ap(f"| {m['code']} | {m.get('name', '')} |")
        if len(soe_review) > 200:
            ap(f"| … | 其余 {len(soe_review) - 200} 只略 |")
        ap("")

    # ---------- 三、四维得分分解 ----------
    ap("## 三、四维得分分解")
    ap("")
    ap("| 维度 | 权重 | 子因子（截面均值） |")
    ap("|---|---:|---|")
    dim_titles = {"technical": "技术面", "dividend": "股息", "industry": "行业",
                  "fundamental": "基本面"}
    for dim in ("technical", "dividend", "industry", "fundamental"):
        means = result.factor_means.get(dim, {})
        parts = []
        for f, m in means.items():
            parts.append(f"{f}={_fmt(m, 3) if m is not None else '—'}")
        ap(f"| {dim_titles[dim]} | {w[dim]} | {'、'.join(parts) or '（无有效因子）'} |")
    ap("")
    ap("> z=(x-mean)/std（样本标准差 n-1，std=0→z=0）；缺失因子 z=0 且按可用权重归一化"
       f"（missing_policy={sc['missing_policy']}）；total_score=Σ weight×z_dim。")
    ap("")

    # ---------- 四、行业分布统计 ----------
    ap("## 四、行业分布统计")
    ap("")
    if cands.empty:
        ap("**无候选股票。**")
    else:
        sel = cands[cands["top_n_selected"].astype(str).isin(["1", "True"])]
        counts = _industry_counts(sel) if not sel.empty else {}
        total = sum(counts.values()) or 1
        ap(f"入选股在 **{len(counts)}** 个证监会二级行业中的分布（行业=证监会二级分类，83组）：")
        ap("")
        ap("| 行业 | 数量 | 占比% |")
        ap("|---|---:|---:|")
        for ind, n in list(counts.items())[:50]:
            ap(f"| {ind} | {n} | {_fmt(n / total * 100, 1)} |")
        if len(counts) > 50:
            ap(f"| …其余 {len(counts) - 50} 个行业略 | | |")
    ap("")

    # ---------- 交叉验证（可选） ----------
    if result.crosscheck:
        ap("## 腾讯实时接口交叉验证（收盘价）")
        ap("")
        ap("| 代码 | 名称 | BaoStock收盘(af=3) | 腾讯最新价 | 偏差% | 通过 |")
        ap("|---|---|---:|---:|---:|---|")
        for c in result.crosscheck:
            ok = "✓" if c["ok"] else "✗"
            ap(
                f"| {c['code']} | {c['name']} | {_fmt(c['bs_close'], 3)} "
                f"| {_fmt(c['tencent_price'], 3)} | {_fmt(c['diff_pct'], 3)} | {ok} |"
            )
        ap("")

    # ---------- v5 ttm_yield 交叉验证（TL D8/验收④：vs 腾讯 idx64）----------
    if getattr(result, "ttm_crosscheck", None):
        tol = float((cfg.get("crosscheck") or {}).get("ttm_tolerance_pct", 0.1))
        n_ok = sum(1 for c in result.ttm_crosscheck if c["ok"])
        ap(f"## v5 TTM股息率交叉验证（自算 vs 腾讯idx64，抽样 {len(result.ttm_crosscheck)} 只，容忍 ≤{tol}pct）")
        ap("")
        ap("> 口径：我方=窗口内已除权分红和÷运行日af3收盘（PIT）；腾讯idx64=实时价口径。差异含当日价格波动。")
        ap("")
        ap(f"**{n_ok}/{len(result.ttm_crosscheck)} 只偏差 ≤{tol}pct。**")
        ap("")
        ap("| 代码 | 名称 | 自算TTM% | 腾讯idx64% | 偏差pct | 通过 |")
        ap("|---|---|---:|---:|---:|---|")
        for c in result.ttm_crosscheck:
            ok = "✓" if c["ok"] else "✗"
            ap(
                f"| {c['code']} | {c['name']} | {_fmt(c['ours_pct'], 3)} "
                f"| {_fmt(c['tencent_pct'], 3)} | {_fmt(c['diff_pct'], 3)} | {ok} |"
            )
        ap("")

    # ---------- 五、数据缺失与异常名单 ----------
    ap("## 五、数据缺失与异常名单")
    ap("")
    if result.missing_fundamental:
        ap(f"### 因子缺失/N-A（{len(result.missing_fundamental)} 只，缺失因子 z=0 且降权）")
        ap("")
        ap("| 代码 | 名称 | 缺失字段 |")
        ap("|---|---|---|")
        for m in result.missing_fundamental[:200]:
            ap(f"| {m['code']} | {m['name']} | {m['missing']} |")
        if len(result.missing_fundamental) > 200:
            ap(f"| … | 其余 {len(result.missing_fundamental) - 200} 只略 | |")
        ap("")
    else:
        ap("无因子缺失。")
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

    # ---------- 六、数据时间戳与来源说明 ----------
    ap("## 六、数据时间戳与来源说明")
    ap("")
    for note in result.data_notes:
        ap(f"- {note}")
    if result.fundamental_pub_dates:
        pub = sorted(set(result.fundamental_pub_dates.values()))
        ap(f"- 候选股财报发布日(pubDate)范围: {min(pub)} ~ {max(pub)}")
    ap("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def _write_report_legacy(result, cfg: Dict[str, Any], path: str) -> None:
    """legacy 模式：v1 章节结构（过滤漏斗/最终入选列表/...）。"""
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    cands = result.candidates
    final = cands[cands["pass_all"]] if not cands.empty else cands

    lines: List[str] = []
    ap = lines.append

    ap(f"# A股选股报告 · {result.run_day}（legacy 四维AND）")
    ap("")
    ap(f"- 生成时间: {now_utc}")
    ap(f"- 请求日期: {result.requested_date}"
       + ("（非交易日，已回退到最近交易日）" if result.date_fallback else ""))
    ap(f"- 筛选运行日: **{result.run_day}**")
    ap(f"- 基本面报告期: {result.fundamental_period or '未知'}（ROE 为报告期累计口径，未年化；"
       f"净利同比字段={cfg['fundamental']['net_profit_yoy_field']}）")
    ap(f"- 总耗时: {result.elapsed_seconds:.0f}s；BaoStock 请求 {result.baostock_requests} 次"
       f"（其中K线 {result.kline_requests} 次）；本地缓存文件 {result.cache_stats.get('files', 0)} 个")
    ap("")

    # ---------- 一、过滤漏斗 ----------
    ap("## 一、过滤漏斗")
    ap("")
    ap("| 层级 | 说明 | 剩余数量 |")
    ap("|---|---|---:|")
    us = result.universe_stats
    if us:
        ap(f"| L0 全部证券 | query_all_stock({result.run_day}) | {us.total_securities} |")
        ap(f"| L1 股票池 | A股前缀过滤 + 当日正常交易(tradeStatus=1) | {us.a_share_count} → **{us.trading_count}** |")
    ap(f"| (ST剔除) | 日K isST=1（名称含ST辅助标记 {us.st_name_count if us else 0} 只） | -{result.st_excluded_count} |")
    ap(f"| L2 硬剔除后 | ST + 上市满{cfg['hard_filter']['listing_min_trading_days']}交易日 | "
       f"**{result.funnel.get('L2_硬剔除后', 0)}** |")
    ap(f"| L3 技术面 | 收盘>MA{cfg['technical']['ma_period']}、近{cfg['technical']['return_window_days']}日收益∈"
       f"[{cfg['technical']['min_return_pct']}%,{cfg['technical']['max_return_pct']}%]、年化波动率<"
       f"{cfg['technical']['max_annual_volatility_pct']}% | "
       f"**{result.funnel.get('L3_技术面', 0)}** |")
    ap(f"| L4 股息率 | 窗口[{result.dividend_window_start}, {result.run_day}]已除权分红 ÷ 不复权收盘价 ≥ "
       f"{cfg['dividend']['min_yield_pct']}%（累计交集口径） | **{result.funnel.get('L4_股息率', 0)}** |")
    ap(f"| L5 行业排名 | 证监会行业内 ROE 前 {cfg['industry']['top_pct']:.0f}%（累计交集口径） | "
       f"**{result.funnel.get('L5_行业排名', 0)}** |")
    ap(f"| L6 最终入选 | 四维全过 | **{result.funnel.get('L6_最终入选', 0)}** |")
    ap("")
    if result.insufficient_kline_count:
        ap(f"> 其中上市/数据不足被硬剔除 {result.insufficient_kline_count} 只。")
        ap("")

    # ---------- 二、最终入选列表 ----------
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
                f"| {r['code']} | {r['name']} | {r['industry']} | {_fmt(r['close'], 3)} "
                f"| {_fmt(r['dividend_yield_pct'])} | {_fmt(r['roe_pct'])} | {pct} "
                f"| {_fmt(r['window_return_pct'])} | {_fmt(r['annual_vol_pct'])} "
                f"| {_fmt(r['yoy_net_profit_pct'])} | {_fmt(r['liability_pct'])} | {_fmt(r['gross_margin_pct'])} |"
            )
    ap("")

    # ---------- 三、腾讯交叉验证（可选） ----------
    if result.crosscheck:
        ap("## 三、腾讯实时接口交叉验证（收盘价）")
        ap("")
        ap("| 代码 | 名称 | BaoStock收盘(af=3) | 腾讯最新价 | 偏差% | 通过 |")
        ap("|---|---|---:|---:|---:|---|")
        for c in result.crosscheck:
            ok = "✓" if c["ok"] else "✗"
            ap(
                f"| {c['code']} | {c['name']} | {_fmt(c['bs_close'], 3)} "
                f"| {_fmt(c['tencent_price'], 3)} | {_fmt(c['diff_pct'], 3)} | {ok} |"
            )
        ap("")

    # ---------- 四、数据缺失与异常名单 ----------
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

    # ---------- 五、数据时间戳与来源说明 ----------
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


def write_report(result, cfg: Dict[str, Any], path: str) -> None:
    """写 report_YYYYMMDD.md（按 result.mode 分支）。"""
    if getattr(result, "mode", "zscore") == "legacy":
        _write_report_legacy(result, cfg, path)
    else:
        _write_report_zscore(result, cfg, path)
