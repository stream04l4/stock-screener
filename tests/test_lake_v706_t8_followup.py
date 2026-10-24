# -*- coding: utf-8 -*-
"""test_lake_v706_t8_followup —— v7.0.6：T8 factor_snapshot 每日增量跟进。

背景：T8 原先只在 full 阶段 3 重算 → 每日增量（cron 11:30 UTC，incremental）
不含 T8 → 因子快照停在上一次 full 的 as_of（2026-10-22 排查时停在 2026-09-30），
而 kline/valuation 每日在更 → 因子口径与最新行情脱节数周。
修复：run_incremental 在 T7 后跟进一次纯本地重算（零网络零配额，~6min 全市场）。

纪律（同 test_lake_v614 契约）：全离线——tmp 库 + tmp yaml + monkeypatch 路径；
fake fetcher 零真实网络；绝不触碰 data/lake/ 生产库。
"""
from __future__ import annotations

import datetime as dt
import json
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
    """路径全隔离（同 v614 契约：progress/yaml/quota 一律 tmp）。"""
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
                "  adata_f10_enabled: true\n")
    monkeypatch.setattr(lconfig, "strategy_yaml_path", lambda: yaml_path)
    monkeypatch.setattr(lb, "_progress_path",
                        lambda: str(tmp_path / "prog" / "backfill_progress.json"))
    monkeypatch.setattr(lconn, "progress_path",
                        lambda: str(tmp_path / "prog" / "backfill_progress.json"))
    # v7.0.10 T9 跟进隔离：TE 现值抓取 + csv 入库走离线替身（零网络、不碰真实 cache）
    import lake.ingest.local_cache_ingest as _lci
    import screener.data.rf as _rf_mod

    monkeypatch.setattr(_rf_mod, "fetch_rf_10y",
                        lambda rf_cfg, cache_dir, run_day, session=None: (
                            0.0164, {"date": run_day, "yield_pct": 1.64,
                                     "source": "tradingeconomics"}))
    monkeypatch.setattr(_lci, "load_macro_rf",
                        lambda con, cache_dir, source="te_local": 0)
    yield


# ---------------------------------------------------------------------------
def _td(n: int = 500) -> list:
    """最近 n 个工作日（升序），止于 FIXED_TODAY（跳周末）。"""
    out, d = [], dt.date(2026, 9, 18)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= dt.timedelta(days=1)
    return list(reversed(out))


def _seed(db_path: str, codes, bars: int = 500, with_kline: bool = True) -> None:
    """T1 播种 + T2 500 日 K线（adj_factor=1.0 → hfq view 可用）+ T7 四指数 500 日。"""
    con = lconn.open(db_path)
    con.executemany(
        "INSERT INTO stock_master (ts_code,name,industry_csric2,board,is_st,"
        "source,fetched_at,data_version) VALUES (?,?,?,?,0,'t','2026-09-15 09:00:00','v6.1')",
        [(c, f"股{c}", "J66", "主板") for c in codes])
    dates = _td(bars)
    if with_kline:
        kl = [(c, d, 10.0 + 0.01 * (i % 7), 10.0 + 0.01 * (i % 7),
               10.0 + 0.01 * (i % 7), 10.0 + 0.01 * (i % 7), 1000, 10000.0, 1.0,
               't', '2026-09-15 09:00:00', 'v6.1')
              for c in codes for i, d in enumerate(dates)]
        con.executemany(
            'INSERT INTO kline_daily (ts_code,date,open,high,low,"close",volume,'
            "amount,adj_factor,is_st,source,fetched_at,data_version) "
            "VALUES (?,?,?,?,?,?,?,?,?,0,?,?,?)", kl)
    con.executemany(
        "INSERT INTO index_daily (index_code,date,open,high,low,close,volume,amount,"
        "source,fetched_at,data_version) VALUES (?,?,?,?,?,?,1000,NULL,'t','2026-09-15 09:00:00','v6.1')",
        [(ic, d, 3000.0 + 0.5 * (i % 5), 3000.0 + 0.5 * (i % 5),
          3000.0 + 0.5 * (i % 5), 3000.0 + 0.5 * (i % 5))
         for ic in INDEX_CODES for i, d in enumerate(dates)])
    con.close()


def _fake_tencent(monkeypatch, kline_rows, snap):
    """fake 取数（零网络；T2 缺口窗口 [date_max+1..今日] 为空 → 无新增 → done）。"""
    import lake.ingest.tencent_ingest as ti

    counter = {"kline": 0, "snapshot": 0}

    def fake_kline(client, ts_code, n):
        counter["kline"] += 1
        return [dict(r) for r in kline_rows]

    def fake_snapshot(client, ts_codes):
        counter["snapshot"] += 1
        return {c: dict(snap[c]) for c in ts_codes if c in snap}

    monkeypatch.setattr(ti, "fetch_kline_ohlcv", fake_kline)
    monkeypatch.setattr(ti, "fetch_snapshot", fake_snapshot)
    # tdx adapter 防网络自检
    from lake.ingest import source_pool as sp
    ad = sp.get_adapter("tdx")
    if ad is not None:
        monkeypatch.setattr(ad, "available", lambda: False)
    return counter


# ---------------------------------------------------------------------------
def test_t8_followup_refreshes_factor_snapshot(tmp_path):
    """incremental 收尾跟进 T8 重算 → factor_snapshot 落在最新 as_of（T2 max date）。"""
    db = str(tmp_path / "t8f.duckdb")
    codes = ["sh.600001"]
    _seed(db, codes)
    con = lconn.open(db)
    snap = {c: {"total_mv_yi": 100.0, "float_mv_yi": 80.0, "pe_ttm": 5.0,
                "pb": 0.8, "turnover": 1.0} for c in codes}
    mp = pytest.MonkeyPatch()
    _fake_tencent(mp, [], snap)
    r = drv.run_incremental(con, db, codes, days=3)
    # T8 跟进：as_of = T2 max date = 今日（种子止于 09-18）；500 bar → ann_vol_3y 可算
    assert "t8_error" not in r, f"T8 跟进不应失败: {r.get('t8_error')}"
    assert r["t8"]["as_of"] == FIXED_TODAY
    assert r["t8"]["rows"] >= 1
    names = {x[0] for x in con.execute(
        "SELECT DISTINCT factor_name FROM factor_snapshot "
        f"WHERE as_of_date='{FIXED_TODAY}'").fetchall()}
    assert "ann_vol_3y" in names, f"500 bar 应可算 ann_vol_3y: {names}"
    # progress 视图：factor_snapshot done=全集、state=done
    with open(lb._progress_path(), encoding="utf-8") as f:
        prog = json.load(f)
    e8 = next(t for t in prog["tasks"] if t.get("table") == "factor_snapshot")
    assert e8["done"] == 1 and e8["state"] == "done", e8
    con.close()


def test_t8_followup_failure_does_not_block_incremental(tmp_path, monkeypatch):
    """T8 重算抛错 → 增量照常完成（t2/t3/t7 统计齐全），仅 stats 记痕 t8_error。"""
    import lake.factors as lf
    db = str(tmp_path / "t8e.duckdb")
    codes = ["sh.600001"]
    _seed(db, codes)
    monkeypatch.setattr(lf, "recompute_all",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom-t8")))
    con = lconn.open(db)
    mp = pytest.MonkeyPatch()
    snap = {c: {"total_mv_yi": 100.0, "float_mv_yi": 80.0, "pe_ttm": 5.0,
                "pb": 0.8, "turnover": 1.0} for c in codes}
    _fake_tencent(mp, [], snap)
    r = drv.run_incremental(con, db, codes, days=3)
    assert r["t2"]["processed"] == 1 and r["t3"]["processed"] == 1
    assert r["t7"]["processed"] == len(INDEX_CODES)
    assert "boom-t8" in r["t8_error"]
    assert "t8" not in r
    con.close()


def test_t8_followup_skips_when_no_kline(tmp_path):
    """库内 T2 无数据（新股/空库）→ T8 显式 skipped（不硬造 as_of）、不抛错。"""
    db = str(tmp_path / "t8s.duckdb")
    codes = ["sh.600001"]
    _seed(db, codes, with_kline=False)
    con = lconn.open(db)
    mp = pytest.MonkeyPatch()
    snap = {c: {"total_mv_yi": 100.0, "float_mv_yi": 80.0, "pe_ttm": 5.0,
                "pb": 0.8, "turnover": 1.0} for c in codes}
    _fake_tencent(mp, [], snap)
    r = drv.run_incremental(con, db, codes, days=3)
    assert r["t8"] == {"skipped": "T2 kline_daily 无数据"}, r.get("t8")
    assert "t8_error" not in r
    con.close()
