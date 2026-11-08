# -*- coding: utf-8 -*-
"""test_cli_af_guard —— v7.0.21：selection/timing CLI 的 af 基准预对齐护栏。

背景（2026-11-08 大秦铁路 5 年分位=0.08% 假值复盘）：
湖增量换源写入假基准跳变后，对齐要等下一轮 sync 前置收口（10 天窗口、次日跑）；
期间任何一次 `python -m martingale timing/selection`（周末手动运维尤甚）都会基于
断步 qfq 序列算出污染快照。护栏 = 命令开跑前先跑同一幂等 rebase_recent(days=10)。

契约（全离线，零真实湖/零网络）：
- _af_prealign：合成湖带未对齐假跳变 → 调用后边界恢复连续（|log(af_move)|≤tol）；
- _cmd_timing：refresh_timing 被调用**之前**跳变已被对齐（顺序断言）+ 正常收尾。
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import duckdb
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import martingale.__main__ as mmain  # noqa: E402
from martingale.config import TimingParams  # noqa: E402
from martingale import timing as tm  # noqa: E402


def _make_lake_with_jump(path: Path) -> Path:
    """文件湖：单股 6 天，第 5 天 af 假降 20%（无除权事件）→ 待对齐跳变。"""
    con = duckdb.connect(str(path))
    con.execute("""CREATE TABLE kline_daily (
        ts_code VARCHAR, date DATE, close DOUBLE, adj_factor DOUBLE)""")
    con.execute("""CREATE TABLE dividend_events (
        ts_code VARCHAR, ex_date DATE)""")
    days = ["2026-11-01", "2026-11-02", "2026-11-03",
            "2026-11-04", "2026-11-05", "2026-11-06"]
    closes = [10.0, 10.1, 10.05, 10.0, 9.95, 10.0]
    afs = [1.0, 1.0, 1.0, 1.0, 0.8, 0.8]  # 11-04→11-05 假跳变 -20%
    for d, c, a in zip(days, closes, afs):
        con.execute("INSERT INTO kline_daily VALUES (?, ?, ?, ?)",
                    ["sh.600000", d, c, a])
    con.execute("CHECKPOINT")
    con.close()
    return path


def _jump_boundary_af(con):
    """返回 (af_1104, af_1105)：DESC LIMIT 3 的 rows[2] / rows[1]（跳变边界）。"""
    rows = con.execute(
        "SELECT adj_factor FROM kline_daily "
        "WHERE ts_code='sh.600000' ORDER BY date DESC LIMIT 3").fetchall()
    return rows[2][0], rows[1][0]  # (af_prev=11-04, af_last=11-05)


def test_af_prealign_fixes_pending_fake_jump(tmp_path):
    p = _make_lake_with_jump(tmp_path / "lake.duckdb")
    con = duckdb.connect(str(p))
    af_prev, af_last = _jump_boundary_af(con)
    assert abs(math.log(af_last / af_prev)) > 0.02  # 前置：跳变确实在
    try:
        mmain._af_prealign(con)
    finally:
        con.close()
    con = duckdb.connect(str(p), read_only=True)
    af_prev, af_last = _jump_boundary_af(con)
    con.close()
    # 护栏后边界恢复连续（af_rebase DEFAULT_TOL=0.02 内）
    assert abs(math.log(af_last / af_prev)) <= 0.02


def test_af_prealign_idempotent_no_jumps(tmp_path):
    p = _make_lake_with_jump(tmp_path / "lake.duckdb")
    con = duckdb.connect(str(p))
    mmain._af_prealign(con)
    af_prev, af_last = _jump_boundary_af(con)
    # 二次调用：无跳变 → 零改写（af 不变）
    mmain._af_prealign(con)
    con.close()
    con = duckdb.connect(str(p), read_only=True)
    r = con.execute(
        "SELECT adj_factor FROM kline_daily "
        "WHERE ts_code='sh.600000' ORDER BY date DESC LIMIT 1").fetchone()
    con.close()
    assert abs(r[0] - af_last) < 1e-12


def test_cmd_timing_prealigns_before_refresh(tmp_path, monkeypatch, capsys):
    """_cmd_timing 顺序断言：refresh_timing 被调时假跳变已对齐。"""
    p = _make_lake_with_jump(tmp_path / "lake.duckdb")
    seen = {}

    def fake_refresh(con, up, tp, verbose=False):
        af_prev, af_last = _jump_boundary_af(con)
        seen["aligned_before_refresh"] = (
            abs(math.log(af_last / af_prev)) <= 0.02)
        return {"as_of": "2026-11-06", "n_codes": 1, "n_watchlist": 0,
                "n_push": 0, "elapsed_s": 0.01}

    monkeypatch.setattr(tm, "refresh_timing", fake_refresh)
    monkeypatch.setattr(mmain, "load_config", lambda path: (object(), object(),
                                                            TimingParams(
                                                                price_window_years=1,
                                                                price_pctile_max=50.0,
                                                                yield_min_pct=4.5,
                                                                below_ma250=False,
                                                                ma_period=250)))
    monkeypatch.setattr(mmain, "default_db_path", lambda: str(p))
    rc = mmain._cmd_timing(
        argparse.Namespace(no_push=True, dry_run=True))
    assert rc == 0
    assert seen["aligned_before_refresh"] is True
