# -*- coding: utf-8 -*-
"""输出层单测：CSV 列契约 + 报告结构（离线，合成 ScreenResult）。"""
from __future__ import annotations

import csv
import os

import pandas as pd
import pytest

from screener.report import CSV_COLUMNS, write_csv, write_report
from screener.screener import ScreenResult


def _make_result() -> ScreenResult:
    r = ScreenResult(run_day="2026-09-04", requested_date="2026-09-04")
    r.candidates = pd.DataFrame([
        {"code": "sh.601398", "name": "工商银行", "industry": "J66货币金融服务",
         "close": 8.13, "ma": 7.9, "window_return_pct": 12.5, "annual_vol_pct": 15.2,
         "cash_per_share": 0.3103, "dividend_yield_pct": 3.82, "roe_pct": 4.05,
         "yoy_net_profit_pct": 3.32, "liability_pct": 92.37, "gross_margin_pct": None,
         "pass_technical": True, "pass_dividend": True,
         "industry_rank": 1, "industry_percentile": 5.0, "industry_group_size": 20,
         "industry_group_skipped": False, "pass_industry": True, "pass_fundamental": False,
         "pass_all": False},
        {"code": "sz.000651", "name": "格力电器", "industry": "C38电气机械",
         "close": 40.0, "ma": 38.0, "window_return_pct": 25.0, "annual_vol_pct": 28.0,
         "cash_per_share": 1.8, "dividend_yield_pct": 4.5, "roe_pct": 22.1,
         "yoy_net_profit_pct": 8.0, "liability_pct": 65.0, "gross_margin_pct": 30.0,
         "pass_technical": True, "pass_dividend": True,
         "industry_rank": 2, "industry_percentile": 10.0, "industry_group_size": 10,
         "industry_group_skipped": False, "pass_industry": True, "pass_fundamental": True,
         "pass_all": True},
    ])
    r.funnel = {"L1_股票池": 5207, "L2_技术面": 800, "L3_股息率": 120,
                "L4_行业排名": 60, "L5_最终入选": 1}
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
    return {
        "technical": {"ma_period": 200, "return_window_days": 250, "min_return_pct": 0,
                      "max_return_pct": 100, "max_annual_volatility_pct": 45},
        "dividend": {"window_days": 365, "min_yield_pct": 3},
        "industry": {"top_pct": 30, "min_group_size": 5},
        "universe": {"listing_min_trading_days": 250},
        "fundamental": {"net_profit_yoy_field": "YOYPNI"},
    }


def test_csv_columns_contract(tmp_path):
    """CSV 必须包含 brief §3.6 要求的全部字段。"""
    required = ["code", "name", "industry", "close", "dividend_yield_pct", "roe_pct",
                "industry_percentile", "pass_technical", "pass_dividend",
                "pass_industry", "pass_fundamental"]
    for col in required:
        assert col in CSV_COLUMNS, f"CSV 缺少必需列 {col}"

    p = tmp_path / "result_20260904.csv"
    n = write_csv(_make_result(), str(p))
    assert n == 2
    with open(p, encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    header, data = rows[0], rows[1:]
    assert header == CSV_COLUMNS
    # 布尔列用"是/否"
    idx_pass_all = header.index("pass_all")
    assert [r[idx_pass_all] for r in data] == ["否", "是"]
    # 缺失值 → 空串（不是 NaN）
    idx_gm = header.index("gross_margin_pct")
    assert data[0][idx_gm] == ""


def test_report_structure(tmp_path):
    p = tmp_path / "report_20260904.md"
    write_report(_make_result(), _cfg(), str(p))
    text = p.read_text(encoding="utf-8")
    assert "# A股选股报告 · 2026-09-04" in text
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


def test_report_empty_final(tmp_path):
    """无入选时报告不崩溃。"""
    r = _make_result()
    r.candidates = r.candidates.copy()
    r.candidates["pass_all"] = False
    r.funnel["L5_最终入选"] = 0
    p = tmp_path / "report.md"
    write_report(r, _cfg(), str(p))
    assert "无股票通过全部四个维度" in p.read_text(encoding="utf-8")
