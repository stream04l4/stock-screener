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
- 已知事件（dividend_events 有 ex_date=d1 行）：期望 af_move=price_ratio
  → 假跳变 iff |log(af_move/price_ratio)| > tol
- 无已知事件但大缺口（疑似送转/配股，分红表仅现金）：双假设都须偏离
  → 假跳变 iff 两项均 > tol（残余误报风险：换源日恰逢真实大缺口，已记录）

**换算系数（统一公式，真实事件已被价格吸收）**：
- 事件日（已知或疑似）：s_new = s_prev × close(d0)×af(d0) / (close(d1)×af(d1))
- 无事件日：            s_new = s_prev × af(d0) / af(d1)
换算后 canonical hfq = close×af×s 在边界处连续（与 hfq 定义一致）。
幂等：修复后边界 af_move 回到期望值 → 重跑不再触发。
"""

from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional, Set, Tuple

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
    "find_spurious_jumps",
]


def classify_boundary(af0: float, af1: float, close0: float, close1: float,
                      has_known_event: bool,
                      tol: float = DEFAULT_TOL,
                      expected_move: Optional[float] = None) -> Tuple[bool, str]:
    """判定边界 (d0→d1) 是否假基准跳变。

    :param expected_move: 已知除权事件的理论官方复权步长（c0*(1+bonus)/(c0-cash)）；
        提供时用它替代含当日市场涨跌幅的 close0/close1，避免除权日正常涨跌 >2%
        被误杀抹平。缺省 None 回退旧 price_ratio 判定（兼容无金额事件与旧调用方）。
    :return: (is_spurious, kind)，kind ∈ {"no_event","event","suspected"}。
    """
    af_move = af1 / af0
    price_ratio = close0 / close1
    if has_known_event:
        kind = "event"
        ref_move = expected_move if (expected_move is not None and expected_move > 0) else price_ratio
        spurious = abs(math.log(af_move / ref_move)) > tol
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


def rebase_stock_af(con, ts_code: str,
                    tol: float = DEFAULT_TOL) -> Dict[str, Any]:
    """单股 af 基准对齐：检测全部假跳变并把各段 af 换算到最早段基准后写回。

    幂等：已对齐的股票（无假跳变）零写入；多段写回包裹在同一事务内防半写。
    :param con: 可写 DuckDB 连接（生产湖或测试合成库）。
    """
    rows = con.execute(
        "SELECT date, close, adj_factor FROM kline_daily "
        "WHERE ts_code=? AND adj_factor IS NOT NULL AND adj_factor > 0 "
        "ORDER BY date", [ts_code]).fetchall()
    if len(rows) < 2:
        return {"ts_code": ts_code, "rows": len(rows), "jumps": [], "rewritten": 0}
    sorted_events = _load_div_events(con, ts_code=ts_code).get(ts_code, [])
    norm = [{"date": str(d)[:10], "close": c, "af": a} for d, c, a in rows]

    # 逐边界判定 + 累积段换算系数（scale 从 1 起，每遇假跳变乘一次修正）
    segments: List[Tuple[float, int, int]] = []   # (scale, first_idx, last_idx)
    jumps: List[Dict[str, Any]] = []
    scale = 1.0
    seg_start = 0
    for i in range(1, len(norm)):
        r0, r1 = norm[i - 1], norm[i]
        if None in (r0["close"], r1["close"]) or r0["close"] <= 0 or r1["close"] <= 0:
            continue  # 缺值对：不可判定，保持同段
        interval_evs = _events_in_interval(sorted_events, r0["date"], r1["date"])
        has_ev = bool(interval_evs)
        exp_move = _expected_event_move(r0["close"], interval_evs) if has_ev else None
        spurious, kind = classify_boundary(
            r0["af"], r1["af"], r0["close"], r1["close"],
            has_ev, tol, expected_move=exp_move)
        if not spurious:
            continue
        old_scale = scale
        if kind == "no_event":
            scale = scale * r0["af"] / r1["af"]
        elif exp_move is not None and exp_move > 0:
            # 已知除权事件：对齐到官方理论步长，保留除权日真实市场涨跌幅
            scale = scale * (r0["af"] * exp_move) / r1["af"]
        else:  # suspected 或无金额占位事件：回退价格吸收公式
            scale = scale * (r0["close"] * r0["af"]) / (r1["close"] * r1["af"])
        jumps.append({"date": r1["date"], "kind": kind,
                      "af_ratio": round(r1["af"] / r0["af"], 6),
                      "scale_before": round(old_scale, 6),
                      "scale_after": round(scale, 6)})
        segments.append((old_scale, seg_start, i - 1))
        seg_start = i
    segments.append((scale, seg_start, len(norm) - 1))

    to_write = [(s, i0, i1) for s, i0, i1 in segments if abs(s - 1.0) >= 1e-9]
    rewritten = 0
    if to_write:
        in_tx = False
        try:
            con.execute("BEGIN TRANSACTION")
            in_tx = True
        except Exception:  # noqa: BLE001 - 已在外部事务内
            in_tx = False
        try:
            for s_scale, i0, i1 in to_write:
                # 段内一次性乘换算（set-based；段内可能夹 NULL-af 行 → NULL×s 仍 NULL，安全）
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
    if jumps:
        log.warning("af_rebase %s：%d 个假基准跳变已对齐（%d 行改写，示例 %s）",
                    ts_code, len(jumps), rewritten, jumps[:3])
    return {"ts_code": ts_code, "rows": len(norm), "jumps": jumps,
            "rewritten": rewritten}


def find_spurious_jumps(con, since_date: Optional[str] = None,
                        max_pairs: int = 300000) -> List[Dict[str, Any]]:
    """全市场扫描假基准跳变（SQL 取相邻对 + 同一判定函数，纯湖内零网络）。

    :param since_date: 只报告 date>=since_date 的跳变对（None=全史）。

    **NULL 盲点修复（2026-11-20 山煤国际事故）**：先在 af>0 行内做 LAG——
    af=NULL 的行（批量 af 失败日，如 2026-11-13 全市场 NULL）不参与配对，
    否则 d0→d1 的假跳变被 NULL 行隔断后对检测器不可见（LAG 只看到
    NULL 邻居 → 整对过滤），rebase_recent 日增护栏静默失效。
    """
    where, params = "", []
    if since_date is not None:
        where, params = "AND t.d1 >= ?", [since_date]
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
        WHERE t.d0 IS NOT NULL
          {where}
        ORDER BY t.ts_code, t.d1
        LIMIT {int(max_pairs)}
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
            af0, af1, c0, c1, has_ev, expected_move=exp_move)
        if spurious:
            seen.add(code)
            out.append({"ts_code": code, "date": d1s, "kind": kind,
                        "af_ratio": round(af1 / af0, 6)})
    log.info("af_rebase 扫描：相邻对 %d，假基准跳变 %d（涉及 %d 股）",
             len(pairs), len(out), len(seen))
    return out


def rebase_recent(con, days: int = 10) -> Dict[str, Any]:
    """日增钩子：检测最近 days 天内的假跳变 → 对齐涉及股票（含其全史）。

    失败语义：异常向上抛由调用方记痕（不静默）；正常路径零网络纯湖内计算。
    """
    since_raw = con.execute(
        "SELECT MAX(date) - INTERVAL (?) DAY FROM kline_daily", [days]).fetchone()[0]
    since = str(since_raw)[:10]
    jumps = find_spurious_jumps(con, since_date=since)
    codes = sorted({j["ts_code"] for j in jumps})
    stats: Dict[str, Any] = {"window_since": since, "jump_pairs": len(jumps),
                             "stocks": len(codes), "rewritten": 0,
                             "stocks_rebased": []}
    for code in codes:
        res = rebase_stock_af(con, code)
        stats["rewritten"] += res["rewritten"]
        if res["jumps"]:
            stats["stocks_rebased"].append(code)
    if codes:
        log.warning("af_rebase 日增：最近 %d 天 %d 股存在假基准跳变 → 已对齐（%d 行改写）",
                    days, len(codes), stats["rewritten"])
    return stats


def repair_all_af(con) -> Dict[str, Any]:
    """一次性全市场修复（v7.0.16 上线时执行；此后由 rebase_recent 日增维持）。"""
    codes = [r[0] for r in con.execute(
        "SELECT DISTINCT ts_code FROM kline_daily "
        "WHERE adj_factor IS NOT NULL AND adj_factor > 0").fetchall()]
    stats: Dict[str, Any] = {"stocks_scanned": len(codes), "stocks_rebased": 0,
                             "jump_pairs": 0, "rows_rewritten": 0, "sample": []}
    for i, code in enumerate(codes, 1):
        res = rebase_stock_af(con, code)
        if res["jumps"]:
            stats["stocks_rebased"] += 1
            stats["jump_pairs"] += len(res["jumps"])
            stats["rows_rewritten"] += res["rewritten"]
            if len(stats["sample"]) < 5:
                stats["sample"].append({"ts_code": code, "jumps": res["jumps"][:2]})
        if i % 1000 == 0:
            log.info("af_rebase 进度 %d/%d（已对齐 %d 股）", i, len(codes),
                     stats["stocks_rebased"])
    con.execute("CHECKPOINT")
    log.warning("af_rebase 全市场完成：%d 股中 %d 股对齐，%d 个跳变，%d 行改写",
                stats["stocks_scanned"], stats["stocks_rebased"],
                stats["jump_pairs"], stats["rows_rewritten"])
    return stats
