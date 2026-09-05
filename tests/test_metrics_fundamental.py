# -*- coding: utf-8 -*-
"""基本面单测（离线）。字段口径：BaoStock 返回小数（0.10=10%），阈值用小数比较。"""
from __future__ import annotations

import pytest

from screener.metrics import compute_fundamental


def _full(roe=0.12, yoy_pni=0.05, lia=0.40, gm=0.30):
    return (
        {"code": "x", "pubDate": "2026-08-29", "statDate": "2026-06-30",
         "roeAvg": roe, "npMargin": 0.2, "gpMargin": gm, "netProfit": 1e9},
        {"code": "x", "pubDate": "2026-08-29", "statDate": "2026-06-30",
         "YOYNI": yoy_pni * 1.1, "YOYPNI": yoy_pni},
        {"code": "x", "pubDate": "2026-08-29", "statDate": "2026-06-30",
         "liabilityToAsset": lia},
    )


def test_all_pass():
    p, g, b = _full()
    r = compute_fundamental("x", p, g, b, period="2026Q2", roe_min=0.10, yoy_field="YOYPNI",
                            liability_max=0.60, gross_margin_min=0.20)
    assert not r.fail_reasons and not r.missing
    assert r.roe_avg == pytest.approx(0.12)
    assert r.yoy_net_profit == pytest.approx(0.05)
    assert r.period == "2026Q2"


def test_roe_boundary_inclusive():
    """ROE 恰好 = 10% → 通过（>= 含边界）。"""
    p, g, b = _full(roe=0.10)
    r = compute_fundamental("x", p, g, b, period="2026Q2", roe_min=0.10, yoy_field="YOYPNI",
                            liability_max=0.60, gross_margin_min=0.20)
    assert not r.fail_reasons


def test_roe_below_fail():
    p, g, b = _full(roe=0.099)
    r = compute_fundamental("x", p, g, b, period="2026Q2", roe_min=0.10, yoy_field="YOYPNI",
                            liability_max=0.60, gross_margin_min=0.20)
    assert any("roeAvg" in x for x in r.fail_reasons)


def test_yoy_zero_fail():
    """净利同比 = 0 → 不通过（须 > 0，不含边界）。"""
    p, g, b = _full(yoy_pni=0.0)
    r = compute_fundamental("x", p, g, b, period="2026Q2", roe_min=0.10, yoy_field="YOYPNI",
                            liability_max=0.60, gross_margin_min=0.20)
    assert any("YOYPNI" in x for x in r.fail_reasons)


def test_yoy_field_switchable():
    """字段可切换：YOYNI 与 YOYPNI 不同时，按配置取对应字段。"""
    p, g, b = _full(yoy_pni=0.05)
    g["YOYNI"] = -0.2  # 净利润同比为负，但归母为正
    r1 = compute_fundamental("x", p, g, b, period="2026Q2", roe_min=0.10, yoy_field="YOYPNI",
                             liability_max=0.60, gross_margin_min=0.20)
    assert not r1.fail_reasons
    r2 = compute_fundamental("x", p, g, b, period="2026Q2", roe_min=0.10, yoy_field="YOYNI",
                             liability_max=0.60, gross_margin_min=0.20)
    assert any("YOYNI" in x for x in r2.fail_reasons)


def test_liability_boundary_inclusive():
    """负债率恰好 60% → 通过（<= 含边界）。"""
    p, g, b = _full(lia=0.60)
    r = compute_fundamental("x", p, g, b, period="2026Q2", roe_min=0.10, yoy_field="YOYPNI",
                            liability_max=0.60, gross_margin_min=0.20)
    assert not r.fail_reasons


def test_gross_margin_boundary_exclusive():
    """毛利率恰好 20% → 不通过（> 不含边界）。"""
    p, g, b = _full(gm=0.20)
    r = compute_fundamental("x", p, g, b, period="2026Q2", roe_min=0.10, yoy_field="YOYPNI",
                            liability_max=0.60, gross_margin_min=0.20)
    assert any("gpMargin" in x for x in r.fail_reasons)


def test_missing_gpmargin_bank():
    """金融业 gpMargin 为空（None）→ 落缺失名单，不崩溃。"""
    p, g, b = _full(gm=None)
    r = compute_fundamental("x", p, g, b, period="2026Q2", roe_min=0.10, yoy_field="YOYPNI",
                            liability_max=0.60, gross_margin_min=0.20)
    assert "gpMargin" in r.missing
    assert r.gross_margin is None


def test_quarter_not_disclosed():
    """季报未披露（三个接口全 None）→ 全部缺失，不崩溃。"""
    r = compute_fundamental("x", None, None, None, period="2026Q3", roe_min=0.10,
                            yoy_field="YOYPNI", liability_max=0.60, gross_margin_min=0.20)
    assert len(r.missing) == 4
    assert r.pub_date == ""
