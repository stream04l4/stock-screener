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
    # 建仓前净值 0；了结后空仓 = 余额（所得全额 = 净盈亏 + 外部投入，
    # 本金已回笼在账户里）——结果字段 round(2)，用 abs 容差
    assert eq[0] == 0.0
    assert eq[-1] == pytest.approx(r["final_equity"], abs=0.01)
    assert eq[-1] == pytest.approx(
        r["realized_cash"] + r["external_invested"], abs=0.01)
    assert r["mdd_pct"] >= 0.0


# ---------------------------------------------------------------------------
# 账户模型（v7.0.53）：余额滚存下一轮，余额耗尽才追加外部投入
# ---------------------------------------------------------------------------
def test_wallet_rolls_over_next_round():
    # 轮 1：30 建仓 → 300 了结（fee=10bps：proceeds ≈ 99.9）
    # 轮 2：89 再建仓（10 U）→ 全部从余额（89.9）扣，不再计外部
    closes = [100.0] * H + [30.0, 300.0, 100.0, 89.0]
    r = _run(closes)
    assert len(r["rounds"]) == 1
    assert r["external_invested"] == pytest.approx(10.0, abs=0.01)  # 仅首轮
    proceeds = r["rounds"][0]["proceeds_usdt"]
    assert r["wallet"] == pytest.approx(proceeds - 10.0, abs=0.01)
    o = r["open_round"]
    # 净值 = 持仓市值 + 余额（恒等式）
    assert r["final_equity"] == pytest.approx(
        o["value_usdt"] + r["wallet"], abs=0.02)


def test_external_topup_when_balance_exhausted():
    # fee=0 便于精确对账：轮 1 了结所得恰 100（10/30×300），余额 = 90
    p0 = StrategyParams(fee_bps=0)
    # 轮 2：89 建仓后逐根贴网格下跌 34 级（每根触发恰好 1 级加仓）
    drops = [89.0 * (1.0 - 0.01 * j) - 0.001 for j in range(1, 35)]
    closes = [100.0] * H + [30.0, 300.0, 100.0, 89.0] + drops
    r = _run(closes, p0)
    o = r["open_round"]
    assert o["k_level"] == 34
    spent2 = o["spent_usdt"]                      # 轮 2 累计支出（fee=0 精确）
    spent_total = r["rounds"][0]["spent_usdt"] + spent2
    proceeds1 = r["rounds"][0]["proceeds_usdt"]   # = 100.0（fee=0）
    # 余额 90 在 ~第 34 级耗尽 → 期末余额 ≈ 0，外部 = 总投入 − 了结所得
    assert r["wallet"] == pytest.approx(0.0, abs=0.02)
    assert r["external_invested"] == pytest.approx(
        spent_total - proceeds1, abs=0.02)
    assert r["external_invested"] > 10.0          # 确实追加过外部
    # 恒等式：外部 + 余额滚存 = 总投入
    assert r["external_invested"] + (proceeds1 - r["wallet"]) == pytest.approx(
        spent_total, abs=0.02)


# ---------------------------------------------------------------------------
# 时间范围（v7.0.54）：外部 h1y 覆盖（窗口前历史做一年高点基线）
# ---------------------------------------------------------------------------
def test_h1y_override_used_for_trigger():
    # 5 根前置高位 bar（100）+ 窗口 [30, 300]：用含前置历史的 h1y，
    # 首根窗口 bar 即 30 ≤ 100×30% → 立即建仓了结；
    # 只用窗口序列 [30, 300] 自算 h1y 则不会触发（部分窗口起点 = 自身价）
    closes = np.array([100.0] * 5 + [30.0, 300.0])
    h1y = rolling_high_1y(closes, 8760)
    r = run_coin("T", _dates(7), closes, P, h1y=h1y)
    assert len(r["rounds"]) == 1
    assert r["rounds"][0]["entry"] == 30.0
    r2 = run_coin("T", _dates(2), np.array([30.0, 300.0]), P)
    assert r2["rounds"] == [] and r2["open_round"] is None


# ---------------------------------------------------------------------------
# 资金预算（v7.0.56）：external_cap 硬预算，买不起整档放弃、不做部分买入
# ---------------------------------------------------------------------------
def test_external_cap_truncates_deep_levels():
    # fee=0 便于精确对账：30 建仓后逐根贴网格下跌 3 级（29.7/29.4/29.1），
    # 累计支出 = 10 + 10.5 + 11.025 + 11.57625 = 43.10125
    p0 = StrategyParams(fee_bps=0)
    closes = [100.0] * H + [30.0, 29.7, 29.4, 29.1, 100.0]
    # 无限预算：3 级全买（返回口径 round(…, 2)）
    full = run_coin("T", _dates(len(closes)), np.array(closes), p0)
    assert full["open_round"]["k_level"] == 3
    assert full["external_invested"] == pytest.approx(43.10125, abs=0.01)
    # 预算 35：L0+L1+L2 = 31.525 ≤ 35，L3（11.576）买不起 → 截断
    capped = run_coin("T", _dates(len(closes)), np.array(closes), p0,
                      external_cap=35.0)
    o = capped["open_round"]
    assert o["k_level"] == 2
    assert o["capped"] is True
    assert o["spent_usdt"] == pytest.approx(31.525, abs=0.01)
    assert capped["external_invested"] == pytest.approx(31.525, abs=0.01)
    assert capped["external_invested"] <= 35.0
    # 截断后份额/净值 = 前 3 档（L0+L1+L2）
    assert o["shares"] == pytest.approx(
        10.0 / 30.0 + 10.5 / 29.7 + 11.025 / 29.4, rel=1e-6)


def test_external_cap_too_small_skips_entry():
    # 首仓 10 > 预算 5 → 买不起 → 不建仓（每 bar 重试，恒不入场）
    p0 = StrategyParams(fee_bps=0)
    closes = [100.0] * H + [30.0, 29.0, 300.0]
    r = run_coin("T", _dates(len(closes)), np.array(closes), p0,
                 external_cap=5.0)
    assert r["rounds"] == [] and r["open_round"] is None
    assert r["external_invested"] == 0.0
    assert r["final_equity"] == 0.0


def test_external_cap_none_matches_legacy():
    # 缺省（无限预算）与显式 None 完全一致
    closes = [100.0] * H + [30.0, 29.7, 29.4, 29.1, 100.0]
    a = run_coin("T", _dates(len(closes)), np.array(closes), P)
    b = run_coin("T", _dates(len(closes)), np.array(closes), P,
                 external_cap=None)
    assert a["external_invested"] == b["external_invested"]
    assert a["open_round"]["k_level"] == b["open_round"]["k_level"]
    assert a["final_equity"] == b["final_equity"]


# ---------------------------------------------------------------------------
# 追踪止损（v8.0.5）：价 ≥ 建仓价×act 激活，自激活后最高收盘回撤 dd% 平仓
# ---------------------------------------------------------------------------
def test_params_from_cfg_trail_and_null_tp():
    p = StrategyParams.from_cfg({"strategy": {
        "take_profit_mult": None,
        "trail_activation_mult": 1.5, "trail_drawdown_pct": 0.2}})
    assert p.take_profit_mult is None
    assert p.trail_activation_mult == 1.5
    assert p.trail_drawdown_pct == 0.2


def test_trail_stop_exits_on_20pct_pullback():
    # fee=0：30 建仓 → 45（=1.5×30 激活，初峰 45）→ 48（峰 48）
    # → 38.4（=48×0.8 → 追踪止损平仓）
    p = StrategyParams(fee_bps=0, take_profit_mult=None,
                       trail_activation_mult=1.5, trail_drawdown_pct=0.2)
    closes = [100.0] * H + [30.0, 45.0, 48.0, 38.4, 100.0]
    r = run_coin("T", _dates(len(closes)), np.array(closes), p)
    assert len(r["rounds"]) == 1
    rd = r["rounds"][0]
    assert rd["reason"] == "trail"
    assert rd["entry"] == 30.0
    assert rd["exit_price"] == 38.4
    assert rd["peak_price"] == 48.0
    assert rd["proceeds_usdt"] == pytest.approx(10.0 / 30.0 * 38.4, abs=0.01)
    assert rd["pnl_usdt"] == pytest.approx(10.0 / 30.0 * 38.4 - 10.0,
                                           abs=0.01)


def test_trail_ignores_pullback_before_activation():
    # 30 建仓 → 42（=1.4×，未激活）→ 30.5（距峰 42 回撤 17.5% 但未激活
    # → 不平仓）；未激活轮次 tp_price=None（无固定止盈）
    p = StrategyParams(fee_bps=0, take_profit_mult=None,
                       trail_activation_mult=1.5, trail_drawdown_pct=0.2)
    closes = [100.0] * H + [30.0, 42.0, 30.5]
    r = run_coin("T", _dates(len(closes)), np.array(closes), p)
    assert r["rounds"] == []
    o = r["open_round"]
    assert o is not None and o["trail_on"] is False
    assert o["tp_price"] is None


def test_trail_rides_monotonic_rise():
    # 激活后单调上涨（无 20% 回撤）一路持有到收盘，平仓线跟随峰值
    p = StrategyParams(fee_bps=0, take_profit_mult=None,
                       trail_activation_mult=1.5, trail_drawdown_pct=0.2)
    closes = [100.0] * H + [30.0, 45.0, 60.0, 90.0, 120.0]
    r = run_coin("T", _dates(len(closes)), np.array(closes), p)
    assert r["rounds"] == []
    o = r["open_round"]
    assert o["trail_on"] is True
    assert o["trail_peak"] == 120.0
    assert o["trail_stop"] == pytest.approx(96.0)


def test_trail_survives_shallow_pullback():
    # 45 激活 → 48 峰 → 40（回撤 16.7% < 20% 不触发）→ 50 新峰 → 仍持仓
    p = StrategyParams(fee_bps=0, take_profit_mult=None,
                       trail_activation_mult=1.5, trail_drawdown_pct=0.2)
    closes = [100.0] * H + [30.0, 45.0, 48.0, 40.0, 50.0]
    r = run_coin("T", _dates(len(closes)), np.array(closes), p)
    assert r["rounds"] == []
    o = r["open_round"]
    assert o["trail_peak"] == 50.0
    assert o["trail_stop"] == pytest.approx(40.0)


def test_fixed_tp_wins_over_trail():
    # 固定止盈 1.4× 低于激活线 1.5× → 止盈先了结，追踪从未激活
    p = StrategyParams(fee_bps=0, take_profit_mult=1.4,
                       trail_activation_mult=1.5, trail_drawdown_pct=0.2)
    closes = [100.0] * H + [30.0, 42.0, 100.0]
    r = run_coin("T", _dates(len(closes)), np.array(closes), p)
    assert len(r["rounds"]) == 1
    assert r["rounds"][0]["reason"] == "tp"
    assert r["rounds"][0]["exit_price"] == 42.0
