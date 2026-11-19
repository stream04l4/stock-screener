# -*- coding: utf-8 -*-
"""test_signal_touch_lake_fallback —— 触碰落盘湖兜底 + 锁重试（v8.2.0 补）。

纪律：全离线——tmp duckdb 库 + monkeypatch 行情/湖路径。

背景（11-17/11-18 实锤）：
- 11-17 20:05：腾讯整批空 + 东财 push2delay WAF 502 → 无当日 low → 触碰丢失；
- 11-18 20:05：逐只 legacy 灌数持锁 → 开湖失败 rc=3。

覆盖：
- _lake_spot：每只取**最新 bar** 的 low/close（停牌股落旧日）；无 bar/
  low NULL → 剔除；
- run()：实时主备皆败 → 湖兜底生效（rc 0，spot=湖值）；湖也缺 → rc 4；
  实时源正常 → 不碰湖；
- 锁重试：开湖被锁退避重试（延迟 monkeypatch 0）；重试耗尽 → {}；
  池上下文开湖被锁重试；非锁异常不重试（rc 3）。

注：DuckDB 同进程同文件只允许一个连接 → fixture 只建 schema 即关闭，
数据按需开连接写入后关闭，_lake_spot 再开 read_only 连接。
"""
from __future__ import annotations

import os
import sys
import types

import pytest

pytest.importorskip("duckdb")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import duckdb  # noqa: E402

import scripts.signal_touch as st  # noqa: E402


@pytest.fixture()
def lake_path(tmp_path, monkeypatch):
    """tmp 湖：建 schema 后关闭连接；模块级路径指向它。"""
    p = str(tmp_path / "touch_lake.duckdb")
    con = duckdb.connect(p)
    con.execute("CREATE TABLE kline_daily (ts_code VARCHAR, date DATE, "
                "low DOUBLE, close DOUBLE)")
    con.close()
    monkeypatch.setattr(st, "_LAKE_PATH", p)
    return p


@pytest.fixture()
def no_sleep(monkeypatch):
    monkeypatch.setattr(st, "_LAKE_RETRY_DELAY_S", 0)


def _ins(path, *rows):
    con = duckdb.connect(path)
    for r in rows:
        con.execute("INSERT INTO kline_daily VALUES (?,?,?,?)",
                    [r[0], r[1], r[2], r[3]])
    con.close()


def _fake_ctx(pool_codes):
    pool = types.SimpleNamespace(codes=pool_codes)
    con = types.SimpleNamespace(close=lambda: None)
    return (object(), object(), con, pool)


def _patch_run_deps(monkeypatch, spot_result, pool_codes=("c1",)):
    """patch run() 外部依赖：池上下文/状态机/行情/触碰计算（捕获 spot）。"""
    monkeypatch.setattr(st, "USER_POSITIONS_PATH", "/nonexistent_up.json")
    import web.app as wapp
    monkeypatch.setattr(wapp, "_load_pool_ctx",
                        lambda *a, **k: _fake_ctx(pool_codes))
    monkeypatch.setattr(st, "state_rows", lambda *a, **k: {})
    import screener.data.realtime as rt
    monkeypatch.setattr(rt, "fetch_spot", lambda *a, **k: spot_result)
    captured = {}

    def fake_compute(rows, spot, sp, held_codes=frozenset()):
        captured["spot"] = dict(spot)
        return []

    import martingale.touches as touches
    monkeypatch.setattr(touches, "compute_touches", fake_compute)
    return captured


def test_lake_spot_latest_bar_per_code(lake_path, no_sleep):
    # c1 最新 bar 11-18（low 8.0）；c2 停牌停在 11-15（low 5.0）
    _ins(lake_path,
         ("c1", "2026-11-17", 8.5, 8.6),
         ("c1", "2026-11-18", 8.0, 8.5),
         ("c2", "2026-11-15", 5.0, 5.5))
    spot = st._lake_spot(["c1", "c2", "c3"])
    assert spot["c1"]["low"] == 8.0 and spot["c1"]["timestamp"] == "20261118"
    assert spot["c2"]["low"] == 5.0 and spot["c2"]["timestamp"] == "20261115"
    assert "c3" not in spot                      # 无 bar → 剔除
    assert spot["c1"]["price"] == 8.5


def test_lake_spot_empty_lake(lake_path, no_sleep):
    assert st._lake_spot(["c1"]) == {}


def test_run_falls_back_to_lake(monkeypatch, lake_path, no_sleep):
    _ins(lake_path, ("c1", "2026-11-18", 8.0, 8.5))
    captured = _patch_run_deps(monkeypatch, spot_result=({}, ""))
    rc = st.run(dry_run=True)
    assert rc == 0                                 # 不是 4
    assert captured["spot"].get("c1", {}).get("low") == 8.0


def test_run_both_fail_rc4(monkeypatch, lake_path, no_sleep):
    # 湖空（无 kline 行）→ 兜底也空 → rc 4
    captured = _patch_run_deps(monkeypatch, spot_result=({}, ""))
    rc = st.run(dry_run=True)
    assert rc == 4
    assert "spot" not in captured                  # 未走到触碰计算


def test_run_spot_ok_no_lake(monkeypatch, lake_path, no_sleep):
    # 实时源正常（整批有 low）→ 不碰湖
    captured = _patch_run_deps(
        monkeypatch,
        spot_result=({"c1": {"low": 9.0, "price": 9.2,
                             "timestamp": "20261118150000"}}, "tencent"))
    rc = st.run(dry_run=True)
    assert rc == 0
    assert captured["spot"]["c1"]["low"] == 9.0    # 实时值，非湖值


def test_lake_lock_retry(monkeypatch, lake_path, no_sleep):
    # 前 2 次开湖被锁 → 第 3 次成功
    _ins(lake_path, ("c1", "2026-11-18", 7.0, 7.5))
    real_connect = duckdb.connect

    def flaky(path, *a, **k):
        nonlocal n
        n += 1
        if n <= 2:
            raise RuntimeError("数据库被其他进程锁定（backfill 灌数进行中）")
        return real_connect(path, *a, **k)

    n = 0
    monkeypatch.setattr(duckdb, "connect", flaky)
    spot = st._lake_spot(["c1"])
    assert spot["c1"]["low"] == 7.0
    assert n == 3


def test_lake_lock_exhausted(monkeypatch, lake_path, no_sleep):
    # 一直锁 → 重试耗尽 → {}（不抛）
    def locked(*a, **k):
        raise RuntimeError("locked")

    monkeypatch.setattr(duckdb, "connect", locked)
    assert st._lake_spot(["c1"]) == {}


def test_run_pool_ctx_lock_retry(monkeypatch, lake_path, no_sleep):
    # 池上下文开湖前 1 次被锁 → 重试成功 → 正常跑完
    import web.app as wapp

    calls = {"n": 0}

    def flaky_ctx(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("数据湖被其他进程锁定")
        return _fake_ctx(("c1",))

    monkeypatch.setattr(wapp, "_load_pool_ctx", flaky_ctx)
    monkeypatch.setattr(st, "state_rows", lambda *a, **k: {})
    import screener.data.realtime as rt
    monkeypatch.setattr(rt, "fetch_spot",
                        lambda *a, **k: ({"c1": {"low": 9.0,
                                                 "price": 9.2,
                                                 "timestamp": "20261118150000"}},
                                         "tencent"))
    import martingale.touches as touches
    monkeypatch.setattr(touches, "compute_touches",
                        lambda *a, **k: [])
    monkeypatch.setattr(st, "USER_POSITIONS_PATH", "/nonexistent_up.json")
    rc = st.run(dry_run=True)
    assert rc == 0
    assert calls["n"] == 2


def test_run_pool_ctx_nonlock_no_retry(monkeypatch, lake_path, no_sleep):
    # 非锁异常（配置错等）→ 不重试，直接 rc 3
    import web.app as wapp

    def bad_ctx(*a, **k):
        raise RuntimeError("策略配置错误")

    monkeypatch.setattr(wapp, "_load_pool_ctx", bad_ctx)
    rc = st.run(dry_run=True)
    assert rc == 3
