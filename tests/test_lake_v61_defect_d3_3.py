# -*- coding: utf-8 -*-
"""test_lake_v61_defect_d3_3 —— DEFECT-D3-3 修复回归（source_health.json 生产污染，离线零网络）。

DEFECT-D3-3（tester 红线违反）：多源开启用例里 driver ``_probe_all_adapters(db_path)``
落点 fallback 调的是**不可被 monkeypatch** 的 ``lake.conn.progress_path()``——用例 patch
了 ``lake.backfill._progress_path``→tmp，健康文件仍写进生产 data/lake/source_health.json。

修复（TL 拍板：统一 fallback 口径）：落点收敛到 :func:`_source_health_path_for_db` =
``_progress_for_db(db_path) or lb._progress_path()``——经 lake.backfill._progress_path
（可 patch、与 progress 同目录）。三场景口径：

- 缺省库（未 patch）→ data/lake/source_health.json（生产行为逐字节不变）；
- patch _progress_path→tmp → tmp 目录（patch 生效，与 progress 同目录）；
- **未 patch + 自定义 --db** → B-2 派生到库目录（health 与 progress 同目录）。

本文件覆盖：
1. ``_source_health_path_for_db`` 三场景口径（纯路径断言，零 IO）；
2. **专项 e2e**（TL 拍板 #3）：LAKE_MULTISOURCE 开启 + monkeypatch _progress_path→tmp
   + 自定义 tmp db 跑一次 driver p0 → 生产 data/lake/ 四文件 sha256 前后一致、
   tmp 目录出现 source_health.json；
3. ``_probe_all_adapters`` 未 patch + 自定义 --db（B-2 派生）→ 健康文件落库目录。

纪律：全离线（mock adapter 注册表 + fake TencentClient，零真实网络）；tmp_path 隔离；
生产 data/lake/ 只读（本用例**断言**其不被写，而非绕过）。
"""
from __future__ import annotations

import hashlib
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

PROD_LAKE = os.path.join(REPO_ROOT, "data", "lake")
# brief 红线口径：data/lake/ 四文件（生产库 + progress + sync.log + source_health）
PROD_FILES = ["lake.duckdb", "backfill_progress.json", "sync.log", "source_health.json"]


def _sha_map(root: str, names) -> dict:
    out = {}
    for n in names:
        p = os.path.join(root, n)
        if not os.path.exists(p):
            out[n] = None
            continue
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        out[n] = h.hexdigest()
    return out


# ===========================================================================
# mock adapter（零网络，可控；同 test_lake_v61_multisource 风格）
# ===========================================================================
class MockAdapter:
    def __init__(self, name, authority=0, kline=None, avail=True):
        self.name = name
        self.authority = authority
        self._kline = kline
        self._avail = avail

    def available(self):
        return self._avail

    def fetch_kline(self, ts_code, start=None, end=None):
        if self._kline is None:
            raise RuntimeError(f"{self.name} no kline")
        return dict(self._kline)


def _set_registry(monkeypatch, mapping):
    from lake.ingest import source_pool as sp

    monkeypatch.setattr(sp, "_REGISTRY", dict(mapping))


# ===========================================================================
# 1) _source_health_path_for_db 三场景口径（纯路径断言）
# ===========================================================================
def test_d33_health_path_default_db_unchanged(monkeypatch):
    """缺省库（未 patch）→ data/lake/source_health.json（生产行为逐字节不变）。"""
    import lake_backfill as drv

    p = drv._source_health_path_for_db(None)
    assert p == os.path.join(PROD_LAKE, "source_health.json"), p
    # 默认库路径字符串同样落生产目录（driver 实际调用口径）
    from lake.conn import default_db_path

    assert drv._source_health_path_for_db(default_db_path()) == p


def test_d33_health_path_patched_progress_goes_tmp(monkeypatch, tmp_path):
    """patch _progress_path→tmp → 健康文件落 tmp（与 progress 同目录，patch 生效）。"""
    from lake import backfill as lb
    import lake_backfill as drv

    monkeypatch.setattr(lb, "_progress_path", lambda: str(tmp_path / "progress.json"))
    p = drv._source_health_path_for_db(str(tmp_path / "x.duckdb"))
    assert p == str(tmp_path / "source_health.json"), p


def test_d33_health_path_unpatched_custom_db_b2_derive(monkeypatch, tmp_path):
    """未 patch + 自定义 --db → B-2 派生到库目录（health 与 progress 同目录）。"""
    import lake_backfill as drv

    db = str(tmp_path / "custom.duckdb")
    p = drv._source_health_path_for_db(db)
    assert p == str(tmp_path / "source_health.json"), p


# ===========================================================================
# 2) 专项 e2e（TL 拍板 #3）：多源 + patch _progress_path→tmp + tmp db 跑 driver p0
#    → 生产 data/lake/ 四文件 sha256 不变、tmp 出现 source_health.json
# ===========================================================================
def test_d33_p0_multisource_no_production_pollution(tmp_path, monkeypatch):
    """DEFECT-D3-3 repro 全链路：开多源 + patch _progress_path→tmp + 自定义 tmp db
    跑一次 driver p0（进程内 main，同 CLI 代码路径）→ 生产零写入。"""
    import lake_backfill as drv
    from lake import backfill as lb

    # ---- 前置：生产四文件 sha256 快照（只读）----
    before = _sha_map(PROD_LAKE, PROD_FILES)

    db = str(tmp_path / "d33_e2e.duckdb")
    from lake import conn as lconn

    con = lconn.open(db)   # 建 tmp 库（schema init）
    con.close()

    # ---- 离线环境（同 test_lake_v61_def1_p0_t2._make_fake_env 口径）----
    prog = str(tmp_path / "progress.json")
    monkeypatch.setattr(lb.BackfillRunner, "_quota_state", lambda self: (False, 0))
    monkeypatch.setattr(lb, "_progress_path", lambda: prog)   # **DEFECT-D3-3 repro 的 patch**

    class _FakeTClient:
        pass

    import screener.data.tencent as tmod

    monkeypatch.setattr(tmod, "TencentClient", _FakeTClient)
    import lake.ingest.tencent_ingest as ti

    monkeypatch.setattr(ti, "fetch_snapshot", lambda client, ts_codes: {})

    # ---- 多源开启 + mock 注册表（get_adapter 零网络）----
    sina = MockAdapter("sina", 0, kline={
        "ohlcv": [
            {"date": "2026-09-14", "open": 10.0, "high": 11.0, "low": 9.5,
             "close": 10.0, "volume": 1_000_000, "amount": 1_000_000.0},
            {"date": "2026-09-15", "open": 10.1, "high": 10.8, "low": 9.9,
             "close": 10.2, "volume": 1_020_000, "amount": 1_020_000.0},
        ],
        "adj_factor": {"2026-09-14": 2.5}})
    tdx = MockAdapter("tdx", 3, kline={
        "ohlcv": [dict(r) for r in sina._kline["ohlcv"]],
        "adj_factor": {"2026-09-14": 2.5}})
    _set_registry(monkeypatch, {"sina": sina, "tdx": tdx})
    monkeypatch.delenv("LAKE_MULTISOURCE", raising=False)   # 多源开启

    rc = drv.main(["--db", db, "p0", "--days", "40", "--skip-t1",
                   "--codes", "sh.601398", "--t4-codes", "sh.601398"])
    assert rc == drv.EXIT_OK, f"p0 应成功退出: {rc}"

    # ---- 核心断言：生产 data/lake/ 四文件 sha256 前后一致（零污染）----
    after = _sha_map(PROD_LAKE, PROD_FILES)
    diff = {n: (before[n], after[n]) for n in PROD_FILES if before[n] != after[n]}
    assert not diff, f"生产 data/lake/ 被测试写入（DEFECT-D3-3 红线）: {diff}"

    # ---- tmp 落点：source_health.json 出现且与 progress 同目录（B-2）----
    hp = tmp_path / "source_health.json"
    assert hp.exists(), f"tmp 目录应出现 source_health.json: {sorted(os.listdir(tmp_path))}"
    data = json.loads(hp.read_text(encoding="utf-8"))
    assert set(data.keys()) == {"sina", "tencent", "baostock", "tdx", "adata_f10"}
    assert data["sina"]["available"] is True      # mock 注册表：sina 存活
    assert data["baostock"]["available"] is False  # 未注册 → false（不猜 null）
    # progress 同目录（patch 生效，两者一致落 tmp）
    assert os.path.exists(prog)


# ===========================================================================
# 3) _probe_all_adapters：未 patch + 自定义 --db（B-2 派生）→ 健康文件落库目录
# ===========================================================================
def test_d33_probe_unpatched_custom_db_lands_in_db_dir(tmp_path, monkeypatch):
    """未 patch _progress_path + 自定义 --db → 多源开启时健康文件落**库目录**
    （B-2 派生；绝不污染生产 data/lake/）——修复前该场景也写生产（fallback 打
    lconn.progress_path()）。"""
    from lake import backfill as lb
    import lake_backfill as drv

    # 双保险：确认 _progress_path 未被 patch（真实生产默认路径）
    assert os.path.abspath(lb._progress_path()) == \
        os.path.join(PROD_LAKE, "backfill_progress.json")

    monkeypatch.delenv("LAKE_MULTISOURCE", raising=False)   # 多源开启
    fakes = {n: MockAdapter(n) for n in ("sina", "tencent", "tdx", "adata_f10")}
    _set_registry(monkeypatch, fakes)

    db = str(tmp_path / "custom2.duckdb")
    res = drv._probe_all_adapters(db)
    assert res["enabled"] is True
    hp = tmp_path / "source_health.json"
    assert res["path"] == str(hp) and hp.exists(), f"应落库目录 {hp}: {res}"

    # 生产 source_health.json 未被本调用触碰（mtime/sha 不变）
    prod_hp = os.path.join(PROD_LAKE, "source_health.json")
    if os.path.exists(prod_hp):
        h1 = _sha_map(PROD_LAKE, ["source_health.json"])["source_health.json"]
        # 直接再调一次确认幂等且不碰生产
        drv._probe_all_adapters(db)
        h2 = _sha_map(PROD_LAKE, ["source_health.json"])["source_health.json"]
        assert h1 == h2
