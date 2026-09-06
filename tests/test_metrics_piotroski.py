# -*- coding: utf-8 -*-
"""Piotroski F-Score 单测（R3 映射表；fixture = samples_v2/piotroski_raw_* 真实值）。

两套 fixture：
1. 非金融全字段（sh.600519 贵州茅台）→ 9 信号全有效，实测 F=5/9；
2. 银行金融业（sh.601398 工商银行）gpMargin/currentRatio 为空 → S6/S8 N/A，
   F=4/7（N/A 不计入分母，不得当 0 分）。
"""
from __future__ import annotations

import pytest

from screener.metrics import piotroski_fscore


# ---------------------------------------------------------------------------
# fixture：真实留样值（samples_v2/piotroski_raw_sh.600519_*.csv）
# ---------------------------------------------------------------------------

def _moutai():
    """贵州茅台 2024Q4/2025Q4 真实字段。"""
    profit_cur = {"roeAvg": "0.344620", "gpMargin": "0.911796",
                  "netProfit": "85310324833.67", "MBRevenue": "172054171890.91",
                  "totalShare": "1252270215.00"}
    profit_prior = {"roeAvg": "0.384283", "gpMargin": "0.919312",
                    "netProfit": "89334728025.90", "MBRevenue": "170611838052.02",
                    "totalShare": "1256197800.00"}
    balance_cur = {"currentRatio": "5.090027", "YOYLiability": "-0.123964"}
    balance_prior = {"currentRatio": "4.454079"}
    growth_cur = {"YOYAsset": "0.016358"}
    cashflow_cur = {"CFOToNP": "0.721158"}
    return profit_cur, profit_prior, balance_cur, balance_prior, growth_cur, cashflow_cur


def _icbc():
    """工商银行 2024Q4/2025Q4 真实字段（金融业：gpMargin/currentRatio 为空）。"""
    profit_cur = {"roeAvg": "0.089739", "gpMargin": "",
                  "netProfit": "370766000000.0", "MBRevenue": "838270000000.0",
                  "totalShare": "356406257089.00"}
    profit_prior = {"roeAvg": "0.094701", "gpMargin": "",
                    "netProfit": "366946000000.0", "MBRevenue": "821803000000.0",
                    "totalShare": "356406257089.00"}
    balance_cur = {"currentRatio": "", "YOYLiability": "0.097498"}
    balance_prior = {"currentRatio": ""}
    growth_cur = {"YOYAsset": "0.095368"}
    cashflow_cur = {"CFOToNP": "5.098984"}
    return profit_cur, profit_prior, balance_cur, balance_prior, growth_cur, cashflow_cur


def test_piotroski_non_finance_full_fields():
    """茅台：9 信号全有效，逐信号对照研究留样（S1=1 S2=1 S3=0 S4=0 S5=1 S6=1 S7=1 S8=0 S9=0）。"""
    pc, pp, bc, bp, gc, cf = _moutai()
    r = piotroski_fscore("sh.600519", pc, pp, bc, bp, gc, cf)
    assert r.n_valid == 9 and r.n_na == 0
    assert r.signals == {
        "S1_ROA_pos": 1,      # netProfit > 0
        "S2_CFO_pos": 1,      # sign(NI × CFOToNP) = (+)(+) > 0
        "S3_dROA_up": 0,      # roeAvg 0.3446 < 0.3843（下降）
        "S4_CFO_gt_NI": 0,    # CFOToNP 0.721 <= 1
        "S5_deleveraging": 1, # YOYLiability -0.124 < 0
        "S6_liquidity_up": 1, # currentRatio 5.09 > 4.45
        "S7_no_new_shares": 1,# totalShare 1252.3M <= 1256.2M
        "S8_gm_up": 0,        # gpMargin 0.9118 < 0.9193
        "S9_turnover_up": 0,  # revG 0.82% < assetG 1.64%
    }
    assert r.fscore == 5          # 研究留样 FSCORE_valid=5
    assert r.ratio == pytest.approx(5 / 9)


def test_piotroski_bank_na_normalization():
    """工行：gpMargin/currentRatio 空 → S6/S8 N/A（不计入分母），F=4/7。"""
    pc, pp, bc, bp, gc, cf = _icbc()
    r = piotroski_fscore("sh.601398", pc, pp, bc, bp, gc, cf)
    assert r.n_valid == 7 and r.n_na == 2
    assert sorted(r.na_signals) == ["S6_liquidity_up", "S8_gm_up"]
    assert r.signals["S6_liquidity_up"] is None
    assert r.signals["S8_gm_up"] is None
    # 有效信号：S1=1 S2=1 S3=0 S4=1 S5=0 S7=1 S9=0 → 和=4（研究留样 FSCORE_valid=4）
    assert r.fscore == 4
    assert r.ratio == pytest.approx(4 / 7)


def test_piotroski_missing_table_all_na():
    """四表全缺 → 9 信号全 N/A，ratio=None（不得当 0 分）。"""
    r = piotroski_fscore("x", None, None, None, None, None, None)
    assert r.n_valid == 0 and r.n_na == 9
    assert r.fscore == 0
    assert r.ratio is None


def test_piotroski_partial_missing():
    """部分字段缺失 → 仅对应信号 N/A（如缺 prior 利润表 → S3/S7/S8/S9 N/A）。"""
    pc, pp, bc, bp, gc, cf = _moutai()
    r = piotroski_fscore("x", pc, None, bc, None, gc, cf)
    # 缺 prior：S3(ΔROE)、S7(totalShare 对比)、S8(gpMargin 对比)、S9(MBRevenue 增速) N/A；
    # S6 需 prior currentRatio → N/A
    for k in ("S3_dROA_up", "S6_liquidity_up", "S7_no_new_shares", "S8_gm_up", "S9_turnover_up"):
        assert r.signals[k] is None, k
    # S1/S2/S4/S5 仍有效
    assert r.n_valid == 4 and r.n_na == 5
