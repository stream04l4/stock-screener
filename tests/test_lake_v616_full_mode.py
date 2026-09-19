# -*- coding: utf-8 -*-
"""test_lake_v616_full_mode —— v6.1.6 单按钮全量补齐（full 模式）离线单测。

覆盖 brief §A/§C：
- **full 三阶段顺序执行**（fake adapters + tmp 库 + tmp progress，零网络零 spawn）：
  history → P3 增量 → T5，段标日志顺序正确、各表数据落库；
- **幂等跳过**：done keys 全在 → 三阶段 skipped_done=total、零重取（fake 计数）；
- **单阶段 error 不阻断后续**：history 失败 → kline_history state=error + last_error，
  P3/T5 照常跑完且各自 state 正常（收尾视图刷新后 error 标记不被冲掉）；
- **phase 字段流转**（/status locked 态）：sync.log 段标回放 → phase=history/p3/t5；
  阶段结束 → None（键不出现）；非 full 进程（无段标）→ 无 phase 键（三态契约不动）；
- **/sync/start 缺省=full、409 互斥不变**（mock start_sync，零 spawn）。

纪律：全离线——沿用 v614 隔离契约（conftest autouse LAKE_MULTISOURCE=0 → legacy
单源路径），fake 打在模块级取数函数上（tencent_ingest/baostock_ingest +
BaoStockClient/TencentClient 构造替身）；T5 的 adata F10 主源经 mock adapter
注入注册表（run_t5 直接 get_adapter("adata_f10")，不走 resolve_source）。
库一律 tmp_path，不碰 data/lake/、不碰生产灌数进程。
"""
from __future__ import annotations

import json
import os
import sys

import pytest

duckdb = pytest.importorskip("duckdb")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
for _p in (REPO_ROOT, SCRIPTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import lake.web_api as wapi  # noqa: E402
from lake import backfill as lb  # noqa: E402
from lake import conn as lconn  # noqa: E402
from lake.ingest.tencent_ingest import INDEX_CODES  # noqa: E402

import lake_backfill as drv  # noqa: E402

FIXED_TODAY = "2026-09-18"


# ===========================================================================
# mock adata F10 adapter（T5 主源；run_t5 直接 get_adapter，不走 resolve_source）
# ===========================================================================
class _MockAdata:
    """adata_f10 adapter mock：fetch_f10 返回 _f10_recs()（或抛错），计数调用。"""

    def __init__(self, fail=False):
        self._fail = fail
        self.f10_calls = 0

    def available(self):
        return True

    def fetch_f10(self, ts_code):
        self.f10_calls += 1
        if self._fail:
            raise RuntimeError(f"T5 adata F10 故障（测试注入）{ts_code}")
        return _f10_recs()


def _set_registry(monkeypatch, mapping):
    from lake.ingest import source_pool as sp

    monkeypatch.setattr(sp, "_REGISTRY", dict(mapping))


# ===========================================================================
# fixture：路径全隔离 + 离线门控（v614 autouse 同口径）
# ===========================================================================
@pytest.fixture(autouse=True)
def _v616_isolate(tmp_path, monkeypatch):
    import lake.config as lconfig

    monkeypatch.setattr(drv, "_today_beijing", lambda: FIXED_TODAY)
    # worker 尾 0.3s 限速小睡 → 零等待（离线单测提速；生产行为不变）
    monkeypatch.setattr(drv.time, "sleep", lambda s: None)
    # QuotaGuard 状态文件隔离（v603/v614 同口径 patch 双保险）
    monkeypatch.setattr(lb.BackfillRunner, "_quota_state", lambda self: (False, 0))
    yaml_path = str(tmp_path / "strategy.yaml")
    with open(yaml_path, "w", encoding="utf-8") as f:
        f.write("lake:\n"
                "  sina_enabled: true\n"
                "  tencent_enabled: true\n"
                "  baostock_probe_enabled: false\n"   # Q6 探测关（离线契约，零网络）
                "  tdx_enabled: true\n"
                "  adata_f10_enabled: true\n")
    monkeypatch.setattr(lconfig, "strategy_yaml_path", lambda: yaml_path)
    monkeypatch.setattr(lb, "_progress_path",
                        lambda: str(tmp_path / "prog" / "backfill_progress.json"))
    monkeypatch.setattr(lconn, "progress_path",
                        lambda: str(tmp_path / "prog" / "backfill_progress.json"))
    # conftest autouse 已置 LAKE_MULTISOURCE=0（legacy 单源路径）——本文件沿用，不删。
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


def _kl_rows():
    return [{"date": d, "open": 10.0, "high": 11.0, "low": 9.5,
             "close": 10.5, "volume": 100.0}
            for d in ("2026-09-16", "2026-09-17", "2026-09-18")]


def _f10_recs():
    return [{"period": "2026Q1", "pub_date": "2026-04-30", "roe_weighted": 5.0,
             "gross_margin": 30.0, "liability_pct": 40.0, "yoy_pni": 8.0,
             "npi": 1e9, "ocf": None}]


def _holders_periods():
    """T6 fake periods（parse_holders_page 结构）：固定 2 报告期 × 3 股东。

    load_t6 逐期展开 → 每股 6 行；字段名与真实 SinaClient.fetch_holders 输出一致
    （holder_rank/holder_name/hold_shares/circ_ratio_pct/share_nature）。
    """
    def _holders(ratio_base):
        return [
            {"holder_rank": 1, "holder_name": f"股东A{ratio_base}",
             "hold_shares": 1000.0, "circ_ratio_pct": ratio_base, "share_nature": "国有股"},
            {"holder_rank": 2, "holder_name": f"股东B{ratio_base}",
             "hold_shares": 500.0, "circ_ratio_pct": round(ratio_base / 2, 4),
             "share_nature": None},
            {"holder_rank": 3, "holder_name": f"股东C{ratio_base}",
             "hold_shares": 200.0, "circ_ratio_pct": round(ratio_base / 5, 4),
             "share_nature": "境内法人股"},
        ]

    return [
        {"end_date": "2026-03-31", "notice_date": "2026-04-20", "holders": _holders(12.5)},
        {"end_date": "2026-06-30", "notice_date": "2026-07-15", "holders": _holders(11.0)},
    ]


def _wire_fakes(monkeypatch, codes, hist_kline_fail=False, t5_fail=False,
                t6_fail_codes=()):
    """离线 fake（v614/v607 同模式）：legacy 路径模块级取数函数 + T5 mock adapter
    + v6.1.7 T6 SinaClient 构造替身。

    - history legacy：ti.fetch_kline_full_history（全史）+ bsi.fetch_adjust_factor
      （adj，空行=无除权，合法）；BaoStockClient/TencentClient 构造替身（零网络）。
    - P3 legacy：ti.fetch_kline_ohlcv（T2 窗口/T7 指数共用）+ ti.fetch_snapshot（T3）。
    - T5：mock adata_f10 adapter 注入注册表（run_t5 直接 get_adapter）。
    - T6（v6.1.7）：drv._t6_client_factory → fake SinaClient（fetch_holders 返回
      _holders_periods()，固定 2 报告期×3 股东；t6_fail_codes 内的 code6 抛错——
      单只失败不阻断后续用例用）。

    :return: counter dict（full_history/kline/snapshot/f10/holders 调用计数，可清零重跑）。
    """
    import lake.ingest.tencent_ingest as ti
    import lake.ingest.baostock_ingest as bsi
    import screener.data.baostock_client as bsc
    import screener.data.tencent as tmod

    counter = {"full_history": 0, "kline": 0, "snapshot": 0, "holders": {}}
    adata = _MockAdata(fail=t5_fail)

    def fake_full_history(client, ts_code, page_size=2000):
        counter["full_history"] += 1
        if hist_kline_fail:
            raise RuntimeError(f"腾讯K线全史故障（测试注入）{ts_code}")
        return [dict(r) for r in _kl_rows()]

    def fake_kline(client, ts_code, n):
        counter["kline"] += 1
        if hist_kline_fail:
            raise RuntimeError(f"腾讯K线窗口故障（测试注入）{ts_code}")
        return [dict(r) for r in _kl_rows()]

    snaps = {c: {"total_mv_yi": 100.0, "float_mv_yi": 80.0, "pe_ttm": 5.0,
                 "pb": 0.8, "turnover": 1.0} for c in codes}

    def fake_snapshot(client, ts_codes):
        counter["snapshot"] += 1
        return {c: dict(snaps[c]) for c in ts_codes if c in snaps}

    class _FakeBS:   # v6.0.10：driver 构造传 stop_checker=（依赖注入）→ fake 接受 kwargs
        def __init__(self, *a, **k):
            pass

        def close(self):
            pass

    class _FakeTClient:
        pass

    class _FakeSina:   # v6.1.7：T6 SinaClient 替身（fetch_holders 固定 periods；零网络）
        def __init__(self, *a, **k):
            pass

        def fetch_holders(self, code6):
            counter["holders"][code6] = counter["holders"].get(code6, 0) + 1
            if code6 in t6_fail_codes:
                raise RuntimeError(f"新浪 F10 股东页故障（测试注入）{code6}")
            return [dict(p) for p in _holders_periods()]

    monkeypatch.setattr(ti, "fetch_kline_full_history", fake_full_history)
    monkeypatch.setattr(ti, "fetch_kline_ohlcv", fake_kline)
    monkeypatch.setattr(ti, "fetch_snapshot", fake_snapshot)
    monkeypatch.setattr(bsi, "fetch_adjust_factor",
                        lambda bs, code, s, e: (["code", "adjustFactor"], []))
    monkeypatch.setattr(bsc, "BaoStockClient", _FakeBS)
    monkeypatch.setattr(tmod, "TencentClient", _FakeTClient)
    monkeypatch.setattr(drv, "_t6_client_factory", lambda: _FakeSina())
    _set_registry(monkeypatch, {"adata_f10": adata})
    counter["f10"] = 0   # 占位（真实计数在 adata.f10_calls）
    return counter


def _read_prog() -> dict:
    with open(lb._progress_path(), encoding="utf-8") as f:
        return json.load(f)


def _task_entry(prog: dict, table: str):
    return next((t for t in prog["tasks"] if t.get("table") == table), None)


# ===========================================================================
# 1) full 三阶段顺序执行 + 数据落库
# ===========================================================================
def test_full_three_phases_sequential_and_data_landed(tmp_path, monkeypatch, capsys):
    """full → history→P3→T5→T6 顺序跑完（v6.1.7 四阶段）：summary.phases 顺序正确、各表行数正确。"""
    db = str(tmp_path / "full.duckdb")
    codes = ["sh.600001", "sz.000002"]
    _seed_master(db, codes)
    con = lconn.open(db)
    counter = _wire_fakes(monkeypatch, codes)

    summary = drv.run_full(con, db, codes, "1990-01-01", FIXED_TODAY, days=3)

    # 四阶段全 ok + 顺序 history→p3→t5→t6（dict 插入序即执行序）
    assert summary["all_ok"] is True, f"四阶段应全成功: {summary['phases']}"
    assert list(summary["phases"]) == ["history", "p3", "t5", "t6"]
    for ph in ("history", "p3", "t5", "t6"):
        assert summary["phases"][ph]["ok"] is True, f"phase {ph} 应成功: {summary['phases'][ph]}"

    # 阶段 1 history：kline_daily 全史（fake 3 日/股）+ done 键 full_history
    n_kl = con.execute("SELECT COUNT(*) FROM kline_daily").fetchone()[0]
    assert n_kl == 2 * 3, f"kline_daily 应 6 行: {n_kl}"
    prog1 = _read_prog()
    done_keys = [tuple(k) for k in prog1.get("done", [])]
    assert ("kline_history", "sh.600001", "full_history") in done_keys, \
        f"history done 键应含 full_history: {done_keys}"
    # 阶段 2 P3：T3 估值快照（date=今日）+ T7 四指数 × 3 日
    n_v = con.execute("SELECT COUNT(*) FROM valuation_daily").fetchone()[0]
    assert n_v == 2, f"valuation_daily 应 2 行: {n_v}"
    d_v = [str(x[0]) for x in con.execute(
        "SELECT DISTINCT date FROM valuation_daily").fetchall()]
    assert d_v == [FIXED_TODAY], f"T3 快照 date 应为今日: {d_v}"
    n_i = con.execute("SELECT COUNT(*) FROM index_daily").fetchone()[0]
    assert n_i == len(INDEX_CODES) * 3, f"index_daily 应 {len(INDEX_CODES)*3} 行: {n_i}"
    # 阶段 3 T5：fundamentals_quarterly（adata F10，每股 1 期）
    n_t5 = con.execute("SELECT COUNT(*) FROM fundamentals_quarterly").fetchone()[0]
    assert n_t5 == 2, f"fundamentals_quarterly 应 2 行: {n_t5}"
    # 阶段 4 T6（v6.1.7）：holders_snapshot（fake 2 期×3 股东/股 → 每股 6 行）
    n_t6 = con.execute("SELECT COUNT(*) FROM holders_snapshot").fetchone()[0]
    assert n_t6 == 2 * 6, f"holders_snapshot 应 12 行（2 股×2 期×3 股东）: {n_t6}"
    # 取数计数：history 每股 1 次全史；P3 T2/T7 用窗口 K线（T2 2 股 + T7 4 指数）；
    # T6 每股 1 次 fetch_holders（code6 口径）
    assert counter["full_history"] == 2, f"history 应每股一次全史: {counter}"
    assert counter["kline"] == 2 + len(INDEX_CODES), f"P3 T2/T7 窗口取数: {counter}"
    assert counter["holders"] == {"600001": 1, "000002": 1}, \
        f"T6 应每股一次 fetch_holders（code6）: {counter['holders']}"
    # 段标日志（stdout）：四阶段开始/结束成对、顺序正确（耗时值不硬断言——计时非契约）
    out = capsys.readouterr().out
    marks = [ln for ln in out.splitlines() if "===== phase:" in ln]
    assert len(marks) == 8, f"应 8 条段标（4 阶段×开始/结束）: {marks}"
    expect_seq = [("history", "开始"), ("history", "结束"),
                  ("p3", "开始"), ("p3", "结束"),
                  ("t5", "开始"), ("t5", "结束"),
                  ("t6", "开始"), ("t6", "结束")]
    for ln, (ph, kind) in zip(marks, expect_seq):
        assert f"===== phase: {ph} =====" in ln and kind in ln, \
            f"段标顺序错误（期望 {ph}/{kind}）: {marks}"
    con.close()


# ===========================================================================
# 2) full 幂等：done keys 全在 → 三阶段全跳过、零重取
# ===========================================================================
def test_full_idempotent_rerun_all_skipped(tmp_path, monkeypatch):
    """同日重跑 full → history/p3/t5/t6 全 skipped_done、fake 零重取（幂等契约）。"""
    db = str(tmp_path / "fullid.duckdb")
    codes = ["sh.600001"]
    _seed_master(db, codes)
    con = lconn.open(db)
    counter = _wire_fakes(monkeypatch, codes)

    r1 = drv.run_full(con, db, codes, "1990-01-01", FIXED_TODAY, days=3)
    assert r1["all_ok"] is True
    # 计数清零 → Run2
    counter["full_history"] = counter["kline"] = counter["snapshot"] = 0
    counter["holders"] = {}

    r2 = drv.run_full(con, db, codes, "1990-01-01", FIXED_TODAY, days=3)
    assert r2["all_ok"] is True
    # history：done 键 full_history 全跳过（零重取）
    assert r2["phases"]["history"]["skipped_done"] == 1
    assert r2["phases"]["history"]["processed"] == 0
    # P3：kline_daily/valuation_daily/index_daily done 键 inc:<今日> 全跳过
    p3 = r2["phases"]["p3"]
    assert p3["t2"]["skipped_done"] == 1 and p3["t2"]["processed"] == 0
    assert p3["t3"]["skipped_done"] == 1 and p3["t3"]["processed"] == 0
    assert p3["t7"]["skipped_done"] == len(INDEX_CODES) and p3["t7"]["processed"] == 0
    # T5：f10_full done 键全跳过 → adata 零重取
    assert r2["phases"]["t5"]["skipped_done"] == 1
    assert r2["phases"]["t5"]["processed"] == 0
    # T6（v6.1.7）：(t6, ts_code) done 键全跳过 → fetch_holders 零重取
    assert r2["phases"]["t6"]["skipped_done"] == 1
    assert r2["phases"]["t6"]["processed"] == 0
    assert counter["full_history"] == 0, f"Run2 history 应零全史重取: {counter}"
    assert counter["kline"] == 0, f"Run2 P3 应零窗口重取: {counter}"
    assert counter["holders"] == {}, f"Run2 T6 应零 fetch_holders 重取: {counter['holders']}"
    # upsert 幂等：行数不翻倍（T6 load_t6 先删后插——重灌也不翻倍）
    assert con.execute("SELECT COUNT(*) FROM kline_daily").fetchone()[0] == 3
    assert con.execute("SELECT COUNT(*) FROM fundamentals_quarterly").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM holders_snapshot").fetchone()[0] == 6
    # done 键含 (holders_snapshot, ts_code, "t6")（brief 逐字幂等键）
    prog = _read_prog()
    done_keys = [tuple(k) for k in prog.get("done", [])]
    assert ("holders_snapshot", "sh.600001", "t6") in done_keys, \
        f"T6 done 键应为 (t6, ts_code): {done_keys}"
    con.close()


# ===========================================================================
# 3) 单阶段 error 不阻断后续（brief §A 核心契约）
# ===========================================================================
def test_full_history_error_continues_to_p3_and_t5(tmp_path, monkeypatch):
    """history 全史取数失败 → kline_history state=error + last_error；P3/T5 照常跑完。"""
    db = str(tmp_path / "fullerr.duckdb")
    codes = ["sh.600001", "sz.000002"]
    _seed_master(db, codes)
    con = lconn.open(db)
    counter = _wire_fakes(monkeypatch, codes, hist_kline_fail=True)

    summary = drv.run_full(con, db, codes, "1990-01-01", FIXED_TODAY, days=3)

    # history 阶段：worker 抛错 → runner 记 errors、不 mark_done → phase ok=False
    assert summary["phases"]["history"]["ok"] is False
    assert "腾讯K线全史故障" in summary["phases"]["history"].get("error", "") or \
        "failed" in summary["phases"]["history"].get("error", ""), \
        f"history 应记 error: {summary['phases']['history']}"
    # P3/T5 照常跑完（单表故障不拖死全量）——P3 T2 用窗口取数（fake 未注入失败于窗口？
    # hist_kline_fail 同时打窗口 fake → P3 T2 也失败，属预期：两阶段各自 error、互不阻断）
    assert summary["phases"]["t5"]["ok"] is True, "T5 不受 history 故障影响"
    n_t5 = con.execute("SELECT COUNT(*) FROM fundamentals_quarterly").fetchone()[0]
    assert n_t5 == 2, "T5 照常落库"
    # kline_history entry：state=error + last_error（收尾视图刷新后仍 error——
    # _refresh_incremental_task_view 只纠正 running/stopping→pending，不碰 error）
    prog = _read_prog()
    eh = _task_entry(prog, "kline_history")
    assert eh is not None and eh["state"] == "error", f"kline_history 应 state=error: {eh}"
    assert eh.get("last_error"), f"error 应有 last_error: {eh}"
    # T5 entry：done==total → done（不受 history 故障影响）
    e5 = _task_entry(prog, "fundamentals_quarterly")
    assert e5["state"] == "done", f"T5 应 done: {e5}"
    con.close()


def test_full_t5_phase_exception_marks_error_and_survives_view_refresh(tmp_path, monkeypatch):
    """T5 阶段级异常（run_t5 抛出）→ fundamentals_quarterly state=error + last_error，
    且**收尾视图刷新后 error 标记不被冲掉**（_refresh_incremental_task_view 无条件覆盖
    T5 entry state——full 收尾须重标；brief"该任务 state=error"以最终落盘为准）。"""
    db = str(tmp_path / "fullt5err.duckdb")
    codes = ["sh.600001"]
    _seed_master(db, codes)
    con = lconn.open(db)
    _wire_fakes(monkeypatch, codes)
    monkeypatch.setattr(drv, "run_t5",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("T5 阶段故障（测试注入）")))

    summary = drv.run_full(con, db, codes, "1990-01-01", FIXED_TODAY, days=3)

    # T5 阶段记 error、不抛；history/p3 照常 ok
    assert summary["phases"]["t5"]["ok"] is False
    assert "T5 阶段故障" in summary["phases"]["t5"]["error"]
    assert summary["phases"]["history"]["ok"] is True
    assert summary["phases"]["p3"]["t2"]["processed"] == 1
    assert summary["all_ok"] is False

    # progress：fundamentals_quarterly state=error + last_error（收尾刷新后仍 error）
    prog = _read_prog()
    e5 = _task_entry(prog, "fundamentals_quarterly")
    assert e5 is not None, "T5 entry 应存在"
    assert e5["state"] == "error", f"T5 阶段异常后收尾仍须 state=error: {e5}"
    assert "phase t5 failed" in (e5.get("last_error") or ""), f"last_error 应含原因: {e5}"
    # phase_errors 顶层记录（可观测）
    assert any(e.get("phase") == "t5" for e in prog.get("phase_errors", []))
    # P3 各表 state 不受 T5 故障影响（done==total → done）
    ek = _task_entry(prog, "kline_daily")
    assert ek["state"] == "done", f"P3 kline_daily 应 done: {ek}"
    # v6.1.7：T6 是 T5 的**下一阶段**——T5 阶段级故障不得阻断 t6（单表故障不拖死全量）
    assert summary["phases"]["t6"]["ok"] is True, "T5 阶段故障不得阻断 T6"
    e6 = _task_entry(prog, "holders_snapshot")
    assert e6["state"] == "done", f"T6 应照常 done: {e6}"
    con.close()


# ===========================================================================
# 4) phase 字段流转（/status locked 态，sync.log 段标回放）
# ===========================================================================
def _locked_status(monkeypatch, tmp_path, sync_log_text):
    """/status locked 态 + 指定 sync.log 内容 → 返回响应 dict。

    connect_existing 被 patch 抛 LakeLocked（灌数持锁三态契约的 locked 分支）；
    progress/sync.log 均 tmp（不碰生产）。
    """
    import lake.sync_control as sc

    db = str(tmp_path / "phase.duckdb")
    with open(db, "wb") as f:   # 非空库文件（probe 前提：存在且 size>0）
        f.write(b"\x00" * 64)
    monkeypatch.setattr(lconn, "default_db_path", lambda: db)
    prog = str(tmp_path / "prog" / "backfill_progress.json")
    os.makedirs(os.path.dirname(prog), exist_ok=True)
    with open(prog, "w", encoding="utf-8") as f:
        json.dump({"updated_at": "2026-09-18 01:00:00", "tasks": [], "coverage": {}}, f)
    monkeypatch.setattr(lconn, "progress_path", lambda: prog)
    monkeypatch.setattr(
        lconn, "connect_existing",
        lambda path=None: (_ for _ in ()).throw(lconn.LakeLocked(db, 4321)))
    log_path = str(tmp_path / "sync.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(sync_log_text)
    monkeypatch.setattr(sc, "_sync_log_path", lambda db_path=None: log_path)
    return wapi.status()


def test_phase_history_while_running(tmp_path, monkeypatch):
    """sync.log 段标：history 开始、未结束 → /status.phase='history'（locked 态）。"""
    d = _locked_status(monkeypatch, tmp_path,
                       "===== phase: history =====（T2 全史 开始）\n"
                       "INFO lake_backfill: backfill kline_history ...\n")
    assert d["backfill_in_progress"] is True
    assert d.get("phase") == "history", f"应报 phase=history: {d.get('phase')}"


def test_phase_p3_after_history_done(tmp_path, monkeypatch):
    """history 结束 + p3 开始 → phase='p3'（段标按序回放，后开者胜）。"""
    d = _locked_status(monkeypatch, tmp_path,
                       "===== phase: history =====（T2 全史 开始）\n"
                       "===== phase: history =====（T2 全史 结束，12.3s）\n"
                       "===== phase: p3 =====（P3 增量 开始）\n")
    assert d.get("phase") == "p3", f"应报 phase=p3: {d.get('phase')}"


def test_phase_t5_and_done_clears(tmp_path, monkeypatch):
    """t5 开始 → phase='t5'；四阶段全结束 → phase=None（**键不出现**，三态契约）。"""
    d = _locked_status(monkeypatch, tmp_path,
                       "===== phase: history =====（T2 全史 开始）\n"
                       "===== phase: history =====（T2 全史 结束，1s）\n"
                       "===== phase: p3 =====（P3 增量 开始）\n"
                       "===== phase: p3 =====（P3 增量 结束，2s）\n"
                       "===== phase: t5 =====（T5 基本面 开始）\n")
    assert d.get("phase") == "t5", f"应报 phase=t5: {d.get('phase')}"

    d2 = _locked_status(monkeypatch, tmp_path,
                        "===== phase: history =====（T2 全史 开始）\n"
                        "===== phase: history =====（T2 全史 结束，1s）\n"
                        "===== phase: p3 =====（P3 增量 开始）\n"
                        "===== phase: p3 =====（P3 增量 结束，2s）\n"
                        "===== phase: t5 =====（T5 基本面 开始）\n"
                        "===== phase: t5 =====（T5 基本面 结束，3s）\n")
    # v6.1.7：t5 结束后进入 t6 阶段——但本日志无 t6 段标（模拟旧 full / t6 尚未开始），
    # 回放结果 phase=None（键不出现）。
    assert d2.get("phase") is None and "phase" not in d2, \
        f"全结束后不应有 phase 键: {list(d2.keys())}"


def test_phase_t6_and_done_clears(tmp_path, monkeypatch):
    """v6.1.7：t6 开始 → phase='t6'；四阶段全结束（含 t6）→ phase=None（键不出现）。"""
    d = _locked_status(monkeypatch, tmp_path,
                       "===== phase: history =====（T2 全史 开始）\n"
                       "===== phase: history =====（T2 全史 结束，1s）\n"
                       "===== phase: p3 =====（P3 增量 开始）\n"
                       "===== phase: p3 =====（P3 增量 结束，2s）\n"
                       "===== phase: t5 =====（T5 基本面 开始）\n"
                       "===== phase: t5 =====（T5 基本面 结束，3s）\n"
                       "===== phase: t6 =====（T6 股东 开始）\n")
    assert d.get("phase") == "t6", f"应报 phase=t6: {d.get('phase')}"

    d2 = _locked_status(monkeypatch, tmp_path,
                        "===== phase: history =====（T2 全史 开始）\n"
                        "===== phase: history =====（T2 全史 结束，1s）\n"
                        "===== phase: p3 =====（P3 增量 开始）\n"
                        "===== phase: p3 =====（P3 增量 结束，2s）\n"
                        "===== phase: t5 =====（T5 基本面 开始）\n"
                        "===== phase: t5 =====（T5 基本面 结束，3s）\n"
                        "===== phase: t6 =====（T6 股东 开始）\n"
                        "===== phase: t6 =====（T6 股东 结束，4s）\n")
    assert d2.get("phase") is None and "phase" not in d2, \
        f"四阶段全结束后不应有 phase 键: {list(d2.keys())}"


def test_phase_absent_for_non_full_process(tmp_path, monkeypatch):
    """非 full 进程（无段标，如旧 incremental）→ 无 phase 键（三态契约其余字段不动）。"""
    d = _locked_status(monkeypatch, tmp_path,
                       "INFO lake_backfill: incremental start\n")
    assert d["backfill_in_progress"] is True
    assert "phase" not in d, f"非 full 进程不应有 phase 键: {list(d.keys())}"


def test_phase_exception_line_clears(tmp_path, monkeypatch):
    """阶段'异常，继续下一阶段'段标 → 该 phase 视为结束（后续无开始标记 → None）。"""
    d = _locked_status(monkeypatch, tmp_path,
                       "===== phase: t5 =====（T5 基本面 开始）\n"
                       "===== phase: t5 =====（T5 基本面 异常，继续下一阶段）: boom\n")
    assert d.get("phase") is None and "phase" not in d


# ===========================================================================
# 5) /sync/start 缺省=full + 409 互斥不变（mock start_sync，零 spawn）
# ===========================================================================
def test_start_default_full_and_409_unchanged(monkeypatch):
    import lake.sync_control as sc

    captured = {}

    def fake_start(**kw):
        captured.update(kw)
        return {"started": True, "pid": 1234, "log_path": "/tmp/x.log"}

    monkeypatch.setattr(sc, "start_sync", fake_start)
    d = wapi.sync_start()   # 缺省 → full
    assert captured.get("sub") == "full", f"缺省应 spawn driver full: {captured}"
    assert d["mode"] == "full" and d["started"] is True

    # 409 互斥语义不变（已 running → LakeSyncConflict，full 不改变）
    monkeypatch.setattr(
        sc, "start_sync",
        lambda **k: {"started": False, "pid": 777, "log_path": None,
                     "reason": "already_running"})
    with pytest.raises(wapi.LakeSyncConflict) as ei:
        wapi.sync_start()   # 缺省 full
    assert ei.value.status_code == 409


def test_start_deprecated_modes_still_spawn_correctly(monkeypatch):
    """deprecated modes（history/incremental/t5）保留为测试/排障通道——spawn 参数不变。"""
    import lake.sync_control as sc

    captured = {}

    def fake_start(**kw):
        captured.update(kw)
        return {"started": True, "pid": 1, "log_path": "/tmp/x.log"}

    monkeypatch.setattr(sc, "start_sync", fake_start)
    wapi.sync_start(mode="history")
    assert captured.get("sub") == "history" and not (captured.get("extra_args") or [])
    wapi.sync_start(mode="incremental")
    assert captured.get("sub") == "incremental"
    wapi.sync_start(mode="t5")
    assert captured.get("sub") == "history" and captured.get("extra_args") == ["--t5"]


# ===========================================================================
# 6) D-1 回归（v6.1.6 修复轮）：full 模式 R3 看门狗 abort 必须 rc=1 + 终止后续阶段
#    run_history 的真实 R3 收尾契约是**返回** hang_watchdog=True（不抛——L951
#    except HangWatchdogError 分支，v6.1.5 cmd_history 已验收，逐字不动）；full
#    编排层必须检查该标志：phase error 化 + break + all_ok=False + cmd_full rc=1。
# ===========================================================================
def test_d1_run_full_hang_watchdog_flag_stops_subsequent_phases(tmp_path, monkeypatch):
    """run_history 按真实 R3 收尾契约返回 hang_watchdog=True → run_full：

    - history phase ok=False + error（含 R3/看门狗字样）+ hang_watchdog 标志留痕；
    - **break**：p3/t5 不执行（summary.phases 只有 history）；
    - all_ok=False（不得误报成功）；
    - progress kline_history state=error + last_error + phase_errors 留痕。
    """
    db = str(tmp_path / "d1full.duckdb")
    codes = ["sh.600001", "sz.000002"]
    _seed_master(db, codes)
    con = lconn.open(db)
    _wire_fakes(monkeypatch, codes)

    def rh_hang(*a, **k):   # run_history 真实 R3 收尾契约：记标志后 return，不抛
        return {"sub": "history", "codes_requested": len(codes),
                "hang_watchdog": True,
                "last_error": "progress 停滞 >10min → 看门狗 abort"}

    p3_calls = {"n": 0}
    t5_calls = {"n": 0}

    def boom_p3(*a, **k):   # 若被调用即说明未 break（回归）
        p3_calls["n"] += 1
        raise AssertionError("R3 abort 后不得执行 p3")

    def boom_t5(*a, **k):
        t5_calls["n"] += 1
        raise AssertionError("R3 abort 后不得执行 t5")

    monkeypatch.setattr(drv, "run_history", rh_hang)
    monkeypatch.setattr(drv, "run_incremental", boom_p3)
    monkeypatch.setattr(drv, "run_t5", boom_t5)

    summary = drv.run_full(con, db, codes, "1990-01-01", FIXED_TODAY, days=3)

    # 只跑了 history（break 生效）——p3/t5 键不存在
    assert list(summary["phases"]) == ["history"], f"应仅含 history: {list(summary['phases'])}"
    assert p3_calls["n"] == 0 and t5_calls["n"] == 0, "R3 abort 后 p3/t5 不得执行"
    # history phase error 化 + 标志留痕
    ph = summary["phases"]["history"]
    assert ph["ok"] is False, f"R3 abort 的 phase 不得 ok=True: {ph}"
    assert ph.get("hang_watchdog") is True
    assert "看门狗" in (ph.get("error") or ""), f"error 应说明 R3 原因: {ph}"
    # all_ok=False（D-1 核心：不得误报成功）
    assert summary["all_ok"] is False, f"R3 abort 后 all_ok 必须 False: {summary['all_ok']}"

    # progress：kline_history state=error + last_error + phase_errors 留痕
    prog = _read_prog()
    eh = _task_entry(prog, "kline_history")
    assert eh is not None and eh["state"] == "error", f"kline_history 应 error: {eh}"
    assert eh.get("last_error"), f"应有 last_error: {eh}"
    assert any(e.get("phase") == "history" for e in prog.get("phase_errors", [])), \
        f"phase_errors 应含 history: {prog.get('phase_errors')}"
    con.close()


def test_d1_cmd_full_rc1_on_real_watchdog(tmp_path):
    """子进程驱动真实 driver `full`（tmp 库 + fake，零网络）：run_history 按真实 R3
    收尾契约返回 hang_watchdog=True → 进程 rc=1、段标仅 history、p3/t5 不执行。

    （与 tester FA4b 同口径的 coder 侧回归——tester 复测仍独立执行其套件。）
    """
    import subprocess

    db = str(tmp_path / "d1sub.duckdb")
    codes = ["sh.600001", "sz.000002"]
    _seed_master(db, codes)
    env = dict(os.environ)
    env["PYTHONPATH"] = REPO_ROOT + os.pathsep + SCRIPTS_DIR
    env["LAKE_MULTISOURCE"] = "0"
    prelude = (
        f"import sys; sys.path[:0]={[REPO_ROOT, SCRIPTS_DIR]!r}\n"
        "import lake_backfill as drv\n"
        "import lake.ingest.tencent_ingest as ti\n"
        "import lake.ingest.baostock_ingest as bsi\n"
        "import screener.data.baostock_client as bsc\n"
        "import screener.data.tencent as tmod\n"
        "from lake.ingest import source_pool as sp\n"
        f"drv._today_beijing=lambda:'{FIXED_TODAY}'\n"
        "drv.time.sleep=lambda s:None\n"
        "def _kl():\n"
        "    return [{'date':d,'open':10.0,'high':11.0,'low':9.5,'close':10.5,'volume':100.0}"
        " for d in ('2026-09-16','2026-09-17','2026-09-18')]\n"
        "ti.fetch_kline_full_history=lambda c,t,page_size=2000:[dict(r) for r in _kl()]\n"
        "ti.fetch_kline_ohlcv=lambda c,t,n:[dict(r) for r in _kl()]\n"
        "ti.fetch_snapshot=lambda c,ts:{x:{'total_mv_yi':1.0,'float_mv_yi':1.0,'pe_ttm':5.0,"
        "'pb':0.8,'turnover':1.0} for x in ts}\n"
        "bsi.fetch_adjust_factor=lambda bs,code,s,e:(['code','adjustFactor'],[])\n"
        "class _BS:\n    def __init__(s,*a,**k):pass\n    def close(s):pass\n"
        "class _TC:pass\n"
        "bsc.BaoStockClient=_BS; tmod.TencentClient=_TC\n"
        "class _AD:\n    def available(s):return True\n"
        "    def fetch_f10(s,c):return [{'period':'2026Q1','pub_date':'2026-04-30',"
        "'roe_weighted':5.0,'gross_margin':30.0,'liability_pct':40.0,'yoy_pni':8.0,"
        "'npi':1e9,'ocf':None}]\n"
        "sp._REGISTRY={'adata_f10':_AD()}\n"
        "def _rh(con,db,codes,s,e):\n"
        "    # run_history 真实 R3 收尾契约（lake_backfill.py except HangWatchdogError）：\n"
        "    # 记 hang_watchdog=True + last_error 后 return，不抛\n"
        "    return {'sub':'history','codes_requested':2,'hang_watchdog':True,\n"
        "            'last_error':'progress 停滞 >10min → 看门狗 abort'}\n"
        "drv.run_history=_rh\n"
        f"rc=drv.main(['--db', r'{db}', 'full', '--start-date','1990-01-01',"
        f"'--end-date','{FIXED_TODAY}','--days','3'])\n"
        "print('RC='+str(rc),flush=True)\n"
    )
    p = subprocess.run([sys.executable, "-c", prelude], capture_output=True,
                       text=True, timeout=120, env=env, cwd=REPO_ROOT)
    out = (p.stdout or "") + (p.stderr or "")
    # rc=1（"卡死自愈退出"与"正常完成"可区分——D-1 核心）
    assert p.returncode == 1, f"R3 abort 应致进程 rc=1，实测 {p.returncode}\n{out[-800:]}"
    # 段标：仅 history（开始 + R3 abort 终止），p3/t5 未执行
    marks = [ln for ln in out.splitlines() if "===== phase:" in ln]
    assert any("phase: history" in ln and "开始" in ln for ln in marks), f"应有 history 开始段标: {marks}"
    assert not any("phase: p3" in ln or "phase: t5" in ln for ln in marks), \
        f"R3 abort 后不得执行 p3/t5: {marks}"
    # progress（B-2：自定义 --db → 库同目录）：kline_history state=error + phase_errors
    with open(os.path.join(str(tmp_path), "backfill_progress.json"), encoding="utf-8") as f:
        prog = json.load(f)
    eh = next((t for t in prog.get("tasks", []) if t.get("table") == "kline_history"), None)
    assert eh is not None and eh["state"] == "error", f"kline_history 应 error: {eh}"
    assert any(e.get("phase") == "history" for e in prog.get("phase_errors", [])), \
        f"phase_errors 应含 history: {prog.get('phase_errors')}"


# ===========================================================================
# 7) v6.1.7：full 第 4 阶段 T6（holders_snapshot 接入 full——Joel 拍板方案 A）
#    brief §C：load_t6 行数/字段、幂等跳过、单只失败不阻断后续、progress 条目升级、
#    --no-t6 跳过、full 四阶段段标顺序（段标顺序已并入用例 1）。
# ===========================================================================
def test_t6_rows_fields_and_progress_upgrade(tmp_path, monkeypatch):
    """run_t6 直驱：load_t6 行数/字段正确 + progress 条目由 no_source/P3/0 升级为
    P2/total=universe/done（brief §A"由 no_source/total=0 升级为真实任务"）。"""
    db = str(tmp_path / "t6rows.duckdb")
    codes = ["sh.600001", "sz.000002"]
    _seed_master(db, codes)
    # 预置 v6.1.4 O3 旧占位条目（no_source/P3/total=0）——验证升级路径
    p = lb._progress_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"updated_at": None, "tasks": [
            {"table": "holders_snapshot", "tier": "P3", "total": 0, "done": 0,
             "quota_used_today": 0, "quota_budget": 5000,
             "state": "no_source", "eta_min": None, "last_error": "",
             "note": "无可用数据源（controller_* 待补源）"}],
            "coverage": {}, "done": []}, f)

    con = lconn.open(db)
    _wire_fakes(monkeypatch, codes)
    from lake.backfill import BackfillRunner

    stats = drv.run_t6(con, db, codes, BackfillRunner(db_path=db))

    # load_t6 行数：2 股 × 2 期 × 3 股东 = 12（brief §C"load_t6 行数/字段"）
    assert stats["processed"] == 2 and not stats.get("errors"), f"run_t6 应全成功: {stats}"
    n = con.execute("SELECT COUNT(*) FROM holders_snapshot").fetchone()[0]
    assert n == 12, f"holders_snapshot 应 12 行: {n}"
    # 字段：hold_ratio=circ_ratio_pct（流通股口径）、controller_* 恒 NULL（待补源）、
    # source=sina_f10、as_of_date=报告期 end_date
    row = con.execute(
        "SELECT ts_code, as_of_date, holder_rank, holder_name, hold_ratio, share_nature, "
        "controller_name, controller_type, controller_ratio, source "
        "FROM holders_snapshot WHERE ts_code='sh.600001' AND as_of_date='2026-03-31' "
        "AND holder_rank=1").fetchone()
    assert row is not None, "首行（rank=1/2026-03-31）应存在"
    (ts_code, as_of, rank, name, ratio, nature, cn, ct, cr, source) = row
    assert ts_code == "sh.600001" and str(as_of) == "2026-03-31" and rank == 1
    assert name == "股东A12.5" and ratio == 12.5, f"hold_ratio 应=circ_ratio_pct: {row}"
    assert nature == "国有股"
    assert cn is None and ct is None and cr is None, "controller_* 应恒 NULL（待补源）"
    assert source == "sina_f10"
    # progress 条目升级：no_source/P3/total=0 → P2/total=universe/done==total
    prog = _read_prog()
    e6 = _task_entry(prog, "holders_snapshot")
    assert e6 is not None and e6["tier"] == "P2", f"条目应升级 tier=P2: {e6}"
    assert e6["total"] == 2, f"total 应=stock_master 行数: {e6}"
    assert e6["done"] == 2, f"done 应=2（run_t6 直驱后）: {e6}"
    # run_t6 直驱收尾 state=running（runner.run 末次 _update_task_view 态）——full 模式
    # 由收尾 _refresh_incremental_task_view 归位 done；此处只断言非 no_source。
    assert e6["state"] != "no_source", f"升级后不得再是 no_source: {e6}"
    con.close()


def test_t6_single_code_failure_not_blocking(tmp_path, monkeypatch):
    """T6 单只失败（600001 抛错）→ 不阻断后续（000002 照常灌）；phase ok=False +
    errors；holders_snapshot state=error + last_error（收尾刷新后重标）。"""
    db = str(tmp_path / "t6fail.duckdb")
    codes = ["sh.600001", "sz.000002"]
    _seed_master(db, codes)
    con = lconn.open(db)
    counter = _wire_fakes(monkeypatch, codes, t6_fail_codes=("600001",))

    summary = drv.run_full(con, db, codes, "1990-01-01", FIXED_TODAY, days=3)

    # 前三个阶段不受影响
    for ph in ("history", "p3", "t5"):
        assert summary["phases"][ph]["ok"] is True, f"{ph} 应 ok: {summary['phases'][ph]}"
    # t6：单只失败 → phase ok=False + errors（runner 记 errors、不 mark_done）
    t6 = summary["phases"]["t6"]
    assert t6["ok"] is False, f"t6 应记 error: {t6}"
    assert "600001" in (t6.get("error") or ""), f"errors 应含失败股: {t6}"
    # 后续只照常：000002 落库（6 行），600001 零行
    n = con.execute("SELECT COUNT(*) FROM holders_snapshot").fetchone()[0]
    assert n == 6, f"仅 000002 应落 6 行: {n}"
    c6 = con.execute("SELECT DISTINCT ts_code FROM holders_snapshot").fetchall()
    assert [r[0] for r in c6] == ["sz.000002"], f"仅 000002: {c6}"
    # fetch_holders 两只都调了（单只失败不阻断后续取数）
    assert set(counter["holders"]) == {"600001", "000002"}, f"两只都应尝试: {counter['holders']}"
    # done 键：仅成功股（防 done 键毒化——失败股下轮重试）
    prog = _read_prog()
    done_keys = [tuple(k) for k in prog.get("done", [])]
    assert ("holders_snapshot", "sz.000002", "t6") in done_keys, f"成功股应标 done: {done_keys}"
    assert not any(k[1] == "sh.600001" and k[0] == "holders_snapshot" for k in done_keys), \
        f"失败股不得标 done: {done_keys}"
    # holders_snapshot entry：state=error + last_error（收尾视图刷新后重标——同 T5 契约）
    e6 = _task_entry(prog, "holders_snapshot")
    assert e6 is not None and e6["state"] == "error", f"应 state=error: {e6}"
    assert e6.get("last_error"), f"应有 last_error: {e6}"
    assert any(e.get("phase") == "t6" for e in prog.get("phase_errors", [])), \
        f"phase_errors 应含 t6: {prog.get('phase_errors')}"
    assert summary["all_ok"] is False, "有任务失败 → all_ok=False"
    con.close()


def test_t6_phase_exception_marks_error(tmp_path, monkeypatch):
    """T6 阶段级异常（run_t6 抛出）→ holders_snapshot state=error + last_error，
    收尾视图刷新后重标生效（brief §A"单表故障 last_error+state=error"）。"""
    db = str(tmp_path / "t6exc.duckdb")
    codes = ["sh.600001"]
    _seed_master(db, codes)
    con = lconn.open(db)
    _wire_fakes(monkeypatch, codes)
    monkeypatch.setattr(drv, "run_t6",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("T6 阶段故障（测试注入）")))

    summary = drv.run_full(con, db, codes, "1990-01-01", FIXED_TODAY, days=3)

    assert summary["phases"]["t6"]["ok"] is False
    assert "T6 阶段故障" in summary["phases"]["t6"]["error"]
    for ph in ("history", "p3", "t5"):
        assert summary["phases"][ph]["ok"] is True, f"{ph} 应不受影响: {summary['phases'][ph]}"
    assert summary["all_ok"] is False

    prog = _read_prog()
    e6 = _task_entry(prog, "holders_snapshot")
    assert e6 is not None and e6["state"] == "error", f"T6 阶段异常后收尾仍须 error: {e6}"
    assert "phase t6 failed" in (e6.get("last_error") or ""), f"last_error 应含原因: {e6}"
    assert any(e.get("phase") == "t6" for e in prog.get("phase_errors", []))
    con.close()


def test_full_no_t6_skips_phase(tmp_path, monkeypatch, capsys):
    """--no-t6（排障用）：t6 阶段跳过——无段标、零 fetch_holders、holders_snapshot 零行；
    summary.phases['t6']={'ok':True,'skipped':True}；all_ok=True。"""
    db = str(tmp_path / "not6.duckdb")
    codes = ["sh.600001", "sz.000002"]
    _seed_master(db, codes)
    con = lconn.open(db)
    counter = _wire_fakes(monkeypatch, codes)

    summary = drv.run_full(con, db, codes, "1990-01-01", FIXED_TODAY, days=3,
                           t6_enabled=False)

    # 前三阶段照常；t6 跳过留痕（不打段标、不取数）
    for ph in ("history", "p3", "t5"):
        assert summary["phases"][ph]["ok"] is True
    assert summary["phases"]["t6"] == {"ok": True, "skipped": True}, \
        f"t6 应 skipped: {summary['phases']['t6']}"
    assert summary["all_ok"] is True
    assert counter["holders"] == {}, f"--no-t6 不得调用 fetch_holders: {counter['holders']}"
    assert con.execute("SELECT COUNT(*) FROM holders_snapshot").fetchone()[0] == 0
    # stdout 无 t6 段标（4→3 阶段：6 条段标）
    out = capsys.readouterr().out
    marks = [ln for ln in out.splitlines() if "===== phase:" in ln]
    assert len(marks) == 6, f"--no-t6 应 6 条段标（3 阶段）: {marks}"
    assert not any("phase: t6" in ln for ln in marks), f"不得有 t6 段标: {marks}"
    # holders_snapshot entry：收尾视图刷新为真实任务 pending（total=universe、tier=P2，
    # done=0——跳过未灌）
    prog = _read_prog()
    e6 = _task_entry(prog, "holders_snapshot")
    assert e6 is not None and e6["state"] == "pending" and e6["total"] == 2 \
        and e6["tier"] == "P2", f"--no-t6 后 entry 应 pending/total=universe: {e6}"
    con.close()


def test_full_cli_no_t6_flag_parses():
    """argparse：full --no-t6 → t6=False；缺省/--t6 → True（deprecated 通道无该 flag）。"""
    p = drv.build_parser()
    a = p.parse_args(["full"])
    assert a.t6 is True, "缺省应开 T6"
    a = p.parse_args(["full", "--no-t6"])
    assert a.t6 is False, "--no-t6 应关 T6"
    a = p.parse_args(["full", "--t6"])
    assert a.t6 is True, "显式 --t6 无行为变化（仍开）"
    # deprecated 单模式通道不加 t6（brief 红线：逐字节不变）
    for sub in ("history", "incremental"):
        a = p.parse_args([sub])
        assert not hasattr(a, "t6"), f"{sub} 不得有 t6 flag: {a}"
