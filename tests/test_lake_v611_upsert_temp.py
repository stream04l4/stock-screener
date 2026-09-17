# -*- coding: utf-8 -*-
"""test_lake_v611_upsert_temp —— v6.1.1 FIX-1（upsert 临时表批量冲突）防回归单测。

FIX-1 把 ``lake.ingest.common.upsert`` 从逐行 ``executemany(INSERT OR REPLACE ...
VALUES)`` 改为"TEMP 表普通 INSERT + INSERT OR REPLACE ... SELECT * FROM _up_tmp"
（768 万行大表上单股全史 29.0s → ~4.1s，~7×）。**语义必须与逐行路径逐字节等价**——
本文件是等价性防线（brief REG-1）：

- (a) **全表哈希一致**：同数据分别走"逐行路径（v6.1.0 基线，测试内联实现）"与
  "temp 表路径（生产 upsert）"→ 两库 kline_daily 全表 md5 + 行数一致。含预置旧行
  （REPLACE 覆盖/部分列不写保留旧值）+ conflict_src 三态混合批；
- (b) **conflict_src 三态在 temp 路径下行为不变**（None=不写列 / 字符串=摘要 /
  write_null()=显式 NULL）——v6.1 DEF-1 既有测试（test_lake_v61_def1_p0_t2.py::
  test_upsert_sentinel_three_states）零改动通过是主防线，此处再对 temp 路径直接断言；
- (c) **upsert 中途异常时临时表被清理**：类型错行 → 原始 ConversionException 上抛 +
  con 无残留 _up_tmp* + 连接仍可用；
- 批内重复 PK = last-wins（逐行 executemany 顺序覆盖语义；REPLACE...SELECT 对源内
  重复键不确定，生产实现 Python 侧按 PK 去重保最后一行）。

行业回填（FIX-2 REG-1）：``backfill_industry_csric2`` 独立函数直接测（tmp 库、零网络）——
"csric2 空 + name 有前缀"→回填；"name 无前缀"→保持 NULL（不是空串）；幂等二次调用=0。

纪律：conftest autouse 默认 LAKE_MULTISOURCE=0（本文件不触网）；库一律 tmp_path。
"""
from __future__ import annotations

import hashlib
import os
import random
import sys

import pytest

duckdb = pytest.importorskip("duckdb")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
for _p in (REPO_ROOT, SCRIPTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ===========================================================================
# 工具：逐行路径基线（v6.1.0 原实现，等价性对照用）+ 全表哈希
# ===========================================================================
def _upsert_rowbyrow(con, table, columns, rows, conflict_src=None):
    """v6.1.0 legacy upsert（逐行 executemany INSERT OR REPLACE VALUES）——基线。

    与生产 upsert 的冲突_src 拼列逻辑逐字一致（None=不写列 / 字符串=摘要 /
    write_null() 哨兵=显式 NULL），仅执行路径不同（VALUES vs temp SELECT）。
    """
    from lake.ingest.common import _WriteNull

    rows = [list(r) for r in rows]
    if not rows:
        return 0
    cols = list(columns)
    if conflict_src is not None:
        cols = cols + ["conflict_src"]
        val = None if isinstance(conflict_src, _WriteNull) else conflict_src
        rows = [r + [val] for r in rows]
    cols_sql = ", ".join(f'"{c}"' for c in cols)
    placeholders = ", ".join(["?"] * len(cols))
    con.executemany(
        f"INSERT OR REPLACE INTO {table} ({cols_sql}) VALUES ({placeholders})", rows)
    return len(rows)


def _table_md5(con, table):
    """全表有序行 → md5（+行数）——A/C 等价性对照口径（同 TL 库副本验证）。"""
    rows = con.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall()
    return hashlib.md5(repr(rows).encode("utf-8")).hexdigest(), len(rows)


KLINE_COLS = ["ts_code", "date", "open", "high", "low", "close", "volume",
              "amount", "pct_chg", "is_st", "preclose", "adj_factor",
              "source", "fetched_at", "data_version"]


def _make_batch(n_codes=8, n_days=250, seed=42):
    """确定性混合批：多 code × 多 date（含 NULL/摘要/write_null 三态混排）。"""
    rng = random.Random(seed)
    rows = []
    for i in range(n_codes * n_days):
        code = f"sh.{600000 + (i % n_codes):06d}" if i % 3 else f"sz.{i % 5:06d}"
        d = f"2026-{(i // 10) % 12 + 1:02d}-{(i % 28) + 1:02d}"
        close = round(rng.uniform(2, 60), 2)
        # conflict_src 三态混排（约 1/3 无分歧 NULL、1/3 摘要、1/3 不写列）
        mode = i % 3
        cs = None if mode == 0 else (f"close:sina:{close}|tdx:{round(close*1.01,2)}"
                                     if mode == 1 else "__OMIT__")
        rows.append({
            "row": [code, d, close - 0.3, close + 0.4, close - 0.5, close,
                    rng.randint(10_000, 9_000_000),
                    None if i % 7 == 0 else round(close * rng.randint(10_000, 9_000_000) / 100, 2),
                    None, 0 if i % 5 else 1, None,
                    None if i % 4 == 0 else round(rng.uniform(1, 3), 4),
                    "sina" if i % 2 else "tencent",
                    "2026-09-17 00:00:00", "v6.0"],
            "cs": cs,
        })
    return rows


def _seed_stale(con, code):
    """预置旧行（带陈旧 conflict_src + 不同 source）——验证 REPLACE 覆盖/保留语义。"""
    con.execute(
        "INSERT OR REPLACE INTO kline_daily (ts_code,date,open,high,low,close,"
        "volume,amount,pct_chg,is_st,preclose,adj_factor,source,fetched_at,"
        "data_version,conflict_src) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [code, "2026-01-05", 9.0, 9.5, 8.8, 9.2, 5_000_000, 4_000_000.0, None, 0,
         None, 1.8, "tencent", "2026-01-05 00:00:00", "v6.0", "STALE:old|summary"])


def _apply(con, table, batch, use_temp_path):
    """把混合批写入库：按三态拆分（不写列的行不带 conflict_src 参数）。"""
    from lake.ingest.common import upsert, write_null

    for item in batch:
        cs = item["cs"]
        if cs == "__OMIT__":
            f = upsert if use_temp_path else _upsert_rowbyrow
            f(con, table, KLINE_COLS, [item["row"]])  # None=不写列
        elif cs is None:
            f = upsert if use_temp_path else _upsert_rowbyrow
            f(con, table, KLINE_COLS, [item["row"]], conflict_src=write_null())
        else:
            f = upsert if use_temp_path else _upsert_rowbyrow
            f(con, table, KLINE_COLS, [item["row"]], conflict_src=cs)


# ===========================================================================
# (a) 全表哈希一致：逐行路径 vs temp 表路径（小库 + 两连接，同数据）
# ===========================================================================
def test_upsert_temp_path_full_table_hash_equivalence(tmp_path):
    from lake import conn as lconn

    db_a = str(tmp_path / "rowbyrow.duckdb")   # A：逐行路径（v6.1.0 基线）
    db_c = str(tmp_path / "temppath.duckdb")   # C：temp 表路径（v6.1.1 生产实现）
    con_a, con_c = lconn.open(db_a), lconn.open(db_c)
    try:
        # 预置旧行（两库同种子数据）——REPLACE 覆盖/部分列保留旧值都要一致
        for con in (con_a, con_c):
            _seed_stale(con, "sh.600001")
            _seed_stale(con, "sz.000003")

        batch = _make_batch()
        # 批内注入与预置旧行同 PK 的行（验证 REPLACE 覆盖语义一致）
        for con, use_temp in ((con_a, False), (con_c, True)):
            _apply(con, "kline_daily", batch, use_temp)

        h_a, n_a = _table_md5(con_a, "kline_daily")
        h_c, n_c = _table_md5(con_c, "kline_daily")
        assert n_a == n_c, f"行数不一致: A={n_a} C={n_c}"
        assert h_a == h_c, f"全表 md5 不一致: A={h_a} C={h_c}"
    finally:
        con_a.close()
        con_c.close()


def test_upsert_temp_path_multibatch_hash_equivalence(tmp_path):
    """多批连续 upsert（同 PK 跨批覆盖 + 三态混排）→ 两路径终态哈希一致。

    模拟 history 灌数形态：同一 code 分多批写入（窗口增量），每批内无重复 PK、
    批间同 PK 覆盖——验证 DESCRIBE 缓存/临时表反复建删不破坏等价性。
    """
    from lake import conn as lconn

    db_a = str(tmp_path / "mb_rowbyrow.duckdb")
    db_c = str(tmp_path / "mb_temp.duckdb")
    con_a, con_c = lconn.open(db_a), lconn.open(db_c)
    try:
        batch = _make_batch(n_codes=4, n_days=120)
        for i in range(0, len(batch), 50):  # 50 行/批，多批覆盖
            sub = batch[i:i + 50]
            _apply(con_a, "kline_daily", sub, use_temp_path=False)
            _apply(con_c, "kline_daily", sub, use_temp_path=True)
        h_a, n_a = _table_md5(con_a, "kline_daily")
        h_c, n_c = _table_md5(con_c, "kline_daily")
        assert (n_a, h_a) == (n_c, h_c), f"多批终态不一致: A=({n_a},{h_a}) C=({n_c},{h_c})"
    finally:
        con_a.close()
        con_c.close()


# ===========================================================================
# (b) conflict_src 三态在 temp 表路径下行为不变（直接断言）
# ===========================================================================
def test_upsert_conflict_src_three_states_temp_path(tmp_path):
    from lake import conn as lconn
    from lake.ingest.common import upsert, write_null

    db = str(tmp_path / "three_states.duckdb")
    con = lconn.open(db)
    try:
        row = lambda src: ["sh.601398", "2026-09-14", 10.0, 11.0, 9.5, 10.5, 1000,
                           1_000_000.0, None, 0, None, 2.5, src,
                           "2026-09-14 00:00:00", "v6.0"]

        # (a) 字符串摘要 → 写入
        upsert(con, "kline_daily", KLINE_COLS, [row("sina")],
               conflict_src="close:sina:10|tdx:11")
        assert con.execute("SELECT conflict_src FROM kline_daily").fetchone()[0] \
            == "close:sina:10|tdx:11"

        # (b) write_null() 哨兵 → 显式 NULL（清掉旧摘要）
        upsert(con, "kline_daily", KLINE_COLS, [row("sina")], conflict_src=write_null())
        assert con.execute("SELECT conflict_src FROM kline_daily").fetchone()[0] is None

        # (c) 缺省 None → 不写列（REPLACE 保留旧值，向后兼容）
        upsert(con, "kline_daily", KLINE_COLS, [row("sina")],
               conflict_src="stale:summary")
        upsert(con, "kline_daily", KLINE_COLS, [row("tencent")])
        assert con.execute("SELECT conflict_src FROM kline_daily").fetchone()[0] \
            == "stale:summary"

        # (d) 部分列（不含 PK 全列）→ NOT NULL 约束拒绝（与逐行路径同错，实测一致）
        with pytest.raises(duckdb.ConstraintException):
            upsert(con, "kline_daily", ["date", "close"],
                   [["2026-09-15", 9.9]])
    finally:
        con.close()


# ===========================================================================
# (c) upsert 中途异常 → 临时表清理 + 原始异常上抛 + 连接可用
# ===========================================================================
def test_upsert_exception_cleans_temp_table(tmp_path):
    from lake import conn as lconn
    from lake.ingest.common import upsert

    db = str(tmp_path / "exc_cleanup.duckdb")
    con = lconn.open(db)
    try:
        bad_row = ["sh.601398", "2026-09-14", 10.0, 11.0, 9.5, "NOT_A_NUMBER", 1000,
                   None, None, 0, None, None, "sina", "2026-09-14 00:00:00", "v6.0"]
        with pytest.raises(duckdb.ConversionException):
            upsert(con, "kline_daily", KLINE_COLS, [bad_row])

        # 无残留 _up_tmp*（SHOW ALL TABLES 含 temp schema）
        leftover = [r[2] for r in con.execute("SHOW ALL TABLES").fetchall()
                    if r[2].startswith("_up_tmp")]
        assert leftover == [], f"异常后残留临时表: {leftover}"

        # 连接仍可用（后续写入正常）
        upsert(con, "kline_daily", KLINE_COLS,
               [["sz.000001", "2026-09-14", 5.0, 5.5, 4.8, 5.2, 1000, None,
                 None, 0, None, None, "sina", "2026-09-14 00:00:00", "v6.0"]])
        assert con.execute("SELECT COUNT(*) FROM kline_daily").fetchone()[0] == 1

        # 连续多次异常 → 每次都有新随机后缀 temp 表、全部清理（不撞名）
        for _ in range(3):
            with pytest.raises(duckdb.ConversionException):
                upsert(con, "kline_daily", KLINE_COLS, [bad_row])
        leftover = [r[2] for r in con.execute("SHOW ALL TABLES").fetchall()
                    if r[2].startswith("_up_tmp")]
        assert leftover == [], f"多次异常后残留临时表: {leftover}"
    finally:
        con.close()


# ===========================================================================
# 批内重复 PK = last-wins（逐行 executemany 顺序覆盖语义）
# ===========================================================================
def test_upsert_intra_batch_dup_key_last_wins(tmp_path):
    from lake import conn as lconn
    from lake.ingest.common import upsert

    db = str(tmp_path / "dupkey.duckdb")
    con = lconn.open(db)
    try:
        # 同 PK（ts_code+date）两行：后行 close=2.0/tencent 应胜出（=逐行路径语义）
        upsert(con, "kline_daily", KLINE_COLS, [
            ["sh.601398", "2026-09-14", 1.0, 1.5, 0.8, 1.2, 1000, None,
             None, 0, None, None, "sina", "2026-09-14 00:00:00", "v6.0"],
            ["sh.601398", "2026-09-14", 2.0, 2.5, 1.8, 2.2, 2000, None,
             None, 0, None, None, "tencent", "2026-09-14 00:00:00", "v6.0"],
        ])
        r = con.execute(
            "SELECT close, source FROM kline_daily WHERE ts_code='sh.601398'").fetchone()
        # 后行 close=2.2（KLINE_COLS 第 5 位）/ source=tencent 胜出
        assert r == (2.2, "tencent"), f"批内重复键应 last-wins: {r}"
        # 返回行数 = 传入行数（调用方口径不变）
        n = upsert(con, "kline_daily", KLINE_COLS, [
            ["sh.601398", "2026-09-14", 3.0, 3.5, 2.8, 3.2, 3000, None,
             None, 0, None, None, "sina", "2026-09-14 00:00:00", "v6.0"],
            ["sh.601398", "2026-09-15", 4.0, 4.5, 3.8, 4.2, 4000, None,
             None, 0, None, None, "sina", "2026-09-14 00:00:00", "v6.0"],
        ])
        assert n == 2
    finally:
        con.close()


# ===========================================================================
# FIX-2 REG-1：industry_csric2 自愈回填（tmp 库、零网络）
# ===========================================================================
def _seed_stock_master(con, rows):
    """rows = [(ts_code, csric2_or_None, name_or_None)]。"""
    for ts, cs, name in rows:
        con.execute(
            "INSERT OR REPLACE INTO stock_master (ts_code,name,industry_csric2,"
            "industry_name) VALUES (?,?,?,?)", [ts, None, cs, name])


def test_backfill_industry_csric2_extracts_prefix(tmp_path):
    import lake_backfill as drv
    from lake import conn as lconn

    db = str(tmp_path / "ind_heal.duckdb")
    con = lconn.open(db)
    try:
        _seed_stock_master(con, [
            ("sh.601398", None, "C39计算机、通信和其他电子设备制造业"),  # 有前缀 → C39
            ("sz.000001", None, "B06煤炭开采和洗选业"),                # 有前缀 → B06
            ("sh.600028", "", "E48土木工程建筑业"),                    # 空串 csric2 → E48
            ("sz.000002", None, "某行业无代码前缀"),                   # 无前缀 → 保持 NULL
            ("sh.600036", None, None),                                # name NULL → 不碰
            ("sz.000003", "J66", "J66货币金融服务"),                   # 已有值 → 不碰（幂等）
        ])
        n = drv.backfill_industry_csric2(con)
        assert n == 3, f"应回填 3 行（有前缀+csric2空）: {n}"

        got = {r[0]: r[1] for r in con.execute(
            "SELECT ts_code, industry_csric2 FROM stock_master ORDER BY ts_code").fetchall()}
        assert got["sh.601398"] == "C39"
        assert got["sz.000001"] == "B06"
        assert got["sh.600028"] == "E48"
        # 无前缀 → **保持 NULL**（不是空串——regexp_extract 无匹配返 ''，必须挡住）
        assert got["sz.000002"] is None, f"无前缀行应保持 NULL: {got['sz.000002']!r}"
        assert got["sh.600036"] is None
        # 已有值不碰（幂等）
        assert got["sz.000003"] == "J66"

        # 二次调用 → 0（幂等：已非空的行不再回填）
        assert drv.backfill_industry_csric2(con) == 0
    finally:
        con.close()


def test_backfill_industry_csric2_noop_when_all_filled(tmp_path):
    """全部已有 csric2 → 零 UPDATE（no-op，不碰任何行）。"""
    import lake_backfill as drv
    from lake import conn as lconn

    db = str(tmp_path / "ind_full.duckdb")
    con = lconn.open(db)
    try:
        _seed_stock_master(con, [
            ("sh.601398", "C39", "C39计算机、通信和其他电子设备制造业"),
            ("sz.000001", "B06", "B06煤炭开采和洗选业"),
        ])
        assert drv.backfill_industry_csric2(con) == 0
        got = {r[0]: r[1] for r in con.execute(
            "SELECT ts_code, industry_csric2 FROM stock_master").fetchall()}
        assert got == {"sh.601398": "C39", "sz.000001": "B06"}
    finally:
        con.close()
