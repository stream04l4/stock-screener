# -*- coding: utf-8 -*-
"""test_lake_v61_source_pool_status —— v6.1 D3：/status.source_pool 扩展 + source_health.json。

规格（research_report_frontend §4 + brief D3-A）：
- **ready 态**追加顶层 ``source_pool``：by_source（9表键恒定，无数据={}）/
  conflict_rows（T1-T7 + total=七表之和）/ adapters（读 source_health.json）/
  baostock_probe（progress 既有键透出，无→null）/ stale（文件缺失或 max(probed_at)
  距今>7天 → true）。v6.1.3：ready 态再追加 sources（各源连通性+数据类型+配额；
  test_lake_v613_source_pool 专测，本文件 A 段键集断言同步含 sources）。
- **兼容红线**：locked/uninitialized 态响应键集一个字节不动（本文件含显式回归断言；
  test_lake_v605_status C 段同纪律保留必过）。
- source_health.json 写入方 = 灌数 driver ``_probe_all_adapters()``（LAKE_MULTISOURCE=0
  跳过零网络；各 adapter available() 记 latency；落 progress 同目录，B-2 派生纪律）。

纪律：全离线——tmp 库 + monkeypatch 路径，绝不触碰 data/lake/ 生产库；driver 探测用
mock adapter（monkeypatch source_pool.get_adapter），零真实网络。
"""
from __future__ import annotations

import datetime as dt
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

import lake.web_api as wapi  # noqa: E402
from lake import conn as lconn  # noqa: E402

NINE_TABLES = [
    "stock_master", "kline_daily", "valuation_daily", "dividend_events",
    "fundamentals_quarterly", "holders_snapshot", "index_daily",
    "factor_snapshot", "macro_rf",
]
SEVEN_CONFLICT = NINE_TABLES[:7]


@pytest.fixture(autouse=True)
def _reset_sp_cache():
    wapi._source_pool_cache_reset()
    yield
    wapi._source_pool_cache_reset()


# ---------------------------------------------------------------------------
# helpers（tmp 库播种：多源混合 + conflict_src 非零，覆盖 by_source/conflict 断言）
# ---------------------------------------------------------------------------
def _seed_sp_db(db_path: str) -> None:
    """kline_daily 3 行（sina×2 + tencent×1，其中 1 行 conflict_src 非 NULL）+
    stock_master 1 行（baostock）。其余 7 表空 → by_source={} / conflict=0。"""
    con = lconn.open(db_path)
    con.execute(
        "INSERT INTO stock_master (ts_code,name,industry_csric2,board,is_st,"
        "source,fetched_at,data_version) VALUES "
        "('sh.601398','工商银行','J66','主板',0,'baostock','2026-09-15 09:26:13','v6.1')")
    con.executemany(
        "INSERT INTO kline_daily (ts_code,date,open,high,low,close,volume,"
        "amount,pct_chg,is_st,preclose,adj_factor,source,fetched_at,data_version,"
        "conflict_src) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [("sh.601398", f"2026-09-{d:02d}", 1, 2, 0.5, 1.5, 100, 1e5, 1.0, 0, 1,
          1.0, src, "2026-09-15 12:42:50", "v6.1", conf)
         for d, src, conf in [(13, "sina", None), (14, "sina", "sina:8.12|tdx:8.11"),
                              (15, "tencent", None)]])
    con.close()


def _write_progress(tmp_path, tasks=None, **extra) -> str:
    p = str(tmp_path / "backfill_progress.json")
    data = {"updated_at": "2026-09-15 13:33:12",
            "tasks": tasks if tasks is not None else [],
            "coverage": {}}
    data.update(extra)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return p


def _ready_status(tmp_path, monkeypatch, db_name="sp_ready.duckdb", **prog_extra):
    """tmp 库播种 + 路径 monkeypatch → wapi.status()（ready 态）。"""
    p = str(tmp_path / db_name)
    _seed_sp_db(p)
    prog = _write_progress(tmp_path, **prog_extra)
    monkeypatch.setattr(lconn, "default_db_path", lambda: p)
    monkeypatch.setattr(lconn, "progress_path", lambda: prog)
    return wapi.status()


def _now_ts(offset_days: float = 0.0) -> str:
    t = dt.datetime.now() - dt.timedelta(days=offset_days)
    return t.strftime("%Y-%m-%d %H:%M:%S")


def _write_source_health(tmp_path, adapters: dict, name="source_health.json"):
    p = str(tmp_path / name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(adapters, f, ensure_ascii=False)
    return p


# ===========================================================================
# A. ready 态 source_pool 形状（by_source 9键恒定 / conflict total 求和）
# ===========================================================================
def test_ready_source_pool_shape_by_source_nine_fixed(tmp_path, monkeypatch):
    d = _ready_status(tmp_path, monkeypatch)
    sp = d["source_pool"]
    # v6.1.3：ready 态 source_pool 追加 sources（各源连通性+数据类型+配额；
    # 仅 ready 态——locked/uninitialized 无 source_pool 键，见 C 段）。
    assert set(sp.keys()) == {"by_source", "conflict_rows", "adapters",
                              "baostock_probe", "stale", "sources"}, \
        f"source_pool 键集: {sorted(sp)}"
    # by_source：9 表键恒定（无数据={}，形状稳定）
    assert list(sp["by_source"].keys()) == NINE_TABLES, \
        f"by_source 必须 9 表固定顺序: {list(sp['by_source'].keys())}"
    # 播种值：kline sina×2 + tencent×1；master baostock×1；其余空表={}
    assert sp["by_source"]["kline_daily"] == {"sina": 2, "tencent": 1}
    assert sp["by_source"]["stock_master"] == {"baostock": 1}
    for t in NINE_TABLES[2:]:
        assert sp["by_source"][t] == {}, f"{t} 空表应为 {{}}: {sp['by_source'][t]}"


def test_ready_source_pool_conflict_rows_total_sum(tmp_path, monkeypatch):
    d = _ready_status(tmp_path, monkeypatch)
    cr = d["source_pool"]["conflict_rows"]
    # T1-T7 七表 + total（T8/T9 无 conflict_src 列 → 不出现）
    assert set(cr.keys()) == set(SEVEN_CONFLICT) | {"total"}, f"conflict_rows 键集: {sorted(cr)}"
    assert cr["kline_daily"] == 1          # 播种 1 行非 NULL
    for t in SEVEN_CONFLICT:
        if t != "kline_daily":
            assert cr[t] == 0
    assert cr["total"] == sum(cr[t] for t in SEVEN_CONFLICT) == 1


def test_ready_source_pool_baostock_probe_passthrough(tmp_path, monkeypatch):
    """baostock_probe = progress 顶层既有键直接透出；无此键 → null。"""
    probe = {"at": "2026-09-15 13:00:00", "alive": False, "elapsed_s": 10.0,
             "detail": "login timeout"}
    d = _ready_status(tmp_path, monkeypatch, db_name="sp_bs.duckdb", baostock_probe=probe)
    assert d["source_pool"]["baostock_probe"] == probe
    d2 = _ready_status(tmp_path, monkeypatch, db_name="sp_nobs.duckdb")
    assert d2["source_pool"]["baostock_probe"] is None


# ===========================================================================
# B. adapters / stale（source_health.json 缺失 / 新鲜 / 过期 三分支）
# ===========================================================================
def test_ready_adapters_missing_file_all_null_stale(tmp_path, monkeypatch):
    """文件缺失 → adapters 5 键全 null + stale=true（'从未探测'语义）。"""
    d = _ready_status(tmp_path, monkeypatch)   # 未写 source_health.json
    sp = d["source_pool"]
    assert set(sp["adapters"].keys()) == {"sina", "tencent", "baostock", "tdx", "adata_f10"}
    for name, a in sp["adapters"].items():
        assert a == {"available": None, "probed_at": None, "latency_ms": None}, \
            f"{name} 缺失文件应全 null: {a}"
    assert sp["stale"] is True


def test_ready_adapters_fresh_file_stale_false(tmp_path, monkeypatch):
    """新鲜探测（now）→ adapters 值透出 + stale=false。"""
    _write_source_health(tmp_path, {
        "sina": {"available": True, "probed_at": _now_ts(), "latency_ms": 320},
        "tencent": {"available": True, "probed_at": _now_ts(), "latency_ms": 95},
        "baostock": {"available": False, "probed_at": _now_ts(), "latency_ms": 10000},
        "tdx": {"available": True, "probed_at": _now_ts(), "latency_ms": 210},
        "adata_f10": {"available": None, "probed_at": None, "latency_ms": None},
    })
    d = _ready_status(tmp_path, monkeypatch)
    sp = d["source_pool"]
    assert sp["adapters"]["sina"] == {"available": True, "probed_at": sp["adapters"]["sina"]["probed_at"], "latency_ms": 320}
    assert sp["adapters"]["baostock"]["available"] is False
    assert sp["adapters"]["adata_f10"] == {"available": None, "probed_at": None, "latency_ms": None}
    assert sp["stale"] is False


def test_ready_adapters_stale_when_probed_older_than_7d(tmp_path, monkeypatch):
    """max(probed_at) 距今 >7 天 → stale=true（值仍透出，前端加'探测数据过期'角标）。

    stale 口径 = **max**(probed_at)：只要任一源在 7 天内探过即视为新鲜；
    本用例全部源都 >7 天（sina 8d / tencent 10d）→ max=8d 前 → stale。"""
    _write_source_health(tmp_path, {
        "sina": {"available": True, "probed_at": _now_ts(8), "latency_ms": 320},
        "tencent": {"available": True, "probed_at": _now_ts(10), "latency_ms": 95},
    })
    d = _ready_status(tmp_path, monkeypatch)
    sp = d["source_pool"]
    assert sp["stale"] is True                    # max=8d 前 >7d
    assert sp["adapters"]["sina"]["available"] is True   # 值仍透出（过期≠丢失）
    # 未探测的源补 null（文件缺键 → 全 null 占位，形状恒定）
    assert sp["adapters"]["baostock"] == {"available": None, "probed_at": None, "latency_ms": None}


def test_ready_adapters_max_probed_at_semantics(tmp_path, monkeypatch):
    """stale 口径 = max(probed_at)：一个源新鲜(now) + 另一个 >7d → stale=false。"""
    _write_source_health(tmp_path, {
        "sina": {"available": True, "probed_at": _now_ts(30), "latency_ms": 320},
        "tencent": {"available": True, "probed_at": _now_ts(), "latency_ms": 95},
    })
    d = _ready_status(tmp_path, monkeypatch)
    assert d["source_pool"]["stale"] is False   # max=now → 新鲜


def test_ready_adapters_corrupt_file_all_null_stale(tmp_path, monkeypatch):
    """文件损坏（非 JSON）→ 全 null + stale=true（防御，不 500）。"""
    p = str(tmp_path / "source_health.json")
    with open(p, "w", encoding="utf-8") as f:
        f.write("{not json!!")
    d = _ready_status(tmp_path, monkeypatch)
    sp = d["source_pool"]
    assert sp["stale"] is True
    assert all(a["available"] is None for a in sp["adapters"].values())


# ===========================================================================
# C. 兼容红线：locked / uninitialized 键集一个字节不动（显式回归）
# ===========================================================================
def test_locked_keyset_byte_unchanged_no_source_pool(tmp_path, monkeypatch):
    p = str(tmp_path / "locked_sp.duckdb")
    with open(p, "wb") as f:
        f.write(b"\x00" * 16)
    prog = _write_progress(tmp_path)
    monkeypatch.setattr(lconn, "default_db_path", lambda: p)
    monkeypatch.setattr(lconn, "progress_path", lambda: prog)
    monkeypatch.setattr(
        lconn, "connect_existing",
        lambda path=None: (_ for _ in ()).throw(lconn.LakeLocked(p, 4321)))
    d = wapi.status()
    assert set(d.keys()) == {"installed", "duckdb_version", "initialized",
                             "backfill_in_progress", "stopping", "lock_holder_pid",
                             "coverage", "tasks", "updated_at"}, \
        f"locked 态键集不得混入 source_pool: {sorted(d.keys())}"


def test_uninitialized_keyset_byte_unchanged_no_source_pool(tmp_path, monkeypatch):
    missing = str(tmp_path / "nope_sp" / "lake.duckdb")
    monkeypatch.setattr(lconn, "default_db_path", lambda: missing)
    d = wapi.status()
    assert set(d.keys()) == {"installed", "duckdb_version", "initialized",
                             "error", "hint", "coverage", "tasks", "updated_at"}, \
        f"uninitialized 态键集不得混入 source_pool: {sorted(d.keys())}"


# ===========================================================================
# D. TTL 缓存（3s 轮询不重复跑聚合查询；db mtime 变 → 失效重查）
# ===========================================================================
def test_source_pool_cache_ttl_and_mtime_invalidation(tmp_path):
    p = str(tmp_path / "cache_sp.duckdb")
    _seed_sp_db(p)
    con = lconn.connect_existing(p)

    # duckdb C 层连接对象方法不可 patch（read-only）→ 用代理包装计数 execute。
    # _source_pool_cache 只调 con.execute → 代理足够。
    class _CountingCon:
        def __init__(self, inner):
            self._inner = inner
            self.n = 0

        def execute(self, *a, **k):
            self.n += 1
            return self._inner.execute(*a, **k)

        def close(self):
            self._inner.close()

    ccon = _CountingCon(con)
    b1, c1 = wapi._source_pool_cache(ccon, p)
    n_after_first = ccon.n
    assert n_after_first > 0
    # TTL 内 + mtime 未变 → 命中缓存（零新查询），返回同一对象
    b2, c2 = wapi._source_pool_cache(ccon, p)
    assert ccon.n == n_after_first, "TTL 内二次调用不得重跑聚合查询"
    assert b2 is b1 and c2 is c1
    # mtime 变化（灌数写入）→ 缓存失效 → 重查
    st = os.stat(p)
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    b3, _ = wapi._source_pool_cache(ccon, p)
    assert ccon.n > n_after_first, "mtime 变化后必须重查"
    assert b3 is not b1
    con.close()


# ===========================================================================
# E. driver 侧 _probe_all_adapters（mock adapter，零网络）
# ===========================================================================
class _FakeAdapter:
    def __init__(self, name, ok=True):
        self.name = name
        self.authority = 0
        self._ok = ok

    def available(self):
        return self._ok


def test_probe_all_adapters_multisource_off_skips(tmp_path, monkeypatch):
    """LAKE_MULTISOURCE=0（conftest autouse 默认）→ 跳过，不写文件、零网络。"""
    import lake_backfill as drv

    res = drv._probe_all_adapters(str(tmp_path / "x.duckdb"))
    assert res["enabled"] is False and res["path"] is None
    assert not (tmp_path / "source_health.json").exists()


def test_probe_all_adapters_writes_health_file(tmp_path, monkeypatch):
    """多源开启 + mock adapter → 落 progress 同目录 source_health.json（B-2 派生：
    自定义 --db → tmp 目录，不碰生产）；5 键齐全、latency_ms=int、未注册源=false。"""
    import lake.ingest.source_pool as sp
    import lake_backfill as drv

    monkeypatch.delenv("LAKE_MULTISOURCE", raising=False)   # 多源开启
    fakes = {n: _FakeAdapter(n, ok=(n != "baostock")) for n in
             ("sina", "tencent", "tdx", "adata_f10")}

    def fake_get_adapter(name):
        return fakes.get(name)   # baostock 未注册 → None → false

    monkeypatch.setattr(sp, "get_adapter", fake_get_adapter)
    db = str(tmp_path / "custom.duckdb")
    res = drv._probe_all_adapters(db)
    assert res["enabled"] is True
    hp = tmp_path / "source_health.json"     # B-2：自定义 --db → 库目录
    assert res["path"] == str(hp) and hp.exists(), f"应落 {hp}: {res}"
    data = json.loads(hp.read_text(encoding="utf-8"))
    assert set(data.keys()) == {"sina", "tencent", "baostock", "tdx", "adata_f10"}
    assert data["sina"]["available"] is True and isinstance(data["sina"]["latency_ms"], int)
    assert data["baostock"]["available"] is False   # 未注册 → false（不猜 null）
    for a in data.values():
        assert a["probed_at"], "每个探测源必须有 probed_at"


def test_probe_all_adapters_adapter_exception_recorded_false(tmp_path, monkeypatch):
    """单源 available() 抛异常 → 记 false（不 crash、不阻塞灌数启动）。"""
    import lake.ingest.source_pool as sp
    import lake_backfill as drv

    class _Boom:
        name = "sina"
        authority = 0

        def available(self):
            raise RuntimeError("network down")

    monkeypatch.delenv("LAKE_MULTISOURCE", raising=False)
    monkeypatch.setattr(sp, "get_adapter", lambda n: _Boom() if n == "sina" else None)
    res = drv._probe_all_adapters(str(tmp_path / "boom.duckdb"))
    assert res["enabled"] is True
    data = json.loads((tmp_path / "source_health.json").read_text(encoding="utf-8"))
    assert data["sina"]["available"] is False
