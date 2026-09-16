# -*- coding: utf-8 -*-
"""lake.ddl —— v6 数据湖 schema（9 表 + 3 view，调研报告 §1 **逐字一致**）。

⚠️ TL 强制要求（tl_decisions.md）：本文件的 DDL 必须与 research_report.md §1 的
sql 块逐字一致（含 ``"close"`` 引号修复——DuckDB 1.5.5 中裸别名 ``close`` 是保留字，
hfq/qfq view 解析失败；已修复为 ``"close"``）。``tests/test_lake_ddl.py`` 会**逐语句
真实执行**整个 sql 块（本次调研事故根因 = view 从未被真实跑过）。

幂等：所有 CREATE 加 IF NOT EXISTS（init_schema 可重复调用）。
溯源契约：每表 ``source/fetched_at/data_version`` 三列（继承 v5.2 canonical）。

v6.1（多源资源池，Q5 拍板）：T1-T7 各表追加 ``conflict_src VARCHAR``——跨源数值
分歧时写落选源+摘要（格式 ``sina:8.12|tdx:8.11`` ≤256B），无分歧=NULL。T8/T9 不加
（T8 本地派生无多源、T9 单源）。**view 不变**（不引用 conflict_src）。已存在的旧库
由 :mod:`lake.migrate_conflict_col` 幂等 ALTER 补列。
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# §1 DDL（逐字；仅在各语句前补 IF NOT EXISTS 以幂等）
# ---------------------------------------------------------------------------
DDL_STATEMENTS = [
    # T1 当前快照
    """CREATE TABLE IF NOT EXISTS stock_master(
  ts_code VARCHAR PRIMARY KEY, name VARCHAR, industry_csric2 VARCHAR, industry_name VARCHAR,
  list_date DATE, delist_date DATE, board VARCHAR, is_st TINYINT, st_since DATE,
  soe_flag VARCHAR, soe_basis VARCHAR, source VARCHAR, fetched_at TIMESTAMP, data_version VARCHAR,
  conflict_src VARCHAR)""",   # v6.1：跨源分歧摘要（无分歧=NULL；Q5）
    # T2 raw 原值 + 复权因子分列
    """CREATE TABLE IF NOT EXISTS kline_daily(
  ts_code VARCHAR, date DATE, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
  volume BIGINT, amount DOUBLE, pct_chg DOUBLE, is_st TINYINT, preclose DOUBLE,
  adj_factor DOUBLE,                  -- 除权日事件值，其余交易日前向填充
  source VARCHAR, fetched_at TIMESTAMP, data_version VARCHAR, conflict_src VARCHAR,
  PRIMARY KEY(ts_code, date))""",     # v6.1：conflict_src（Q5）
    # T3
    """CREATE TABLE IF NOT EXISTS valuation_daily(
  ts_code VARCHAR, date DATE, total_mv DOUBLE, float_mv DOUBLE, pe_ttm DOUBLE, pb DOUBLE,
  turnover_pct DOUBLE, ttm_yield_pct DOUBLE, source VARCHAR, fetched_at TIMESTAMP, data_version VARCHAR,
  conflict_src VARCHAR, PRIMARY KEY(ts_code, date))""",   # v6.1：conflict_src（Q5）
    # T4 em_dividend_all.csv 1991→今（事件驱动，无 PK：同 ex_date 可多行）
    """CREATE TABLE IF NOT EXISTS dividend_events(
  ts_code VARCHAR, ex_date DATE, ann_date DATE, period VARCHAR, cash_dps DOUBLE, stk_div DOUBLE,
  source VARCHAR, fetched_at TIMESTAMP, data_version VARCHAR, conflict_src VARCHAR)""",   # v6.1：conflict_src（Q5）；cash_dps=元/股(em dps_pretax 已/10)；stk_div 暂无源→NULL
    # T5 PIT：pub_date 必存
    """CREATE TABLE IF NOT EXISTS fundamentals_quarterly(
  ts_code VARCHAR, period VARCHAR,    -- period=YYYYQn（对齐 BaoStock Q4 基准）
  pub_date DATE, roe_avg DOUBLE, roe_weighted DOUBLE, yoy_pni DOUBLE, npi DOUBLE, ocf DOUBLE,
  gross_margin DOUBLE, liability_pct DOUBLE, source VARCHAR, fetched_at TIMESTAMP, data_version VARCHAR,
  conflict_src VARCHAR, PRIMARY KEY(ts_code, period))""",   # v6.1：conflict_src（Q5）
    # T6 季度；controller_* 暂 NULL（§6-Q1：本期无源，待补源）
    """CREATE TABLE IF NOT EXISTS holders_snapshot(
  ts_code VARCHAR, as_of_date DATE, holder_rank TINYINT, holder_name VARCHAR, hold_ratio DOUBLE,
  share_nature VARCHAR, controller_name VARCHAR, controller_type VARCHAR, controller_ratio DOUBLE,
  source VARCHAR, fetched_at TIMESTAMP, data_version VARCHAR, conflict_src VARCHAR)""",   # v6.1：conflict_src（Q5）
    # T7 四指数 sh000001/sh000300/sz399001/sh000922
    """CREATE TABLE IF NOT EXISTS index_daily(
  index_code VARCHAR, date DATE, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume BIGINT,
  amount DOUBLE,                      -- 腾讯指数行 amount 可能缺→NULL
  source VARCHAR, fetched_at TIMESTAMP, data_version VARCHAR, conflict_src VARCHAR,
  PRIMARY KEY(index_code, date))""",   # v6.1：conflict_src（Q5）
    # T8 EAV：加因子零 schema 变更
    """CREATE TABLE IF NOT EXISTS factor_snapshot(
  ts_code VARCHAR, as_of_date DATE, factor_name VARCHAR, value DOUBLE, params_json VARCHAR,
  source VARCHAR, fetched_at TIMESTAMP, data_version VARCHAR,
  PRIMARY KEY(ts_code, as_of_date, factor_name))""",
    # T9 现值序列起步，历史缺口显式 NULL
    """CREATE TABLE IF NOT EXISTS macro_rf(
  date DATE PRIMARY KEY, yield_pct DOUBLE, source VARCHAR, fetched_at TIMESTAMP, data_version VARCHAR)""",
    # 复权派生 view（不物化）：后复权=raw×af；前复权=raw×(af/该股最新af)
    # ⚠️ "close" 必须带引号（DuckDB 1.5.5 保留字；TL 已修复并实测通过）
    """CREATE VIEW IF NOT EXISTS kline_daily_hfq AS
  SELECT ts_code,date, open*adj_factor open, high*adj_factor high, low*adj_factor low,
         close*adj_factor "close", volume,amount,pct_chg,is_st,adj_factor FROM kline_daily""",
    """CREATE VIEW IF NOT EXISTS kline_daily_qfq AS
  SELECT ts_code,date,
    open*(adj_factor/MAX(adj_factor) OVER(PARTITION BY ts_code)) open,
    high*(adj_factor/MAX(adj_factor) OVER(PARTITION BY ts_code)) high,
    low *(adj_factor/MAX(adj_factor) OVER(PARTITION BY ts_code)) low,
    close*(adj_factor/MAX(adj_factor) OVER(PARTITION BY ts_code)) "close",
    volume,amount,pct_chg,is_st FROM kline_daily""",
    # 每股全景：T1⋈T3(最新)+T8(标量因子)+T4(最近分红)；LEFT JOIN 缺数据→NULL 不丢股
    """CREATE VIEW IF NOT EXISTS stock_panorama AS
  SELECT m.ts_code,m.name,m.industry_csric2,m.industry_name,m.board,m.is_st,m.soe_flag,m.soe_basis,
    v.total_mv,v.float_mv,v.pe_ttm,v.pb,v.turnover_pct,v.ttm_yield_pct,
    (SELECT value FROM factor_snapshot f WHERE f.ts_code=m.ts_code AND f.factor_name='ann_vol_5y' ORDER BY f.as_of_date DESC LIMIT 1) ann_vol_5y,
    (SELECT value FROM factor_snapshot f WHERE f.ts_code=m.ts_code AND f.factor_name='yield_pctile_own_hist' ORDER BY f.as_of_date DESC LIMIT 1) yield_pctile_own_hist,
    (SELECT cash_dps FROM dividend_events d WHERE d.ts_code=m.ts_code ORDER BY d.ex_date DESC LIMIT 1) last_cash_dps,
    (SELECT MAX(ex_date) FROM dividend_events d WHERE d.ts_code=m.ts_code) last_ex_date
  FROM stock_master m LEFT JOIN valuation_daily v ON v.ts_code=m.ts_code AND v.date=(SELECT MAX(date) FROM valuation_daily)""",
]

# §1 索引（点查热列）
INDEX_STATEMENTS = [
    "CREATE INDEX IF NOT EXISTS idx_kline_ts ON kline_daily(ts_code)",
    "CREATE INDEX IF NOT EXISTS idx_valuation_ts ON valuation_daily(ts_code)",
    "CREATE INDEX IF NOT EXISTS idx_factor_ts_name ON factor_snapshot(ts_code, factor_name)",
]

TABLES = [
    "stock_master", "kline_daily", "valuation_daily", "dividend_events",
    "fundamentals_quarterly", "holders_snapshot", "index_daily",
    "factor_snapshot", "macro_rf",
]

# v6.1（Q5）：带 conflict_src 列的表 = T1-T7。T8 factor_snapshot / T9 macro_rf 不加
# （T8 本地派生无多源、T9 单源）。migrate_conflict_col 按本清单幂等 ALTER。
CONFLICT_SRC_TABLES = [
    "stock_master", "kline_daily", "valuation_daily", "dividend_events",
    "fundamentals_quarterly", "holders_snapshot", "index_daily",
]


def init_schema(con) -> None:
    """执行 §1 全部 DDL + view + 索引（幂等，可重复调用）。

    :param con: duckdb Connection。逐语句 execute——任何一条失败立即抛错
        （绝不静默跳过；view 解析错误必须在此暴露，不得留到运行期）。
    """
    for stmt in DDL_STATEMENTS + INDEX_STATEMENTS:
        con.execute(stmt)
