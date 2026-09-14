# -*- coding: utf-8 -*-
"""test_lake_ddl —— v6 数据湖 schema 测试（TL 强制要求）。

**核心纪律（tl_decisions.md）**：必须把报告 §1 的**整个 sql 块逐语句真实执行**
（本次调研事故根因 = view 从未被真实跑过——hfq/qfq view 的裸别名 ``close`` 在
DuckDB 1.5.5 是保留字，只有真实执行才暴露）。本测试直接 import ``lake.ddl.DDL_STATEMENTS``
（= §1 逐字 DDL + IF NOT EXISTS）并**逐条 execute**，任何一条解析/绑定失败立即 fail。

覆盖：
1. 9 表 + 3 view + 索引全量真实执行（幂等：连跑两遍不报错）。
2. 复权 view 数值断言（构造已知 af 数据）：
   - hfq close = raw × af（4.1 × 2.5545 = 10.47345，精确值）；
   - qfq close = raw × (af/该股最新af)（最新行=raw，历史行按比例缩放）。
3. Parquet round-trip（hive 分区 + zstd 导出 → read_parquet 读回一致）。
"""
from __future__ import annotations

import os

import pytest

duckdb = pytest.importorskip("duckdb")


@pytest.fixture()
def con():
    c = duckdb.connect(":memory:")
    yield c
    c.close()


# ---------------------------------------------------------------------------
# 1. §1 DDL 逐语句真实执行（全量；幂等）
# ---------------------------------------------------------------------------
def test_ddl_full_execution(con):
    """lake.ddl.DDL_STATEMENTS（=报告§1逐字）逐条 execute——任何一条失败即 fail。"""
    from lake.ddl import DDL_STATEMENTS, INDEX_STATEMENTS, init_schema

    # 直接逐语句执行（不经过 init_schema 封装，确保"真实跑过每一条"）
    for stmt in DDL_STATEMENTS + INDEX_STATEMENTS:
        con.execute(stmt)

    # 幂等：再跑一遍 init_schema（IF NOT EXISTS）不得报错
    init_schema(con)


def test_ddl_tables_and_views_exist(con):
    from lake.ddl import TABLES, init_schema

    init_schema(con)
    tables = {r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='main'").fetchall()}
    for t in TABLES:
        assert t in tables, f"表 {t} 未创建"
    # 3 个 view 必须存在（事故根因=view 没被真实跑过）
    views = {r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_type='VIEW' AND table_schema='main'").fetchall()}
    for v in ("kline_daily_hfq", "kline_daily_qfq", "stock_panorama"):
        assert v in views, f"view {v} 未创建"


# ---------------------------------------------------------------------------
# 2. 复权 view 数值断言（构造已知 af）
# ---------------------------------------------------------------------------
def test_hfq_view_exact_value(con):
    """hfq close = raw × af：4.1 × 2.5545 = 10.47345（精确）。"""
    from lake.ddl import init_schema

    init_schema(con)
    con.execute(
        "INSERT INTO kline_daily (ts_code,date,open,high,low,close,volume,"
        "amount,pct_chg,is_st,preclose,adj_factor,source,fetched_at,data_version) "
        "VALUES ('sh.601398','2026-05-13',4.0,4.2,3.9,4.1,1000,4100.0,1.2,0,NULL,"
        "2.5545,'t','2026-09-14 00:00:00','v6.0')")
    r = con.execute('SELECT "close" FROM kline_daily_hfq WHERE ts_code=\'sh.601398\'').fetchone()
    assert abs(r[0] - 4.1 * 2.5545) < 1e-9, f"hfq close={r[0]} != {4.1*2.5545}"
    assert abs(r[0] - 10.47345) < 1e-6


def test_qfq_view_forward_fill(con):
    """qfq close = raw × (af/最新af)：最新行=raw，历史行按比例缩放。

    构造两日：2026-05-12 af=2.452626 close=4.0；2026-05-13 af=2.5545 close=4.1（最新）。
    - 最新行 qfq = 4.1 × (2.5545/2.5545) = 4.1（=raw）；
    - 历史行 qfq = 4.0 × (2.452626/2.5545)。
    """
    from lake.ddl import init_schema

    init_schema(con)
    con.execute(
        "INSERT INTO kline_daily (ts_code,date,close,adj_factor) VALUES "
        "('sh.601398','2026-05-12',4.0,2.452626),('sh.601398','2026-05-13',4.1,2.5545)")
    rows = con.execute(
        "SELECT date,\"close\" FROM kline_daily_qfq WHERE ts_code='sh.601398' "
        "ORDER BY date").fetchall()
    d12, d13 = rows[0][1], rows[1][1]
    assert abs(d13 - 4.1) < 1e-9, f"最新行 qfq={d13} != raw 4.1"
    expect_d12 = 4.0 * (2.452626 / 2.5545)
    assert abs(d12 - expect_d12) < 1e-9, f"历史行 qfq={d12} != {expect_d12}"


def test_panorama_view_assembly(con):
    """stock_panorama：T1⋈T3(最新)+T8(标量因子)+T4(最近分红)拼装正确。"""
    from lake.ddl import init_schema

    init_schema(con)
    con.execute("INSERT INTO stock_master (ts_code,name,industry_csric2,board,is_st,"
                "soe_flag,source,fetched_at,data_version) VALUES "
                "('sh.601398','工商银行','J66','主板',0,'央国企','t','2026-09-14 00:00:00','v6.0')")
    con.execute("INSERT INTO valuation_daily (ts_code,date,total_mv,pe_ttm,pb,"
                "ttm_yield_pct,source,fetched_at,data_version) VALUES "
                "('sh.601398','2026-09-14',25000.0,5.2,0.6,4.5,'t','2026-09-14 00:00:00','v6.0')")
    con.execute("INSERT INTO factor_snapshot (ts_code,as_of_date,factor_name,value,"
                "source,fetched_at,data_version) VALUES "
                "('sh.601398','2026-09-14','ann_vol_5y',18.5,'t','2026-09-14 00:00:00','v6.0')")
    con.execute("INSERT INTO dividend_events (ts_code,ex_date,cash_dps,source,"
                "fetched_at,data_version) VALUES "
                "('sh.601398','2026-05-13',2.3,'t','2026-09-14 00:00:00','v6.0')")
    r = con.execute("SELECT name,total_mv,ann_vol_5y,last_cash_dps,last_ex_date "
                    "FROM stock_panorama WHERE ts_code='sh.601398'").fetchone()
    assert r[0] == "工商银行"
    assert abs(r[1] - 25000.0) < 1e-9
    assert abs(r[2] - 18.5) < 1e-9
    assert abs(r[3] - 2.3) < 1e-9
    assert str(r[4]) == "2026-05-13"


# ---------------------------------------------------------------------------
# 2b. 回归：无 PK 事件表二次灌入不重复行（v6 run1 BinderException 修复）
#     DuckDB 的 INSERT OR REPLACE 要求目标表有 UNIQUE/PK 约束；dividend_events /
#     holders_snapshot 无 PK → ingest 必须走 delete_where + insert_many 先删后插。
# ---------------------------------------------------------------------------
def test_dividend_events_reinsert_idempotent(con, tmp_path):
    """T4 dividend_events（无 PK）：同一 CSV 二次灌入，行数不翻倍、内容一致。"""
    from lake.ddl import init_schema
    from lake.ingest.em_dividend_ingest import load_dividends

    init_schema(con)
    cache_dir = str(tmp_path / "cache")
    os.makedirs(cache_dir, exist_ok=True)
    # 与 em.py 落盘格式一致：哨兵行 + 表头 + 数据（dps_pretax 已 /10，元/股）
    csv_text = (
        "stock-screener-em-cache-v1\n"
        "code,report_date,plan_notice_date,ex_date,dps_pretax,progress\n"
        "601398,2025-12-31,2026-07-31,2026-08-14,1.699500,实施分配\n"
        "601398,2024-12-31,2025-07-31,2025-08-15,1.603100,实施分配\n"
    )
    with open(os.path.join(cache_dir, "em_dividend_all.csv"), "w", encoding="utf-8") as f:
        f.write(csv_text)

    n1 = load_dividends(con, cache_dir, ts_codes=["sh.601398"])
    assert n1 == 2, f"首次灌入应 2 行，实际 {n1}"
    c1 = con.execute(
        "SELECT COUNT(*) FROM dividend_events WHERE ts_code='sh.601398'").fetchone()[0]
    assert c1 == 2

    # 二次灌入（同数据）：先删后插 → 仍 2 行，不重复
    n2 = load_dividends(con, cache_dir, ts_codes=["sh.601398"])
    assert n2 == 2
    c2 = con.execute(
        "SELECT COUNT(*) FROM dividend_events WHERE ts_code='sh.601398'").fetchone()[0]
    assert c2 == 2, f"二次灌入后行数翻倍（{c2}）——无 PK 表幂等被破坏"
    # 内容未变（ex_date/dps 精确一致）
    rows = con.execute(
        "SELECT ex_date, cash_dps FROM dividend_events WHERE ts_code='sh.601398' "
        "ORDER BY ex_date").fetchall()
    assert [str(r[0]) for r in rows] == ["2025-08-15", "2026-08-14"]
    assert abs(rows[0][1] - 1.6031) < 1e-9 and abs(rows[1][1] - 1.6995) < 1e-9


def test_holders_snapshot_reinsert_idempotent(con):
    """T6 holders_snapshot（无 PK）：load_t6 二次灌入不重复行；PIT 模式只换当期。"""
    from lake.ddl import init_schema
    from lake.ingest.sina_ingest import load_t6

    init_schema(con)
    periods = [{
        "end_date": "2026-06-30",
        "notice_date": "2026-08-29",
        "holders": [
            {"holder_rank": 1, "holder_name": "中央汇金投资有限责任公司",
             "circ_ratio_pct": 34.5, "share_nature": "国有股"},
            {"holder_rank": 2, "holder_name": "全国社保基金一零三组合",
             "circ_ratio_pct": 1.2, "share_nature": "其他内资持股"},
        ],
    }]

    n1 = load_t6(con, "sh.601398", periods)
    assert n1 == 2
    c1 = con.execute(
        "SELECT COUNT(*) FROM holders_snapshot WHERE ts_code='sh.601398'").fetchone()[0]
    assert c1 == 2

    # 二次灌入（同数据）：先删后插 → 仍 2 行，不重复
    n2 = load_t6(con, "sh.601398", periods)
    assert n2 == 2
    c2 = con.execute(
        "SELECT COUNT(*) FROM holders_snapshot WHERE ts_code='sh.601398'").fetchone()[0]
    assert c2 == 2, f"二次灌入后行数翻倍（{c2}）——无 PK 表幂等被破坏"

    # 全量模式（as_of_date=None）：输入只有 2025-12-31 期 → 整股快照被替换为该行
    other = [{
        "end_date": "2025-12-31",
        "notice_date": "2026-04-30",
        "holders": [{"holder_rank": 1, "holder_name": "中央汇金投资有限责任公司",
                     "circ_ratio_pct": 35.0, "share_nature": "国有股"}],
    }]
    load_t6(con, "sh.601398", other)
    c3 = con.execute(
        "SELECT COUNT(*) FROM holders_snapshot WHERE ts_code='sh.601398'").fetchone()[0]
    assert c3 == 1, f"全量重灌后应只剩输入期（{c3}）"

    # PIT：只更新 2026-06-30，不碰 2025-12-31
    changed = [{
        "end_date": "2026-06-30",
        "notice_date": "2026-08-29",
        "holders": [{"holder_rank": 1, "holder_name": "中央汇金资产管理有限责任公司",
                     "circ_ratio_pct": 34.6, "share_nature": "国有股"}],
    }]
    load_t6(con, "sh.601398", changed, as_of_date="2026-06-30")
    c4 = con.execute(
        "SELECT COUNT(*) FROM holders_snapshot WHERE ts_code='sh.601398'").fetchone()[0]
    assert c4 == 2, f"PIT 更新后应为 2 期（{c4}）"
    r25 = con.execute(
        "SELECT holder_name FROM holders_snapshot WHERE ts_code='sh.601398' "
        "AND as_of_date='2025-12-31'").fetchone()
    assert r25 and r25[0] == "中央汇金投资有限责任公司", "PIT 更新误删了其他期"
    r26 = con.execute(
        "SELECT holder_name FROM holders_snapshot WHERE ts_code='sh.601398' "
        "AND as_of_date='2026-06-30' AND holder_rank=1").fetchone()
    assert r26 and r26[0] == "中央汇金资产管理有限责任公司", "PIT 当期未更新"

    # 零行输入 → no-op（不删旧数据：无法区分真无数据与解析漂移）
    n0 = load_t6(con, "sh.601398", [], as_of_date="2026-06-30")
    assert n0 == 0
    c5 = con.execute(
        "SELECT COUNT(*) FROM holders_snapshot WHERE ts_code='sh.601398'").fetchone()[0]
    assert c5 == 2, f"零行输入不应删旧数据（{c5}）"


# ---------------------------------------------------------------------------
# 3. Parquet round-trip（hive 分区 + zstd）
# ---------------------------------------------------------------------------
def test_parquet_roundtrip(tmp_path):
    """kline_daily → COPY hive 分区(zstd) → read_parquet 读回，值一致。"""
    from lake import conn as lconn
    from lake.ddl import init_schema

    db = str(tmp_path / "lake.duckdb")
    con = duckdb.connect(db)
    init_schema(con)
    con.execute(
        "INSERT INTO kline_daily (ts_code,date,close,adj_factor) VALUES "
        "('sh.601398','2026-05-12',4.0,2.452626),('sh.601398','2026-05-13',4.1,2.5545)")
    out_dir = str(tmp_path / "parquet")
    target = lconn.export_parquet("kline_daily", out_dir, con=con)
    # 分区目录应存在（date=YYYY-MM-DD）
    assert os.path.isdir(target)
    back = con.execute(
        f"SELECT ts_code, date, close FROM read_parquet('{os.path.join(out_dir,'kline_daily','**','*.parquet')}', "
        "HIVE_PARTITIONING=1) ORDER BY date").fetchall()
    assert len(back) == 2
    assert back[0][0] == "sh.601398" and str(back[0][1]) == "2026-05-12" and abs(back[0][2] - 4.0) < 1e-9
    assert str(back[1][1]) == "2026-05-13" and abs(back[1][2] - 4.1) < 1e-9
    con.close()
