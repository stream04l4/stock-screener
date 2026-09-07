# -*- coding: utf-8 -*-
"""指标计算已知答案单测（报告 R5 测试规格 3：手工算的年化/回撤/夏普）。

期望值全部**独立推导**（测试内用基础公式手算，不引用 metrics_bt 内部函数），
覆盖：年化/波动/夏普/最大回撤/Calmar/beta+alpha/信息比率/月度胜率/逐年分解/切片。
口径：250 交易日年化（与 v2 一致）。
"""
from __future__ import annotations

import math
from typing import List

import pytest

from backtest.metrics_bt import (annualized_return, annualized_volatility,
                                 beta_alpha, calmar_ratio, daily_returns,
                                 information_ratio, max_drawdown,
                                 monthly_win_rate, sharpe_ratio, slice_window,
                                 summarize, yearly_breakdown)


def _navs_from_rets(rets: List[float]) -> List[float]:
    navs = [1.0]
    for r in rets:
        navs.append(navs[-1] * (1.0 + r))
    return navs


# ---------------------------------------------------------------------------
# 基础：日收益 / 年化
# ---------------------------------------------------------------------------
def test_daily_returns_basic():
    assert daily_returns([1.0, 1.1, 1.21]) == pytest.approx([0.1, 0.1])
    assert daily_returns([1.0]) == []


def test_annualized_return_one_year_exact():
    """250 个交易日（n=250）→ 年化 = 总收益。"""
    navs = _navs_from_rets([0.01] * 250)
    total = (1.01 ** 250 - 1.0)
    assert annualized_return(navs) == pytest.approx(total, rel=1e-9)


def test_annualized_return_compounding():
    """n=125（半年）翻倍 → 年化 = 2^(250/125) - 1 = 3.0。"""
    navs = [1.0] + [1.0] * 124 + [2.0]
    assert annualized_return(navs) == pytest.approx(2.0 ** (250 / 125) - 1.0, rel=1e-9)


def test_annualized_return_insufficient_points():
    assert annualized_return([1.0]) is None
    assert annualized_return([]) is None


# ---------------------------------------------------------------------------
# 波动 / 夏普（交替收益 → std 手算）
# ---------------------------------------------------------------------------
def test_volatility_and_sharpe_known_answer():
    """rets = [0.01, 0.02]×50：mean=0.015，样本std≈0.005（n-1）。"""
    rets = [0.01, 0.02] * 50
    navs = _navs_from_rets(rets)
    n = len(rets)
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1)
    std = math.sqrt(var)
    assert annualized_volatility(navs) == pytest.approx(std * math.sqrt(250), rel=1e-9)

    ar = annualized_return(navs)
    rf_pct = 2.0   # 2%
    expected_sharpe = (ar - rf_pct / 100.0) / (std * math.sqrt(250))
    assert sharpe_ratio(navs, rf_pct) == pytest.approx(expected_sharpe, rel=1e-9)


def test_zero_volatility_sharpe_none():
    """恒定收益 → std=0 → 夏普 None（除零保护）。"""
    navs = _navs_from_rets([0.01] * 30)
    assert sharpe_ratio(navs, 2.0) is None


# ---------------------------------------------------------------------------
# 最大回撤 / Calmar
# ---------------------------------------------------------------------------
def test_max_drawdown_known_answer():
    """[1, 1.5, 1.2, 1.8, 1.6]：峰 1.5 → 谷 1.2 → mdd = -0.2。"""
    assert max_drawdown([1.0, 1.5, 1.2, 1.8, 1.6]) == pytest.approx(-0.2)


def test_max_drawdown_no_dip():
    assert max_drawdown([1.0, 1.1, 1.2]) == 0.0


def test_calmar_known_answer():
    """Calmar = 年化/|mdd|；平盘一年 → ar=0 → None。"""
    navs = _navs_from_rets([0.0] * 249)          # 平盘一年 → ar=0 → calmar None
    assert calmar_ratio(navs) is None
    # [1, 1.5, 1.2, 2.0]：mdd=-0.2，ar=(2/1)^(250/3)-1 → Calmar = ar/0.2
    navs2 = [1.0, 1.5, 1.2, 2.0]
    ar = annualized_return(navs2)
    assert calmar_ratio(navs2) == pytest.approx(ar / 0.2, rel=1e-9)


# ---------------------------------------------------------------------------
# beta / alpha / 信息比率（OLS 精确恢复）
# ---------------------------------------------------------------------------
def test_beta_alpha_exact_ols():
    """r_s = a + b·r_b 精确线性 → OLS 恢复 b、alpha=a×250。"""
    rets_b = [0.01, -0.01, 0.02, -0.02] * 25
    a, b = 0.001, 1.5
    rets_s = [a + b * r for r in rets_b]
    navs_s, navs_b = _navs_from_rets(rets_s), _navs_from_rets(rets_b)
    beta, alpha_pct = beta_alpha(navs_s, navs_b)
    assert beta == pytest.approx(b, rel=1e-6)
    assert alpha_pct == pytest.approx(a * 250 * 100.0, rel=1e-4)


def test_beta_zero_variance_none():
    navs_s = _navs_from_rets([0.0] * 30)
    navs_b = _navs_from_rets([0.01] * 30)
    beta, alpha = beta_alpha(navs_s, navs_b)
    assert beta is None and alpha is None


def test_information_ratio_known_answer():
    """主动收益 [0.01, 0.02]×25：mean=0.015, std≈0.005 → IR = 3×√250。"""
    rets_b = [0.0] * 50
    active = [0.01, 0.02] * 25
    rets_s = list(active)
    navs_s, navs_b = _navs_from_rets(rets_s), _navs_from_rets(rets_b)
    n = len(active)
    mean = sum(active) / n
    std = math.sqrt(sum((x - mean) ** 2 for x in active) / (n - 1))
    assert information_ratio(navs_s, navs_b) == pytest.approx(
        mean / std * math.sqrt(250), rel=1e-9)


# ---------------------------------------------------------------------------
# 月度胜率 / 逐年分解 / 切片
# ---------------------------------------------------------------------------
def test_monthly_win_rate_known_answer():
    """3 个月：+、−、+ → 胜率 2/3。"""
    dates = ["2025-01-01", "2025-01-31", "2025-02-28", "2025-03-31"]
    navs = [1.0, 1.1, 1.05, 1.06]   # 1月 +10%、2月 -4.5%、3月 +0.95%
    assert monthly_win_rate(navs, dates) == pytest.approx(2 / 3 * 100.0)


def test_yearly_breakdown_known_answer():
    """两年：2025 收益 +10%，2026（至年中）+5%。"""
    dates = ["2025-01-01", "2025-12-31", "2026-06-30"]
    navs = [1.0, 1.1, 1.155]
    out = yearly_breakdown(dates, navs)
    assert len(out) == 2
    assert out[0]["year"] == 2025 and out[0]["total_return_pct"] == pytest.approx(10.0)
    assert out[1]["year"] == 2026 and out[1]["total_return_pct"] == pytest.approx(5.0)


def test_slice_window():
    dates = [f"2025-{i // 30 + 1:02d}-{i % 30 + 1:02d}" for i in range(800)]
    navs = [1.0 + i * 0.001 for i in range(800)]
    sd, sn = slice_window(dates, navs, 250)
    assert len(sd) == 250 and sd == dates[-250:] and sn == navs[-250:]
    # 不足窗口 → 全量
    sd2, sn2 = slice_window(dates[:100], navs[:100], 250)
    assert len(sd2) == 100


# ---------------------------------------------------------------------------
# summarize 集成（含换手率统计）
# ---------------------------------------------------------------------------
def test_summarize_integration():
    rets = [0.01, 0.02] * 50
    navs = _navs_from_rets(rets)
    dates = [f"2025-{(i // 30) + 1:02d}-{i % 30 + 1:02d}" for i in range(len(navs))]
    s = summarize(dates, navs, risk_free_pct=2.0,
                  turnover_by_day={"2025-01-15": 0.4, "2025-03-15": 0.6})
    assert s.total_return_pct == pytest.approx((navs[-1] / navs[0] - 1) * 100.0)
    assert s.avg_turnover_one_way == pytest.approx(50.0)   # (0.4+0.6)/2 × 100%
    assert s.sharpe is not None and s.max_drawdown_pct <= 0.0
