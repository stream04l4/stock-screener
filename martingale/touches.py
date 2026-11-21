# -*- coding: utf-8 -*-
"""盘中触碰信号（v7.0.20）：触发价被**盘中最低价**摸到、但收盘（现价）已回出
信号区的事件——当日保留在「当前信号」，次日移入「历史信号」（Joel 2026-11-08）。

背景：策略信号 = 实时「价 ≤ 触发线」状态。盘中达成后涨回来，信号从页面消失；
且「历史信号」只收状态机**收盘价**回放（收盘未过线 → 无 buy 记录）→ 盘中触碰
完全无痕。本模块补上这一层：

- **判定源** = 腾讯行情「最新行情日」当日最低价（tencent.py IDX_LOW；盘后=全天，
  行情日滚动后旧触碰自然出「当前」段）；
- **事件落盘** = data/alerts/signal_touch_state.json（scripts/signal_touch.py
  每交易日收盘后写（cron 12:05 UTC = 北京 20:05，lake-sync 灌完当日 bar 之后，
  保证 job 的信号线与页面当日视图同口径）；原子替换，防半截状态，与
  pushplus_alert_state 同模式）；
- **消费** = Web /api/strategy_signals：当前段 = 现价在区内 OR 最新行情日已触碰
  （status=touched，「已触回」）；历史段 = 状态机回放 buys ∪ 触碰事件
  （同 (代码,日期,档) 去重：回放 buy 优先——收盘过线那天的触碰不重复记）。

每 (代码, 行情日) 只记**下一档**（页面上的信号线）：更深档位的触碰由次日状态机
推进后的新信号线覆盖；若一日内穿多档且收盘也过线，回放 buys 已全量记录。
"""
from __future__ import annotations

import json
import os
from datetime import date, timedelta
from typing import AbstractSet, Any, Dict, List, Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

STATE_FILENAME = "signal_touch_state.json"
WINDOW_DAYS = 90  # 事件保留窗口（与 Web 历史信号 SIG_HISTORY_WINDOW_DAYS 对齐）


def default_state_path() -> str:
    return os.path.join(PROJECT_ROOT, "data", "alerts", STATE_FILENAME)


def _beijing_now_iso() -> str:
    from datetime import datetime, timedelta, timezone
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def quote_date_of(timestamp: Optional[str]) -> Optional[str]:
    """行情时间戳 'YYYYMMDDHHMMSS' → 'YYYY-MM-DD'；也接受裸日期 'YYYYMMDD'；
    其他长度/格式异常 → None（fail-closed：宁可不判触碰，不用脏时间戳）。"""
    if not timestamp:
        return None
    s = str(timestamp).strip()
    if len(s) not in (8, 14) or not s[:8].isdigit():
        return None
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


def trigger_price(row: Dict[str, Any], held: bool = False) -> Optional[float]:
    """信号触发线（原始口径）：与 /api/strategy_signals 同一分支（v7.0.23
    Joel 定案「非持仓首仓 = 时机成熟 且 价已跌破首仓线，双条件」）——
    持仓股：in_pos → next 档（加仓线）；未 in_pos → 首仓线（不变）；
    非持仓股：**恒用首仓线**（首仓信号线；不再用模拟 next 档——时机门
    （分位+股息）替代旧「非持仓 in_pos 取 next 档」的防幽灵机制，老股锚
    高首仓线远低于市价时由时机门兜住，不产幽灵信号）。"""
    if held and row.get("in_pos"):
        return row.get("next_add_price_raw")
    return row.get("first_level_raw")


def touch_lots(sp, row: Dict[str, Any], held: bool = False) -> Optional[int]:
    """触碰档的建议手数（与 classify_signal 同一口径，v7.0.28 手数化）：
    持仓股 in_pos → first_lots + k×add_lots_step（加仓）；
    否则 → 首仓 first_lots 手（非持仓触碰 = 首仓信号，v7.0.23）。"""
    if sp is None:
        return None
    if held and row.get("in_pos"):
        return int(sp.first_lots + (row.get("k_level") or 0) * sp.add_lots_step)
    return int(sp.first_lots)


def compute_touches(rows: Dict[str, Dict[str, Any]], spot: Dict[str, Dict[str, Any]],
                    sp, held_codes: AbstractSet[str] = frozenset()) -> List[Dict[str, Any]]:
    """逐股判定「最新行情日已触碰」→ 事件列表（无排序保证，按 rows 顺序）。

    触碰 = 行情当日最低价 ≤ 触发线（与 classify_signal 的 active 判定同向：
    价到过线上即算触碰）。无行情/无触发线/低价缺失 → 不产事件（fail-closed）。
    事件 date = **该股行情日**（停牌股行情停在旧日 → 事件也落旧日，不污染当日）。
    :param held_codes: 用户持仓代码集（决定触发线/档位/金额口径，v7.0.23）。
    """
    out: List[Dict[str, Any]] = []
    for c, row in rows.items():
        held = c in held_codes
        trig = trigger_price(row, held=held)
        q = spot.get(c) or {}
        low = q.get("low")
        if trig is None or low is None or trig <= 0 or low <= 0 or low > trig:
            continue
        d = quote_date_of(q.get("timestamp"))
        if d is None:
            continue
        # 档位/首笔标注：仅当持仓且处于轮内加仓档（k>0）才标加仓；
        # k==0（含再武装后新一轮二次建仓）统一标 first_of_round=True
        k_lv = row.get("k_level") or 0
        in_pos_eff = bool(row.get("in_pos")) and held and k_lv > 0
        out.append({
            "ts_code": c,
            "name": row.get("name") or c,
            "date": d,
            "level": k_lv if in_pos_eff else 0,
            "first_of_round": not in_pos_eff,
            "level_price": trig,
            "lots": touch_lots(sp, row, held=held),
            "low_price": round(float(low), 4),
            "price": q.get("price"),
        })
    return out


def latest_for_code(alerts: Dict[str, Dict[str, Any]], ts_code: str) -> Optional[Dict[str, Any]]:
    """从 alerts（支持新键 ts_code|date 及旧裸键 ts_code）取指定股最新一条触碰记录。"""
    best: Optional[Dict[str, Any]] = None
    for k, ev in alerts.items():
        if not isinstance(ev, dict):
            continue
        code = ev.get("ts_code") or (k.split("|", 1)[0] if "|" in k else k)
        if code != ts_code:
            continue
        if best is None or str(ev.get("date") or "") > str(best.get("date") or ""):
            best = ev
    return best


def load_state(path: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """读触碰事件状态 → {key: event}。缺失/损坏 → {}（fail-safe）。"""
    p = path or default_state_path()
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        alerts = data.get("alerts", {})
        return {str(k): v for k, v in alerts.items() if isinstance(v, dict)}
    except (OSError, ValueError):
        return {}


def save_state(path: Optional[str], alerts: Dict[str, Dict[str, Any]],
               updated_at: Optional[str] = None) -> None:
    """原子写（mkstemp + os.replace，防并发竞争同一 .tmp）。"""
    import tempfile
    p = path or default_state_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(p)),
                               prefix=".signal_touch.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"updated_at": updated_at or _beijing_now_iso(),
                       "alerts": alerts}, f, ensure_ascii=False, indent=1, allow_nan=False)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def merge_events(existing: Dict[str, Dict[str, Any]],
                 new: List[Dict[str, Any]],
                 window_days: int = WINDOW_DAYS) -> Dict[str, Dict[str, Any]]:
    """合并今日事件入历史：每 (代码, 日期) 保留最新一条；窗口外老事件滚动清理。

    键格式统一为 ``f"{ts_code}|{date}"``（同时兼容清理历史遗留裸 ts_code 键）。
    """
    merged: Dict[str, Dict[str, Any]] = {}
    for k, ev in existing.items():
        if not isinstance(ev, dict):
            continue
        code = ev.get("ts_code") or (k.split("|", 1)[0] if "|" in k else k)
        d = ev.get("date")
        nk = f"{code}|{d}" if d else str(k)
        merged[nk] = dict(ev)
    for ev in new:
        ev = dict(ev)
        d = ev.get("date")
        nk = f"{ev['ts_code']}|{d}" if d else ev["ts_code"]
        merged[nk] = ev
    if merged:
        dates_present = [e["date"] for e in merged.values() if e.get("date")]
        if dates_present:
            max_d = max(dates_present)
            cutoff = (date.fromisoformat(max_d) - timedelta(days=window_days)).isoformat()
            merged = {k: e for k, e in merged.items()
                      if e.get("date") is None or e["date"] >= cutoff}
    return merged


def dedupe_buys(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """回放 buys 与触碰事件同 (代码,日期,档) 去重：buy 优先（收盘过线 → 回放
    已记，触碰不重复展示）。回放 buy 行**无 source 字段**（或 source!="touch"
    的行均视为 buy）；触碰行 source="touch"。"""
    seen = set()
    out: List[Dict[str, Any]] = []
    # buy 行先占位（输入序扫一遍标记），再逐行收——touch 同键即弃
    for h in history:
        if h.get("source") != "touch":
            seen.add((h["ts_code"], h["date"], h.get("level")))
    for h in history:
        key = (h["ts_code"], h["date"], h.get("level"))
        if h.get("source") == "touch" and key in seen:
            continue
        out.append(h)
    return out
