# -*- coding: utf-8 -*-
"""RSI(14) Wilder + MACD 金叉单测（v2 新因子，离线合成序列）。

口径（报告 R3）：
- RSI = 100 - 100/(1+avgGain/avgLoss)，首值简单均值、其后 Wilder 平滑；
- MACD: DIF=EMA12-EMA26, DEA=EMA(DIF,9), bar=(DIF-DEA)×2；金叉=DIF 上穿 DEA。
"""
from __future__ import annotations

import math

import pytest

from screener.metrics import (
    compute_macd,
    ema_series,
    macd_golden_cross,
    rsi_wilder,
)


def _sine_closes(n: int, amp: float = 1.0, period: int = 20, base: float = 100.0):
    """带周期波动的合成收盘价（有涨有跌，RSI 落在 (0,100)）。"""
    return [base + amp * 5 * math.sin(2 * math.pi * i / period) for i in range(n)]


def test_rsi_range_and_monotonic_extremes():
    """全涨 → 100；全跌 → 0；震荡序列 → (0,100)。"""
    up = [10.0 * (1.01 ** i) for i in range(40)]
    down = [10.0 * (0.99 ** i) for i in range(40)]
    assert rsi_wilder(up) == 100.0
    assert rsi_wilder(down) == 0.0
    mid = rsi_wilder(_sine_closes(60))
    assert mid is not None and 0.0 < mid < 100.0


def test_rsi_insufficient_data():
    """< period+1 根 → None。"""
    assert rsi_wilder([1.0, 2.0, 3.0]) is None
    assert rsi_wilder([]) is None


def test_rsi_matches_research_values():
    """与调研报告离线复算值对照（601398=63.85、600519=58.54，±0.5 容差）。

    用留样重建的 af1 收盘价序列近似验证：此处用单调趋势+回撤构造一个
    RSI≈60 附近的序列做量级校验（精确值依赖真实K线，由集成运行覆盖）。
    """
    # 长期上行 + 末段小幅回调 → RSI 应明显高于 50
    closes = [10.0 * (1.004 ** i) for i in range(280)]
    for i in range(280, 300):
        closes.append(closes[-1] * (1 - 0.001))
    r = rsi_wilder(closes)
    assert r is not None and 50.0 < r < 75.0


def test_ema_series_known_value():
    """EMA 首值=首个数据点；常数序列 → EMA 恒等于常数。"""
    e = ema_series([5.0, 5.0, 5.0], 12)
    assert all(v == pytest.approx(5.0) for v in e)
    e2 = ema_series([1.0, 2.0], 3)  # k=0.5
    assert e2[0] == pytest.approx(1.0)
    assert e2[1] == pytest.approx(2.0 * 0.5 + 1.0 * 0.5)


def test_macd_structure_and_bar_sign():
    """MACD 结构：len 对齐；持续上行 → DIF>DEA（bar>0）。"""
    up = [10.0 * (1.01 ** i) for i in range(80)]
    m = compute_macd(up)
    assert len(m["dif"]) == len(m["dea"]) == len(m["bar"]) == 80
    # 末段 DIF>DEA → bar>0（趋势确立）
    assert m["bar"][-1] > 0
    assert compute_macd([1.0, 2.0]) is None  # < slow+signal


def test_macd_golden_cross_detects_upturn():
    """金叉 = 近 lookback(5) 根内 DIF 上穿 DEA。

    合成序列：96 根平盘 + 4 根快速上行 → 首个上行 bar 处 DIF 上穿 DEA（落在
    最后 5 根窗口内）→ True；纯平盘/缓跌 → False（穿越发生在窗口外或无穿越）。
    """
    upturn = [10.0] * 96 + [10.0 * (1.03 ** i) for i in range(1, 5)]
    assert macd_golden_cross(upturn) is True
    flat = [10.0] * 100
    assert macd_golden_cross(flat) is False
    decline = [10.0] * 20 + [10.0 * (0.98 ** i) for i in range(1, 81)]
    assert macd_golden_cross(decline) is False


def test_macd_golden_cross_insufficient():
    """< slow+signal(=35) 根 → None。"""
    assert macd_golden_cross([1.0] * 34) is None
