# -*- coding: utf-8 -*-
"""ROE 近3年（年度Q4）稳定性单测（含 n<3 边界）。

口径（报告 R3）：mean/std(近3个Q4 roeAvg)；上市<3年用可得 n；n<2 → std=None。
std 用总体标准差 pstdev（与调研脚本 r3_piotroski.py 同口径）。
"""
from __future__ import annotations

import math

import pytest

from screener.metrics import roe_stability


def test_roe_stability_research_values_icbc():
    """工行：2023/2024/2025 Q4 ROE = 10.038%/9.470%/8.974% → mean=9.494%, std=0.435%。"""
    m, s = roe_stability([0.10038, 0.09470, 0.08974])
    assert m == pytest.approx(0.09494, abs=5e-5)
    assert s == pytest.approx(0.00435, abs=5e-5)


def test_roe_stability_research_values_moutai():
    """茅台：36.175%/38.428%/34.462% → mean=36.355%, std=1.624%。"""
    m, s = roe_stability([0.36175, 0.38428, 0.34462])
    assert m == pytest.approx(0.36355, abs=5e-5)
    assert s == pytest.approx(0.01624, abs=5e-5)


def test_roe_stability_population_std():
    """std 是总体标准差（除以 n，不是 n-1）——与调研脚本同口径。"""
    vals = [0.10, 0.20, 0.30]
    m, s = roe_stability(vals)
    assert m is not None and s is not None
    assert m == pytest.approx(0.20)
    pstdev = math.sqrt(sum((v - m) ** 2 for v in vals) / 3)
    assert s == pytest.approx(pstdev)
    # 区别于样本标准差（n-1）
    sample_std = math.sqrt(sum((v - m) ** 2 for v in vals) / 2)
    assert s != pytest.approx(sample_std)


def test_roe_stability_n_less_than_3():
    """上市<3年：用可得 n（n=2 → mean+std；n=1 → mean，std=None）。"""
    m2, s2 = roe_stability([0.15, 0.18])
    assert m2 is not None and s2 is not None
    assert m2 == pytest.approx(0.165)
    assert s2 >= 0

    m1, s1 = roe_stability([0.15])
    assert m1 == pytest.approx(0.15)
    assert s1 is None  # n<2 → std=None


def test_roe_stability_missing_values_skipped():
    """None（未披露年份）跳过，用可得值。"""
    m, s = roe_stability([None, 0.18, 0.15])
    assert m is not None and s is not None
    assert m == pytest.approx(0.165)


def test_roe_stability_all_missing():
    """全缺 → (None, None)。"""
    assert roe_stability([None, None]) == (None, None)
    assert roe_stability([]) == (None, None)
