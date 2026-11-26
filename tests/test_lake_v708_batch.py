# -*- coding: utf-8 -*-
"""test_lake_v708_batch —— v7.0.8：全市场单日批量（每日增量主轴）。

背景：新浪逐只 K线+hfq + 腾讯逐只快照 ≈ 1.5 万次调用，sina 限速下每日增量
9-10h（Joel 2026-10-22 投诉不可受）→ trade_date 全市场单次取回
（v7.0.8 时代 tushare pro.daily/pro.adj_factor/pro.daily_basic 各 1 次/分钟；
2026-11-26 tushare 弃用后改东财 clist 快照 1 次分页取回）→ 批量缓存 →
逐只只补批量没覆盖的缺口。

纪律（同 v614/v706 契约）：全离线——tmp 库 + tmp yaml + monkeypatch 路径；
EM 批量函数 fake 化（零真实网络）；绝不触碰 data/lake/ 生产库。
原 tushare 单位换算/重试/限频单测 2026-11-26 随源码一并移除。
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
    # v7.0.10 T9 跟进隔离：TE 现值抓取 + csv 入库走离线替身（零网络、不碰真实 cache）
    import lake.ingest.local_cache_ingest as _lci
    import screener.data.rf as _rf_mod

    monkeypatch.setattr(_rf_mod, "fetch_rf_10y",
                        lambda rf_cfg, cache_dir, run_day, session=None: (
                            0.0164, {"date": run_day, "yield_pct": 1.64,
                                     "source": "tradingeconomics"}))
    monkeypatch.setattr(_lci, "load_macro_rf",
                        lambda con, cache_dir, source="te_local": 7)
    # v7.0.32 T5 季度增量隔离：注册表注入 adata_f10 离线 mock（available()=False →
    # T5 抛错记痕、不阻断增量；零网络。T5 语义见 tests/test_lake_t5_incremental.py。）
    import lake.ingest.source_pool as _sp

    class _AdataDown:
        name = "adata_f10"

        def available(self):
            return False

        def fetch_f10(self, ts_code):
            raise AssertionError("T5 离线测试：不应取数")

    _reg = dict(_sp._adapter_registry())
    _reg["adata_f10"] = _AdataDown()
    monkeypatch.setattr(_sp, "_REGISTRY", _reg)
    # v7.0.33→2026-11-26：EM 是唯一批量通道（tushare 备源已移除）
    # → fixture 让 EM 恒离线，验证失败回退逐只 legacy 路径
    import lake.ingest.em_batch_ingest as _emb

    def _emb_offline(allow_sg=True):
        raise _emb.EmBatchError("test: EM offline（验证逐只 legacy 回退路径）")

    monkeypatch.setattr(_emb, "fetch_clist_snapshot", _emb_offline)
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


def _fake_tencent(monkeypatch, snap, idx_up_to=None):
    """fake 腾讯（零网络）：股票 K线恒空；指数 sh000001=工作日 bar 至 idx_up_to
    （缺省=运行日）——v7.0.33 批量通道以指数最新 bar 为日期锚。快照按 snap 返回。"""
    import lake.ingest.tencent_ingest as ti

    counter = {"kline": 0, "snapshot": 0}

    def fake_kline(client, ts_code, n):
        counter["kline"] += 1
        if ts_code == "sh000001":
            end = idx_up_to or drv._today_beijing()
            out, d = [], dt.date.fromisoformat(end)
            while len(out) < n:
                if d.weekday() < 5:
                    out.append({"date": d.isoformat(), "close": 1.0})
                d -= dt.timedelta(days=1)
            return list(reversed(out))[-n:]
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


def test_batch_failure_falls_back_legacy(tmp_path, monkeypatch):
    """EM 批量抛错 → stats 记痕 + 回退逐只 legacy（tushare 备源 2026-11-26 已移除；
    本例 fake 空窗口 → done 不写库）。"""
    db = str(tmp_path / "b2.duckdb")
    codes = ["sh.600001"]
    _seed(db, codes)
    con = lconn.open(db)
    snap = {c: {"total_mv_yi": 100.0, "float_mv_yi": 80.0, "pe_ttm": 5.0,
                "pb": 0.8, "turnover": 1.0} for c in codes}
    tc = _fake_tencent(monkeypatch, snap)
    r = drv.run_incremental(con, db, codes, days=3)
    assert "batch" in r and "EM offline" in r["batch"]["failed"], r.get("batch")
    # legacy 逐只路径被走到（腾讯 K线 fake 被调）；fake 空窗口 → 不写新 K线
    assert tc["kline"] >= 1, f"应走 legacy 逐只: {tc}"
    assert con.execute(
        "SELECT COUNT(*) FROM kline_daily WHERE ts_code='sh.600001' "
        "AND date='2026-09-18'").fetchone()[0] == 0
    # T3 无批量 → 走腾讯快照（stale 值，语义同 v7.0.7 前）
    assert con.execute(
        "SELECT source FROM valuation_daily WHERE ts_code='sh.600001' "
        "AND date='2026-09-18'").fetchone()[0] not in ("tushare", "em_batch")
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
    # 指数最新 bar=09-17（运行日 09-18 无 bar）→ ①.5 no_trade 短路
    # （不取 EM 快照：no_trade 判定只看指数最新 bar vs 缺口众数）
    tc = _fake_tencent(monkeypatch, snap, idx_up_to="2026-09-17")
    r = drv.run_incremental(con, db, codes, days=3)
    assert r.get("batch", {}).get("no_trade") is True, r.get("batch")
    # 无新 K线（不写）；也不逐只 legacy（腾讯 K线只被 T7 指数调用，非股票）
    assert con.execute(
        "SELECT COUNT(*) FROM kline_daily WHERE date='2026-09-18'").fetchone()[0] == 0
    # v7.0.33：no_trade 携指数 bar 日期 → data_date=09-17 → 表级预检判定 T2 已齐
    # → 连逐只任务都不建（比旧版逐只 no-op 更强；processed=0 即无空转）
    assert r["t2"]["processed"] == 0
    con.close()


def test_batch_weekend_gap_refetches_index_latest_trade_day(tmp_path, monkeypatch):
    """周一跑：众数缺口=周六（陈旧缺口）→ EM 快照=指数最新交易日（周一）
    → 按指数最新交易日重取批量（2026-10-24 修复：原逻辑回退逐只，周一要 2-4h；
    2026-11-26 tushare 弃用后本测试改锚 EM 通道，refetch 判定语义不变）。"""
    import lake.ingest.em_batch_ingest as emb

    db = str(tmp_path / "b5.duckdb")
    codes = ["sh.600001", "sh.600002"]
    monkeypatch.setattr(drv, "_today_beijing", lambda: "2026-09-21")  # 周一
    _seed(db, codes, last_date="2026-09-18")   # 缺口 = 09-19（周六）
    con = lconn.open(db)
    snap = {c: {"total_mv_yi": 100.0, "float_mv_yi": 80.0, "pe_ttm": 5.0,
                "pb": 0.8, "turnover": 1.0} for c in codes}
    # 指数 sh000001：09-18 + 09-21（周一）两根 bar；股票 K线 fake 恒空
    import lake.ingest.tencent_ingest as tix

    def fake_kline(client, ts_code, n):
        if ts_code == "sh000001":
            return [{"date": "2026-09-18", "open": 1, "high": 1, "low": 1,
                     "close": 1, "volume": 1},
                    {"date": "2026-09-21", "open": 1, "high": 1, "low": 1,
                     "close": 1, "volume": 1}]
        return []

    monkeypatch.setattr(tix, "fetch_kline_ohlcv", fake_kline)
    monkeypatch.setattr(tix, "fetch_snapshot",
                        lambda client, ts_codes: {c: dict(snap[c])
                                                  for c in ts_codes if c in snap})
    from lake.ingest import source_pool as sp
    ad = sp.get_adapter("tdx")
    if ad is not None:
        monkeypatch.setattr(ad, "available", lambda: False)

    # 收盘后（非盘中 guard）；EM 快照=指数最新交易日（周一）
    monkeypatch.setattr(drv, "_now_beijing_dt",
                        lambda: dt.datetime(2026, 9, 21, 21, 0))
    monkeypatch.setattr(emb, "fetch_clist_snapshot",
                        lambda allow_sg=True: [_em_snap_row(c) for c in codes])
    monkeypatch.setattr(emb, "fetch_ex_events", lambda d: [])

    r = drv.run_incremental(con, db, codes, days=3)
    assert r.get("batch", {}).get("date") == "2026-09-21", r.get("batch")
    assert r["batch"]["channel"] == "em" and r["batch"]["refetch"] is True
    # 周一 bar 落库（source=em_batch），周六不写
    n = con.execute(
        "SELECT COUNT(*) FROM kline_daily WHERE date='2026-09-21' "
        "AND source='em_batch'").fetchone()[0]
    assert n == 2, n
    assert con.execute(
        "SELECT COUNT(*) FROM kline_daily WHERE date='2026-09-19'").fetchone()[0] == 0
    assert r["t2"]["processed"] == 2
    con.close()


def test_multi_day_gap_code_falls_back_legacy(tmp_path, monkeypatch):
    """多日缺口股（停牌）不在批量单日窗口 → 逐只 legacy 补整窗（批量只补单日股）。

    2026-11-26 tushare 弃用后：EM 快照只含单日缺口股 sh.600001；多日缺口股
    sh.600002 的 T2/T3 全部走逐只 legacy（本例 T2 窗口 fake 空 → 无新行）。
    """
    import lake.ingest.em_batch_ingest as emb

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
    # 收盘后（非盘中 guard）；EM 快照只含单日缺口股 sh.600001
    monkeypatch.setattr(drv, "_now_beijing_dt",
                        lambda: dt.datetime(2026, 9, 18, 21, 30))
    monkeypatch.setattr(emb, "fetch_clist_snapshot",
                        lambda allow_sg=True: [_em_snap_row("sh.600001")])
    monkeypatch.setattr(emb, "fetch_ex_events", lambda d: [])
    r = drv.run_incremental(con, db, fresh + halted, days=3)
    assert r["batch"]["t2_codes"] == 1   # 批量只含单日缺口股
    # 单日缺口股：批量命中落库
    assert con.execute(
        "SELECT source FROM kline_daily WHERE ts_code='sh.600001' "
        "AND date='2026-09-18'").fetchone()[0] == "em_batch"
    # 多日缺口股：legacy 空窗口（fake K线空）→ 无新行、不报错
    assert con.execute(
        "SELECT COUNT(*) FROM kline_daily WHERE ts_code='sh.600002' "
        "AND date='2026-09-18'").fetchone()[0] == 0
    assert r["t2"]["processed"] == 2
    # 多日缺口股 T3 不在 EM 快照 → 走腾讯快照（sh.600001 批量命中零快照）
    assert tc["snapshot"] >= 1
    con.close()


# ---------------------------------------------------------------------------
# v7.0.9：表级完整性预检（"不需要更新的数据就不要更新"）
# ---------------------------------------------------------------------------
def _seed_all_tables(db: str, codes, data_date: str,
                     drop_codes_t3: tuple = ()) -> None:
    """在 data_date 补齐 T3/T7/T8（T2 由 _seed 提供）；drop_codes_t3 = 挖掉的估值股。"""
    con = lconn.open(db)
    con.executemany(
        "INSERT INTO valuation_daily (ts_code,date,total_mv,float_mv,pe_ttm,pb,"
        "turnover_pct,source,fetched_at,data_version) VALUES (?,?,?,?,?,?,?,'t',"
        "'2026-09-15 09:00:00','v6.1')",
        [(c, data_date, 100.0, 80.0, 5.0, 0.8, 1.0)
         for c in codes if c not in drop_codes_t3])
    con.executemany(
        "INSERT INTO index_daily (index_code,date,open,high,low,\"close\",volume,"
        "source,fetched_at,data_version) VALUES (?,?,1,1,1,1,1,'t',"
        "'2026-09-15 09:00:00','v6.1')",
        [(ic, data_date) for ic in INDEX_CODES])
    con.executemany(
        "INSERT INTO factor_snapshot (ts_code,as_of_date,factor_name,value,"
        "source,fetched_at,data_version) VALUES (?,?,?,1.0,'t',"
        "'2026-09-15 09:00:00','v6.1')",
        [(c, data_date, "mom_1m") for c in codes])
    con.close()


def test_precheck_all_up_to_date_early_exit(tmp_path, monkeypatch):
    """全表 data_date 已齐 + 今日非交易日 → 预检提前返回：零逐只任务、零快照、
    K线只被指数探测调用 1 次（2026-10-24：周末空转 5219 个 no-op 的根治）。"""
    db = str(tmp_path / "pc1.duckdb")
    codes = ["sh.600001", "sh.600002"]
    _seed(db, codes, last_date="2026-09-17")      # K线止于 09-17 → gap=09-18
    _seed_all_tables(db, codes, "2026-09-17")     # T3/T7/T8 全部 09-17 已齐
    con = lconn.open(db)

    import lake.ingest.tencent_ingest as tix

    counter = {"kline": 0, "snapshot": 0}

    def fake_kline(client, ts_code, n):
        counter["kline"] += 1
        if ts_code == "sh000001":
            return [{"date": "2026-09-16", "open": 1, "high": 1, "low": 1,
                     "close": 1, "volume": 1},
                    {"date": "2026-09-17", "open": 1, "high": 1, "low": 1,
                     "close": 1, "volume": 1}]
        return []

    monkeypatch.setattr(tix, "fetch_kline_ohlcv", fake_kline)
    monkeypatch.setattr(tix, "fetch_snapshot",
                        lambda client, ts_codes:
                        ({c: {"total_mv_yi": 100.0, "float_mv_yi": 80.0,
                              "pe_ttm": 5.0, "pb": 0.8, "turnover": 1.0}
                          for c in ts_codes if c in codes}))
    from lake.ingest import source_pool as sp
    ad = sp.get_adapter("tdx")
    if ad is not None:
        monkeypatch.setattr(ad, "available", lambda: False)
    # 预检早退路径不触发批量（fixture 的 EM 离线 guard 仍在，防误调真实网络）

    r = drv.run_incremental(con, db, codes, days=3)
    assert r["precheck"]["data_date"] == "2026-09-17", r["precheck"]
    assert r["precheck"]["result"] == "all_up_to_date", r["precheck"]
    # 零逐只任务：早退发生在建队列之前 → T2/T3/T7/T8 均无 stats 键
    assert r.get("t2") is None and r.get("t3") is None
    assert r.get("t7") is None and r.get("t8") is None
    # 网络：K线仅指数探测 1 次；快照 0 次
    assert counter["kline"] == 1, counter
    assert counter["snapshot"] == 0, counter
    # 无新写入
    assert con.execute(
        "SELECT COUNT(*) FROM kline_daily WHERE date='2026-09-18'").fetchone()[0] == 0
    con.close()


def test_precheck_partial_only_sweeps_missing(tmp_path, monkeypatch):
    """T2/T7/T8 齐、T3 缺 1 只 → 只扫那 1 只（T2/T7 零任务、T8 跳过）。"""
    db = str(tmp_path / "pc2.duckdb")
    codes = ["sh.600001", "sh.600002"]
    _seed(db, codes, last_date="2026-09-17")
    _seed_all_tables(db, codes, "2026-09-17", drop_codes_t3=("sh.600002",))
    con = lconn.open(db)

    import lake.ingest.tencent_ingest as tix

    counter = {"kline": 0, "snapshot": 0, "snap_calls": []}
    snap = {c: {"total_mv_yi": 100.0, "float_mv_yi": 80.0,
                "pe_ttm": 5.0, "pb": 0.8, "turnover": 1.0} for c in codes}

    def fake_kline(client, ts_code, n):
        counter["kline"] += 1
        if ts_code == "sh000001":
            return [{"date": "2026-09-17", "open": 1, "high": 1, "low": 1,
                     "close": 1, "volume": 1}]
        return []

    def fake_snapshot(client, ts_codes):
        counter["snapshot"] += 1
        counter["snap_calls"].append(list(ts_codes))
        return {c: dict(snap[c]) for c in ts_codes if c in snap}

    monkeypatch.setattr(tix, "fetch_kline_ohlcv", fake_kline)
    monkeypatch.setattr(tix, "fetch_snapshot", fake_snapshot)
    from lake.ingest import source_pool as sp
    ad = sp.get_adapter("tdx")
    if ad is not None:
        monkeypatch.setattr(ad, "available", lambda: False)
    # T2 齐 → 无 K线缺口 → EM 批量不触发（fixture 的 EM 离线 guard 仍在）

    r = drv.run_incremental(con, db, codes, days=3)
    assert r["precheck"]["t2"] is True and r["precheck"]["t3"] is False
    assert r["t2"]["total"] == 0 and r["t2"]["processed"] == 0   # K线齐 → 零任务
    assert r["t3"]["total"] == 1 and r["t3"]["processed"] == 1, r["t3"]
    assert r["t7"]["total"] == 0                                 # 指数齐 → 零任务
    assert "skipped" in r["t8"], r.get("t8")                     # 因子齐 → 跳过重算
    # 快照只探测+取数缺口那只（sh.600002），sh.600001 不碰
    fetched = [c for call in counter["snap_calls"] for c in call]
    assert "sh.600001" not in fetched, fetched
    assert "sh.600002" in fetched, fetched
    # 缺口估值落库（date=今日 09-18，快照口径）
    assert con.execute(
        "SELECT COUNT(*) FROM valuation_daily WHERE ts_code='sh.600002' "
        "AND date='2026-09-18'").fetchone()[0] == 1
    con.close()


# ---------------------------------------------------------------------------
# v7.0.10：T9 macro_rf 每日跟进（"T9 好像还不是最新的"——2026-10-24）
# ---------------------------------------------------------------------------
def _t9_setup(tmp_path, monkeypatch, drop_codes_t3=("sh.600002",)):
    """T2/T7/T8 齐、T3 缺 1 只 → 不早退 → 走到 T9 跟进（公共播种+fake）。"""
    db = str(tmp_path / "t9.duckdb")
    codes = ["sh.600001", "sh.600002"]
    _seed(db, codes, last_date="2026-09-17")
    _seed_all_tables(db, codes, "2026-09-17", drop_codes_t3=drop_codes_t3)
    con = lconn.open(db)

    import lake.ingest.tencent_ingest as tix

    monkeypatch.setattr(tix, "fetch_kline_ohlcv",
                        lambda client, ts_code, n:
                        ([{"date": "2026-09-17", "open": 1, "high": 1, "low": 1,
                           "close": 1, "volume": 1}]
                         if ts_code == "sh000001" else []))
    monkeypatch.setattr(tix, "fetch_snapshot",
                        lambda client, ts_codes:
                        ({c: {"total_mv_yi": 100.0, "float_mv_yi": 80.0,
                              "pe_ttm": 5.0, "pb": 0.8, "turnover": 1.0}
                          for c in ts_codes if c in codes}))
    from lake.ingest import source_pool as sp
    ad = sp.get_adapter("tdx")
    if ad is not None:
        monkeypatch.setattr(ad, "available", lambda: False)
    # T2 齐 → 无 K线缺口 → EM 批量不触发（fixture 的 EM 离线 guard 仍在）
    return db, codes, con


def test_incremental_t9_followup_fetch_and_load(tmp_path, monkeypatch):
    """v7.0.10：增量 T9 跟进 = TE 现值抓取 1 次 + csv 全量 upsert → stats.t9 记痕。

    2026-10-24 根因：macro_rf 只在 full 阶段 4/手动 t9 落库，每日增量不碰 →
    表停 09-30、csv 已到 10-20。修复后每次增量 macro_rf 恒为最新。
    """
    db, codes, con = _t9_setup(tmp_path, monkeypatch)

    import lake.ingest.local_cache_ingest as _lci
    import screener.data.rf as _rf_mod

    calls = {"fetch": 0, "load": 0}

    def fake_fetch(rf_cfg, cache_dir, run_day, session=None):
        calls["fetch"] += 1
        return 0.0164, {"date": run_day, "yield_pct": 1.64,
                        "source": "tradingeconomics"}

    def fake_load(con2, cache_dir, source="te_local"):
        calls["load"] += 1
        return 3

    monkeypatch.setattr(_rf_mod, "fetch_rf_10y", fake_fetch)
    monkeypatch.setattr(_lci, "load_macro_rf", fake_load)

    r = drv.run_incremental(con, db, codes, days=3)
    assert r["precheck"].get("result") != "all_up_to_date"   # 不早退 → 走到 T9
    assert r["t9"] == {"rows": 3, "source": "tradingeconomics"}, r.get("t9")
    assert "t9_error" not in r
    assert calls == {"fetch": 1, "load": 1}, calls       # 各恰好一次
    con.close()


def test_incremental_t9_fetch_failure_nonblocking(tmp_path, monkeypatch):
    """v7.0.10：TE 抓取失败 → 用现有 csv 继续（D4' 语义）、不阻断增量。"""
    db, codes, con = _t9_setup(tmp_path, monkeypatch)

    import screener.data.rf as _rf_mod

    calls = {"fetch": 0}

    def fake_fetch_fail(rf_cfg, cache_dir, run_day, session=None):
        calls["fetch"] += 1
        raise RuntimeError("TE 页面不可达（测试注入）")

    monkeypatch.setattr(_rf_mod, "fetch_rf_10y", fake_fetch_fail)
    # load_macro_rf 用 fixture 替身（返回 7）

    r = drv.run_incremental(con, db, codes, days=3)
    assert calls["fetch"] == 1
    assert r["t9"]["source"] == "fetch_failed_use_existing", r.get("t9")
    assert r["t9"]["rows"] == 7                            # 现有 csv 照常入库
    assert "t9_error" not in r, r                          # 不阻断
    # 增量主体不受 T9 故障影响
    assert r["t3"]["processed"] == 1
    con.close()


# ---------------------------------------------------------------------------
# v7.0.33：EM 主通道（东财 clist 批量取代 tushare 主轴）
# ---------------------------------------------------------------------------

def _em_snap_row(ts_code="sh.600001"):
    """fetch_clist_snapshot 契约行（字段映射后口径）。"""
    return {"ts_code": ts_code, "name": "测试股", "close": 10.25,
            "pct_chg": 2.5, "open": 10.1, "high": 10.3, "low": 10.0,
            "preclose": 10.0, "volume": 123456, "amount": 1000000.0,
            "turnover": 1.0, "total_mv_yi": 100.0, "float_mv_yi": 80.0,
            "list_date": "2000-01-01", "industry": "测试行业",
            "pe_ttm": 5.0, "pb": 0.8}


def test_em_batch_primary_channel(tmp_path, monkeypatch):
    """EM 主通道命中 → 行落库 source=em_batch + af 本地推导（tushare 2026-11-26 已弃用，
    无备源概念）。"""
    import lake.ingest.em_batch_ingest as emb

    db = str(tmp_path / "em1.duckdb")
    codes = ["sh.600001"]
    _seed(db, codes)   # 缺口 = 09-18 单日；kline 止 09-17（af=1.0 已播种）
    con = lconn.open(db)
    snap = {c: {"total_mv_yi": 100.0, "float_mv_yi": 80.0, "pe_ttm": 5.0,
                "pb": 0.8, "turnover": 1.0} for c in codes}
    tc = _fake_tencent(monkeypatch, snap)   # 指数 bar 至运行日 → EM 触发
    # 收盘后（非盘中 guard）
    monkeypatch.setattr(drv, "_now_beijing_dt",
                        lambda: dt.datetime(2026, 9, 18, 21, 30))
    # EM 在线：快照 1 行 + 1 条除权事件（现金 0.2 元/股，无送转）
    monkeypatch.setattr(emb, "fetch_clist_snapshot",
                        lambda allow_sg=True: [_em_snap_row()])
    monkeypatch.setattr(emb, "fetch_ex_events",
                        lambda d: [{"ts_code": "sh.600001", "ex_date": d,
                                    "cash": 0.2, "bonus": 0.0,
                                    "has_allot": False}])
    r = drv.run_incremental(con, db, codes, days=3)

    # 批量走 EM：channel=em（tushare 2026-11-26 已弃用，无备源概念）
    assert r["batch"]["channel"] == "em", r.get("batch")
    assert r["batch"]["n_events"] == 1
    # T2：em_batch 行落库
    row = con.execute(
        'SELECT "close", volume, amount, pct_chg, preclose, source '
        "FROM kline_daily WHERE ts_code='sh.600001' AND date='2026-09-18'"
    ).fetchone()
    assert row == (10.25, 123456, 1000000.0, 2.5, 10.0, "em_batch"), row
    # af：本地推导（除权参考价步长，c0=09-17 close，prev af=1.0）
    c0 = con.execute(
        "SELECT \"close\" FROM kline_daily WHERE ts_code='sh.600001' "
        "AND date='2026-09-17'").fetchone()[0]
    af = con.execute(
        "SELECT adj_factor FROM kline_daily WHERE ts_code='sh.600001' "
        "AND date='2026-09-18'").fetchone()[0]
    assert af == pytest.approx(1.0 * c0 * 1.0 / (c0 - 0.2), rel=1e-9), af
    # T3：em_batch 落库（date=批量日，非运行日）
    vrow = con.execute(
        "SELECT total_mv, source FROM valuation_daily "
        "WHERE ts_code='sh.600001' AND date='2026-09-18'").fetchone()
    assert vrow == (100.0, "em_batch"), vrow
    # T2/T3 全命中批量 → 腾讯快照零调用
    assert tc["snapshot"] == 0
    con.close()


def test_refetch_batch_cannot_cover_pre_batch_trading_gap(tmp_path, monkeypatch):
    """v7.0.33 修复：refetch 批量（批量日=指数最新交易日）不得覆盖批量日**之前**
    的缺失交易日。

    2026-11-15 实锤：5215 只 11-13 缺口被 done 键毒化（空窗误标完成）→ 周一批量日
    =11-16，若按旧判定（区间内无其他交易日即覆盖）批量 11-16 行落库后标 done
    → 11-13 永久空洞。修复：start 本身是批量日前交易日（在指数 bar 中）→ 批量不
    覆盖 → 逐只窗口取数补齐。
    """
    import lake.ingest.em_batch_ingest as emb

    db = str(tmp_path / "em3.duckdb")
    codes = ["sh.600001"]
    monkeypatch.setattr(drv, "_today_beijing", lambda: "2026-09-21")  # 周一
    _seed(db, codes, last_date="2026-09-17")   # start=09-18（周五，缺失）
    con = lconn.open(db)
    snap = {c: {"total_mv_yi": 100.0, "float_mv_yi": 80.0, "pe_ttm": 5.0,
                "pb": 0.8, "turnover": 1.0} for c in codes}
    # 指数 bar：09-17/09-18/09-21（09-18 在 bar 中=缺失的交易日）；股票 K线=
    # 09-18+09-21 两根（逐只窗口取数可补齐）
    import lake.ingest.tencent_ingest as tix

    def fake_kline(client, ts_code, n):
        if ts_code == "sh000001":
            return [{"date": d, "open": 1, "high": 1, "low": 1, "close": 1,
                     "volume": 1} for d in ("2026-09-17", "2026-09-18",
                                            "2026-09-21")]
        return [{"date": "2026-09-18", "open": 10.1, "high": 10.3, "low": 10.0,
                 "close": 10.2, "volume": 1000},
                {"date": "2026-09-21", "open": 10.3, "high": 10.5, "low": 10.1,
                 "close": 10.4, "volume": 2000}]

    monkeypatch.setattr(tix, "fetch_kline_ohlcv", fake_kline)
    monkeypatch.setattr(tix, "fetch_snapshot",
                        lambda client, ts_codes: {c: dict(snap[c])
                                                  for c in ts_codes if c in snap})
    from lake.ingest import source_pool as sp
    ad = sp.get_adapter("tdx")
    if ad is not None:
        monkeypatch.setattr(ad, "available", lambda: False)
    # EM 在线：快照行（批量日=09-21，refetch）
    monkeypatch.setattr(drv, "_now_beijing_dt",
                        lambda: dt.datetime(2026, 9, 21, 21, 0))
    monkeypatch.setattr(emb, "fetch_clist_snapshot",
                        lambda allow_sg=True: [_em_snap_row()])
    monkeypatch.setattr(emb, "fetch_ex_events", lambda d: [])
    r = drv.run_incremental(con, db, codes, days=3)

    assert r["batch"]["channel"] == "em" and r["batch"]["date"] == "2026-09-21"
    assert r["batch"].get("refetch") is True
    # 09-18（批量日前缺失交易日）必须由逐只窗口取数补齐——不得被 EM 09-21 行替代
    rows = con.execute(
        "SELECT date, source FROM kline_daily WHERE ts_code='sh.600001' "
        "AND date >= '2026-09-18' ORDER BY date").fetchall()
    assert [str(x[0]) for x in rows] == ["2026-09-18", "2026-09-21"], rows
    assert all(x[1] != "em_batch" for x in rows), rows  # 走逐只（tencent），非批量行
    con.close()


def test_em_intraday_guard_skips_batch(tmp_path, monkeypatch):
    """盘中（北京<15:05 且指数 bar=今日）→ EM 拒用 → 逐只 legacy（无备源）。

    2026-11-26 tushare 弃用后：EM 拒用不再有备源，T2 走腾讯逐只 K线。
    """
    import lake.ingest.em_batch_ingest as emb

    db = str(tmp_path / "em2.duckdb")
    codes = ["sh.600001"]
    _seed(db, codes)
    con = lconn.open(db)
    snap = {c: {"total_mv_yi": 100.0, "float_mv_yi": 80.0, "pe_ttm": 5.0,
                "pb": 0.8, "turnover": 1.0} for c in codes}
    _fake_tencent(monkeypatch, snap)
    monkeypatch.setattr(drv, "_now_beijing_dt",
                        lambda: dt.datetime(2026, 9, 18, 10, 30))  # 盘中
    monkeypatch.setattr(emb, "fetch_clist_snapshot",
                        lambda allow_sg=True: (_ for _ in ()).throw(
                            AssertionError("盘中不应取 EM 快照")))
    # 逐只 legacy T2 路径：腾讯 K线给 09-18 bar（EM 拒用后由它接管）
    import lake.ingest.tencent_ingest as tix

    def fake_kline(client, ts_code, n):
        if ts_code == "sh000001":
            return [{"date": "2026-09-18", "close": 1.0}]
        if ts_code == "sh.600001":
            return [{"date": "2026-09-18", "open": 10.1, "high": 10.3,
                     "low": 10.0, "close": 10.2, "volume": 1000}]
        return []

    monkeypatch.setattr(tix, "fetch_kline_ohlcv", fake_kline)
    r = drv.run_incremental(con, db, codes, days=3)
    # EM 盘中拒用（batch failed 记痕含 em: 前缀）→ 逐只 legacy 接管
    assert "em:" in r["batch"]["failed"], r.get("batch")
    row = con.execute(
        "SELECT source FROM kline_daily WHERE ts_code='sh.600001' "
        "AND date='2026-09-18'").fetchone()
    assert row is not None and row[0] != "em_batch", row   # 逐只落库，非批量行
    con.close()


# ---------------------------------------------------------------------------
# v7.0.34 af 铁律（2026-11-20 山煤国际事故）：同一参数同一源
# ---------------------------------------------------------------------------
def test_batch_hit_af_missing_no_anchor_fill(tmp_path, monkeypatch):
    """EM 命中但无除权事件 → af 沿用湖内 level（1.0），绝不写 NULL 洞
    （NULL 行隔断 af_rebase 检测对 → 护栏失明，山煤事故实锤；
    tushare 备源 2026-11-26 已移除，无备源概念）。"""
    import lake.ingest.em_batch_ingest as emb

    db = str(tmp_path / "b6.duckdb")
    codes = ["sh.600001"]
    _seed(db, codes)   # 历史 af 恒 1.0
    con = lconn.open(db)
    snap = {c: {"total_mv_yi": 100.0, "float_mv_yi": 80.0, "pe_ttm": 5.0,
                "pb": 0.8, "turnover": 1.0} for c in codes}
    _fake_tencent(monkeypatch, snap)
    monkeypatch.setattr(drv, "_now_beijing_dt",
                        lambda: dt.datetime(2026, 9, 18, 21, 30))
    monkeypatch.setattr(emb, "fetch_clist_snapshot",
                        lambda allow_sg=True: [_em_snap_row()])
    # 无除权事件 → derive_af_batch 沿用前一日 level（09-17 播种 af=1.0）
    monkeypatch.setattr(emb, "fetch_ex_events", lambda d: [])
    r = drv.run_incremental(con, db, codes, days=3)
    assert r["batch"]["channel"] == "em", r.get("batch")
    row = con.execute(
        "SELECT adj_factor, source FROM kline_daily "
        "WHERE ts_code='sh.600001' AND date='2026-09-18'").fetchone()
    # af = 湖内 level 1.0（沿用），非 NULL
    assert row == (1.0, "em_batch"), row
    con.close()


def test_per_ticker_sina_derived_af_not_written(tmp_path, monkeypatch):
    """逐只 fallback：sina 源私有推导 af（hfq÷raw 任意常数）不落库——
    写湖=无锚点填充沿用湖内 level（2026-10-26 sh.600018 / 2026-11-20
    山煤国际事故同根因：源私有归一化混写 → 换源日假断崖）。"""
    monkeypatch.delenv("LAKE_MULTISOURCE", raising=False)  # 多源开启（conftest 默认 0）
    db = str(tmp_path / "b7.duckdb")
    codes = ["sh.600001"]
    _seed(db, codes)   # 历史 af 恒 1.0
    con = lconn.open(db)
    snap = {c: {"total_mv_yi": 100.0, "float_mv_yi": 80.0, "pe_ttm": 5.0,
                "pb": 0.8, "turnover": 1.0} for c in codes}
    _fake_tencent(monkeypatch, snap)
    # EM 批量失败 → 逐只（fixture 的 EM 离线 guard：被调即抛 EmBatchError）
    import lake.ingest.source_pool as sp
    sina = sp.get_adapter("sina")

    def fake_sina_kline(code, start=None, end=None):
        # 源私有锚点 9.99（与湖内 1.0 相差 9.9 倍——旧逻辑会原样落库造假断崖）
        return {"ohlcv": [{"date": "2026-09-18", "open": 10.1, "high": 10.3,
                           "low": 10.0, "close": 10.25,
                           "volume": 12345, "amount": 100000.0}],
                "adj_factor": {"2026-09-18": 9.99}}

    monkeypatch.setattr(sina, "available", lambda: True)
    monkeypatch.setattr(sina, "fetch_kline", fake_sina_kline)
    r = drv.run_incremental(con, db, codes, days=3)
    row = con.execute(
        "SELECT adj_factor, source FROM kline_daily "
        "WHERE ts_code='sh.600001' AND date='2026-09-18'").fetchone()
    # af = 湖内 level 1.0（无锚点填充），绝不=sina 私有推导 9.99
    assert row == (1.0, "sina"), row
    assert r["t2"]["processed"] == 1
    con.close()
