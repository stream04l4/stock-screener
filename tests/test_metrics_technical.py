# -*- coding: utf-8 -*-
"""技术面指标单测（离线，固定样例数据）。"""
from __future__ import annotations

import math
from datetime import date

import pytest

from conftest_helpers import synthetic_closes, trading_dates
from screener.metrics import compute_technical


def test_above_ma_pass():
    """收盘价稳定上涨 → close > MA200，收益/波动率达标。"""
    closes = synthetic_closes(260, start_price=10.0, daily_drift=0.001)  # 温和上涨
    dates = trading_dates(260, date(2026, 9, 4))
    r = compute_technical("sh.600000", dates, closes, ma_period=200, return_window_days=250,
                          min_return=0.0, max_return=1.0, max_vol=0.45)
    assert r.above_ma is True
    assert not r.fail_reasons
    assert r.window_return is not None and 0 <= r.window_return <= 1.0
    # 恒定漂移 → 波动率≈0（round 到 6 位小数引入 ~1e-7 噪声）
    assert r.annual_volatility == pytest.approx(0.0, abs=1e-5)


def test_below_ma_fail():
    """横盘/下跌 → close < MA200 → 不通过。"""
    closes = synthetic_closes(260, start_price=10.0, daily_drift=-0.002)
    dates = trading_dates(260, date(2026, 9, 4))
    r = compute_technical("sh.600001", dates, closes, ma_period=200, return_window_days=250,
                          min_return=0.0, max_return=1.0, max_vol=0.45)
    assert r.above_ma is False
    assert any("MA" in x for x in r.fail_reasons)


def test_return_range_bounds():
    """区间收益率边界：恰好 0% 通过（含边界），>100% 不通过。"""
    # 构造：前 250 根=10，最后 10 根不变 → 收益≈0
    closes = [10.0] * 251 + [10.0] * 9
    dates = trading_dates(260, date(2026, 9, 4))
    r = compute_technical("c", dates, closes, ma_period=200, return_window_days=250,
                          min_return=0.0, max_return=1.0, max_vol=0.45)
    assert r.window_return == pytest.approx(0.0, abs=1e-9)

    # 暴涨 >100%：最后价格 = 2.1×基期 → 收益 110%
    closes2 = [10.0] * 251 + [21.0] * 9
    r2 = compute_technical("c", dates, closes2, ma_period=200, return_window_days=250,
                           min_return=0.0, max_return=1.0, max_vol=0.45)
    assert r2.window_return == pytest.approx(1.1)
    assert any("收益" in x for x in r2.fail_reasons)


def test_insufficient_history():
    """K线不足 MA 周期 → 数据不足，不抛异常。"""
    closes = synthetic_closes(50, start_price=10.0)
    dates = trading_dates(50, date(2026, 9, 4))
    r = compute_technical("c", dates, closes, ma_period=200, return_window_days=250,
                          min_return=0.0, max_return=1.0, max_vol=0.45)
    assert r.above_ma is None
    assert any("K线不足" in x for x in r.fail_reasons)


def test_annual_volatility_calc():
    """年化波动率公式验证：日收益率标准差 × sqrt(250)（与实现同口径）。"""
    import random
    random.seed(42)
    n = 300
    closes = [10.0]
    for _ in range(n - 1):
        closes.append(closes[-1] * (1 + random.gauss(0, 0.02)))
    dates = trading_dates(n, date(2026, 9, 4))
    r = compute_technical("c", dates, closes, ma_period=200, return_window_days=250,
                          min_return=-1.0, max_return=10.0, max_vol=10.0)
    # 手工复算：实现取 i ∈ [n-ma, n) 的 log(c[i]/c[i-1])，即 closes[n-ma-1:] 共 ma+2 个点
    seg = closes[n - 201:]
    rets = [math.log(seg[i] / seg[i - 1]) for i in range(1, len(seg))]
    mean = sum(rets) / len(rets)
    var = sum((x - mean) ** 2 for x in rets) / (len(rets) - 1)
    expected = math.sqrt(var) * math.sqrt(250)
    assert r.annual_volatility == pytest.approx(expected, rel=1e-9)


def test_high_volatility_fail():
    """日波动 3% → 年化 ≈ 47.4% > 45% → 不通过。"""
    import random
    random.seed(7)
    n = 300
    closes = [10.0]
    for _ in range(n - 1):
        closes.append(closes[-1] * (1 + random.gauss(0, 0.03)))
    dates = trading_dates(n, date(2026, 9, 4))
    r = compute_technical("c", dates, closes, ma_period=200, return_window_days=250,
                          min_return=-1.0, max_return=10.0, max_vol=0.45)
    assert r.annual_volatility is not None and r.annual_volatility > 0.45
    assert any("波动率" in x for x in r.fail_reasons)
