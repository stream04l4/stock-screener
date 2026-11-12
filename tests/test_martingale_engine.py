# -*- coding: utf-8 -*-
"""martingale.engine 状态机单测（纯数组合成数据，零网络零湖）。

覆盖调研 ⑥ 自测面：首买/分档加仓/跳空一日多档/bounce rearm（创新低当日不触发）/
newhigh rearm/手数 sizing（v7.0.28：第 k 档 = first_lots+k×add_lots_step 手）/
分红前一日收持仓/停牌顺延/除权推导 3 情形/再武装后 k 归零重新从 first_lots 手起。

v8.1.0 新增（Joel 2026-11-12 定案）：
- 首仓（任何一轮 k=0）= 双条件：① 回撤线 + ② N 年收盘分位（first_ok）；
- 加仓锚点 = 首仓价 P0（round_anchor），非轮高；rearm 重置 P0；
- first_ok_series / extend_start 纯函数单测。
（first_ok=None 仅关闭分位门用于旧语义对照；锚点变更无条件生效。）
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


def _run(p, sig, exec_c=None, exmap=None, dates=None, first_ok=None):
    n = len(sig)
    d = dates or _dates(*[f"2020-01-{i+1:02d}" if i < 28 else f"2020-02-{i-27:02d}"
                          for i in range(n)])
    exec_c = np.asarray(exec_c, dtype=float) if exec_c is not None else np.asarray(sig, dtype=float)
    fo = np.asarray(first_ok, dtype=bool) if first_ok is not None else None
    return run_stock("test.000001", d, np.asarray(sig, dtype=float), exec_c, exmap, p,
                     verbose=False, first_ok=fo)


# ---------------------------------------------------------------------------
# 首买 + 分档加仓
# ---------------------------------------------------------------------------

def test_first_buy_and_lot_laddering():
    """v8.1.0 P0 锚点：H=100（bar0）。bar1 80 破首仓线(85) 买 1 手，P0=80；
    bar2 74 ≤ P0×0.95=76 → 2 手；bar3 70 ≤ P0×0.90=72 → 3 手（C 方案手数阶梯；
    加仓锚点 = 首仓价而非轮高——首仓当根不再同日连穿多档）。"""
    p = _params()
    stat, state, *_ , buys = _run(p, [100.0, 80.0, 74.0, 70.0])
    assert stat["n_buys"] == 3
    # v7.0.28 手数阶梯：1/2/3 手 × 100 股
    assert [b[5] for b in buys] == [1, 2, 3]
    # 现金支出 = 股数 × 成交价 × (1+comm)
    comm = p.commission
    assert [b[2] for b in buys] == pytest.approx(
        [100.0 * 80.0 * (1 + comm), 200.0 * 74.0 * (1 + comm),
         300.0 * 70.0 * (1 + comm)])
    assert stat["total_invested"] == pytest.approx(sum(b[2] for b in buys))
    assert state["shares"] == pytest.approx(100.0 * (1 + 2 + 3))
    assert stat["first_buy_date"].startswith("2020-01-02")
    assert stat["last_status"] == "open"
    # v8.1.0：加仓投影锚点 = P0（首仓信号价 80）；k_level=3（首仓 1 + 加仓 2 笔）
    assert state["round_anchor"] == pytest.approx(80.0)
    assert state["k_level"] == 3
    assert state["next_add_price"] == pytest.approx(80.0 * (1 - 3 * 0.05))


def test_no_buy_above_first_level():
    """跌 10%（未到 15% 首档）→ 不买。"""
    p = _params()
    stat, *_ = _run(p, [100.0, 90.0, 86.0])
    assert stat["n_buys"] == 0
    assert stat["total_invested"] == 0.0
    assert stat["last_status"] == "never"


def test_gap_day_multi_level():
    """v8.1.0 P0 锚点下跳空一日穿多档（加仓日）：bar1 70 首仓 1 手（P0=70，
    首仓当根不再连穿——档线全在 P0 下方）；bar2 60 跳空：60 ≤ P0×0.95=66.5 穿
    k=1 买 2 手、60 ≤ P0×0.90=63 穿 k=2 买 3 手（同日两笔）；k=3 线 59.5 不穿。"""
    p = _params()
    stat, *_ , buys = _run(p, [100.0, 70.0, 60.0])
    assert stat["n_buys"] == 3
    assert [b[5] for b in buys] == [1, 2, 3]
    assert buys[1][0] == buys[2][0]  # 两笔加仓同一天
    assert buys[0][0] != buys[1][0]  # 首仓在前一天


# ---------------------------------------------------------------------------
# rearm 语义
# ---------------------------------------------------------------------------

def test_bounce_rearm_resets_k_to_first_lots():
    """v8.1.0：rearm 后 k 归零 + P0 清零 → 第二轮首仓重新从 first_lots(1) 手起算，
    且首仓标注 first_of_round 每轮都打（第二轮首仓也是新一轮的 k=0 档）。
    v8.1.0 起 n_rounds = 首仓买入次数（rearm 重置 P0 后重新计轮）：
    bar0 H=100；bar1 80 首仓 1 手（P0=80，k=1 线=40 不穿）；
    bar2 50（创新低，low=50，50<57.5 不反弹）；bar3 60：60≥50×1.15=57.5 → rearm
    H=60 k=0 P0=0；bar4 50：50≤60×0.85=51 → 第二轮首仓 1 手。"""
    p = _params(add_step=0.5)
    stat, state, *_ , buys = _run(p, [100.0, 80.0, 50.0, 60.0, 50.0])
    assert stat["n_rearms"] == 1
    assert [b[5] for b in buys] == [1, 1]  # 两轮首档都是 first_lots=1 手
    assert [b[4] for b in buys] == [True, True]  # v8.1.0：每轮首仓都标注
    assert stat["n_rounds"] == 2  # v8.1.0：轮次 = 首仓次数（不再单一持仓贯穿）
    # 再武装后 k 从 0 起：bar4 买的是新周期 k=0 档 → 买后 k_level=1、P0=50
    assert state["k_level"] == 1
    assert state["entry_high"] == pytest.approx(60.0)
    assert state["round_anchor"] == pytest.approx(50.0)


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
    """跨两年（v8.1.0 P0 锚点）：2020 首仓 1 手@80（P0=80，k=1 线 76 未穿，
    2021-01-02 70 之前无加仓——本例 2021 段价格 82/82/90 始终 >76）含费 8002.4，
    年末市值 8400；2021-01-02 分红 100（100 股×1.0，现金入账未再投入）。
    盈亏(Y)=年末资产−年初资产−当年投入；Σ=总盈亏。"""
    p = _params()
    d = _dates("2019-12-30", "2020-01-01", "2020-01-02", "2020-01-03",
               "2021-01-01", "2021-01-02", "2021-01-03")
    exmap = {np.datetime64("2021-01-02", "D").astype("datetime64[ns]"): (1.0, 0.0)}
    sig = np.array([100.0, 100.0, 80.0, 84.0, 82.0, 82.0, 90.0])
    stat, *_ = run_stock("t", d, sig, sig, exmap, p, verbose=False)
    ann = stat["annual"]
    assert [a["year"] for a in ann] == [2020, 2021]  # 2019 建仓前全零 → 跳过
    a20 = ann[0]
    assert a20["invested_ext"] == pytest.approx(100 * 80 * 1.0003)
    assert a20["invested_div"] == 0.0
    assert a20["div_surplus"] == 0.0
    assert a20["price_pnl"] == pytest.approx(100 * 84.0 - 0.0)
    assert a20["end_assets"] == pytest.approx(100 * 84.0)
    a21 = ann[1]
    assert a21["invested_ext"] == 0.0
    assert a21["invested_div"] == 0.0
    assert a21["div_surplus"] == pytest.approx(100 * 1.0)
    # 股价盈亏 = 持仓市值变化 = 100×90 − 100×84（分红现金走 div_surplus）
    assert a21["price_pnl"] == pytest.approx(100 * 90.0 - 100 * 84.0)
    assert a21["end_assets"] == pytest.approx(100 * 90.0 + 100.0)
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


def test_dividend_priority_funding():
    """v7.0.36：分红余额优先出资，缺口外部（Joel 2026-11-10 定案；
    v8.1.0 P0 锚点下重算）：
    2020-01-02 首仓 1 手@80（P0=80，含费 8002.4）；
    2021-01-01 分红 1.0/股 × 100 股 → 余额 100；
    2021-01-02 70：70 ≤ P0×0.95=76 买 2 手（14004.2，首笔 100 由余额出资，
    余额清零）+ 70 ≤ P0×0.90=72 买 3 手（21006.3）。
    买入决策不看出资 → 时间表/笔数与无出资规则时一致。"""
    p = _params()
    d = _dates("2020-01-01", "2020-01-02", "2020-01-03", "2021-01-01", "2021-01-02")
    exmap = {np.datetime64("2021-01-01", "D").astype("datetime64[ns]"): (1.0, 0.0)}
    sig = np.array([100.0, 80.0, 84.0, 82.0, 70.0])
    stat, state, v_arr, c_arr, flag, buys = run_stock(
        "t", d, sig, sig, exmap, p, verbose=False)
    # 1) 买入时间表与出资规则无关（3 笔：2020 首仓 1 手；2021 跳空穿两档 2+3 手；
    # 金额含 3bps）
    assert stat["n_buys"] == 3
    assert [b[5] for b in buys] == [1, 2, 3]
    assert [b[2] for b in buys] == pytest.approx(
        [100 * 80 * 1.0003, 200 * 70 * 1.0003, 300 * 70 * 1.0003])
    # 2) 出资归属：外部净投入 = Σcost − 分红出资(100)
    c_total = 100 * 80 * 1.0003 + 200 * 70 * 1.0003 + 300 * 70 * 1.0003
    assert stat["total_invested"] == pytest.approx(c_total - 100.0)
    assert stat["dividends"] == pytest.approx(100.0)      # 累计收到（口径不变）
    assert stat["div_pool"] == pytest.approx(0.0)          # 余额全部出资
    # 3) 总盈亏绝对额与旧口径恒等（M + Σd − C），余额在分子分母同时消去
    m_end = 600 * 70.0
    assert stat["final_assets"] == pytest.approx(m_end)
    assert stat["total_pnl"] == pytest.approx(m_end + 100.0 - c_total)
    assert stat["pnl_pct"] == pytest.approx((m_end + 100.0 - c_total) / (c_total - 100.0))
    # 4) 年度汇总：2021 分红投入=100；盈余=余额净变动=收到100−投出100=0
    a21 = stat["annual"][1]
    assert a21["invested_div"] == pytest.approx(100.0)
    assert a21["div_surplus"] == pytest.approx(0.0)
    assert a21["invested_ext"] == pytest.approx(200 * 70 * 1.0003 + 300 * 70 * 1.0003 - 100.0)
    assert a21["price_pnl"] == pytest.approx(m_end - 100 * 84.0)
    assert a21["end_assets"] == pytest.approx(m_end)
    # 5) 恒等式 + telescoping
    a20 = stat["annual"][0]
    assert a21["end_assets"] == pytest.approx(
        a20["end_assets"] + a21["price_pnl"] + a21["div_surplus"])
    assert sum(a["price_pnl"] + a["div_surplus"] for a in stat["annual"]) \
        == pytest.approx(stat["final_assets"])
    # 6) state 投影：浮亏口径 = 净值 − 外部净投入
    assert state["cash_div"] == pytest.approx(0.0)
    assert state["unreal_pct"] == pytest.approx((m_end - (c_total - 100.0)) / (c_total - 100.0))
    # 7) 净值/成本轴一致：v_arr − c_arr = 净值 − 外部净投入（末值 = total_pnl）
    assert v_arr[-1] - c_arr[-1] == pytest.approx(stat["total_pnl"])


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
    # v8.1.0 P0 锚点：[100, 80, 74]：bar1 首仓 1 手（P0=80），bar2 74 ≤ P0×0.95
    # 穿 k=1 买 2 手 → k_level=2
    stat, state, v_arr, c_arr, flag, buys = _run(p, [100.0, 80.0, 74.0])
    assert state["in_pos"] is True
    assert state["shares"] > 0
    assert state["avg_cost"] == pytest.approx(state["open_cost"] / state["shares"])
    # v8.1.0 下一档 = P0×(1−k_level×add_step) = 80×(1−2×0.05) = 72（旧口径为轮高锚点）
    assert state["next_add_price"] == pytest.approx(80.0 * (1 - 2 * 0.05))
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


# ---------------------------------------------------------------------------
# v8.1.0：首仓双条件（回撤 + N 年分位）+ P0 锚点
# ---------------------------------------------------------------------------

def test_first_buy_requires_both_drawdown_and_pctile():
    """v8.1.0 双条件：回撤达标但分位门未过（first_ok=False）→ 不买；
    分位门过（True）→ 买。[100,100,80,80,80,80] 首仓线=100×0.85=85。"""
    p = _params()
    sig = [100.0, 100.0, 80.0, 80.0, 80.0, 80.0]
    # 分位门未过：全程不买
    stat_off, *_ = _run(p, sig, first_ok=[False] * 6)
    assert stat_off["n_buys"] == 0
    assert stat_off["last_status"] == "never"
    # 分位门过：bar2 破线且 ok=True → 首仓 1 手，P0=80
    stat_on, state_on, *_ , buys_on = _run(p, sig, first_ok=[False, False, True, True, True, True])
    assert stat_on["n_buys"] == 1
    assert state_on["round_anchor"] == pytest.approx(80.0)
    assert buys_on[0][2] == pytest.approx(100.0 * 80.0 * (1 + p.commission))


def test_add_on_ignores_pctile_gate():
    """v8.1.0：加仓无分位条件——首仓后继续下跌且 first_ok=False 仍按 P0 档线加仓。
    [100,100,80,76]：bar2 首仓（ok=True，P0=80）；bar3 76 ≤ P0×0.95=76 且 ok=False
    → 仍买 2 手。"""
    p = _params()
    stat, state, *_ , buys = _run(p, [100.0, 100.0, 80.0, 76.0],
                                  first_ok=[False, False, True, False])
    assert stat["n_buys"] == 2
    assert [b[5] for b in buys] == [1, 2]
    assert [b[4] for b in buys] == [True, False]  # 首仓/加仓标注
    assert state["next_add_price"] == pytest.approx(80.0 * (1 - 2 * 0.05))


def test_second_round_first_buy_needs_both_conditions():
    """v8.1.0：再武装后的第二轮首仓同样双条件。
    [100,80,50,60,50]：bar1 首仓(ok=True)；bar3 60≥50×1.15 rearm（H=60,P0=0）；
    bar4 50 ≤ 60×0.85=51 → 需 ok=True 才买第二轮首仓。"""
    p = _params(add_step=0.5)
    sig = [100.0, 80.0, 50.0, 60.0, 50.0]
    # bar4 分位门未过 → 只有一笔首仓
    stat_no, *_ = _run(p, sig, first_ok=[False, True, False, False, False])
    assert stat_no["n_buys"] == 1
    assert stat_no["n_rearms"] == 1
    assert stat_no["n_rounds"] == 1  # 第二轮未触发首仓 → 轮次不增
    # bar4 分位门过 → 第二轮首仓 1 手
    stat_yes, state_yes, *_ , buys_yes = _run(
        p, sig, first_ok=[False, True, False, False, True])
    assert stat_yes["n_buys"] == 2
    assert stat_yes["n_rounds"] == 2
    assert [b[4] for b in buys_yes] == [True, True]
    assert state_yes["round_anchor"] == pytest.approx(50.0)


def test_rearm_resets_p0_anchor():
    """v8.1.0：rearm 后 P0 清零——加仓投影回到首仓线 H×(1−dd)（无 P0 时），
    新轮首仓后 P0 更新为新轮信号价。"""
    p = _params(add_step=0.5)
    # bar1 80 首仓 P0=80（k=1 线=40 未穿）；bar2 50 新低；bar3 60 rearm（H=60,P0=0）
    stat, state, *_ = _run(p, [100.0, 80.0, 50.0, 60.0])
    assert stat["n_rearms"] == 1
    assert state["round_anchor"] == pytest.approx(0.0)  # P0 已重置
    # 无 P0 时下一档 = 首仓线 60×0.85 = 51
    assert state["next_add_price"] == pytest.approx(60.0 * (1 - 0.15))


# ---------------------------------------------------------------------------
# v8.1.0：first_ok_series / extend_start 纯函数
# ---------------------------------------------------------------------------

def test_first_ok_series_monotone_and_fail_closed():
    """first_ok_series PIT 语义：
    - 严格递增 300 bar（5 年窗口）：当前 bar 恒为窗口最高 → 分位 100 > 40 → 全 False；
    - 严格递减：当前 bar 为窗口最低 → 分位 0 ≤ 40 → 窗口满 60 bar 起全 True；
    - 窗口不足 min_bars=60 的前 59 bar → False（fail-closed，数据不足不买）；
    - pct_max=0 且价格恒定 → 分位 100 → 全 False（页面 max=0 fail-closed 等价）。"""
    from martingale.engine import first_ok_series
    n = 300
    d = np.array([np.datetime64("2020-01-01", "D") + np.timedelta64(i, "D")
                  for i in range(n)], dtype="datetime64[ns]")
    up = np.arange(1.0, n + 1)
    down = np.arange(n, 0, -1, dtype=float)
    flat = np.full(n, 10.0)

    out_up = first_ok_series(d, up, 5, 40)
    assert not out_up.any()

    out_down = first_ok_series(d, down, 5, 40)
    assert not out_down[:59].any()
    assert out_down[59:].all()

    out_zero = first_ok_series(d, flat, 5, 0)
    assert not out_zero.any()

    # 窗口年份非法 → 全 False（fail-closed）
    assert not first_ok_series(d, down, 0, 40).any()


def test_extend_start_calendar_years():
    """extend_start：日历年前移（与 selection/timing 的 _years_ago 同语义）。"""
    from martingale.engine import extend_start
    assert extend_start("2016-11-11", 5) == "2011-11-11"
    assert extend_start("2020-02-29", 1) == "2019-02-28"  # 2/29 → 2/28
    assert extend_start("2023-01-01", 0) == "2023-01-01"  # 0 年前移 = 原样
    # 非闰年 2/28 前移 → 2/28
    assert extend_start("2023-02-28", 2) == "2021-02-28"
