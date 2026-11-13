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
- 动作：策略状态机 in_pos（持仓中，第 k 档）→ "add"（加到 k+1 档，
  (first_lots + k×add_lots_step) 手）；未持仓（k=0）→ "buy"（首仓 first_lots 手）。
  v7.0.28 手数口径：建议量 = 手数（suggested_lots）。

v8.2.0 累计差值口径（Joel 2026-11-13 定案）：
- 理论持仓 = 用户**建仓日起**策略买入的累计股数（pos["strat_shares_since"]，
  状态机真实买入序列按日期过滤——建仓早于时机/晚于多轮均正确；建仓日锚点由
  账本 since_date 记录，缺省 = 今天）+ 本档手数（新档触发即执行后的理论累计）；
- 实际持仓 = 用户账本股数（pos["shares"]）；
- 差值 = 理论 − 实际：>0 → suggested_lots = 差值（向上取整到整手，至少 1
  手），信号行附 theory_lots/actual_lots/gap_lots；≤0（持仓够了/超配）→
  **不产信号**（不提醒）；
- strat_shares_since/shares 缺失（湖中无数据等）→ 回退旧行为（建议本档手数，
  不附差值字段），保持旧调用方形状不变。
- 该口径**仅持仓判断路径**（Web 我的持仓 / 微信持仓通道）生效；回测与新建仓
  逻辑零影响（shares_since 由 state_rows(since_map=…) 单独计算，缺省不产）。
"""
from __future__ import annotations

import math

from typing import Any, Dict, List, Optional, Tuple

# 「即将进入信号区」显示阈值（百分数）：现价高于下一档触发价且在
# trigger×(1+approach_pct%) 以内 → approaching。
# **显示参数，非策略阈值**：不影响状态机任何决策/成交金额（schema 定案勿增删，
# 不进 Web 配置）——只决定股票出现在哪个展示分区。v7.0.30 起生产值来自
# config/strategy.yaml 的 timing.approach_pct（缺键回退本默认值 5.0）。
DEFAULT_APPROACH_PCT = 5.0


def classify_signal(price: Optional[float], trigger: Optional[float],
                    in_pos: Optional[bool], k_level: int, sp: Optional[Any] = None,
                    approach_pct: float = DEFAULT_APPROACH_PCT) -> Dict[str, Any]:
    """现价 vs 下一档触发价（原始口径）→ 四态分类（纯函数，零 I/O）。

    状态（v7.0.19 策略信号中心口径，active 与 build_signals 逐字一致）：
    - active:      price ≤ trigger（当前信号，已触线）
    - approaching: trigger < price ≤ trigger×(1+approach_pct/100)（即将进入信号区）
    - idle:        price 高于 approaching 带
    - no_data:     price 或 trigger 缺失/非正（湖中无数据/价源不可用）

    action/suggested_lots 对 active 与 approaching 均给出（即将触发的动作与
    建议手数：in_pos→加仓 (first_lots + k×add_lots_step) 手；未持仓→首仓
    first_lots 手）；sp=None 时 suggested_lots=None（与 build_signals 缺省
    语义一致）。

    :return: {status, action, suggested_lots, distance_pct}
        distance_pct = (price−trigger)/trigger×100（active ≤ 0，
        approaching ∈ (0, approach_pct]，2 位小数；no_data 为 None）
    """
    k = k_level or 0
    if price is None or trigger is None or trigger <= 0:
        return {"status": "no_data", "action": None, "suggested_lots": None,
                "distance_pct": None}
    if in_pos:
        action = "add"
        suggested = (sp.first_lots + k * sp.add_lots_step) if sp is not None else None
    else:
        action = "buy"
        suggested = sp.first_lots if sp is not None else None
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
        "suggested_lots": int(suggested) if (
            suggested is not None and status in ("active", "approaching")) else None,
        "distance_pct": distance_pct,
    }


def _gap_suggested(sp: Optional[Any], pos: Dict[str, Any], k: int, in_pos: bool,
                   base_suggested: Optional[int]) -> Tuple[Optional[int], Optional[Dict[str, float]], bool]:
    """v8.2.0 累计差值（纯函数）：理论（建仓日起策略买入累计 + 本档）vs
    实际（账本股数）→ 建议。

    本档手数 = 新档触发即执行的那一档：in_pos（第 k 档）→ first_lots +
    k×add_lots_step；未持仓 → first_lots。

    :return: (suggested_lots, extra, judged)；extra = {theory_lots, actual_lots,
        gap_lots}。judged=True 表示差值已判定：此时 suggested=None = 不提醒
        （持仓够了）。strat_shares_since/shares/lot_size 缺失 →
        (base_suggested, None, False)：未判定，回退旧行为（含 sp=None 的
        防御性调用方）。
    """
    theory_shares = pos.get("strat_shares_since")
    shares = pos.get("shares")
    if sp is None or not getattr(sp, "lot_size", None) \
            or theory_shares is None or shares is None:
        return base_suggested, None, False
    stage_lots = (sp.first_lots + k * sp.add_lots_step) if in_pos else sp.first_lots
    theory_lots = float(theory_shares) / sp.lot_size + stage_lots
    actual_lots = float(shares) / sp.lot_size
    gap = theory_lots - actual_lots
    extra = {"theory_lots": round(theory_lots, 2), "actual_lots": round(actual_lots, 2),
             "gap_lots": round(max(0.0, gap), 2)}
    if gap <= 0:
        return None, extra, True  # 持仓够了（或超配）→ 不提醒
    return max(1, math.ceil(gap)), extra, True


def judge_held_lots(sp: Optional[Any], k: int, in_pos: bool,
                    base_suggested: Optional[int],
                    theory_shares: Optional[float],
                    shares: Optional[float]) -> Tuple[Optional[int], Optional[Dict[str, float]], bool]:
    """v8.2.1 持仓股判定逻辑（公开入口，单一定义点，Joel 2026-11-13：两处信号
    一致）。策略中心 /api/strategy_signals 的持仓股行动用它（与
    /api/my_positions、微信 position_alert 同一口径）：

    - 差值 >0 → 建议补差值手数（至少 1 手）；
    - 差值 ≤0（持仓够了/超配）→ (None, extra, True)：不产动作；
    - 数据缺失（theory_shares/shares/sp 缺）→ (base_suggested, None, False)：
      未判定，回退旧行为（展示原动作）。
    """
    return _gap_suggested(sp, {"strat_shares_since": theory_shares, "shares": shares},
                          k, in_pos, base_suggested)


def build_signals(positions: List[Dict[str, Any]], sp: Optional[Any]) -> List[Dict[str, Any]]:
    """对 positions（含 spot_price/next_add_price_raw/in_pos/k_level/shares/
    strat_shares_since）产信号列表。

    v8.2.0：strat_shares_since（用户建仓日起策略买入累计，state_rows
    since_map 产出）+ shares（账本股数）齐备时按累计差值口径——实际 ≥ 理论
    （本档执行后）→ 不产信号；差值 >0 → 建议补差值手数（行内附 theory_lots/
    actual_lots/gap_lots）。缺 strat_shares_since 回退旧行为。

    :param positions: 每行须含 ts_code/name/spot_price/last_price/
        next_add_price_raw/in_pos/k_level；
    :param sp: 策略参数对象（.first_lots/.add_lots_step/.lot_size）；
        None 时 suggested_lots 置 None（策略参考缺省不报错）。
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
        k = pos.get("k_level") or 0
        next_level = k + 1
        suggested, extra, judged = _gap_suggested(sp, pos, k, bool(pos.get("in_pos")),
                                                  c["suggested_lots"])
        if judged and suggested is None:
            continue  # v8.2.0：实际 ≥ 当前轮理论 → 持仓够了，不提醒（未判定的回退不跳过）
        sig = {
            "ts_code": pos.get("ts_code"),
            "name": pos.get("name"),
            "price": price_now,
            "next_add_price": nap_raw,
            "k_level": k,
            "next_level": next_level,
            "suggested_lots": int(suggested) if suggested is not None else None,
            "action": c["action"],
        }
        if extra is not None:
            sig.update(extra)
        signals.append(sig)
    signals.sort(key=lambda x: x.get("ts_code") or "")
    return signals
