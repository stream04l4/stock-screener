# -*- coding: utf-8 -*-
"""test_lake_v613_source_pool —— v6.1.3 数据源池状态面板：/status.source_pool.sources。

规格（v6.1.3 brief A/C；v7.0.18 更新；v1.1 2026-11-10 重设计更新）：
- **ready 态** ``source_pool`` 追加 ``sources`` 数组（固定顺序 7 源：4 灌数池源
  + tushare/eastmoney/fuyao 展示源，与前端 SOURCE_COLORS 对齐），每源一项：
  ``{name, enabled, switchable, available, probed_at, latency_ms, authority,
  provides, quota, rate_limit}``。
- ``switchable``：仅灌数池 4 源 true（strategy.yaml 有开关键；v1.1 新源无→false）；
- ``available/probed_at/latency_ms`` **复用现有 adapters[name]**（null=未探测）；
- ``authority`` = Q1 权威性数值（source_pool.AUTHORITY；v1.1 新源不参与 conflict
  裁决→None）；
- ``provides`` = 静态能力表 SOURCE_CAPABILITIES（v1.1 三角色：主源/备源/交叉校验 +
  观察期标注；东财含 T4 分红在线主源）；
- ``quota`` **恒 null**（v7.0.18：baostock 日配额随渠道移除，无计数方）；
- ``rate_limit`` = min_interval_s 派生文案（sina/tdx/adata_f10 有，tencent→null；
  v1.1：tushare/eastmoney/fuyao 静态实测配额文案）；
- **三态契约红线**：sources 仅 ready 态出现——locked/uninitialized 响应体逐字节不动
  （无 source_pool 键 → 无 sources）；ready 态 ``sync.quota_used_today/quota_budget``
  **保留不删**（v605 断言依赖 + API 契约，本次只是前端不再展示）。

纪律：全离线——tmp 库 + monkeypatch 路径，绝不触碰 data/lake/ 生产库；不碰网络。
"""
from __future__ import annotations

import json
import os
import sys

import pytest

duckdb = pytest.importorskip("duckdb")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import lake.web_api as wapi  # noqa: E402
from lake import conn as lconn  # noqa: E402
from lake.ingest.source_pool import AUTHORITY, SOURCE_CAPABILITIES  # noqa: E402

# 固定顺序（与前端 SOURCE_COLORS / _ADAPTER_NAMES 对齐；v7.0.18 baostock 移除；
# v1.1 2026-11-10：+tushare/eastmoney/fuyao 展示源）
ALL_SOURCES = ["sina", "tencent", "tdx", "adata_f10", "tushare", "eastmoney", "fuyao"]
SWITCHABLE_SOURCES = ["sina", "tencent", "tdx", "adata_f10"]   # 灌数池源（有开关键）
# 每源条目键集恒定（形状稳定，前端按此渲染四行卡；v1.1 +switchable；
# v1.2 +sg_* 四字段=SG 中转备用通道档）
ENTRY_KEYS = {"name", "enabled", "switchable", "available", "probed_at", "latency_ms",
              "sg_available", "sg_probed_at", "sg_latency_ms", "sg_detail",
              "authority", "provides", "quota", "rate_limit"}


@pytest.fixture(autouse=True)
def _reset_sp_cache():
    wapi._source_pool_cache_reset()
    yield
    wapi._source_pool_cache_reset()


# ---------------------------------------------------------------------------
# helpers（tmp 库播种 + 路径 monkeypatch → ready/locked/uninitialized）
# ---------------------------------------------------------------------------
def _seed_db(db_path: str) -> None:
    """建 schema + 播种最小数据（ready 态探测只需库可连，数据量无关 sources）。"""
    con = lconn.open(db_path)
    con.execute(
        "INSERT INTO stock_master (ts_code,name,industry_csric2,board,is_st,"
        "source,fetched_at,data_version) VALUES "
        "('sh.601398','工商银行','J66','主板',0,'baostock','2026-09-15 09:26:13','v6.1')")
    con.close()


def _write_progress(tmp_path, tasks=None) -> str:
    p = str(tmp_path / "backfill_progress.json")
    data = {"updated_at": "2026-09-15 13:33:12",
            "tasks": tasks if tasks is not None else [],
            "coverage": {}}
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return p


def _ready_status(tmp_path, monkeypatch, db_name="sp13_ready.duckdb", tasks=None):
    """tmp 库播种 + 路径 monkeypatch → wapi.status()（ready 态）。"""
    p = str(tmp_path / db_name)
    _seed_db(p)
    prog = _write_progress(tmp_path, tasks=tasks)
    monkeypatch.setattr(lconn, "default_db_path", lambda: p)
    monkeypatch.setattr(lconn, "progress_path", lambda: prog)
    return wapi.status()


def _locked_status(tmp_path, monkeypatch):
    """灌数持锁态（connect_existing 抛 LakeLocked）→ /status locked 分支。"""
    p = str(tmp_path / "locked13.duckdb")
    with open(p, "wb") as f:
        f.write(b"\x00" * 16)
    prog = _write_progress(tmp_path)
    monkeypatch.setattr(lconn, "default_db_path", lambda: p)
    monkeypatch.setattr(lconn, "progress_path", lambda: prog)
    monkeypatch.setattr(
        lconn, "connect_existing",
        lambda path=None: (_ for _ in ()).throw(lconn.LakeLocked(p, 4321)))
    return wapi.status()


def _uninitialized_status(tmp_path, monkeypatch):
    missing = str(tmp_path / "nope13" / "lake.duckdb")
    monkeypatch.setattr(lconn, "default_db_path", lambda: missing)
    return wapi.status()


# ===========================================================================
# A. ready 态 sources 数组结构（4 源齐全 + 固定顺序 + 每源键集恒定）
# ===========================================================================
def test_ready_sources_five_fixed_order_and_keys(tmp_path, monkeypatch):
    d = _ready_status(tmp_path, monkeypatch)
    sp = d["source_pool"]
    # v6.1.3：sources 键随 source_pool 追加（旧 5 键仍在）；
    # v6.3.5：追加 conflict_codes（股票数口径——行级被 adj 摘要复制放大误导）
    assert set(sp.keys()) == {"by_source", "conflict_rows", "conflict_codes",
                              "adapters", "stale", "sources"}, \
        f"source_pool 键集: {sorted(sp)}"
    # conflict_codes 形状 = conflict_rows 同键集（T1-T7 + total）
    assert set(sp["conflict_codes"].keys()) == set(sp["conflict_rows"].keys())
    srcs = sp["sources"]
    assert isinstance(srcs, list) and len(srcs) == 7, f"sources 应 7 源: {len(srcs)}"
    # 固定顺序（与前端 SOURCE_COLORS / _ADAPTER_NAMES 对齐）
    assert [s["name"] for s in srcs] == ALL_SOURCES, \
        f"sources 顺序必须=ALL_SOURCES: {[s['name'] for s in srcs]}"
    # 每源条目键集恒定（形状稳定）
    for s in srcs:
        assert set(s.keys()) == ENTRY_KEYS, f"{s['name']} 键集: {sorted(s)}"
    # v1.1：switchable 仅灌数池 4 源（新展示源无 strategy.yaml 开关键）
    by_sw = {s["name"]: s["switchable"] for s in srcs}
    for n in SWITCHABLE_SOURCES:
        assert by_sw[n] is True, f"{n} 应可开关"
    for n in ("tushare", "eastmoney", "fuyao"):
        assert by_sw[n] is False, f"{n} 不应有开关（无 yaml 键）"


def test_ready_sources_available_reuses_adapters(tmp_path, monkeypatch):
    """available/probed_at/latency_ms 复用现有 adapters[name]（null=未探测）。"""
    d = _ready_status(tmp_path, monkeypatch)   # 未写 source_health.json → 全 null
    sp = d["source_pool"]
    for s in sp["sources"]:
        assert s["available"] is None and s["probed_at"] is None \
            and s["latency_ms"] is None, f"{s['name']} 未探测应全 null: {s}"
        # 与 adapters 同值（复用，非另算）
        a = sp["adapters"][s["name"]]
        assert (s["available"], s["probed_at"], s["latency_ms"]) == \
               (a["available"], a["probed_at"], a["latency_ms"]), \
            f"{s['name']} sources 值应与 adapters 一致"


def test_ready_sources_available_passthrough_when_probed(tmp_path, monkeypatch):
    """source_health.json 有探测值 → sources 透出（与 adapters 同值）。"""
    _write_source_health(tmp_path, {
        "sina": {"available": True, "probed_at": "2026-09-17 08:00:00", "latency_ms": 320},
        "tencent": {"available": False, "probed_at": "2026-09-17 08:00:01", "latency_ms": None},
    })
    d = _ready_status(tmp_path, monkeypatch)
    sp = d["source_pool"]
    by_name = {s["name"]: s for s in sp["sources"]}
    assert by_name["sina"]["available"] is True
    assert by_name["sina"]["latency_ms"] == 320
    assert by_name["tencent"]["available"] is False
    # 未探测源仍 null（文件缺键 → adapters 补 null 占位，sources 同）
    assert by_name["tdx"]["available"] is None


def _write_source_health(tmp_path, adapters: dict):
    p = str(tmp_path / "source_health.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(adapters, f, ensure_ascii=False)
    return p


# ===========================================================================
# B. authority（Q1 权威性数值，从 source_pool.AUTHORITY 取）
# ===========================================================================
def test_ready_sources_authority_from_constant(tmp_path, monkeypatch):
    d = _ready_status(tmp_path, monkeypatch)
    by_name = {s["name"]: s for s in d["source_pool"]["sources"]}
    # v1.1：仅灌数池 4 源参与 conflict 裁决（AUTHORITY 有键）
    for name in SWITCHABLE_SOURCES:
        assert by_name[name]["authority"] == AUTHORITY[name], \
            f"{name} authority 应={AUTHORITY[name]}: {by_name[name]['authority']}"
    # v1.1：新展示源不参与裁决 → None（晋主前不写入，无裁决需求）
    for name in ("tushare", "eastmoney", "fuyao"):
        assert by_name[name]["authority"] is None
    # Q1 序：sina(0) < tencent(1) < tdx(3) == adata_f10(3)（baostock:2 空位随渠道移除）
    assert by_name["sina"]["authority"] < by_name["tencent"]["authority"] \
        < by_name["tdx"]["authority"]
    assert by_name["tdx"]["authority"] == by_name["adata_f10"]["authority"] == 3


# ===========================================================================
# C. provides（静态能力表 SOURCE_CAPABILITIES 原样透出；T4 不列）
# ===========================================================================
def test_ready_sources_provides_matches_capabilities(tmp_path, monkeypatch):
    d = _ready_status(tmp_path, monkeypatch)
    by_name = {s["name"]: s for s in d["source_pool"]["sources"]}
    for name in ALL_SOURCES:
        # 原样透出（deep copy，值相等）
        assert by_name[name]["provides"] == SOURCE_CAPABILITIES.get(name, []), \
            f"{name} provides 应=SOURCE_CAPABILITIES: {by_name[name]['provides']}"
    # 关键内容断言（防能力表被静默改动）——v1.1 角色语义（观察期标注）
    sina = by_name["sina"]["provides"]
    assert {"table": "kline_daily", "field_group": "ohlcv_amount",
            "role": "主源(观察期→降备)"} \
        in [{k: p[k] for k in ("table", "field_group", "role")} for p in sina]
    # v6.3.3：sina 能力表含 T6 前十大股东（v6.1.7 加 T6 时漏回填，Joel 10-02 反馈补登）
    assert {"table": "holders_snapshot", "field_group": "holders", "role": "主源"} \
        in [{k: p[k] for k in ("table", "field_group", "role")} for p in sina]
    # tencent 含 T1 主档（v1.1：ST 抽查降校验）+ T3 估值主源 + T7 指数（v1.1 降校验）
    tx = by_name["tencent"]["provides"]
    tables_tx = {(p["table"], p["field_group"]) for p in tx}
    assert ("stock_master", "master") in tables_tx
    assert ("valuation_daily", "valuation") in tables_tx
    assert ("index_daily", "ohlcv") in tables_tx
    # v1.1：tdx T7 由「amount 主源」收敛为单源全字段（观察期主源，东财晋主后降备）
    tdx = {(p["table"], p["field_group"], p["role"]) for p in by_name["tdx"]["provides"]}
    assert ("index_daily", "ohlcv_amount", "主源(观察期→降备)") in tdx
    # adata_f10 仅 T5 f10 主源
    assert by_name["adata_f10"]["provides"] == [
        {"table": "fundamentals_quarterly", "table_cn": "T5 季度基本面",
         "field_group": "f10", "role": "主源"}]
    # v1.1：tushare=T1 备源(现主源)+T3 备源；eastmoney=观察期目标主源矩阵；
    # fuyao=T5 备源+T6 校验
    em = {(p["table"], p["field_group"], p["role"]) for p in by_name["eastmoney"]["provides"]}
    assert ("kline_daily", "ohlcv_amount", "主源(观察期→晋主)") in em
    assert ("index_daily", "ohlcv_amount", "主源(观察期→晋主)") in em
    assert ("dividend_events", "dividend", "主源(端点待验证)") in em
    ts = {(p["table"], p["field_group"], p["role"]) for p in by_name["tushare"]["provides"]}
    assert ("stock_master", "master", "备源(现主源)") in ts
    # v1.1 修订（11-10 review 断档修复）：T2 每日增量主引擎必须如实登记
    # （生产 kline_daily 4.7 万行 source=tushare；§9-②「角色如实标注」）
    assert ("kline_daily", "ohlcv_amount", "主源(每日全市场批量增量)") in ts
    assert ("valuation_daily", "valuation", "备源(全市场1调用/日)") in ts
    fy = {(p["table"], p["role"]) for p in by_name["fuyao"]["provides"]}
    assert ("fundamentals_quarterly", "备源(PIT待验证)") in fy
    # T4 分红：v1.1 起仅东财在线源列 T4（旧 em_local_static CSV 是 source 值不是源能力）
    for name in ALL_SOURCES:
        if name == "eastmoney":
            continue
        assert all(p["table"] != "dividend_events" for p in by_name[name]["provides"]), \
            f"{name} provides 不应含 T4（仅东财在线主源）"


# ===========================================================================
# D. quota（v7.0.18：baostock 日配额随渠道移除 → 全源恒 null）
# ===========================================================================
def test_ready_sources_quota_all_null(tmp_path, monkeypatch):
    """v7.0.18：sources[].quota 恒 null（无计数方；progress tasks 配额键不影响）。"""
    tasks = [
        {"table": "kline_daily", "quota_used_today": 300, "quota_budget": 5000},
        {"table": "stock_master", "quota_used_today": 900, "quota_budget": 5000},
    ]
    d = _ready_status(tmp_path, monkeypatch, tasks=tasks)
    for s in d["source_pool"]["sources"]:
        assert s["quota"] is None, f"{s['name']} quota 应恒 null: {s['quota']}"


def test_ready_sources_quota_none_without_tasks(tmp_path, monkeypatch):
    """无 tasks（从未灌数）→ 仍全源 quota=null（形状稳定）。"""
    d = _ready_status(tmp_path, monkeypatch, tasks=[])
    for s in d["source_pool"]["sources"]:
        assert s["quota"] is None


# ===========================================================================
# E. rate_limit（min_interval_s 派生文案；无配置 → null）
# ===========================================================================
def test_ready_sources_rate_limit_text(tmp_path, monkeypatch):
    d = _ready_status(tmp_path, monkeypatch)
    by_name = {s["name"]: s for s in d["source_pool"]["sources"]}
    # sina=1.0→"≥1s/股"、tdx=0.5→"≥0.5s/股"、adata_f10=1.0→"≥1s/股"（config 缺省）
    assert by_name["sina"]["rate_limit"] == "≥1s/股", by_name["sina"]["rate_limit"]
    assert by_name["tdx"]["rate_limit"] == "≥0.5s/股", by_name["tdx"]["rate_limit"]
    assert by_name["adata_f10"]["rate_limit"] == "≥1s/股", by_name["adata_f10"]["rate_limit"]
    # tencent 无 min_interval 配置 → null（前端显示"无官方配额"）
    assert by_name["tencent"]["rate_limit"] is None
    # v1.1：新展示源静态实测文案（config 无关；2026-11-10 限额探测结论）
    assert by_name["tushare"]["rate_limit"] == "免费档 5次/天·stock_basic 1次/时（超封~24h）"
    assert by_name["eastmoney"]["rate_limit"] == "无硬限（2026-11-10 实测 20并发≈12 req/s）"
    assert by_name["fuyao"]["rate_limit"] == "15次/秒（X-RateLimit-Limit）·日配额未观测"


def test_ready_sources_rate_limit_from_config_override(tmp_path, monkeypatch):
    """min_interval_s 可被 config 覆盖（派生非硬编码）。"""
    import lake.config as lcfg

    real = lcfg.lake_cfg()
    fake = dict(real)
    fake["tdx_min_interval_s"] = 2.0
    monkeypatch.setattr(lcfg, "lake_cfg", lambda: fake)
    d = _ready_status(tmp_path, monkeypatch)
    tdx = {s["name"]: s for s in d["source_pool"]["sources"]}["tdx"]
    assert tdx["rate_limit"] == "≥2s/股", f"tdx rate_limit 应随 config=2.0: {tdx['rate_limit']}"


# ===========================================================================
# F. enabled（config 开关；未配置开关的源 → true）
# ===========================================================================
def test_ready_sources_enabled_default_true(tmp_path, monkeypatch):
    d = _ready_status(tmp_path, monkeypatch)
    by_name = {s["name"]: s for s in d["source_pool"]["sources"]}
    # 默认全开（config 缺省 *_enabled=True；tencent 无独立开关 → True；
    # v1.1 新源无 yaml 开关键 → 同样默认 True）
    for name in ALL_SOURCES:
        assert by_name[name]["enabled"] is True, f"{name} enabled 默认应 True"


def test_ready_sources_enabled_false_when_switch_off(tmp_path, monkeypatch):
    """sina_enabled=False → sina.enabled=false（前端显示'已禁用'badge）。"""
    import lake.config as lcfg

    real = lcfg.lake_cfg()
    fake = dict(real)
    fake["sina_enabled"] = False
    monkeypatch.setattr(lcfg, "lake_cfg", lambda: fake)
    d = _ready_status(tmp_path, monkeypatch)
    by_name = {s["name"]: s for s in d["source_pool"]["sources"]}
    assert by_name["sina"]["enabled"] is False
    # 其余源不受影响
    assert by_name["tencent"]["enabled"] is True


# ===========================================================================
# G. 三态契约红线（sources 仅 ready；locked/uninitialized 逐字节不动）
# ===========================================================================
def test_locked_no_source_pool_hence_no_sources(tmp_path, monkeypatch):
    d = _locked_status(tmp_path, monkeypatch)
    # locked 态键集一个字节不动——无 source_pool 键 → 自然无 sources
    assert "source_pool" not in d, f"locked 态不得混入 source_pool: {sorted(d.keys())}"
    assert "sources" not in d
    assert set(d.keys()) == {"installed", "duckdb_version", "initialized",
                             "backfill_in_progress", "stopping", "lock_holder_pid",
                             "coverage", "tasks", "updated_at"}, \
        f"locked 态键集: {sorted(d.keys())}"


def test_uninitialized_no_source_pool_hence_no_sources(tmp_path, monkeypatch):
    d = _uninitialized_status(tmp_path, monkeypatch)
    assert "source_pool" not in d, f"uninitialized 态不得混入 source_pool: {sorted(d.keys())}"
    assert "sources" not in d
    assert set(d.keys()) == {"installed", "duckdb_version", "initialized",
                             "error", "hint", "coverage", "tasks", "updated_at"}, \
        f"uninitialized 态键集: {sorted(d.keys())}"


def test_ready_sync_quota_retained(tmp_path, monkeypatch):
    """ready 态 ``sync.quota_used_today/quota_budget`` 保留不删（v605 契约 + API）。

    v6.1.3 只是前端不再展示配额列——后端 sync 字段是既有契约，不得移除。"""
    tasks = [{"table": "kline_daily", "quota_used_today": 9, "quota_budget": 5000}]
    d = _ready_status(tmp_path, monkeypatch, tasks=tasks)
    assert d["sync"]["quota_used_today"] == 9, f"sync.quota_used_today 应保留: {d['sync']}"
    assert d["sync"]["quota_budget"] == 5000
    # sources 与 sync.quota 并存（前者新增、后者保留）
    assert "sources" in d["source_pool"]


# ===========================================================================
# H. 零网络 / 只读纪律（sources 组装不碰库查询、不触发 adapter 懒加载）
# ===========================================================================
def test_build_sources_no_db_no_adapter_import(tmp_path, monkeypatch):
    """_build_sources 纯静态+文件：不 import adapter 实例、不发 EU 自检。

    LAKE_MULTISOURCE=0（conftest autouse）下 get_adapter 若被调会走真实注册表——
    这里断言 _build_sources 只依赖 AUTHORITY/SOURCE_CAPABILITIES 常量 + config，
    通过 monkeypatch 让任何 adapter import 都失败也不影响 sources 组装。"""
    from lake.ingest import source_pool as sp

    # 强制注册表不可用（模拟重依赖未装）——sources 仍应正常组装
    monkeypatch.setattr(sp, "_REGISTRY", {"sina": object()})
    prog = {"tasks": [{"quota_used_today": 42}]}
    adapters = {n: {"available": None, "probed_at": None, "latency_ms": None}
                for n in ALL_SOURCES}
    out = wapi._build_sources(prog, adapters)
    assert [s["name"] for s in out] == ALL_SOURCES
    # v7.0.18：quota 恒 null（progress 配额键不影响——无计数方）
    assert all(s["quota"] is None for s in out)
    # provides 来自常量（不依赖 adapter 实例）
    assert out[0]["provides"] == SOURCE_CAPABILITIES["sina"]
