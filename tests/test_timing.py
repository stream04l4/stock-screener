# -*- coding: utf-8 -*-
"""test_timing —— v7.0.13：选股时机（候选池 ①价格分位 ②股息率 ③年线）。

契约（全离线，零真实湖/零网络）：
- config：timing 段缺省 → 生产默认值；段存在时 5 键齐全 + 范围/类型校验；
- refresh_timing：①分位/回撤计算、②③ 参考展示列（v7.0.24 起不参与门）、
  门 = ① 单条件（数据不足 fail-closed）、整表刷新幂等；
- v7.0.25 起时机无独立微信推送（check_and_push 已退役——微信统一为
  「进入当前信号才推」，由 scripts/position_alert.py 负责）；
- Web GET /api/timing（fake 湖：命中排序/push_eligible 标记/空表 fail-closed）。
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
    base = dict(price_window_years=1, price_pctile_max=50.0, yield_min_pct=4.5,
                below_ma250=False, ma_period=250)
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
    y = """
strategy:
  name: drawdown_martingale
  initial_amount: 100000
  base_add_amount: 10000
  first_drawdown_pct: 15
  add_step_pct: 5
  growth_ratio: 1.1
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
  yield_min_pct: 4.5
  below_ma250: false
  ma_period: 250
"""
    return y


def test_config_timing_defaults_when_missing(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text(_valid_yaml(with_timing=False), encoding="utf-8")
    _sp, _up, tp = load_config(str(p))
    assert tp == TimingParams()  # 缺省 = 生产默认（5/40/4.5/false/250）


def test_config_timing_valid_roundtrip(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text(_valid_yaml(), encoding="utf-8")
    _sp, _up, tp = load_config(str(p))
    assert (tp.price_window_years, tp.price_pctile_max, tp.yield_min_pct,
            tp.below_ma250, tp.ma_period) == (5, 40.0, 4.5, False, 250)


@pytest.mark.parametrize("mutate,match", [
    (lambda d: d["timing"].pop("ma_period"), "ma_period 缺失"),
    (lambda d: d["timing"].update(price_pctile_max=150), "price_pctile_max"),
    (lambda d: d["timing"].update(yield_min_pct=-1), "yield_min_pct"),
    (lambda d: d["timing"].update(below_ma250="yes"), "below_ma250 必须是布尔"),
    (lambda d: d["timing"].update(price_window_years=0), "price_window_years"),
    (lambda d: d["timing"].update(ma_period=10), "ma_period"),
])
def test_config_timing_rejects_invalid(tmp_path, mutate, match):
    d = yaml.safe_load(_valid_yaml())
    mutate(d)
    p = tmp_path / "s.yaml"
    p.write_text(yaml.safe_dump(d, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ConfigError, match=match):
        load_config(str(p))


# ---------------------------------------------------------------------------
# 2) refresh_timing：条件逻辑
# ---------------------------------------------------------------------------
def test_refresh_timing_conditions():
    N = 300
    series = {
        "a.1": [10.0] * N,                 # 分位 100（高位）+ 股息 5.0 → ①✗ 不进门
        "b.2": [20.0] * (N - 1) + [10.0],  # 分位 ≈0.3（低位）+ 股息 4.6 → ①✓ → 进门
        "c.3": [10.0] * N,                 # 高位 + 股息 3.0 → 无触发
    }
    con = make_lake(series, price_only=["d.4"])  # d.4 无 qfq（fail-closed）
    members = [member("a.1", 5.0), member("b.2", 4.6),
               member("c.3", 3.0), member("d.4", 6.0)]
    tp = make_tp()
    out = tm.refresh_timing(con, up=None, tp=tp, members=members)
    # v7.0.24：门 = ① 单条件 → 仅 b.2 进观察/提醒
    assert out["n_codes"] == 4 and out["n_watchlist"] == 1 and out["n_push"] == 1

    rows = {r[0]: r for r in con.execute(f"SELECT * FROM {tm.TABLE}").fetchall()}
    # 列序: ts_code(0) name(1) as_of(2) close(3) pctile(4) dd(5) yield(6)
    #       ma(7) below_ma(8) c1(9) c2(10) c3(11) watch(12) push(13) reasons(14)
    # a.1: ①✗ → 不进门（② 仅参考展示，不参与门）
    assert rows["a.1"][9] is False and rows["a.1"][10] is True
    assert rows["a.1"][12] is False and rows["a.1"][13] is False
    # b.2: ①✓ → 进门；reasons 只含 ①（②③ 不再进触发原因）
    assert rows["b.2"][9] is True and rows["b.2"][10] is True
    assert rows["b.2"][12] is True and rows["b.2"][13] is True
    assert abs(rows["b.2"][4] - 1 / N * 100) < 0.01      # 分位 ≈ 0.33%
    assert abs(rows["b.2"][5] - 50.0) < 1e-9             # 回撤 50%
    assert "价格分位" in rows["b.2"][14] and "股息率" not in rows["b.2"][14]
    assert rows["c.3"][12] is False and rows["c.3"][13] is False
    # d.4 无 qfq → ① fail-closed False（② 命中也不进门）
    assert rows["d.4"][9] is False and rows["d.4"][10] is True
    assert rows["d.4"][4] is None and rows["d.4"][5] is None
    assert rows["d.4"][12] is False and rows["d.4"][13] is False

    # 整表刷新幂等（二次刷新不累积）
    tm.refresh_timing(con, up=None, tp=tp, members=members)
    assert con.execute(f"SELECT COUNT(*) FROM {tm.TABLE}").fetchone()[0] == 4
    con.close()


def test_refresh_timing_below_ma():
    N = 300
    series = {
        "a.1": [10.0] * N,                       # 均线=10，close<ma 假
        "b.2": [20.0] * (N - 1) + [10.0],        # 均线≈19.9，close<ma 真
        "e.5": [10.0] * 100,                     # 不足 ma_period → ma None（fail-closed）
    }
    con = make_lake(series)
    members = [member("a.1", 5.0), member("b.2", 4.6), member("e.5", 4.6)]
    tp = make_tp(below_ma250=True, ma_period=250)
    tm.refresh_timing(con, up=None, tp=tp, members=members)
    rows = {r[0]: r for r in con.execute(f"SELECT * FROM {tm.TABLE}").fetchall()}
    # v7.0.24：门 = ① 单条件（③ 仅参考展示，不关闸）
    # a.1: ①✗ → 不进门（③✗ 也不影响门，仅数据列展示）
    assert rows["a.1"][9] is False and rows["a.1"][10] is True and rows["a.1"][11] is False
    assert rows["a.1"][12] is False and rows["a.1"][13] is False
    # b.2: ①✓ → 进门（②③ 不参与判定）；reasons 只含 ①
    assert rows["b.2"][9] and rows["b.2"][10] and rows["b.2"][11]
    assert rows["b.2"][12] and rows["b.2"][13]
    assert "价格分位" in rows["b.2"][14] and "低于250日线" not in rows["b.2"][14]
    # e.5: 数据不足 → ma None、③ NULL（不显示）；①✗ → 不进门
    assert rows["e.5"][7] is None and rows["e.5"][8] is None and rows["e.5"][11] is None
    assert rows["e.5"][9] is False and rows["e.5"][10] is True
    assert rows["e.5"][12] is False and rows["e.5"][13] is False
    con.close()
