"""af_rebase —— adj_factor 基准对齐（v7.0.16；v7.0.21 提升至仓库根共享模块）。

模块位置说明：原 `lake/af_rebase.py`。因 `martingale` 包（timing/selection CLI
预对齐护栏）也需调用本模块，而 test_lake_zero_import 断言两包零 import 耦合
（lake↔martingale 平行、文件级解耦），故提升到仓库根成为共享模块：lake 与
martingale 均可 import，互不依赖不变。纯 duckdb + stdlib，无 lake 包依赖。
logger 名保留 "lake.af_rebase"（日志 grep 连续性）。

背景（2026-10-26，sh.600018 上港集团 5 年分位=0.5% 假信号）：
多源池（sina→tencent→tdx）按可用性**逐日**给每只股票选源（~2-10 只/天换源，
2026-09-16 单日 399 只）。各适配器的 adj_factor 是"源私有归一化"（窗口首日
hfq÷raw 比值，绝对基准任意）。kline_daily 假定同一股票 af 单一基准 → 换源日
产生**假断崖**（sh.600018：af 16.987→1.747，原始价连续，-90% 假跌）→
hfq/qfq view 断步 → 5 年分位/MA250/马丁阶梯/十年回测全部失真。全市场近 5.5 年
823 只多源混杂。

**关键不变量**：af 的绝对基准对消费者无害——hfq=raw×af 整体缩放等价、
qfq=raw×af/MAX(af) 基准自消；唯一有害的是**同一股票内部的断步**。
因此修复=把每只股票的 af 段统一换算到最早段的基准（canonical base），
只消除断步，不改变任何真实除权跳变。

**判定（只用湖内数据，分红表仅用于确认"已知事件日"）**：对相邻行 d0→d1（af 均>0）：
- af_move = af(d1)/af(d0)；price_ratio = close(d0)/close(d1)
- 段内派生 af 在两次除权事件之间**恒定**（所有适配器同性质）；事件日
  源 hfq 连续 ⇒ 事件日 af_move 应 = close(d0)/close(d1)（含当日实际漂移）。
- 无已知事件且无大缺口（price_ratio<1.08）：期望 af_move=1
  → 假跳变 iff |log(af_move)| > tol
- 已知事件且有量化公式（exp_move > 0）：期望 af_move = exp_move
  → 假跳变 iff |log(af_move/exp_move)| > tol（对齐到官方步长，保留真实涨跌）
- 已知事件但无金额/送转占位（expected_move is None）：kind="unquantified_event"
  → 仅检测标记，不再用价格比强行改写 af
- 无已知事件且无大缺口（price_ratio<1.08）：期望 af_move=1
  → 假跳变 iff |log(af_move)| > tol
- 无已知事件但大缺口（price_ratio>=1.08，疑似送转或市场暴跌）：kind="suspected"
  → 禁止以价格比盖章改写源 af！默认仅告警记录（verify_event_fn 明确核实后才允许对齐）

**换算系数**：
- 已知量化事件日：s_new = s_prev × af(d0) × exp_move / af(d1)
- 无事件日：      s_new = s_prev × af(d0) / af(d1)
- 幻影反盖章：    s_new = s_prev × af(d0) / af(d1)（将误写入的价格比除掉，恢复平坦）
换算后 canonical hfq = close×af×s 在边界处连续（与 hfq 定义一致）。
幂等：修复后边界 af_move 回到期望值 → 重跑不再触发。
"""

from __future__ import annotations

import logging
import math
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

log = logging.getLogger("lake.af_rebase")

# 容差校准（2026-10-26 生产湖实测，16.4M 同源无事件对）：
# 同源派生 af 漂移 P99=0.033% / P999=0.072%（sina 窗口基准微漂）→ 2% 容差
# 下同源零误报；基准切换的源私有基准比通常 ≫2%（实测 3%~9.7 倍）。
DEFAULT_TOL = 0.02
# price_ratio ≥ 1.08 ⇒ 当日真实跌幅 ≥7.4%（疑似送转/配股/崩盘，分红表无记录时）
LARGE_DROP_RATIO = 1.08

__all__ = [
    "DEFAULT_TOL", "LARGE_DROP_RATIO",
    "classify_boundary", "rebase_stock_af", "repair_all_af", "rebase_recent",
    "find_spurious_jumps", "find_phantom_stamps", "un_stamp_phantoms",
    "restore_flattened_events",
]


def classify_boundary(af0: float, af1: float, close0: float, close1: float,
                      has_known_event: bool,
                      tol: float = DEFAULT_TOL,
                      expected_move: Optional[float] = None) -> Tuple[bool, str]:
    """判定边界 (d0→d1) 是否假基准跳变。

    :param expected_move: 已知除权事件的理论官方复权步长（c0*(1+bonus)/(c0-cash)）；
        提供时用它替代含当日市场涨跌幅的 close0/close1，避免除权日正常涨跌 >2%
        被误杀抹平。缺省 None 标为 unquantified_event（占位不盖章）。
    :return: (is_spurious, kind)，kind ∈ {"no_event","event","unquantified_event","suspected"}。
    """
    af_move = af1 / af0
    price_ratio = close0 / close1
    if has_known_event:
        if expected_move is not None and expected_move > 0:
            kind = "event"
            spurious = abs(math.log(af_move / expected_move)) > tol
        else:
            kind = "unquantified_event"
            spurious = (abs(math.log(af_move / price_ratio)) > tol
                        and abs(math.log(af_move)) > tol)
    elif price_ratio >= LARGE_DROP_RATIO:
        kind = "suspected"
        spurious = (abs(math.log(af_move / price_ratio)) > tol
                    and abs(math.log(af_move)) > tol)
    else:
        kind = "no_event"
        spurious = abs(math.log(af_move)) > tol
    return spurious, kind


def _expected_event_move(c0: float,
                         ev_list: List[Tuple[str, float, float]]) -> Optional[float]:
    """根据 (d0, d1] 区间内聚合后的除权事件列表计算理论官方复权步长。

    :param ev_list: [(ex_date, cash_dps_sum, stk_div_per_share_sum), ...] 升序。
    :return: >0 的理论步长；若所有事件均无金额/送转（仅日期占位）或数据异常 → None。
    """
    cur_p = float(c0)
    move = 1.0
    has_quant = False
    for _ex, cash, bonus in ev_list:
        cash_f = float(cash or 0.0)
        bonus_f = float(bonus or 0.0)
        if cash_f == 0.0 and bonus_f == 0.0:
            continue
        denom = cur_p - cash_f
        if denom <= 0 or (1.0 + bonus_f) <= 0:
            return None
        p_ex = denom / (1.0 + bonus_f)
        move *= cur_p / p_ex
        cur_p = p_ex
        has_quant = True
    return move if has_quant and move > 0 else None


def _events_in_interval(sorted_events: List[Tuple[str, float, float]],
                        d0: str, d1: str) -> List[Tuple[str, float, float]]:
    """返回落在 (d0, d1] 区间内的全部除权事件（覆盖当日与停牌顺延除权）。"""
    return [e for e in sorted_events if d0 < e[0] <= d1]


def _load_div_events(con, ts_code: Optional[str] = None) -> Dict[str, List[Tuple[str, float, float]]]:
    """读取 dividend_events（自动兼容只含 ts_code/ex_date 的精简表）。"""
    cols = {str(r[1]).lower() for r in con.execute("PRAGMA table_info('dividend_events')").fetchall()}
    has_cash = "cash_dps" in cols
    has_stk = "stk_div" in cols
    cash_expr = "SUM(COALESCE(cash_dps, 0))" if has_cash else "0.0"
    stk_expr = "SUM(COALESCE(stk_div, 0))" if has_stk else "0.0"
    out: Dict[str, List[Tuple[str, float, float]]] = {}
    if ts_code is not None:
        rows = con.execute(
            f"SELECT ex_date, {cash_expr}, {stk_expr} "
            "FROM dividend_events WHERE ts_code=? AND ex_date IS NOT NULL "
            "GROUP BY ex_date ORDER BY ex_date",
            [ts_code]).fetchall()
        out[ts_code] = [(str(d)[:10], float(cd or 0.0), float(sd or 0.0)) for d, cd, sd in rows]
    else:
        rows = con.execute(
            f"SELECT ts_code, ex_date, {cash_expr}, {stk_expr} "
            "FROM dividend_events WHERE ex_date IS NOT NULL "
            "GROUP BY ts_code, ex_date ORDER BY ts_code, ex_date").fetchall()
        for code, d, cd, sd in rows:
            out.setdefault(code, []).append((str(d)[:10], float(cd or 0.0), float(sd or 0.0)))
    return out


def _apply_segments_tx(con, ts_code: str, norm: List[Dict[str, Any]],
                       segments: List[Tuple[float, int, int]]) -> int:
    """在单事务内按段缩放 adj_factor，返回改写行数。"""
    to_write = [(s, i0, i1) for s, i0, i1 in segments if abs(s - 1.0) >= 1e-9]
    if not to_write:
        return 0
    rewritten = 0
    in_tx = False
    try:
        con.execute("BEGIN TRANSACTION")
        in_tx = True
    except Exception:  # noqa: BLE001 - 已在外部事务内
        in_tx = False
    try:
        for s_scale, i0, i1 in to_write:
            n = con.execute(
                "UPDATE kline_daily SET adj_factor = adj_factor * ? "
                "WHERE ts_code=? AND date BETWEEN ? AND ? "
                "AND adj_factor IS NOT NULL RETURNING adj_factor",
                [s_scale, ts_code, norm[i0]["date"], norm[i1]["date"]]).fetchall()
            rewritten += len(n)
        if in_tx:
            con.execute("COMMIT")
    except Exception:
        if in_tx:
            try:
                con.execute("ROLLBACK")
            except Exception:  # noqa: BLE001
                pass
        raise
    return rewritten


def rebase_stock_af(
    con, ts_code: str,
    tol: float = DEFAULT_TOL,
    verify_event_fn: Optional[Callable[[str, str, str], Optional[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    """单股 af 基准对齐：检测假跳变并把各段 af 换算到最早段基准后写回。

    - no_event / event（已知量化除权事件）：按平坦(1.0)或官方理论步长对齐；
    - suspected（跌>=8% 无除权记录）与 unquantified_event（占位无金额）：
      严禁以价格比(c0/c1)吸收！仅记 suspected 告警；仅当传入 verify_event_fn
      明确返回 confirmed=True/False 时才按官方步长或平坦对齐。
    :param verify_event_fn: 可选核查回调 (ts_code, d0, d1) -> {"confirmed": bool, "cash": float, "bonus": float} | None。
    """
    rows = con.execute(
        "SELECT date, close, adj_factor FROM kline_daily "
        "WHERE ts_code=? AND adj_factor IS NOT NULL AND adj_factor > 0 "
        "ORDER BY date", [ts_code]).fetchall()
    if len(rows) < 2:
        return {"ts_code": ts_code, "rows": len(rows), "jumps": [],
                "suspected": [], "rewritten": 0}
    sorted_events = _load_div_events(con, ts_code=ts_code).get(ts_code, [])
    norm = [{"date": str(d)[:10], "close": c, "af": a} for d, c, a in rows]

    segments: List[Tuple[float, int, int]] = []
    jumps: List[Dict[str, Any]] = []
    suspected: List[Dict[str, Any]] = []
    scale = 1.0
    seg_start = 0
    for i in range(1, len(norm)):
        r0, r1 = norm[i - 1], norm[i]
        if None in (r0["close"], r1["close"]) or r0["close"] <= 0 or r1["close"] <= 0:
            continue
        interval_evs = _events_in_interval(sorted_events, r0["date"], r1["date"])
        has_ev = bool(interval_evs)
        exp_move = _expected_event_move(r0["close"], interval_evs) if has_ev else None
        spurious, kind = classify_boundary(
            r0["af"], r1["af"], r0["close"], r1["close"],
            has_ev, tol, expected_move=exp_move)
        if not spurious:
            continue

        old_scale = scale
        out_kind = kind
        new_scale: Optional[float] = None
        if kind == "no_event":
            new_scale = scale * r0["af"] / r1["af"]
        elif kind == "event" and exp_move is not None and exp_move > 0:
            new_scale = scale * (r0["af"] * exp_move) / r1["af"]
        else:
            # suspected / unquantified_event：严禁使用 price_ratio=(c0/c1) 吸收！
            ver: Optional[Dict[str, Any]] = None
            if verify_event_fn is not None:
                try:
                    ver = verify_event_fn(ts_code, r0["date"], r1["date"])
                except Exception as exc:  # noqa: BLE001
                    log.warning("af_rebase verify_event_fn(%s, %s..%s) 异常，跳过改写: %s",
                                ts_code, r0["date"], r1["date"], exc)
                    ver = None
            if isinstance(ver, dict) and ver.get("confirmed") is True:
                v_move = _expected_event_move(
                    r0["close"],
                    [(r1["date"], float(ver.get("cash") or 0.0), float(ver.get("bonus") or 0.0))])
                if v_move is not None and v_move > 0:
                    new_scale = scale * (r0["af"] * v_move) / r1["af"]
                    out_kind = "verified_event"
            elif isinstance(ver, dict) and ver.get("confirmed") is False:
                new_scale = scale * r0["af"] / r1["af"]
                out_kind = "verified_no_event"

        if new_scale is None:
            suspected.append({
                "ts_code": ts_code,
                "prev_date": r0["date"],
                "date": r1["date"],
                "kind": kind,
                "c0": float(r0["close"]),
                "c1": float(r1["close"]),
                "af0": float(r0["af"]),
                "af1": float(r1["af"]),
                "price_ratio": round(float(r0["close"]) / float(r1["close"]), 6),
                "af_move": round(float(r1["af"]) / float(r0["af"]), 6),
            })
            continue

        scale = new_scale
        jumps.append({"date": r1["date"], "kind": out_kind,
                      "af_ratio": round(r1["af"] / r0["af"], 6),
                      "scale_before": round(old_scale, 6),
                      "scale_after": round(scale, 6)})
        segments.append((old_scale, seg_start, i - 1))
        seg_start = i
    segments.append((scale, seg_start, len(norm) - 1))

    rewritten = _apply_segments_tx(con, ts_code, norm, segments)
    if jumps:
        log.warning("af_rebase %s：%d 个假基准跳变已对齐（%d 行改写，示例 %s）",
                    ts_code, len(jumps), rewritten, jumps[:3])
    if suspected:
        log.warning("af_rebase %s：%d 个疑似大缺口边界保持源 af 不动（待核实：%s）",
                    ts_code, len(suspected),
                    ", ".join(f"{x['date']}(px={x['price_ratio']},af={x['af_move']})"
                              for x in suspected[:3]))
    return {"ts_code": ts_code, "rows": len(norm), "jumps": jumps,
            "suspected": suspected, "rewritten": rewritten}


def find_spurious_jumps(con, since_date: Optional[str] = None,
                        max_pairs: Optional[int] = None,
                        tol: float = DEFAULT_TOL) -> List[Dict[str, Any]]:
    """全市场扫描假基准跳变（SQL 无损预筛 + 同一判定函数，纯湖内零网络）。

    :param since_date: 只报告 date>=since_date 的跳变对（None=全史）。
    :param max_pairs: 可选候选上限（None=不截断，彻底避免 16.8M 行被 30w LIMIT 截断）。
    """
    where_parts = ["t.d0 IS NOT NULL"]
    params: List[Any] = []
    if since_date is not None:
        where_parts.append("t.d1 >= ?")
        params.append(since_date)
    # 数学完备无损预筛：若 af_move 在 tol 内且区间 (d0, d1] 无任何除权事件，则必为正常平稳对
    where_parts.append("""(
        ABS(LN(t.af1 / t.af0)) > ?
        OR EXISTS (
            SELECT 1 FROM dividend_events d
            WHERE d.ts_code = t.ts_code
              AND d.ex_date IS NOT NULL
              AND d.ex_date > t.d0 AND d.ex_date <= t.d1
        )
    )""")
    params.append(float(tol))
    where_sql = " AND ".join(where_parts)
    limit_sql = f"LIMIT {int(max_pairs)}" if (max_pairs is not None and int(max_pairs) > 0) else ""
    pairs = con.execute(f"""
        SELECT t.ts_code, t.d0, t.d1, t.c0, t.c1, t.af0, t.af1
        FROM (SELECT ts_code,
                     date d1, LAG(date)       OVER w d0,
                     close c1, LAG(close)     OVER w c0,
                     adj_factor af1, LAG(adj_factor) OVER w af0
              FROM kline_daily
              WHERE adj_factor IS NOT NULL AND adj_factor > 0
                AND close IS NOT NULL AND close > 0
              WINDOW w AS (PARTITION BY ts_code ORDER BY date)) t
        WHERE {where_sql}
        ORDER BY t.ts_code, t.d1
        {limit_sql}
    """, params).fetchall()
    events_by_code = _load_div_events(con, ts_code=None)
    out: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for code, d0, d1, c0, c1, af0, af1 in pairs:
        d0s, d1s = str(d0)[:10], str(d1)[:10]
        interval_evs = _events_in_interval(events_by_code.get(code, []), d0s, d1s)
        has_ev = bool(interval_evs)
        exp_move = _expected_event_move(c0, interval_evs) if has_ev else None
        spurious, kind = classify_boundary(
            af0, af1, c0, c1, has_ev, tol=tol, expected_move=exp_move)
        if spurious:
            seen.add(code)
            out.append({"ts_code": code, "date": d1s, "prev_date": d0s, "kind": kind,
                        "af_ratio": round(af1 / af0, 6)})
    log.info("af_rebase 扫描：候选对 %d，跳变 %d（涉及 %d 股）",
             len(pairs), len(out), len(seen))
    return out


def find_phantom_stamps(con, since_date: str = "2026-01-01",
                        min_af_move: float = 1.075,
                        match_tol: float = DEFAULT_TOL) -> Dict[str, Any]:
    """扫描历史疑似「暴跌日幻影盖章」及「被价格比覆盖的真除权失真」边界。

    特征：af(d1)/af(d0) >= min_af_move 且 af(d1)/af(d0) ≈ close(d0)/close(d1)（在 match_tol 内）。
    - 若 (d0, d1] 无除权记录 → phantoms（幻影盖章，目标步长应恢复 1.0）；
    - 若 (d0, d1] 有量化除权且 |ln(af_move / exp_move)| > match_tol → event_distortions。
    """
    pairs = con.execute("""
        SELECT t.ts_code, t.d0, t.d1, t.c0, t.c1, t.af0, t.af1
        FROM (SELECT ts_code,
                     date d1, LAG(date) OVER w d0,
                     close c1, LAG(close) OVER w c0,
                     adj_factor af1, LAG(adj_factor) OVER w af0
              FROM kline_daily
              WHERE adj_factor IS NOT NULL AND adj_factor > 0
                AND close IS NOT NULL AND close > 0
              WINDOW w AS (PARTITION BY ts_code ORDER BY date)) t
        WHERE t.d0 IS NOT NULL
          AND t.d1 >= ?
          AND (t.af1 / t.af0) >= ?
          AND ABS(LN((t.af1 / t.af0) / (t.c0 / t.c1))) <= ?
        ORDER BY t.ts_code, t.d1
    """, [since_date, float(min_af_move), float(match_tol)]).fetchall()
    events_by_code = _load_div_events(con, ts_code=None)
    phantoms: List[Dict[str, Any]] = []
    event_distortions: List[Dict[str, Any]] = []
    for code, d0, d1, c0, c1, af0, af1 in pairs:
        d0s, d1s = str(d0)[:10], str(d1)[:10]
        af_move = float(af1) / float(af0)
        price_ratio = float(c0) / float(c1)
        interval_evs = _events_in_interval(events_by_code.get(code, []), d0s, d1s)
        item = {
            "ts_code": code, "prev_date": d0s, "date": d1s,
            "c0": float(c0), "c1": float(c1),
            "af0": float(af0), "af1": float(af1),
            "af_move": round(af_move, 6),
            "price_ratio": round(price_ratio, 6),
        }
        if not interval_evs:
            phantoms.append(item)
        else:
            exp_move = _expected_event_move(float(c0), interval_evs)
            if exp_move is not None and exp_move > 0:
                if abs(math.log(af_move / exp_move)) > match_tol:
                    item["expected_move"] = round(exp_move, 6)
                    event_distortions.append(item)
    return {
        "since_date": since_date,
        "phantoms": phantoms,
        "event_distortions": event_distortions,
        "stocks_affected": len({p["ts_code"] for p in phantoms}),
        "distortion_stocks": len({p["ts_code"] for p in event_distortions}),
    }


def un_stamp_phantoms(con, phantoms: List[Dict[str, Any]],
                      dry_run: bool = True) -> Dict[str, Any]:
    """对幻影盖章边界执行代数反盖章：把后续段落除以误写入的步长比，恢复边界平坦。

    同一股票多处幻影边界会按时间序累积连除，同时完整保留其余真实除权步长。
    """
    by_code: Dict[str, Set[str]] = {}
    for item in phantoms:
        code = str(item.get("ts_code") or "").strip()
        d1 = str(item.get("date") or "")[:10]
        if code and d1:
            by_code.setdefault(code, set()).add(d1)
    if not by_code:
        return {"dry_run": dry_run, "stocks": 0, "boundaries": 0,
                "rows_rewritten": 0, "sample": []}

    rows_total = 0
    sample: List[Dict[str, Any]] = []
    for code, bad_dates in sorted(by_code.items()):
        krows = con.execute(
            "SELECT date, close, adj_factor FROM kline_daily "
            "WHERE ts_code=? AND adj_factor IS NOT NULL AND adj_factor > 0 "
            "ORDER BY date", [code]).fetchall()
        if len(krows) < 2:
            continue
        norm = [{"date": str(d)[:10], "close": c, "af": float(a)} for d, c, a in krows]
        segments: List[Tuple[float, int, int]] = []
        scale = 1.0
        seg_start = 0
        hit = 0
        for i in range(1, len(norm)):
            if norm[i]["date"] not in bad_dates:
                continue
            r0, r1 = norm[i - 1], norm[i]
            old_scale = scale
            scale = scale * (r0["af"] / r1["af"])
            segments.append((old_scale, seg_start, i - 1))
            seg_start = i
            hit += 1
        segments.append((scale, seg_start, len(norm) - 1))
        if not hit:
            continue
        if dry_run:
            n_est = sum(i1 - i0 + 1 for s, i0, i1 in segments if abs(s - 1.0) >= 1e-9)
            rows_total += n_est
        else:
            rows_total += _apply_segments_tx(con, code, norm, segments)
        if len(sample) < 10:
            sample.append({"ts_code": code, "dates": sorted(bad_dates),
                           "final_scale": round(scale, 6)})
    return {
        "dry_run": dry_run,
        "stocks": len(by_code),
        "boundaries": sum(len(v) for v in by_code.values()),
        "rows_rewritten": rows_total,
        "sample": sample,
    }


def restore_flattened_events(con, since_date: Optional[str] = None,
                             min_exp_move: float = 1.005,
                             flat_tol: float = 0.002,
                             dev_tol: float = 0.004) -> Dict[str, Any]:
    """恢复已补齐分红记录但因旧 rebase/缺失被抹平的中小步长真除权（含 0.5%~2% 步长股）。

    判定：区间 (d0, d1] 存在官方步长 >= min_exp_move，而当前库内 af 几乎平坦
    (|ln(af_move)| <= flat_tol 且 |ln(af_move / exp_move)| >= dev_tol) → 按 exp_move 对齐。
    """
    where_sql = "AND t.d1 >= ?" if since_date else ""
    params: List[Any] = [since_date] if since_date else []
    pairs = con.execute(f"""
        SELECT t.ts_code, t.d0, t.d1, t.c0, t.af0, t.af1
        FROM (SELECT ts_code,
                     date d1, LAG(date) OVER w d0,
                     close c1, LAG(close) OVER w c0,
                     adj_factor af1, LAG(adj_factor) OVER w af0
              FROM kline_daily
              WHERE adj_factor IS NOT NULL AND adj_factor > 0
                AND close IS NOT NULL AND close > 0
              WINDOW w AS (PARTITION BY ts_code ORDER BY date)) t
        WHERE t.d0 IS NOT NULL
          {where_sql}
          AND EXISTS (
              SELECT 1 FROM dividend_events d
              WHERE d.ts_code = t.ts_code
                AND d.ex_date IS NOT NULL
                AND d.ex_date > t.d0 AND d.ex_date <= t.d1
          )
        ORDER BY t.ts_code, t.d1
    """, params).fetchall()
    events_by_code = _load_div_events(con, ts_code=None)
    to_fix: Dict[str, List[Tuple[str, float]]] = {}
    for code, d0, d1, c0, af0, af1 in pairs:
        d0s, d1s = str(d0)[:10], str(d1)[:10]
        evs = _events_in_interval(events_by_code.get(code, []), d0s, d1s)
        exp_move = _expected_event_move(float(c0), evs) if evs else None
        if exp_move is None or exp_move < min_exp_move:
            continue
        af_move = float(af1) / float(af0)
        if abs(math.log(af_move)) <= flat_tol and abs(math.log(af_move / exp_move)) >= dev_tol:
            to_fix.setdefault(code, []).append((d1s, exp_move))

    rows_rewritten = 0
    restored_details: List[Dict[str, Any]] = []
    for code, items in sorted(to_fix.items()):
        move_by_date = {d: mv for d, mv in items}
        krows = con.execute(
            "SELECT date, close, adj_factor FROM kline_daily "
            "WHERE ts_code=? AND adj_factor IS NOT NULL AND adj_factor > 0 "
            "ORDER BY date", [code]).fetchall()
        if len(krows) < 2:
            continue
        norm = [{"date": str(d)[:10], "close": c, "af": float(a)} for d, c, a in krows]
        segments: List[Tuple[float, int, int]] = []
        scale = 1.0
        seg_start = 0
        for i in range(1, len(norm)):
            mv = move_by_date.get(norm[i]["date"])
            if mv is None:
                continue
            r0, r1 = norm[i - 1], norm[i]
            old_scale = scale
            scale = scale * (r0["af"] * mv) / r1["af"]
            segments.append((old_scale, seg_start, i - 1))
            seg_start = i
            restored_details.append({
                "ts_code": code, "date": norm[i]["date"],
                "old_af_move": round(r1["af"] / r0["af"], 6),
                "expected_move": round(mv, 6),
            })
        segments.append((scale, seg_start, len(norm) - 1))
        rows_rewritten += _apply_segments_tx(con, code, norm, segments)
    return {
        "stocks": len(to_fix),
        "boundaries": len(restored_details),
        "rows_rewritten": rows_rewritten,
        "details": restored_details,
    }


def rebase_recent(con, days: int = 10) -> Dict[str, Any]:
    """日增钩子：检测最近 days 天内的假跳变 → 对齐涉及股票（含其全史）。

    suspected / unquantified_event 仅计 suspected_pairs，不动源 af。
    """
    since_raw = con.execute(
        "SELECT MAX(date) - INTERVAL (?) DAY FROM kline_daily", [days]).fetchone()[0]
    since = str(since_raw)[:10]
    jumps = find_spurious_jumps(con, since_date=since)
    codes = sorted({j["ts_code"] for j in jumps})
    stats: Dict[str, Any] = {"window_since": since, "jump_pairs": len(jumps),
                             "stocks": len(codes), "rewritten": 0,
                             "suspected_pairs": 0, "stocks_rebased": []}
    for code in codes:
        res = rebase_stock_af(con, code)
        stats["rewritten"] += res["rewritten"]
        stats["suspected_pairs"] += len(res.get("suspected", []))
        if res["jumps"]:
            stats["stocks_rebased"].append(code)
    if stats["stocks_rebased"]:
        log.warning("af_rebase 日增：最近 %d 天 %d 股假基准跳变已对齐（%d 行改写）",
                    days, len(stats["stocks_rebased"]), stats["rewritten"])
    if stats["suspected_pairs"]:
        log.warning("af_rebase 日增：最近 %d 天发现 %d 处疑似大缺口边界（保持源 af 不动）",
                    days, stats["suspected_pairs"])
    return stats


def repair_all_af(con) -> Dict[str, Any]:
    """全市场基准修复：对齐无事件跳变与已知量化除权偏离，绝不对 suspected 盖章。"""
    codes = [r[0] for r in con.execute(
        "SELECT DISTINCT ts_code FROM kline_daily "
        "WHERE adj_factor IS NOT NULL AND adj_factor > 0").fetchall()]
    stats: Dict[str, Any] = {"stocks_scanned": len(codes), "stocks_rebased": 0,
                             "jump_pairs": 0, "suspected_pairs": 0,
                             "rows_rewritten": 0, "sample": [],
                             "suspected_sample": []}
    for i, code in enumerate(codes, 1):
        res = rebase_stock_af(con, code)
        if res["jumps"]:
            stats["stocks_rebased"] += 1
            stats["jump_pairs"] += len(res["jumps"])
            stats["rows_rewritten"] += res["rewritten"]
            if len(stats["sample"]) < 5:
                stats["sample"].append({"ts_code": code, "jumps": res["jumps"][:2]})
        if res.get("suspected"):
            stats["suspected_pairs"] += len(res["suspected"])
            if len(stats["suspected_sample"]) < 5:
                stats["suspected_sample"].append(res["suspected"][0])
        if i % 1000 == 0:
            log.info("af_rebase 进度 %d/%d（已对齐 %d 股）", i, len(codes),
                     stats["stocks_rebased"])
    con.execute("CHECKPOINT")
    log.warning("af_rebase 全市场完成：%d 股中 %d 股对齐，%d 个跳变，%d 行改写，%d 个疑似大缺口保持原状",
                stats["stocks_scanned"], stats["stocks_rebased"],
                stats["jump_pairs"], stats["rows_rewritten"], stats["suspected_pairs"])
    return stats
