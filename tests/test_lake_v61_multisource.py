# -*- coding: utf-8 -*-
"""test_lake_v61_multisource —— v6.1 多源资源池回归单测（离线 tmp 库 + mock adapter）。

覆盖 brief「测试与交付」要求的全部离线断言：
- source_pool 优先级 / fallback / cross_check 分歧写入（纯函数 + worker 级）；
- baostock socket 超时 patch（patch 后 gettimeout() 非 None + 开关可关 + env 覆盖）；
- migrate_conflict_col 幂等（新库 + 已有旧 schema 库各一遍，行数不变）；
- worker 多源选择逻辑（sina 主源→source='sina'、amount 非 NULL、adj 前向填充、
  conflict_src 有值或 NULL 不报错；tdx 分歧 → conflict_src 非 NULL；sina 挂→fallback 腾讯）；
- Q6 探测两分支（alive / dead，零网络）。

纪律：全离线——mock adapter + monkeypatch BaoStockClient/TencentClient/_run_bs_probe，
零真实网络；库一律 tmp_path，不碰 data/lake/。多源开启靠显式 delenv LAKE_MULTISOURCE
+ 注入 mock 注册表（conftest autouse 默认置 0 → legacy 路径，既有测试零回归）。
"""
from __future__ import annotations

import json
import os
import sys

import pytest

duckdb = pytest.importorskip("duckdb")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
for _p in (REPO_ROOT, SCRIPTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ===========================================================================
# mock adapter（零网络，可控）
# ===========================================================================
class MockAdapter:
    """SourceAdapter mock：available/fetch_* 全由构造参数决定，计数调用次数。

    kline 参数 = dict {"ohlcv":[...], "adj_factor":{..}|None} **或** callable
    (ts_code, start=None, end=None)->dict（模拟取数失败时抛异常）。
    """

    def __init__(self, name, authority, kline=None, adj=None, f10=None, idx=None,
                 avail=True):
        self.name = name
        self.authority = authority
        self._kline = kline
        self._adj = adj
        self._f10 = f10
        self._idx = idx
        self._avail = avail
        self.kline_calls = 0
        self.adj_calls = 0
        self.f10_calls = 0
        self.idx_calls = 0

    def available(self):
        return self._avail

    def fetch_kline(self, ts_code, start=None, end=None):
        self.kline_calls += 1
        if callable(self._kline):
            return self._kline(ts_code, start=start, end=end)
        if self._kline is None:
            raise RuntimeError(f"{self.name} no kline")
        return dict(self._kline)

    def fetch_adj_factor(self, ts_code, start, end):
        self.adj_calls += 1
        return dict(self._adj or {})

    def fetch_f10(self, ts_code):
        self.f10_calls += 1
        return list(self._f10 or [])

    def fetch_index_kline(self, index_code, n=290):
        self.idx_calls += 1
        return list(self._idx or [])


def _set_registry(monkeypatch, mapping):
    """把 source_pool 注册表替换为 mock（resolve_source/get_adapter 直接读它）。"""
    from lake.ingest import source_pool as sp

    monkeypatch.setattr(sp, "_REGISTRY", dict(mapping))


def _seed_db(db_path):
    from lake import conn as lconn

    con = lconn.open(db_path)
    con.close()


# ===========================================================================
# 1) cross_check 纯函数：分歧检测 + 摘要格式 + 阈值 + ≤256B
# ===========================================================================
def test_cross_check_kline_no_divergence():
    from lake.ingest.source_pool import cross_check_kline

    a = [{"date": "2026-09-10", "close": 10.0, "amount": 1000.0},
         {"date": "2026-09-11", "close": 10.5, "amount": 1100.0}]
    b = [{"date": "2026-09-10", "close": 10.01, "amount": 1001.0},
         {"date": "2026-09-11", "close": 10.5, "amount": 1100.0}]
    # close 差 <0.5%、amount 差 <2% → 无分歧
    assert cross_check_kline(a, "sina", b, "tdx", close_pct=0.5, amount_pct=2.0) is None


def test_cross_check_kline_close_divergence():
    from lake.ingest.source_pool import cross_check_kline

    a = [{"date": "2026-09-10", "close": 10.0, "amount": 1000.0}]
    b = [{"date": "2026-09-10", "close": 10.6, "amount": 1000.0}]  # close 差 6% >0.5%
    s = cross_check_kline(a, "sina", b, "tdx", close_pct=0.5, amount_pct=2.0)
    assert s is not None and s.startswith("close:")
    assert "sina:10" in s and "tdx:10.6" in s


def test_cross_check_kline_amount_divergence_and_truncate():
    from lake.ingest.source_pool import cross_check_kline

    a = [{"date": f"2026-09-{i:02d}", "close": 10.0, "amount": 1_000_000.0} for i in range(1, 8)]
    b = [{"date": f"2026-09-{i:02d}", "close": 10.0, "amount": 1_100_000.0} for i in range(1, 8)]  # +10%
    s = cross_check_kline(a, "sina", b, "tdx", close_pct=0.5, amount_pct=2.0)
    assert s is not None and s.startswith("amount:")
    assert len(s) <= 256  # ≤256B 约束


def test_cross_check_kline_no_common_dates_returns_none():
    from lake.ingest.source_pool import cross_check_kline

    a = [{"date": "2026-09-10", "close": 10.0}]
    b = [{"date": "2026-08-01", "close": 99.0}]  # 日期无交集
    assert cross_check_kline(a, "sina", b, "tdx") is None


def test_cross_check_adj_factor_tail_divergence():
    from lake.ingest.source_pool import cross_check_adj_factor

    a = {"2026-09-14": 2.5, "2026-09-15": 2.5}
    b = {"2026-09-14": 2.5, "2026-09-15": 2.6}  # 末因子差 4% >0.5%
    s = cross_check_adj_factor(a, "sina", b, "tdx", pct=0.5)
    assert s is not None and s.startswith("adj:")


def test_cross_check_f10_pp_threshold():
    from lake.ingest.source_pool import cross_check_f10

    a = [{"period": "2026Q2", "roe_weighted": 4.3, "gross_margin": 37.8, "liability_pct": 92.0}]
    b = [{"period": "2026Q2", "roe_weighted": None, "gross_margin": 37.9, "liability_pct": 93.5}]
    # gross_margin 差 0.1pp <1 → 无；liability_pct 差 1.5pp >1 → 有
    s = cross_check_f10(a, "adata_f10", b, "baostock", pp=1.0)
    assert s is not None and "liability_pct" in s and "gross_margin" not in s


# ===========================================================================
# 2) resolve_source：优先级 + fallback（available 过滤）+ 源开关 + 测试隔离门
# ===========================================================================
def test_resolve_source_priority_order(monkeypatch):
    from lake.ingest import source_pool as sp

    sina = MockAdapter("sina", 0, kline={"ohlcv": [], "adj_factor": None})
    tencent = MockAdapter("tencent", 1, kline={"ohlcv": [], "adj_factor": None})
    tdx = MockAdapter("tdx", 3, kline={"ohlcv": [], "adj_factor": None})
    _set_registry(monkeypatch, {"sina": sina, "tencent": tencent, "tdx": tdx})
    monkeypatch.delenv("LAKE_MULTISOURCE", raising=False)
    out = sp.resolve_source("kline_daily", "ohlcv_amount")
    assert [a.name for a in out] == ["sina", "tencent", "tdx"]  # Q1 拍板序


def test_resolve_source_fallback_skips_unavailable(monkeypatch):
    from lake.ingest import source_pool as sp

    sina = MockAdapter("sina", 0, avail=False)  # EU 不可达 → 跳过
    tencent = MockAdapter("tencent", 1, kline={"ohlcv": [], "adj_factor": None})
    _set_registry(monkeypatch, {"sina": sina, "tencent": tencent})
    monkeypatch.delenv("LAKE_MULTISOURCE", raising=False)
    out = sp.resolve_source("kline_daily", "ohlcv_amount")
    assert [a.name for a in out] == ["tencent"]  # fallback 到腾讯


def test_resolve_source_switch_off_excludes(monkeypatch):
    from lake.ingest import source_pool as sp

    sina = MockAdapter("sina", 0, kline={"ohlcv": [], "adj_factor": None})
    tencent = MockAdapter("tencent", 1, kline={"ohlcv": [], "adj_factor": None})
    _set_registry(monkeypatch, {"sina": sina, "tencent": tencent})
    import lake.config as lcfg

    real = lcfg.lake_cfg

    def fake_cfg():
        c = real()
        c["sina_enabled"] = False  # 源开关关 → 新浪被排除
        return c

    monkeypatch.setattr(lcfg, "lake_cfg", fake_cfg)
    monkeypatch.delenv("LAKE_MULTISOURCE", raising=False)
    out = sp.resolve_source("kline_daily", "ohlcv_amount")
    assert [a.name for a in out] == ["tencent"]


def test_resolve_source_test_isolation_gate(monkeypatch):
    from lake.ingest import source_pool as sp

    sina = MockAdapter("sina", 0, kline={"ohlcv": [], "adj_factor": None})
    _set_registry(monkeypatch, {"sina": sina})
    monkeypatch.setenv("LAKE_MULTISOURCE", "0")
    assert sp.resolve_source("kline_daily", "ohlcv_amount") == []  # 强制 legacy


# ===========================================================================
# 3) baostock socket 超时 patch（gettimeout 非 None + 开关可关 + env 覆盖）
# ===========================================================================
def test_socket_timeout_patch_sets_gettimeout(monkeypatch):
    import baostock.common.context as ctx
    from baostock.util.socketutil import SocketUtil
    import screener.data.baostock_client as bsc

    bsc.reset_socket_patch_for_test()

    class FakeSock:
        def __init__(self):
            self._tmo = None

        def settimeout(self, t):
            self._tmo = t

        def gettimeout(self):
            return self._tmo

    fake = FakeSock()
    # stub 原始 connect（避免真实网络）：只把 fake socket 挂到 context.default_socket
    monkeypatch.setattr(SocketUtil, "connect",
                        lambda self: setattr(ctx, "default_socket", fake))
    bsc.BaoStockClient(quota_path=False, socket_timeout=15.0)  # 安装 patch（包装 stub）
    SocketUtil.connect(None)  # 调 patched connect → settimeout(15.0)
    assert fake.gettimeout() is not None and fake.gettimeout() == 15.0


def test_socket_timeout_patch_switch_off(monkeypatch):
    """socket_timeout<=0 → 不安装 patch（开关可关，brief 红线）。"""
    import screener.data.baostock_client as bsc

    bsc.reset_socket_patch_for_test()
    assert bsc._PATCHED is False
    bsc.BaoStockClient(quota_path=False, socket_timeout=0)  # <=0 → 禁用
    assert bsc._PATCHED is False, "socket_timeout<=0 不得安装 patch（开关可关）"
    bsc.reset_socket_patch_for_test()


def test_socket_timeout_env_var_override(monkeypatch):
    """env BS_SOCKET_TIMEOUT_MS 覆盖缺省 15s（screener 层不读 lake.config）。"""
    import screener.data.baostock_client as bsc

    monkeypatch.setenv("BS_SOCKET_TIMEOUT_MS", "7000")
    c = bsc.BaoStockClient(quota_path=False)  # socket_timeout=None → 读 env
    assert c.socket_timeout == 7.0


# ===========================================================================
# 4) migrate_conflict_col 幂等（新库 + 已有旧 schema 库各一遍，行数不变）
# ===========================================================================
def test_migrate_new_db_idempotent(tmp_path):
    from lake import conn as lconn
    from lake.migrate_conflict_col import migrate_conflict_col
    from lake.ddl import CONFLICT_SRC_TABLES

    db = str(tmp_path / "new.duckdb")
    con = lconn.open(db)  # 新库：init_schema 已含 conflict_src（新 DDL）
    try:
        res = migrate_conflict_col(con)
        for t in CONFLICT_SRC_TABLES:
            assert res[t]["added"] is False, f"新库 {t} 不应 added（列已存在）"
            assert res[t]["rows_before"] == res[t]["rows_after"]
        # 再跑一遍 → 仍全 no-op（幂等）
        res2 = migrate_conflict_col(con)
        assert all(res2[t]["added"] is False for t in CONFLICT_SRC_TABLES)
    finally:
        con.close()


def test_migrate_old_schema_db_adds_column(tmp_path):
    """模拟 v6.0.x 旧库（T1-T7 无 conflict_src）→ ALTER 补列 + 行数不变 + 幂等。

    用裸 duckdb 直接建**旧 schema**（无 conflict_src、无 view——v6.0.x 真实形态），
    不走 lconn.open（那会建新 DDL+view，view 依赖 kline_daily 导致无法 DROP 列模拟）。
    """
    import duckdb as _duck

    from lake.migrate_conflict_col import migrate_conflict_col
    from lake.ddl import CONFLICT_SRC_TABLES

    db = str(tmp_path / "old.duckdb")
    # v6.0.x 旧 DDL（T1-T7 无 conflict_src；只建迁移涉及的表，零 view）
    old_ddl = {
        "stock_master": ("ts_code VARCHAR PRIMARY KEY, name VARCHAR, industry_csric2 VARCHAR,"
                         " industry_name VARCHAR, list_date DATE, delist_date DATE, board VARCHAR,"
                         " is_st TINYINT, st_since DATE, soe_flag VARCHAR, soe_basis VARCHAR,"
                         " source VARCHAR, fetched_at TIMESTAMP, data_version VARCHAR"),
        "kline_daily": ("ts_code VARCHAR, date DATE, open DOUBLE, high DOUBLE, low DOUBLE,"
                        " close DOUBLE, volume BIGINT, amount DOUBLE, pct_chg DOUBLE, is_st TINYINT,"
                        " preclose DOUBLE, adj_factor DOUBLE, source VARCHAR, fetched_at TIMESTAMP,"
                        " data_version VARCHAR, PRIMARY KEY(ts_code, date)"),
        "valuation_daily": ("ts_code VARCHAR, date DATE, total_mv DOUBLE, float_mv DOUBLE,"
                            " pe_ttm DOUBLE, pb DOUBLE, turnover_pct DOUBLE, ttm_yield_pct DOUBLE,"
                            " source VARCHAR, fetched_at TIMESTAMP, data_version VARCHAR,"
                            " PRIMARY KEY(ts_code, date)"),
        "dividend_events": ("ts_code VARCHAR, ex_date DATE, ann_date DATE, period VARCHAR,"
                            " cash_dps DOUBLE, stk_div DOUBLE, source VARCHAR, fetched_at TIMESTAMP,"
                            " data_version VARCHAR"),
        "fundamentals_quarterly": ("ts_code VARCHAR, period VARCHAR, pub_date DATE, roe_avg DOUBLE,"
                                   " roe_weighted DOUBLE, yoy_pni DOUBLE, npi DOUBLE, ocf DOUBLE,"
                                   " gross_margin DOUBLE, liability_pct DOUBLE, source VARCHAR,"
                                   " fetched_at TIMESTAMP, data_version VARCHAR,"
                                   " PRIMARY KEY(ts_code, period)"),
        "holders_snapshot": ("ts_code VARCHAR, as_of_date DATE, holder_rank TINYINT, holder_name VARCHAR,"
                             " hold_ratio DOUBLE, share_nature VARCHAR, controller_name VARCHAR,"
                             " controller_type VARCHAR, controller_ratio DOUBLE, source VARCHAR,"
                             " fetched_at TIMESTAMP, data_version VARCHAR"),
        "index_daily": ("index_code VARCHAR, date DATE, open DOUBLE, high DOUBLE, low DOUBLE,"
                        " close DOUBLE, volume BIGINT, amount DOUBLE, source VARCHAR,"
                        " fetched_at TIMESTAMP, data_version VARCHAR, PRIMARY KEY(index_code, date)"),
    }
    con = _duck.connect(db)
    try:
        for t in CONFLICT_SRC_TABLES:
            con.execute(f"CREATE TABLE {t}({old_ddl[t]})")
        # 灌一行 kline_daily（此时无 conflict_src 列）
        con.execute(
            "INSERT INTO kline_daily (ts_code,date,open,high,low,close,volume,amount,"
            "pct_chg,is_st,preclose,adj_factor,source,fetched_at,data_version) VALUES "
            "('sh.601398','2026-09-10',10,11,9,10.5,1000,10000,0.5,0,10.0,2.5,'tencent',"
            "'2026-09-10 00:00:00','v6.0')")
        before = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in CONFLICT_SRC_TABLES}

        res = migrate_conflict_col(con)
        # 旧库：7 表全 added=True（列被补上）
        assert all(res[t]["added"] is True for t in CONFLICT_SRC_TABLES), f"{res}"
        # kline_daily 行数不变（只加列不碰数据）
        after = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                 for t in CONFLICT_SRC_TABLES}
        assert before == after, f"行数必须一致: {before} vs {after}"
        assert after["kline_daily"] == 1

        # 列确实补上了
        cols = [r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='kline_daily'").fetchall()]
        assert "conflict_src" in cols

        # 幂等：再跑一遍 → 全 no-op（added=False），行数仍不变
        res2 = migrate_conflict_col(con)
        assert all(res2[t]["added"] is False for t in CONFLICT_SRC_TABLES)
        assert con.execute("SELECT COUNT(*) FROM kline_daily").fetchone()[0] == 1
    finally:
        con.close()


# ===========================================================================
# 5) worker 多源选择逻辑（run_history：sina 主源 / fallback / conflict / done）
# ===========================================================================
def _run_history_ms(monkeypatch, tmp_path, db, codes, registry,
                    start="1990-01-01", end="2026-09-15"):
    """离线跑多源 run_history：mock 注册表 + fake BaoStock/Tencent + 跳过 Q6 探测。"""
    import lake_backfill as drv
    from lake import backfill as lb
    from lake.ingest import source_pool as sp
    import screener.data.baostock_client as bsc
    import screener.data.tencent as tmod

    _set_registry(monkeypatch, registry)
    monkeypatch.delenv("LAKE_MULTISOURCE", raising=False)  # 多源开启
    # Q6 探测跳过（零网络）——签名带 db_path（v6.1 隔离修复后按库目录派生 progress）
    monkeypatch.setattr(drv, "_run_bs_probe",
                        lambda db_path=None: {"enabled": False, "alive": False,
                                              "detail": "test (no probe)"})
    # 预算门 + progress 重定向 tmp（零生产副作用）
    monkeypatch.setattr(lb.BackfillRunner, "_quota_state", lambda self: (False, 0))
    monkeypatch.setattr(lb, "_progress_path", lambda: str(tmp_path / "progress.json"))

    class _FakeBS:
        def __init__(self, *a, **k):
            pass

        def close(self):
            pass

    class _FakeTClient:
        pass

    monkeypatch.setattr(bsc, "BaoStockClient", _FakeBS)
    monkeypatch.setattr(tmod, "TencentClient", _FakeTClient)

    from lake import conn as lconn

    con = lconn.open(db)
    try:
        return drv.run_history(con, db, codes, start, end)
    finally:
        con.close()


def test_worker_sina_primary_source_amount_adj_conflict_null(tmp_path, monkeypatch):
    """sina 主源成功：source='sina'、amount 非 NULL、adj 前向填充、conflict_src=NULL。"""
    db = str(tmp_path / "ms_ok.duckdb")
    _seed_db(db)

    days = ["2026-09-10", "2026-09-11", "2026-09-14", "2026-09-15"]
    sina_kline = {
        "ohlcv": [{"date": d, "open": 10.0, "high": 11.0, "low": 9.5,
                   "close": 10.5, "volume": 1_000_000, "amount": 10_500_000.0}
                  for d in days],
        # adj 事件值：2026-09-14 除权 af=2.5（前向填充 → 09-14/09-15 = 2.5）
        "adj_factor": {"2026-09-14": 2.5},
    }
    sina = MockAdapter("sina", 0, kline=sina_kline)
    # tdx 验证源：与 sina 一致（close/amount 相同）→ conflict=NULL
    tdx = MockAdapter("tdx", 3, kline={"ohlcv": [dict(r) for r in sina_kline["ohlcv"]],
                                       "adj_factor": {"2026-09-14": 2.5}})
    stats = _run_history_ms(monkeypatch, tmp_path, db, ["sh.601398"],
                            {"sina": sina, "tdx": tdx})

    assert stats["processed"] == 1 and not stats["errors"], f"{stats}"
    assert stats["multisource"] is True and stats["ohlcv_sources"][0] == "sina"
    assert sina.kline_calls >= 1  # 新浪主源被调用

    from lake import conn as lconn

    con = lconn.open(db)
    rows = con.execute(
        "SELECT date, close, amount, adj_factor, source, conflict_src FROM kline_daily "
        "WHERE ts_code='sh.601398' ORDER BY date").fetchall()
    con.close()
    assert len(rows) == 4
    for r in rows:
        assert r[4] == "sina", f"source 应为 sina: {r}"
        assert r[2] is not None, f"amount 应非 NULL: {r}"
    # adj 前向填充：09-10/09-11 = None（首个事件前），09-14/09-15 = 2.5
    assert rows[0][3] is None and rows[1][3] is None
    assert rows[2][3] == 2.5 and rows[3][3] == 2.5
    # tdx 与 sina 一致 → conflict_src=NULL（不报错）
    assert all(r[5] is None for r in rows), f"无分歧应 conflict_src=NULL: {rows}"


def test_worker_tdx_divergence_writes_conflict(tmp_path, monkeypatch):
    """sina 主源 + tdx close 分歧 >0.5% → conflict_src 非 NULL（不阻断、取主源值）。"""
    db = str(tmp_path / "ms_conf.duckdb")
    _seed_db(db)

    days = ["2026-09-14", "2026-09-15"]
    sina_kline = {
        "ohlcv": [{"date": d, "open": 10.0, "high": 11.0, "low": 9.5,
                   "close": 10.5, "volume": 1_000_000, "amount": 10_500_000.0}
                  for d in days],
        "adj_factor": {"2026-09-14": 2.5},
    }
    sina = MockAdapter("sina", 0, kline=sina_kline)
    # tdx close 差 6%（10.5 vs 11.1）>0.5% → 分歧
    tdx_rows = [{"date": d, "open": 10.0, "high": 11.0, "low": 9.5,
                 "close": 11.1, "volume": 1_000_000, "amount": 10_500_000.0} for d in days]
    tdx = MockAdapter("tdx", 3, kline={"ohlcv": tdx_rows, "adj_factor": {"2026-09-14": 2.5}})
    stats = _run_history_ms(monkeypatch, tmp_path, db, ["sh.601398"],
                            {"sina": sina, "tdx": tdx})
    assert stats["processed"] == 1 and not stats["errors"], f"{stats}"

    from lake import conn as lconn

    con = lconn.open(db)
    rows = con.execute(
        "SELECT date, close, source, conflict_src FROM kline_daily "
        "WHERE ts_code='sh.601398' ORDER BY date").fetchall()
    con.close()
    # 主源值仍=sina（不阻断）；conflict_src 记录分歧
    assert all(r[2] == "sina" for r in rows)
    assert all(r[3] is not None and "close:" in r[3] for r in rows), f"{rows}"


def test_worker_fallback_to_tencent_when_sina_down(tmp_path, monkeypatch):
    """sina 取数失败 → fallback 腾讯（source='tencent'，volume 手→股 ×100）。"""
    db = str(tmp_path / "ms_fb.duckdb")
    _seed_db(db)

    days = ["2026-09-14", "2026-09-15"]

    def sina_boom(ts, start=None, end=None):
        raise RuntimeError("sina EU down")

    sina = MockAdapter("sina", 0, kline=sina_boom)
    # 腾讯 K线：volume=手（load_t2 volume_is_shares=False → ×100）
    tencent_kline = {"ohlcv": [{"date": d, "open": 10.0, "high": 11.0, "low": 9.5,
                                "close": 10.5, "volume": 10_000.0, "amount": None}
                               for d in days],
                     "adj_factor": None}
    tencent = MockAdapter("tencent", 1, kline=tencent_kline)
    stats = _run_history_ms(monkeypatch, tmp_path, db, ["sh.601398"],
                            {"sina": sina, "tencent": tencent})
    assert stats["processed"] == 1 and not stats["errors"], f"{stats}"

    from lake import conn as lconn

    con = lconn.open(db)
    rows = con.execute(
        "SELECT date, close, volume, source FROM kline_daily "
        "WHERE ts_code='sh.601398' ORDER BY date").fetchall()
    con.close()
    assert all(r[3] == "tencent" for r in rows), f"应 fallback 腾讯: {rows}"
    assert all(r[2] == 1_000_000 for r in rows), f"volume 手→股 ×100: {rows}"


def test_worker_all_sources_down_raises_not_done(tmp_path, monkeypatch):
    """所有源失败 → worker 抛错**不 mark_done**（下轮重试，防 done 键毒化）。"""
    db = str(tmp_path / "ms_allfail.duckdb")
    _seed_db(db)

    def boom(ts, start=None, end=None):
        raise RuntimeError("down")

    sina = MockAdapter("sina", 0, kline=boom)
    tencent = MockAdapter("tencent", 1, kline=boom)
    stats = _run_history_ms(monkeypatch, tmp_path, db, ["sh.601398"],
                            {"sina": sina, "tencent": tencent})
    assert stats["processed"] == 0 and len(stats["errors"]) == 1
    prog = json.load(open(str(tmp_path / "progress.json"), encoding="utf-8"))
    kh_done = [k for k in prog.get("done", []) if k[0] == "kline_history"]
    assert kh_done == [], f"失败不得标 done: {kh_done}"


# ===========================================================================
# 6) Q6 探测两分支（alive / dead，零网络）+ 进程内共享状态
# ===========================================================================
def test_q6_probe_alive(monkeypatch):
    import screener.data.baostock_client as bsc

    class FakeClient:
        def __init__(self, **k):
            self.k = k

        def call_with_fields(self, qf, label=""):
            return (["code"], [["sh.601398"], ["sz.000001"]])

        def close(self):
            pass

    monkeypatch.setattr(bsc, "BaoStockClient", FakeClient)
    res = bsc.probe_baostock_alive(timeout_s=10.0)
    assert res["alive"] is True and "rows=" in res["detail"]


def test_q6_probe_dead(monkeypatch):
    import screener.data.baostock_client as bsc

    class FakeClient:
        def __init__(self, **k):
            pass

        def call_with_fields(self, qf, label=""):
            raise RuntimeError("baostock login 失败")

        def close(self):
            pass

    monkeypatch.setattr(bsc, "BaoStockClient", FakeClient)
    res = bsc.probe_baostock_alive(timeout_s=10.0)
    assert res["alive"] is False


def test_q6_probe_sets_shared_state(monkeypatch):
    from lake.ingest.source_pool import (baostock_alive, set_baostock_alive,
                                         reset_baostock_state_for_test)

    reset_baostock_state_for_test()
    assert baostock_alive() is False  # 未探测 → 保守按死
    set_baostock_alive(True, "ok")
    assert baostock_alive() is True
    reset_baostock_state_for_test()
