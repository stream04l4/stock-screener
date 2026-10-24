# -*- coding: utf-8 -*-
"""martingale.selection —— 候选池选股（v7.0.12，Joel 2026-10-24 拍板）。

概念：股票池 = **候选池**。入选 ≠ 买入信号——买不买看价格是否到策略触发线
（Web「我的持仓」页信号 / position_alert 微信提醒，口径不变）。

漏斗（在 universe.build_pool 基础 5 条件 ①~⑤ 之上）：
- G7 十年回测必须为正（backtest_positive）：n_buys ≥ 1 且 total_pnl > 0
  （净利含现金分红、扣 3bps 佣金；窗口 = 最新 K线日回溯 backtest_window_years 年，
  滚动；参数 = 当前生产策略参数）。**fail-closed**：无预计算行的代码不入选
  （预计算缺失时宁缺毋滥，Web 侧以 funnel.backtest 计数暴露异常）。
- G8 TTM 股息率 ≥ min_ttm_yield_pct（valuation_daily 每只最新一行）。
- G9 最新收盘 ∈ [price_min_yuan, price_max_yuan]。
- 波动率门槛：经讨论**明确不加**（2026-10-24）。

手动添加（data/manual_pool.json，原子写）：绕过 G7~G9 恒在候选池；
入口 = Web POST/DELETE /api/pool/manual。

预计算（每日离线，run_lake_sync_daily.sh 在湖增量后驱动，亦可用
`python -m martingale selection` 手动触发）：
- refresh_selection(con_write, sp, up)：基础池每只跑十年滚动回测 → 湖表
  selection_backtest_10y（PK ts_code，整表刷新为最新快照）；
- build_candidate_pool 读表 O(1)，请求路径零回测计算。
"""
from __future__ import annotations

import dataclasses
import json
import math
import os
import re
import time
from datetime import date, datetime
from typing import Any, Dict, List, Optional

import duckdb

from .config import StrategyParams, UniverseParams
from .engine import load, run_stock
from .universe import Member, PoolResult, build_pool

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TABLE = "selection_backtest_10y"
_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    ts_code        VARCHAR PRIMARY KEY,
    as_of_date     DATE NOT NULL,      -- = 最新 K线日（快照基准日）
    start_date     DATE NOT NULL,
    end_date       DATE NOT NULL,
    n_bars         INTEGER,
    n_buys         INTEGER,
    total_invested DOUBLE,
    final_assets   DOUBLE,
    dividends      DOUBLE,
    total_pnl      DOUBLE,
    pnl_pct        DOUBLE,             -- 未投入（从未触发）→ NULL
    min_unreal_pct DOUBLE,
    last_status    VARCHAR             -- never | open | closed
)
"""

MANUAL_POOL_FILENAME = "manual_pool.json"

_CODE_RE = re.compile(r"^(?:sh|sz|bj)\.(\d{6})$")
_CODE_NODOT_RE = re.compile(r"^(sh|sz|bj)(\d{6})$")
_BARE_RE = re.compile(r"^(\d{6})$")


# ---------------------------------------------------------------------------
# 预计算：十年滚动回测 → 湖表
# ---------------------------------------------------------------------------
def ensure_table(con) -> None:
    con.execute(_SCHEMA)


def _latest_kline_date(con) -> Optional[date]:
    r = con.execute("SELECT MAX(date) FROM kline_daily").fetchone()
    return r[0] if r and r[0] is not None else None


def _years_ago(d: date, years: int) -> date:
    """同月日回溯 N 年（2/29 → 2/28）。"""
    try:
        return d.replace(year=d.year - years)
    except ValueError:
        return d.replace(year=d.year - years, day=28)


def _nan_to_none(v: Any) -> Optional[float]:
    if v is None:
        return None
    f = float(v)
    return None if math.isnan(f) else f


def refresh_selection(con, sp: StrategyParams, up: UniverseParams,
                      base_codes: Optional[List[str]] = None,
                      verbose: bool = False) -> Dict[str, Any]:
    """基础池逐股十年滚动回测 → 写入 selection_backtest_10y（整表刷新）。

    :param con: **写**连接（每日作业在湖增量后独占执行；勿在 Web 请求路径调用）。
    :param base_codes: 缺省 = build_pool 基础池（①~⑤）。
    返回 {as_of, start, n_codes, n_written, n_positive, elapsed_s}。
    """
    t0 = time.time()
    ensure_table(con)
    latest = _latest_kline_date(con)
    if latest is None:
        raise RuntimeError("kline_daily 为空，无法计算选股回测窗口")
    start = _years_ago(latest, up.backtest_window_years)
    if base_codes is None:
        base_codes = build_pool(con, up).codes
    base_codes = sorted(base_codes)

    names, data, divs = load(con, codes=base_codes,
                             start=str(start), end=str(latest))
    rows: List[tuple] = []
    n_pos = 0
    for c in base_codes:
        series = data.get(c)
        if not series or len(series[0]) < 2:
            continue  # 无足够数据 → 不写行（G7 fail-closed 不入选）
        dates, sig, exec_c = series
        stat, _state, _v, _cst, _f, _buys = run_stock(
            c, dates, sig, exec_c, divs.get(c), sp, verbose=verbose)
        pnl = _nan_to_none(stat["total_pnl"])
        pct = _nan_to_none(stat["pnl_pct"])
        n_buys = int(stat["n_buys"] or 0)
        if n_buys >= 1 and (pnl or 0.0) > 0.0:
            n_pos += 1
        rows.append((c, str(latest), str(start), str(dates[-1])[:10],
                     int(stat["n_bars"]), n_buys,
                     float(stat["total_invested"] or 0.0),
                     float(stat["final_assets"] or 0.0),
                     float(stat["dividends"] or 0.0),
                     pnl, pct,
                     float(stat["min_unreal_pct"] or 0.0),
                     str(stat["last_status"])))

    con.execute(f"DELETE FROM {TABLE}")
    if rows:
        con.executemany(
            f"INSERT INTO {TABLE} VALUES "
            f"(?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    out = {
        "as_of": str(latest), "start": str(start),
        "n_codes": len(base_codes), "n_written": len(rows),
        "n_positive": n_pos, "elapsed_s": round(time.time() - t0, 2),
    }
    if verbose:
        print(f"[selection] {out}")
    return out


def load_backtest_map(con) -> Dict[str, Dict[str, Any]]:
    """预计算表 → {ts_code: 行}。表不存在/为空 → {}（G7 全 fail-closed）。"""
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
# 候选池构建（请求路径；G7 读预计算表 O(1)）
# ---------------------------------------------------------------------------
def build_candidate_pool(con, up: UniverseParams,
                         manual: Optional[Dict[str, Dict[str, Any]]] = None,
                         base: Optional[PoolResult] = None) -> PoolResult:
    """候选池 = 基础漏斗 ∩ G7 ∩ G8 ∩ G9 ∪ 手动添加。

    :param manual: {ts_code: {"name","added_at"}}（Web 层加载 manual_pool.json 传入）。
    :param base:   可注入（测试用）；缺省 build_pool(con, up)。
    返回 PoolResult：funnel 扩展键 backtest/yield/price/final/manual；
    Member 扩展字段（bt_pnl_pct/bt_n_buys/price_latest/is_manual）填充。
    """
    manual = manual or {}
    base = base or build_pool(con, up)

    # G9：最新收盘价（每只一行）
    con.execute(
        "SELECT ts_code, close FROM kline_daily "
        "WHERE date = (SELECT MAX(date) FROM kline_daily)")
    price_map: Dict[str, float] = {}
    for c, cl in con.fetchall():
        if cl is not None:
            price_map[str(c)] = float(cl)

    bt = load_backtest_map(con) if up.backtest_positive else {}

    n_g7 = n_g8 = 0
    auto: List[Member] = []
    for m in base.members:
        b = bt.get(m.ts_code)
        g7 = (not up.backtest_positive) or (
            b is not None and int(b["n_buys"] or 0) >= 1
            and float(b["total_pnl"] or 0.0) > 0.0)
        if not g7:
            continue
        n_g7 += 1
        yld = m.ttm_yield_pct
        if yld is None or yld < up.min_ttm_yield_pct:
            continue
        n_g8 += 1
        px = price_map.get(m.ts_code)
        if px is None or not (up.price_min_yuan <= px <= up.price_max_yuan):
            continue
        auto.append(dataclasses.replace(
            m,
            bt_pnl_pct=(float(b["pnl_pct"]) if b is not None
                        and b.get("pnl_pct") is not None else None),
            bt_n_buys=int(b["n_buys"] or 0) if b is not None else None,
            price_latest=px,
            is_manual=False))

    # 手动添加：绕过 G7~G9；只保留 stock_master 中存活（未退市）的代码
    manual_members: List[Member] = []
    manual_codes = sorted(set(manual))
    if manual_codes:
        fmt = ", ".join(["?"] * len(manual_codes))
        con.execute(
            f"SELECT sm.ts_code, sm.name, sm.industry_csric2, sm.list_date, "
            f"        sm.soe_basis, sm.delist_date "
            f"FROM stock_master sm WHERE sm.ts_code IN ({fmt})",
            manual_codes)
        master = {r[0]: r for r in con.fetchall() if r[5] is None}
        if len(master) != len(manual_codes):
            stale = [c for c in manual_codes if c not in master]
            # 失效手动项静默丢弃（页面刷新后不再展示；文件由下次写回时清理）
            for c in stale:
                manual.pop(c, None)
        # 手动股的展示指标（市值/股息率/上市天数）一次批量取
        mv_map, ttm_map, bars_map = {}, {}, {}
        if master:
            fmt2 = ", ".join(["?"] * len(master))
            con.execute(
                f"SELECT ts_code, total_mv FROM valuation_daily WHERE "
                f"date = (SELECT MAX(date) FROM valuation_daily) AND ts_code IN ({fmt2})",
                list(master))
            mv_map = dict(con.fetchall())
            con.execute(
                f"SELECT v.ts_code, v.ttm_yield_pct FROM valuation_daily v "
                f"JOIN (SELECT ts_code, MAX(date) md FROM valuation_daily "
                f"      WHERE ts_code IN ({fmt2}) GROUP BY ts_code) x "
                f"  ON v.ts_code = x.ts_code AND v.date = x.md",
                list(master))
            ttm_map = dict(con.fetchall())
            con.execute(
                f"SELECT ts_code, COUNT(*) FROM kline_daily "
                f"WHERE ts_code IN ({fmt2}) GROUP BY ts_code", list(master))
            bars_map = dict(con.fetchall())

        for c in manual_codes:
            if c not in master:
                continue
            _c, name, industry, list_date, soe_basis, _dl = master[c]
            b = bt.get(c)
            tv = mv_map.get(c)
            manual_members.append(Member(
                ts_code=c,
                name=str(name) if name is not None else "",
                industry_csric2=str(industry or ""),
                soe_basis=str(soe_basis or "手动添加"),
                total_mv=float(tv) if tv is not None else None,
                ttm_yield_pct=float(ttm_map[c]) if ttm_map.get(c) is not None else None,
                n_bars=int(bars_map.get(c, 0)),
                list_date=str(list_date)[:10] if list_date is not None else None,
                bt_pnl_pct=(float(b["pnl_pct"]) if b is not None
                            and b.get("pnl_pct") is not None else None),
                bt_n_buys=int(b["n_buys"] or 0) if b is not None else None,
                price_latest=price_map.get(c),
                is_manual=True,
            ))

    # 去重：auto 与 manual 交集 → 保留 auto 版但标记 is_manual=True
    manual_set = {m.ts_code for m in manual_members}
    members: List[Member] = []
    for m in auto:
        if m.ts_code in manual_set:
            members.append(dataclasses.replace(m, is_manual=True))
        else:
            members.append(m)
    members.extend(m for m in manual_members if m.ts_code not in
                   {mm.ts_code for mm in auto})

    # 排序：自动成员按十年回测收益率降序（无值殿后）；手动成员（含双料）按代码
    auto_sorted = sorted((m for m in members if not m.is_manual
                          and m.bt_pnl_pct is not None),
                         key=lambda x: (-(x.bt_pnl_pct or 0.0), x.ts_code))
    auto_none = sorted((m for m in members if not m.is_manual
                        and m.bt_pnl_pct is None), key=lambda x: x.ts_code)
    manual_sorted = sorted((m for m in members if m.is_manual),
                           key=lambda x: x.ts_code)
    members = auto_sorted + auto_none + manual_sorted

    funnel = dict(base.funnel)
    funnel.update({
        "backtest": n_g7,
        "yield": n_g8,
        "price": len(auto),
        "final": len(members),
        "manual": len(manual_members),
    })
    return PoolResult(funnel=funnel, members=members)


# ---------------------------------------------------------------------------
# 手动池文件（data/manual_pool.json，原子写）
# ---------------------------------------------------------------------------
def default_manual_pool_path() -> str:
    return os.path.join(PROJECT_ROOT, "data", MANUAL_POOL_FILENAME)


def load_manual_pool(path: Optional[str] = None) -> Dict[str, Dict[str, str]]:
    """读手动池 → {ts_code: {name, added_at}}。缺失/损坏 → {}（不阻断）。"""
    p = path or default_manual_pool_path()
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        stocks = d.get("stocks") if isinstance(d, dict) else None
        if not isinstance(stocks, dict):
            return {}
        return {str(k): v for k, v in stocks.items() if isinstance(v, dict)}
    except (OSError, ValueError):
        return {}


def save_manual_pool(stocks: Dict[str, Dict[str, str]],
                     path: Optional[str] = None) -> None:
    p = path or default_manual_pool_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "stocks": stocks,
        }, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)  # 原子替换


def normalize_ts_code(raw: str) -> str:
    """'600000' / 'SH.600000' / 'sh600000' → 'sh.600000'。非法 → ValueError。"""
    s = str(raw).strip().lower().replace(" ", "")
    s = s.replace("sh.", "sh.").removesuffix(".sh")
    if _CODE_RE.match(s):
        return s
    m = _CODE_NODOT_RE.match(s)  # 'sh600000'（无点写法）
    if m:
        return f"{m.group(1)}.{m.group(2)}"
    m = _BARE_RE.match(s)
    if m:
        d = m.group(1)
        if d[0] in ("6", "9"):
            return f"sh.{d}"
        if d[0] in "03":
            return f"sz.{d}"
        if d[0] in "48":
            return f"bj.{d}"
    raise ValueError(f"股票代码非法: {raw!r}（应形如 600000 或 sh.600000）")


def add_manual_code(con, raw_code: str,
                    path: Optional[str] = None) -> Dict[str, str]:
    """添加手动池成员（校验存在且未退市）。已存在 → 幂等返回原条目。"""
    code = normalize_ts_code(raw_code)
    r = con.execute(
        "SELECT name, delist_date FROM stock_master WHERE ts_code = ?",
        [code]).fetchone()
    if not r:
        raise ValueError(f"未找到股票 {code}（不在 stock_master）")
    if r[1] is not None:
        raise ValueError(f"{code} 已退市，不能加入候选池")
    stocks = load_manual_pool(path)
    if code in stocks:
        return stocks[code]
    entry = {"name": str(r[0]) if r[0] else code,
             "added_at": datetime.now().astimezone().isoformat(timespec="seconds")}
    stocks[code] = entry
    save_manual_pool(stocks, path)
    return entry


def remove_manual_code(code: str, path: Optional[str] = None) -> bool:
    code = normalize_ts_code(code)
    stocks = load_manual_pool(path)
    if code not in stocks:
        return False
    del stocks[code]
    save_manual_pool(stocks, path)
    return True
