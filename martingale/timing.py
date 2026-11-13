# -*- coding: utf-8 -*-
"""martingale.timing —— 选股时机（v7.0.13，Joel 2026-10-25 拍板）。

概念：在**候选池**（selection.build_candidate_pool，基础 5 条件 ∩ 选股 3 门槛
∪ 手动）之上筛**买入时机**。选股 ≠ 买入——触发后由 Joel 决定是否买入，
买入后录入「我的持仓」由持仓策略（martingale.strategy 参数段）接管档位。

条件项（TimingParams；页面可调参）：
- ① 价格低位：当前价处于近 price_window_years 年收盘价分位 ≤ price_pctile_max%
  （前复权口径；分位 = 窗口内 ≤ 当前价的交易日占比）——**时机门唯一条件**。
- ② 股息率 / ③ 250 日线：**v7.0.26 彻底删除**（不再保留参考展示）。

**时机门 = ① 单条件（v7.0.24，Joel 2026-11-09 定案）**：10 年回测（2016-01→2026-11，
池 62 只，生产状态机逐笔复刻，48+106 组参数面）结论——② 是总盈利主要拖累
（y=4.5 较 y=0 少 38~45%，且与池入场 G8"当前息率≥3%"同维冗余）、③ 年线近乎
无效（±2%）、① 与 ② 同属"便宜程度"维度高度冗余 → v7.0.24 先降参考展示，
**v7.0.26（Joel 定案）连参考展示一并删除**；门 = 5 年分位 ≤ 40%。

**首仓建仓回撤归入选股策略（v7.0.26，Joel 定案）**：first_drawdown_pct 自
strategy 段移入 timing 段管理（首仓线 = 本轮高点×(1−该值)，属首仓买入时机；
持仓策略段只保留加仓/再武装参数）。引擎数学零改动（经 StrategyParams 读取）。

**推送纪律（v7.0.25，Joel 2026-11-09 定案）**：微信**不再**为时机独立推送
（"时机成熟（首仓待价）"通道已退役——只有时机没到价无意义）。微信统一为
**进入当前信号才推**（盘中「策略点位提醒」：首仓 = 时机✓ 且 价破首仓线；
加仓 = 价破档线）。本模块只做**预计算 + 页面参考展示**：
- 页面「今日选股」= ① 命中（in_watchlist；push_eligible 列保留 = ①，同值）；
- 破线后的买入信号 / 推送由 scripts/position_alert.py 首仓通道负责。

预计算（每日离线，run_lake_sync_daily.sh 在 selection 之后驱动，亦可用
`python -m martingale timing` 手动触发）：
- refresh_timing(con_write, up, tp)：候选池逐股算 ① → 湖表 selection_timing
  （PK ts_code，整表刷新）。零回测、零行情、零 tushare 配额、零推送。
"""
from __future__ import annotations

import os
import time
from datetime import date
from typing import Any, Dict, List, Optional

import duckdb

from .config import TimingParams, UniverseParams
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
    cond_price_low   BOOLEAN NOT NULL,   -- ① 触发（数据不足 → FALSE，fail-closed）
    in_watchlist     BOOLEAN NOT NULL,   -- 观察口径：①（v7.0.24 单条件）
    push_eligible    BOOLEAN NOT NULL,   -- = ①（与 in_watchlist 同值；v7.0.25 起无独立推送，仅页面徽标）
    reasons          VARCHAR             -- 触发原因（页面展示）
)
"""


# ---------------------------------------------------------------------------
# 预计算：候选池逐股时机条件 → 湖表
# ---------------------------------------------------------------------------
def ensure_table(con) -> None:
    # v7.0.26：schema 收缩（② 股息率 / ③ 年线列删除）→ 旧表须整表重建，
    # 仅 CREATE IF NOT EXISTS 会残留旧列；本表每次全量刷新，drop 安全。
    con.execute(f"DROP TABLE IF EXISTS {TABLE}")
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
    """候选池逐股算 ①（时机门唯一条件）→ 写入 selection_timing（整表刷新）。

    v7.0.26：② 股息率 / ③ 250 日线计算与数据列一并删除（回测定案无用，
    不留观察项）；分位 / 窗口回撤保留（页面展示 = 时机门本体）。

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
        # v7.0.24：时机门 = ① 单条件（②③ 参考展示）；v7.0.26：②③ 彻底删除
        in_watch = bool(cond_price_low)
        push_ok = bool(cond_price_low)
        if in_watch:
            n_watch += 1
        if push_ok:
            n_push += 1

        parts: List[str] = []
        if cond_price_low:
            parts.append(f"{tp.price_window_years}年价格分位 "
                         f"{pctile:.1f}%（≤{tp.price_pctile_max:g}%）")
        rows.append((
            c, m.name, str(latest),
            m.price_latest,
            round(pctile, 2) if pctile is not None else None,
            round(dd, 2) if dd is not None else None,
            cond_price_low,
            in_watch, push_ok, "；".join(parts),
        ))

    con.execute(f"DELETE FROM {TABLE}")
    if rows:
        con.executemany(
            f"INSERT INTO {TABLE} VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    out = {"as_of": str(latest), "n_codes": len(codes),
           "n_watchlist": n_watch, "n_push": n_push,
           "elapsed_s": round(time.time() - t0, 2)}
    if verbose:
        print(f"[timing] {out}")
    return out


def live_pctile_map(con, codes: List[str], prices: Dict[str, Optional[float]],
                    tp, now=None) -> Dict[str, Optional[float]]:
    """实时分位（v7.0.31）：当前价（前复权折算）在 5 年前复权收盘分布中的秩
    = #{窗口收盘 ≤ 当前价} / 窗口长度 ×100（v8.1.3 审计 F1：docstring 原
    「替换窗口末值」为不精确表述——实现是标准秩口径，参考分布不含被检点，
    下行日较「替换末值」语义低 1/W，仅微信推送通道，量级 ~0.08pp@W≈1250）。

    微信推送盘中判定用（Joel 2026-11-10 定案 A 案）：refresh_timing 的盘后
    收盘快照滞后一天——「价格破线 + 分位破 40%」同日达成时，快照只在当晚
    刷新、推送窗口（15:05 止）早已关闭 → 结构性漏推（山煤国际 2026-11-10
    实案：前夜 42.00% → 当日 37.95%，价全天在首仓线下）。窗口与
    refresh_timing 同一口径（kline_daily_qfq、近 N 年、截至湖最新 bar）；
    现价（原始价）按该股**自身末 bar** 因子折前复权（qfq_now = price ×
    last_qfq/last_raw；非事件日该因子=末 bar 当日 raw→qfq 换算，误差 ~bp 级）。
    v7.0.32 除权日修正（now=参照日）：除权日盘中末 bar 是事件前 K 线，
    前复权窗口停在事件前口径，现价（事件后）须乘 pending 除权累计复权比
    （engine.pending_exrights_factor，与触发价修正同一因子）对齐窗口口径，
    否则分位被除权缺口系统性压低（假"时机成熟"）。now=None → 零修正。

    :param prices: {code: 当前原始价（spot；无行情时传湖最后收盘）}；
      缺/None/≤0 → 该股 None。
    :return: {code: 分位(0-100) | None}；无 qfq 序列/bar 不足/无因子 →
      None（fail-closed，调用方按「时机未过」处理）。
    """
    if not codes:
        return {}
    latest = con.execute("SELECT MAX(date) FROM kline_daily").fetchone()[0]
    if latest is None:
        return {c: None for c in codes}
    start = str(_years_ago(latest, tp.price_window_years))
    fmt = ", ".join(["?"] * len(codes))
    con.execute(
        f"SELECT ts_code, close FROM kline_daily_qfq "
        f"WHERE ts_code IN ({fmt}) AND date >= ? ORDER BY ts_code, date",
        [*codes, start])
    series: Dict[str, List[float]] = {}
    for c, cl in con.fetchall():
        if cl is not None:
            series.setdefault(str(c), []).append(float(cl))
    # 各股自身末 bar 的 raw→qfq 因子
    con.execute(
        f"SELECT k.ts_code, k.close AS raw, h.close AS qfq "
        f"FROM kline_daily k "
        f"JOIN kline_daily_qfq h USING (ts_code, date) "
        f"JOIN (SELECT ts_code, MAX(date) AS md FROM kline_daily "
        f"      WHERE ts_code IN ({fmt}) GROUP BY ts_code) m "
        f"  ON m.ts_code = k.ts_code AND m.md = k.date "
        f"WHERE k.ts_code IN ({fmt}) AND k.close > 0",
        list(codes) + list(codes))
    fac: Dict[str, float] = {}
    for c, raw, qfq in con.fetchall():
        if raw and qfq:
            fac[str(c)] = float(qfq) / float(raw)
    # v7.0.32 除权日修正：现价 × pending 事件累计复权比（对齐事件前窗口口径）
    from .engine import pending_exrights_factor
    rebase = pending_exrights_factor(con, codes, now) if now is not None else {}
    out: Dict[str, Optional[float]] = {}
    for c in codes:
        ks = series.get(str(c))
        p = prices.get(c)
        f = fac.get(str(c))
        if not ks or len(ks) < 2 or p is None or p <= 0 or not f:
            out[c] = None
            continue
        cur = float(p) * f * rebase.get(str(c), 1.0)
        out[c] = sum(1 for x in ks if x <= cur) / len(ks) * 100.0
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
