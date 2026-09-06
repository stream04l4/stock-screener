# -*- coding: utf-8 -*-
"""TTM 滚动股息率 + 股利支付率单测（v2 新因子，离线）。

fixture：tests/fixtures/dividend_601398_full_history_2019-2026.csv（真实留样，
含同一除权日重复行——验证 (code, ex-date) 去重）+ samples_v2 的 totalShare/netProfit。
"""
from __future__ import annotations

from datetime import date

import pytest

from screener.metrics import dedup_dividends, payout_ratio, ttm_dividend_yield
from conftest_helpers import dividend_records_601398


def test_dedup_dividends_same_ex_date():
    """同一除权日的重复行（预案+正式）只计一次。"""
    recs = [
        {"code": "x", "dividOperateDate": "2025-10-10", "dividCashPsBeforeTax": 0.3},
        {"code": "x", "dividOperateDate": "2025-10-10", "dividCashPsBeforeTax": 0.3},  # 重复
        {"code": "x", "dividOperateDate": "2026-06-10", "dividCashPsBeforeTax": 0.4},
    ]
    cash, ex = dedup_dividends(recs, date(2025, 9, 5), date(2026, 9, 4))
    assert cash == pytest.approx(0.7)
    assert ex == ["2025-10-10", "2026-06-10"]


def test_dedup_window_boundary():
    """窗口边界 [window_start, run_day] 闭区间。"""
    recs = [
        {"code": "x", "dividOperateDate": "2025-09-04", "dividCashPsBeforeTax": 1.0},  # 早一天，排除
        {"code": "x", "dividOperateDate": "2025-09-05", "dividCashPsBeforeTax": 2.0},  # 边界内
        {"code": "x", "dividOperateDate": "2026-09-04", "dividCashPsBeforeTax": 3.0},  # 边界内
        {"code": "x", "dividOperateDate": "2026-09-05", "dividCashPsBeforeTax": 4.0},  # 晚一天，排除
    ]
    cash, _ = dedup_dividends(recs, date(2025, 9, 5), date(2026, 9, 4))
    assert cash == pytest.approx(5.0)


def test_ttm_yield_icbc_research_value():
    """工行 TTM 股息率 = 研究留样 3.8167%（窗口 [2025-09-05, 2026-09-04]，af3 close=8.13）。

    真实 fixture：2025-12-15 (0.1414) + 2026-05-13 (0.1689) = 0.3103 → /8.13。
    """
    recs = dividend_records_601398()
    y = ttm_dividend_yield(recs, date(2025, 9, 5), date(2026, 9, 4), 8.13)
    assert y == pytest.approx(0.03816728167281673, rel=1e-9)


def test_ttm_yield_no_dividend_in_window():
    """窗口内无已除权分红 → None（非报错）。"""
    recs = [{"code": "x", "dividOperateDate": "2024-06-10", "dividCashPsBeforeTax": 0.5}]
    assert ttm_dividend_yield(recs, date(2025, 9, 5), date(2026, 9, 4), 10.0) is None


def test_ttm_yield_missing_price():
    """当前价缺失 → None。"""
    recs = [{"code": "x", "dividOperateDate": "2026-05-13", "dividCashPsBeforeTax": 0.1689}]
    assert ttm_dividend_yield(recs, date(2025, 9, 5), date(2026, 9, 4), None) is None


def test_payout_ratio_icbc_research_value():
    """工行支付率 = 研究留样 43.199%（2025 年度：0.4494 × totalShare / netProfit）。"""
    p = payout_ratio(0.4494, 356406257089.0, 370766000000.0)
    assert p == pytest.approx(0.43199476741609694, rel=1e-9)


def test_payout_ratio_moutai_research_value():
    """茅台支付率 = 研究留样 75.788%。"""
    p = payout_ratio(51.6300, 1252270215.0, 85310324833.67)
    assert p == pytest.approx(0.7578767438350239, rel=1e-9)


def test_payout_ratio_loss_making_or_no_dividend():
    """netProfit<=0（亏损）或无分红 → None。"""
    assert payout_ratio(0.5, 1e9, -1e8) is None
    assert payout_ratio(None, 1e9, 1e8) is None
    assert payout_ratio(0.5, None, 1e8) is None
