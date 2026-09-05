# -*- coding: utf-8 -*-
"""行业排名单测（离线）。"""
from __future__ import annotations

import pytest

from screener.metrics import build_industry_groups, compute_industry_rank, industry_pass


def test_top30_percentile():
    """10 只股票按 ROE 降序，前 30% = 前 3 名（percentile <= 30）。"""
    codes = [f"c{i:02d}" for i in range(10)]
    roe_map = {c: (9 - idx) * 0.01 for idx, c in enumerate(codes)}  # c00 最高(0.09), c09 最低(0.01)
    groups = build_industry_groups({c: "C15酒饮料" for c in codes})
    res_best = compute_industry_rank("c00", groups["C15酒饮料"], roe_map, top_pct=0.30, min_group_size=5)
    res_mid = compute_industry_rank("c04", groups["C15酒饮料"], roe_map, top_pct=0.30, min_group_size=5)
    res_worst = compute_industry_rank("c09", groups["C15酒饮料"], roe_map, top_pct=0.30, min_group_size=5)

    assert res_best.rank == 1 and res_best.percentile == pytest.approx(10.0)
    assert industry_pass(res_best, 0.30) is True
    # c04 = 第5名 → percentile 50% > 30% → 不通过
    assert res_mid.rank == 5 and res_mid.percentile == pytest.approx(50.0)
    assert industry_pass(res_mid, 0.30) is False
    assert res_worst.rank == 10
    assert industry_pass(res_worst, 0.30) is False


def test_small_group_skipped():
    """组内不足 min_group_size → 跳过排名约束（视为通过），报告注明。"""
    codes = ["a", "b", "c"]
    roe_map = {"a": 0.5, "b": 0.1, "c": None}
    res = compute_industry_rank("c", codes, roe_map, top_pct=0.30, min_group_size=5)
    assert res.group_skipped is True
    assert res.rank is None and res.percentile is None
    assert industry_pass(res, 0.30) is True


def test_empty_industry_no_crash():
    """industry 为空 → 归入'无行业'组，不崩溃。"""
    imap = {"a": "", "b": "C15酒饮料", "c": ""}
    groups = build_industry_groups(imap)
    assert set(groups.keys()) == {"无行业", "C15酒饮料"}
    assert sorted(groups["无行业"]) == ["a", "c"]
    roe_map = {"a": 0.3, "b": 0.4, "c": 0.2}
    res = compute_industry_rank("a", groups["无行业"], roe_map, top_pct=0.30, min_group_size=5)
    assert res.industry == "无行业"
    assert res.group_skipped is True  # 只有2只 < 5


def test_roe_missing_ranks_last():
    """ROE 缺失的股票排在组内最后。"""
    codes = ["a", "b", "c", "d", "e"]
    roe_map = {"a": 0.3, "b": None, "c": 0.5, "d": 0.1, "e": 0.4}
    res_b = compute_industry_rank("b", codes, roe_map, top_pct=0.60, min_group_size=5)
    assert res_b.rank == 5  # ROE 缺失排最后
    res_c = compute_industry_rank("c", codes, roe_map, top_pct=0.60, min_group_size=5)
    assert res_c.rank == 1


def test_group_sizes():
    imap = {"a": "X", "b": "X", "c": "Y"}
    groups = build_industry_groups(imap)
    assert len(groups["X"]) == 2 and len(groups["Y"]) == 1
