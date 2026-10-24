# -*- coding: utf-8 -*-
"""martingale.timing —— 选股时机（v7.0.13，Joel 2026-10-25 拍板）。

概念：在**候选池**（selection.build_candidate_pool，基础 5 条件 ∩ 选股 3 门槛
∪ 手动）之上筛**买入时机**。选股 ≠ 买入——触发后由 Joel 决定是否买入，
买入后录入「我的持仓」由持仓策略（martingale.strategy 参数段）接管档位。

三个条件（TimingParams；页面可开关/调参）：
- ① 价格低位：当前价处于近 price_window_years 年收盘价分位 ≤ price_pctile_max%
  （前复权口径；分位 = 窗口内 ≤ 当前价的交易日占比）；
- ② 股息率触发：最新 TTM 股息率 ≥ yield_min_pct%（候选池成员自带 ttm_yield_pct）；
- ③ 年线下：当前价 < 近 ma_period 日均线（below_ma250 开关，默认关）。

双口径：
- 观察口径（页面「今日选股」）= 已启用条件的 **OR**（in_watchlist）；
- 提醒口径（PushPlus 微信）= ①且②（③启用时叠加）（push_eligible），
  且仅**新触发**推送——边沿状态 data/alerts/timing_push_state.json
  （= 上轮 push_eligible 且已知的代码集；掉出买区后状态清除，
  再次进入 = 新触发可再推。持续在买区不重推。推送失败不落状态 → 下轮重试）。

预计算（每日离线，run_lake_sync_daily.sh 在 selection 之后驱动，亦可用
`python -m martingale timing` 手动触发）：
- refresh_timing(con_write, up, tp)：候选池逐股算 ①②③ → 湖表 selection_timing
  （PK ts_code，整表刷新）；
- check_and_push：读表 + 边沿状态 → PushPlus（唯一外部网络调用，失败记痕不阻断）。
零回测、零行情、零 tushare 配额。
"""
from __future__ import annotations

import dataclasses
import json
import os
import time
from collections import defaultdict
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional

import duckdb

from .config import TimingParams, UniverseParams
from .pushplus import load_token, send as pushplus_send
from .selection import TABLE as _BT_TABLE, Member, build_candidate_pool

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TABLE = "selection_timing"
_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    ts_code          VARCHAR PRIMARY KEY,
    name             VARCHAR,
    as_of_date       DATE NOT NULL,      -- = 最新 K线日（快照基准日）
    close            DOUBLE,             -- 最新收盘（原始价，= 候选池 price_latest）
    pctile_window    DOUBLE,             -- ① 当前价在近 N 年收盘分布的分位（0-100）
    dd_window        DOUBLE,             -- 近 N 年高点回撤（%）
    ttm_yield_pct    DOUBLE,             -- ② 最新 TTM 股息率（%）
    ma_value         DOUBLE,             -- ③ 近 ma_period 日均线（前复权）
    below_ma         BOOLEAN,            -- ③ close < ma（ma 缺失 → NULL）
    cond_price_low   BOOLEAN NOT NULL,   -- ① 触发（数据不足 → FALSE，fail-closed）
    cond_yield       BOOLEAN NOT NULL,   -- ② 触发（股息率缺失 → FALSE）
    cond_below_ma    BOOLEAN,            -- ③ 触发（开关关/数据不足 → NULL）
    in_watchlist     BOOLEAN NOT NULL,   -- 观察口径：已启用条件 OR
    push_eligible    BOOLEAN NOT NULL,   -- 提醒口径：①且②（③启用时叠加）
    reasons          VARCHAR             -- 触发原因（页面展示 + 微信正文）
)
"""

EDGE_STATE_FILENAME = "timing_push_state.json"


def default_edge_state_path() -> str:
    return os.path.join(PROJECT_ROOT, "data", "alerts", EDGE_STATE_FILENAME)


# ---------------------------------------------------------------------------
# 预计算：候选池逐股时机条件 → 湖表
# ---------------------------------------------------------------------------
def ensure_table(con) -> None:
    con.execute(_SCHEMA)


def _years_ago(d: date, years: int) -> date:
    """同月日回溯 N 年（2/29 → 2/28）。与 selection 同语义（窗口对齐）。"""
    try:
        return d.replace(year=d.year - years)
    except ValueError:
        return d.replace(year=d.year - years, day=28)


def refresh_timing(con, up: UniverseParams, tp: TimingParams,
                   manual: Optional[Dict[str, Dict[str, Any]]] = None,
                   members: Optional[List[Member]] = None,
                   verbose: bool = False) -> Dict[str, Any]:
    """候选池逐股算 ①②③ → 写入 selection_timing（整表刷新）。

    :param con: **写**连接（每日作业在湖增量后独占执行；勿在 Web 请求路径调用）。
    :param members: 可注入（测试用）；缺省 build_candidate_pool(con, up, manual)。
    返回 {as_of, n_codes, n_watchlist, n_push, elapsed_s}。
    """
    t0 = time.time()
    ensure_table(con)
    r = con.execute("SELECT MAX(date) FROM kline_daily").fetchone()
    latest = r[0] if r and r[0] is not None else None
    if latest is None:
        raise RuntimeError("kline_daily 为空，无法计算选股时机窗口")

    members = (members if members is not None
               else build_candidate_pool(con, up, manual=manual or {}).members)
    codes = sorted({m.ts_code for m in members})
    by_code = {m.ts_code: m for m in members}

    series: Dict[str, List[float]] = {}
    if codes:
        start = _years_ago(latest, tp.price_window_years)
        fmt = ", ".join(["?"] * len(codes))
        con.execute(
            f"SELECT ts_code, close FROM kline_daily_qfq "
            f"WHERE ts_code IN ({fmt}) AND date >= ? "
            f"ORDER BY ts_code, date",
            [*codes, str(start)])
        for c, cl in con.fetchall():
            if cl is not None:
                series.setdefault(str(c), []).append(float(cl))

    rows: List[tuple] = []
    n_watch = n_push = 0
    for c in codes:
        m = by_code[c]
        ks = series.get(c)
        # ① 价格分位 / 窗口回撤（前复权窗口序列；数据不足 → fail-closed False）
        pctile = dd = None
        if ks and len(ks) >= 2:
            cur_qfq = ks[-1]
            pctile = sum(1 for x in ks if x <= cur_qfq) / len(ks) * 100.0
            dd = (1.0 - cur_qfq / max(ks)) * 100.0
        cond_price_low = (pctile is not None and pctile <= tp.price_pctile_max)
        # ② 股息率（候选池成员自带；None → fail-closed False）
        yld = m.ttm_yield_pct
        cond_yield = (yld is not None and yld >= tp.yield_min_pct)
        # ③ 年线（前复权均线；前复权最新价 = 原始最新价，口径一致）
        ma = below = cond_below = None
        if tp.below_ma250 and ks:
            if len(ks) >= tp.ma_period:
                ma = sum(ks[-tp.ma_period:]) / tp.ma_period
                below = (ks[-1] < ma)
                cond_below = below
            # 数据不足 ma_period → 保持 None（不触发、不显示，fail-closed）
        in_watch = bool(cond_price_low or cond_yield
                        or (cond_below if tp.below_ma250 else False))
        push_ok = bool(cond_price_low and cond_yield
                       and (cond_below if tp.below_ma250 else True))
        if in_watch:
            n_watch += 1
        if push_ok:
            n_push += 1

        parts: List[str] = []
        if cond_price_low:
            parts.append(f"{tp.price_window_years}年价格分位 "
                         f"{pctile:.1f}%（≤{tp.price_pctile_max:g}%）")
        if cond_yield:
            parts.append(f"股息率 {yld:.2f}%（≥{tp.yield_min_pct:g}%）")
        if tp.below_ma250 and cond_below:
            parts.append(f"低于{tp.ma_period}日线")
        rows.append((
            c, m.name, str(latest),
            m.price_latest,
            round(pctile, 2) if pctile is not None else None,
            round(dd, 2) if dd is not None else None,
            round(yld, 4) if yld is not None else None,
            round(ma, 4) if ma is not None else None,
            below,
            cond_price_low, cond_yield, cond_below,
            in_watch, push_ok, "；".join(parts),
        ))

    con.execute(f"DELETE FROM {TABLE}")
    if rows:
        con.executemany(
            f"INSERT INTO {TABLE} VALUES "
            f"(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    out = {"as_of": str(latest), "n_codes": len(codes),
           "n_watchlist": n_watch, "n_push": n_push,
           "elapsed_s": round(time.time() - t0, 2)}
    if verbose:
        print(f"[timing] {out}")
    return out


def load_timing_map(con) -> Dict[str, Dict[str, Any]]:
    """预计算表 → {ts_code: 行}。表不存在/为空 → {}（fail-closed 全不触发）。"""
    try:
        con.execute(f"SELECT * FROM {TABLE}")
    except duckdb.CatalogException:
        return {}
    cols = [d[0] for d in con.description]
    out: Dict[str, Dict[str, Any]] = {}
    for r in con.fetchall():
        row = dict(zip(cols, r))
        out[str(row["ts_code"])] = row
    return out


# ---------------------------------------------------------------------------
# 边沿状态 + PushPlus（新触发才推）
# ---------------------------------------------------------------------------
def load_edge_state(path: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """读边沿状态 → {ts_code: {as_of, at, reasons}}。缺失/损坏 → {}（不阻断）。"""
    p = path or default_edge_state_path()
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        codes = d.get("codes") if isinstance(d, dict) else None
        if not isinstance(codes, dict):
            return {}
        return {str(k): v for k, v in codes.items() if isinstance(v, dict)}
    except (OSError, ValueError):
        return {}


def save_edge_state(codes: Dict[str, Dict[str, Any]],
                    path: Optional[str] = None) -> None:
    p = path or default_edge_state_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "codes": codes,
        }, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)  # 原子替换


def render_timing_message(rows: List[Dict[str, Any]], tp: TimingParams,
                          n_pool: int) -> tuple:
    """推送加消息（title 纯文本 + content HTML，微信服务号原生渲染）。"""
    names = []
    blocks: List[str] = []
    for r in rows:
        name = r.get("name") or r["ts_code"]
        names.append(name)
        bits = []
        if r.get("close") is not None:
            bits.append(f"现价 <b>{r['close']:.2f}</b>")
        if r.get("pctile_window") is not None:
            bits.append(f"{tp.price_window_years}年分位 {r['pctile_window']:.1f}%")
        if r.get("dd_window") is not None:
            bits.append(f"回撤 {r['dd_window']:.1f}%")
        if r.get("ttm_yield_pct") is not None:
            bits.append(f"股息率 {r['ttm_yield_pct']:.2f}%")
        blocks.append(
            f"<p><b>{name}（{r['ts_code']}）</b> 新进入买入区</p>"
            f"<p>{' ｜ '.join(bits)}</p>"
            f"<p>触发：{r.get('reasons') or '-'}</p><hr/>")
    title = ("选股提醒：" + "、".join(names[:3]) +
             (" 等" if len(names) > 3 else "") +
             f" 新进入买入区（{len(rows)} 只）")
    footer = (f"<p style='color:#888;font-size:12px'>选股策略（候选池 {n_pool} 只）｜ "
              f"as_of {rows[0].get('as_of_date')} ｜ 仅新触发推送 ｜ "
              f"决定买入请录入「我的持仓」由持仓策略接管</p>")
    return title, "".join(blocks) + footer


def check_and_push(con, tp: TimingParams,
                   state_path: Optional[str] = None,
                   send_fn: Optional[Callable[[str, str, str], None]] = None,
                   token: Optional[str] = None,
                   dry_run: bool = False,
                   verbose: bool = False) -> Dict[str, Any]:
    """读 selection_timing + 边沿状态 → 新触发推送 PushPlus。

    :param send_fn: 可注入（测试用）；缺省 martingale.pushplus.send。
    :param token: 可注入；缺省 load_token()（.env PUSHPLUS_TOKEN）。
    状态语义：state = 上轮 push_eligible 的代码集。新触发 = 本轮 eligible
    且不在 state。掉出买区 → 从 state 清除（再进入 = 新触发）。
    推送失败 → 不落状态 → 下轮重试（与 position_alert 同一纪律）。
    返回 {n_watchlist, n_eligible, n_new, pushed, dry_run, error}。
    """
    tmap = load_timing_map(con)
    eligible = [r for r in tmap.values() if r.get("push_eligible")]
    watch = [r for r in tmap.values() if r.get("in_watchlist")]
    state = load_edge_state(state_path)
    new = [r for r in eligible if r["ts_code"] not in state]
    res = {"n_watchlist": len(watch), "n_eligible": len(eligible),
           "n_new": len(new), "pushed": False, "dry_run": dry_run,
           "error": None}
    if verbose:
        print(f"[timing] watchlist={len(watch)} eligible={len(eligible)} "
              f"new={len(new)}")

    def _rewrite_state() -> None:
        # state ← 本轮 eligible 全集（含已推/已知）：掉出者清除、再进入可重推
        save_edge_state({
            r["ts_code"]: {"as_of": str(r.get("as_of_date")),
                           "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                           "reasons": r.get("reasons") or ""}
            for r in eligible
        }, state_path)

    if not new:
        if not dry_run:
            _rewrite_state()  # 同步清除掉出者（即使无新触发）
        return res

    if dry_run:
        for r in new:
            print(f"[timing][dry-run] 新触发: {r.get('name')}（{r['ts_code']}） "
                  f"{r.get('reasons')}")
        return res

    send = send_fn or pushplus_send
    tk = token if token is not None else load_token()
    if tk is None:
        res["error"] = "PUSHPLUS_TOKEN 缺失（.env 未配置；不落状态，配置后下轮重推）"
        return res
    title, content = render_timing_message(new, tp, len(watch) or len(tmap))
    try:
        send(tk, title, content)
    except Exception as exc:  # noqa: BLE001 - 推送失败不落状态 → 下轮重试
        res["error"] = f"pushplus 推送失败: {exc}"
        return res
    res["pushed"] = True
    _rewrite_state()
    return res
