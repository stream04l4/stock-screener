# -*- coding: utf-8 -*-
"""T3 估值历史回填（v6.3.6，T3 选 A）回归。

覆盖（零网络——akshare 全 monkeypatch；_no_network autouse 双保险）：
1. code6 / fetch_valuation_baidu（fake akshare 注入）：3 指标 date 对齐排序、
   NaN 剔除、单指标失败 → RuntimeError、akshare 缺失 → RuntimeError。
2. merge_monthly_rows：3 指标按 date 并集对齐（缺值列=NULL）。
3. load_valuation_history：条件 upsert 只写/更新 bd_static 行——同日已有
   tencent 行时**不得覆盖**（v6.3.6 红线：当前快照与历史回填共存）；
   bd_static 重跑幂等覆盖。
4. derive_yield_for_rows：TTM DPS ÷ close 派生 ttm_yield_pct（%）；
   无分红/无 close 的锚点日 → NULL；只动 bd_static 行（tencent 行不受影响）。
5. t3-history checkpoint：save/load 幂等、损坏文件 → 空集。

纪律锚点：akshare 仅本低频回填路径（source=bd_static），不进每日主链路
（日更当前快照仍走 tencent T3）。
"""
from __future__ import annotations

import json
import os
import sys
import types

import duckdb
import pytest

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")

from lake.ingest import baidu_valuation_adapter as bdv  # noqa: E402


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    import socket
    monkeypatch.setattr(socket.socket, "connect",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("network disabled in test")))


@pytest.fixture()
def con():
    from lake.ddl import init_schema
    c = duckdb.connect(":memory:")
    init_schema(c)
    yield c
    c.close()


# ---------------------------------------------------------------------------
# fake akshare（百度估值：{indicator: [(date, value), ...]}）
# ---------------------------------------------------------------------------
class _FakeDF:
    def __init__(self, rows, cols=("date", "value")):
        self._rows = rows
        self.columns = cols

    def __len__(self):
        return len(self._rows)

    def iterrows(self):
        for i, r in enumerate(self._rows):
            yield i, dict(zip(self.columns, r))


def _fake_ak(pe, pb, mv, fail_indicator=None):
    data = {"市盈率(TTM)": pe, "市净率": pb, "总市值": mv}

    mod = types.ModuleType("akshare")
    mod.__version__ = "1.18.88"

    def _call(symbol, period, indicator):
        if indicator == fail_indicator:
            raise RuntimeError(f"boom {indicator}")
        return _FakeDF(data[indicator])

    mod.stock_zh_valuation_baidu = _call
    return mod


_PE = [("2025-09-30", 10.0), ("2025-10-31", 11.0)]
_PB = [("2025-09-30", 1.5), ("2025-10-31", 1.6)]
_MV = [("2025-09-30", 200.0), ("2025-10-31", 220.0)]


def _mk_hist():
    return {"pe_ttm": [(d, float(v)) for d, v in _PE],
            "pb": [(d, float(v)) for d, v in _PB],
            "total_mv": [(d, float(v)) for d, v in _MV]}


# ---------------------------------------------------------------------------
# code6 / fetch
# ---------------------------------------------------------------------------
def test_code6():
    assert bdv.code6("sh.600000") == "600000"
    assert bdv.code6("sz.000001") == "000001"
    assert bdv.code6("600000") == "600000"


def test_fetch_valuation_baidu_normal():
    hist = bdv.fetch_valuation_baidu("600000", sleep=0,
                                     _ak=_fake_ak(_PE, _PB, _MV))
    assert hist["pe_ttm"] == [("2025-09-30", 10.0), ("2025-10-31", 11.0)]
    assert hist["pb"] == [("2025-09-30", 1.5), ("2025-10-31", 1.6)]
    assert hist["total_mv"] == [("2025-09-30", 200.0), ("2025-10-31", 220.0)]


def test_fetch_valuation_baidu_nan_and_unsorted():
    import math
    pe = [("2025-10-31", 11.0), ("2025-09-30", float("nan")), ("2025-08-29", 9.5)]
    hist = bdv.fetch_valuation_baidu("600000", sleep=0,
                                     _ak=_fake_ak(pe, _PB, _MV))
    assert hist["pe_ttm"] == [("2025-08-29", 9.5), ("2025-10-31", 11.0)], \
        "NaN 行剔除 + date 升序"


def test_fetch_valuation_baidu_indicator_fail():
    with pytest.raises(RuntimeError, match="boom"):
        bdv.fetch_valuation_baidu("600000", sleep=0,
                                  _ak=_fake_ak(_PE, _PB, _MV, fail_indicator="市净率"))


def test_fetch_valuation_baidu_no_akshare(monkeypatch):
    monkeypatch.setattr(bdv, "baidu_available", lambda: False)
    with pytest.raises(RuntimeError, match="akshare"):
        bdv.fetch_valuation_baidu("600000", sleep=0)


# ---------------------------------------------------------------------------
# merge_monthly_rows
# ---------------------------------------------------------------------------
def test_merge_monthly_rows_union_and_null():
    hist = _mk_hist()
    hist["pb"] = hist["pb"][:1]  # pb 缺 10 月
    rows = bdv.merge_monthly_rows("sh.600000", hist)
    assert [r["date"] for r in rows] == ["2025-09-30", "2025-10-31"], "date 并集"
    assert rows[0]["pb"] == 1.5 and rows[1]["pb"] is None, "缺失指标 → NULL"
    assert all(r["source"] == "bd_static" and r["ts_code"] == "sh.600000" for r in rows)


def test_merge_monthly_rows_empty():
    assert bdv.merge_monthly_rows("sh.600000", {"pe_ttm": [], "pb": [], "total_mv": []}) == []


# ---------------------------------------------------------------------------
# load_valuation_history（条件 upsert 红线）
# ---------------------------------------------------------------------------
def test_load_history_insert(con):
    n = bdv.load_valuation_history(con, "sh.600000",
                                   bdv.merge_monthly_rows("sh.600000", _mk_hist()))
    assert n == 2
    row = con.execute(
        "SELECT date, pe_ttm, pb, total_mv, source, ttm_yield_pct "
        "FROM valuation_daily WHERE ts_code='sh.600000' AND date='2025-10-31'"
    ).fetchone()
    assert row[0] is not None and str(row[0]) == "2025-10-31"
    assert (row[1], row[2], row[3], row[4]) == (11.0, 1.6, 220.0, "bd_static")
    assert row[5] is None, "ttm_yield_pct 由派生函数统一写，load 阶段 NULL"


def test_load_history_does_not_overwrite_tencent_snapshot(con):
    """v6.3.6 红线（PK ts_code+date，同日仅一行）：同日已有 tencent 快照 →
    回填 ON CONFLICT WHERE source='bd_static' 不匹配 → **no-op 保 tencent**
    （当前数据优先，同 T9 只补缺口语义）；无冲突日正常插 bd_static 行。"""
    from lake.ingest.tencent_ingest import load_t3
    load_t3(con, "sh.600000", {"total_mv_yi": 999.0, "pe_ttm": 8.8, "pb": 1.1,
                               "turnover": 0.5, "ttm_yield_pct": 4.4},
            "2025-10-31")
    n = bdv.load_valuation_history(con, "sh.600000",
                                   bdv.merge_monthly_rows("sh.600000", _mk_hist()))
    assert n == 1, "10-31 被 tencent 占 → no-op；仅 09-30 插入（缺口语义）"
    rows = con.execute(
        "SELECT source, pe_ttm, total_mv FROM valuation_daily "
        "WHERE ts_code='sh.600000' ORDER BY date"
    ).fetchall()
    assert ("tencent", 8.8, 999.0) in rows, "tencent 快照行必须原样保留"
    assert ("bd_static", 10.0, 200.0) in rows, "无冲突日 09-30 正常回填"
    assert not any(s == "bd_static" and d == "2025-10-31"
                   for d, s, _, _ in con.execute(
                       "SELECT date, source, pe_ttm, total_mv FROM valuation_daily").fetchall()), \
        "10-31 不得出现 bd_static 行（tencent 优先）"


def test_load_history_idempotent_over_bd_static(con):
    bdv.load_valuation_history(con, "sh.600000",
                               bdv.merge_monthly_rows("sh.600000", _mk_hist()))
    hist2 = _mk_hist()
    hist2["pe_ttm"] = [("2025-09-30", 9.0), ("2025-10-31", 10.5)]
    bdv.load_valuation_history(con, "sh.600000",
                               bdv.merge_monthly_rows("sh.600000", hist2))
    v = con.execute("SELECT pe_ttm FROM valuation_daily "
                    "WHERE ts_code='sh.600000' AND date='2025-09-30' "
                    "AND source='bd_static'").fetchone()[0]
    assert v == 9.0, "重跑覆盖旧 bd_static 值（幂等）"
    cnt = con.execute("SELECT COUNT(*) FROM valuation_daily "
                      "WHERE ts_code='sh.600000' AND source='bd_static'").fetchone()[0]
    assert cnt == 2, "不重复插行"


# ---------------------------------------------------------------------------
# derive_yield_for_rows
# ---------------------------------------------------------------------------
def _seed_dividends(con, ts_code, rows):
    con.executemany("INSERT INTO dividend_events (ts_code, ex_date, cash_dps) VALUES (?,?,?)",
                    [(ts_code, d, v) for d, v in rows])


def test_derive_yield_basic(con):
    bdv.load_valuation_history(con, "sh.600000",
                               bdv.merge_monthly_rows("sh.600000", _mk_hist()))
    con.execute("INSERT INTO kline_daily (ts_code, date, close) VALUES "
                "('sh.600000','2025-09-30',20.0),('sh.600000','2025-10-31',22.0)")
    # 2025-10-31 的 TTM 窗口（> 2024-10-31 ~ 2025-10-31）含两笔分红
    _seed_dividends(con, "sh.600000", [("2025-06-10", 0.5), ("2025-09-20", 0.3)])
    upd = bdv.derive_yield_for_rows(con)
    assert upd == 2
    y10 = con.execute("SELECT ttm_yield_pct FROM valuation_daily "
                      "WHERE ts_code='sh.600000' AND date='2025-10-31' "
                      "AND source='bd_static'").fetchone()[0]
    assert abs(y10 - (0.8 / 22.0) * 100) < 1e-9, "TTM DPS 0.8 ÷ close 22"


def test_derive_yield_no_dividend_stays_null(con):
    """TTM 无分红 / 无 close → 派生式 NULL → 行保持 NULL（不写脏值）。"""
    bdv.load_valuation_history(con, "sh.600000",
                               bdv.merge_monthly_rows("sh.600000", _mk_hist()))
    # 只给 09-30 close（10-31 停牌 → 派生 NULL）；无分红 → 09-30 也 NULL
    con.execute("INSERT INTO kline_daily (ts_code, date, close) VALUES "
                "('sh.600000','2025-09-30',20.0)")
    bdv.derive_yield_for_rows(con)
    vals = con.execute("SELECT ttm_yield_pct FROM valuation_daily "
                       "WHERE source='bd_static'").fetchall()
    assert vals == [(None,), (None,)], "无分红/无 close 的锚点日保持 NULL"


def test_derive_yield_tencent_rows_unaffected(con):
    """只动 bd_static 行——tencent 快照行的 ttm_yield_pct（idx64 交叉值）不被覆盖。

    PK ts_code+date：10-31 被 tencent 占 → bd 该日 no-op；bd 实际行只有 09-30。
    """
    from lake.ingest.tencent_ingest import load_t3
    load_t3(con, "sh.600000", {"total_mv_yi": 999.0, "pe_ttm": 8.8, "pb": 1.1,
                               "turnover": 0.5, "ttm_yield_pct": 4.4}, "2025-10-31")
    bdv.load_valuation_history(con, "sh.600000",
                               bdv.merge_monthly_rows("sh.600000", _mk_hist()))
    con.execute("INSERT INTO kline_daily (ts_code, date, close) VALUES "
                "('sh.600000','2025-09-30',20.0)")
    _seed_dividends(con, "sh.600000", [("2025-06-10", 0.5)])
    bdv.derive_yield_for_rows(con)
    tenc = con.execute("SELECT ttm_yield_pct FROM valuation_daily "
                       "WHERE source='tencent' AND date='2025-10-31'").fetchone()[0]
    assert tenc == 4.4, "tencent 行保持 idx64 原值"
    bd = con.execute("SELECT ttm_yield_pct FROM valuation_daily "
                     "WHERE source='bd_static' AND date='2025-09-30'").fetchone()[0]
    assert abs(bd - (0.5 / 20.0) * 100) < 1e-9, "bd 09-30 派生 0.5÷20"


# ---------------------------------------------------------------------------
# checkpoint 工具
# ---------------------------------------------------------------------------
def test_checkpoint_roundtrip_and_corrupt(tmp_path):
    from lake_backfill import _t3_history_load_ckpt, _t3_history_save_ckpt
    p = str(tmp_path / "t3_history_checkpoint.json")
    _t3_history_save_ckpt(p, {"sh.600000", "sz.000001"})
    assert _t3_history_load_ckpt(p) == {"sh.600000", "sz.000001"}
    _t3_history_save_ckpt(p, {"sh.600000", "sz.000001", "sz.000002"})
    assert len(_t3_history_load_ckpt(p)) == 3
    with open(p, "w") as f:
        f.write("{corrupt")
    assert _t3_history_load_ckpt(p) == set(), "损坏文件 → 空集（全量重跑安全）"
    assert _t3_history_load_ckpt(str(tmp_path / "missing.json")) == set()


def test_t3_history_subcommand_registered():
    """t3-history 在 argparse 注册 + 持写锁路径（write_cmd 清单）。"""
    import lake_backfill
    import re
    src = open(lake_backfill.__file__, encoding="utf-8").read()
    assert '"t3-history"' in re.search(r'write_cmd = args\.cmd in \(([^)]+)\)', src).group(1)
    p = lake_backfill.build_parser()
    args = p.parse_args(["t3-history", "--limit", "5"])
    assert args.cmd == "t3-history" and args.limit == 5 and args.func is lake_backfill.cmd_t3_history
