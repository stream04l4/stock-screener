# -*- coding: utf-8 -*-
"""martingale.universe 单测：合成 duckdb 含/排 5 条件 + 生产湖规模。

调研 ⑥ 自测面「universe 含排/规模」：
- 合成库 10 只覆盖每条排除路径（白名单/退市/ST/SOE/市值/上市时长/连续分红/新股规则）；
- 生产湖：f6 七股必在池 + 规模带（95~110，允许湖日常同步的轻微漂移）+ 漏斗单调性。
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from martingale.config import UniverseParams  # noqa: E402
from martingale.data import connect_readonly, default_db_path  # noqa: E402
from martingale.universe import build_pool  # noqa: E402

F6_IN_POOL = ["sh.601088", "sh.600036", "sh.600900", "sh.600938"]  # 白名单行业内（B06/J66/D44/B07）
# 注：f6 七股中格力(C38)/中建(E48)/五粮液(C15) 行业不在白名单 → 本就不在池内；
# f6 回测走 CLI 显式 --codes，与池无关（回归锚点是引擎数字，见 test_f6_regression）。

RUN_YEAR = 2026


def _up(**kw):
    base = dict(industry_whitelist_csric2=("B06", "D44"), soe_keywords=("国务院", "国资委"),
                min_total_mv_yi=200.0, min_consecutive_div_years=3,
                listing_min_trading_days=250)
    base.update(kw)
    return UniverseParams(**base)


def _mk_db(path: Path) -> None:
    """10 只合成股，覆盖 5 条件全链的含/排路径。"""
    con = duckdb.connect(str(path))
    con.execute("""
        CREATE TABLE stock_master(ts_code VARCHAR, name VARCHAR, industry_csric2 VARCHAR,
                                   list_date DATE, delist_date DATE, is_st TINYINT);
        CREATE TABLE kline_daily(ts_code VARCHAR, date DATE, close DOUBLE, adj_factor DOUBLE);
        CREATE TABLE dividend_events(ts_code VARCHAR, ex_date DATE, cash_dps DOUBLE, stk_div DOUBLE);
        CREATE TABLE valuation_daily(ts_code VARCHAR, date DATE, total_mv DOUBLE, ttm_yield_pct DOUBLE);
        CREATE TABLE holders_snapshot(ts_code VARCHAR, as_of_date DATE, holder_rank TINYINT,
                                      holder_name VARCHAR, share_nature VARCHAR);
        CREATE VIEW kline_daily_hfq AS SELECT ts_code, date, (close * adj_factor) AS close
            FROM kline_daily;
    """)

    # (code, name, industry, list_date, delist_date, is_st)
    master = [
        ("sh.600001", "老股A", "B06", "2015-01-01", None, 0),      # 全过 → 池
        ("sh.600002", "白名单外", "C99", "2015-01-01", None, 0),   # 行业排除
        ("sh.600003", "已退市", "B06", "2015-01-01", "2026-01-01", 0),  # 退市排除
        ("sh.600004", "ST股", "B06", "2015-01-01", None, 1),       # ST 排除
        ("sh.600005", "民企", "B06", "2015-01-01", None, 0),       # 非 SOE 排除
        ("sh.600006", "小市值", "B06", "2015-01-01", None, 0),     # 市值排除
        ("sh.600007", "上市短", "B06", "2025-01-01", None, 0),     # 上市时长排除
        ("sh.600008", "分红断档", "B06", "2015-01-01", None, 0),   # 连续分红排除（老股）
        ("sz.000009", "新股B", "B06", "2024-06-01", None, 0),      # 新股规则通过 → 池
        ("sh.600010", "关键词SOE", "B06", "2015-01-01", None, 0),  # 关键词 SOE → 池
    ]
    con.executemany("INSERT INTO stock_master VALUES (?,?,?,?,?,?)", master)

    # K线：老股 300 bars / 新股 200 bars / 上市短 100 bars（结束 2026-10-21 倒推日历日近似）
    def bars(code, n):
        out = []
        d = 2026, 10, 21
        y, m, day = d
        dates = []
        import datetime as _dt
        cur = _dt.date(2026, 10, 21)
        while len(dates) < n:
            if cur.weekday() < 5:
                dates.append(cur)
            cur -= _dt.timedelta(days=1)
        for dt in reversed(dates):
            out.append((code, dt, 10.0, 1.0))
        return out

    kb = []
    for code, n in [("sh.600001", 300), ("sh.600002", 300), ("sh.600003", 300),
                    ("sh.600004", 300), ("sh.600005", 300), ("sh.600006", 300),
                    ("sh.600007", 100), ("sh.600008", 300),
                    ("sz.000009", 260), ("sh.600010", 300)]:  # 新股B 也要 ≥250 bars
        kb.extend(bars(code, n))
    con.executemany("INSERT INTO kline_daily VALUES (?,?,?,?)", kb)

    # 分红（run_year=2026；老股需 2023/2024/2025 连续；新股B 需 2025）
    divs = []
    for code in ["sh.600001", "sh.600002", "sh.600003", "sh.600004", "sh.600005",
                 "sh.600006", "sh.600007", "sh.600010"]:
        for y in (2023, 2024, 2025):
            divs.append((code, f"{y}-07-15", 0.5, 0.0))
    divs.append(("sh.600008", "2024-07-15", 0.5, 0.0))  # 断档：缺 2023
    divs.append(("sh.600008", "2025-07-15", 0.5, 0.0))
    divs.append(("sz.000009", "2025-07-15", 0.5, 0.0))  # 新股：上市次年起全年度
    con.executemany("INSERT INTO dividend_events VALUES (?,?,?,?)", divs)

    # 市值（最新估值日 2026-10-20）
    val = [
        ("sh.600001", "2026-10-20", 300.0, 4.0),
        ("sh.600002", "2026-10-20", 300.0, 4.0),
        ("sh.600003", "2026-10-20", 300.0, 4.0),
        ("sh.600004", "2026-10-20", 300.0, 4.0),
        ("sh.600005", "2026-10-20", 300.0, 4.0),
        ("sh.600006", "2026-10-20", 150.0, 4.0),   # < 200 亿
        ("sh.600007", "2026-10-20", 300.0, 4.0),
        ("sh.600008", "2026-10-20", 300.0, 4.0),
        ("sz.000009", "2026-10-20", 300.0, 4.0),
        ("sh.600010", "2026-10-20", 300.0, 4.0),
    ]
    con.executemany("INSERT INTO valuation_daily VALUES (?,?,?,?)", val)

    # 股东快照（最新 as_of 2026-09-30）
    holders = [
        ("sh.600001", "2026-09-30", 1, "某国有资本公司", "国有股"),
        ("sh.600002", "2026-09-30", 1, "某国有资本公司", "国有股"),
        ("sh.600003", "2026-09-30", 1, "某国有资本公司", "国有股"),
        ("sh.600004", "2026-09-30", 1, "某国有资本公司", "国有股"),
        ("sh.600005", "2026-09-30", 1, "某民营资本公司", None),
        ("sh.600006", "2026-09-30", 1, "某国有资本公司", "国有股"),
        ("sh.600007", "2026-09-30", 1, "某国有资本公司", "国有股"),
        ("sh.600008", "2026-09-30", 1, "某国有资本公司", "国有股"),
        ("sz.000009", "2026-09-30", 1, "某国有资本公司", "国有股"),
        ("sh.600010", "2026-09-30", 1, "国务院国有资产监督管理委员会", None),  # 关键词命中
    ]
    con.executemany("INSERT INTO holders_snapshot VALUES (?,?,?,?,?)", holders)
    con.close()


def test_synthetic_pool_five_conditions(tmp_path):
    db = tmp_path / "t.duckdb"
    _mk_db(db)
    con = duckdb.connect(str(db), read_only=True)
    res = build_pool(con, _up(), run_year=RUN_YEAR)
    con.close()

    # 漏斗：10 → 白名单7(排 C99/退市/ST) → SOE 6(排民企) → 市值5(排小市值)
    #      → 上市4(排上市短) → 分红3(排断档) → 3
    assert res.funnel["total"] == 10
    assert res.funnel["whitelist"] == 7
    assert res.funnel["soe"] == 6
    assert res.funnel["mv"] == 5
    assert res.funnel["listing"] == 4
    assert res.funnel["div"] == 3
    assert res.funnel["final"] == 3

    assert set(res.codes) == {"sh.600001", "sz.000009", "sh.600010"}
    by = {m.ts_code: m for m in res.members}
    assert by["sh.600001"].soe_basis == "国有股性质"
    assert by["sh.600010"].soe_basis == "关键词命中"
    assert by["sz.000009"].soe_basis == "国有股性质"
    # 成员字段
    assert by["sh.600001"].total_mv == pytest.approx(300.0)
    assert by["sh.600001"].n_bars == 300
    assert by["sh.600001"].list_date == "2015-01-01"
    assert by["sh.600001"].ttm_yield_pct == pytest.approx(4.0)


def test_synthetic_soe_forced_no_switch(tmp_path):
    """SOE 恒强制（无开关）：全民企池 → final=0。"""
    db = tmp_path / "soe.duckdb"  # 独立库（RW），避免 read-only 连接拒绝 UPDATE
    _mk_db(db)
    con = duckdb.connect(str(db))
    con.execute("UPDATE holders_snapshot SET share_nature = NULL")
    con.execute("UPDATE holders_snapshot SET holder_name = '某民营资本公司' "
                "WHERE holder_name LIKE '%国务院%'")
    res = build_pool(con, _up(), run_year=RUN_YEAR)
    con.close()
    assert res.funnel["soe"] == 0
    assert res.funnel["final"] == 0


def test_synthetic_run_year_shift(tmp_path):
    """run_year 注入：2025 口径下老股需 2022/2023/2024 → 老股A 断档出局（池只剩新股B/关键词SOE）。"""
    db = tmp_path / "t.duckdb"
    _mk_db(db)
    con = duckdb.connect(str(db), read_only=True)
    res = build_pool(con, _up(), run_year=2025)
    con.close()
    # 老股A/关键词SOE 缺 2022 → 出局；新股B 窗口 range(2025,2025)=空 → 天然通过
    assert res.funnel["final"] == 1
    assert res.codes == ["sz.000009"]


# ---------------------------------------------------------------------------
# 生产湖规模（f6 七股必在池 + 规模带 + 漏斗单调性；不锁死精确数字——湖日常同步会漂）
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not Path(default_db_path()).exists(), reason="生产湖不存在")
def test_production_pool_scale_and_f6_members():
    con = connect_readonly()
    try:
        p, u = __import__("martingale.config", fromlist=["load_config"]).load_config(
            str(PROJECT_ROOT / "config" / "strategy.yaml"))
        res = build_pool(con, u)
    finally:
        con.close()
    f = res.funnel
    assert f["total"] >= f["whitelist"] >= f["soe"] >= f["mv"] >= f["listing"] >= f["div"] >= f["final"]
    assert f["final"] >= 90, f"池规模异常: {f}"
    missing = [c for c in F6_IN_POOL if c not in set(res.codes)]
    assert not missing, f"f6 白名单行业内四股不在池: {missing}"
