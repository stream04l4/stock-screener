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

v8.2.0 累计差值口径（Joel 2026-11-13 定案）——**v8.2.3 退役**（Joel 2026-11-23
轮次口径定案，显示层重构）：差值补差建议与「实际≥理论→不提醒」抑制整体移除，
建议量 = 到线即执行的本档手数（classify_signal 单点）；「持仓已够不提醒」
不再存在（持仓信号触线必提醒，k=0 首仓仍过时机门）。理论总数改为**多轮总
理论值**（用户首轮窗口起点起策略累计，见 round_windows/user_round），仅显示
（Web 表格 theory_lots/gap_lots 列与信号行透传字段），不参与建议。
- **首档（k=0）时机门**（build_signals 的 timing_map 参数，Joel 2026-11-13
  二次定案「二次建仓也要符合时机门才能提示」）：再武装后新一轮的首仓
  （持仓股 k=0 触线 = 二次建仓）或模拟从未持仓轮次的首仓触线，须**当前**
  时机门通过才产信号（fail-closed：缺条目 = 未过）——「不受约束」只覆盖
  轮内加仓档（k≥1）。信号行附 new_round（k=0 → True，「建仓」而非「加仓」）
  与 in_pos（文案区分「二次建仓」/「建仓」）。
- v8.2.3：信号行附 round_no（用户相对轮次号：有持仓的第一个轮次 = 首轮，
  其后每次轮内再武装 +1；首期内未建仓则下轮仍为首轮——持仓股有值，
  非持仓 None；前端文案「首轮第 N 档 加仓」/「首轮建仓」/「第二轮建仓」，
  前缀不带股票名）。
- 该口径**仅持仓判断路径**（Web 我的持仓 / 策略中心持仓股 / 微信持仓通道）
  生效；回测与新建仓逻辑零影响（状态机本身 v8.1.0 首仓双条件含 PIT 时机门；
  round_marks 为显示专用字段，引擎决策零改动）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

# 「即将进入信号区」显示阈值（百分数）：现价高于下一档触发价且在
# trigger×(1+approach_pct%) 以内 → approaching。
# **显示参数，非策略阈值**：不影响状态机任何决策/成交金额（schema 定案勿增删，
# 不进 Web 配置）——只决定股票出现在哪个展示分区。v7.0.30 起生产值来自
# config/strategy.yaml 的 timing.approach_pct（缺键回退本默认值 5.0）。
DEFAULT_APPROACH_PCT = 5.0


# ---------------------------------------------------------------------------
# v8.2.3 轮次口径（Joel 2026-11-23 定案；纯显示层，零 I/O，引擎决策不受影响）
# ---------------------------------------------------------------------------

def round_windows(marks: List[Tuple[str, str]]) -> List[Tuple[str, Optional[str]]]:
    """轮次窗口切分（纯函数）。

    marks = [(date, "first_buy"|"rearm")]（引擎 state["round_marks"]，
    YYYY-MM-DD 时间序；rearm 仅 k≥1 轮内再武装 = 终结一轮）：
    - 第 1 轮窗口 W_1 = [F_1, R_1)：本轮首笔起，至终结本轮的轮内再武装；
    - 第 i 轮窗口（i≥2）W_i = [R_{i-1}, R_i)：**再武装后的等待期（再武装
      日至下轮首笔前）归属下一轮的首仓生命周期**（Joel：建仓时间落在首次
      建仓的生命周期内 = 该轮）；
    - 末窗口 end=None（开放）。
    不变式：n_rearm ∈ {n_rounds−1（当前轮活跃）、n_rounds（当前轮已再武装
    等待首仓）}；窗口数 = n_rearm + 1 = 当前显示轮号。
    :return: [(start_date, end_date|None)] 时间序。
    """
    windows: List[List[Any]] = []
    for d, kind in marks:
        if kind == "rearm":
            if windows:
                windows[-1][1] = d  # 终结前窗口
            windows.append([d, None])  # 新一轮窗口（等待期）开启
        else:  # first_buy
            if not windows:
                windows.append([d, None])  # 仅第 1 轮在此开新窗口；
            # 第 2 轮起 first_buy 落在前一轮 rearm 已开启的窗口内，不切分
    return [(s, e) for s, e in windows]


def user_round(marks: List[Tuple[str, str]], since_date: Optional[str]):
    """当前显示轮的用户相对轮次号 + 其首轮窗口起点（纯函数）。

    轮次编号规则（Joel 2026-11-23）：有持仓的第一个轮次 = 首轮；之后每次
    轮内再武装 +1（第二轮/第三轮……）；若首个轮次期内一直未建仓，下一次
    再武装后的轮次仍叫首轮 → 即**首轮 = 包含建仓日 since_date 的窗口**
    （建仓早于第一轮 → 首轮 = 第 1 轮）。
    当前轮次号 = 窗口数 − 首轮窗口下标（其首轮 = 下标 0 → 1 轮；其后每
    再武装 +1）。
    :return: (round_no, window_start)；window_start = 首轮窗口起点（多轮总
        理论值的累计起点，YYYY-MM-DD；无 marks/无锚点 → None → 调用方按
        无数据处理）。
    """
    windows = round_windows(marks)
    if not windows:
        return 1, None
    if not since_date:
        # 无锚点（账本缺 since_date，调用方按缺省=今天处理）→ 今天必落在末
        # 开放窗口 → 首轮 = 当前轮
        return 1, windows[-1][0]
    if since_date < windows[0][0]:
        first_idx = 0  # 建仓早于第一轮 → 首轮 = 第 1 轮
    else:
        first_idx = len(windows) - 1  # 末窗口开放 → 必然命中，兜底取末
        for i, (s, e) in enumerate(windows):
            if s <= since_date and (e is None or since_date < e):
                first_idx = i
                break
    return len(windows) - first_idx, windows[first_idx][0]


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
    if price is None or price <= 0 or trigger is None or trigger <= 0:
        return {"status": "no_data", "action": None, "suggested_lots": None,
                "distance_pct": None}
    # 统一 k==0（含再武装后二次建仓 in_pos=True, k=0）动作键为 "buy"，
    # 避免买入登记后微信去重键 buy vs add 分裂引发重复推送及历史已推标注失效；
    # 文案区分「首仓」与「二次建仓」由上层 new_round + in_pos 决定。
    if in_pos and k > 0:
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


def build_signals(positions: List[Dict[str, Any]], sp: Optional[Any],
                  timing_map: Optional[Dict[str, bool]] = None) -> List[Dict[str, Any]]:
    """对 positions（含 spot_price/next_add_price_raw/in_pos/k_level）产信号列表。

    v8.2.3（Joel 2026-11-23 轮次口径定案）：建议量 = 到线即执行的本档手数
    （classify_signal 单点）；v8.2.0 差值口径（补差建议 + 「实际≥理论→不
    提醒」抑制）已退役——持仓信号触线必提醒。多轮总理论值（首轮窗口起点
    起累计）仅显示：positions 行内带 theory_lots/actual_lots/gap_lots 时
    原样透传到信号行，不参与建议。

    v8.2.0 补（Joel 2026-11-13 二次定案）：首档（k=0）触线 = 建仓信号
    （再武装后新一轮 = 「二次建仓」），须当前时机门通过才提示——
    :param timing_map: {ts_code: 时机是否通过}；提供时 k=0 信号仅在该条目为
        True 时产出（fail-closed：缺条目 = 未过），轮内加仓档（k≥1）不受
        约束（「不受约束」定案只豁免加仓档）。None = 无时机门（旧行为）。

    :param positions: 每行须含 ts_code/name/spot_price/last_price/
        next_add_price_raw/in_pos/k_level；可选 round_no（v8.2.3 用户相对
        轮次号，透传到信号行供前端「首轮第 N 档」文案）与 theory_lots/
        actual_lots/gap_lots（显示字段，原样透传）。
    :param sp: 策略参数对象（.first_lots/.add_lots_step/.lot_size）；
        None 时 suggested_lots 置 None（策略参考缺省不报错）。
    :param timing_map: 见上（可选）。
    :return: 按 ts_code 升序的信号列表（形状同 Web /api/my_positions.signals；
        每行附 new_round/in_pos/round_no 字段）。
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
        # v8.2.0 补：首档（k=0）= 建仓（再武装后新一轮即「二次建仓」）→
        # 须过当前时机门才提示；k≥1 轮内加仓档不受约束
        if k == 0 and timing_map is not None \
                and not timing_map.get(pos.get("ts_code") or "", False):
            continue
        next_level = k + 1
        # v8.2.3：建议量 = 本档手数（到线即执行的纯配置口径）；理论总数
        # 仅显示（行内透传），不参与建议；「持仓已够不提醒」抑制已退役
        suggested = c["suggested_lots"]
        sig = {
            "ts_code": pos.get("ts_code"),
            "name": pos.get("name"),
            "price": price_now,
            "next_add_price": nap_raw,
            "k_level": k,
            "next_level": next_level,
            "suggested_lots": int(suggested) if suggested is not None else None,
            "action": c["action"],
            # v8.2.0 补：首档触线 = 建仓（k=0）；in_pos 供文案区分「二次建仓」
            # （已有轮次、再武装后新轮）与「建仓」（模拟从未持仓）
            "new_round": (k == 0),
            "in_pos": bool(pos.get("in_pos")),
            # v8.2.3：用户相对轮次号（持仓行有值；非持仓 None → 前端无轮次前缀）
            "round_no": pos.get("round_no"),
        }
        # v8.2.3：多轮总理论值显示字段原样透传（首轮窗口起点累计，仅展示）
        for _k in ("theory_lots", "actual_lots", "gap_lots"):
            if pos.get(_k) is not None:
                sig[_k] = pos[_k]
        signals.append(sig)
    signals.sort(key=lambda x: x.get("ts_code") or "")
    return signals
