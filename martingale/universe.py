# -*- coding: utf-8 -*-
"""martingale.universe —— 股票池（全链 5 条件，全离线单库）。

链路（顺序 = 漏斗输出顺序；调研 ①②，TL 独立 SQL 复算确认 5556→347→289→125→124→~95）：
1. 全市场 stock_master
2. ∩ 行业白名单（industry_csric2 前缀匹配；v8.0.8：["*"] = 全放开）∩ 未退市 ∩ 非ST
3. ∩ 央国企（SOE 双规则，恒强制）
   v8.0.8 国企控股强度门（可选，默认关闭）：最大单一国有占比 ≥ min_soe_ratio_pct
   或 top10 国有合计 ≥ min_soe_sum_ratio_pct 或 soe_control_overrides 豁免；
   另有 exclude_codes 硬剔除名单（伪国企，如隆鑫通用）
4. ∩ 总市值 ≥ min_total_mv_yi（最新估值日）
5. ∩ 上市满 listing_min_trading_days 个交易日（K线行数）
6. ∩ 连续分红（老股规则 + 新股规则，语义 as-is 移植自 v6.x _div_history_ok）

SOE 双规则（移植自 v6.x metrics.soe_flag/soe_basis，语义不变）：
- 取该股 holders_snapshot 最新 as_of 的 top10 持股；
- 规则 A：任一股东 share_nature='国有股'；
- 规则 B：任一股东名含 soe_keywords 任一关键词（LIKE %kw%）；
- 命中即 SOE；soe_basis 标注命中来源（规则 A 优先）。

分红规则（as-is 移植，brief 允许与调研 95 的 ±1~3 偏差——新股规则语义差异）：
- 年度现金分红 = dividend_events 按 ex_date 年份聚合的 cash_dps 之和；
- 老股（上市早于 run_year−N 年）：[run_year−N, run_year−1] 每一年度都 > 0；
- 新股（上市不早于该窗口起点）：上市次年至 run_year−1 的**全部**年度都 > 0
  （窗口不足 1 年 → 天然通过）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as _date
from typing import Any, Dict, List, Optional, Tuple

from .config import UniverseParams


@dataclass(frozen=True)
class Member:
    """池成员（/api/pool 与 CLI pool 的输出单元）。

    v7.0.12 起为候选池成员：新增 bt_*/price_latest/is_manual（选股门槛预计算
    展示字段；非候选池路径（旧语义调用）保持缺省 None/False）。
    """
    ts_code: str
    name: str
    industry_csric2: str
    soe_basis: str            # 国有股性质 | 关键词命中
    total_mv: Optional[float]   # 最新估值日总市值（亿元）
    ttm_yield_pct: Optional[float]  # 最新估值日股息率 TTM（%，展示用，不作门槛）
    n_bars: int                 # 上市以来交易日数
    list_date: Optional[str]    # YYYY-MM-DD
    # —— v7.0.12 候选池扩展（selection.build_candidate_pool 填充）——
    bt_pnl_pct: Optional[float] = None    # 十年回测净收益率（% 小数，None=未触发）
    bt_n_buys: Optional[int] = None       # 十年回测买入次数
    bt_div_pool: Optional[float] = None   # 十年回测分红余额（元，v8.0.11）
    bt_price_pnl: Optional[float] = None  # 十年回测价差盈亏（元，v8.0.11）
    bt_dividends: Optional[float] = None  # 十年回测累计分红（元，v8.0.12；与余额互补：余额=累计−再投）
    bt_div_share: Optional[float] = None  # 分红占比=分红余额/总盈亏（小数；None=不可拆，v8.0.11）
    bt_price_share: Optional[float] = None  # 上涨占比=价差盈亏/总盈亏（小数；v8.0.11）
    price_latest: Optional[float] = None  # 最新收盘价（元）
    is_manual: bool = False               # 手动添加（绕过 G7~G9）


@dataclass(frozen=True)
class PoolResult:
    funnel: Dict[str, int]      # total/whitelist/soe/mv/listing/div/final
    members: List[Member] = field(default_factory=list)

    @property
    def codes(self) -> List[str]:
        return [m.ts_code for m in self.members]


def _div_history_ok(annual_dps: Dict[int, float], min_years: int, run_year: int,
                    list_year: Optional[int]) -> bool:
    """连续分红判定（as-is 移植 v6.x _div_history_ok + new_stock_div_ok）。

    为什么新股规则更严（全部年度而非最近 N 年）：新股上市时间短，"最近 N 年"
    会漏掉其上市首年的分红缺失；as-is 移植 = brief 允许的最终池偏差来源。
    """
    min_year = run_year - min_years
    if list_year is None or list_year < min_year:
        # 老股：[min_year, run_year-1] 每一年度都分红
        return all(annual_dps.get(y, 0.0) > 0.0 for y in range(min_year, run_year))
    years = list(range(list_year + 1, run_year))
    if not years:
        return True
    return all(annual_dps.get(y, 0.0) > 0.0 for y in years)


def _soe_set(con, soe_keywords: tuple) -> Dict[str, str]:
    """SOE 双规则：{ts_code: basis}（basis = 国有股性质 | 关键词命中）。

    取该股 holders_snapshot **最新 as_of 的前十大股东**（holder_rank<=10，
    与 v6.x 新浪 F10"前十大股东"口径一致）；
    规则 A：任一股东 share_nature='国有股'；
    规则 B：任一股东名含 soe_keywords 任一关键词（LIKE %kw%）；
    命中即 SOE；soe_basis 标注命中来源（规则 A 优先，字段级证据强于名称推断）。
    """
    kw_ors = " OR ".join("h.holder_name LIKE ?" for _ in soe_keywords)
    sql = f"""
        SELECT h.ts_code,
               MAX(CASE WHEN h.share_nature = '国有股' THEN 1 ELSE 0 END) AS is_state,
               MAX(CASE WHEN ({kw_ors}) THEN 1 ELSE 0 END) AS is_kw
        FROM holders_snapshot h
        JOIN (SELECT ts_code, MAX(as_of_date) AS latest FROM holders_snapshot GROUP BY ts_code) l
          ON h.ts_code = l.ts_code AND h.as_of_date = l.latest
        WHERE h.holder_rank <= 10
        GROUP BY h.ts_code
    """
    con.execute(sql, [f"%{kw}%" for kw in soe_keywords])
    out: Dict[str, str] = {}
    for code, is_state, is_kw in con.fetchall():
        if is_state:
            out[code] = "国有股性质"
        elif is_kw:
            out[code] = "关键词命中"
    return out


def _soe_ratio_map(con) -> Dict[str, Tuple[float, float]]:
    """v8.0.8：每股最新 as_of 的 top10 国有性质（share_nature='国有股'）持股。

    返回 {code: (最大单一国有占比, top10 国有合计占比)}（百分数口径，与
    min_soe_ratio_pct/min_soe_sum_ratio_pct 同）。无国有 top10 股东的股不出现在
    结果里（调用方用 .get(c, (0.0, 0.0))——SOE 双规则已保证命中时至少一方非零：
    规则 A 命中 ⇒ 国有占比 > 0；规则 B 关键词命中而无「国有股」性质标注的，
    按 0 处理 = 需过合计/豁免档，属设计内从严）。
    """
    q = """
        SELECT h.ts_code, h.hold_ratio
        FROM holders_snapshot h
        JOIN (SELECT ts_code, MAX(as_of_date) la
              FROM holders_snapshot GROUP BY ts_code) l
          ON h.ts_code = l.ts_code AND h.as_of_date = l.la
        WHERE h.holder_rank <= 10 AND h.share_nature = '国有股'
    """
    acc: Dict[str, List[float]] = {}
    for code, ratio in con.execute(q).fetchall():
        acc.setdefault(code, []).append(float(ratio or 0.0))
    return {c: (max(rs), sum(rs)) for c, rs in acc.items()}


# ---------------------------------------------------------------------------
# v8.0.13 PIT（point-in-time）版漏斗：动态池回测用「截至 as_of 日的股票池」。
# 与 build_pool 的语义差（均为设计内近似，README §动态池 有载）：
# - 退市/ST 按 as_of 判定（delist_date > as_of 仍存活；st_since ≤ as_of 视为 ST
#   ——无 ST 摘帽日期，从严）；生产 build_pool 用当前快照（delist IS NULL）。
# - holders 取 ≤ as_of 的最近季度快照（快照缺失期沿用更早快照）。
# - 估值/价格/分红/K线行数全部 date ≤ as_of 口径。
# 生产路径（build_pool）零改动。
# ---------------------------------------------------------------------------
def _soe_set_asof(con, as_of, soe_keywords: tuple) -> Dict[str, str]:
    """_soe_set 的 PIT 版：取 ≤ as_of 的最近 holders 快照。"""
    kw_ors = " OR ".join("h.holder_name LIKE ?" for _ in soe_keywords)
    sql = f"""
        SELECT h.ts_code,
               MAX(CASE WHEN h.share_nature = '国有股' THEN 1 ELSE 0 END) AS is_state,
               MAX(CASE WHEN ({kw_ors}) THEN 1 ELSE 0 END) AS is_kw
        FROM holders_snapshot h
        JOIN (SELECT ts_code, MAX(as_of_date) AS latest
              FROM holders_snapshot WHERE as_of_date <= ?
              GROUP BY ts_code) l
          ON h.ts_code = l.ts_code AND h.as_of_date = l.latest
        WHERE h.holder_rank <= 10
        GROUP BY h.ts_code
    """
    # 占位符文本序：SELECT 里的 kw_ors 在前，子查询的 as_of 在后
    con.execute(sql, [f"%{kw}%" for kw in soe_keywords] + [as_of])
    out: Dict[str, str] = {}
    for code, is_state, is_kw in con.fetchall():
        if is_state:
            out[code] = "国有股性质"
        elif is_kw:
            out[code] = "关键词命中"
    return out


def _soe_ratio_map_asof(con, as_of) -> Dict[str, Tuple[float, float]]:
    """_soe_ratio_map 的 PIT 版：取 ≤ as_of 的最近 holders 快照。"""
    q = """
        SELECT h.ts_code, h.hold_ratio
        FROM holders_snapshot h
        JOIN (SELECT ts_code, MAX(as_of_date) la
              FROM holders_snapshot WHERE as_of_date <= ?
              GROUP BY ts_code) l
          ON h.ts_code = l.ts_code AND h.as_of_date = l.la
        WHERE h.holder_rank <= 10 AND h.share_nature = '国有股'
    """
    acc: Dict[str, List[float]] = {}
    for code, ratio in con.execute(q, [as_of]).fetchall():
        acc.setdefault(code, []).append(float(ratio or 0.0))
    return {c: (max(rs), sum(rs)) for c, rs in acc.items()}


def build_pool_asof(con, up: UniverseParams, as_of) -> PoolResult:
    """v8.0.13：as_of 日口径的股票池（漏斗与 build_pool 逐门一致，数据全部
    date ≤ as_of）。:param as_of: date/datetime64/str（YYYY-MM-DD 均可，SQL ?
    绑定自动适配）。run_year 固定 = as_of 年（连续分红窗口的截止年）。"""
    a = str(as_of)[:10]
    run_year = int(str(as_of)[:4])

    # 2) 白名单 + 存活(as_of) + 非ST(as_of)
    if set(up.industry_whitelist_csric2) == {"*"}:
        wl_sql = """
            SELECT ts_code, name, industry_csric2, list_date
            FROM stock_master sm
            WHERE (delist_date IS NULL OR delist_date > ?)
              AND (st_since IS NULL OR st_since > ?)
        """
        con.execute(wl_sql, [a, a])
    else:
        wl_ors = " OR ".join("sm.industry_csric2 LIKE ?" for _ in up.industry_whitelist_csric2)
        wl_sql = f"""
            SELECT ts_code, name, industry_csric2, list_date
            FROM stock_master sm
            WHERE ({wl_ors}) AND (delist_date IS NULL OR delist_date > ?)
              AND (st_since IS NULL OR st_since > ?)
        """
        con.execute(wl_sql, [f"{c}%" for c in up.industry_whitelist_csric2] + [a, a])
    wl_rows = con.fetchall()
    wl = {r[0]: r for r in wl_rows}

    # 3) SOE（PIT）
    soe = _soe_set_asof(con, a, up.soe_keywords)
    soe_codes = sorted(set(wl) & set(soe))
    soe_raw = len(soe_codes)

    # 3-b) 硬剔除
    if up.exclude_codes:
        excl = set(up.exclude_codes)
        soe_codes = [c for c in soe_codes if c not in excl]

    # 3-c) 控股强度门（PIT）
    if up.min_soe_ratio_pct > 0 or up.min_soe_sum_ratio_pct > 0:
        ratios = _soe_ratio_map_asof(con, a)
        ov = set(up.soe_control_overrides)
        soe_codes = [
            c for c in soe_codes
            if (ratios.get(c, (0.0, 0.0))[0] >= up.min_soe_ratio_pct
                or ratios.get(c, (0.0, 0.0))[1] >= up.min_soe_sum_ratio_pct
                or c in ov)
        ]

    # 4) 市值（每股 ≤ as_of 最近估值日）
    con.execute("""
        SELECT v.ts_code, v.total_mv FROM valuation_daily v
        JOIN (SELECT ts_code, MAX(date) md FROM valuation_daily
              WHERE date <= ? GROUP BY ts_code) x
          ON v.ts_code = x.ts_code AND v.date = x.md
    """, [a])
    mv_map = {c: mv for c, mv in con.fetchall() if mv is not None}
    mv_codes = sorted(c for c in soe_codes
                      if mv_map.get(c) is not None and mv_map[c] >= up.min_total_mv_yi)

    # 5) 上市时长（≤ as_of 的 K线行数）
    con.execute(
        "SELECT ts_code, COUNT(*) FROM kline_daily WHERE date <= ? GROUP BY ts_code", [a])
    bars = dict(con.fetchall())
    listing_codes = sorted(c for c in mv_codes
                           if bars.get(c, 0) >= up.listing_min_trading_days)

    # 6) 连续分红（窗口截止年 = as_of 年；只取 ≤ as_of 的除权事件）
    con.execute("""
        SELECT ts_code, CAST(strftime(ex_date, '%Y') AS INTEGER) AS y,
               SUM(COALESCE(cash_dps, 0)) AS dps
        FROM dividend_events WHERE ex_date IS NOT NULL AND ex_date <= ?
        GROUP BY ts_code, y
    """, [a])
    div_map: Dict[str, Dict[int, float]] = {}
    for code, y, dps in con.fetchall():
        div_map.setdefault(code, {})[int(y)] = float(dps or 0.0)
    div_codes: List[str] = []
    for c in listing_codes:
        row = wl[c]
        list_year = None
        if row[3] is not None:
            ld = row[3]
            list_year = ld.year if hasattr(ld, "year") else int(str(ld)[:4])
        if _div_history_ok(div_map.get(c, {}), up.min_consecutive_div_years,
                           run_year, list_year):
            div_codes.append(c)
    div_codes = sorted(div_codes)

    # 成员详情（ttm 取 ≤ as_of 最近估值日）
    members: List[Member] = []
    for c in div_codes:
        name = wl[c][1]
        industry = wl[c][2] or ""
        ld = wl[c][3]
        list_date = str(ld)[:10] if ld is not None else None
        tv = mv_map.get(c)
        con.execute(
            "SELECT ttm_yield_pct FROM valuation_daily WHERE ts_code = ? AND date = "
            "(SELECT MAX(date) FROM valuation_daily WHERE ts_code = ? AND date <= ?) "
            "ORDER BY date DESC LIMIT 1",
            [c, c, a])
        r = con.fetchone()
        ttm = float(r[0]) if r and r[0] is not None else None
        members.append(Member(
            ts_code=c, name=str(name) if name is not None else "",
            industry_csric2=str(industry), soe_basis=soe[c],
            total_mv=float(tv) if tv is not None else None, ttm_yield_pct=ttm,
            n_bars=int(bars.get(c, 0)), list_date=list_date,
        ))

    funnel = {
        "total": int(con.execute("SELECT COUNT(*) FROM stock_master").fetchone()[0]),
        "whitelist": len(wl),
        "soe": soe_raw,
        "soe_ratio": len(soe_codes),
        "mv": len(mv_codes),
        "listing": len(listing_codes),
        "div": len(div_codes),
        "final": len(members),
    }
    return PoolResult(funnel=funnel, members=members)


def build_pool(con, up: UniverseParams, run_year: Optional[int] = None) -> PoolResult:
    """构建股票池。:param con: 只读 duckdb 连接；:param run_year: 分红判定的
    "当前年"（缺省 = 当年；测试可注入以复现历史口径）。"""
    run_year = run_year or _date.today().year

    # 2) 白名单 + 存活 + 非ST（industry_csric2 前缀匹配；v8.0.8：["*"] = 全放开）
    if set(up.industry_whitelist_csric2) == {"*"}:
        wl_sql = """
            SELECT ts_code, name, industry_csric2, list_date
            FROM stock_master sm
            WHERE delist_date IS NULL AND COALESCE(is_st, 0) = 0
        """
        con.execute(wl_sql)
    else:
        wl_ors = " OR ".join("sm.industry_csric2 LIKE ?" for _ in up.industry_whitelist_csric2)
        wl_sql = f"""
            SELECT ts_code, name, industry_csric2, list_date
            FROM stock_master sm
            WHERE ({wl_ors}) AND delist_date IS NULL AND COALESCE(is_st, 0) = 0
        """
        con.execute(wl_sql, [f"{c}%" for c in up.industry_whitelist_csric2])
    wl_rows = con.fetchall()
    wl = {r[0]: r for r in wl_rows}

    # 3) SOE
    soe = _soe_set(con, up.soe_keywords)
    soe_codes = sorted(set(wl) & set(soe))
    soe_raw = len(soe_codes)  # 漏斗展示用：交后计数（门前的口径）

    # 3-b) v8.0.8 硬剔除名单（控股强度门之上的硬排除，审计留痕见 yaml 注释）
    if up.exclude_codes:
        excl = set(up.exclude_codes)
        soe_codes = [c for c in soe_codes if c not in excl]

    # 3-c) v8.0.8 SOE 控股强度门（两键均 0 = 关闭，回退 v8.0.0 行为）：
    # 最大单一国有占比 ≥ min_soe_ratio_pct 或 top10 国有合计 ≥ min_soe_sum_ratio_pct
    # 或命中 soe_control_overrides（湖数据标注缺口豁免）→ 保留。
    if up.min_soe_ratio_pct > 0 or up.min_soe_sum_ratio_pct > 0:
        ratios = _soe_ratio_map(con)
        ov = set(up.soe_control_overrides)
        soe_codes = [
            c for c in soe_codes
            if (ratios.get(c, (0.0, 0.0))[0] >= up.min_soe_ratio_pct
                or ratios.get(c, (0.0, 0.0))[1] >= up.min_soe_sum_ratio_pct
                or c in ov)
        ]

    # 4) 市值（最新估值日 total_mv；valuation_daily 日期列名 = date）
    mv_sql = """
        SELECT ts_code, total_mv FROM valuation_daily
        WHERE date = (SELECT MAX(date) FROM valuation_daily)
    """
    con.execute(mv_sql)
    mv_map = dict(con.fetchall())
    mv_codes = sorted(c for c in soe_codes
                      if (mv_map.get(c) is not None and mv_map[c] >= up.min_total_mv_yi))

    # 5) 上市时长（K线行数）
    con.execute("SELECT ts_code, COUNT(*) FROM kline_daily GROUP BY ts_code")
    bars = dict(con.fetchall())
    listing_codes = sorted(c for c in mv_codes if bars.get(c, 0) >= up.listing_min_trading_days)

    # 6) 连续分红
    con.execute("""
        SELECT ts_code, CAST(strftime(ex_date, '%Y') AS INTEGER) AS y,
               SUM(COALESCE(cash_dps, 0)) AS dps
        FROM dividend_events WHERE ex_date IS NOT NULL
        GROUP BY ts_code, y
    """)
    div_map: Dict[str, Dict[int, float]] = {}
    for code, y, dps in con.fetchall():
        div_map.setdefault(code, {})[int(y)] = float(dps or 0.0)
    div_codes: List[str] = []
    for c in listing_codes:
        row = wl[c]
        list_year = None
        if row[3] is not None:
            ld = row[3]
            list_year = ld.year if hasattr(ld, "year") else int(str(ld)[:4])
        if _div_history_ok(div_map.get(c, {}), up.min_consecutive_div_years, run_year, list_year):
            div_codes.append(c)
    div_codes = sorted(div_codes)

    # 成员详情
    members: List[Member] = []
    for c in div_codes:
        name = wl[c][1]
        industry = wl[c][2] or ""
        ld = wl[c][3]
        list_date = str(ld)[:10] if ld is not None else None
        tv = mv_map.get(c)
        con.execute(
            "SELECT ttm_yield_pct FROM valuation_daily WHERE ts_code = ? AND date = "
            "(SELECT MAX(date) FROM valuation_daily WHERE ts_code = ?) ORDER BY date DESC LIMIT 1",
            [c, c])
        r = con.fetchone()
        ttm = float(r[0]) if r and r[0] is not None else None
        members.append(Member(
            ts_code=c, name=str(name) if name is not None else "",
            industry_csric2=str(industry), soe_basis=soe[c],
            total_mv=float(tv) if tv is not None else None, ttm_yield_pct=ttm,
            n_bars=int(bars.get(c, 0)), list_date=list_date,
        ))

    funnel = {
        "total": int(con.execute("SELECT COUNT(*) FROM stock_master").fetchone()[0]),
        "whitelist": len(wl),
        "soe": soe_raw,
        "soe_ratio": len(soe_codes),  # v8.0.8 排除+控股强度门后的计数（门关闭时=soe）
        "mv": len(mv_codes),
        "listing": len(listing_codes),
        "div": len(div_codes),
        "final": len(members),
    }
    return PoolResult(funnel=funnel, members=members)
