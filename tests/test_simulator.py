# -*- coding: utf-8 -*-
"""组合模拟器已知答案单测（报告 R5 测试规格 1：合成价格序列 → 成本/再投资/换手）。

合成 ``SynthPriceSource`` 实现 PriceSource 协议（零 live、零缓存依赖）：
- 每只股票 = [(date, open, close, tradestatus), ...]；
- 日历 = 全部股票日期的并集。

已知答案覆盖：
1. 成本函数直接验证（佣金下限5元/印花税日期分段/过户费/滑点，手工算值）；
2. T+1 成交（决策日提交 → 次日开盘价成交，fills 记在次日）；
3. 再投资/收益口径 B（af1 日收益：价格 10→11 → NAV +10%）；
4. 换手率 = Σ|Δw|/2（建仓 0.5 / 清仓 0.5）；
5. 停牌顺延 ≤5 日成交、超期转现金（drop_to_cash）；
6. 退市按最后成交价退出 + delisting_haircut_pct 打折。

所有数值在测试内手工推导（不依赖被测代码计算期望值）。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

import pytest

from backtest.simulator import (CostsConfig, PortfolioSimulator, RebalanceOrder,
                                SuspensionConfig)


class SynthPriceSource:
    """合成价格源（bars: {code: [(date, open|None, close|None, tradestatus), ...]}）。"""

    def __init__(self, bars: Dict[str, List[Tuple[str, Optional[float], Optional[float], int]]]):
        self.bars = bars
        self.cal = sorted({d for v in bars.values() for d, *_ in v})

    def trade_calendar(self, start: date, end: date) -> List[str]:
        s, e = start.isoformat(), end.isoformat()
        return [d for d in self.cal if s <= d <= e]

    def exec_price(self, code: str, day: str, use_open: bool):
        for (d, o, c, ts) in self.bars.get(code, []):
            if d == day:
                if ts == 0:
                    return None, False          # 停牌 → 顺延
                if use_open and o is not None:
                    return float(o), True
                if c is not None:
                    return float(c), False      # open 缺失 → close 兜底
                return None, False
        return None, False                      # 无该日 bar（退市/未上市）

    def last_bar_date(self, code: str) -> Optional[str]:
        bs = self.bars.get(code, [])
        return bs[-1][0] if bs else None

    def last_close_on_or_before(self, code: str, day: str) -> Optional[float]:
        out = None
        for (d, o, c, ts) in self.bars.get(code, []):
            if d <= day and c is not None:
                out = float(c)
        return out


def _costs(**kw) -> CostsConfig:
    base = dict(commission_bp=0.0, min_commission_cny=0.0, initial_capital_cny=1_000_000,
                stamp_tax_sell=[], transfer_fee_bp=0.0, slippage_bp=0.0,
                delisting_haircut_pct=0.0)
    base.update(kw)
    return CostsConfig(**base)


def _sim(src, costs=None, use_open=True, susp=None):
    return PortfolioSimulator(src, costs or _costs(),
                              susp or SuspensionConfig(max_defer_days=5,
                                                      on_timeout="drop_to_cash"),
                              use_open=use_open)


def _days(start: str, n: int) -> List[str]:
    d0 = date.fromisoformat(start)
    return [(d0 + timedelta(days=i)).isoformat() for i in range(n)]


# ---------------------------------------------------------------------------
# 1. 成本函数（手工算值）
# ---------------------------------------------------------------------------
def test_cost_functions_known_values():
    """佣金(双边万2.5,下限5元/100万=5e-6) + 印花税(卖出单边分段) + 过户费 + 滑点。"""
    costs = CostsConfig(commission_bp=2.5, min_commission_cny=5.0,
                        initial_capital_cny=1_000_000,
                        stamp_tax_sell=[{"from": "1900-01-01", "to": "2023-08-27", "bp": 10},
                                        {"from": "2023-08-28", "to": "2999-12-31", "bp": 5}],
                        transfer_fee_bp=0.1, slippage_bp=5.0)
    # 印花税分段（按成交日）
    assert costs.stamp_tax_bp("2023-08-27") == 10.0      # 旧档 0.1%
    assert costs.stamp_tax_bp("2023-08-28") == 5.0       # 新档 0.05%（财政部2023-39号）
    assert costs.stamp_tax_bp("2026-09-04") == 5.0
    assert CostsConfig().stamp_tax_bp("2020-01-01") == 0.0   # 无分段配置 → 0（不静默扣费）

    src = SynthPriceSource({"x": []})
    sim = _sim(src, costs=costs)
    # 买入 1.0（归一化净值单位）：佣金 max(2.5e-4, 5e-6)=2.5e-4 + 过户 1e-5 + 滑点 5e-4
    assert sim._buy_cost(1.0) == pytest.approx(2.5e-4 + 1e-5 + 5e-4)
    # 卖出 1.0 @2023-08-27：佣金 2.5e-4 + 印花税 1e-3 + 过户 1e-5 + 滑点 5e-4 = 1.76e-3
    proceeds, cost = sim._sell_cost(1.0, "2023-08-27")
    assert cost == pytest.approx(1.76e-3)
    assert proceeds == pytest.approx(1.0 - 1.76e-3)
    # 卖出 @2023-08-28：印花税减半(5e-4) → 2.5e-4+5e-4+1e-5+5e-4 = 1.26e-3
    _, cost2 = sim._sell_cost(1.0, "2023-08-28")
    assert cost2 == pytest.approx(1.26e-3)


def test_commission_floor_kicks_in():
    """小单佣金触发下限：amount=0.01 → 费率 2.5e-6 < 下限 5e-6 → 取下限。"""
    costs = CostsConfig(commission_bp=2.5, min_commission_cny=5.0,
                        initial_capital_cny=1_000_000, transfer_fee_bp=0.0, slippage_bp=0.0)
    sim = _sim(SynthPriceSource({"x": []}), costs=costs)
    # 下限生效：buy_cost = 5e-6（而非 2.5e-6）
    assert sim._buy_cost(0.01) == pytest.approx(5e-6)
    # 大单按费率：1.0 → 2.5e-4 > 5e-6
    assert sim._buy_cost(1.0) == pytest.approx(2.5e-4)


# ---------------------------------------------------------------------------
# 2. T+1 成交 + fills
# ---------------------------------------------------------------------------
def test_t1_execution_and_fills():
    """决策日 D 提交 → D+1 开盘价成交；fills 记在 D+1。"""
    days = _days("2025-01-01", 5)
    bars = {"A": [(d, 10.0, 10.0, 1) for d in days]}   # open=close=10
    src = SynthPriceSource(bars)
    sim = _sim(src)
    sim.step(days[0])                                   # NAV(1)=1.0（全现金）
    sim.submit_order(RebalanceOrder(decision_date=days[0], targets={"A": 1.0}))
    sim.step(days[1])                                   # D+1 开盘成交 @10
    r = sim.result
    assert days[1] in r.fills and r.fills[days[1]]["A"] == pytest.approx(10.0)
    assert len(r.dates) == 2 and r.nav[-1] == pytest.approx(1.0)   # 零成本 → NAV 不变
    # open 缺失 → close 兜底计数（本例有 open → 0）
    assert sum(r.open_fallback_days.values()) == 0


def test_open_missing_falls_back_to_close():
    """open=None 的行：use_open=True 时按 close 成交并计入 fallback 笔数。"""
    days = _days("2025-01-01", 3)
    bars = {"A": [(days[0], None, 10.0, 1), (days[1], None, 10.0, 1), (days[2], None, 10.0, 1)]}
    src = SynthPriceSource(bars)
    sim = _sim(src, use_open=True)
    sim.step(days[0])
    sim.submit_order(RebalanceOrder(decision_date=days[0], targets={"A": 1.0}))
    sim.step(days[1])
    assert sim.result.open_fallback_days.get(days[1]) == 1


# ---------------------------------------------------------------------------
# 3. 收益口径 B（af1 日收益 = 再投资）
# ---------------------------------------------------------------------------
def test_return_scheme_b_reinvestment():
    """价格 10→11（af1 语义，分红已隐含）→ NAV +10%（零成本）。"""
    days = _days("2025-01-01", 4)
    bars = {"A": [(days[0], 10.0, 10.0, 1), (days[1], 10.0, 10.0, 1),
                  (days[2], 11.0, 11.0, 1), (days[3], 11.0, 11.0, 1)]}
    src = SynthPriceSource(bars)
    sim = _sim(src)
    sim.step(days[0])
    sim.submit_order(RebalanceOrder(decision_date=days[0], targets={"A": 1.0}))
    sim.step(days[1])   # 成交 @10，NAV=1.0
    sim.step(days[2])   # close 11 → NAV = 1.1（日收益 +10%）
    assert sim.result.nav[-1] == pytest.approx(1.1)


def test_partial_weight_and_cash():
    """目标权重和 <1 → 余数留现金：{A:0.5, B:0.3}，价格恒定 → NAV 恒 1.0。"""
    days = _days("2025-01-01", 3)
    bars = {"A": [(d, 10.0, 10.0, 1) for d in days],
            "B": [(d, 20.0, 20.0, 1) for d in days]}
    src = SynthPriceSource(bars)
    sim = _sim(src)
    sim.step(days[0])
    sim.submit_order(RebalanceOrder(decision_date=days[0], targets={"A": 0.5, "B": 0.3}))
    sim.step(days[1])
    assert sim.result.nav[-1] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 4. 换手率 = Σ|Δw|/2（决策日权重）
# ---------------------------------------------------------------------------
def test_turnover_known_values():
    """建仓 {A:1} → 单边换手 0.5；清仓 {} → 0.5。"""
    days = _days("2025-01-01", 7)
    bars = {"A": [(d, 10.0, 10.0, 1) for d in days]}
    src = SynthPriceSource(bars)
    sim = _sim(src)
    sim.step(days[0])
    sim.submit_order(RebalanceOrder(decision_date=days[0], targets={"A": 1.0}))
    sim.step(days[1])                                   # 建仓成交
    assert sim.result.turnover[days[1]] == pytest.approx(0.5)
    # 第 2 次调仓（决策 days[3]，执行 days[4]）：清仓
    sim.step(days[2])
    sim.step(days[3])
    sim.submit_order(RebalanceOrder(decision_date=days[3], targets={}))
    sim.step(days[4])
    assert sim.result.turnover[days[4]] == pytest.approx(0.5)
    assert sim.result.n_rebalances == 2


# ---------------------------------------------------------------------------
# 5. 停牌顺延 / 超期转现金（R2）
# ---------------------------------------------------------------------------
def test_suspension_defer_then_fill():
    """目标股执行日起停牌 3 日、第 4 日复牌 → 顺延成交（≤max_defer_days=5）。"""
    days = _days("2025-01-01", 8)
    # A：day1~day3 停牌(ts=0)，day4 起复牌 open=close=10
    bars = {"A": [(days[0], 10.0, 10.0, 1), (days[1], None, None, 0),
                  (days[2], None, None, 0), (days[3], None, None, 0),
                  (days[4], 10.0, 10.0, 1)] + [(d, 10.0, 10.0, 1) for d in days[5:]]}
    src = SynthPriceSource(bars)
    sim = _sim(src)
    sim.step(days[0])
    sim.submit_order(RebalanceOrder(decision_date=days[0], targets={"A": 1.0}))
    for d in days[1:]:
        sim.step(d)
    # day4 复牌成交（attempts: day1 失败→1, day2→2, day3→3, day4 成功）
    assert "A" in sim.result.fills.get(days[4], {}), f"fills={sim.result.fills}"
    assert sim.result.n_drop_to_cash == 0


def test_suspension_timeout_drop_to_cash():
    """连续停牌 >5 日 → 放弃该股，权重转现金（on_timeout=drop_to_cash）。"""
    days = _days("2025-01-01", 10)
    bars = {"A": [(d, None, None, 0) for d in days]}     # 全程停牌
    src = SynthPriceSource(bars)
    sim = _sim(src)
    sim.step(days[0])
    sim.submit_order(RebalanceOrder(decision_date=days[0], targets={"A": 1.0}))
    for d in days[1:]:
        sim.step(d)
    assert sim.result.n_drop_to_cash == 1
    assert "A" not in sim._shares                        # 未建仓 → 全现金
    assert sim.result.nav[-1] == pytest.approx(1.0)      # 现金无收益


# ---------------------------------------------------------------------------
# 6. 退市退出 + haircut（R2）
# ---------------------------------------------------------------------------
def test_delisting_exit_last_price_with_haircut():
    """持仓股 K线末根 < 决策日 → 最后成交价×(1−haircut) 退出；拖累单列。"""
    days = _days("2025-01-01", 8)
    # C：只到 day3（day4 起退市）；A：全程存活（提供日历）
    bars = {"C": [(d, 10.0, 10.0, 1) for d in days[:4]],
            "A": [(d, 10.0, 10.0, 1) for d in days]}
    src = SynthPriceSource(bars)
    costs = _costs(delisting_haircut_pct=10.0)          # 打 9 折
    sim = _sim(src, costs=costs)
    sim.step(days[0])
    sim.submit_order(RebalanceOrder(decision_date=days[0], targets={"C": 1.0}))
    sim.step(days[1])                                   # 建仓 @10
    sim.step(days[2]); sim.step(days[3])                # NAV 恒 1.0（价格不变）
    # 决策 days[4]：C 末根 days[3] < days[4] → 退市退出；执行 days[5]
    sim.submit_order(RebalanceOrder(decision_date=days[4], targets={}))
    sim.step(days[5])
    r = sim.result
    assert r.n_delisted_exits == 1
    # 退出价 = 10×0.9=9 → 回款 ≈ 9（零其他成本）→ NAV ≈ 0.9
    assert r.nav[-1] == pytest.approx(0.9, rel=1e-3)
    # 拖累 = haircut 损失(0.1×10) / 决策日净值(1.0) = 10%
    assert r.delisting_drag_pct == pytest.approx(10.0, abs=0.5)


def test_delisted_target_cannot_buy():
    """目标股在决策日已退市（且非持仓）→ 无法买入，权重转现金。"""
    days = _days("2025-01-01", 8)
    # A：全程存活（日历）；E：只到 day1（day2 起退市，从未持有）
    bars = {"A": [(d, 10.0, 10.0, 1) for d in days],
            "E": [(d, 10.0, 10.0, 1) for d in days[:2]]}
    src = SynthPriceSource(bars)
    sim = _sim(src)
    sim.step(days[0])
    # 决策 days[3]：E 末根 days[1] < days[3] → 退市，买入转现金；A 正常建仓
    sim.submit_order(RebalanceOrder(decision_date=days[3], targets={"A": 0.5, "E": 0.5}))
    sim.step(days[4])
    assert sim.result.n_drop_to_cash == 1
    assert "E" not in sim._shares and "A" in sim._shares
