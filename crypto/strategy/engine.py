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

账户模型（Joel 2026-11-11 定案，v7.0.53）：单账户资金闭环——
  首次建仓全部资金为**外部投入**；止盈了结后所得成为**账户余额**，
  滚入下一轮：下一轮建仓/加仓优先从余额扣除，**余额耗尽才追加外部
  投入**。净值曲线 = 持仓市值 + 余额（现金）。external_invested =
  累计外部投入（全期口径），wallet = 期末余额。

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
    h1y: np.ndarray | None = None,
    external_cap: float | None = None,
) -> Dict[str, Any]:
    """单币状态机回测（1h 收盘价序列）。

    :param h1y: 可选的一年高点序列（与 closes 等长）。时间范围回测时由
        runner 用**含窗口前历史**的完整序列计算后切片传入——保证窗口
        起点的第一根 bar 就有准确的一年高点；缺省 = 用 closes 自算
        （全史回测，头部部分窗口）。
    :param external_cap: 可选的外部投入硬预算（USDT，累计口径）。预算
        耗尽后：首仓买不起 → 不建仓（每 bar 重试）；加仓买不起 → 该轮
        截断（后续档位同样买不起，直接停）。**不做部分买入**（整档全买
        或放弃）。余额（了结所得）不受此限。缺省 = 预算无限（原行为）。
    :returns: 结果 dict：已了结轮次 rounds + 未了结 open_round +
        净值曲线 equity（= 持仓市值 + 余额）+ 最大回撤 mdd_pct +
        事件序列 events + external_invested（累计外部投入）+
        wallet（期末余额）。纯函数，零 IO。
    """
    dates = np.asarray(dates)
    c_arr = np.asarray(closes, dtype=float)
    n = len(c_arr)
    if n == 0:
        return {"symbol": symbol, "n_bars": 0,
                "start": None, "end": None,
                "rounds": [], "open_round": None,
                "realized_cash": 0.0, "final_equity": 0.0,
                "mdd_pct": 0.0, "equity": np.array([]), "dates": np.array([]),
                "closes": np.array([]), "events": [],
                "external_invested": 0.0, "wallet": 0.0,
                "note": "无数据"}

    if h1y is None:
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
    wallet = 0.0         # 账户余额现金（了结所得滚存；恒 ≥ 0）
    external = 0.0       # 累计外部投入（余额不足时才追加）
    capped = False       # 本轮是否因预算截断（external_cap 生效）
    rounds: List[Dict[str, Any]] = []
    events: List[Dict[str, Any]] = []  # 图表事件：entry/add/exit（日期+级数+价）
    equity = np.empty(n, dtype=float)

    def _fund(amount: float) -> float:
        """买入出资：先扣余额，不足部分计外部投入（账户模型）。

        external_cap 生效时外部投入累计封顶（硬预算、**不做部分买入**）：
        可用资金 < 整档金额 → 返回 0（调用方整档放弃）；否则全额出资
        并返回 amount。
        """
        nonlocal wallet, external
        avail = wallet + (
            (external_cap - external)
            if external_cap is not None else float("inf"))
        if avail < amount - _EPS:
            return 0.0
        take = amount
        from_wallet = min(wallet, take)
        wallet -= from_wallet
        external += take - from_wallet
        return take

    for i in range(n):
        c = float(c_arr[i])
        if not in_pos:
            # 1) 建仓：价 ≤ 一年高点 × 30%（预算不足 → 不建仓，下 bar 重试）
            if c <= h1y[i] * trig + _EPS:
                usdt = p.base_amount_usdt
                if _fund(usdt) > 0.0:
                    in_pos = True
                    entry = c
                    round_start = i
                    k = 0
                    capped = False
                    shares = usdt * (1.0 - fee) / c
                    round_spent = usdt
                    events.append({"date": str(dates[i])[:10], "type": "entry",
                                   "level": 0, "price": round(c, 4)})
        else:
            # 2) 加仓：固定网格 entry×(1−grid×k)，跳空穿多级 → 同 bar 逐级；
            #    预算不足 → 本轮截断（后续档位更贵，同样买不起）
            while k < _MAX_LEVELS and c <= entry * (1.0 - grid * (k + 1)) + _EPS:
                usdt = p.base_amount_usdt * (p.growth ** (k + 1))
                if _fund(usdt) <= 0.0:
                    capped = True
                    break
                k += 1
                shares += usdt * (1.0 - fee) / c
                round_spent += usdt
                events.append({"date": str(dates[i])[:10], "type": "add",
                               "level": k, "price": round(c, 4)})
            # 3) 止盈：价 ≥ 建仓价 × 10 → 全卖，本轮了结；所得滚入余额
            if c >= entry * p.take_profit_mult - _EPS:
                proceeds = shares * c * (1.0 - fee)
                pnl = proceeds - round_spent
                realized_cash += pnl
                wallet += proceeds
                events.append({"date": str(dates[i])[:10], "type": "exit",
                               "level": 0, "price": round(c, 4)})
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
                    "capped": capped,
                })
                in_pos = False
                shares = 0.0
                entry = 0.0
                round_start = -1
                k = 0
                round_spent = 0.0
                capped = False
        # 净值 = 持仓市值 + 余额（账户口径；首轮余额恒 0，与原口径一致）
        equity[i] = shares * c + wallet

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
            "capped": capped,
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
        "closes": c_arr,
        "events": events,
        "external_invested": round(external, 2),
        "wallet": round(wallet, 2),
    }
