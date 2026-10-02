# -*- coding: utf-8 -*-
"""test_adata_sentinel_pubdate —— v6.3.5：adata F10 notice_date 哨兵值过滤。

生产事故（2026-10-02 审查实锤）：东财 get_core_index 对 notice_date 缺失的老财报
（9 只老股 71 行，2001Q4~2007Q2）返回默认值 **1900-01-01** 而非空值——原
"pub_date 缺失行跳过"防不住这个哨兵：

1. PIT 污染：screener/data/lake_source.py 的 PIT 查询 ``pub_date <= run_day``
   让"未披露"行恒命中 → 历史时点筛选用上了当时根本没披露的财务数据
   （伪"远古已披露"）。
2. 展示污染：/status T5 数据区间 min(pub_date)=1900-01-01（页面显示"1900 年"）。

修复：fetch_f10 里 pub_date < 2000-01-01 → 视为缺失，跳过（A 股财报真披露日
最早 2000 年，阈值留了 100 年安全余量）。本文件 = 回归纪律（离线，零网络）。
"""
from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import pandas as pd  # noqa: E402

from lake.ingest.adata_f10_adapter import AdataF10Adapter  # noqa: E402


def _make_adapter(monkeypatch, rows):
    """构造 adapter 并 mock _core_index（离线；零网络）。"""
    ad = AdataF10Adapter()
    monkeypatch.setattr(ad, "_limiter_wait", lambda: None, raising=False)
    ad._limiter.wait = lambda: None
    df = pd.DataFrame(rows)
    monkeypatch.setattr(ad, "_core_index", lambda code6: df)
    return ad


def _row(report_date, notice_date, **kw):
    base = {"report_date": report_date, "notice_date": notice_date,
            "roe_wtd": 12.5, "gross_margin": 30.0,
            "asset_liab_ratio": 55.0, "net_profit_yoy_gr": 8.0,
            "net_profit_attr_sh": 1e9}
    base.update(kw)
    return base


def test_sentinel_1900_skipped(monkeypatch):
    """notice_date=1900-01-01（东财缺失默认值）→ 跳过，不进 out。"""
    ad = _make_adapter(monkeypatch, [
        _row("2005-03-31", "1900-01-01"),        # 哨兵 → 跳过
        _row("2005-06-30", "2005-08-20"),        # 真披露日 → 保留
    ])
    recs = ad.fetch_f10("sh.601099")
    assert len(recs) == 1, f"哨兵行应被跳过: {recs}"
    assert recs[0]["period"] == "2005Q2" and recs[0]["pub_date"] == "2005-08-20"


def test_genuine_old_pubdate_kept(monkeypatch):
    """真早期披露日（1990/2000 年代）不受阈值误伤——只挡 <2000 的哨兵。"""
    ad = _make_adapter(monkeypatch, [
        _row("1990-03-31", "1990-04-28"),        # 生产库真实最早披露（1990Q1）
        _row("1999-12-31", "2000-04-15"),        # 2000 边界（>=2000-01-01 保留）
    ])
    recs = ad.fetch_f10("sh.600000")
    assert len(recs) == 2, f"早期真披露日不应被误杀: {recs}"
    assert {r["pub_date"] for r in recs} == {"1990-04-28", "2000-04-15"}


def test_empty_notice_date_still_skipped(monkeypatch):
    """传统缺失（空 notice_date）→ 仍跳过（原行为不回归）。"""
    ad = _make_adapter(monkeypatch, [
        _row("2004-12-31", ""),
        _row("2005-03-31", "2005-04-26"),
    ])
    recs = ad.fetch_f10("sz.002500")
    assert len(recs) == 1 and recs[0]["period"] == "2005Q1"


def test_all_sentinel_returns_empty(monkeypatch):
    """整股全哨兵 → []（该 (ts_code, period) 全 PIT 不可用——调用方零行不 mark_done
    或留空，与"无数据"同路径，不污染）。"""
    ad = _make_adapter(monkeypatch, [
        _row("2004-12-31", "1900-01-01"),
        _row("2005-03-31", "1900-01-01"),
    ])
    assert ad.fetch_f10("sh.601901") == []
