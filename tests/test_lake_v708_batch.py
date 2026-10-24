# -*- coding: utf-8 -*-
"""test_lake_v708_batch —— v7.0.8：tushare 全市场单日批量（每日增量主轴）。

背景：新浪逐只 K线+hfq + 腾讯逐只快照 ≈ 1.5 万次调用，sina 限速下每日增量
9-10h（Joel 2026-10-22 投诉不可受）→ tushare trade_date 全市场单次取回
（pro.daily / pro.adj_factor / pro.daily_basic，各 1 次/分钟，~2.5min）。

纪律（同 v614/v706 契约）：全离线——tmp 库 + tmp yaml + monkeypatch 路径；
tushare 批量函数 fake 化（零真实网络）；绝不触碰 data/lake/ 生产库。
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
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from lake import backfill as lb  # noqa: E402
from lake import conn as lconn  # noqa: E402
from lake.ingest.tencent_ingest import INDEX_CODES  # noqa: E402

import lake_backfill as drv  # noqa: E402

FIXED_TODAY = "2026-09-18"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """路径全隔离（同 v706 契约）+ batch_min_codes=1（1 只即触发批量）。"""
    import lake.config as lconfig

    monkeypatch.setattr(drv, "_today_beijing", lambda: FIXED_TODAY)
    monkeypatch.setattr(drv.time, "sleep", lambda s: None)
    monkeypatch.setattr(lb.BackfillRunner, "_quota_state", lambda self: (False, 0))
    yaml_path = str(tmp_path / "strategy.yaml")
    with open(yaml_path, "w", encoding="utf-8") as f:
        f.write("lake:\n"
                "  sina_enabled: true\n"
                "  tencent_enabled: true\n"
                "  baostock_probe_enabled: true\n"
                "  tdx_enabled: true\n"
                "  adata_f10_enabled: true\n"
                "  batch_min_codes: 1\n"
                "  batch_min_rows: 1\n")
    monkeypatch.setattr(lconfig, "strategy_yaml_path", lambda: yaml_path)
    monkeypatch.setattr(lb, "_progress_path",
                        lambda: str(tmp_path / "prog" / "backfill_progress.json"))
    monkeypatch.setattr(lconn, "progress_path",
                        lambda: str(tmp_path / "prog" / "backfill_progress.json"))
    yield


# ---------------------------------------------------------------------------
def _td(n: int = 500) -> list:
    """最近 n 个工作日（升序），止于给定日（跳周末）。"""
    out, d = [], dt.date(2026, 9, 18)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= dt.timedelta(days=1)
    return list(reversed(out))


def _seed(db_path: str, codes, last_date: str = "2026-09-17") -> None:
    """T1 播种 + T2 K线（止于 last_date → gap_start = last_date+1 工作日）。

    last_date 默认 09-17 → gap_start = 09-18 = FIXED_TODAY（单日缺口，批量判据命中）。
    """
    con = lconn.open(db_path)
    con.executemany(
        "INSERT INTO stock_master (ts_code,name,industry_csric2,board,is_st,"
        "source,fetched_at,data_version) VALUES (?,?,?,?,0,'t','2026-09-15 09:00:00','v6.1')",
        [(c, f"股{c}", "J66", "主板") for c in codes])
    dates = _td(500)
    # 只保留 <= last_date 的 bar（缺口 = FIXED_TODAY 单日）
    dates = [d for d in dates if d <= last_date]
    kl = [(c, d, 10.0 + 0.01 * (i % 7), 10.0 + 0.01 * (i % 7),
           10.0 + 0.01 * (i % 7), 10.0 + 0.01 * (i % 7), 1000, 10000.0, 1.0,
           't', '2026-09-15 09:00:00', 'v6.1')
          for c in codes for i, d in enumerate(dates)]
    con.executemany(
        'INSERT INTO kline_daily (ts_code,date,open,high,low,"close",volume,'
        "amount,adj_factor,is_st,source,fetched_at,data_version) "
        "VALUES (?,?,?,?,?,?,?,?,?,0,?,?,?)", kl)
    con.close()


def _fake_tencent(monkeypatch, snap):
    """fake 腾讯（零网络）：K线恒空（T7 指数不写库）；快照按 snap 返回。"""
    import lake.ingest.tencent_ingest as ti

    counter = {"kline": 0, "snapshot": 0}

    def fake_kline(client, ts_code, n):
        counter["kline"] += 1
        return []

    def fake_snapshot(client, ts_codes):
        counter["snapshot"] += 1
        return {c: dict(snap[c]) for c in ts_codes if c in snap}

    monkeypatch.setattr(ti, "fetch_kline_ohlcv", fake_kline)
    monkeypatch.setattr(ti, "fetch_snapshot", fake_snapshot)
    from lake.ingest import source_pool as sp
    ad = sp.get_adapter("tdx")
    if ad is not None:
        monkeypatch.setattr(ad, "available", lambda: False)
    return counter


def _fake_batch(monkeypatch, t2_rows=None, af_map=None, t3_rows=None,
                t2_exc=None):
    """fake tushare 批量三接口（计数；t2_exc → 批量失败路径）。"""
    import lake.ingest.tushare_ingest as ti

    counter = {"daily": 0, "af": 0, "basic": 0}

    def fake_daily(trade_date):
        counter["daily"] += 1
        if t2_exc:
            raise t2_exc
        return list(t2_rows or [])

    def fake_af(trade_date):
        counter["af"] += 1
        if t2_exc:
            raise t2_exc
        return dict(af_map or {})

    def fake_basic(trade_date):
        counter["basic"] += 1
        if t2_exc:
            raise t2_exc
        return list(t3_rows or [])

    monkeypatch.setattr(ti, "tushare_available", lambda: True)
    monkeypatch.setattr(ti, "fetch_daily_batch", fake_daily)
    monkeypatch.setattr(ti, "fetch_adj_factor_batch", fake_af)
    monkeypatch.setattr(ti, "fetch_daily_basic_batch", fake_basic)
    return counter


# ---------------------------------------------------------------------------
def test_batch_hit_writes_tushare_rows_zero_network(tmp_path, monkeypatch):
    """批量命中 → tushare 行零网络落库（股/元/亿元换算 + source=tushare + af 填充）。"""
    db = str(tmp_path / "b1.duckdb")
    codes = ["sh.600001"]
    _seed(db, codes)  # 缺口 = 09-18 单日
    con = lconn.open(db)
    snap = {c: {"total_mv_yi": 999.0, "float_mv_yi": 888.0, "pe_ttm": 99.0,
                "pb": 9.0, "turnover": 9.0} for c in codes}
    tc = _fake_tencent(monkeypatch, snap)
    t2_rows = [{"ts_code": "sh.600001", "date": "2026-09-18",
                "open": 10.1, "high": 10.3, "low": 10.0, "close": 10.25,
                "preclose": 10.0, "pct_chg": 2.5,
                "volume": 123456, "amount": 1000000.0}]   # 已换算：股/元
    t3_rows = [{"ts_code": "sh.600001", "date": "2026-09-18",
                "total_mv_yi": 100.0, "float_mv_yi": 80.0, "pe_ttm": 5.0,
                "pb": 0.8, "turnover": 1.0, "ttm_yield_pct": 3.2}]
    bs = _fake_batch(monkeypatch, t2_rows=t2_rows,
                     af_map={"sh.600001": {"2026-09-18": 1.5}},
                     t3_rows=t3_rows)
    r = drv.run_incremental(con, db, codes, days=3)

    # 批量三接口各恰 1 次；腾讯零调用（T2/T3 全命中缓存；T7 指数走 fake_kline）
    assert bs == {"daily": 1, "af": 1, "basic": 1}, bs
    assert tc["snapshot"] == 0, f"T3 应零腾讯调用: {tc}"
    assert r.get("batch") and r["batch"]["date"] == FIXED_TODAY
    assert r["batch"]["t2_codes"] == 1 and r["batch"]["t3_codes"] == 1

    # T2：tushare 行落库（换算值原样 + af 填充 + pct_chg/preclose 入列）
    row = con.execute(
        'SELECT "close", volume, amount, pct_chg, preclose, adj_factor, source '
        "FROM kline_daily WHERE ts_code='sh.600001' AND date='2026-09-18'"
    ).fetchone()
    assert row == (10.25, 123456, 1000000.0, 2.5, 10.0, 1.5, "tushare"), row
    # T3：tushare daily_basic 落库（date=运行日，单位亿元）
    vrow = con.execute(
        "SELECT total_mv, pe_ttm, pb, turnover_pct, ttm_yield_pct, source "
        "FROM valuation_daily WHERE ts_code='sh.600001' AND date='2026-09-18'"
    ).fetchone()
    assert vrow == (100.0, 5.0, 0.8, 1.0, 3.2, "tushare"), vrow
    assert r["t2"]["processed"] == 1 and r["t3"]["processed"] == 1
    con.close()


def test_batch_failure_falls_back_legacy(tmp_path, monkeypatch):
    """批量抛错 → stats 记痕 + 全量回退逐只 legacy（本例 fake 空窗口 → done 不写库）。"""
    db = str(tmp_path / "b2.duckdb")
    codes = ["sh.600001"]
    _seed(db, codes)
    con = lconn.open(db)
    snap = {c: {"total_mv_yi": 100.0, "float_mv_yi": 80.0, "pe_ttm": 5.0,
                "pb": 0.8, "turnover": 1.0} for c in codes}
    tc = _fake_tencent(monkeypatch, snap)
    bs = _fake_batch(monkeypatch, t2_exc=RuntimeError("频率超限 x8"))
    r = drv.run_incremental(con, db, codes, days=3)
    assert "batch" in r and "频率超限" in r["batch"]["failed"], r.get("batch")
    # legacy 逐只路径被走到（腾讯 K线 fake 被调）；fake 空窗口 → 不写新 K线
    assert tc["kline"] >= 1, f"应走 legacy 逐只: {tc}"
    assert con.execute(
        "SELECT COUNT(*) FROM kline_daily WHERE ts_code='sh.600001' "
        "AND date='2026-09-18'").fetchone()[0] == 0
    # T3 无批量 → 走腾讯快照（stale 值，语义同 v7.0.7 前）
    assert con.execute(
        "SELECT source FROM valuation_daily WHERE ts_code='sh.600001' "
        "AND date='2026-09-18'").fetchone()[0] != "tushare"
    assert r["t2"]["processed"] == 1 and r["t3"]["processed"] == 1
    con.close()


def test_batch_no_trade_day_marks_done_without_write(tmp_path, monkeypatch):
    """批量 K线空 + 指数无当日 bar → 非交易日：全缺口视为完成，不写库不逐只空转。"""
    db = str(tmp_path / "b3.duckdb")
    codes = ["sh.600001", "sh.600002"]
    _seed(db, codes)
    con = lconn.open(db)
    # 节假日：腾讯快照仍是"当前时点"旧值（非空）→ legacy T3 照写（既有行为）
    snap = {c: {"total_mv_yi": 100.0, "float_mv_yi": 80.0, "pe_ttm": 5.0,
                "pb": 0.8, "turnover": 1.0} for c in codes}
    tc = _fake_tencent(monkeypatch, snap)
    # 批量：daily 空；af 空；daily_basic 也空 → 但 no_trade 判定只看 daily+指数
    bs = _fake_batch(monkeypatch, t2_rows=[], af_map={}, t3_rows=[])
    r = drv.run_incremental(con, db, codes, days=3)
    assert r.get("batch", {}).get("no_trade") is True, r.get("batch")
    # 无新 K线（不写）；也不逐只 legacy（腾讯 K线只被 T7 指数调用，非股票）
    assert con.execute(
        "SELECT COUNT(*) FROM kline_daily WHERE date='2026-09-18'").fetchone()[0] == 0
    assert r["t2"]["processed"] == 2
    con.close()


def test_multi_day_gap_code_falls_back_legacy(tmp_path, monkeypatch):
    """多日缺口股（停牌）不在批量单日窗口 → 逐只 legacy 补整窗（批量只补单日股）。"""
    db = str(tmp_path / "b4.duckdb")
    fresh, halted = ["sh.600001"], ["sh.600002"]
    _seed(db, fresh + halted)
    con = lconn.open(db)
    # 把 sh.600002 的 K线抹到 09-15 前（gap_start=09-16，多日缺口 ≠ 09-18）
    con.execute("DELETE FROM kline_daily WHERE ts_code='sh.600002' "
                "AND date >= '2026-09-16'")
    con.commit()
    snap = {c: {"total_mv_yi": 100.0, "float_mv_yi": 80.0, "pe_ttm": 5.0,
                "pb": 0.8, "turnover": 1.0} for c in fresh + halted}
    tc = _fake_tencent(monkeypatch, snap)
    t2_rows = [{"ts_code": "sh.600001", "date": "2026-09-18",
                "open": 10.1, "high": 10.3, "low": 10.0, "close": 10.25,
                "preclose": 10.0, "pct_chg": 2.5,
                "volume": 123456, "amount": 1000000.0}]
    bs = _fake_batch(monkeypatch, t2_rows=t2_rows,
                     af_map={"sh.600001": {"2026-09-18": 1.5}},
                     t3_rows=[{"ts_code": c, "date": "2026-09-18",
                               "total_mv_yi": 100.0, "float_mv_yi": 80.0,
                               "pe_ttm": 5.0, "pb": 0.8, "turnover": 1.0,
                               "ttm_yield_pct": 3.2} for c in fresh + halted])
    r = drv.run_incremental(con, db, fresh + halted, days=3)
    assert r["batch"]["t2_codes"] == 1   # 批量只含单日缺口股
    # 单日缺口股：批量命中落库
    assert con.execute(
        "SELECT source FROM kline_daily WHERE ts_code='sh.600001' "
        "AND date='2026-09-18'").fetchone()[0] == "tushare"
    # 多日缺口股：legacy 空窗口（fake K线空）→ 无新行、不报错
    assert con.execute(
        "SELECT COUNT(*) FROM kline_daily WHERE ts_code='sh.600002' "
        "AND date='2026-09-18'").fetchone()[0] == 0
    assert r["t2"]["processed"] == 2
    # T3 两只都在批量 t3 缓存 → 零腾讯快照
    assert tc["snapshot"] == 0
    con.close()


# ---------------------------------------------------------------------------
def test_tushare_batch_unit_conversions(monkeypatch):
    """tushare_ingest 批量接口单位换算（vol 手→股、amount 千元→元、mv 万→亿、代码）。"""
    import pandas as pd
    import lake.ingest.tushare_ingest as ti

    class FakePro:
        def daily(self, trade_date):
            return pd.DataFrame([
                {"ts_code": "601398.SH", "trade_date": "20260918", "open": 1,
                 "high": 2, "low": 1, "close": 1.5, "pre_close": 1.4,
                 "pct_chg": 7.14, "vol": 123.0, "amount": 4567.0},
                {"ts_code": "430047.BJ", "trade_date": "20260918", "open": 1,
                 "high": 2, "low": 1, "close": 3.0, "pre_close": 2.0,
                 "pct_chg": 50.0, "vol": 1.0, "amount": 2.0},  # BJ → 丢弃
            ])

        def adj_factor(self, trade_date):
            return pd.DataFrame([
                {"ts_code": "601398.SH", "trade_date": "20260918",
                 "adj_factor": 1.25},
            ])

        def daily_basic(self, trade_date):
            return pd.DataFrame([
                {"ts_code": "601398.SH", "trade_date": "20260918", "pe": 8.0,
                 "pe_ttm": 8.5, "pb": 0.9, "total_mv": 123456000.0,
                 "float_mv": 100000000.0, "turnover_rate": 0.3,
                 "dv_ttm": 4.2},
            ])

    monkeypatch.setattr(ti, "_pro", lambda: FakePro())
    rows = ti.fetch_daily_batch("2026-09-18")
    assert len(rows) == 1, rows          # BJ 丢弃
    r = rows[0]
    assert r["ts_code"] == "sh.601398"   # 代码转换
    assert r["date"] == "2026-09-18"     # wire 格式 20260918 → 湖 YYYY-MM-DD
    assert r["volume"] == 12300         # 手→股 ×100
    assert r["amount"] == 4567000.0     # 千元→元 ×1000
    assert r["pct_chg"] == 7.14 and r["preclose"] == 1.4
    af = ti.fetch_adj_factor_batch("2026-09-18")
    # af 键 = 湖日期格式（2026-10-24 事故前此处断言 wire 格式 "20260918"，
    # 与 load_t2 按湖日期查 adj_map 的口径不一致 → 静默丢 af；一并固化修复）
    assert af == {"sh.601398": {"2026-09-18": 1.25}}, af
    b = ti.fetch_daily_basic_batch("2026-09-18")
    assert b[0]["date"] == "2026-09-18"
    assert b[0]["total_mv_yi"] == pytest.approx(12345.6)   # 万元→亿元 ÷10000
    assert b[0]["float_mv_yi"] == pytest.approx(10000.0)
    assert b[0]["pe_ttm"] == 8.5 and b[0]["turnover"] == 0.3
    assert b[0]["ttm_yield_pct"] == 4.2


def test_tushare_code_conversion_edge():
    """_to_lake_code 边界：SH/SZ 转换；BJ/畸形 → None。"""
    import lake.ingest.tushare_ingest as ti

    assert ti._to_lake_code("601398.SH") == "sh.601398"
    assert ti._to_lake_code("000001.SZ") == "sz.000001"
    assert ti._to_lake_code("430047.BJ") is None
    assert ti._to_lake_code("000001") is None
    assert ti._to_lake_code("") is None


def test_batch_call_retries_on_none(monkeypatch):
    """tushare API code!=0 返回 None（不抛异常，实测 adj_factor 瞬时错误）
    → _batch_call 退避重试，绝不把 None 当空市场。"""
    import pandas as pd
    import lake.ingest.tushare_ingest as ti

    calls = {"n": 0}

    class FakePro:
        def adj_factor(self, trade_date):
            calls["n"] += 1
            if calls["n"] == 1:
                return None   # 首次瞬时错误
            return pd.DataFrame([
                {"ts_code": "601398.SH", "trade_date": "20260918",
                 "adj_factor": 1.25},
            ])

    monkeypatch.setattr(ti, "_pro", lambda: FakePro())
    import time as _real_time

    # _batch_call 内 `import time as _time` 走全局 time 模块 → patch 全局 sleep
    monkeypatch.setattr(_real_time, "sleep", lambda s: None)
    rows = ti.fetch_adj_factor_batch("2026-09-18")
    assert calls["n"] == 2, f"应重试一次: {calls}"
    assert rows == {"sh.601398": {"2026-09-18": 1.25}}, rows


def test_batch_call_quota_penalty_fails_fast(monkeypatch):
    """配额耗尽（实测免费档 adj_factor/daily_basic 5 次/天 → 惩罚 1 次/小时）
    → 立即抛错不重试（重试无意义，最多 1 次/时）→ 上层回退逐只 legacy。"""
    import lake.ingest.tushare_ingest as ti

    calls = {"n": 0}
    sleeps = []

    class FakePro:
        def daily(self, trade_date):
            calls["n"] += 1
            raise Exception("抱歉，您访问接口(daily)频率超限(5次/天)，"
                            "具体频次详情：https://tushare.pro/document/1?doc_id=108")

    monkeypatch.setattr(ti, "_pro", lambda: FakePro())
    import time as _real_time

    monkeypatch.setattr(_real_time, "sleep", lambda s: sleeps.append(s))
    try:
        ti.fetch_daily_batch("2026-09-18")
        raise AssertionError("配额惩罚态应立即抛错")
    except RuntimeError as exc:
        assert "配额耗尽" in str(exc), str(exc)
    assert calls["n"] == 1, f"不得重试: {calls}"
    assert sleeps == [], f"不得退避等待: {sleeps}"
