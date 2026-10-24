# -*- coding: utf-8 -*-
"""test_selection —— v7.0.12：候选池选股（十年回测为正 + 股息率 + 价格区间 ∪ 手动添加）。

契约（全离线，零真实湖/零网络）：
- G7 fail-closed：无预计算行 / 从未触发 / 净利≤0 → 不入选；
- G8 股息率边界（< 阈值淘汰）；G9 价格区间边界；backtest_positive=False 关 G7；
- 手动添加绕过 G7~G9、退市股剔除、auto∩manual 去重保留自动版+标记；
- refresh_selection 写预计算表（monkeypatch 回测引擎，只测簿记与窗口）；
- 手动池文件 roundtrip（原子写/幂等/非法代码/退市拒绝）；
- Web POST/DELETE /api/pool/manual（端点直调，fake 湖）。
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Optional

import duckdb
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import web.app as wapp  # noqa: E402

from martingale.config import UniverseParams  # noqa: E402
from martingale import selection as sel  # noqa: E402
from martingale.universe import Member, PoolResult  # noqa: E402


def make_up(**kw) -> UniverseParams:
    base = dict(
        industry_whitelist_csric2=("B06",),
        soe_keywords=("国资",),
        min_total_mv_yi=200.0,
        min_consecutive_div_years=5,
        listing_min_trading_days=250,
        backtest_positive=True,
        backtest_window_years=10,
        min_ttm_yield_pct=3.0,
        price_min_yuan=3.0,
        price_max_yuan=100.0,
    )
    base.update(kw)
    return UniverseParams(**base)


def member(code, yield_pct=4.0, **kw) -> Member:
    d = dict(name=code, industry_csric2="B06", soe_basis="关键词命中",
             total_mv=300.0, ttm_yield_pct=yield_pct, n_bars=2400,
             list_date="2010-01-01")
    d.update(kw)
    return Member(ts_code=code, **d)


def base_pool(codes_yields: List[tuple]) -> PoolResult:
    members = [member(c, y) for c, y in codes_yields]
    return PoolResult(
        funnel={"total": 100, "whitelist": 40, "soe": 30, "mv": 20,
                "listing": 12, "div": len(members), "final": len(members)},
        members=members)


def make_lake(bt_rows: List[tuple], prices: Dict[str, float],
              manual_master: Optional[List[tuple]] = None) -> duckdb.DuckDBPyConnection:
    """内存湖：kline_daily（最新价）+ selection 表 +（可选）stock_master/估值。"""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE kline_daily (ts_code VARCHAR, date DATE, close DOUBLE)")
    con.executemany(
        "INSERT INTO kline_daily VALUES (?, '2026-10-23', ?)",
        [(c, p) for c, p in prices.items()])
    if bt_rows is not None:
        con.execute(sel._SCHEMA)
        if bt_rows:
            con.executemany(
                f"INSERT INTO {sel.TABLE} VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                bt_rows)
    if manual_master:
        con.execute("""CREATE TABLE stock_master (ts_code VARCHAR, name VARCHAR,
                       industry_csric2 VARCHAR, list_date DATE, delist_date DATE,
                       board VARCHAR, is_st INTEGER, st_since DATE,
                       soe_flag INTEGER, soe_basis VARCHAR, source VARCHAR,
                       fetched_at TIMESTAMP, data_version VARCHAR,
                       conflict_src VARCHAR)""")
        con.executemany(
            "INSERT INTO stock_master VALUES (?, ?, 'B06', DATE '2010-01-01', NULL, 'main', 0, NULL, 1, '手动', 't', CURRENT_TIMESTAMP, 'v', NULL)",
            [(c, n) for c, n in manual_master])
        con.execute("""CREATE TABLE valuation_daily (ts_code VARCHAR, date DATE,
                       total_mv DOUBLE, float_mv DOUBLE, pe_ttm DOUBLE, pb DOUBLE,
                       turnover_pct DOUBLE, ttm_yield_pct DOUBLE, source VARCHAR,
                       fetched_at TIMESTAMP, data_version VARCHAR,
                       conflict_src VARCHAR)""")
        con.executemany(
            "INSERT INTO valuation_daily VALUES (?, '2026-10-23', 300, NULL, NULL, NULL, NULL, ?, 't', CURRENT_TIMESTAMP, 'v', NULL)",
            [(c, 4.0) for c, _n in manual_master])
    return con


def bt_row(code, n_buys, total_pnl, pnl_pct) -> tuple:
    return (code, "2026-10-23", "2016-10-24", "2026-10-23", 2400, n_buys,
            100000.0, 150000.0, 20000.0, total_pnl, pnl_pct, -0.1, "open")


# ---------------------------------------------------------------------------
# 1) 门槛语义
# ---------------------------------------------------------------------------
def test_gates_pass_fail_boundaries():
    up = make_up()
    base = base_pool([
        ("a.1", 4.0),   # 全过
        ("b.2", 4.0),   # G7 失败：从未触发
        ("c.3", 4.0),   # G7 失败：净利为负
        ("d.4", 2.99),  # G8 失败：股息率 2.99 < 3
        ("e.5", 4.0),   # G9 失败：价格 2.5 < 3
        ("f.6", 4.0),   # G9 失败：价格 150 > 100
        ("g.7", 4.0),   # G7 fail-closed：无预计算行
    ])
    con = make_lake(
        bt_rows=[bt_row("a.1", 5, 12000.0, 0.12),
                 bt_row("b.2", 0, 0.0, None),
                 bt_row("c.3", 7, -3000.0, -0.03)],
        prices={"a.1": 10.0, "b.2": 10.0, "c.3": 10.0, "d.4": 10.0,
                "e.5": 2.5, "f.6": 150.0, "g.7": 10.0})
    pool = sel.build_candidate_pool(con, up, base=base)
    assert [m.ts_code for m in pool.members] == ["a.1"]
    fu = pool.funnel
    assert (fu["backtest"], fu["yield"], fu["price"], fu["final"], fu["manual"]) == (1, 1, 1, 1, 0)
    m = pool.members[0]
    assert m.bt_pnl_pct == 0.12 and m.bt_n_buys == 5 and m.price_latest == 10.0
    con.close()


def test_backtest_gate_disabled():
    up = make_up(backtest_positive=False)
    base = base_pool([("a.1", 4.0), ("b.2", 4.0)])
    con = make_lake(bt_rows=None, prices={"a.1": 10.0, "b.2": 10.0})
    pool = sel.build_candidate_pool(con, up, base=base)
    assert [m.ts_code for m in pool.members] == ["a.1", "b.2"]  # G7 关闭
    con.close()


def test_yield_exact_boundary_passes():
    up = make_up(min_ttm_yield_pct=3.0)
    base = base_pool([("a.1", 3.0)])
    con = make_lake(bt_rows=[bt_row("a.1", 2, 1000.0, 0.01)],
                    prices={"a.1": 10.0})
    pool = sel.build_candidate_pool(con, up, base=base)
    assert [m.ts_code for m in pool.members] == ["a.1"]  # 3.0 >= 3.0 过
    con.close()


# ---------------------------------------------------------------------------
# 2) 手动添加
# ---------------------------------------------------------------------------
def test_manual_bypasses_gates_and_dedupes():
    up = make_up()
    base = base_pool([("a.1", 4.0)])  # 自动入选
    manual = {"m.1": {"name": "手动股", "added_at": "2026-10-24T12:00:00"},
              "a.1": {"name": "A", "added_at": "2026-10-24T12:00:00"}}  # 双料
    con = make_lake(
        bt_rows=[bt_row("a.1", 5, 12000.0, 0.12),
                 bt_row("m.1", 1, -500.0, -0.005)],  # m.1 回测为负但手动→仍入选
        prices={"a.1": 10.0, "m.1": 50.0},
        manual_master=[("m.1", "手动股"), ("a.1", "A")])
    pool = sel.build_candidate_pool(con, up, manual=manual, base=base)
    codes = [m.ts_code for m in pool.members]
    assert set(codes) == {"a.1", "m.1"}
    by = {m.ts_code: m for m in pool.members}
    assert by["a.1"].is_manual is True          # 双料 → 保留自动版+手动标记
    assert by["a.1"].bt_pnl_pct == 0.12
    assert by["m.1"].is_manual is True
    assert by["m.1"].bt_pnl_pct == -0.005       # 手动股回测值为负也照实展示
    assert pool.funnel["manual"] == 2
    con.close()


def test_manual_delisted_excluded():
    up = make_up()
    con2 = duckdb.connect(":memory:")
    con2.execute("CREATE TABLE kline_daily (ts_code VARCHAR, date DATE, close DOUBLE)")
    con2.execute("""CREATE TABLE stock_master (ts_code VARCHAR, name VARCHAR,
                   industry_csric2 VARCHAR, list_date DATE, delist_date DATE,
                   board VARCHAR, is_st INTEGER, st_since DATE,
                   soe_flag INTEGER, soe_basis VARCHAR, source VARCHAR,
                   fetched_at TIMESTAMP, data_version VARCHAR,
                   conflict_src VARCHAR)""")
    con2.execute("INSERT INTO stock_master VALUES ('m.1','已退市','B06',NULL,DATE '2026-01-01','main',1,NULL,0,'','t',CURRENT_TIMESTAMP,'v',NULL)")
    # 手动股已退市 → 剔除（并顺手从传入的 manual dict 中清掉失效项）
    manual = {"m.1": {"name": "已退市", "added_at": "x"}}
    pool = sel.build_candidate_pool(con2, up, manual=manual,
                                    base=PoolResult(funnel={"final": 0}, members=[]))
    assert pool.members == []
    assert manual == {}, "失效手动项应被清理"
    con2.close()


# ---------------------------------------------------------------------------
# 3) 手动池文件 roundtrip
# ---------------------------------------------------------------------------
def test_manual_file_roundtrip(tmp_path):
    p = str(tmp_path / "manual_pool.json")
    assert sel.load_manual_pool(p) == {}

    con = duckdb.connect(":memory:")
    con.execute("""CREATE TABLE stock_master (ts_code VARCHAR, name VARCHAR,
                   delist_date DATE)""")
    con.execute("INSERT INTO stock_master VALUES ('sh.600000','浦发银行',NULL)")
    con.execute("INSERT INTO stock_master VALUES ('sz.000001','平安银行',DATE '2026-01-01')")

    # 6 位数字 → 自动补前缀
    e1 = sel.add_manual_code(con, "600000", path=p)
    assert e1["name"] == "浦发银行"
    # 幂等：再次添加返回原条目
    e2 = sel.add_manual_code(con, "SH.600000", path=p)
    assert e2 == e1
    stocks = sel.load_manual_pool(p)
    assert set(stocks) == {"sh.600000"}
    # 原子写格式
    raw = json.loads(Path(p).read_text(encoding="utf-8"))
    assert "updated_at" in raw and "stocks" in raw

    # 退市股拒绝
    with pytest.raises(ValueError, match="已退市"):
        sel.add_manual_code(con, "sz.000001", path=p)
    # 不存在的代码拒绝
    with pytest.raises(ValueError, match="未找到"):
        sel.add_manual_code(con, "600999", path=p)
    # 非法格式拒绝
    with pytest.raises(ValueError, match="非法"):
        sel.add_manual_code(con, "abcdef", path=p)

    assert sel.remove_manual_code("600000", path=p) is True
    assert sel.remove_manual_code("600000", path=p) is False  # 幂等
    assert sel.load_manual_pool(p) == {}
    con.close()


def test_normalize_code_variants():
    assert sel.normalize_ts_code("600000") == "sh.600000"
    assert sel.normalize_ts_code("000001") == "sz.000001"
    assert sel.normalize_ts_code("300750") == "sz.300750"
    assert sel.normalize_ts_code("SH.600000") == "sh.600000"
    assert sel.normalize_ts_code("sh600000") == "sh.600000"
    assert sel.normalize_ts_code(" bj.920001 ") == "bj.920001"
    with pytest.raises(ValueError):
        sel.normalize_ts_code("12345")


# ---------------------------------------------------------------------------
# 4) refresh_selection 簿记（fake 回测引擎）
# ---------------------------------------------------------------------------
def test_refresh_selection_writes_table(monkeypatch, tmp_path):
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE kline_daily (ts_code VARCHAR, date DATE, close DOUBLE)")
    for d in ("2016-10-24", "2026-10-23"):
        con.execute("INSERT INTO kline_daily VALUES ('a.1', ?, 10.0)", [d])

    import numpy as np
    dates = np.array(["2016-10-24", "2026-10-23"], dtype="datetime64[ns]")
    stats = {
        "a.1": dict(n_bars=2, n_buys=3, total_invested=110000.0,
                    final_assets=130000.0, dividends=10000.0, total_pnl=20000.0,
                    pnl_pct=0.18, min_unreal_pct=-0.12, last_status="open"),
    }
    monkeypatch.setattr(sel, "load", lambda con, codes=None, start=None, end=None:
                        ({}, {"a.1": (dates, np.array([1.0, 2.0]), np.array([1.0, 2.0]))}, {}))
    monkeypatch.setattr(sel, "run_stock",
                        lambda c, d, s, e, dv, sp, verbose=False:
                        (stats[c], {}, np.array([1.0, 1.0]), np.array([1.0, 1.0]),
                         np.array([1, 1]), []))

    up = make_up()
    out = sel.refresh_selection(con, sp=None, up=up, base_codes=["a.1"])
    assert out["n_written"] == 1 and out["n_positive"] == 1
    assert out["as_of"] == "2026-10-23"
    assert out["start"] == "2016-10-23"   # as_of 回溯恰好 10 年
    row = con.execute(f"SELECT * FROM {sel.TABLE}").fetchall()
    assert row[0][0] == "a.1" and row[0][5] == 3 and row[0][9] == 20000.0

    # 二次刷新 = 整表快照（行数不变，无累积）
    sel.refresh_selection(con, sp=None, up=up, base_codes=["a.1"])
    assert con.execute(f"SELECT COUNT(*) FROM {sel.TABLE}").fetchone()[0] == 1
    con.close()


# ---------------------------------------------------------------------------
# 5) Web 端点（直调；fake 湖 + tmp 手动池文件）
# ---------------------------------------------------------------------------
@pytest.fixture()
def web_fake(monkeypatch, tmp_path):
    # 临时文件湖：端点语义=每次调用新连接用完即关，内存单连接模拟不了
    lake = tmp_path / "lake.duckdb"
    con = duckdb.connect(str(lake))
    con.execute("""CREATE TABLE stock_master (ts_code VARCHAR, name VARCHAR,
                   delist_date DATE)""")
    con.execute("INSERT INTO stock_master VALUES ('sh.600000','浦发银行',NULL)")
    con.close()

    import duckdb as _duckdb
    import martingale.data as mdata

    monkeypatch.setattr(mdata, "connect_readonly",
                        lambda p=None: _duckdb.connect(str(lake), read_only=True))
    mp = str(tmp_path / "manual_pool.json")
    monkeypatch.setattr(sel, "default_manual_pool_path", lambda: mp)
    yield lake, mp


def test_web_add_and_remove_manual(web_fake):
    from fastapi import HTTPException

    out = wapp.add_pool_manual({"ts_code": "600000"})
    assert out["ok"] is True and out["entry"]["name"] == "浦发银行"
    # 幂等再添加
    out2 = wapp.add_pool_manual({"ts_code": "sh.600000"})
    assert out2["entry"] == out["entry"]

    with pytest.raises(HTTPException) as e:
        wapp.add_pool_manual({"ts_code": "600999"})
    assert e.value.status_code == 400

    d = wapp.remove_pool_manual({"ts_code": "600000"})
    assert d["ok"] is True
    d2 = wapp.remove_pool_manual({"ts_code": "600000"})
    assert d2["ok"] is False
    with pytest.raises(HTTPException) as e2:
        wapp.remove_pool_manual({"ts_code": "xxx"})
    assert e2.value.status_code == 400
