# -*- coding: utf-8 -*-
"""test_timing —— v7.0.13：选股时机（候选池 ①价格分位；v7.0.26 起 ②③ 已删）。

契约（全离线，零真实湖/零网络）：
- config：timing 段缺省 → 生产默认值（且 first_drawdown_pct 回退读 strategy
  段，兼容旧快照）；段存在时 3 键齐全 + 范围/类型校验；
- refresh_timing：①分位/回撤计算、门 = ① 单条件（数据不足 fail-closed）、
  整表刷新幂等（v7.0.26 新 schema 10 列，②③ 列删除）；
- v7.0.25 起时机无独立微信推送（check_and_push 已退役——微信统一为
  「进入当前信号才推」，由 scripts/position_alert.py 负责）。
"""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Dict, List

import duckdb
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import web.app as wapp  # noqa: E402
from martingale.config import (ConfigError, TimingParams, UniverseParams,  # noqa: E402
                               load_config)
from martingale import timing as tm  # noqa: E402
from martingale.universe import Member  # noqa: E402


def make_tp(**kw) -> TimingParams:
    base = dict(price_window_years=1, price_pctile_max=50.0)
    base.update(kw)
    return TimingParams(**base)


def member(code, yield_pct=4.0, price=10.0) -> Member:
    return Member(ts_code=code, name=f"股{code}", industry_csric2="B06",
                  soe_basis="关键词命中", total_mv=300.0, ttm_yield_pct=yield_pct,
                  n_bars=2400, list_date="2010-01-01",
                  bt_pnl_pct=0.1, bt_n_buys=3, price_latest=price, is_manual=False)


def make_lake(series: Dict[str, List[float]],
              price_only: List[str] = ()) -> duckdb.DuckDBPyConnection:
    """内存湖：kline_daily（最新价）+ kline_daily_qfq（窗口序列，日期自 2025-10-24 起逐日）。

    :param price_only: 只有 kline_daily 最新价、无 qfq 序列的代码（fail-closed 用例）。
    """
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE kline_daily (ts_code VARCHAR, date DATE, close DOUBLE)")
    con.execute("""CREATE TABLE kline_daily_qfq (ts_code VARCHAR, date DATE,
                   open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
                   volume DOUBLE, amount DOUBLE, pct_chg DOUBLE, is_st INTEGER)""")
    from datetime import date, timedelta
    start = date(2025, 10, 24)
    for code, closes in series.items():
        for i, c in enumerate(closes):
            d = start + timedelta(days=i)
            con.execute("INSERT INTO kline_daily_qfq VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0, 0)",
                        [code, str(d), c, c, c, c])
        con.execute("INSERT INTO kline_daily VALUES (?, ?, ?)",
                    [code, str(start + timedelta(days=len(closes) - 1)), closes[-1]])
    for code in price_only:
        con.execute("INSERT INTO kline_daily VALUES (?, ?, ?)",
                    [code, "2026-10-23", 10.0])
    return con


# ---------------------------------------------------------------------------
# 1) config：timing 段缺省 + 校验
# ---------------------------------------------------------------------------
def _valid_yaml(with_timing: bool = True) -> str:
    # with_timing=True：v7.0.26 新布局（timing 3 键，dd 居 timing 段）；
    # with_timing=False：旧快照布局（无 timing 段，dd 在 strategy 段 → 回退兼容）
    strategy_dd = "" if with_timing else "  first_drawdown_pct: 15\n"
    y = f"""
strategy:
  name: drawdown_martingale
  lot_size: 100
  first_lots: 1
  add_lots_step: 1
{strategy_dd}  add_step_pct: 5
  rearm_mode: bounce
  rearm_pct: 15
  accounting: cash
universe:
  industry_whitelist_csric2: [B06]
  soe_keywords: [国资]
  min_total_mv_yi: 200
  min_consecutive_div_years: 5
  listing_min_trading_days: 250
  backtest_positive: true
  backtest_window_years: 10
  min_ttm_yield_pct: 3
  price_min_yuan: 3
  price_max_yuan: 100
"""
    if with_timing:
        y += """
timing:
  price_window_years: 5
  price_pctile_max: 40
  first_drawdown_pct: 15
"""
    return y


def test_config_timing_defaults_when_missing(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text(_valid_yaml(with_timing=False), encoding="utf-8")
    sp, _up, tp = load_config(str(p))
    assert tp == TimingParams()  # 缺省 = 生产默认（5/40）
    # 无 timing 段 → first_drawdown 回退读 strategy 段（旧布局兼容）
    assert sp.first_drawdown == pytest.approx(0.15)


def test_config_timing_valid_roundtrip(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text(_valid_yaml(), encoding="utf-8")
    sp, _up, tp = load_config(str(p))
    assert (tp.price_window_years, tp.price_pctile_max) == (5, 40.0)
    # v7.0.26：first_drawdown 取自 timing 段
    assert sp.first_drawdown == pytest.approx(0.15)


@pytest.mark.parametrize("mutate,match", [
    (lambda d: d["timing"].pop("first_drawdown_pct"), "first_drawdown_pct 缺失"),
    (lambda d: d["timing"].update(price_pctile_max=150), "price_pctile_max"),
    (lambda d: d["timing"].update(price_window_years=0), "price_window_years"),
    (lambda d: d["timing"].update(first_drawdown_pct=95), "first_drawdown_pct"),
])
def test_config_timing_rejects_invalid(tmp_path, mutate, match):
    d = yaml.safe_load(_valid_yaml())
    mutate(d)
    p = tmp_path / "s.yaml"
    p.write_text(yaml.safe_dump(d, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ConfigError, match=match):
        load_config(str(p))


# ---------------------------------------------------------------------------
# 2) refresh_timing：条件逻辑（v7.0.26 新 schema：10 列）
# ---------------------------------------------------------------------------
def test_refresh_timing_conditions():
    N = 300
    series = {
        "a.1": [10.0] * N,                 # 分位 100（高位）→ ①✗ 不进门
        "b.2": [20.0] * (N - 1) + [10.0],  # 分位 ≈0.3（低位）→ ①✓ → 进门
        "c.3": [10.0] * N,                 # 高位 → 无触发
    }
    con = make_lake(series, price_only=["d.4"])  # d.4 无 qfq（fail-closed）
    members = [member("a.1", 5.0), member("b.2", 4.6),
               member("c.3", 3.0), member("d.4", 6.0)]
    tp = make_tp()
    out = tm.refresh_timing(con, up=None, tp=tp, members=members)
    # v7.0.24：门 = ① 单条件 → 仅 b.2 进观察名单
    assert out["n_codes"] == 4 and out["n_watchlist"] == 1 and out["n_push"] == 1

    rows = {r[0]: r for r in con.execute(f"SELECT * FROM {tm.TABLE}").fetchall()}
    # 列序（v7.0.26）: ts_code(0) name(1) as_of(2) close(3) pctile(4) dd(5)
    #       c1(6) watch(7) push(8) reasons(9)
    # a.1: ①✗ → 不进门
    assert rows["a.1"][6] is False
    assert rows["a.1"][7] is False and rows["a.1"][8] is False
    # b.2: ①✓ → 进门；reasons 只含 ①
    assert rows["b.2"][6] is True
    assert rows["b.2"][7] is True and rows["b.2"][8] is True
    assert abs(rows["b.2"][4] - 1 / N * 100) < 0.01      # 分位 ≈ 0.33%
    assert abs(rows["b.2"][5] - 50.0) < 1e-9             # 回撤 50%
    assert "价格分位" in rows["b.2"][9] and "股息率" not in rows["b.2"][9]
    assert rows["c.3"][7] is False and rows["c.3"][8] is False
    # d.4 无 qfq → ① fail-closed False
    assert rows["d.4"][6] is False
    assert rows["d.4"][4] is None and rows["d.4"][5] is None
    assert rows["d.4"][7] is False and rows["d.4"][8] is False

    # 整表刷新幂等（二次刷新不累积；v7.0.26 drop+重建后仍 4 行）
    tm.refresh_timing(con, up=None, tp=tp, members=members)
    assert con.execute(f"SELECT COUNT(*) FROM {tm.TABLE}").fetchone()[0] == 4
    con.close()
