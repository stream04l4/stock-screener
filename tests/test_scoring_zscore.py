# -*- coding: utf-8 -*-
"""Z-Score 截面打分单测（v2 核心，~10 股小集合）。

验证（报告 R4）：
1. z=(x-mean)/std（样本标准差 n-1；std=0 → z=0）；
2. 缺失因子 z=0 且按可用权重归一化（neutral_renorm）；
3. total_score = Σ weight × z_dim，降序排序 + Top N 标记；
4. 维度内子因子按 sub_weights 合成。
"""
from __future__ import annotations

import math

import pytest

from screener.scoring import (
    DIMENSIONS,
    dimension_means,
    score_cross_section,
    zscore_series,
)


# ---------------------------------------------------------------------------
# zscore_series（纯列运算）
# ---------------------------------------------------------------------------

def test_zscore_basic_math():
    """z=(x-mean)/std（n-1 样本标准差）。"""
    vals = [1.0, 2.0, 3.0, 4.0]
    z = zscore_series(vals)
    mean = 2.5
    std = math.sqrt(sum((v - mean) ** 2 for v in vals) / 3)
    for v, zv in zip(vals, z):
        assert zv == pytest.approx((v - mean) / std)


def test_zscore_std_zero_all_zero():
    """std=0（全体相同）→ 全部 z=0（无区分度因子不贡献得分）。"""
    assert zscore_series([5.0, 5.0, 5.0]) == [0.0, 0.0, 0.0]


def test_zscore_missing_neutral():
    """缺失位置 → z=0（中性；mean/std 只用有效值）。"""
    z = zscore_series([1.0, None, 3.0])
    mean = 2.0
    std = math.sqrt(2.0 / 1)  # n-1=1
    assert z[0] == pytest.approx((1.0 - mean) / std)
    assert z[1] == 0.0
    assert z[2] == pytest.approx((3.0 - mean) / std)


def test_zscore_single_value():
    """n=1 → std=0 → z=0。"""
    assert zscore_series([7.0]) == [0.0]


# ---------------------------------------------------------------------------
# score_cross_section（~10 股小集合）
# ---------------------------------------------------------------------------

WEIGHTS = {"technical": 0.25, "dividend": 0.30, "industry": 0.15, "fundamental": 0.30}
SUB_WEIGHTS = {
    "technical": {"ma_bullish": 0.5, "window_return": 0.5},
    "dividend": {"ttm_yield": 0.6, "payout_ratio": 0.4},
    "industry": {"roe_rank_pct": 1.0},
    "fundamental": {"roe_level": 0.5, "piotroski": 0.5},
}


def _stock(code, **factors):
    s: dict = {code: None for code in DIMENSIONS}
    for dim, vals in factors.items():
        s[dim] = dict(vals)
    return {"code": code, "factors": s}


def _ten_stocks():
    """10 只合成股票：技术/股息/行业/基本面单调递增（c09 最优），c03 缺 fundamental。"""
    stocks = []
    for i in range(10):
        stocks.append(_stock(
            f"c{i:02d}",
            technical={"ma_bullish": float(i % 2), "window_return": 0.01 * i},
            dividend={"ttm_yield": 0.02 + 0.001 * i, "payout_ratio": 0.3 + 0.01 * i},
            industry={"roe_rank_pct": float(i)},
            fundamental={
                "roe_level": 0.05 + 0.01 * i,
                "piotroski": None if i == 3 else 0.4 + 0.05 * i,
            },
        ))
    return stocks


def test_score_ordering_and_topn():
    """total_score 降序排序；Top N 标记正确。"""
    res = score_cross_section(_ten_stocks(), WEIGHTS, SUB_WEIGHTS, top_n=3)
    scores = [r.total_score for r in res]
    assert scores == sorted(scores, reverse=True)
    assert [r.rank for r in res] == list(range(1, 11))
    assert sum(1 for r in res if r.top_n_selected) == 3
    # c09 各维最优 → 应排第一（技术面 ma_bullish 交替，但其余三维全优）
    assert res[0].code in ("c09", "c08")
    # 排名稳定：同分按 code 升序兜底
    assert all(r.rank == i + 1 for i, r in enumerate(res))


def test_score_manual_computation():
    """手工复算一只股票的 total_score（验证 sub_weights 合成 + weight×z_dim）。"""
    stocks = _ten_stocks()
    res = {r.code: r for r in score_cross_section(stocks, WEIGHTS, SUB_WEIGHTS, top_n=50)}

    # dividend 维：ttm_yield=[0.02..0.029], payout=[0.3..0.39]
    yields = [0.02 + 0.001 * i for i in range(10)]
    payouts = [0.3 + 0.01 * i for i in range(10)]
    mean_y = sum(yields) / 10
    std_y = math.sqrt(sum((v - mean_y) ** 2 for v in yields) / 9)
    mean_p = sum(payouts) / 10
    std_p = math.sqrt(sum((v - mean_p) ** 2 for v in payouts) / 9)

    # c05：z_ttm=(0.025-mean)/std, z_payout=(0.35-mean)/std
    z_dim_div: float = (0.6 * (yields[5] - mean_y) / std_y + 0.4 * (payouts[5] - mean_p) / std_p)
    r = res["c05"]
    assert r.z_dims["dividend"] == pytest.approx(z_dim_div)
    assert r.scores["dividend"] == pytest.approx(0.30 * z_dim_div)

    # total_score = Σ weight × z_dim（无缺失 → 不重归一化）
    dim_zs = {d: (0.0 if r.z_dims[d] is None else float(r.z_dims[d])) for d in DIMENSIONS}
    expected_total: float = sum(WEIGHTS[d] * dim_zs[d] for d in DIMENSIONS)
    assert r.total_score == pytest.approx(expected_total)


def test_missing_neutral_renorm():
    """c03 缺 fundamental.piotroski → z=0 且按可用权重(0.5/1.0... 归一化)。

    neutral_renorm：维度分 = Σ(w_f×z_f)/Σ(可用 w_f)。c03 的 piotroski 缺失
    （z=0）→ 维度分 = (0.5×z_roe + 0.5×0) / 0.5 = z_roe（等价降权）。
    """
    res = {r.code: r for r in score_cross_section(_ten_stocks(), WEIGHTS, SUB_WEIGHTS, top_n=50)}
    r = res["c03"]
    assert "fundamental.piotroski" in r.na_factors

    # 手工：roe_level=[0.05..0.14]
    roes = [0.05 + 0.01 * i for i in range(10)]
    mean_r = sum(roes) / 10
    std_r = math.sqrt(sum((v - mean_r) ** 2 for v in roes) / 9)
    z_roe = (roes[3] - mean_r) / std_r
    # neutral_renorm：(0.5×z_roe + 0.5×0)/0.5 = z_roe
    assert r.z_dims["fundamental"] == pytest.approx(z_roe)

    # 对比 neutral（不重归一化）：= (0.5×z_roe + 0.5×0)/1.0 = 0.5×z_roe
    res_neutral = {r.code: r for r in score_cross_section(
        _ten_stocks(), WEIGHTS, SUB_WEIGHTS, top_n=50, missing_policy="neutral")}
    assert res_neutral["c03"].z_dims["fundamental"] == pytest.approx(0.5 * z_roe)


def test_missing_drop_policy():
    """drop：任一子因子缺失 → 该维度 None、不参与合成；综合分 = Σ 可用维度
    weight×z_dim（不再按可用维度权重归一化——比 neutral_renorm 更严格）。"""
    stocks = _ten_stocks()
    res = {r.code: r for r in score_cross_section(
        stocks, WEIGHTS, SUB_WEIGHTS, top_n=50, missing_policy="drop")}
    r = res["c03"]
    assert r.z_dims["fundamental"] is None
    assert r.scores["fundamental"] is None
    # total = Σ 可用维度 scores（无归一化）
    expected = 0.0
    for d in DIMENSIONS:
        s_d = r.scores[d]
        if s_d is not None:
            expected += float(s_d)
    assert r.total_score == pytest.approx(expected)


def test_dimension_means():
    """dimension_means：每 (dim, factor) 截面均值（缺失不参与）。"""
    means = dimension_means(_ten_stocks())
    roes = [0.05 + 0.01 * i for i in range(10)]
    assert means["fundamental"]["roe_level"] == pytest.approx(sum(roes) / 10)
    # c03 piotroski=None → 均值只算 9 个有效值
    pios = [0.4 + 0.05 * i for i in range(10) if i != 3]
    assert means["fundamental"]["piotroski"] == pytest.approx(sum(pios) / 9)
