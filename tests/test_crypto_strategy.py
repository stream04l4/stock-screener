# -*- coding: utf-8 -*-
"""crypto 马丁格尔策略单测（v7.0.47）。

零网络零湖：全部用合成 1h 收盘价序列直接驱动 crypto.strategy.engine。
覆盖：一年高点滚动窗口（含部分窗口/排除过期高点）、30% 建仓触发边界、
固定网格加仓（逐级 + 跳空穿多级）、上涨不加仓/不刷新 entry、
10x 止盈了结 + 下一轮重新等规则 1、手续费口径。
"""
import numpy as np
import pytest

from crypto.strategy.engine import StrategyParams, rolling_high_1y, run_coin

P = StrategyParams()  # 默认 = 拍板规格：跌70%建仓/1%网格/10x止盈/10bps
H = 8760             # 一年窗口


def _dates(n: int) -> np.ndarray:
    return np.arange(n, dtype="int64").astype("datetime64[D]")


def _run(closes: list, p: StrategyParams = P) -> dict:
    return run_coin("TEST", _dates(len(closes)), np.array(closes, dtype=float), p)


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------
def test_params_from_cfg():
    p = StrategyParams.from_cfg({
        "strategy": {"first_drop_pct": 70, "add_grid_pct": 1,
                     "base_amount_usdt": 10, "growth": 1.05,
                     "take_profit_mult": 10, "fee_bps": 10, "lookback_h": 8760}})
    assert p.trigger_frac() == pytest.approx(0.30)
    # 缺省段 → 全默认值
    d = StrategyParams.from_cfg({})
    assert d == StrategyParams()


# ---------------------------------------------------------------------------
# 一年高点（滚动窗口）
# ---------------------------------------------------------------------------
def test_rolling_high_partial_warmup():
    c = np.array([10.0, 12.0, 9.0, 30.0, 11.0])
    h = rolling_high_1y(c, 3)
    assert list(h[:2]) == [10.0, 12.0]          # 部分窗口 = 已有历史最大
    assert list(h[2:]) == [12.0, 30.0, 30.0]     # 窗口 3 根
    assert list(rolling_high_1y(np.array([]), 3)) == []


def test_rolling_high_excludes_stale_high():
    n_win = 4
    c = np.array([100.0, 10.0, 10.0, 10.0, 10.0, 20.0])
    h = rolling_high_1y(c, n_win)
    # i=4 窗口 = 下标 1..4（全 10，含当日）→ 100（下标 0）已滚出窗口
    assert h[4] == 10.0
    assert h[3] == 100.0  # i=3 窗口含下标 0 → 旧高点仍在


# ---------------------------------------------------------------------------
# 建仓触发（价 ≤ 一年高点 × 30%）
# ---------------------------------------------------------------------------
def test_first_buy_boundary():
    closes = [100.0] * H + [30.01, 30.0]
    r = _run(closes)
    o = r["open_round"]
    assert o is not None
    # 30.01 不触发（>30.0），30.0 触发；entry = 触发 bar 收盘价
    assert o["entry"] == 30.0
    assert o["entry_date"] == str(_dates(len(closes))[-1])[:10]
    assert o["k_level"] == 0 and o["spent_usdt"] == 10.0


def test_no_buy_above_trigger():
    closes = [100.0] * H + [31.0, 35.0, 40.0]
    r = _run(closes)
    assert r["open_round"] is None and r["rounds"] == []


def test_warmup_partial_window_allows_early_buy():
    # 数据不足一年：部分窗口（已有历史最大）也允许触发
    closes = [100.0, 90.0, 29.0]
    r = _run(closes)
    assert r["open_round"] is not None
    assert r["open_round"]["entry"] == 29.0


# ---------------------------------------------------------------------------
# 加仓（固定网格 entry×(1−1%×k)，金额 10×1.05^k）
# ---------------------------------------------------------------------------
def test_grid_adds_step_by_step():
    closes = [100.0] * H + [30.0, 29.7, 29.4, 29.1]
    r = _run(closes)
    o = r["open_round"]
    assert o["k_level"] == 3
    # 10 + 10×1.05 + 10×1.05² + 10×1.05³
    want = 10.0 * sum(1.05 ** k for k in range(4))
    assert o["spent_usdt"] == pytest.approx(round(want, 2))
    # 事件序列：建仓 1 + 逐级加仓 3（级数 1/2/3）
    ev = r["events"]
    assert [e["type"] for e in ev] == ["entry", "add", "add", "add"]
    assert [e["level"] for e in ev[1:]] == [1, 2, 3]


def test_gap_crosses_multiple_levels_in_one_bar():
    closes = [100.0] * H + [30.0, 28.5]   # 一下穿 29.7/29.4/29.1/28.8/28.5 五级
    r = _run(closes)
    assert r["open_round"]["k_level"] == 5
    want = 10.0 * sum(1.05 ** k for k in range(6))
    assert r["open_round"]["spent_usdt"] == pytest.approx(round(want, 2))
    # 跳空同 bar 多级：5 个 add 事件同日期
    adds = [e for e in r["events"] if e["type"] == "add"]
    assert len(adds) == 5
    assert len({e["date"] for e in adds}) == 1


def test_no_add_on_rise_and_entry_not_refreshed():
    closes = [100.0] * H + [30.0, 60.0, 80.0, 29.7]
    r = _run(closes)
    o = r["open_round"]
    # 新高不刷新 entry：若刷新到 80，29.7 远低于 80×0.99=79.2 不会加仓；
    # 实际锚定 30 → 29.7 = 30×0.99 触发 1 级加仓
    assert o["entry"] == 30.0
    assert o["k_level"] == 1


# ---------------------------------------------------------------------------
# 止盈（价 ≥ 建仓价 × 10 → 全卖）+ 下一轮
# ---------------------------------------------------------------------------
def test_exit_10x_closes_round_and_reenters():
    closes = [100.0] * H + [30.0, 300.0, 100.0, 89.0]
    r = _run(closes)
    assert len(r["rounds"]) == 1
    rd = r["rounds"][0]
    assert rd["entry"] == 30.0 and rd["exit_price"] == 300.0
    assert rd["pnl_usdt"] > 0  # 10x 了结必盈（首仓 10 USDT → ~100 USDT）
    # 了结后空仓：100 > 一年高点(300)×30% = 90 → 不建仓
    # 89 ≤ 90 → 新轮建仓，entry=89
    o = r["open_round"]
    assert o is not None and o["entry"] == 89.0
    assert o["k_level"] == 0
    # 事件序列：建仓(30) → 平仓(300) → 再建仓(89)
    ev = r["events"]
    assert [e["type"] for e in ev] == ["entry", "exit", "entry"]
    assert [e["price"] for e in ev] == [30.0, 300.0, 89.0]


def test_no_exit_below_10x():
    closes = [100.0] * H + [30.0, 90.0, 120.0, 50.0]   # 最高 120 < 300
    r = _run(closes)
    assert r["rounds"] == [] and r["open_round"] is not None
    assert r["open_round"]["entry"] == 30.0  # entry 未被 120 刷新


# ---------------------------------------------------------------------------
# 手续费
# ---------------------------------------------------------------------------
def test_fees_reduce_shares_and_pnl():
    closes = [100.0] * H + [30.0, 300.0]
    r_fee = _run(closes, StrategyParams(fee_bps=10))
    r_no = _run(closes, StrategyParams(fee_bps=0))
    # 有费：首仓 10×0.999/30 币 < 无费 10/30；了结所得同样折费
    assert r_fee["rounds"][0]["proceeds_usdt"] < r_no["rounds"][0]["proceeds_usdt"]
    assert r_fee["rounds"][0]["pnl_usdt"] < r_no["rounds"][0]["pnl_usdt"]
    # 无费时首仓 10 USDT @30 全转币，300 卖出 ≈ 100
    assert r_no["rounds"][0]["proceeds_usdt"] == pytest.approx(100.0, abs=0.01)


def test_net_equity_and_mdd_shape():
    closes = [100.0] * H + [30.0, 29.7, 300.0]
    r = _run(closes)
    eq = r["equity"]
    # 建仓前净值 0；了结后 = 净盈亏（恒定）——结果字段 round(2)，用 abs 容差
    assert eq[0] == 0.0
    assert eq[-1] == pytest.approx(r["final_equity"], abs=0.01)
    assert eq[-1] == pytest.approx(r["realized_cash"], abs=0.01)
    assert r["mdd_pct"] >= 0.0
