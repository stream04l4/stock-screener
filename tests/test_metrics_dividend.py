# -*- coding: utf-8 -*-
"""股息率单测（离线，真实样例数据 601398）。

覆盖高危坑：
- 同一除权日重复行必须去重（2025-01-07 两行 0.1434 → 只算一次）
- 窗口筛选 window_start <= dividOperateDate <= run_day
- 无分红记录 → 不通过（不是报错）
- 金额已是元/股，不再除 10
"""
from __future__ import annotations

from datetime import date

import pytest

from conftest_helpers import dividend_records_601398
from screener.metrics import compute_dividend_yield


def test_research_report_example():
    """调研报告 §1 样例：窗口 [2025-09-05, 2026-09-05]，命中 2025-12-15(0.1414)
    + 2026-05-13(0.1689)，和=0.3103，当前价 8.13 → 股息率 3.82%。"""
    recs = dividend_records_601398()
    r = compute_dividend_yield(
        "sh.601398", recs,
        window_start=date(2025, 9, 5), run_day=date(2026, 9, 5),
        current_price=8.13, min_yield=0.03,
    )
    assert r.has_dividend is True
    assert len(r.dividends_in_window) == 2
    assert r.cash_per_share == pytest.approx(0.1414 + 0.1689)
    assert r.yield_pct == pytest.approx(0.3103 / 8.13, rel=1e-9)
    assert not r.fail_reasons  # 3.82% >= 3% → 通过


def test_duplicate_ex_date_dedup():
    """高危坑：同一除权日 2025-01-07 有预案+正式两行（均 0.1434），求和只算一次。

    窗口 [2024-12-01, 2025-02-01] 只命中该除权日 → cash 应为 0.1434 而非 0.2868。
    """
    recs = dividend_records_601398()
    r = compute_dividend_yield(
        "sh.601398", recs,
        window_start=date(2024, 12, 1), run_day=date(2025, 2, 1),
        current_price=5.0, min_yield=0.03,
    )
    assert len(r.dividends_in_window) == 1
    assert r.cash_per_share == pytest.approx(0.1434)


def test_no_dividend_not_error():
    """无分红记录 → 该维度不通过（fail_reasons 非空），不抛异常。"""
    r = compute_dividend_yield(
        "sh.688999", [],  # 空记录
        window_start=date(2025, 9, 5), run_day=date(2026, 9, 5),
        current_price=100.0, min_yield=0.03,
    )
    assert r.has_dividend is False
    assert r.yield_pct is None
    assert r.fail_reasons


def test_window_boundary_excluded():
    """除权日恰在窗口起点之前一天 → 不计入。"""
    recs = [
        {"code": "x", "dividOperateDate": "2025-09-04", "dividCashPsBeforeTax": 1.0},
        {"code": "x", "dividOperateDate": "2025-09-05", "dividCashPsBeforeTax": 2.0},
    ]
    r = compute_dividend_yield(
        "x", recs, window_start=date(2025, 9, 5), run_day=date(2026, 9, 5),
        current_price=10.0, min_yield=0.03,
    )
    assert r.cash_per_share == pytest.approx(2.0)


def test_unimplemented_dividend_excluded():
    """无 dividOperateDate（仅预案）→ 不算已除权，不计入。"""
    recs = [
        {"code": "x", "dividOperateDate": "", "dividCashPsBeforeTax": 5.0},
        {"code": "x", "dividOperateDate": "2026-01-10", "dividCashPsBeforeTax": 1.0},
    ]
    r = compute_dividend_yield(
        "x", recs, window_start=date(2025, 9, 5), run_day=date(2026, 9, 5),
        current_price=10.0, min_yield=0.03,
    )
    assert r.cash_per_share == pytest.approx(1.0)


def test_future_ex_date_excluded():
    """除权日在运行日之后（已公告未实施）→ 不计入。"""
    recs = [
        {"code": "x", "dividOperateDate": "2026-09-10", "dividCashPsBeforeTax": 9.0},
    ]
    r = compute_dividend_yield(
        "x", recs, window_start=date(2025, 9, 5), run_day=date(2026, 9, 5),
        current_price=10.0, min_yield=0.03,
    )
    assert r.has_dividend is False


def test_missing_price():
    """当前价缺失 → 不通过（不是报错）。"""
    recs = [{"code": "x", "dividOperateDate": "2026-01-10", "dividCashPsBeforeTax": 1.0}]
    r = compute_dividend_yield(
        "x", recs, window_start=date(2025, 9, 5), run_day=date(2026, 9, 5),
        current_price=None, min_yield=0.03,
    )
    assert r.yield_pct is None
    assert any("当前价" in x for x in r.fail_reasons)


def test_yield_below_threshold():
    """股息率低于阈值 → 不通过。"""
    recs = [{"code": "x", "dividOperateDate": "2026-01-10", "dividCashPsBeforeTax": 0.1}]
    r = compute_dividend_yield(
        "x", recs, window_start=date(2025, 9, 5), run_day=date(2026, 9, 5),
        current_price=10.0, min_yield=0.03,  # 0.1/10 = 1% < 3%
    )
    assert r.yield_pct == pytest.approx(0.01)
    assert any("股息率" in x for x in r.fail_reasons)
