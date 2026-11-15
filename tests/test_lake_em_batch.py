# -*- coding: utf-8 -*-
"""test_lake_em_batch —— 东财 push2/datacenter 全市场批量通道（tushare 替代）。

纪律（同 v708 契约）：全离线——tmp/in-memory 库 + monkeypatch 网络函数；
绝不触碰 data/lake/ 生产库与真实东财接口。

覆盖：
- clist 快照：字段映射（f17/open f15/high f16/low f18/preclose，f5 手→股，
  f20 元→亿元）、停牌 '-'→None、非沪深代码 fail-closed、EU→SG 出口降级；
- datacenter 除权事件：filter 括号包裹、/10 换算、配股标记；
- af 推导：官方除权参考价步长、非事件股前一日 level 沿用、新股 1.0 起步、
  停牌沿用、配股 fail-closed、历史 af NULL 跳过；
- snapshot_to_t2t3：停牌行 T2 剔除 T3 保留。
"""
from __future__ import annotations

import datetime as dt
import os
import sys

import pytest

pytest.importorskip("duckdb")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import duckdb  # noqa: E402
from lake.ingest import em_batch_ingest as emb  # noqa: E402


def _raw(f12, **kw):
    """构造 clist 原始行（默认=正常交易股）。"""
    base = {
        "f12": f12, "f14": "测试股", "f2": 10.0, "f3": 1.0,
        "f5": 1000, "f6": 100000.0, "f8": 0.5,
        "f15": 10.5, "f16": 9.8, "f17": 9.9, "f18": 9.9,
        "f20": 1e9, "f21": 8e8, "f26": 20200101,
        "f100": "测试行业", "f115": 8.8, "f167": 1.2,
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# fetch_clist_snapshot
# ---------------------------------------------------------------------------

def test_clist_snapshot_mapping_and_units(monkeypatch):
    pages = [
        {"data": {"total": 5, "diff": [
            _raw("600519"), _raw("000001"), _raw("300750")]}},
        {"data": {"total": 5, "diff": [
            _raw("601398"), _raw("002594")]}},
    ]
    it = iter(pages)

    def fake_get(url, params):
        return next(it)

    monkeypatch.setattr(emb, "_get_json", fake_get)
    monkeypatch.setattr(emb.time, "sleep", lambda s: None)
    snap = emb.fetch_clist_snapshot(allow_sg=False)
    assert len(snap) == 5
    r = snap[0]
    assert r["ts_code"] == "sh.600519"
    assert r["open"] == 9.9 and r["high"] == 10.5 and r["low"] == 9.8
    assert r["preclose"] == 9.9 and r["close"] == 10.0
    assert r["volume"] == 1000 * 100          # 手→股
    assert abs(r["total_mv_yi"] - 10.0) < 1e-9  # 元→亿元
    assert r["list_date"] == "2020-01-01"
    assert r["industry"] == "测试行业"
    assert r["pe_ttm"] == 8.8 and r["pb"] == 1.2


def test_clist_snapshot_suspended_dash_to_none(monkeypatch):
    pages = [{"data": {"total": 1, "diff": [
        _raw("000003", f2="-", f3="-", f5="-", f6="-", f8="-",
             f15="-", f16="-", f17="-", f18="-", f20="-", f21="-",
             f100="-", f115="-", f167="-")]}}]
    monkeypatch.setattr(emb, "_get_json", lambda url, params: pages[0])
    monkeypatch.setattr(emb.time, "sleep", lambda s: None)
    snap = emb.fetch_clist_snapshot(allow_sg=False)
    assert snap[0]["close"] is None and snap[0]["volume"] is None
    assert snap[0]["total_mv_yi"] is None


def test_clist_snapshot_non_hs_code_fail_closed(monkeypatch):
    """fs 参数漂移（出现北交所等）→ 拒绝整批（防污染 universe 口径）。"""
    pages = [{"data": {"total": 2, "diff": [_raw("600519"), _raw("920000")]}}]
    monkeypatch.setattr(emb, "_get_json", lambda url, params: pages[0])
    with pytest.raises(emb.EmBatchError, match="非沪深"):
        emb.fetch_clist_snapshot(allow_sg=False)


def test_clist_snapshot_eu_fail_falls_back_to_sg(monkeypatch):
    pages = [{"data": {"total": 1, "diff": [_raw("600519")]}}]
    monkeypatch.setattr(
        emb, "_fetch_all_pages",
        lambda url: (_ for _ in ()).throw(emb.EmBatchError("502 模拟")))
    got = {"called": 0}

    def fake_sg():
        got["called"] += 1
        return [pages[0]["data"]["diff"][0]], 1

    monkeypatch.setattr(emb, "_fetch_all_pages_sg", fake_sg)
    snap = emb.fetch_clist_snapshot(allow_sg=True)
    assert got["called"] == 1 and len(snap) == 1
    # allow_sg=False → 直接抛
    with pytest.raises(emb.EmBatchError):
        emb.fetch_clist_snapshot(allow_sg=False)


def test_clist_snapshot_empty_raises(monkeypatch):
    monkeypatch.setattr(emb, "_fetch_all_pages", lambda url: ([], None))
    with pytest.raises(emb.EmBatchError, match="为空"):
        emb.fetch_clist_snapshot(allow_sg=False)


# ---------------------------------------------------------------------------
# fetch_ex_events
# ---------------------------------------------------------------------------

def test_ex_events_parse_and_filter(monkeypatch):
    captured = {}

    def fake_get(url, params):
        captured.update(params)
        return {"result": {"pages": 1, "data": [
            {"SECURITY_CODE": "601091", "EX_DIVIDEND_DATE": 20261113,
             "PRETAX_BONUS_RMB": 2.0, "BONUS_RATIO": 4.0, "IT_RATIO": 0.0,
             "ALLOT_RATIO": 0.0, "IMPL_PLAN_PROFILE": "10派2元转4股"},
            {"SECURITY_CODE": "000002", "EX_DIVIDEND_DATE": 20261113,
             "PRETAX_BONUS_RMB": None, "BONUS_RATIO": None,
             "IT_RATIO": 10.0, "ALLOT_RATIO": None,
             "IMPL_PLAN_PROFILE": "10转10股"},
        ]}}

    monkeypatch.setattr(emb, "_get_json", fake_get)
    evs = emb.fetch_ex_events("2026-11-13")
    # filter 必须括号包裹（东财 datacenter 实测：不包 → count=None）
    assert captured["filter"] == "(EX_DIVIDEND_DATE='2026-11-13')"
    assert evs[0]["ts_code"] == "sh.601091"
    assert evs[0]["cash"] == pytest.approx(0.2)     # 2.0/10 每股
    assert evs[0]["bonus"] == pytest.approx(0.4)    # 4/10 每股送
    assert evs[0]["has_allot"] is False
    assert evs[1]["cash"] == 0.0 and evs[1]["bonus"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# derive_af_batch
# ---------------------------------------------------------------------------

D = "2026-11-13"
D_PREV = "2026-11-12"


def _lake(tmp_path):
    db = str(tmp_path / "t.duckdb")
    con = duckdb.connect(db)
    con.execute("CREATE TABLE kline_daily (ts_code VARCHAR, date DATE, "
                "close DOUBLE, adj_factor DOUBLE)")
    rows = [
        # A：事件股（现金 0.5 送转 0.4），c0=29.60，af_prev=1.0
        ("sh.600001", dt.date(2026, 11, 12), 29.60, 1.0),
        # B：非事件股 af_prev=2.5（沿用）
        ("sh.600002", dt.date(2026, 11, 12), 10.0, 2.5),
        # C：事件股但无任何历史行（新股除权）→ 1.0 起步
        ("sh.600003", dt.date(2026, 11, 13), 20.0, None),
        # D：事件股但 D-1 close NULL（停牌）→ 沿用
        ("sh.600004", dt.date(2026, 11, 10), 15.0, 3.0),
        # E：配股事件股 → fail-closed
        ("sh.600005", dt.date(2026, 11, 12), 8.0, 1.0),
        # F：历史 af 全 NULL → 不写（保持 NULL）
        ("sh.600006", dt.date(2026, 11, 12), 5.0, None),
        # G：多日停牌后 c0 取 D 前最近 close
        ("sh.600007", dt.date(2026, 11, 5), 40.0, 2.0),
        # H：有 D-1 close 但 af NULL（历史缺失）+ 事件 → 1.0 起步
        ("sh.600008", dt.date(2026, 11, 12), 5.0, None),
    ]
    con.executemany("INSERT INTO kline_daily VALUES (?,?,?,?)", rows)
    return con


def test_derive_af_refprice_step(tmp_path):
    con = _lake(tmp_path)
    out = emb.derive_af_batch(con, D, [
        {"ts_code": "sh.600001", "ex_date": D, "cash": 0.5, "bonus": 0.4,
         "has_allot": False},
        {"ts_code": "sh.600004", "ex_date": D, "cash": 0.0, "bonus": 0.0,
         "has_allot": False},
    ])
    # A：step = 29.60×1.4/(29.60−0.5) = 41.44/29.10
    assert out["sh.600001"][D] == pytest.approx(
        1.0 * 29.60 * 1.4 / (29.60 - 0.5), rel=1e-12)
    # B：非事件股沿用前一日 level
    assert out["sh.600002"][D] == pytest.approx(2.5)
    # D：停牌（D-1 无 close）→ 沿用 3.0
    assert out["sh.600004"][D] == pytest.approx(3.0)
    # F：历史 af NULL → 不在输出
    assert "sh.600006" not in out
    con.close()


def test_derive_af_new_stock_no_c0_skipped(tmp_path):
    con = _lake(tmp_path)
    out = emb.derive_af_batch(con, D, [
        {"ts_code": "sh.600003", "ex_date": D, "cash": 0.0, "bonus": 1.0,
         "has_allot": False},
    ])
    # 新股无任何 D-1 前 close → 无法推导步长 → 不写（fail-open 跳过，不抛错）
    assert "sh.600003" not in out
    con.close()


def test_derive_af_null_history_starts_at_1(tmp_path):
    con = _lake(tmp_path)
    out = emb.derive_af_batch(con, D, [
        {"ts_code": "sh.600008", "ex_date": D, "cash": 0.0, "bonus": 0.5,
         "has_allot": False},
    ])
    # c0=5.0，step = 5.0×1.5/5.0 = 1.5；base=1.0（历史 af NULL）
    assert out["sh.600008"][D] == pytest.approx(1.5)
    con.close()


def test_derive_af_allot_fail_closed(tmp_path):
    con = _lake(tmp_path)
    with pytest.raises(emb.EmBatchError, match="配股"):
        emb.derive_af_batch(con, D, [
            {"ts_code": "sh.600005", "ex_date": D, "cash": 0.0, "bonus": 0.0,
             "has_allot": True, "plan": "10配3股"},
        ])
    con.close()


def test_derive_af_bad_denominator_fail_closed(tmp_path):
    con = _lake(tmp_path)
    with pytest.raises(emb.EmBatchError, match="分母非正"):
        emb.derive_af_batch(con, D, [
            {"ts_code": "sh.600001", "ex_date": D, "cash": 30.0, "bonus": 0.0,
             "has_allot": False},   # cash > c0 → 分母负
        ])
    con.close()


# ---------------------------------------------------------------------------
# snapshot_to_t2t3
# ---------------------------------------------------------------------------

def test_snapshot_to_t2t3_splits_suspended():
    snap = [
        {"ts_code": "sh.600519", "name": "x", "close": 10.0, "pct_chg": 1.0,
         "open": 9.9, "high": 10.5, "low": 9.8, "preclose": 9.9,
         "volume": 100000, "amount": 1.0e6, "turnover": 0.5,
         "total_mv_yi": 100.0, "float_mv_yi": 90.0,
         "list_date": "2001-08-27", "industry": "白酒", "pe_ttm": 20.0,
         "pb": 5.0},
        {"ts_code": "sz.000003", "name": "y", "close": None, "pct_chg": None,
         "open": None, "high": None, "low": None, "preclose": None,
         "volume": None, "amount": None, "turnover": None,
         "total_mv_yi": 50.0, "float_mv_yi": 40.0,
         "list_date": "1991-04-03", "industry": None, "pe_ttm": None,
         "pb": None},
    ]
    t2, t3 = emb.snapshot_to_t2t3(snap, D)
    assert [r["ts_code"] for r in t2] == ["sh.600519"]
    assert t2[0]["date"] == D and t2[0]["open"] == 9.9
    assert [r["ts_code"] for r in t3] == ["sh.600519", "sz.000003"]
    assert t3[0]["ttm_yield_pct"] is None   # 东财 clist 无 dv_ttm
