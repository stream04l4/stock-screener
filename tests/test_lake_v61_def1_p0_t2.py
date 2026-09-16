# -*- coding: utf-8 -*-
"""test_lake_v61_def1_p0_t2 —— v6.1 DEF-1 修复回归单测（p0 T2 多源化，离线零网络）。

DEF-1（tester §12）：p0 T2 日线增量路径曾是 v6.0.x legacy 腾讯单源——对已
history-done 的股执行 p0 会用 source=tencent/amount=NULL/adj_factor=NULL 覆盖
多源写入的 sina 行，且 upsert 残留陈旧 conflict_src。修复后 run_t2 镜像
run_history 的多源 worker（窗口化：只 upsert 最近 (days+30) 自然日窗口）。

本文件覆盖 brief「测试」全部要求（mock adapter + tmp 库，零真实网络）：
- p0 T2 多源：已 history-done 股跑 p0 T2 → 新日期行 source=sina、amount/adj 非 NULL；
- 不降级：窗口外旧 sina 行不被重写；对照未覆盖股零退化；
- conflict_src 清除：有分歧写摘要 → 无分歧 REPLACE 后=NULL（无陈旧残留）；
- legacy 回退：LAKE_MULTISOURCE=0 → p0 T2 走腾讯单源（v6.0.x 行为，零回归）;
- 全源失败 → worker 抛错不 mark_done（防 done 键毒化）；
- upsert 哨兵三态（None=不写列 / write_null()=显式 NULL / 字符串=摘要），
  T8/T9 无 conflict_src 列的调用方零影响。

纪律：conftest autouse 默认 LAKE_MULTISOURCE=0；多源用例显式 delenv + 注入 mock
注册表（同 test_lake_v61_multisource 风格）。库一律 tmp_path，不碰 data/lake/。
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
# mock adapter（零网络，可控；同 test_lake_v61_multisource 风格）
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


# ===========================================================================
# p0 T2 离线驱动（--skip-t1，fake TencentClient，progress/配额全隔离 tmp）
# ===========================================================================
def _make_fake_env(monkeypatch, tmp_path, prog_path):
    """p0 离线环境：BackfillRunner 配额门 + progress 重定向 + TencentClient fake。

    T4（本地静态 csv）零网络；T7/T3 的腾讯调用打到 _FakeTClient / fake fetch_snapshot
    （无 session → 零真实网络）。⚠️ T7 的 tdx amount 补充走 ``sp.get_adapter("tdx")``
    ——**不**受 LAKE_MULTISOURCE=0 门控（get_adapter 只读注册表），故每个用例都必须
    注入 mock/空注册表，否则真实 tdx adapter 会进池发 EU 自检请求（破坏离线契约）。
    """
    import lake_backfill as drv
    from lake import backfill as lb

    monkeypatch.setattr(lb.BackfillRunner, "_quota_state", lambda self: (False, 0))
    monkeypatch.setattr(lb, "_progress_path", lambda: prog_path)

    class _FakeTClient:
        pass

    import screener.data.tencent as tmod

    monkeypatch.setattr(tmod, "TencentClient", _FakeTClient)

    # T3 快照 fake（零网络；返回 {} → T3 worker 无行可写，任务正常完成不报错）
    import lake.ingest.tencent_ingest as ti

    monkeypatch.setattr(ti, "fetch_snapshot", lambda client, ts_codes: {})


def _run_p0_t2(monkeypatch, tmp_path, db, days, codes, registry=None):
    """跑 p0 --skip-t1（进程内 main，同 CLI 代码路径）。

    registry=None → legacy 门（LAKE_MULTISOURCE=0）+ **空注册表**（get_adapter 全 None，
    T7 tdx 补充零网络）；dict → 多源开启 + mock 注册表。
    """
    import lake_backfill as drv

    prog = str(tmp_path / f"progress_{days}.json")
    _make_fake_env(monkeypatch, tmp_path, prog)
    if registry is not None:
        _set_registry(monkeypatch, registry)
        monkeypatch.delenv("LAKE_MULTISOURCE", raising=False)  # 多源开启
    else:
        _set_registry(monkeypatch, {})  # 空池：get_adapter→None（T7 tdx 补充离线）
        monkeypatch.setenv("LAKE_MULTISOURCE", "0")  # legacy 门（conftest 已置，双保险）
    rc = drv.main(["--db", db, "p0", "--days", str(days), "--skip-t1",
                   "--codes", ",".join(codes), "--t4-codes", codes[0]])
    assert rc == drv.EXIT_OK, f"p0 应成功退出: {rc}"
    return prog


def _seed_sina_rows(con, code, rows):
    """模拟 history 多源写入的 sina 行（含 amount/adj_factor，可带陈旧 conflict_src）。"""
    for d, close, amount, af, conflict in rows:
        con.execute(
            "INSERT OR REPLACE INTO kline_daily (ts_code,date,open,high,low,close,"
            "volume,amount,pct_chg,is_st,preclose,adj_factor,source,fetched_at,"
            "data_version,conflict_src) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [code, d, close, close + 0.5, close - 0.5, close, 1_000_000, amount,
             None, 0, None, af, "sina", "2026-09-15 00:00:00", "v6.0", conflict])


def _by_source(con, code):
    return {r[0]: r[1] for r in con.execute(
        "SELECT source, COUNT(*) FROM kline_daily WHERE ts_code=? GROUP BY source",
        [code]).fetchall()}


# ===========================================================================
# 1) p0 T2 多源：已 history-done 股 → 新日期行 source=sina、amount/adj 非 NULL
#    + 不降级（窗口外旧行不重写 + 对照未覆盖股零退化）
# ===========================================================================
def test_p0_t2_multisource_sina_no_degradation(tmp_path, monkeypatch):
    db = str(tmp_path / "def1_ms.duckdb")
    from lake import conn as lconn

    con = lconn.open(db)
    # history 已写入：sh.601398（窗口内 2 行 + 窗口外旧行）；sz.000001 对照（未覆盖）
    _seed_sina_rows(con, "sh.601398", [
        ("2025-01-02", 10.0, 1_000_000.0, 2.0, None),   # 窗口外旧行（不应被重写）
        ("2026-09-14", 10.0, 1_000_000.0, 2.5, None),
        ("2026-09-15", 10.2, 1_020_000.0, 2.5, None),
    ])
    _seed_sina_rows(con, "sz.000001", [
        ("2026-09-14", 11.0, 2_000_000.0, 3.0, None),
        ("2026-09-15", 11.1, 2_100_000.0, 3.0, None),
    ])
    con.close()

    # p0 T2：sina 主源返回全史（含窗口外行）——worker 只 upsert 窗口内行
    sina = MockAdapter("sina", 0, kline={
        "ohlcv": [
            {"date": "2025-01-02", "open": 9.0, "high": 9.6, "low": 8.8,
             "close": 10.0, "volume": 900_000, "amount": 900_000.0},   # 窗口外
            {"date": "2026-09-14", "open": 10.0, "high": 11.0, "low": 9.5,
             "close": 10.0, "volume": 1_000_000, "amount": 1_000_000.0},
            {"date": "2026-09-15", "open": 10.1, "high": 10.8, "low": 9.9,
             "close": 10.2, "volume": 1_020_000, "amount": 1_020_000.0},
        ],
        "adj_factor": {"2026-09-14": 2.5},
    })
    # tdx 验证源：与 sina 一致 → conflict=NULL（不阻断）
    tdx = MockAdapter("tdx", 3, kline={
        "ohlcv": [dict(r) for r in sina._kline["ohlcv"]],
        "adj_factor": {"2026-09-14": 2.5}})
    _run_p0_t2(monkeypatch, tmp_path, db, days=40, codes=["sh.601398"],
               registry={"sina": sina, "tdx": tdx})

    con = lconn.open(db)
    try:
        # 核心断言：覆盖窗口行 source=sina、amount/adj_factor 非 NULL（不再是 tencent/NULL）
        rows = {str(r[0]): r for r in con.execute(
            "SELECT date, close, amount, adj_factor, source, conflict_src FROM kline_daily "
            "WHERE ts_code='sh.601398' ORDER BY date").fetchall()}
        assert _by_source(con, "sh.601398") == {"sina": 3}, f"应全为 sina: {rows}"
        for d in ("2026-09-14", "2026-09-15"):
            r = rows[d]
            assert r[4] == "sina", f"新日期行 source 应为 sina: {r}"
            assert r[2] is not None, f"amount 应非 NULL: {r}"
            assert r[3] is not None, f"adj_factor 应非 NULL（前向填充）: {r}"
        # adj 前向填充：09-14/09-15 = 2.5
        assert rows["2026-09-14"][3] == 2.5 and rows["2026-09-15"][3] == 2.5
        # 窗口化：sina 全史返回的 2025-01-02 行**不得**重写（保留原 amount=1e6/af=2.0）
        old = rows["2025-01-02"]
        assert old[2] == 1_000_000.0 and old[3] == 2.0, f"窗口外旧行被重写: {old}"
        # 单源一致 → conflict_src=NULL（无分歧）
        assert all(r[5] is None for r in rows.values()), f"{rows}"
        # 对照未覆盖股 sz.000001：零退化
        ctrl = con.execute(
            "SELECT source, amount, adj_factor FROM kline_daily WHERE ts_code='sz.000001' "
            "ORDER BY date").fetchall()
        assert len(ctrl) == 2 and all(r[0] == "sina" and r[1] is not None
                                      and r[2] is not None for r in ctrl), f"{ctrl}"
    finally:
        con.close()


# ===========================================================================
# 2) conflict_src：有分歧写摘要 → 无分歧 REPLACE 后=NULL（无陈旧残留）
# ===========================================================================
def test_p0_t2_conflict_written_then_cleared(tmp_path, monkeypatch):
    db = str(tmp_path / "def1_conf.duckdb")
    from lake import conn as lconn

    con = lconn.open(db)
    _seed_sina_rows(con, "sh.601398", [
        ("2026-09-14", 10.0, 1_000_000.0, 2.5, None),
        ("2026-09-15", 10.2, 1_020_000.0, 2.5, None),
    ])
    con.close()

    sina = MockAdapter("sina", 0, kline={
        "ohlcv": [
            {"date": "2026-09-14", "open": 10.0, "high": 11.0, "low": 9.5,
             "close": 10.0, "volume": 1_000_000, "amount": 1_000_000.0},
            {"date": "2026-09-15", "open": 10.1, "high": 10.8, "low": 9.9,
             "close": 10.2, "volume": 1_020_000, "amount": 1_020_000.0},
        ],
        "adj_factor": {"2026-09-14": 2.5}})

    # ---- Run A：tdx close 差 6%（10.0 vs 10.6）>0.5% → conflict_src 写摘要 ----
    tdx_conf = MockAdapter("tdx", 3, kline={
        "ohlcv": [
            {"date": "2026-09-14", "open": 10.0, "high": 11.0, "low": 9.5,
             "close": 10.6, "volume": 1_000_000, "amount": 1_000_000.0},
            {"date": "2026-09-15", "open": 10.1, "high": 10.8, "low": 9.9,
             "close": 10.2, "volume": 1_020_000, "amount": 1_020_000.0},
        ],
        "adj_factor": {"2026-09-14": 2.5}})
    _run_p0_t2(monkeypatch, tmp_path, db, days=40, codes=["sh.601398"],
               registry={"sina": sina, "tdx": tdx_conf})

    con = lconn.open(db)
    conf_a = [r[0] for r in con.execute(
        "SELECT conflict_src FROM kline_daily WHERE ts_code='sh.601398'").fetchall()]
    con.close()
    assert all(c is not None and c.startswith("close:") for c in conf_a), f"{conf_a}"

    # ---- Run B：tdx 与 sina 一致 → REPLACE 后 conflict_src=NULL（无陈旧残留）----
    tdx_ok = MockAdapter("tdx", 3, kline={
        "ohlcv": [dict(r) for r in sina._kline["ohlcv"]],
        "adj_factor": {"2026-09-14": 2.5}})
    # days=41 → 新 done 键（同参重跑会被 is_done 跳过，无法验证清除）
    _run_p0_t2(monkeypatch, tmp_path, db, days=41, codes=["sh.601398"],
               registry={"sina": sina, "tdx": tdx_ok})

    con = lconn.open(db)
    try:
        rows = con.execute(
            "SELECT date, source, amount, adj_factor, conflict_src FROM kline_daily "
            "WHERE ts_code='sh.601398' ORDER BY date").fetchall()
        assert len(rows) == 2
        for r in rows:
            assert r[1] == "sina" and r[2] is not None and r[3] is not None, f"{r}"
            assert r[4] is None, f"无分歧 REPLACE 后 conflict_src 必须=NULL（无陈旧残留）: {r}"
    finally:
        con.close()


# ===========================================================================
# 3) 全源失败 → worker 抛错不 mark_done（防 done 键毒化，同 history）
# ===========================================================================
def test_p0_t2_all_sources_down_not_done(tmp_path, monkeypatch):
    db = str(tmp_path / "def1_fail.duckdb")
    from lake import conn as lconn

    con = lconn.open(db)
    _seed_sina_rows(con, "sh.601398", [("2026-09-14", 10.0, 1_000_000.0, 2.5, None)])
    con.close()

    def boom(ts, start=None, end=None):
        raise RuntimeError("down")

    sina = MockAdapter("sina", 0, kline=boom)
    tencent = MockAdapter("tencent", 1, kline=boom)
    prog = _run_p0_t2(monkeypatch, tmp_path, db, days=40, codes=["sh.601398"],
                      registry={"sina": sina, "tencent": tencent})

    p = json.load(open(prog, encoding="utf-8"))
    done_keys = {tuple(k) for k in p.get("done", [])}
    assert ("kline_daily", "sh.601398", "40") not in done_keys, \
        f"全源失败不得标 done: {done_keys}"

    con = lconn.open(db)
    # 原有 sina 行未被破坏（worker 抛错前未落库）
    r = con.execute(
        "SELECT source, amount FROM kline_daily WHERE ts_code='sh.601398'").fetchone()
    con.close()
    assert r == ("sina", 1_000_000.0), f"{r}"


# ===========================================================================
# 4) legacy 回退：LAKE_MULTISOURCE=0 → p0 T2 走腾讯单源（v6.0.x 行为，零回归）
# ===========================================================================
def test_p0_t2_legacy_fallback_zero_regression(tmp_path, monkeypatch):
    """池空（LAKE_MULTISOURCE=0）→ 既有腾讯直连路径逐字节不变：
    fetch_kline_ohlcv(n=days+30) + load_t2(adj_map=None) → source=tencent、amount=NULL。"""
    db = str(tmp_path / "def1_legacy.duckdb")
    from lake import conn as lconn

    con = lconn.open(db)
    _seed_sina_rows(con, "sh.601398", [("2026-09-14", 10.0, 1_000_000.0, 2.5, None)])
    con.close()

    import lake.ingest.tencent_ingest as ti

    calls = []

    def fake_fetch_kline(client, ts_code, n):
        calls.append((ts_code, n))
        return [{"date": "2026-09-14", "open": 10.0, "high": 11.0, "low": 9.5,
                 "close": 10.0, "volume": 10_000.0},   # volume=手（腾讯口径）
                {"date": "2026-09-15", "open": 10.1, "high": 10.8, "low": 9.9,
                 "close": 10.2, "volume": 10_200.0}]

    monkeypatch.setattr(ti, "fetch_kline_ohlcv", fake_fetch_kline)
    # registry=None → LAKE_MULTISOURCE=0（legacy 门）；腾讯 adapter 不进池
    _run_p0_t2(monkeypatch, tmp_path, db, days=40, codes=["sh.601398"], registry=None)

    # 只断言股票 K线（T7 指数 code 无点，走同一 fake 但属 T7 路径）
    stock_calls = [(c, n) for c, n in calls if "." in c]
    assert stock_calls == [("sh.601398", 70)], \
        f"legacy 应直连 fetch_kline_ohlcv(n=days+30): {stock_calls}"
    con = lconn.open(db)
    try:
        rows = {str(r[0]): r for r in con.execute(
            "SELECT date, close, volume, amount, adj_factor, source FROM kline_daily "
            "WHERE ts_code='sh.601398' ORDER BY date").fetchall()}
        # v6.0.x 行为：source=tencent、amount=NULL、adj_factor=NULL（本步留 NULL）
        assert rows["2026-09-14"][5] == "tencent" and rows["2026-09-15"][5] == "tencent"
        assert rows["2026-09-14"][3] is None and rows["2026-09-15"][3] is None, \
            f"legacy 腾讯不提供 amount → NULL: {rows}"
        assert rows["2026-09-14"][4] is None, f"legacy adj_map=None → NULL: {rows}"
        # volume 手→股 ×100（Q8 既有行为）
        assert rows["2026-09-14"][2] == 1_000_000 and rows["2026-09-15"][2] == 1_020_000
    finally:
        con.close()


# ===========================================================================
# 5) upsert 哨兵三态（DEF-1 §3：conflict_src 必须反映本次写入）
# ===========================================================================
def test_upsert_sentinel_three_states(tmp_path):
    from lake import conn as lconn
    from lake.ingest.common import upsert, write_null

    db = str(tmp_path / "sentinel.duckdb")
    con = lconn.open(db)
    try:
        cols = ["ts_code", "date", "open", "high", "low", "close", "volume",
                "amount", "pct_chg", "is_st", "preclose", "adj_factor",
                "source", "fetched_at", "data_version"]
        row = lambda src, amt: ["sh.601398", "2026-09-14", 10.0, 11.0, 9.5, 10.5,
                                1000, amt, None, 0, None, 2.5, src,
                                "2026-09-14 00:00:00", "v6.0"]

        # (a) 字符串摘要 → 写入
        upsert(con, "kline_daily", cols, [row("sina", 1_000_000.0)],
               conflict_src="close:sina:10|tdx:11")
        v = con.execute("SELECT conflict_src FROM kline_daily").fetchone()[0]
        assert v == "close:sina:10|tdx:11"

        # (b) write_null() 哨兵 → 显式 NULL（清掉旧摘要，不残留）
        upsert(con, "kline_daily", cols, [row("sina", 1_000_000.0)],
               conflict_src=write_null())
        v = con.execute("SELECT conflict_src FROM kline_daily").fetchone()[0]
        assert v is None, f"哨兵应显式写 NULL: {v!r}"

        # (c) 缺省 None → **不写该列**（向后兼容：REPLACE 保留旧值——旧调用方零影响）
        upsert(con, "kline_daily", cols, [row("sina", 1_000_000.0)],
               conflict_src="stale:summary")
        upsert(con, "kline_daily", cols, [row("tencent", None)])  # 不传 conflict_src
        v = con.execute("SELECT conflict_src FROM kline_daily").fetchone()[0]
        assert v == "stale:summary", f"None=不写列（保留旧值，向后兼容）: {v!r}"

        # (d) load_t2 契约：conflict_src=None → 显式 NULL（kline_daily 每次写入必落列）
        from lake.ingest.tencent_ingest import load_t2

        upsert(con, "kline_daily", cols, [row("sina", 1_000_000.0)],
               conflict_src="stale:again")
        load_t2(con, "sh.601398",
                [{"date": "2026-09-14", "open": 10.0, "high": 11.0, "low": 9.5,
                  "close": 10.5, "volume": 10_000.0}],
                adj_map=None)  # conflict_src 缺省 None
        v = con.execute("SELECT conflict_src FROM kline_daily").fetchone()[0]
        assert v is None, f"load_t2(None) 必须显式清 NULL（DEF-1）: {v!r}"

        # (e) T3 valuation_daily（有列、调用方不传）→ 行为不变（不写列，保留旧值）
        upsert(con, "valuation_daily",
               ["ts_code", "date", "total_mv", "float_mv", "pe_ttm", "pb",
                "turnover_pct", "ttm_yield_pct", "source", "fetched_at", "data_version"],
               [["sh.601398", "2026-09-14", 1.0, 1.0, 8.0, 0.8, 1.0, None,
                 "sina", "2026-09-14 00:00:00", "v6.0"]],
               conflict_src="stale:t3")
        upsert(con, "valuation_daily",
               ["ts_code", "date", "total_mv", "float_mv", "pe_ttm", "pb",
                "turnover_pct", "ttm_yield_pct", "source", "fetched_at", "data_version"],
               [["sh.601398", "2026-09-14", 2.0, 2.0, 8.5, 0.9, 1.1, None,
                 "tencent", "2026-09-14 00:00:00", "v6.0"]])
        v = con.execute("SELECT conflict_src FROM valuation_daily").fetchone()[0]
        assert v == "stale:t3", f"T3 旧调用方行为必须不变（不写列）: {v!r}"
    finally:
        con.close()
