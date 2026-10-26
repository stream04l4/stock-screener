"""lake.af_rebase —— adj_factor 基准对齐（v7.0.16）。

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
                      tol: float = DEFAULT_TOL) -> Tuple[bool, str]:
    """判定边界 (d0→d1) 是否假基准跳变。

    :return: (is_spurious, kind)，kind ∈ {"no_event","event","suspected"}。
    """
    af_move = af1 / af0
    price_ratio = close0 / close1
    if has_known_event:
        kind = "event"
        spurious = abs(math.log(af_move / price_ratio)) > tol
    elif price_ratio >= LARGE_DROP_RATIO:
        kind = "suspected"
        spurious = (abs(math.log(af_move / price_ratio)) > tol
                    and abs(math.log(af_move)) > tol)
    else:
        kind = "no_event"
        spurious = abs(math.log(af_move)) > tol
    return spurious, kind


def rebase_stock_af(con, ts_code: str,
                    tol: float = DEFAULT_TOL) -> Dict[str, Any]:
    """单股 af 基准对齐：检测全部假跳变并把各段 af 换算到最早段基准后写回。

    幂等：已对齐的股票（无假跳变）零写入。
    :param con: 可写 DuckDB 连接（生产湖或测试合成库）。
    """
    rows = con.execute(
        "SELECT date, close, adj_factor FROM kline_daily "
        "WHERE ts_code=? AND adj_factor IS NOT NULL AND adj_factor > 0 "
        "ORDER BY date", [ts_code]).fetchall()
    if len(rows) < 2:
        return {"ts_code": ts_code, "rows": len(rows), "jumps": [], "rewritten": 0}
    events: Set[str] = {str(d)[:10] for (d,) in con.execute(
        "SELECT DISTINCT ex_date FROM dividend_events WHERE ts_code=?",
        [ts_code]).fetchall()}
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
        spurious, kind = classify_boundary(r0["af"], r1["af"], r0["close"],
                                           r1["close"], r1["date"] in events, tol)
        if not spurious:
            continue
        old_scale = scale
        if kind == "no_event":
            scale = scale * r0["af"] / r1["af"]
        else:  # event / suspected：真实事件已被价格吸收（见模块 docstring）
            scale = scale * (r0["close"] * r0["af"]) / (r1["close"] * r1["af"])
        jumps.append({"date": r1["date"], "kind": kind,
                      "af_ratio": round(r1["af"] / r0["af"], 6),
                      "scale_before": round(old_scale, 6),
                      "scale_after": round(scale, 6)})
        segments.append((old_scale, seg_start, i - 1))
        seg_start = i
    segments.append((scale, seg_start, len(norm) - 1))

    rewritten = 0
    for s_scale, i0, i1 in segments:
        if abs(s_scale - 1.0) < 1e-9:
            continue
        # 段内一次性乘换算（set-based；段内可能夹 NULL-af 行 → NULL×s 仍 NULL，安全）
        n = con.execute(
            "UPDATE kline_daily SET adj_factor = adj_factor * ? "
            "WHERE ts_code=? AND date BETWEEN ? AND ? "
            "AND adj_factor IS NOT NULL RETURNING adj_factor",
            [s_scale, ts_code, norm[i0]["date"], norm[i1]["date"]]).fetchall()
        rewritten += len(n)
    if jumps:
        log.warning("af_rebase %s：%d 个假基准跳变已对齐（%d 行改写，示例 %s）",
                    ts_code, len(jumps), rewritten, jumps[:3])
    return {"ts_code": ts_code, "rows": len(norm), "jumps": jumps,
            "rewritten": rewritten}


def find_spurious_jumps(con, since_date: Optional[str] = None,
                        max_pairs: int = 300000) -> List[Dict[str, Any]]:
    """全市场扫描假基准跳变（SQL 取相邻对 + 同一判定函数，纯湖内零网络）。

    :param since_date: 只报告 date>=since_date 的跳变对（None=全史）。
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
              WINDOW w AS (PARTITION BY ts_code ORDER BY date)) t
        WHERE t.d0 IS NOT NULL
          AND t.af1 IS NOT NULL AND t.af1 > 0
          AND t.af0 IS NOT NULL AND t.af0 > 0
          AND t.c1 IS NOT NULL AND t.c1 > 0
          AND t.c0 IS NOT NULL AND t.c0 > 0
          {where}
        LIMIT {int(max_pairs)}
    """, params).fetchall()
    events_by_code: Dict[str, Set[str]] = {}
    for code, d in con.execute(
            "SELECT ts_code, ex_date FROM dividend_events").fetchall():
        events_by_code.setdefault(code, set()).add(str(d)[:10])
    out: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for code, d0, d1, c0, c1, af0, af1 in pairs:
        spurious, kind = classify_boundary(
            af0, af1, c0, c1,
            str(d1)[:10] in events_by_code.get(code, ()))
        if spurious:
            seen.add(code)
            out.append({"ts_code": code, "date": str(d1)[:10], "kind": kind,
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
