# -*- coding: utf-8 -*-
"""fix round 2 单测（离线，不联网、零 live BaoStock）。

覆盖 brief 验收要求的新增 ≥12 项：
问题 1（cutover 全量回补）：
- 非候选缺口股获得缺失行（消除日期洞）
- cutover 判定改为全量扫描（弃用 codes[:200] 抽样，>200 位置的缺口也能检出）
- 上限截断（cutover_max_gap_backfill 超上限 → 截断 + warning）
- 幂等（重复运行不产生重复行）
问题 2（BaoStock 依赖降级）：
- all_stock 当日 miss + env 设 → 用陈旧池（0 次 live BaoStock + universe_notes 注明）
- all_stock env 未设 → 现状 live（语义不变）
- all_stock 陈旧池超龄（>stale_max_days）→ 回退 live
- all_stock 陈旧池忽略空文件（D-01 污染件）
- industry TTL 过期 + env 设 → 用现有 industry.csv（0 次 live + 注明）
- industry env 未设 → 现状 live
- prefetch_universe.py mock：成功落盘 / login 失败非零 / 空池非零
"""
from __future__ import annotations

import os
from datetime import date, timedelta

import pytest

from screener.data.cache import CACHE_SENTINEL, DiskCache
from screener.data.fetchers import DataFetcher

# 复用 v4 测试的离线 fake / 构造器（conftest 已把 tests/ 加入 sys.path）
from test_v4_datasource import (
    FakeBSClient,
    FakeKlineSource,
    make_bars,
    make_fetcher,
    seed_kline_af3,
    tencent_ds_cfg,
)

RUN_DAY = "2026-09-08"


# ===========================================================================
# 问题 1：cutover 全量判定 + 非候选缺口回补
# ===========================================================================
def test_noncandidate_gap_backfill_appends_missing_rows(tmp_path):
    """非候选缺口股（dev<θ、无除权）→ 取腾讯 raw K线 append 缺失行，消除日期洞。"""
    # sh.600001 缓存尾=2026-09-04（缺 09-07），preclose==t-1 close → dev=0 → 非候选
    bars = make_bars({"sh.600001": ("平安", 10.5, 10.0, 1000)})
    kspec = {"sh.600001": (
        [("2026-09-04", 10.0), ("2026-09-07", 10.2), ("2026-09-08", 10.5)],  # raw
        [("2026-09-04", 10.0), ("2026-09-07", 10.2), ("2026-09-08", 10.5)],  # qfq=raw → 无事件
    )}
    f = make_fetcher(tmp_path, tencent_ds_cfg(),
                     all_stock_rows=[["sh.600001", "1", "平安"]], snapshot_bars=bars)
    f._kline_source = FakeKlineSource(kspec)
    seed_kline_af3(f.cache, "sh.600001",
                   [["2026-09-04", "sh.600001", "10.0000", "0", "1"]])  # 尾=09-04（缺口）
    f.set_run_day(date(2026, 9, 8))

    f._ensure_snapshot(RUN_DAY)   # 触发全量扫描 + cutover + 非候选回补

    dates = [r[0] for r in f.cache.get("kline_af3_sh.600001")["rows"]]
    assert "2026-09-07" in dates          # 缺口行已回补（消除日期洞）
    # 回补行口径：[d, code, close:.4f, "0", "0"]（isST/tradestatus 占位 0）
    row = {r[0]: r for r in f.cache.get("kline_af3_sh.600001")["rows"]}["2026-09-07"]
    assert row == ["2026-09-07", "sh.600001", "10.2000", "0", "0"]
    # 非候选 → 无除权事件（_cutover_events 空）
    assert f._cutover_events == {}


def test_cutover_fullscan_detects_gap_beyond_sample_window(tmp_path):
    """cutover 判定=全量扫描：>200 位置的缺口股也能检出（旧 codes[:200] 抽样会漏）。"""
    n = 250
    codes = [f"sh.6{i:04d}" for i in range(n)]   # sh.600000 .. sh.600249
    all_rows = [[c, "1", "股"] for c in codes]
    bars_spec = {}
    cache = DiskCache(str(tmp_path / "cache"))
    # 前 249 只：缓存尾==run_day（当前，无缺口）；t-1 close=10.0
    for c in codes[:-1]:
        seed_kline_af3(cache, c, [
            ["2026-09-07", c, "10.0000", "0", "1"],
            ["2026-09-08", c, "10.1000", "0", "1"],
        ])
        bars_spec[c] = ("股", 10.1, 10.0, 1000)   # preclose==t-1 close → dev=0 非候选
    # 第 250 只（index 249，远超抽样窗口 200）：缓存尾=09-04（缺口）；t-1 close=8.0
    gapped = codes[-1]
    seed_kline_af3(cache, gapped, [["2026-09-04", gapped, "8.0000", "0", "1"]])
    bars_spec[gapped] = ("股", 8.1, 8.0, 1000)    # preclose==t-1 close → dev=0 非候选

    client = FakeBSClient(all_rows)
    f = DataFetcher(client, cache, tencent_ds_cfg())
    from test_v4_datasource import FakeSnapshotSource
    f._snapshot_source = FakeSnapshotSource(make_bars(bars_spec))
    f._kline_source = FakeKlineSource({gapped: (
        [("2026-09-04", 8.0), ("2026-09-07", 8.05)],   # raw（缺口期含 09-07）
        [("2026-09-04", 8.0), ("2026-09-07", 8.05)],   # qfq=raw → 无事件
    )})
    f.set_run_day(date(2026, 9, 8))

    f._ensure_snapshot(RUN_DAY)

    # 全量扫描检出 index 249 的缺口 → cutover=True → 非候选回补 09-07 行
    dates = [r[0] for r in cache.get(f"kline_af3_{gapped}")["rows"]]
    assert "2026-09-07" in dates   # 若用抽样（[:200]）则 cutover=False，此行不会回补


def test_noncandidate_backfill_cap_truncation(tmp_path, caplog):
    """非候选缺口数 > cutover_max_gap_backfill → 截断 + warning（只回补前 cap 只）。"""
    n = 6
    codes = [f"sh.6{i:04d}" for i in range(n)]
    bars_spec = {}
    kspec = {}
    cache = DiskCache(str(tmp_path / "cache"))
    for c in codes:
        seed_kline_af3(cache, c, [["2026-09-04", c, "10.0000", "0", "1"]])  # 全部缺口
        bars_spec[c] = ("股", 10.5, 10.0, 1000)   # dev=0 非候选
        kspec[c] = (
            [("2026-09-04", 10.0), ("2026-09-07", 10.2)],
            [("2026-09-04", 10.0), ("2026-09-07", 10.2)],
        )
    # cap=3（覆盖 exdate_detector 全部必需键 + cutover_max_gap_backfill=3）
    ds_cfg = tencent_ds_cfg(exdate_detector={
        "preclose_dev_threshold_pct": 0.5, "factor_sanity_cap_pct": 30,
        "max_candidates_per_day": 200, "cutover_max_candidates": 300,
        "cutover_max_gap_backfill": 3,
    })
    client = FakeBSClient([[c, "1", "股"] for c in codes])
    f = DataFetcher(client, cache, ds_cfg)
    from test_v4_datasource import FakeSnapshotSource
    f._snapshot_source = FakeSnapshotSource(make_bars(bars_spec))
    f._kline_source = FakeKlineSource(kspec)
    f.set_run_day(date(2026, 9, 8))

    with caplog.at_level("WARNING", logger="screener.data.fetch"):
        f._ensure_snapshot(RUN_DAY)

    # 只回补前 3 只（cap=3），后 3 只跳过
    backfilled = [c for c in codes if "2026-09-07" in
                  [r[0] for r in cache.get(f"kline_af3_{c}")["rows"]]]
    assert backfilled == codes[:3]
    assert any("截断" in m.message for m in caplog.records)


def test_noncandidate_backfill_idempotent(tmp_path):
    """幂等：重复 _ensure_snapshot 不产生重复行（kline_af3_append 按日期去重）。"""
    bars = make_bars({"sh.600001": ("平安", 10.5, 10.0, 1000)})
    kspec = {"sh.600001": (
        [("2026-09-04", 10.0), ("2026-09-07", 10.2)],
        [("2026-09-04", 10.0), ("2026-09-07", 10.2)],
    )}
    f = make_fetcher(tmp_path, tencent_ds_cfg(),
                     all_stock_rows=[["sh.600001", "1", "平安"]], snapshot_bars=bars)
    f._kline_source = FakeKlineSource(kspec)
    seed_kline_af3(f.cache, "sh.600001",
                   [["2026-09-04", "sh.600001", "10.0000", "0", "1"]])
    f.set_run_day(date(2026, 9, 8))

    f._ensure_snapshot(RUN_DAY)
    n1 = len(f.cache.get("kline_af3_sh.600001")["rows"])
    # 重置快照缓存态 → 再跑一次（模拟重跑）
    f._snapshot = None
    f._ensure_snapshot(RUN_DAY)
    rows = f.cache.get("kline_af3_sh.600001")["rows"]
    assert len(rows) == n1                       # 行数不变（无重复）
    dates = [r[0] for r in rows]
    assert len(dates) == len(set(dates))         # 日期唯一


# ===========================================================================
# 问题 2：all_stock / industry 陈旧回退（半封禁态降级）
# ===========================================================================
def _seed_allstock(cache: DiskCache, day: str, rows):
    """按 all_stock 列结构写入 allstock_{day}.csv。"""
    cache.put(f"allstock_{day}", ["code", "tradeStatus", "code_name"], rows)


def test_all_stock_stale_fallback_when_env_set(tmp_path, monkeypatch):
    """env 设 + 当日 miss → 用 ≤7 天前陈旧池（0 次 live BaoStock + universe_notes 注明）。"""
    monkeypatch.setenv("BS_UNIVERSE_STALE_OK", "1")
    cache = DiskCache(str(tmp_path / "cache"))
    # 只放 4 天前的非空池（2026-09-04），当日(09-08)缓存缺失
    _seed_allstock(cache, "2026-09-04",
                   [["sh.601398", "1", "工商银行"], ["sz.000001", "1", "平安银行"]])
    client = FakeBSClient()   # 若被调 live 会计数
    f = DataFetcher(client, cache, tencent_ds_cfg())

    df = f.all_stock(RUN_DAY)

    assert len(df) == 2                       # 拿到陈旧池
    assert client.request_count == 0          # **0 次 live BaoStock**
    assert any("4 天前" in n for n in f.universe_notes)   # data_notes 注明


def test_all_stock_live_when_env_unset(tmp_path, monkeypatch):
    """env 未设（Web/手动）→ 现状 live 尝试（语义不变，45min 进程超时兜底）。"""
    monkeypatch.delenv("BS_UNIVERSE_STALE_OK", raising=False)
    cache = DiskCache(str(tmp_path / "cache"))
    _seed_allstock(cache, "2026-09-04", [["sh.601398", "1", "工商银行"]])  # 有陈旧池但不该用
    client = FakeBSClient([["sh.601398", "1", "工商银行"], ["sz.000001", "1", "平安银行"]])
    f = DataFetcher(client, cache, tencent_ds_cfg())

    df = f.all_stock(RUN_DAY)

    assert len(df) == 2                       # live 拉取结果
    assert client.request_count == 1          # 走了 live（非陈旧池）
    assert f.universe_notes == []             # 无陈旧注记


def test_all_stock_stale_too_old_falls_back_to_live(tmp_path, monkeypatch):
    """env 设但陈旧池超龄（>stale_max_days=7）→ 回退 live（最后手段）。"""
    monkeypatch.setenv("BS_UNIVERSE_STALE_OK", "1")
    cache = DiskCache(str(tmp_path / "cache"))
    # 38 天前的池（2026-08-01），远超 7 天上限
    _seed_allstock(cache, "2026-08-01", [["sh.601398", "1", "工商银行"]])
    client = FakeBSClient([["sh.601398", "1", "工商银行"], ["sz.000001", "1", "平安银行"]])
    f = DataFetcher(client, cache, tencent_ds_cfg())

    df = f.all_stock(RUN_DAY)

    assert client.request_count == 1          # 超龄 → live
    assert len(df) == 2
    assert f.universe_notes == []             # 未用陈旧池


def test_all_stock_stale_ignores_empty_polluted_file(tmp_path, monkeypatch):
    """env 设 + 最近的池是空文件（D-01 污染件）→ 跳过它，用更早的非空池。"""
    monkeypatch.setenv("BS_UNIVERSE_STALE_OK", "1")
    cdir = tmp_path / "cache"
    cdir.mkdir()
    cache = DiskCache(str(cdir))
    # 09-07 空文件（污染件）+ 09-04 非空池
    with open(cdir / "allstock_2026-09-07.csv", "w", encoding="utf-8") as fh:
        fh.write(f"{CACHE_SENTINEL}\ncode,tradeStatus,code_name\n")
    _seed_allstock(cache, "2026-09-04", [["sh.601398", "1", "工商银行"]])
    client = FakeBSClient()
    f = DataFetcher(client, cache, tencent_ds_cfg())

    df = f.all_stock(RUN_DAY)

    assert len(df) == 1                       # 用 09-04 非空池（跳过空 09-07）
    assert client.request_count == 0          # 0 次 live
    assert any("4 天前" in n for n in f.universe_notes)


def test_industry_stale_fallback_when_env_set(tmp_path, monkeypatch):
    """industry TTL 过期 + env 设 → 用现有 industry.csv（任意年龄，0 次 live + 注明）。"""
    monkeypatch.setenv("BS_UNIVERSE_STALE_OK", "1")
    cache = DiskCache(str(tmp_path / "cache"))
    ind_rows = [["2026-08-31", "sh.600000", "浦发银行", "J66货币金融服务"],
                ["2026-08-31", "sz.000001", "平安银行", "J66货币金融服务"]]
    cache.put("industry", ["updateDate", "code", "code_name", "industry"], ind_rows)
    # 把 mtime 回拨 48h → TTL(24h) 过期
    p = cache._path("industry")
    old = os.path.getmtime(p) - 48 * 3600
    os.utime(p, (old, old))
    client = FakeBSClient()   # 若被调 live 会计数
    f = DataFetcher(client, cache, tencent_ds_cfg())

    df = f.industry()

    assert len(df) == 2                       # 拿到陈旧行业
    assert client.request_count == 0          # **0 次 live BaoStock**
    assert any("行业分类为陈旧快照" in n for n in f.universe_notes)


def test_industry_live_when_env_unset(tmp_path, monkeypatch):
    """industry TTL 过期 + env 未设 → 现状 live（TTL 24h，语义不变）。"""
    monkeypatch.delenv("BS_UNIVERSE_STALE_OK", raising=False)
    cache = DiskCache(str(tmp_path / "cache"))
    ind_rows = [["2026-08-31", "sh.600000", "浦发银行", "J66货币金融服务"]]
    cache.put("industry", ["updateDate", "code", "code_name", "industry"], ind_rows)
    p = cache._path("industry")
    old = os.path.getmtime(p) - 48 * 3600
    os.utime(p, (old, old))

    class _IndClient(FakeBSClient):
        def call_with_fields(self, query_fn, *, label="", **kw):
            self.request_count += 1
            self.labels.append(label)
            if label == "industry":
                return ["updateDate", "code", "code_name", "industry"], [
                    ["2026-09-08", "sh.600000", "浦发银行", "J66货币金融服务"]]
            raise AssertionError(f"unexpected {label}")

    client = _IndClient()
    f = DataFetcher(client, cache, tencent_ds_cfg())

    df = f.industry()

    assert client.request_count == 1          # 走了 live（非陈旧）
    assert len(df) == 1


# ===========================================================================
# prefetch_universe.py mock 测试（不真跑 login+query_all_stock——它会挂起）
# ===========================================================================
class PrefetchFakeClient:
    """假 BaoStock client：all_stock/industry 返回预置行；fail=True → 抛 DataSourceError。"""

    def __init__(self, all_rows=None, ind_rows=None, fail=False):
        self.all_rows = list(all_rows or [])
        self.ind_rows = list(ind_rows or [])
        self.fail = fail
        self.request_count = 0

    def call_with_fields(self, query_fn, *, label="", **kw):
        self.request_count += 1
        if self.fail:
            from screener.data.baostock_client import DataSourceError
            raise DataSourceError("simulated login/query failure")
        if label == "all_stock":
            return ["code", "tradeStatus", "code_name"], list(self.all_rows)
        if label == "industry":
            return ["updateDate", "code", "code_name", "industry"], list(self.ind_rows)
        raise AssertionError(f"unexpected call: {label}")

    def close(self):
        pass


def _import_prefetch():
    import importlib.util
    path = os.path.join(os.path.dirname(__file__), "..", "scripts", "prefetch_universe.py")
    spec = importlib.util.spec_from_file_location("prefetch_universe", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_prefetch_success_writes_cache(tmp_path):
    """成功：login+query_all_stock+query_stock_industry 经缓存层落盘，exit 0。"""
    pf = _import_prefetch()
    cache_dir = str(tmp_path / "cache")
    client = PrefetchFakeClient(
        all_rows=[["sh.601398", "1", "工商银行"], ["sz.000001", "1", "平安银行"]],
        ind_rows=[["2026-08-31", "sh.600000", "浦发银行", "J66货币金融服务"]],
    )
    rc = pf.prefetch_universe(RUN_DAY, cache_dir, client=client)

    assert rc == 0
    # 复用 DataFetcher/DiskCache 写路径 → 原子落盘
    c = DiskCache(cache_dir)
    assert c.get(f"allstock_{RUN_DAY}") is not None        # allstock 落盘
    assert len(c.get(f"allstock_{RUN_DAY}")["rows"]) == 2
    assert c.get("industry") is not None                   # industry 落盘


def test_prefetch_login_failure_nonzero(tmp_path):
    """login/查询失败（DataSourceError）→ 立即非零退出（不重试、不吞）。"""
    pf = _import_prefetch()
    client = PrefetchFakeClient(all_rows=[["sh.601398", "1", "工商银行"]], fail=True)
    rc = pf.prefetch_universe(RUN_DAY, str(tmp_path / "cache"), client=client)
    assert rc != 0


def test_prefetch_empty_allstock_nonzero(tmp_path):
    """query_all_stock 返回空池（数据源异常）→ 非零退出（不写误导性的空缓存）。"""
    pf = _import_prefetch()
    client = PrefetchFakeClient(all_rows=[], ind_rows=[["2026-08-31", "sh.600000", "浦发银行", "J"]])
    rc = pf.prefetch_universe(RUN_DAY, str(tmp_path / "cache"), client=client)
    assert rc != 0
    # 空池不落盘（D-01 纵深防御）
    c = DiskCache(str(tmp_path / "cache"))
    assert c.get(f"allstock_{RUN_DAY}") is None
