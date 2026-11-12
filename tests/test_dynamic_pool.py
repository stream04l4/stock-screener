# -*- coding: utf-8 -*-
"""v8.0.13 动态池回测单测（合成湖，零网络，确定性）。

覆盖：
1. month_starts（每月第一个交易日）
2. build_pool_asof PIT（holders 快照切换 / 退市 as_of 判定）
3. pool_schedule 门控（SOE / G8 TTM / G7 滚动回测，ROLLING_DAYS 缩窗加速）
4. build_code_mask 段语义（首重算日前=禁交易；出池段=False）
5. 引擎 trade_mask（冻结买入/持仓保留/分红照收/全 True 与 None 恒等）
"""
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import duckdb  # noqa: E402

import lake.ddl as lake_ddl  # noqa: E402
from martingale import dynamic_pool as dp  # noqa: E402
from martingale.config import StrategyParams  # noqa: E402
from martingale.engine import run_stock  # noqa: E402
from martingale.config import UniverseParams  # noqa: E402

# ---------------------------------------------------------------------------
# 合成湖（模块级建一次）
# ---------------------------------------------------------------------------
# 日历：2022-01 ~ 2024-12 工作日
CAL = [d for d in np.arange("2022-01-03", "2025-01-01", dtype="datetime64[D]")
       if d.astype("datetime64[D]").astype(object).weekday() < 5]
CAL_STR = [str(d)[:10] for d in CAL]

# A 价格：强上升趋势 + 周期性深 V（保证任意滚动窗内 ≥1 次触发且收益为正）
def _price_a(d):
    i = CAL_STR.index(str(d)[:10])
    base = 10.0 + 1.5 * i / len(CAL)          # 10 → ~14.5 趋势
    cyc = i % 120
    if 60 <= cyc < 70:
        base *= 0.78                          # 深 V（-22%）
    elif 70 <= cyc < 80:
        base *= 0.78 + 0.22 * (cyc - 60) / 10  # 恢复
    return round(base, 2)

UP = dict(industry_whitelist_csric2=("B06",), soe_keywords=("国资",),
          min_total_mv_yi=200.0, min_consecutive_div_years=5,
          listing_min_trading_days=250, backtest_positive=True,
          backtest_window_years=10, backtest_min_return_pct=5.0,
          min_ttm_yield_pct=3.0, price_min_yuan=3.0, price_max_yuan=100.0,
          min_soe_ratio_pct=8.0, min_soe_sum_ratio_pct=30.0,
          exclude_codes=(), soe_control_overrides=())

SP = StrategyParams(name="drawdown_martingale", lot_size=100, first_lots=1,
                    first_drawdown=0.2, add_step=0.05, add_lots_step=1,
                    rearm_mode="newhigh", rearm_pct=0.05, accounting="cash")


@pytest.fixture(scope="module")
def con():
    c = duckdb.connect(":memory:")
    lake_ddl.init_schema(c)
    # stock_master
    c.executemany(
        "INSERT INTO stock_master VALUES (?, ?, 'B06', NULL, DATE '2010-01-01', ?, "
        "NULL, 0, NULL, '1', '国有股性质', 't', CURRENT_TIMESTAMP, 'v', NULL)",
        [("sh.600001", "国资甲", None), ("sh.600002", "非国乙", None),
         ("sh.600003", "零息丙", None), ("sh.600004", "退市丁",
          "2024-06-30")])
    # K线：A 周期深 V；B/C/D 平直 10
    rows = []
    for d in CAL_STR:
        rows.append(("sh.600001", d, _price_a(d)))
        for code in ("sh.600002", "sh.600003", "sh.600004"):
            rows.append((code, d, 10.0))
    c.executemany(
        "INSERT INTO kline_daily VALUES (?, ?, ?, ?, ?, ?, 1000, 10000, 0.1, 0, ?,"
        " 1.0, 't', CURRENT_TIMESTAMP, 'v', NULL)",
        [(code, d, p, p, p, p, p) for code, d, p in rows])
    # 分红：A 每年 7 月 0.5 元（2018~2024）
    c.executemany(
        "INSERT INTO dividend_events VALUES (?, CAST(? AS DATE), CAST(? AS DATE), NULL, 0.5, 0.0,"
        " 't', CURRENT_TIMESTAMP, 'v', NULL)",
        [(code, f"{y}-07-15", f"{y}-07-01")
         for code in ("sh.600001", "sh.600004") for y in range(2018, 2025)])
    # 估值：A/D total_mv 500 ttm 5.0；C ttm NULL
    vrows = []
    for d in CAL_STR:
        vrows.append(("sh.600001", d, 500.0, 5.0))
        vrows.append(("sh.600004", d, 500.0, 5.0))
        vrows.append(("sh.600003", d, 500.0, None))
    c.executemany(
        "INSERT INTO valuation_daily VALUES (?, ?, 500.0, 300.0, 10.0, 1.0, 0.1,"
        " ?, 't', CURRENT_TIMESTAMP, 'v', NULL)",
        [(code, d, t) for code, d, _, t in
         [(r[0], r[1], r[2], r[3]) for r in vrows]])
    # holders：A 2022Q4 国有 40% → 2024-06-30 转民营（SOE 翻转）；
    #          C 国有 30%；B 民营；D 国有 30%
    c.executemany(
        "INSERT INTO holders_snapshot VALUES (?, ?, 1, ?, ?, ?,"
        " 't', CURRENT_TIMESTAMP, 'v', NULL)",
        [("sh.600001", "2022-12-31", "甲国资集团", 40.0, "国有股"),
         ("sh.600001", "2024-06-30", "甲民营资本", 45.0, "社会"),
         ("sh.600002", "2022-12-31", "乙个人", 50.0, "社会"),
         ("sh.600003", "2022-12-31", "丙国资", 30.0, "国有股"),
         ("sh.600004", "2022-12-31", "丁国资", 30.0, "国有股")])
    yield c
    c.close()


# ---------------------------------------------------------------------------
# 1) month_starts
# ---------------------------------------------------------------------------
def test_month_starts():
    d = np.array(CAL_STR, dtype="datetime64[ns]")
    ms = dp.month_starts(d)
    got = [str(x)[:10] for x in ms]
    assert got[0] == "2022-01-03"
    assert "2023-07-03" in got      # 2023-07-01/2 为周末
    assert all(str(x)[:7] != str(y)[:7] for x, y in zip(got, got[1:]))


# ---------------------------------------------------------------------------
# 2) build_pool_asof PIT
# ---------------------------------------------------------------------------
def test_build_pool_asof_pit(con):
    up = UniverseParams(**UP)
    from martingale.universe import build_pool_asof
    pool1 = build_pool_asof(con, up, "2023-06-01")
    c1 = {m.ts_code for m in pool1.members}
    assert "sh.600001" in c1            # 快照 2022Q4 国有 40% 过强度门
    assert "sh.600004" in c1            # 未退市（delist 2024-06-30 > as_of）
    assert "sh.600002" not in c1        # 非 SOE
    pool2 = build_pool_asof(con, up, "2024-09-01")
    c2 = {m.ts_code for m in pool2.members}
    assert "sh.600001" not in c2        # 2024-06-30 快照转民营 → SOE 翻转
    assert "sh.600004" not in c2        # 已退市（delist ≤ as_of）


# ---------------------------------------------------------------------------
# 3) pool_schedule 门控（ROLLING_DAYS 缩到 250 天加速）
# ---------------------------------------------------------------------------
def test_pool_schedule_gates(con, monkeypatch):
    monkeypatch.setattr(dp, "ROLLING_DAYS", 250)
    up = UniverseParams(**UP)
    sched = dp.pool_schedule(con, SP, up, "2023-01-02", "2024-12-31",
                             progress=None)
    assert sched
    allp = set().union(*sched.values())
    assert "sh.600002" not in allp      # B：SOE 关
    assert "sh.600003" not in allp      # C：G8 TTM None
    # A：2023 年在池（快照国有）
    j23 = [d for d in sorted(sched) if d.startswith("2023")]
    assert any("sh.600001" in sched[d] for d in j23)
    # A：2024-07 起出池（SOE 翻转于 2024-06-30 快照）
    assert all("sh.600001" not in sched[d] for d in sorted(sched)
               if d >= "2024-07-01")
    # 重算日 = 每月第一个交易日
    ms = [str(x)[:10] for x in dp.month_starts(
        np.array(CAL_STR, dtype="datetime64[ns]"))]
    assert set(sched) <= set(ms)


# ---------------------------------------------------------------------------
# 4) build_code_mask 段语义
# ---------------------------------------------------------------------------
def test_build_code_mask_segments():
    dates = np.array(["2023-02-01", "2023-03-15", "2023-06-15", "2023-07-01"],
                     dtype="datetime64[ns]")
    sched = {"2023-03-01": {"sh.600001"}, "2023-06-01": set()}
    m = dp.build_code_mask("sh.600001", dates, sorted(sched), sched)
    # 02-01 首重算前=False；03-15 在池段=True；06-15/07-01 出池段=False
    assert list(m) == [False, True, False, False]
    # 非成员恒 False
    m2 = dp.build_code_mask("sh.600002", dates, sorted(sched), sched)
    assert not m2.any()
    # 空调度 → 全 False
    m3 = dp.build_code_mask("sh.600001", dates, [], {})
    assert not m3.any()


# ---------------------------------------------------------------------------
# 5) 引擎 trade_mask
# ---------------------------------------------------------------------------
def _engine_case():
    """100 bar：bar20-24 深 V 触发首仓；bar45-49 第二次 V（再武装后 k0 加仓）；
    bar50 现金分红 0.3。"""
    n = 100
    dates = (np.datetime64("2023-01-01") + np.arange(n)).astype("datetime64[ns]")
    p = np.full(n, 10.0)
    p[:20] = 10.0
    p[20:25] = [9.6, 9.1, 8.6, 8.2, 7.8]     # 深 V → k0 买（level 8.0）
    p[25:45] = np.linspace(8.2, 11.0, 20)    # 回升创新高 → rearm
    p[45:50] = [10.4, 9.9, 9.4, 9.0, 8.7]    # 第二次 V（entry_high≈11 → level 8.8）
    p[50:] = np.linspace(9.0, 12.0, 50)
    sig = exec_c = p
    exmap = {dates[50]: (0.3, 0.0)}
    return dates, sig, exec_c, exmap


def _stat(r):
    stat, state, v, c, fl, buys = r
    return stat, buys


def test_engine_mask_identity_all_true():
    dates, sig, exec_c, exmap = _engine_case()
    s_none, _ = _stat(run_stock("t", dates, sig, exec_c, exmap, SP, verbose=False))
    s_true, _ = _stat(run_stock("t", dates, sig, exec_c, exmap, SP, verbose=False,
                                trade_mask=np.ones(len(dates), dtype=bool)))
    for k in ("n_buys", "total_pnl", "pnl_pct", "div_pool", "price_pnl",
              "last_status", "first_buy_date", "last_buy_date"):
        assert s_none[k] == s_true[k], k


def test_engine_mask_freeze_first_buy():
    dates, sig, exec_c, exmap = _engine_case()
    mask = np.ones(len(dates), dtype=bool)
    mask[18:27] = False                       # 首仓触发段（V1）冻结
    mask[43:52] = False                       # 加仓触发段（V2）也冻结
    s, buys = _stat(run_stock("t", dates, sig, exec_c, exmap, SP, verbose=False,
                              trade_mask=mask))
    assert s["n_buys"] == 0 and s["last_status"] == "never"
    assert buys == []
    # 无冻结 → 有买入（V1 + V2 各一次）
    s2, _ = _stat(run_stock("t", dates, sig, exec_c, exmap, SP, verbose=False))
    assert s2["n_buys"] >= 2


def test_engine_mask_exit_freezes_add_buys_keeps_position():
    dates, sig, exec_c, exmap = _engine_case()
    mask = np.ones(len(dates), dtype=bool)
    mask[43:52] = False                       # 出池：第二次 V（加仓触发）被冻结
    s, _ = _stat(run_stock("t", dates, sig, exec_c, exmap, SP, verbose=False,
                           trade_mask=mask))
    s_free, _ = _stat(run_stock("t", dates, sig, exec_c, exmap, SP, verbose=False))
    assert s["n_buys"] == s_free["n_buys"] - 1   # 少了冻结段的加仓
    assert s["n_buys"] >= 1                       # 首仓保留（持仓未平）
    # 分红照收（bar50 在冻结段内，持仓存在 → 现金照进余额；按股数 100/200 折半）
    assert s["dividends"] == pytest.approx(30.0)
    assert s_free["dividends"] == pytest.approx(60.0)
    assert s["div_pool"] == pytest.approx(30.0)
    assert s_free["div_pool"] == pytest.approx(60.0)


# ---------------------------------------------------------------------------
# 6) 默认窗口 + 端到端产物（v8.0.13：空窗口动态池 = 最近 ROLLING_DAYS，非全跨度）
# ---------------------------------------------------------------------------
def test_dynamic_run_default_window_and_artifacts(con, tmp_path, monkeypatch):
    import json as _json
    import time as _time
    monkeypatch.setattr(dp, "ROLLING_DAYS", 730)
    up = UniverseParams(**UP)
    rc = dp.run_dynamic_pool_backtest(con, SP, up, None, None,
                                      str(tmp_path), _time.time(), "synthetic")
    assert rc == 0
    status = _json.loads((tmp_path / "status.json").read_text())
    assert status["mode"] == "dynamic_pool"
    # 默认 start = end(2024-12-31) − 730d ≈ 2023-01-01 → 24 个月度重算
    assert status["pool"]["n_recomputes"] == 24
    assert status["pool"]["recompute_dates"][0] >= "2023-01-01"
    assert (tmp_path / "pool_timeline.csv").exists()
    head = (tmp_path / "per_stock.csv").read_text().splitlines()[0]
    for col in ("pool_months", "pool_first", "pool_last"):
        assert col in head
    rows = (tmp_path / "per_stock.csv").read_text().splitlines()
    r_a = next(r for r in rows if r.startswith("sh.600001"))
    months = r_a.split(",")[list(head.split(",")).index("pool_months")]
    assert int(months) >= 10   # A 2023 年在池（2024-07 起 SOE 翻转出池）


# ---------------------------------------------------------------------------
# 7) pool_schedule 子进程模式（内存归零架构的编排层：失败 fail-closed）
# ---------------------------------------------------------------------------
def test_pool_schedule_subprocess_mode(con, monkeypatch):
    """子进程模式：逐月走一次性子进程入口；进度按完成计数；子进程失败 → 抛出
    （fail-closed，不静默空池）。"""
    monkeypatch.setattr(dp, "ROLLING_DAYS", 250)
    up = UniverseParams(**UP)
    calls = []

    def fake(rd, cfg):
        calls.append(rd)
        return {"sh.600001"} if rd.startswith("2023") else set()

    monkeypatch.setattr(dp, "_subprocess_month_pool", fake)
    seen = []
    sched = dp.pool_schedule(con, SP, up, "2023-01-02", "2024-12-31",
                             progress=lambda s, d, t: seen.append(d),
                             use_subprocess=True, config_path="x.yaml")
    assert sched and calls                       # 逐月经子进程入口
    assert len(seen) == len(sched) and seen[-1] == len(sched)
    assert any("sh.600001" in s for s in sched.values())
    assert all(not s for d, s in sched.items() if d.startswith("2024"))

    def boom(rd, cfg):
        raise RuntimeError("dynpool-month 2023-01-02 失败 rc=1")

    monkeypatch.setattr(dp, "_subprocess_month_pool", boom)
    with pytest.raises(RuntimeError):
        dp.pool_schedule(con, SP, up, "2023-01-02", "2023-06-01",
                         use_subprocess=True)
