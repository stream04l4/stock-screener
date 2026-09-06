# -*- coding: utf-8 -*-
"""输出层单测：CSV 列契约 + 报告结构（v2 双模式，离线合成 ScreenResult）。

v2 契约（报告 R4）：
- CSV 统一列集 = v1 旧列（web 兼容）+ v2 新列（z_*/score_*/total_score/rank/
  top_n_selected/na_factors + 四维原始因子值）。
- 报告按 mode 分支：
  - zscore：一、KPI概览 / 二、综合得分榜单 / 三、四维得分分解 / 四、行业分布统计 /
    五、数据缺失与异常名单 / 六、数据时间戳与来源说明；必须标注"行业=证监会二级分类"
    "S2/S3/S5/S9 为比率代理口径"。
  - legacy：v1 章节（过滤漏斗/最终入选列表/...）。
"""
from __future__ import annotations

import csv

import pandas as pd
import pytest

from screener.report import CSV_COLUMNS, write_csv, write_report
from screener.screener import ScreenResult


# ---------------------------------------------------------------------------
# 合成结果构造
# ---------------------------------------------------------------------------

def _make_result_zscore() -> ScreenResult:
    """zscore 模式：全体候选（含未入选），Top N 由 top_n_selected 标记。"""
    r = ScreenResult(run_day="2026-09-04", requested_date="2026-09-04", mode="zscore",
                     top_n=50, annual_year=2025)
    r.candidates = pd.DataFrame([
        {"code": "sz.000651", "name": "格力电器", "industry": "C38电气机械", "close": 40.0,
         "ma_bullish": 1, "window_return_pct": 25.0, "annual_vol_pct": 28.0,
         "rsi14": 62.5, "macd_golden_cross": 1,
         "ttm_dividend_yield_pct": 4.5, "payout_ratio_pct": 43.2,
         "industry_roe_rank_pct": 10.0, "industry_yoy_pni_rank_pct": 20.0,
         "roe_pct": 22.1, "roe_3y_mean_pct": 21.0, "roe_3y_std_pct": 1.5,
         "liability_pct": 65.0, "gross_margin_pct": 30.0,
         "piotroski_fscore": 7, "piotroski_valid": 9,
         "z_technical": 0.8, "z_dividend": 1.2, "z_industry": 0.5, "z_fundamental": 0.9,
         "score_technical": 0.2, "score_dividend": 0.36, "score_industry": 0.075,
         "score_fundamental": 0.27, "total_score": 0.905, "rank": 1,
         "top_n_selected": True, "na_factors": "",
         "pass_technical": True, "pass_dividend": True, "pass_industry": True,
         "pass_fundamental": True, "pass_all": True},
        {"code": "sh.601398", "name": "工商银行", "industry": "J66货币金融服务", "close": 8.13,
         "ma_bullish": 0, "window_return_pct": 12.5, "annual_vol_pct": 15.2,
         "rsi14": 55.0, "macd_golden_cross": 0,
         "ttm_dividend_yield_pct": 3.82, "payout_ratio_pct": 43.2,
         "industry_roe_rank_pct": 30.0, "industry_yoy_pni_rank_pct": None,
         "roe_pct": 9.5, "roe_3y_mean_pct": 9.49, "roe_3y_std_pct": 0.44,
         "liability_pct": 92.37, "gross_margin_pct": None,
         "piotroski_fscore": 4, "piotroski_valid": 7,
         "z_technical": -0.4, "z_dividend": 0.6, "z_industry": -0.2, "z_fundamental": -0.3,
         "score_technical": -0.1, "score_dividend": 0.18, "score_industry": -0.03,
         "score_fundamental": -0.09, "total_score": -0.04, "rank": 2,
         "top_n_selected": False, "na_factors": "fundamental.gross_margin",
         "pass_technical": False, "pass_dividend": True, "pass_industry": False,
         "pass_fundamental": False, "pass_all": False},
    ])
    r.funnel = {"L1_股票池": 5207, "L2_硬剔除后": 4800, "L3_打分候选": 4800, "L4_TopN入选": 1}
    r.scored = []
    r.factor_means = {
        "technical": {"ma_bullish": 0.5, "window_return": 0.12},
        "dividend": {"ttm_yield": 0.035},
        "industry": {"roe_rank_pct": 50.0},
        "fundamental": {"roe_level": 0.11, "piotroski": 0.62},
    }
    r.missing_fundamental = [{"code": "sh.601398", "name": "工商银行",
                              "missing": "fundamental.gross_margin"}]
    r.small_groups_skipped = {"C39计算机通信": 3}
    r.no_industry_codes = ["sz.301688"]
    r.data_notes = [
        "主数据源: BaoStock，筛选运行日 2026-09-04",
        "行业=证监会二级分类（query_stock_industry，updateDate=2026-08-31，83组）",
        "Piotroski F-Score: S2/S3/S5/S9 为比率代理口径（BaoStock 只给比率不给绝对值）",
    ]
    r.industry_update_date = "2026-08-31"
    r.elapsed_seconds = 12.3
    r.baostock_requests = 100
    r.kline_requests = 0
    r.cache_stats = {"files": 5}
    return r


def _make_result_legacy() -> ScreenResult:
    """legacy 模式：v1 四维AND语义（原 test_report 的合成数据）。"""
    r = ScreenResult(run_day="2026-09-04", requested_date="2026-09-04", mode="legacy")
    r.candidates = pd.DataFrame([
        {"code": "sh.601398", "name": "工商银行", "industry": "J66货币金融服务",
         "close": 8.13, "ma": 7.9, "window_return_pct": 12.5, "annual_vol_pct": 15.2,
         "cash_per_share": 0.3103, "dividend_yield_pct": 3.82, "roe_pct": 4.05,
         "yoy_net_profit_pct": 3.32, "liability_pct": 92.37, "gross_margin_pct": None,
         "pass_technical": True, "pass_dividend": True,
         "industry_rank": 1, "industry_percentile": 5.0, "industry_group_size": 20,
         "pass_industry": True, "pass_fundamental": False, "pass_all": False},
        {"code": "sz.000651", "name": "格力电器", "industry": "C38电气机械",
         "close": 40.0, "ma": 38.0, "window_return_pct": 25.0, "annual_vol_pct": 28.0,
         "cash_per_share": 1.8, "dividend_yield_pct": 4.5, "roe_pct": 22.1,
         "yoy_net_profit_pct": 8.0, "liability_pct": 65.0, "gross_margin_pct": 30.0,
         "pass_technical": True, "pass_dividend": True,
         "industry_rank": 2, "industry_percentile": 10.0, "industry_group_size": 10,
         "pass_industry": True, "pass_fundamental": True, "pass_all": True},
    ])
    r.funnel = {"L1_股票池": 5207, "L2_硬剔除后": 4800, "L3_技术面": 800,
                "L4_股息率": 120, "L5_行业排名": 60, "L6_最终入选": 1}
    r.universe_stats = type("US", (), {"total_securities": 7376, "a_share_count": 5215,
                                       "trading_count": 5207, "suspended_count": 8,
                                       "st_name_count": 200})()
    r.missing_fundamental = [{"code": "sh.601398", "name": "工商银行", "missing": "gpMargin"}]
    r.small_groups_skipped = {"C39计算机通信": 3}
    r.no_industry_codes = ["sz.301688"]
    r.data_notes = ["主数据源: BaoStock，筛选运行日 2026-09-04",
                    "股息率窗口: [2025-09-05, 2026-09-04]"]
    r.industry_update_date = "2026-08-31"
    r.fundamental_period = "2026Q2"
    r.elapsed_seconds = 12.3
    r.baostock_requests = 100
    r.cache_stats = {"files": 5}
    return r


def _cfg() -> dict:
    """报告渲染所需的最小完整配置（含 v2 scoring/badges/hard_filter 段）。"""
    return {
        "technical": {"ma_period": 200, "return_window_days": 250, "min_return_pct": 0,
                      "max_return_pct": 100, "max_annual_volatility_pct": 45},
        "dividend": {"window_days": 365, "min_yield_pct": 3},
        "industry": {"top_pct": 30, "min_group_size": 5},
        "universe": {"listing_min_trading_days": 250},
        "fundamental": {"net_profit_yoy_field": "YOYPNI"},
        "scoring": {
            "mode": "zscore", "top_n": 50, "missing_policy": "neutral_renorm",
            "weights": {"technical": 0.25, "dividend": 0.30, "industry": 0.15,
                        "fundamental": 0.30},
        },
        "badges": {"industry_top_pct": 10, "fscore_min": 7},
        "hard_filter": {"st_enabled": True, "listing_min_trading_days": 250},
    }


# ---------------------------------------------------------------------------
# CSV 列契约（v2）
# ---------------------------------------------------------------------------

def test_csv_columns_contract(tmp_path):
    """CSV 必须包含 v1 兼容列 + v2 新列（R4 全量列结构）。"""
    # v1 兼容列（web 解析依赖，不得删）
    legacy_required = ["code", "name", "industry", "close", "dividend_yield_pct",
                       "roe_pct", "industry_percentile", "pass_technical",
                       "pass_dividend", "pass_industry", "pass_fundamental", "pass_all"]
    # v2 新列（R4）
    v2_required = [
        "ma_bullish", "window_return_pct", "annual_vol_pct", "rsi14", "macd_golden_cross",
        "ttm_dividend_yield_pct", "payout_ratio_pct",
        "industry_roe_rank_pct", "industry_yoy_pni_rank_pct",
        "roe_3y_mean_pct", "roe_3y_std_pct", "liability_pct", "gross_margin_pct",
        "piotroski_fscore", "piotroski_valid",
        "z_technical", "z_dividend", "z_industry", "z_fundamental",
        "score_technical", "score_dividend", "score_industry", "score_fundamental",
        "total_score", "rank", "top_n_selected", "na_factors",
    ]
    for col in legacy_required + v2_required:
        assert col in CSV_COLUMNS, f"CSV 缺少必需列 {col}"

    p = tmp_path / "result_20260904.csv"
    n = write_csv(_make_result_zscore(), str(p))
    assert n == 2
    with open(p, encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    header, data = rows[0], rows[1:]
    assert header == CSV_COLUMNS
    # top_n_selected 是整数列（0/1），不是布尔"是/否"
    idx_topn = header.index("top_n_selected")
    assert [r[idx_topn] for r in data] == ["1", "0"]
    # na_factors 逗号分隔缺失因子名
    idx_na = header.index("na_factors")
    assert data[1][idx_na] == "fundamental.gross_margin"
    # 缺失值 → 空串（不是 NaN）
    idx_gm = header.index("gross_margin_pct")
    assert data[1][idx_gm] == ""


def test_csv_legacy_mode_empty_v2_cols(tmp_path):
    """legacy 模式下 v2 专属列以空值输出（单一 CSV_COLUMNS，web 解析兼容）。"""
    p = tmp_path / "result_legacy.csv"
    n = write_csv(_make_result_legacy(), str(p))
    assert n == 2
    with open(p, encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    header, data = rows[0], rows[1:]
    idx_total = header.index("total_score")
    idx_rank = header.index("rank")
    assert all(r[idx_total] == "" for r in data)
    assert all(r[idx_rank] == "" for r in data)
    # v1 布尔列仍是"是/否"
    idx_pass_all = header.index("pass_all")
    assert [r[idx_pass_all] for r in data] == ["否", "是"]


# ---------------------------------------------------------------------------
# 报告结构（zscore）
# ---------------------------------------------------------------------------

def test_report_zscore_structure(tmp_path):
    p = tmp_path / "report_20260904.md"
    write_report(_make_result_zscore(), _cfg(), str(p))
    text = p.read_text(encoding="utf-8")
    assert "# A股选股报告 · 2026-09-04（v2 多因子打分）" in text
    # R4 新章节结构
    assert "## 一、KPI 概览" in text
    assert "## 二、综合得分榜单（Top 50）" in text
    assert "## 三、四维得分分解" in text
    assert "## 四、行业分布统计" in text
    assert "## 五、数据缺失与异常名单" in text
    assert "## 六、数据时间戳与来源说明" in text
    # KPI 表行
    assert "| 入选数（Top 50） | 1 |" in text
    assert "| 平均TTM股息率% |" in text
    assert "| 平均ROE% |" in text
    assert "| 行业集中度（Top1行业 / HHI） |" in text
    # 榜单：入选股 + 综合得分
    assert "格力电器" in text and "0.9050" in text
    # TL 拍板强制标注
    assert "行业=证监会二级分类" in text
    assert "S2/S3/S5/S9 为比率代理口径" in text
    # 缺失名单 + 跳过组
    assert "fundamental.gross_margin" in text
    assert "C39计算机通信" in text


def test_report_zscore_empty(tmp_path):
    """无候选时 zscore 报告不崩溃。"""
    r = _make_result_zscore()
    r.candidates = r.candidates.iloc[0:0]
    r.funnel["L4_TopN入选"] = 0
    p = tmp_path / "report_empty.md"
    write_report(r, _cfg(), str(p))
    text = p.read_text(encoding="utf-8")
    assert "**无候选股票。**" in text


# ---------------------------------------------------------------------------
# 报告结构（legacy，v1 章节回归）
# ---------------------------------------------------------------------------

def test_report_legacy_structure(tmp_path):
    p = tmp_path / "report_legacy.md"
    write_report(_make_result_legacy(), _cfg(), str(p))
    text = p.read_text(encoding="utf-8")
    assert "# A股选股报告 · 2026-09-04（legacy 四维AND）" in text
    assert "## 一、过滤漏斗" in text
    assert "## 二、最终入选列表" in text
    assert "## 四、数据缺失与异常名单" in text
    assert "## 五、数据时间戳与来源说明" in text
    # 漏斗数字出现
    assert "5207" in text and "800" in text
    # 入选股出现
    assert "格力电器" in text
    # 缺失名单出现
    assert "gpMargin" in text
    # 跳过组注明
    assert "C39计算机通信" in text


def test_report_legacy_empty_final(tmp_path):
    """无入选时 legacy 报告不崩溃。"""
    r = _make_result_legacy()
    r.candidates = r.candidates.copy()
    r.candidates["pass_all"] = False
    r.funnel["L6_最终入选"] = 0
    p = tmp_path / "report.md"
    write_report(r, _cfg(), str(p))
    assert "无股票通过全部四个维度" in p.read_text(encoding="utf-8")
