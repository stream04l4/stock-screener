# -*- coding: utf-8 -*-
"""test_lake_t5_incremental —— T5 季度增量（v7.0.32，Joel 2026-11-14 拍板）。

背景（2026-11-14 排查实锤）：run_t5 全量用一次性 ``f10_full`` done 键（每股一次抓
全部历史报告期，标 done 后永不重取），而每日增量只覆盖 T2/T3/T7 →
fundamentals_quarterly 冻结在末次全量的报告期（生产 2026Q3=0 行、"季度基本面一直
滞后"、Web 进度停在 5215/5223 陈旧快照）。v7.0.32 修复：每日增量按报告期跟进——
探测源上最新报告期 P* → 湖内逐股 MAX(period) 缺口判定 → fetch_f10 + upsert 补齐。

**验收点（本文件）**：
- 探测：探测标的 max period = P*；单只探测失败容错；全灭抛错（下轮重试）。
- 缺口判定以**湖**为准：无行（次新股）/ 低于 P* → 取；≥ P* → 跳过零网络。
- done 键 = 实际最大期（非固定 q:P*）：未披露 P* 不标 q:P*（防键毒化、下轮自愈）。
- 单股取空/取错 → 记痕不标 done，其余股不受影响。
- adata 不可用 → 抛错（不标 done，下轮重试）。
- 视图：_refresh_incremental_task_view 的 T5 条目 done = 湖内达最新期股数（湖驱动）。

零网络：monkeypatch source_pool.get_adapter → FakeAdapter；in-memory DuckDB。
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("duckdb")

_SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import duckdb  # noqa: E402

FUND_DDL = (
    "CREATE TABLE fundamentals_quarterly("
    " ts_code VARCHAR, period VARCHAR, pub_date DATE, roe_weighted DOUBLE, "
    " yoy_pni DOUBLE, npi DOUBLE, gross_margin DOUBLE, liability_pct DOUBLE, "
    " source VARCHAR, fetched_at TIMESTAMP, data_version VARCHAR, conflict_src VARCHAR, "
    " PRIMARY KEY(ts_code, period))"
)
MASTER_DDL = "CREATE TABLE stock_master(ts_code VARCHAR, delist_date DATE)"


def _rec(period: str, pub: str) -> dict:
    return {"period": period, "pub_date": pub, "roe_weighted": 5.0,
            "yoy_pni": 1.0, "npi": 1e9, "gross_margin": 30.0, "liability_pct": 55.0}


class FakeAdapter:
    """adata F10 fake：data={code: [rec,...]}；fail 集合内 code 抛错；calls 记痕。"""

    name = "adata_f10"

    def __init__(self, data, fail=()):
        self.data = data
        self.fail = set(fail)
        self.calls = []

    def available(self):
        return True

    def fetch_f10(self, code):
        self.calls.append(code)
        if code in self.fail:
            raise RuntimeError(f"fake adata 失败 {code}")
        return self.data.get(code, [])


def _make_con(rows):
    """in-memory 湖：fundamentals_quarterly + stock_master（rows=[(code, period, pub)]）。"""
    con = duckdb.connect(":memory:")
    con.execute(FUND_DDL)
    con.execute(MASTER_DDL)
    for code, period, pub in rows:
        con.execute(
            "INSERT INTO fundamentals_quarterly VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [code, period, pub, 5.0, 1.0, 1e9, 30.0, 55.0, "adata_f10",
             "2026-11-14 00:00:00", "v6", None])
        con.execute("INSERT INTO stock_master VALUES (?, NULL)", [code])
    return con


def _runner(tmp_path):
    from lake.backfill import BackfillRunner

    # tmp progress_path 隔离真实 data/lake/；save_progress 只写 tmp 文件——零副作用
    return BackfillRunner(budget_per_day=5000,
                          progress_path=str(tmp_path / "progress.json"))


def _patch_adapter(monkeypatch, fake):
    from lake.ingest import source_pool as sp

    monkeypatch.setattr(sp, "get_adapter", lambda name: fake if name == "adata_f10" else None)


# ---------------------------------------------------------------------------
# 探测
# ---------------------------------------------------------------------------

def test_probe_latest_period_is_max(monkeypatch, tmp_path):
    import lake_backfill as drv

    fake = FakeAdapter({
        "sh.600519": [_rec("2026Q3", "2026-10-25"), _rec("2026Q2", "2026-07-30")],
        "sh.600000": [_rec("2026Q2", "2026-08-28")],
    })
    _patch_adapter(monkeypatch, fake)
    assert drv._t5_probe_latest_period(fake) == "2026Q3"
    # 两只探测都调了（取 max，不短路）
    assert set(fake.calls) == {"sh.600519", "sh.600000"}


def test_probe_falls_back_on_single_failure(monkeypatch):
    import lake_backfill as drv

    fake = FakeAdapter({"sh.600000": [_rec("2026Q3", "2026-10-31")]},
                       fail=("sh.600519",))
    assert drv._t5_probe_latest_period(fake) == "2026Q3"


def test_probe_all_fail_raises(monkeypatch):
    import lake_backfill as drv

    fake = FakeAdapter({}, fail=("sh.600519", "sh.600000"))
    with pytest.raises(RuntimeError, match="探测失败"):
        drv._t5_probe_latest_period(fake)


# ---------------------------------------------------------------------------
# run_t5_incremental 主路径
# ---------------------------------------------------------------------------

def test_catch_up_new_quarter(monkeypatch, tmp_path):
    """湖停在 2026Q2、源已出 2026Q3 → 补 Q3 行 + done 键 q:2026Q3。"""
    import lake_backfill as drv

    con = _make_con([("sh.600100", "2026Q2", "2026-07-30"),
                     ("sh.600100", "2026Q1", "2026-04-28")])
    fake = FakeAdapter({
        "sh.600519": [_rec("2026Q3", "2026-10-25")],   # 探测
        "sh.600000": [_rec("2026Q3", "2026-10-28")],
        "sh.600100": [_rec("2026Q3", "2026-11-02"), _rec("2026Q2", "2026-07-30"),
                      _rec("2026Q1", "2026-04-28")],
    })
    _patch_adapter(monkeypatch, fake)
    r = _runner(tmp_path)
    stats = drv.run_t5_incremental(con, ":memory:", ["sh.600100"], r)

    assert stats["probe"] == "2026Q3"
    assert stats["needed"] == 1 and stats["processed"] == 1
    assert stats["not_reported"] == 0 and stats["errors"] == []
    n = con.execute("SELECT COUNT(*) FROM fundamentals_quarterly "
                    "WHERE ts_code='sh.600100' AND period='2026Q3'").fetchone()[0]
    assert n == 1
    assert ("fundamentals_quarterly", "sh.600100", "q:2026Q3") in r._done


def test_not_reported_self_heals_next_round(monkeypatch, tmp_path):
    """P* 未披露（actual<P*）→ 不标 q:P*、not_reported+1；下轮源出新期后自动重取。"""
    import lake_backfill as drv

    con = _make_con([("sh.600200", "2026Q2", "2026-07-30")])
    data = {
        "sh.600519": [_rec("2026Q3", "2026-10-25")],   # 探测（茅台先披露）
        "sh.600000": [_rec("2026Q3", "2026-10-28")],
        "sh.600200": [_rec("2026Q2", "2026-07-30")],   # 该股 Q3 未披露
    }
    fake = FakeAdapter(data)
    _patch_adapter(monkeypatch, fake)
    r = _runner(tmp_path)

    s1 = drv.run_t5_incremental(con, ":memory:", ["sh.600200"], r)
    assert s1["not_reported"] == 1 and s1["processed"] == 1
    assert ("fundamentals_quarterly", "sh.600200", "q:2026Q2") in r._done
    assert ("fundamentals_quarterly", "sh.600200", "q:2026Q3") not in r._done
    assert con.execute("SELECT COUNT(*) FROM fundamentals_quarterly "
                       "WHERE period='2026Q3'").fetchone()[0] == 0

    # 下轮：源已出该股 Q3 → 湖内 600200 max=2026Q2 < P* → 重取补齐
    data["sh.600200"] = [_rec("2026Q3", "2026-10-31"), _rec("2026Q2", "2026-07-30")]
    s2 = drv.run_t5_incremental(con, ":memory:", ["sh.600200"], r)
    assert s2["needed"] == 1 and s2["not_reported"] == 0
    assert con.execute("SELECT COUNT(*) FROM fundamentals_quarterly "
                       "WHERE ts_code='sh.600200' AND period='2026Q3'").fetchone()[0] == 1
    assert ("fundamentals_quarterly", "sh.600200", "q:2026Q3") in r._done


def test_skips_up_to_date_zero_network(monkeypatch, tmp_path):
    """湖已达 P* → 跳过，一只都不取（零网络）。"""
    import lake_backfill as drv

    con = _make_con([("sh.600300", "2026Q3", "2026-11-05")])
    fake = FakeAdapter({
        "sh.600519": [_rec("2026Q3", "2026-10-25")],
        "sh.600000": [_rec("2026Q3", "2026-10-28")],
        "sh.600300": [_rec("2026Q3", "2026-11-05")],
    })
    _patch_adapter(monkeypatch, fake)
    r = _runner(tmp_path)
    stats = drv.run_t5_incremental(con, ":memory:", ["sh.600300"], r)
    assert "skipped" in stats and stats["needed"] == 0
    assert "sh.600300" not in fake.calls   # 只调了探测标的


def test_new_stock_no_rows(monkeypatch, tmp_path):
    """次新股（湖内无行）→ 全期灌入 + done q:<实际最大期>。"""
    import lake_backfill as drv

    con = _make_con([])
    fake = FakeAdapter({
        "sh.600519": [_rec("2026Q3", "2026-10-25")],
        "sh.600000": [_rec("2026Q3", "2026-10-28")],
        "sh.688837": [_rec("2026Q3", "2026-10-30"), _rec("2026Q2", "2026-08-20"),
                      _rec("2026Q1", "2026-04-25")],
    })
    _patch_adapter(monkeypatch, fake)
    r = _runner(tmp_path)
    stats = drv.run_t5_incremental(con, ":memory:", ["sh.688837"], r)
    assert stats["needed"] == 1 and stats["processed"] == 1
    assert con.execute("SELECT COUNT(*) FROM fundamentals_quarterly "
                       "WHERE ts_code='sh.688837'").fetchone()[0] == 3
    assert ("fundamentals_quarterly", "sh.688837", "q:2026Q3") in r._done


def test_fetch_empty_recorded_no_done(monkeypatch, tmp_path):
    """单股取空 → 记痕、不标 done、不写行；其余股正常。"""
    import lake_backfill as drv

    con = _make_con([("sh.600400", "2026Q2", "2026-07-30"),
                     ("sh.600401", "2026Q2", "2026-07-30")])
    fake = FakeAdapter({
        "sh.600519": [_rec("2026Q3", "2026-10-25")],
        "sh.600000": [_rec("2026Q3", "2026-10-28")],
        # sh.600400 取空；sh.600401 正常
        "sh.600401": [_rec("2026Q3", "2026-10-30"), _rec("2026Q2", "2026-07-30")],
    })
    _patch_adapter(monkeypatch, fake)
    r = _runner(tmp_path)
    stats = drv.run_t5_incremental(con, ":memory:", ["sh.600400", "sh.600401"], r)
    assert stats["processed"] == 1
    assert len(stats["errors"]) == 1 and "sh.600400" in stats["errors"][0]
    assert not any(k[1] == "sh.600400" for k in r._done)
    assert con.execute("SELECT COUNT(*) FROM fundamentals_quarterly "
                       "WHERE ts_code='sh.600400'").fetchone()[0] == 1   # 只有旧 Q2 行


def test_fetch_error_does_not_abort_queue(monkeypatch, tmp_path):
    """单股取数异常 → 记痕继续，队尾股仍被处理。"""
    import lake_backfill as drv

    con = _make_con([("sh.600500", "2026Q2", "2026-07-30"),
                     ("sh.600501", "2026Q2", "2026-07-30")])
    fake = FakeAdapter({
        "sh.600519": [_rec("2026Q3", "2026-10-25")],
        "sh.600000": [_rec("2026Q3", "2026-10-28")],
        "sh.600501": [_rec("2026Q3", "2026-11-01")],
    }, fail=("sh.600500",))
    _patch_adapter(monkeypatch, fake)
    r = _runner(tmp_path)
    stats = drv.run_t5_incremental(con, ":memory:", ["sh.600500", "sh.600501"], r)
    assert stats["processed"] == 1
    assert any("sh.600500" in e for e in stats["errors"])
    assert ("fundamentals_quarterly", "sh.600501", "q:2026Q3") in r._done


def test_adata_unavailable_raises(monkeypatch, tmp_path):
    """adata 不可用 → 抛错（增量捕获后记痕、下轮重试，不标 done）。"""
    import lake_backfill as drv

    class DownAdapter:
        def available(self):
            return False

        def fetch_f10(self, code):
            raise AssertionError("不应调用")

    con = _make_con([])
    _patch_adapter(monkeypatch, DownAdapter())
    r = _runner(tmp_path)
    with pytest.raises(RuntimeError, match="adata F10 不可用"):
        drv.run_t5_incremental(con, ":memory:", ["sh.600600"], r)


def test_probe_failure_raises_before_any_fetch(monkeypatch, tmp_path):
    """探测全灭 → 抛错，业务股一只不取。"""
    import lake_backfill as drv

    con = _make_con([("sh.600700", "2026Q2", "2026-07-30")])
    fake = FakeAdapter({"sh.600700": [_rec("2026Q3", "2026-10-30")]},
                       fail=("sh.600519", "sh.600000"))
    _patch_adapter(monkeypatch, fake)
    r = _runner(tmp_path)
    with pytest.raises(RuntimeError, match="探测失败"):
        drv.run_t5_incremental(con, ":memory:", ["sh.600700"], r)
    assert "sh.600700" not in fake.calls


# ---------------------------------------------------------------------------
# 视图（_refresh_incremental_task_view 的 T5 条目——湖驱动口径）
# ---------------------------------------------------------------------------

def test_view_t5_done_counts_latest_period(monkeypatch, tmp_path):
    """T5 条目 done = 湖内达全库最新期的 universe 股数；全齐 → state=done。"""
    import lake_backfill as drv

    con = _make_con([
        ("sh.600800", "2026Q3", "2026-11-05"),
        ("sh.600800", "2026Q2", "2026-07-30"),
        ("sh.600801", "2026Q3", "2026-11-06"),
        ("sh.600802", "2026Q2", "2026-07-30"),   # 未达最新期
    ])
    r = _runner(tmp_path)
    drv._refresh_incremental_task_view(r, con, "2026-11-14")
    e5 = next(t for t in r.progress["tasks"] if t.get("table") == "fundamentals_quarterly")
    assert e5["total"] == 3
    assert e5["done"] == 2
    assert e5["state"] == "pending"
    assert "2026Q3" in e5["note"]

    # 补齐后 → done
    con.execute("INSERT INTO fundamentals_quarterly VALUES "
                "('sh.600802','2026Q3','2026-11-07',5.0,1.0,1e9,30.0,55.0,"
                "'adata_f10',TIMESTAMP '2026-11-14 00:00:00','v6',NULL)")
    drv._refresh_incremental_task_view(r, con, "2026-11-14")
    e5 = next(t for t in r.progress["tasks"] if t.get("table") == "fundamentals_quarterly")
    assert e5["done"] == 3 and e5["state"] == "done"


def test_view_t5_empty_table_pending(monkeypatch, tmp_path):
    """空表（新库）→ done=0、state=pending，不抛错。"""
    import lake_backfill as drv

    con = _make_con([])
    r = _runner(tmp_path)
    drv._refresh_incremental_task_view(r, con, "2026-11-14")
    e5 = next(t for t in r.progress["tasks"] if t.get("table") == "fundamentals_quarterly")
    assert e5["total"] == 0 and e5["done"] == 0 and e5["state"] == "pending"


# ---------------------------------------------------------------------------
# 实时视图（locked 态 /status 读 progress tasks——Joel 2026-11-15 实锤缺提示）
# ---------------------------------------------------------------------------

def test_live_view_updated_during_and_after_run(monkeypatch, tmp_path):
    """T5 循环开始+收尾各刷一次实时视图（<200 只无周期刷）；done=湖内已达 P* 股数。"""
    import lake_backfill as drv

    con = _make_con([("sh.601000", "2026Q2", "2026-07-30")])
    fake = FakeAdapter({
        "sh.600519": [_rec("2026Q3", "2026-10-25")],
        "sh.600000": [_rec("2026Q3", "2026-10-28")],
        "sh.601000": [_rec("2026Q3", "2026-11-02"), _rec("2026Q2", "2026-07-30")],
    })
    _patch_adapter(monkeypatch, fake)
    r = _runner(tmp_path)

    calls = []
    orig = drv._t5_live_view

    def spy(*a, **kw):
        calls.append(a)
        return orig(*a, **kw)

    monkeypatch.setattr(drv, "_t5_live_view", spy)
    drv.run_t5_incremental(con, ":memory:", ["sh.601000"], r)

    # 3 只 < 200 → 仅开始(0) + 收尾(processed) 两次
    assert [c[-1] for c in calls] == [0, 1]
    e5 = next(t for t in r.progress["tasks"] if t.get("table") == "fundamentals_quarterly")
    assert e5["state"] == "running"
    assert e5["done"] == 1 and e5["total"] == 1
    assert "2026Q3" in e5["note"] and "季度增量中" in e5["note"]


def test_live_view_fallback_on_sql_error(monkeypatch, tmp_path):
    """视图湖查询失败 → 退本轮 processed 计数，不抛错、不阻断。"""
    import lake_backfill as drv

    con = _make_con([])

    class _DeadCon:
        def execute(self, *a, **kw):
            raise RuntimeError("fake lake error")

    e = {"table": "fundamentals_quarterly"}
    r = _runner(tmp_path)
    r.progress.setdefault("tasks", []).append(e)
    drv._t5_live_view(_DeadCon(), r, "2026Q3", total=10, processed=7)
    assert e["done"] == 7 and e["state"] == "running"


def test_sweep_stale_running_only_flips_running(tmp_path):
    """新 run 启动时 state=running 必为陈旧标记（旧进程已死）→ 归一
    stopped_by_signal（前端渲染"已停止"）；done/pending 等其他状态不动。"""
    import lake_backfill as drv

    r = _runner(tmp_path)
    r.progress["tasks"] = [
        {"table": "kline_daily", "state": "running", "done": 5223, "total": 5223},
        {"table": "fundamentals_quarterly", "state": "done", "done": 5, "total": 5},
        {"table": "index_daily", "state": "pending", "done": 0, "total": 4},
    ]
    n = drv._sweep_stale_running(r)
    assert n == 1
    states = [t["state"] for t in r.progress["tasks"]]
    assert states == ["stopped_by_signal", "done", "pending"]
    # done/total 数值不动（页面仍显示真实湖口径 5223/5223）
    assert r.progress["tasks"][0]["done"] == 5223
