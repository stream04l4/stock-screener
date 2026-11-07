# -*- coding: utf-8 -*-
"""持仓策略点位信号（纯函数，零 I/O）。

v7.0.11（Joel 2026-10-24："持仓到策略点位要微信提醒"）：从 web/app.py
get_my_positions 内联信号段抽出，供 **Web /api/my_positions** 与
**scripts/position_alert.py（微信定时推送）** 共用同一口径——单一定义点，
防两处漂移。

口径（与 Web 页面"加仓信号"逐字一致）：
- 现价（实时价优先；缺失回退湖最后价）≤ 下一档触发价（**原始口径** raw）→ 触发；
- 触发价口径：引擎 next_add_price 基于 hfq 信号价，raw = hfq / F（F = sig[-1]/exec[-1]），
  与实时价/原始价同口径才可比（分红股 F 可达数十倍，直接比会假信号）；
- 动作：策略状态机 in_pos（持仓中，第 k 档）→ "add"（加到 k+1 档，base×growth^k）；
  未持仓（k=0）→ "buy"（首仓 initial_amount）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# 「即将进入信号区」显示阈值（百分数）：现价高于下一档触发价且在
# trigger×(1+3%) 以内 → approaching。
# **显示参数，非策略阈值**：不影响状态机任何决策/成交金额（schema 定案勿增删，
# 不进 config/strategy.yaml、不进 Web 配置）——只决定股票出现在哪个展示分区。
DEFAULT_APPROACH_PCT = 3.0


def classify_signal(price: Optional[float], trigger: Optional[float],
                    in_pos: Optional[bool], k_level: int, sp: Optional[Any] = None,
                    approach_pct: float = DEFAULT_APPROACH_PCT) -> Dict[str, Any]:
    """现价 vs 下一档触发价（原始口径）→ 四态分类（纯函数，零 I/O）。

    状态（v7.0.19 策略信号中心口径，active 与 build_signals 逐字一致）：
    - active:      price ≤ trigger（当前信号，已触线）
    - approaching: trigger < price ≤ trigger×(1+approach_pct/100)（即将进入信号区）
    - idle:        price 高于 approaching 带
    - no_data:     price 或 trigger 缺失/非正（湖中无数据/价源不可用）

    action/suggested_amount 对 active 与 approaching 均给出（即将触发的动作与
    建议金额：in_pos→加仓 base×growth^k；未持仓→首仓 initial_amount）；
    sp=None 时 suggested_amount=None（与 build_signals 缺省语义一致）。

    :return: {status, action, suggested_amount, distance_pct}
        distance_pct = (price−trigger)/trigger×100（active ≤ 0，
        approaching ∈ (0, approach_pct]，2 位小数；no_data 为 None）
    """
    k = k_level or 0
    if price is None or trigger is None or trigger <= 0:
        return {"status": "no_data", "action": None, "suggested_amount": None,
                "distance_pct": None}
    if in_pos:
        action = "add"
        suggested = (sp.base_add_amount * (sp.growth_ratio ** k)) if sp is not None else None
    else:
        action = "buy"
        suggested = sp.initial_amount if sp is not None else None
    distance_pct = round((price - trigger) / trigger * 100.0, 2)
    if price <= trigger:
        status = "active"
    elif price <= trigger * (1.0 + approach_pct / 100.0):
        status = "approaching"
    else:
        status = "idle"
    return {
        "status": status,
        "action": action if status in ("active", "approaching") else None,
        "suggested_amount": round(suggested) if (
            suggested is not None and status in ("active", "approaching")) else None,
        "distance_pct": distance_pct,
    }


def build_signals(positions: List[Dict[str, Any]], sp: Optional[Any]) -> List[Dict[str, Any]]:
    """对 positions（含 spot_price/next_add_price_raw/in_pos/k_level）产信号列表。

    :param positions: 每行须含 ts_code/name/spot_price/last_price/
        next_add_price_raw/in_pos/k_level；
    :param sp: 策略参数对象（.base_add_amount/.growth_ratio/.initial_amount）；
        None 时 suggested_amount 置 None（策略参考缺省不报错）。
    :return: 按 ts_code 升序的信号列表（形状同 Web /api/my_positions.signals）。
    """
    signals: List[Dict[str, Any]] = []
    for pos in positions:
        price_now = pos.get("spot_price")
        if price_now is None:
            price_now = pos.get("last_price")
        nap_raw = pos.get("next_add_price_raw")
        # v7.0.19：判定收敛到 classify_signal（active 语义与旧内联判定逐字一致：
        # price_now ≤ nap_raw 才触发；无价/无触发价 → no_data → 跳过）
        c = classify_signal(price_now, nap_raw, pos.get("in_pos"),
                            pos.get("k_level") or 0, sp)
        if c["status"] != "active":
            continue
        next_level = (pos.get("k_level") or 0) + 1
        signals.append({
            "ts_code": pos.get("ts_code"),
            "name": pos.get("name"),
            "price": price_now,
            "next_add_price": nap_raw,
            "k_level": pos.get("k_level") or 0,
            "next_level": next_level,
            "suggested_amount": c["suggested_amount"],
            "action": c["action"],
        })
    signals.sort(key=lambda x: x.get("ts_code") or "")
    return signals
