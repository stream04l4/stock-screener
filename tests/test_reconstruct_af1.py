# -*- coding: utf-8 -*-
"""af1 本地重建单测（R1 核心逻辑的离线回归）。

验证：
1. ``af1_close(t) = af3_close(t) × F(t)``，F = 截至 t 的最新累计因子（首事件前=1.0）；
2. **新除权事件不改变事件日之前的任何值**（增量缓存"历史不可变+尾部追加"的数学基础）；
3. 边界：新股（无事件 F=1.0）、停牌空 close、退市（无新行）。

fixture 基于 samples_v2/reconstruction_fullhistory_sh.601398.csv 的结构截取。
"""
from __future__ import annotations

from datetime import date

import pytest

from screener.reconstruct import (
    factor_at,
    moving_average,
    rebuild_kline_series,
    reconstruct_af1,
)


def test_factor_at_step_function():
    """F(t) 是 step 函数：只取 ex_date <= t 的最近事件；无事件 → 1.0。"""
    factors = [(date(2024, 6, 1), 1.5), (date(2025, 2, 1), 2.0)]
    assert factor_at(date(2024, 5, 31), factors) == 1.0   # 首事件前
    assert factor_at(date(2024, 6, 1), factors) == 1.5    # 事件当天生效
    assert factor_at(date(2024, 12, 31), factors) == 1.5
    assert factor_at(date(2025, 2, 1), factors) == 2.0
    assert factor_at(date(2026, 9, 4), factors) == 2.0
    # 空序列（新股无除权）→ 恒 1.0
    assert factor_at(date(2026, 9, 4), []) == 1.0


def test_reconstruct_af1_basic():
    """af1 = af3 × F：两段因子，逐日验证。"""
    dates = ["2025-01-01", "2025-01-02", "2025-06-15", "2025-06-16"]
    af3 = [10.0, 11.0, 9.0, 9.5]
    factors = [(date(2025, 6, 15), 2.0)]
    out = reconstruct_af1(dates, af3, factors)
    assert out == [10.0, 11.0, 18.0, 19.0]


def test_reconstruct_new_event_does_not_change_history():
    """R1-a 核心性质：新增更晚的除权事件 → 事件日之前的 af1 全部不变。"""
    dates = [f"2025-01-{d:02d}" for d in range(1, 9)] + [f"2025-02-{d:02d}" for d in range(1, 6)]
    af3 = [10.0] * len(dates)
    factors_old = [(date(2024, 6, 1), 1.5)]
    old = reconstruct_af1(dates, af3, factors_old)

    # 新事件：2025-02-01 除权，累计因子升到 2.0
    factors_new = [(date(2024, 6, 1), 1.5), (date(2025, 2, 1), 2.0)]
    new = reconstruct_af1(dates, af3, factors_new)

    # 事件日（含）之前：逐值相等
    for i, d in enumerate(dates):
        if d < "2025-02-01":
            assert old[i] == pytest.approx(new[i]), f"{d} 被新事件改变了"
    # 事件日及之后：取新累计因子
    for i, d in enumerate(dates):
        if d >= "2025-02-01":
            assert new[i] == pytest.approx(af3[i] * 2.0)
            assert old[i] == pytest.approx(af3[i] * 1.5)


def test_reconstruct_new_stock_no_events():
    """新股：无除权事件 → F=1.0，af1==af3（自然成立，无需特判）。"""
    dates = ["2026-07-01", "2026-07-02"]
    af3 = [25.0, 26.5]
    out = reconstruct_af1(dates, af3, [])
    assert out == [25.0, 26.5]


def test_reconstruct_suspended_empty_close():
    """停牌空 close：保留日期、重建为 None（MA 计算时跳过）。"""
    dates = ["2025-01-01", "2025-01-02", "2025-01-03"]
    af3 = [10.0, None, 10.5]
    out = reconstruct_af1(dates, af3, [])
    assert out[0] == 10.0 and out[1] is None and out[2] == 10.5


def test_rebuild_kline_series_v2_layout():
    """rebuild_kline_series：v2 五字段布局（date,code,close,isST,tradestatus）按列名取 close。"""
    kline_rows = [
        ["2025-01-01", "sh.601398", "8.00", "0", "1"],
        ["2025-01-02", "sh.601398", "8.10", "0", "1"],
        ["2025-01-03", "sh.601398", "", "0", "0"],  # 停牌空 close
    ]
    columns = ["date", "code", "close", "isST", "tradestatus"]
    factor_rows = [["sh.601398", "2025-01-02", "0.4", "2.0", "2.0"]]
    out = rebuild_kline_series(kline_rows, factor_rows, columns)
    assert out["dates"] == ["2025-01-01", "2025-01-02", "2025-01-03"]
    assert out["af3_close"] == [8.0, 8.1, None]
    # F: 2025-01-01 → 1.0；2025-01-02 起 → 2.0
    assert out["af1_close"][0] == pytest.approx(8.0)
    assert out["af1_close"][1] == pytest.approx(16.2)
    assert out["af1_close"][2] is None


def test_rebuild_kline_series_legacy_15field_layout():
    """兼容早期 15 字段文件：显式传 columns 时按列名定位 close（位置 5）。"""
    kline_rows = [
        ["2025-01-01", "sh.601398", "7.9", "8.0", "7.8", "8.00", "7.9", "100", "800", "0.1",
         "1", "1.2", "5.0", "0.6", "0"],
    ]
    columns = ["date", "code", "open", "high", "low", "close", "preclose", "volume",
               "amount", "turn", "tradestatus", "pctChg", "peTTM", "pbMRQ", "isST"]
    out = rebuild_kline_series(kline_rows, [], columns)
    assert out["af3_close"] == [8.0]


def test_moving_average():
    """MA：前 period-1 为 None；窗口含 None → 该点 None。"""
    ma = moving_average([1.0, 2.0, 3.0, 4.0], 3)
    assert ma[0] is None and ma[1] is None
    assert ma[2] == pytest.approx(2.0)
    assert ma[3] == pytest.approx(3.0)
    ma2 = moving_average([1.0, None, 3.0], 2)
    assert ma2 == [None, None, None]
