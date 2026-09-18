# -*- coding: utf-8 -*-
"""test_lake_v614 —— v6.1.4 五项优化：O1 market 排序 / O2 P3 增量 / O3 tasks 视图 /
O4 源开关+手动探测 /（O5 前端色板见 vitest）。

纪律（brief 红线）：
- **全离线**——tmp 库 + tmp yaml + monkeypatch 路径；fake fetcher 计数断言零真实网络；
  绝不触碰 data/lake/ 生产库、config/strategy.yaml 生产配置。
- conftest autouse 已置 LAKE_MULTISOURCE=0（resolve_source→[] → legacy 单源路径，
  _run_bs_probe/_probe_all_adapters 同门控跳过）——本文件沿用该隔离契约。
- O2 幂等键 = per-table+code+运行日（``inc:YYYY-MM-DD`` / T3=日期本身）；
  _today_beijing monkeypatch 固定日期 → done 键确定可断言。

规格对照：
- **O1** ``/market?sort=<白名单>&order=asc|desc``：非法 sort/order → 400（不降级——
  排序列是 SQL 注入面）；ORDER BY 走 SQL NULLS LAST；响应追加 ``"sort":{column,order}``。
- **O2** ``incremental`` 子命令：kline_daily 最近缺口（date_max+1→今日；无行近 250 日）、
  valuation_daily 腾讯最新快照、index_daily 四指数近 N 日；同日重跑全 skipped_done
  零重复取数；tasks 占位条目刷成真实 total/done。
- **O3** tasks 视图刷新（incremental/status 路径）：kline_history 完成态如实标 done
  （不依赖已死进程自报）、T5 fundamentals_quarterly 加 pending 条目、
  T6 holders_snapshot state="no_source"。
- **O4** ``POST /sources/toggle``（写 tmp yaml，.bak 备份 + ruamel roundtrip 保注释）
  + ``POST /sources/probe``（fake _probe_one_source；15s 超时纪律 → "probe timeout"）。
"""
from __future__ import annotations

import json
import os
import sys

import pytest

duckdb = pytest.importorskip("duckdb")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import lake.web_api as wapi  # noqa: E402
from lake import backfill as lb  # noqa: E402
from lake import conn as lconn  # noqa: E402
from lake.ingest.tencent_ingest import INDEX_CODES  # noqa: E402

import lake_backfill as drv  # noqa: E402

# 固定"今日"（done 键 inc:<date> / T3=<date> 确定可断言）
FIXED_TODAY = "2026-09-18"


@pytest.fixture(autouse=True)
def _v614_isolate(tmp_path, monkeypatch):
    """路径全隔离：progress/health/yaml 一律 tmp（绝不落生产 data/lake/、config/）。"""
    import lake.config as lconfig

    monkeypatch.setattr(drv, "_today_beijing", lambda: FIXED_TODAY)
    # O2 worker 尾 0.3s 限速小睡 → 零等待（离线单测提速；生产行为不变）。
    # ⚠️ 超时用例的 hang 用 threading.Event.wait（不经 time.sleep）——与本 patch 正交。
    monkeypatch.setattr(drv.time, "sleep", lambda s: None)
    # QuotaGuard 状态文件隔离（_quota_state 读它；v603 同口径 patch 双保险）
    monkeypatch.setattr(lb.BackfillRunner, "_quota_state", lambda self: (False, 0))
    # tmp yaml（O4 toggle + lake_cfg 现读现用断言共用；conftest 已隔离 raw/canonical）
    yaml_path = str(tmp_path / "strategy.yaml")
    with open(yaml_path, "w", encoding="utf-8") as f:
        f.write("lake:\n"
                "  # v6.1 源开关（注释须 roundtrip 保留）\n"
                "  sina_enabled: true\n"
                "  tencent_enabled: true\n"
                "  baostock_probe_enabled: true\n"
                "  tdx_enabled: true\n"
                "  adata_f10_enabled: true\n")
    monkeypatch.setattr(lconfig, "strategy_yaml_path", lambda: yaml_path)
    # progress/health 路径（web_api._source_health_path 经 lconn.progress_path 派生）
    monkeypatch.setattr(lb, "_progress_path",
                        lambda: str(tmp_path / "prog" / "backfill_progress.json"))
    monkeypatch.setattr(lconn, "progress_path",
                        lambda: str(tmp_path / "prog" / "backfill_progress.json"))
    yield


# ===========================================================================
# helpers
# ===========================================================================
def _seed_master(db_path: str, codes) -> None:
    """T1 stock_master 播种（delist_date=NULL → universe 全集）。"""
    con = lconn.open(db_path)
    con.executemany(
        "INSERT INTO stock_master (ts_code,name,industry_csric2,board,is_st,"
        "source,fetched_at,data_version) VALUES (?,?,?,?,0,'t','2026-09-15 09:00:00','v6.1')",
        [(c, f"股{c}", "J66", "主板") for c in codes])
    con.close()


def _seed_kline(db_path: str, rows) -> None:
    """kline_daily 播种（rows=[(ts_code,date,close)]；12 列与 DDL 对齐）。"""
    con = lconn.open(db_path)
    con.executemany(
        "INSERT INTO kline_daily (ts_code,date,open,high,low,\"close\",volume,"
        "amount,is_st,source,fetched_at,data_version) VALUES (?,?,?,?,?,?,?,?,?,'t','2026-09-15 09:00:00','v6.1')",
        [(c, d, cl, cl, cl, cl, 1000, 10000.0, 0) for c, d, cl in rows])
    con.close()


def _fake_tencent(monkeypatch, kline_rows, snap=None):
    """monkeypatch tencent_ingest 取数函数 → fake（计数断言零网络）。

    :return: counter dict（kline/snapshot 调用计数）
    """
    import lake.ingest.tencent_ingest as ti

    counter = {"kline": 0, "snapshot": 0}

    def fake_kline(client, ts_code, n):
        counter["kline"] += 1
        return [dict(r) for r in kline_rows]

    snap = snap or {}

    def fake_snapshot(client, ts_codes):
        counter["snapshot"] += 1
        out = {}
        for c in ts_codes:
            if c in snap:
                out[c] = dict(snap[c])
        return out

    monkeypatch.setattr(ti, "fetch_kline_ohlcv", fake_kline)
    monkeypatch.setattr(ti, "fetch_snapshot", fake_snapshot)
    return counter


def _no_tdx(monkeypatch):
    """tdx adapter available() → False（防 EU 自检网络调用；离线契约）。"""
    from lake.ingest import source_pool as sp

    ad = sp.get_adapter("tdx")
    if ad is not None:
        monkeypatch.setattr(ad, "available", lambda: False)


def _prog_file() -> str:
    """本测试 progress 文件路径（autouse fixture 已把 lb._progress_path 指到 tmp/prog/）。"""
    return lb._progress_path()


def _read_prog() -> dict:
    """读 progress 文件（BackfillRunner(db_path=tmp) 经 patch 后的 _progress_path 落点）。"""
    with open(_prog_file(), encoding="utf-8") as f:
        return json.load(f)


def _task_entry(prog: dict, table: str):
    return next((t for t in prog["tasks"] if t.get("table") == table), None)


# ===========================================================================
# O1：/market 排序（白名单 + order + NULLS LAST + sort 回显）
# ===========================================================================
def _seed_market_sort_db(db_path: str) -> None:
    """T1×3 + T2 最新行（close/volume/amount/date）+ T3 快照（pe_ttm/pb）。

    sh.600001 close=10 / pe=5；sh.600002 close=8 / pe=9；sz.000003 **无 K线**
    （close/volume/amount/date=NULL → NULLS LAST 断言）。
    """
    con = lconn.open(db_path)
    con.executemany(
        "INSERT INTO stock_master (ts_code,name,industry_csric2,board,is_st,"
        "source,fetched_at,data_version) VALUES (?,?,?,?,0,'t','2026-09-15 09:00:00','v6.1')",
        [("sh.600001", "甲银行", "J66", "主板"),
         ("sh.600002", "乙地产", "J70", "主板"),
         ("sz.000003", "丙科技", "I65", "主板")])
    con.executemany(
        "INSERT INTO kline_daily (ts_code,date,open,high,low,\"close\",volume,"
        "amount,source,fetched_at,data_version) VALUES (?,?,?,?,?,?,?,?,'t','2026-09-15 09:00:00','v6.1')",
        [("sh.600001", "2026-09-17", 9, 10.5, 8.5, 10.0, 10000, 100000.0),
         ("sh.600002", "2026-09-17", 7, 8.5, 7.5, 8.0, 20000, 160000.0)])
    con.executemany(
        "INSERT INTO valuation_daily (ts_code,date,total_mv,float_mv,pe_ttm,pb,"
        "source,fetched_at,data_version) VALUES (?,?,?,?,?,?,'t','2026-09-15 09:00:00','v6.1')",
        [("sh.600001", "2026-09-17", 100.0, 80.0, 5.0, 0.8),
         ("sh.600002", "2026-09-17", 50.0, 40.0, 9.0, 1.2)])
    con.close()


def _market(tmp_path, monkeypatch, **kw):
    db = str(tmp_path / f"mkt_{len(list(tmp_path.iterdir()))}.duckdb")
    _seed_market_sort_db(db)
    monkeypatch.setattr(lconn, "default_db_path", lambda: db)
    return wapi.market(**kw)


def test_o1_market_sort_close_desc_and_echo(tmp_path, monkeypatch):
    d = _market(tmp_path, monkeypatch, sort="close", order="desc")
    assert [r["ts_code"] for r in d["rows"]] == ["sh.600001", "sh.600002", "sz.000003"]
    assert d["sort"] == {"column": "close", "order": "desc"}


def test_o1_market_sort_asc_and_nulls_last(tmp_path, monkeypatch):
    """asc 时 NULL（无 K线股）仍排最后——brief 逐字"NULLS LAST"（双向）。"""
    d = _market(tmp_path, monkeypatch, sort="close", order="asc")
    assert [r["ts_code"] for r in d["rows"]] == ["sh.600002", "sh.600001", "sz.000003"]
    assert d["sort"] == {"column": "close", "order": "asc"}


def test_o1_market_sort_pe_ttm_and_date(tmp_path, monkeypatch):
    d = _market(tmp_path, monkeypatch, sort="pe_ttm", order="desc")
    assert [r["ts_code"] for r in d["rows"]] == ["sh.600002", "sh.600001", "sz.000003"]
    d2 = _market(tmp_path, monkeypatch, sort="date", order="asc")
    assert [r["ts_code"] for r in d2["rows"]] == ["sh.600001", "sh.600002", "sz.000003"]


def test_o1_market_sort_default_code_desc(tmp_path, monkeypatch):
    """缺省 sort=code（白名单不含旧 total_mv → 稳定列）+ order=desc。"""
    d = _market(tmp_path, monkeypatch)
    assert [r["ts_code"] for r in d["rows"]] == ["sz.000003", "sh.600002", "sh.600001"]
    assert d["sort"] == {"column": "code", "order": "desc"}


@pytest.mark.parametrize("bad_sort", ["total_mv", "ttm_yield_pct", "", "close;DROP TABLE x"])
def test_o1_market_bad_sort_400(tmp_path, monkeypatch, bad_sort):
    """白名单外（含旧值/空串/注入串）→ 400，不猜不降级。"""
    _seed_market_sort_db(str(tmp_path / "mktb.duckdb"))
    monkeypatch.setattr(lconn, "default_db_path", lambda: str(tmp_path / "mktb.duckdb"))
    with pytest.raises(wapi.HTTPException) as ei:
        wapi.market(sort=bad_sort)
    assert ei.value.status_code == 400


@pytest.mark.parametrize("bad_order", ["up", "ASC", ""])
def test_o1_market_bad_order_400(tmp_path, monkeypatch, bad_order):
    _seed_market_sort_db(str(tmp_path / "mkto.duckdb"))
    monkeypatch.setattr(lconn, "default_db_path", lambda: str(tmp_path / "mkto.duckdb"))
    with pytest.raises(wapi.HTTPException) as ei:
        wapi.market(order=bad_order)
    assert ei.value.status_code == 400


def test_o1_market_sort_with_industry_filter(tmp_path, monkeypatch):
    """排序与既有 industry 过滤正交（WHERE + ORDER BY 同查询）。"""
    d = _market(tmp_path, monkeypatch, industry="J66", sort="close", order="desc")
    assert [r["ts_code"] for r in d["rows"]] == ["sh.600001"]
    assert d["total"] == 1


# ===========================================================================
# O2：incremental（P3 每日增量）——gap 计算 / 全量跑 / 幂等重跑 / tasks 视图
# ===========================================================================
def test_o2_kline_gap_start_date_max_plus_one_and_fallback(tmp_path):
    """T2 缺口起点：有行 → date_max+1；无行 → 近 250 自然日（fallback）。"""
    import datetime as dt

    db = str(tmp_path / "gap.duckdb")
    _seed_master(db, ["sh.600001", "sz.000002"])
    _seed_kline(db, [("sh.600001", "2026-09-14", 10.0)])
    con = lconn.open(db)
    assert drv._kline_gap_start(con, "sh.600001", 250) == "2026-09-15"
    fb = drv._kline_gap_start(con, "sz.000002", 250)
    expected_fb = (dt.date.today() - dt.timedelta(days=250)).isoformat()
    assert fb == expected_fb, f"无行股应回退近 250 日: {fb} != {expected_fb}"
    con.close()


def test_o2_incremental_full_run_and_idempotent_rerun(tmp_path):
    """全量跑（3 股 + 4 指数）→ 库内行数正确；同日重跑 → 全 skipped_done、
    零重复取数（fake 计数）、行数不翻倍（upsert 幂等）。"""
    db = str(tmp_path / "inc.duckdb")
    codes = ["sh.600001", "sz.000002", "bj.430047"]
    _seed_master(db, codes)
    con = lconn.open(db)

    kl_rows = [{"date": d, "open": 10.0, "high": 11.0, "low": 9.5,
                "close": 10.5, "volume": 100.0} for d in
               ("2026-09-16", "2026-09-17", "2026-09-18")]
    snaps = {c: {"total_mv_yi": 100.0, "float_mv_yi": 80.0, "pe_ttm": 5.0,
                 "pb": 0.8, "turnover": 1.0} for c in codes}
    mp = pytest.MonkeyPatch()
    counter = _fake_tencent(mp, kl_rows, snaps)
    _no_tdx(mp)

    # ---- Run 1：首次 ----
    r1 = drv.run_incremental(con, db, codes, days=3)
    assert r1["t2"]["processed"] == 3 and r1["t2"]["skipped_done"] == 0
    assert r1["t3"]["processed"] == 3
    assert r1["t7"]["processed"] == len(INDEX_CODES)
    # T2：每股 3 日（fake 全量返回、缺口窗口 [start..今日] 全含）
    n_kl = con.execute("SELECT COUNT(*) FROM kline_daily").fetchone()[0]
    assert n_kl == 3 * 3, f"kline_daily 应 9 行: {n_kl}"
    # T3：每股 1 快照（date=今日）
    n_v = con.execute("SELECT COUNT(*) FROM valuation_daily").fetchone()[0]
    assert n_v == 3
    d_v = con.execute("SELECT DISTINCT date FROM valuation_daily").fetchall()
    assert [str(x[0]) for x in d_v] == [FIXED_TODAY]
    # T7：4 指数 × 3 日
    n_i = con.execute("SELECT COUNT(*) FROM index_daily").fetchone()[0]
    assert n_i == len(INDEX_CODES) * 3

    # ---- Run 2：同日重跑 → 全跳过、零重复取数 ----
    counter["kline"] = 0
    counter["snapshot"] = 0
    r2 = drv.run_incremental(con, db, codes, days=3)
    assert r2["t2"]["skipped_done"] == 3 and r2["t2"]["processed"] == 0
    assert r2["t3"]["skipped_done"] == 3 and r2["t3"]["processed"] == 0
    assert r2["t7"]["skipped_done"] == len(INDEX_CODES) and r2["t7"]["processed"] == 0
    # T2/T7 零重取；T3 仅前置探测 1 次（worker 全跳过）
    assert counter["kline"] == 0, f"Run2 T2/T7 应零取数: {counter}"
    assert counter["snapshot"] == 1, f"Run2 T3 仅前置探测: {counter}"
    # upsert 幂等：行数不翻倍
    assert con.execute("SELECT COUNT(*) FROM kline_daily").fetchone()[0] == n_kl
    assert con.execute("SELECT COUNT(*) FROM index_daily").fetchone()[0] == n_i
    con.close()


def test_o2_incremental_gap_window_only_new_rows(tmp_path):
    """T2 缺口窗口化：库内已有 09-14，fake 返回 09-13..09-18 → 只灌 [09-15..今日]。"""
    db = str(tmp_path / "incw.duckdb")
    codes = ["sh.600001"]
    _seed_master(db, codes)
    _seed_kline(db, [("sh.600001", "2026-09-14", 9.0)])
    con = lconn.open(db)

    kl_rows = [{"date": d, "open": 10.0, "high": 11.0, "low": 9.5,
                "close": 10.5, "volume": 100.0} for d in
               ("2026-09-13", "2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17")]
    mp = pytest.MonkeyPatch()
    _fake_tencent(mp, kl_rows, {codes[0]: {"pe_ttm": 5.0}})
    mp.setattr(drv.time, "sleep", lambda s: None)
    _no_tdx(mp)

    drv.run_incremental(con, db, codes, days=3)
    dates = sorted(str(x[0]) for x in con.execute(
        "SELECT date FROM kline_daily WHERE ts_code='sh.600001'").fetchall())
    # 09-14 既有 + 缺口 [09-15, 09-16, 09-17]（fake 无 09-18）；09-13 早于 start 不入
    assert dates == ["2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17"], dates
    con.close()


def test_o2_incremental_t3_all_empty_aborts(tmp_path):
    """T3 快照全空（腾讯接口故障）→ 抛错中止，不标 done（防假完成毒化）。"""
    db = str(tmp_path / "inct3.duckdb")
    codes = ["sh.600001"]
    _seed_master(db, codes)
    con = lconn.open(db)
    mp = pytest.MonkeyPatch()
    # kline 正常返回、snapshot 恒空 → T2 完成、T3 前置探测全空 → RuntimeError
    _fake_tencent(mp, [{"date": "2026-09-18", "open": 1.0, "high": 1.0,
                        "low": 1.0, "close": 1.0, "volume": 1.0}], snap={})
    mp.setattr(drv.time, "sleep", lambda s: None)
    _no_tdx(mp)
    with pytest.raises(RuntimeError, match="T3 估值快照全空"):
        drv.run_incremental(con, db, codes, days=3)
    # T2 done 键已落（该表确实完成）；T3 无 done 键（下轮重试）
    prog = _read_prog()
    assert any(k[0] == "kline_daily" and k[1] == codes[0] for k in prog.get("done", []))
    assert not any(k[0] == "valuation_daily" for k in prog.get("done", []))
    con.close()


def test_o2_incremental_tasks_view_real_totals(tmp_path):
    """占位 tasks 刷成真实 total/done：kline_daily/valuation_daily total=universe 行数、
    index_daily total=4；--codes 子集时 done=子集数（state 不谎报 done）。"""
    db = str(tmp_path / "inctv.duckdb")
    codes = ["sh.600001", "sz.000002"]   # universe=3，只灌 2 只
    _seed_master(db, codes + ["bj.430047"])
    con = lconn.open(db)
    mp = pytest.MonkeyPatch()
    kl_rows = [{"date": "2026-09-18", "open": 1.0, "high": 1.0, "low": 1.0,
                "close": 1.0, "volume": 1.0}]
    snaps = {c: {"pe_ttm": 5.0} for c in codes}
    _fake_tencent(mp, kl_rows, snaps)
    mp.setattr(drv.time, "sleep", lambda s: None)
    _no_tdx(mp)

    drv.run_incremental(con, db, codes, days=3)
    prog = _read_prog()
    e2 = _task_entry(prog, "kline_daily")
    assert (e2["total"], e2["done"]) == (3, 2), f"kline_daily 应 total=3 done=2: {e2}"
    assert e2["state"] != "done", "子集灌数不得谎报整表 done"
    ev = _task_entry(prog, "valuation_daily")
    assert (ev["total"], ev["done"]) == (3, 2)
    ei = _task_entry(prog, "index_daily")
    assert (ei["total"], ei["done"]) == (len(INDEX_CODES), len(INDEX_CODES))
    assert ei["state"] == "done"
    con.close()


# ===========================================================================
# O3：tasks 视图——kline_history 完成态 + T5 pending + T6 no_source
# ===========================================================================
def _seed_stuck_history_progress(codes) -> None:
    """模拟生产实况：history 长跑被杀 → kline_history entry 停在 state=running，
    done 明细齐全（universe 全量）。progress 落 patch 后的 _progress_path（tmp/prog/）。"""
    import json as _json

    p = lb._progress_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    n = len(codes)
    data = {"updated_at": None,
            "tasks": [{"table": "kline_history", "tier": "P2", "total": n, "done": n,
                       "quota_used_today": 0, "quota_budget": 5000,
                       "state": "running", "eta_min": None, "last_error": ""}],
            "coverage": {},
            "done": [["kline_history", c, "full_history"] for c in codes]}
    with open(p, "w", encoding="utf-8") as f:
        _json.dump(data, f, ensure_ascii=False)


def test_o3_refresh_marks_kline_history_done_and_adds_t5_t6(tmp_path):
    """incremental 收尾刷新：kline_history running→done（不依赖已死进程自报）；
    T5 fundamentals_quarterly 加 pending 条目（备注 history --t5）；
    T6 holders_snapshot state=no_source。"""
    db = str(tmp_path / "o3.duckdb")
    codes = ["sh.600001", "sz.000002"]
    _seed_master(db, codes)
    _seed_stuck_history_progress(codes)

    con = lconn.open(db)
    mp = pytest.MonkeyPatch()
    kl_rows = [{"date": "2026-09-18", "open": 1.0, "high": 1.0, "low": 1.0,
                "close": 1.0, "volume": 1.0}]
    _fake_tencent(mp, kl_rows, {c: {"pe_ttm": 5.0} for c in codes})
    mp.setattr(drv.time, "sleep", lambda s: None)
    _no_tdx(mp)

    drv.run_incremental(con, db, codes, days=3)
    prog = _read_prog()
    eh = _task_entry(prog, "kline_history")
    assert eh["state"] == "done" and (eh["total"], eh["done"]) == (2, 2), \
        f"kline_history 应如实标 done: {eh}"
    e5 = _task_entry(prog, "fundamentals_quarterly")
    assert e5 is not None and e5["state"] == "pending" and e5["total"] == 2, \
        f"T5 应加 pending 条目: {e5}"
    assert "--t5" in (e5.get("note") or "")
    e6 = _task_entry(prog, "holders_snapshot")
    assert e6 is not None and e6["state"] == "no_source", \
        f"T6 应标 no_source: {e6}"
    con.close()


def test_o3_status_path_refreshes_tasks_view(tmp_path):
    """status 路径同样刷新（能拿到有效 con = 无活跃 writer → 无条件刷新安全）：
    死进程残留的 running entry 被纠正为 done。"""
    db = str(tmp_path / "o3s.duckdb")
    codes = ["sh.600001"]
    _seed_master(db, codes)
    _seed_stuck_history_progress(codes)
    con = lconn.open(db)

    summary = drv.run_status(con, db)
    assert summary["sub"] == "status"
    prog = _read_prog()
    eh = _task_entry(prog, "kline_history")
    assert eh["state"] == "done", f"status 路径应纠正死进程残留: {eh}"
    e6 = _task_entry(prog, "holders_snapshot")
    assert e6 is not None and e6["state"] == "no_source"
    con.close()


# ===========================================================================
# O2 Web：/sync/start mode 参数（history 缺省 / incremental / 非法 400）
# ===========================================================================
def test_o2_sync_start_mode_incremental_passthrough(monkeypatch):
    """mode=incremental → start_sync(sub='incremental')；响应回显 mode。"""
    import lake.sync_control as sc

    captured = {}

    def fake_start(**kw):
        captured.update(kw)
        return {"started": True, "pid": 1234, "log_path": "/tmp/x.log"}

    monkeypatch.setattr(sc, "start_sync", fake_start)
    d = wapi.sync_start(mode="incremental")
    assert captured.get("sub") == "incremental"
    assert d["mode"] == "incremental" and d["started"] is True


def test_o2_sync_start_mode_default_history(monkeypatch):
    """缺省 mode=history（FieldInfo 直调归一化）→ sub='history'（v6.0.9 行为不变）。"""
    import lake.sync_control as sc

    captured = {}

    def fake_start(**kw):
        captured.update(kw)
        return {"started": True, "pid": 1234, "log_path": "/tmp/x.log"}

    monkeypatch.setattr(sc, "start_sync", fake_start)
    d = wapi.sync_start()   # 直调缺省 → mode=FieldInfo → 归一化 history
    assert captured.get("sub") == "history"
    assert d["mode"] == "history"


def test_o2_sync_start_mode_with_codes(monkeypatch):
    """codes + incremental 组合：--codes 透传 extra_args。"""
    import lake.sync_control as sc

    captured = {}

    def fake_start(**kw):
        captured.update(kw)
        return {"started": True, "pid": 1234, "log_path": "/tmp/x.log"}

    monkeypatch.setattr(sc, "start_sync", fake_start)
    wapi.sync_start(codes="sh.601398,sz.000001", mode="incremental")
    assert captured.get("sub") == "incremental"
    assert captured.get("extra_args") == ["--codes", "sh.601398,sz.000001"]


def test_o2_sync_start_bad_mode_400(monkeypatch):
    import lake.sync_control as sc

    monkeypatch.setattr(sc, "start_sync", lambda **k: pytest.fail("非法 mode 不得 spawn"))
    with pytest.raises(wapi.HTTPException) as ei:
        wapi.sync_start(mode="full")
    assert ei.value.status_code == 400


def test_o2_sync_start_running_409_unchanged(monkeypatch):
    """已 running → 409 现状不变（mode 不影响互斥语义）。"""
    import lake.sync_control as sc

    monkeypatch.setattr(
        sc, "start_sync",
        lambda **k: {"started": False, "pid": 777, "log_path": None,
                     "reason": "already_running"})
    with pytest.raises(wapi.LakeSyncConflict) as ei:
        wapi.sync_start(mode="incremental")
    assert ei.value.status_code == 409


# ===========================================================================
# O4：/sources/toggle（写 tmp yaml）+ /sources/probe（fake + 15s 超时）
# ===========================================================================
def test_o4_toggle_writes_yaml_with_backup_and_roundtrip(tmp_path, monkeypatch):
    """toggle sina→false：yaml 目标键改值、注释保留（ruamel roundtrip）、.bak=写前备份。"""
    import lake.config as lconfig

    d = wapi.sources_toggle({"name": "sina", "enabled": False})
    assert d["key"] == "sina_enabled" and d["enabled"] is False
    path = lconfig.strategy_yaml_path()
    bak = path + ".bak"
    assert os.path.exists(bak), "写前必须备份 .bak"
    with open(path, encoding="utf-8") as f:
        after = f.read()
    with open(bak, encoding="utf-8") as f:
        before = f.read()
    assert "sina_enabled: false" in after and "sina_enabled: true" in before
    assert "# v6.1 源开关（注释须 roundtrip 保留）" in after, "ruamel roundtrip 必须保注释"
    # tencent 键（v6.1.4 新增 _DEFAULTS）→ tencent_enabled
    d2 = wapi.sources_toggle({"name": "tencent", "enabled": False})
    assert d2["key"] == "tencent_enabled"
    with open(path, encoding="utf-8") as f:
        assert "tencent_enabled: false" in f.read()


def test_o4_toggle_immediate_effect_via_lake_cfg(tmp_path, monkeypatch):
    """立即生效：lake_cfg() 每次现读文件（无进程级缓存）→ toggle 后新值立即可见。"""
    import lake.config as lconfig

    assert lconfig.lake_cfg()["tdx_enabled"] is True
    wapi.sources_toggle({"name": "tdx", "enabled": False})
    assert lconfig.lake_cfg()["tdx_enabled"] is False, \
        "lake_cfg 现读现用——toggle 后必须立即读到新值"


def test_o4_toggle_baostock_key_name(tmp_path, monkeypatch):
    """baostock → baostock_probe_enabled（与 _DEFAULTS 对齐，非 baostock_enabled）。"""
    import lake.config as lconfig

    d = wapi.sources_toggle({"name": "baostock", "enabled": False})
    assert d["key"] == "baostock_probe_enabled"
    with open(lconfig.strategy_yaml_path(), encoding="utf-8") as f:
        assert "baostock_probe_enabled: false" in f.read()


@pytest.mark.parametrize("payload", [
    {"name": "foo", "enabled": True},       # 非白名单源名
    {"name": "sina", "enabled": "yes"},     # enabled 非 bool
    {"name": "sina"},                       # 缺 enabled
])
def test_o4_toggle_bad_payload_400(tmp_path, monkeypatch, payload):
    with pytest.raises(wapi.HTTPException) as ei:
        wapi.sources_toggle(payload)
    assert ei.value.status_code == 400


def test_o4_probe_results_and_health_file(tmp_path, monkeypatch):
    """probe 单源：fake _probe_one_source 结果透传 + source_health.json 合并落盘
    （复用 _read_source_health 格式；未探测源保留既有记录）。"""
    import lake.config as lconfig

    health = str(tmp_path / "prog" / "source_health.json")
    monkeypatch.setattr(wapi, "_source_health_path", lambda: health)
    # 既有健康文件（tencent 昨日探测）→ 合并后须保留
    os.makedirs(os.path.dirname(health), exist_ok=True)
    with open(health, "w", encoding="utf-8") as f:
        json.dump({"tencent": {"available": True, "probed_at": "2026-09-17 08:00:00",
                               "latency_ms": 120}}, f)

    monkeypatch.setattr(
        wapi, "_probe_one_source",
        lambda name: {"available": True, "probed_at": "2026-09-18 08:00:00",
                      "latency_ms": 350, "detail": ""})

    d = wapi.sources_probe({"names": ["sina"]})
    assert d["results"]["sina"]["available"] is True
    assert d["path"] == health
    with open(health, encoding="utf-8") as f:
        saved = json.load(f)
    assert saved["sina"]["available"] is True
    assert saved["tencent"]["probed_at"] == "2026-09-17 08:00:00", \
        "合并写必须保留未探测源的既有记录"


def test_o4_probe_default_all_and_bad_name_400(tmp_path, monkeypatch):
    import lake.config as lconfig

    calls = []
    monkeypatch.setattr(wapi, "_source_health_path",
                        lambda: str(tmp_path / "prog" / "sh.json"))
    monkeypatch.setattr(
        wapi, "_probe_one_source",
        lambda name: (calls.append(name), {"available": False, "probed_at": None,
                                           "latency_ms": None, "detail": ""})[1])
    d = wapi.sources_probe({})   # 缺省 = 全部 5 源
    assert calls == ["sina", "tencent", "baostock", "tdx", "adata_f10"]
    with pytest.raises(wapi.HTTPException) as ei:
        wapi.sources_probe({"names": ["sina", "nope"]})
    assert ei.value.status_code == 400


def test_o4_probe_timeout_15s_isolation(tmp_path, monkeypatch):
    """超时纪律：单源 hang >15s → 请求不拖死，记 available=false detail='probe timeout'。

    hang 用 threading.Event.wait（**不经 time.sleep**——autouse fixture 把 drv.time.sleep
    patch 成 no-op 提速 O2 用例；Event.wait 是独立 C 路径，patch 不到 → 真阻塞）。
    """
    import threading as _th
    import time as _time

    monkeypatch.setattr(wapi, "_source_health_path",
                        lambda: str(tmp_path / "prog" / "sh.json"))
    monkeypatch.setattr(wapi, "_PROBE_TIMEOUT_S", 0.5)   # 测试提速（生产 15s）

    never = _th.Event()

    def hang(name):
        never.wait(3)   # 真阻塞（Event.wait 不被 time.sleep patch 影响）
        return {"available": True, "probed_at": None, "latency_ms": None, "detail": ""}

    monkeypatch.setattr(wapi, "_probe_one_source", hang)
    t0 = _time.monotonic()
    d = wapi.sources_probe({"names": ["baostock"]})
    elapsed = _time.monotonic() - t0
    assert elapsed < 2.0, f"hang 源不得拖死请求（{elapsed:.1f}s）"
    r = d["results"]["baostock"]
    assert r["available"] is False and r["detail"] == "probe timeout"
