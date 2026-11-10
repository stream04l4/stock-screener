# -*- coding: utf-8 -*-
"""martingale.engine 状态机单测（纯数组合成数据，零网络零湖）。

覆盖调研 ⑥ 自测面：首买/分档加仓/跳空一日多档/bounce rearm（创新低当日不触发）/
newhigh rearm/手数 sizing（v7.0.28：第 k 档 = first_lots+k×add_lots_step 手）/
分红前一日收持仓/停牌顺延/除权推导 3 情形/再武装后 k 归零重新从 first_lots 手起。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from martingale.config import StrategyParams  # noqa: E402
from martingale.engine import run_stock  # noqa: E402


def _dates(*iso):
    return np.array([np.datetime64(d, "D").astype("datetime64[ns]") for d in iso])


def _params(**kw):
    base = dict(name="drawdown_martingale", lot_size=100, first_lots=1,
                add_lots_step=1, first_drawdown=0.15, add_step=0.05, rearm_mode="bounce",
                rearm_pct=0.15, accounting="cash")
    base.update(kw)
    return StrategyParams(**base)


def _run(p, sig, exec_c=None, exmap=None, dates=None):
    n = len(sig)
    d = dates or _dates(*[f"2020-01-{i+1:02d}" if i < 28 else f"2020-02-{i-27:02d}"
                          for i in range(n)])
    exec_c = np.asarray(exec_c, dtype=float) if exec_c is not None else np.asarray(sig, dtype=float)
    return run_stock("test.000001", d, np.asarray(sig, dtype=float), exec_c, exmap, p, verbose=False)


# ---------------------------------------------------------------------------
# 首买 + 分档加仓
# ---------------------------------------------------------------------------

def test_first_buy_and_lot_laddering():
    """H=100（bar0）。bar1 跌到 80：穿 k=0 档(85) 买 1 手，同 bar 再穿 k=1 档(80) 买 2 手。
    bar2 74 穿 k=2(75) 买 3 手，bar3 70 穿 k=3(70) 买 4 手（C 方案手数阶梯）。"""
    p = _params()
    stat, state, *_ , buys = _run(p, [100.0, 80.0, 74.0, 70.0])
    assert stat["n_buys"] == 4
    # v7.0.28 手数阶梯：1/2/3/4 手 × 100 股
    assert [b[5] for b in buys] == [1, 2, 3, 4]
    # 现金支出 = 股数 × 成交价 × (1+comm)
    comm = p.commission
    assert [b[2] for b in buys] == pytest.approx(
        [100.0 * 80.0 * (1 + comm), 200.0 * 80.0 * (1 + comm),
         300.0 * 74.0 * (1 + comm), 400.0 * 70.0 * (1 + comm)])
    assert stat["total_invested"] == pytest.approx(sum(b[2] for b in buys))
    assert state["shares"] == pytest.approx(100.0 * (1 + 2 + 3 + 4))
    assert stat["first_buy_date"].startswith("2020-01-02")
    assert stat["last_status"] == "open"


def test_no_buy_above_first_level():
    """跌 10%（未到 15% 首档）→ 不买。"""
    p = _params()
    stat, *_ = _run(p, [100.0, 90.0, 86.0])
    assert stat["n_buys"] == 0
    assert stat["total_invested"] == 0.0
    assert stat["last_status"] == "never"


def test_gap_day_multi_level():
    """跳空一日穿多档：100 → 70 单日穿 k=0..k=3 四档（75 线之上逐档买）。"""
    p = _params()
    stat, *_ , buys = _run(p, [100.0, 70.0])
    # k=0:85, k=1:80, k=2:75, k=3:70 全穿（70≤70）；k=4:65 不穿
    assert stat["n_buys"] == 4
    assert all(b[0] == buys[0][0] for b in buys)  # 全在同一天


# ---------------------------------------------------------------------------
# rearm 语义
# ---------------------------------------------------------------------------

def test_bounce_rearm_resets_k_to_first_lots():
    """rearm 后 k 归零 → 新轮从 first_lots(1) 手重新起算（v7.0.28 手数口径）。
    同时验证 rearm 后 k 从 0 重起（再跌到 新H×0.85 才买）。
    注意：不卖 → in_pos 恒真 → n_rounds=1（rearm 不开新轮次，只重置 k/H）。"""
    p = _params(add_step=0.5)
    # bar0 H=100；bar1 80 穿 k=0(85) 买 1 手（k=1 线=35 不穿）
    # bar2 50（创新低，low=50，50<57.5 不反弹）；bar3 60：60≥50×1.15=57.5 → rearm H=60 k=0
    # bar4 50：新 H 下 k=0 线=51 → 50≤51 → 新轮首档买 1 手（k 归零重新起算）
    stat, state, *_ , buys = _run(p, [100.0, 80.0, 50.0, 60.0, 50.0])
    assert stat["n_rearms"] == 1
    assert [b[5] for b in buys] == [1, 1]  # 两轮首档都是 first_lots=1 手
    assert [b[4] for b in buys] == [True, False]  # 首仓/加仓标注不随 rearm 重置
    assert stat["n_rounds"] == 1  # 不卖 → 单一持仓轮次贯穿全程
    # 再武装后 k 从 0 起：bar4 买的是新周期 k=0 档 → 买后 k_level=1
    assert state["k_level"] == 1
    assert state["entry_high"] == pytest.approx(60.0)


def test_bounce_rearm_not_on_new_low_day():
    """创新低当日不触发 rearm（先更新 low_since_high 再判反弹）：
    low=70 时价格 65（新低）→ 不 rearm；次根 75 ≥ 65×1.15=74.75 → 才 rearm。"""
    p = _params(add_step=0.5)
    # bar0 H=100；bar1 70（穿 85 买 initial）；bar2 65 新低（low=65；65<74.75 不 rearm）
    # bar3 75：75≥65×1.15=74.75 → rearm
    stat, state, *_ , buys = _run(p, [100.0, 70.0, 65.0, 75.0])
    assert stat["n_rearms"] == 1
    # rearm 发生在 bar3（75 那根）：entry_high 重置为 75
    assert state["entry_high"] == pytest.approx(75.0)


def test_newhigh_rearm_mode():
    """newhigh 模式：仅创新高刷新 H（k 重置），低点对反弹无感。"""
    p = _params(rearm_mode="newhigh")
    # bar0 H=100；bar1 80 买（initial）；bar2 90 不创新高（无 rearm）；bar3 110 新高 → rearm
    stat, state, *_ , buys = _run(p, [100.0, 80.0, 90.0, 110.0])
    assert stat["n_rearms"] == 1
    assert state["entry_high"] == pytest.approx(110.0)
    assert state["next_rearm_price"] is None  # newhigh 模式无 rearm 价位


# ---------------------------------------------------------------------------
# 分红（cash 口径）
# ---------------------------------------------------------------------------

def test_cash_dividend_collected_on_prev_close_holding():
    """除息日 bar2：前一日收持仓 shares × cash_dps 收现金（在当日买入前执行）。
    bar1 81 只穿 k=0(85) 一档（k=1 线=80，81>80 不穿）→ 单一持仓。"""
    p = _params()
    d = _dates("2020-01-01", "2020-01-02", "2020-01-03")
    exmap = {np.datetime64("2020-01-03", "D").astype("datetime64[ns]"): (1.0, 0.0)}
    stat, state, *_ = run_stock("t", d, np.array([100.0, 81.0, 82.0]),
                                np.array([100.0, 81.0, 82.0]), exmap, p, verbose=False)
    shares_after_buy = 100.0  # 首档 1 手 = 100 股（v7.0.28 手数 sizing）
    assert stat["n_buys"] == 1
    assert stat["dividends"] == pytest.approx(shares_after_buy * 1.0)
    assert state["cash_div"] == pytest.approx(shares_after_buy * 1.0)


def test_stk_div_adds_shares_proportionally():
    """送转：10 送 2 → shares ×1.2（与现金分红同日事件合并处理）。"""
    p = _params()
    d = _dates("2020-01-01", "2020-01-02", "2020-01-03")
    exmap = {np.datetime64("2020-01-03", "D").astype("datetime64[ns]"): (0.0, 2.0)}
    stat, state, *_ = run_stock("t", d, np.array([100.0, 81.0, 82.0]),
                                np.array([100.0, 81.0, 82.0]), exmap, p, verbose=False)
    s0 = 100.0  # 1 手 = 100 股
    assert state["shares"] == pytest.approx(s0 * 1.2)
    assert stat["dividends"] == 0.0


def test_suspended_exdate_rolls_to_resume_bar():
    """停牌期除权事件顺延：ex_date 不在 bar 序列 → 复牌首个 bar 收取。"""
    p = _params()
    d = _dates("2020-01-01", "2020-01-02", "2020-01-04")  # 01-03 停牌
    exmap = {np.datetime64("2020-01-03", "D").astype("datetime64[ns]"): (1.0, 0.0)}
    stat, state, *_ = run_stock("t", d, np.array([100.0, 81.0, 82.0]),
                                np.array([100.0, 81.0, 82.0]), exmap, p, verbose=False)
    s0 = 100.0  # 1 手 = 100 股
    assert stat["n_buys"] == 1
    assert stat["dividends"] == pytest.approx(s0 * 1.0)  # 复牌日 bar 收取


def test_exdate_after_data_end_ignored():
    """除权日晚于数据终点 → 不计。"""
    p = _params()
    d = _dates("2020-01-01", "2020-01-02", "2020-01-03")
    exmap = {np.datetime64("2020-06-01", "D").astype("datetime64[ns]"): (5.0, 0.0)}
    stat, *_ = run_stock("t", d, np.array([100.0, 81.0, 82.0]),
                         np.array([100.0, 81.0, 82.0]), exmap, p, verbose=False)
    assert stat["dividends"] == 0.0


# ---------------------------------------------------------------------------
# v7.0.35 年汇总（stat["annual"]：外部投入/分红(盈余)/盈亏；报告专用字段）
# ---------------------------------------------------------------------------

def test_annual_summary_two_years():
    """跨两年：2020 建仓（2 笔含费 24007.2）年末市值 25200；2021 分红 300 +
    市值 25200→27300。盈亏(Y)=年末资产−年初资产−当年投入；Σ=总盈亏。"""
    p = _params()
    d = _dates("2019-12-30", "2020-01-01", "2020-01-02", "2020-01-03",
               "2021-01-01", "2021-01-02", "2021-01-03")
    exmap = {np.datetime64("2021-01-02", "D").astype("datetime64[ns]"): (1.0, 0.0)}
    sig = np.array([100.0, 100.0, 80.0, 84.0, 82.0, 82.0, 90.0])
    stat, *_ = run_stock("t", d, sig, sig, exmap, p, verbose=False)
    ann = stat["annual"]
    assert [a["year"] for a in ann] == [2020, 2021]  # 2019 建仓前全零 → 跳过
    a20 = ann[0]
    assert a20["invested_ext"] == pytest.approx(100 * 80 * 1.0003 + 200 * 80 * 1.0003)
    assert a20["invested_div"] == 0.0
    assert a20["div_surplus"] == 0.0
    assert a20["price_pnl"] == pytest.approx(300 * 84.0 - 0.0)
    assert a20["end_assets"] == pytest.approx(300 * 84.0)
    a21 = ann[1]
    assert a21["invested_ext"] == 0.0
    assert a21["invested_div"] == 0.0
    assert a21["div_surplus"] == pytest.approx(300 * 1.0)
    # 股价盈亏 = 市值变化 = (27300-300) - 25200 = 1800
    assert a21["price_pnl"] == pytest.approx((300 * 90.0 + 300.0 - 300.0) - 300 * 84.0)
    assert a21["end_assets"] == pytest.approx(300 * 90.0 + 300.0)
    # 恒等式：年底总资产 = 年初总资产 + 股价盈亏 + 分红盈余（投入不另加）
    assert a20["end_assets"] == pytest.approx(0.0 + a20["price_pnl"] + a20["div_surplus"])
    assert a21["end_assets"] == pytest.approx(a20["end_assets"] + a21["price_pnl"] + a21["div_surplus"])
    # telescoping：Σ(股价盈亏+分红盈余) = 期末总资产 = final_assets
    assert sum(a["price_pnl"] + a["div_surplus"] for a in ann) == pytest.approx(stat["final_assets"])
    assert sum(a["div_surplus"] for a in ann) == pytest.approx(stat["dividends"])


def test_annual_summary_never_triggered_empty():
    """从未建仓（无投入/无持仓/无分红）→ annual 空（不产全零行）。"""
    p = _params()
    d = _dates("2020-01-01", "2020-01-02", "2020-01-03")
    sig = np.array([100.0, 101.0, 102.0])
    stat, *_ = run_stock("t", d, sig, sig, {}, p, verbose=False)
    assert stat["annual"] == []


# ---------------------------------------------------------------------------
# 隐含除权推导（af_ratio 算法）
# ---------------------------------------------------------------------------

def _af_run(exec_c, af, **kw):
    """exmap={} 触发推导块（None 会整体跳过——与 CLI 路径一致：load() 总返回 dict）。"""
    p = _params(**kw)
    n = len(exec_c)
    d = _dates(*[f"2020-01-{i+1:02d}" for i in range(n)])
    sig = np.array(exec_c, dtype=float) * np.array(af, dtype=float)
    return run_stock("t", d, sig, np.array(exec_c, dtype=float), {}, p, verbose=False)


def test_implied_exdividend_stk_derived():
    """af 跳变 1.25、原始价按送转比例缺口（8.5→6.8）、无当日事件且 x10=2.5≥1
    → 推导 10 送 2.5（shares ×1.25）。"""
    p = _params(add_step=0.5)
    exec_c = [10.0, 10.0, 8.5, 8.5, 6.8, 6.8]
    af = [1.0, 1.0, 1.0, 1.0, 1.25, 1.25]
    stat, state, *_ = _af_run(exec_c, af)
    # bar2（sig 8.5）首买 initial 1000 @8.5
    s0 = 100.0  # 1 手 = 100 股
    # bar4 推导 10 送 2.5 → shares ×1.25
    assert state["shares"] == pytest.approx(s0 * 1.25)


def test_implied_exdividend_cash_backfill():
    """af 跳变 1.02、x10=0.2<1 且非整数组、无当日事件 → 推导为漏记现金分红
    （不送转、不加股；补记 pc×(ratio-1)/股）。"""
    p = _params(add_step=0.5)
    exec_c = [10.0, 10.0, 8.5, 8.5, 8.3333333333, 8.3333333333]
    af = [1.0, 1.0, 1.0, 1.0, 1.02, 1.02]
    stat, state, *_ = _af_run(exec_c, af)
    s0 = 100.0  # 1 手 = 100 股
    # 推导现金 ≈ (af_ratio-1)×前收×shares：shares 不变、cash_div > 0
    assert state["shares"] == pytest.approx(s0)
    assert stat["dividends"] > 0


def test_factor_glitch_rejected():
    """af 跳变与原始价缺口不匹配（容差 5pp+30%）→ 因子 glitch，忽略（不加股不补现金）。"""
    p = _params(add_step=0.5)
    exec_c = [10.0, 8.4, 8.4, 8.4]
    af = [1.0, 1.0, 1.3, 1.3]
    stat, state, *_ = _af_run(exec_c, af)
    s0 = 100.0  # 1 手 = 100 股
    assert state["shares"] == pytest.approx(s0)
    assert stat["dividends"] == 0.0


# ---------------------------------------------------------------------------
# 聚合 + 状态投影
# ---------------------------------------------------------------------------

def test_state_projection_fields():
    p = _params()
    # [100, 80, 74]：bar1 穿 k=0(85)+k=1(80)，bar2 穿 k=2(75) → 买 3 档，k_level=3
    stat, state, v_arr, c_arr, flag, buys = _run(p, [100.0, 80.0, 74.0])
    assert state["in_pos"] is True
    assert state["shares"] > 0
    assert state["avg_cost"] == pytest.approx(state["open_cost"] / state["shares"])
    # 下一档 = k_level(3) 对应线 = 100×(1−0.15−3×0.05) = 70
    assert state["next_add_price"] == pytest.approx(100.0 * (1 - 0.15 - 3 * 0.05))
    assert state["next_rearm_price"] == pytest.approx(state["low_since_high"] * 1.15)
    # v_arr/c_arr 末值 = 持仓市值/成本
    assert v_arr[-1] == pytest.approx(state["market_value"] + state["cash_div"])
    assert c_arr[-1] == pytest.approx(state["open_cost"])
    assert flag[-1] == 1


def test_aggregate_into_fill_last_value():
    """组合聚合末值结转：停牌后持仓不消失（g_value 末值向前结转）。"""
    from martingale.engine import aggregate_into
    gdates = np.array([np.datetime64(f"2020-01-{d:02d}", "D").astype("datetime64[ns]")
                       for d in (1, 2, 3, 4, 5)])
    g_value = np.zeros(5)
    g_cost = np.zeros(5)
    g_open = np.zeros(5, dtype=np.int64)
    dates = np.array([np.datetime64(f"2020-01-{d:02d}", "D").astype("datetime64[ns]")
                      for d in (1, 2)])  # 该股只有 2 根 bar
    v_arr = np.array([100.0, 150.0])
    c_arr = np.array([50.0, 50.0])
    flag = np.array([1, 1], dtype=np.int8)
    aggregate_into(gdates, g_value, g_cost, g_open, dates, v_arr, c_arr, flag)
    # 末值结转从该股末 bar 当日开始（含当日，参考脚本 L409-412 逐行语义：
    # scatter 与结转在末 bar 当日重叠 —— f6 七股数据都到窗口末、不触发此分支，
    # 仅在数据早于全局窗口末端时可见）
    assert list(g_value) == [100.0, 300.0, 150.0, 150.0, 150.0]
    assert list(g_open) == [1, 2, 1, 1, 1]  # 末 bar 当日 flag 同样重叠 +1
