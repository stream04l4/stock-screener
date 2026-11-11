# -*- coding: utf-8 -*-
"""crypto.strategy —— 加密货币马丁格尔策略（v7.0.47）。

策略规格（Joel 2026-11-11 定案，两个决策门已拍板）：

1. 建仓：无仓且收盘价 ≤ 近一年最高收盘价 × 30%（即从一年高点跌掉 70%）
   → 首笔 10 USDT，entry = 触发 bar 收盘价（本轮回测的锚点）。
2. 加仓：固定网格 entry × (1 − 1% × k)，k = 1,2,… 不设上限；
   第 k 级花费 = 10 × 1.05^k USDT（初始 10，每次 ×1.05）。
3. 持有：上涨不加仓，新高**不刷新** entry（区别于股票侧 high-ladder 的
   滚动高点——加密侧一轮内 entry 恒定，直到止盈了结）。
4. 止盈：收盘价 ≥ entry × 10 → 全部卖出，本轮结束；entry 重置，
   下一轮重新等规则 1（一年高点滚动重算）触发。

执行口径：1h K 线收盘价成交；跳空一下穿多级 → 同一 bar 逐级加仓；
taker 手续费 fee_bps（默认 10bps = Binance 现货 0.1%），买卖双向计。
一年高点：滚动 365 天（8760 根 1h bar）收盘价最大值，含当日 bar；
数据头不足一年时用已有全部历史（部分窗口），报告中注明。

边界纪律（设计文档 §7）：回测 = 策略验证工具，不接自动交易、不做预测。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

import numpy as np

_EPS = 1e-9
_MAX_LEVELS = 5000  # 网格级数保险丝（异常数据防护，正常路径远够不到）


@dataclass(frozen=True)
class StrategyParams:
    """策略参数（crypto/config/crypto.yaml 的 strategy 段加载，单一定义点）。"""
    first_drop_pct: float = 70.0     # 建仓：价 ≤ 一年高点 × (1 − 70%) = 30%
    add_grid_pct: float = 1.0        # 加仓级距：建仓价的 1%/级（固定网格）
    base_amount_usdt: float = 10.0   # 初始建仓金额
    growth: float = 1.05             # 每次加仓 × 1.05
    take_profit_mult: float = 10.0   # 止盈：价 ≥ 建仓价 × 10
    fee_bps: float = 10.0            # taker 手续费（0 = 关闭）
    lookback_h: int = 8760           # 一年高点窗口（小时）

    @classmethod
    def from_cfg(cls, cfg: Dict[str, Any]) -> "StrategyParams":
        """从 crypto.yaml 全文的 strategy 段构造；缺省字段用默认值。"""
        s = cfg.get("strategy", {}) or {}
        return cls(
            first_drop_pct=float(s.get("first_drop_pct", 70.0)),
            add_grid_pct=float(s.get("add_grid_pct", 1.0)),
            base_amount_usdt=float(s.get("base_amount_usdt", 10.0)),
            growth=float(s.get("growth", 1.05)),
            take_profit_mult=float(s.get("take_profit_mult", 10.0)),
            fee_bps=float(s.get("fee_bps", 10.0)),
            lookback_h=int(s.get("lookback_h", 8760)),
        )

    def trigger_frac(self) -> float:
        """建仓触发 = 一年高点的比例（0.30）。"""
        return 1.0 - self.first_drop_pct / 100.0


def rolling_high_1y(closes: np.ndarray, window: int) -> np.ndarray:
    """滚动窗口最高收盘价（含当日 bar）。数据不足窗口时 = 已有历史最大。"""
    c = np.asarray(closes, dtype=float)
    n = len(c)
    out = np.empty(n, dtype=float)
    if n == 0:
        return out
    if n < window:
        out[:] = np.maximum.accumulate(c)
        return out
    out[: window - 1] = np.maximum.accumulate(c[: window - 1])
    win = np.lib.stride_tricks.sliding_window_view(c, window)
    out[window - 1:] = win.max(axis=1)
    return out


def run_coin(
    symbol: str,
    dates: np.ndarray,
    closes: np.ndarray,
    p: StrategyParams,
) -> Dict[str, Any]:
    """单币状态机回测（1h 收盘价序列）。

    :returns: 结果 dict：已了结轮次 rounds + 未了结 open_round +
        净值曲线 equity + 最大回撤 mdd_pct。纯函数，零 IO。
    """
    dates = np.asarray(dates)
    c_arr = np.asarray(closes, dtype=float)
    n = len(c_arr)
    if n == 0:
        return {"symbol": symbol, "n_bars": 0, "rounds": [], "open_round": None,
                "equity": np.array([]), "mdd_pct": 0.0, "note": "无数据"}

    h1y = rolling_high_1y(c_arr, p.lookback_h)
    trig = p.trigger_frac()
    fee = p.fee_bps / 10000.0
    grid = p.add_grid_pct / 100.0

    in_pos = False
    entry = 0.0          # 本轮建仓价（本轮内恒定，不刷新）
    round_start = -1     # 本轮首仓 bar 下标
    k = 0                # 已加仓级数（首仓不计级）
    shares = 0.0         # 当前持有数量
    round_spent = 0.0    # 本轮累计支出（USDT 本金口径；手续费折所得/数量）
    realized_cash = 0.0  # 已了结轮次净盈亏累计（proceeds − spent）
    rounds: List[Dict[str, Any]] = []
    equity = np.empty(n, dtype=float)

    for i in range(n):
        c = float(c_arr[i])
        if not in_pos:
            # 1) 建仓：价 ≤ 一年高点 × 30%
            if c <= h1y[i] * trig + _EPS:
                in_pos = True
                entry = c
                round_start = i
                k = 0
                usdt = p.base_amount_usdt
                shares = usdt * (1.0 - fee) / c
                round_spent = usdt
        else:
            # 2) 加仓：固定网格 entry×(1−grid×k)，跳空穿多级 → 同 bar 逐级
            while k < _MAX_LEVELS and c <= entry * (1.0 - grid * (k + 1)) + _EPS:
                k += 1
                usdt = p.base_amount_usdt * (p.growth ** k)
                shares += usdt * (1.0 - fee) / c
                round_spent += usdt
            # 3) 止盈：价 ≥ 建仓价 × 10 → 全卖，本轮了结
            if c >= entry * p.take_profit_mult - _EPS:
                proceeds = shares * c * (1.0 - fee)
                pnl = proceeds - round_spent
                realized_cash += pnl
                rounds.append({
                    "entry_date": str(dates[round_start])[:10],
                    "entry": round(entry, 4),
                    "exit_date": str(dates[i])[:10],
                    "exit_price": round(c, 4),
                    "bars": i - round_start + 1,
                    "max_level": k,
                    "spent_usdt": round(round_spent, 2),
                    "proceeds_usdt": round(proceeds, 2),
                    "pnl_usdt": round(pnl, 2),
                    "pnl_pct": round(pnl / round_spent * 100.0, 2),
                })
                in_pos = False
                shares = 0.0
                entry = 0.0
                round_start = -1
                k = 0
                round_spent = 0.0
        equity[i] = shares * c + realized_cash

    # 未了结轮次（持仓中）
    open_round = None
    if in_pos and shares > 0:
        last = float(c_arr[-1])
        open_round = {
            "entry_date": str(dates[round_start])[:10],
            "entry": round(entry, 4),
            "k_level": int(k),
            "spent_usdt": round(round_spent, 2),
            "shares": round(shares, 6),
            "avg_cost": round(round_spent / shares, 4),
            "last_price": round(last, 4),
            "value_usdt": round(shares * last, 2),
            "unrealized_usdt": round(shares * last - round_spent, 2),
            "unrealized_pct": round((shares * last / round_spent - 1.0) * 100.0, 2),
            "tp_price": round(entry * p.take_profit_mult, 4),
            "bars_holding": n - round_start,
        }

    # 最大回撤（基于净值曲线；净值恒 ≥0，peak=0 段回撤视为 0）
    peak = np.maximum.accumulate(equity)
    safe_peak = np.where(peak > 0, peak, 1.0)
    dd = np.where(peak > 0, 1.0 - equity / safe_peak, 0.0)
    mdd_pct = float(dd.max() * 100.0)

    return {
        "symbol": symbol,
        "n_bars": n,
        "start": str(dates[0])[:10],
        "end": str(dates[-1])[:10],
        "rounds": rounds,
        "open_round": open_round,
        "realized_cash": round(realized_cash, 2),
        "final_equity": round(float(equity[-1]), 2),
        "mdd_pct": round(mdd_pct, 2),
        "equity": equity,
        "dates": dates,
    }
