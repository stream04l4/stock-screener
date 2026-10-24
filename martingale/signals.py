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
        if not (price_now is not None and nap_raw is not None and price_now <= nap_raw):
            continue
        k = pos.get("k_level") or 0
        if pos.get("in_pos"):
            suggested = (sp.base_add_amount * (sp.growth_ratio ** k)) if sp is not None else None
            action, next_level = "add", k + 1
        else:
            suggested = sp.initial_amount if sp is not None else None
            action, next_level = "buy", 1
        signals.append({
            "ts_code": pos.get("ts_code"),
            "name": pos.get("name"),
            "price": price_now,
            "next_add_price": nap_raw,
            "k_level": k,
            "next_level": next_level,
            "suggested_amount": round(suggested) if suggested is not None else None,
            "action": action,
        })
    signals.sort(key=lambda x: x.get("ts_code") or "")
    return signals
