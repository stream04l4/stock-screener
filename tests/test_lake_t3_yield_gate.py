# -*- coding: utf-8 -*-
"""test_lake_t3_yield_gate —— T3 息率质量门（v8.2.0 补，2026-11-17 池归零实锤）。

纪律：全离线——tmp duckdb 库 + 无网络。

背景：EM 批量源（clist）无 dv_ttm → 该通道日 T3 息率全 NULL → G8
fail-closed → 候选池归零。质量门：当日 NULL 率 >50% → ① 前 ≤10 日最近
有值日价格调整结转（商业口径一致）② 严格 TTM 只填 NULL 兜底 ③ 停牌/真
无息保持 NULL。

覆盖：
- 结转：前日商业值 × 前日 close ÷ 当日 close；
- 兜底：前 10 日无商业值 → 严格 TTM（dividend_events 365 天窗口）；
- 无息/停牌：保持 NULL（G8 fail-closed 语义不变）；
- 触发条件：NULL 率 ≤50% 不触发；>50% 触发；当日无行 → 空操作；
- derive_yield_for_date 只填 NULL（不覆盖已有值——46 行实损实锤）；
- 结转窗口：>10 日前的值不结转。
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("duckdb")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import duckdb  # noqa: E402

from lake.ingest.baidu_valuation_adapter import (  # noqa: E402
    derive_yield_for_date, derive_yield_for_rows)
from scripts.lake_backfill import _t3_yield_gate  # noqa: E402

D = "2026-11-17"
D_PREV = "2026-11-16"


@pytest.fixture()
def lake(tmp_path):
    """三表最小 schema（只含质量门/派生 SQL 触达的列）。"""
    p = str(tmp_path / "t3gate.duckdb")
    con = duckdb.connect(p)
    con.execute("""
        CREATE TABLE valuation_daily (
            ts_code VARCHAR, date DATE, ttm_yield_pct DOUBLE,
            source VARCHAR)""")
    con.execute("""
        CREATE TABLE kline_daily (
            ts_code VARCHAR, date DATE, close DOUBLE)""")
    con.execute("""
        CREATE TABLE dividend_events (
            ts_code VARCHAR, ex_date DATE, cash_dps DOUBLE)""")
    yield con
    con.close()


def _ins(con, t3=None, k2=None, div=None):
    for c, d, y, s in t3 or []:
        con.execute("INSERT INTO valuation_daily VALUES (?,?,?,?)", [c, d, y, s])
    for c, d, x in k2 or []:
        con.execute("INSERT INTO kline_daily VALUES (?,?,?)", [c, d, x])
    for c, d, v in div or []:
        con.execute("INSERT INTO dividend_events VALUES (?,?,?)", [c, d, v])


def test_gate_carryforward_then_strict_fallback(lake):
    # c1：前日有商业值 → 结转（4.0% × 10/10.2）
    # c2：前 10 日无商业值，有 TTM 分红 → 严格派生（0.5/20×100=2.5）
    # c3：无息 → 保持 NULL
    _ins(lake,
         t3=[("c1", D_PREV, 4.0, "em_batch"),
             ("c1", D, None, "em_batch"),
             ("c2", D, None, "em_batch"),
             ("c3", D, None, "em_batch")],
         k2=[("c1", D_PREV, 10.0), ("c1", D, 10.2),
             ("c2", D, 20.0), ("c3", D, 30.0)],
         div=[("c2", "2026-05-01", 0.5)])
    res = _t3_yield_gate(lake, D)
    got = dict(lake.execute(
        "SELECT ts_code, ttm_yield_pct FROM valuation_daily "
        "WHERE date=? ORDER BY ts_code", [D]).fetchall())
    assert abs(got["c1"] - 4.0 * 10.0 / 10.2) < 1e-9
    assert abs(got["c2"] - 2.5) < 1e-9
    assert got["c3"] is None
    assert res["null_before"] == 3 and res["null_after"] == 1


def test_gate_no_trigger_when_null_rate_low(lake):
    # 4 行 2 NULL = 50%（不 >50%）→ 不触发，NULL 行保持
    _ins(lake,
         t3=[("c1", D, 4.0, "em_batch"), ("c2", D, None, "em_batch"),
             ("c3", D, 3.0, "em_batch"), ("c4", D, None, "em_batch")],
         k2=[("c2", D, 10.0), ("c4", D, 10.0)])
    res = _t3_yield_gate(lake, D)
    assert res["ok"] is True
    got = dict(lake.execute(
        "SELECT ts_code, ttm_yield_pct FROM valuation_daily "
        "WHERE date=? AND ts_code IN ('c2','c4')", [D]).fetchall())
    assert got["c2"] is None and got["c4"] is None


def test_gate_no_rows_noop(lake):
    res = _t3_yield_gate(lake, D)
    assert res["rows"] == 0 and res["ok"] is True


def test_gate_suspended_no_close_stays_null(lake):
    # c1：前日有值，但当日无 close（停牌）→ 不结转保持 NULL
    _ins(lake,
         t3=[("c1", D_PREV, 4.0, "em_batch"), ("c1", D, None, "em_batch")],
         k2=[("c1", D_PREV, 10.0)])
    _t3_yield_gate(lake, D)
    got = lake.execute(
        "SELECT ttm_yield_pct FROM valuation_daily WHERE ts_code='c1' AND date=?",
        [D]).fetchone()[0]
    assert got is None


def test_gate_carryforward_window_10d(lake):
    # 最近有值日 = D-12（超出 10 日窗口）且无分红 → 不结转保持 NULL
    _ins(lake,
         t3=[("c1", "2026-11-05", 4.0, "em_batch"), ("c1", D, None, "em_batch")],
         k2=[("c1", "2026-11-05", 10.0), ("c1", D, 10.2)])
    _t3_yield_gate(lake, D)
    got = lake.execute(
        "SELECT ttm_yield_pct FROM valuation_daily WHERE ts_code='c1' AND date=?",
        [D]).fetchone()[0]
    assert got is None


def test_gate_strict_window_excludes_old_dividend(lake):
    # 分红 ex_date 在 365 天窗口外 → 严格 TTM=NULL（不兜底）
    _ins(lake,
         t3=[("c1", D, None, "em_batch")],
         k2=[("c1", D, 10.0)],
         div=[("c1", "2024-11-01", 0.5)])
    _t3_yield_gate(lake, D)
    got = lake.execute(
        "SELECT ttm_yield_pct FROM valuation_daily WHERE ts_code='c1' AND date=?",
        [D]).fetchone()[0]
    assert got is None


def test_derive_yield_for_date_null_only(lake):
    # 已有值（商业口径）不被严格 TTM 覆盖（46 行实损实锤回归）
    _ins(lake,
         t3=[("c1", D, 4.5, "em_batch"), ("c2", D, None, "em_batch")],
         k2=[("c1", D, 10.0), ("c2", D, 10.0)],
         div=[("c2", "2026-05-01", 0.3)])
    n = derive_yield_for_date(lake, D)
    got = dict(lake.execute(
        "SELECT ts_code, ttm_yield_pct FROM valuation_daily WHERE date=?",
        [D]).fetchall())
    assert abs(got["c1"] - 4.5) < 1e-12        # 不覆盖
    assert abs(got["c2"] - 3.0) < 1e-9          # 只填 NULL
    assert n == 1


def test_derive_yield_for_rows_unchanged(lake):
    # bd_static 路径语义不变：按 source 圈定，可覆盖同值重算
    _ins(lake,
         t3=[("c1", D, 9.9, "bd_static"), ("c2", D, None, "bd_static"),
             ("c3", D, None, "em_batch")],
         k2=[("c1", D, 10.0), ("c2", D, 10.0), ("c3", D, 10.0)],
         div=[("c2", "2026-05-01", 0.3), ("c3", "2026-05-01", 0.3)])
    n = derive_yield_for_rows(lake)
    got = dict(lake.execute(
        "SELECT ts_code, ttm_yield_pct FROM valuation_daily WHERE date=?",
        [D]).fetchall())
    # c1 无分红事件 → 派生 NULL（覆盖 9.9，bd_static 路径原语义）
    assert got["c1"] is None
    assert abs(got["c2"] - 3.0) < 1e-9
    assert got["c3"] is None
    # 返回=前后非 NULL 计数差：c1 9.9→NULL、c2 NULL→3.0，计数不变 → 0
    assert n == 0
