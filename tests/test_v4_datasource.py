# -*- coding: utf-8 -*-
"""v4 数据源架构单测（离线，不联网）。

覆盖 brief 验收要求的新增 ≥25 项：
- sources 解析（R1-b 字段下标 / is_st 名称前缀 / tradestatus vol 规则）
- 批量 + 重试（TencentSnapshotSource.snapshot 分批 / 失败批 / 契约(a)行数==请求数）
- 除权检测器（preclose_dev 信号 θ / sanity 上界 / cutover 截断 / 连续0日告警）
- 因子推导（derive_factor_row：new_back=old×r_event、fore=1.0、adjustFactor=back）
- 幂等（kline_af3_incremental 缓存尾==run_day → 0 追加）
- fail_fast 语义（全批失败 → DataSourceError，绝不伪装空结果）
- 契约监控（解析行数==请求数 / pct 一致性 / 候选数异常）

所有阈值/批量参数来自 config ``datasource:`` 段（零硬编码纪律），测试里显式构造。
"""
from __future__ import annotations

import os
from datetime import date

import pytest

from screener.config import ConfigError, datasource_cfg
from screener.data.baostock_client import DataSourceError
from screener.data.cache import DiskCache
from screener.data.fetchers import DataFetcher
from screener.data.sources import (
    ExdateCandidate,
    ExdateDetector,
    StockBar,
    TencentSnapshotSource,
    derive_factor_row,
    is_st_name,
    tradestatus_from_vol,
)

N_FIELDS = 88


# ===========================================================================
# helpers：构造合成腾讯 payload / fake client / fake snapshot source
# ===========================================================================
def make_line(bs_code: str, name: str, close: float, preclose: float,
              vol: float = 1000.0, pct: float = None) -> str:
    """构造单只股票完整 88 字段 payload（只填关键字段下标，其余空串）。"""
    f = [""] * N_FIELDS
    f[0] = "1"
    f[1] = name
    f[2] = bs_code.replace(".", "")
    f[3] = str(close)
    f[4] = str(preclose)
    f[5] = str(preclose - 0.01)          # open
    f[6] = str(int(vol))                  # volume(手)
    f[30] = "20260908161432"              # ts
    if pct is None:
        pct = round((close / preclose - 1.0) * 100.0, 2) if preclose else 0.0
    f[32] = str(pct)                      # pct_chg
    f[47] = str(round(preclose * 1.1, 2))  # limit_up
    f[48] = str(round(preclose * 0.9, 2))  # limit_down
    return 'v_%s="%s";' % (bs_code.replace(".", ""), "~".join(f))


def make_bars(spec: dict) -> dict:
    """spec={code:(name,close,preclose,vol)} → {code: StockBar}（含派生 is_st/tradestatus）。"""
    out = {}
    for code, (name, close, preclose, vol) in spec.items():
        bar = StockBar(code=code, name=name, close=close, preclose=preclose,
                       open=preclose - 0.01, volume_hand=vol, ts="20260908161432",
                       pct_chg=round((close / preclose - 1) * 100, 2),
                       limit_up=preclose * 1.1, limit_down=preclose * 0.9)
        bar.is_st = is_st_name(name)
        bar.tradestatus = tradestatus_from_vol(vol, close)
        out[code] = bar
    return out


class FakeTencentClient:
    """假腾讯 client：按 URL 中的代码返回对应行；fail_batches 里的批索引返回 None。"""

    def __init__(self, lines_by_code, fail_batches=()):
        self._lines = dict(lines_by_code)   # bs_code -> 完整 v_xxx="..." 行
        self._fail = set(fail_batches)
        self.calls = 0

    def _get_text(self, url):
        batch_idx = self.calls
        self.calls += 1
        if batch_idx in self._fail:
            return None
        import re as _re
        m = _re.search(r"q=([^&]*)", url)
        t_codes = m.group(1).split(",") if m else []
        out = []
        for tc in t_codes:
            bs_code = f"{tc[:2]}.{tc[2:]}"
            if bs_code in self._lines:
                out.append(self._lines[bs_code])
        return "\n".join(out)


class FlakySession:
    """假 requests.Session：前 fail_times 次 get() 抛 RequestException，之后返回 200。

    用于测试 TencentClient._get_text 的**内部重试循环**（在 session.get 层失败，
    让 _get_text 自己重试到成功 → failed_batches=0）。
    """

    def __init__(self, text: str, fail_times: int = 1):
        self._text = text
        self.fail_times = fail_times
        self.n = 0

    def get(self, url, timeout=None):
        import requests as _rq
        self.n += 1
        if self.n <= self.fail_times:
            raise _rq.RequestException("simulated network failure")

        class _R:
            status_code = 200
            content = self._text.encode("gbk", errors="replace")
        return _R()


class FakeBSClient:
    """假 BaoStock client：all_stock 返回预置行；kline_af3/adjfactor fetch 记录调用（默认空）。"""

    def __init__(self, all_stock_rows=None):
        self.all_stock_rows = list(all_stock_rows or [])
        self.request_count = 0
        self.labels = []
        self.kline_fetches = []
        self.adjfactor_fetches = []

    def call_with_fields(self, query_fn, *, label="", **kwargs):
        self.request_count += 1
        self.labels.append(label)
        if label == "all_stock":
            return ["code", "tradeStatus", "code_name"], list(self.all_stock_rows)
        if label.startswith("kline_af3_"):
            self.kline_fetches.append((label, kwargs))
            return [], []  # 空 → 不追加（用于断言"是否走了 BaoStock K线回退"）
        if label.startswith("adjfactor_"):
            self.adjfactor_fetches.append(label)
            return [], []
        raise AssertionError(f"unexpected call: {label}")

    def close(self):
        pass


class FakeSnapshotSource:
    """假快照源：注入 DataFetcher._snapshot_source，避免真实网络。"""

    def __init__(self, bars, requested=None):
        self._bars = bars
        self.requested_count = len(bars) if requested is None else requested
        self.parsed_count = len(bars)
        self.failed_batches = 0
        self.total_batches = 1
        self.warnings = []

    def snapshot(self, codes):
        return {c: self._bars[c] for c in codes if c in self._bars}

    def pct_consistency_sample(self, bars, n, tol):
        return []


def tencent_ds_cfg(**over) -> dict:
    base = {
        "primary": "tencent", "fallback": "fail_fast",
        "tencent": {"snapshot_batch_size": 2, "snapshot_interval_s": 0.0,
                    "timeout_s": 15, "max_attempts": 3, "kline_bars": 40},
        "exdate_detector": {"preclose_dev_threshold_pct": 0.5, "factor_sanity_cap_pct": 30,
                            "max_candidates_per_day": 200, "cutover_max_candidates": 300},
        "contract": {"pct_sample_size": 20, "pct_tolerance_pct": 0.5},
    }
    base.update(over)
    return base


def make_fetcher(tmp_path, ds_cfg=None, all_stock_rows=None, snapshot_bars=None):
    client = FakeBSClient(all_stock_rows)
    cache = DiskCache(str(tmp_path / "cache"))
    fetcher = DataFetcher(client, cache, ds_cfg)
    if snapshot_bars is not None:
        fetcher._snapshot_source = FakeSnapshotSource(snapshot_bars)
    return fetcher


def seed_kline_af3(cache: DiskCache, code: str, rows):
    """把历史行写入 kline_af3 稳定键缓存（rows=[[date,code,close,isST,ts],...]）。"""
    cache.put(f"kline_af3_{code}", ["date", "code", "close", "isST", "tradestatus"], rows)


def seed_adjfactor(cache: DiskCache, code: str, back_factor: float):
    cache.put(f"adjfactor_{code}",
              ["code", "dividOperateDate", "foreAdjustFactor", "backAdjustFactor", "adjustFactor"],
              [[code, "2025-07-10", "1.0", f"{back_factor:.6f}", f"{back_factor:.6f}"]])


# ===========================================================================
# 1) sources 解析：字段下标 / is_st / tradestatus
# ===========================================================================
def test_parse_field_indices():
    """R1-b 字段下标全部对齐（88 字段，close=idx3/preclose=idx4/vol=idx6/pct=idx32）。"""
    text = make_line("sh.600036", "招商银行", 36.88, 37.55)
    src = TencentSnapshotSource(tencent_ds_cfg()["tencent"])
    out = src._parse_batch(text)
    assert "sh.600036" in out
    b = out["sh.600036"]
    assert b.name == "招商银行"
    assert b.close == pytest.approx(36.88)
    assert b.preclose == pytest.approx(37.55)
    assert b.volume_hand == pytest.approx(1000.0)
    assert b.ts == "20260908161432"
    assert b.pct_chg == pytest.approx((36.88 / 37.55 - 1) * 100, abs=0.01)


def test_parse_derived_is_st_and_tradestatus():
    """派生规则：is_st=名称前缀、tradestatus=vol 规则（停牌 vol=0→0）。"""
    text = (make_line("sh.600036", "招商银行", 10.0, 9.9) + "\n" +
            make_line("sz.002650", "ST加加", 6.15, 6.10, vol=0.0))
    src = TencentSnapshotSource(tencent_ds_cfg()["tencent"])
    out = src._parse_batch(text)
    assert out["sh.600036"].is_st == 0 and out["sh.600036"].tradestatus == 1
    assert out["sz.002650"].is_st == 1        # ST 前缀
    assert out["sz.002650"].tradestatus == 0  # vol=0 → 停牌


def test_is_st_name_prefixes():
    assert is_st_name("ST瑞贝卡") == 1
    assert is_st_name("*ST闻泰") == 1
    assert is_st_name("招商银行") == 0
    assert is_st_name("") == 0


def test_tradestatus_from_vol_rule():
    assert tradestatus_from_vol(0, 10.0) == 0     # vol=0 & price>0 → 停牌
    assert tradestatus_from_vol(100, 10.0) == 1   # 正常
    assert tradestatus_from_vol(None, 10.0) == 1  # vol 缺失 → 正常
    assert tradestatus_from_vol(0, 0.0) == 1      # price 非正 → 不按停牌


# ===========================================================================
# 2) 批量 + 重试（分批 / 失败批 / 契约(a)）
# ===========================================================================
def test_snapshot_batch_splitting():
    """batch_size=2 → 4 只分 2 批；解析行数==请求数（契约 a）。"""
    lines = {
        "sh.600036": make_line("sh.600036", "招商银行", 36.88, 37.55),
        "sz.000001": make_line("sz.000001", "平安银行", 11.78, 11.70),
        "sh.601398": make_line("sh.601398", "工商银行", 8.13, 8.09),
        "sz.300750": make_line("sz.300750", "宁德时代", 348.2, 351.0),
    }
    src = TencentSnapshotSource(tencent_ds_cfg()["tencent"])
    src.client = FakeTencentClient(lines)
    out = src.snapshot(["sh.600036", "sz.000001", "sh.601398", "sz.300750"])
    assert len(out) == 4
    assert src.total_batches == 2
    assert src.parsed_count == src.requested_count == 4
    assert not any("契约(a)" in w for w in src.warnings)


def test_snapshot_failed_batch_partial():
    """单批失败 → 该批跳过、记 failed_batches + 契约(a)告警；其余批正常。"""
    lines = {
        "sh.601398": make_line("sh.601398", "工商银行", 8.13, 8.09),
        "sz.300750": make_line("sz.300750", "宁德时代", 348.2, 351.0),
    }
    src = TencentSnapshotSource(tencent_ds_cfg()["tencent"])
    # 批1（sh.600036,sz.000001）失败，批2（sh.601398,sz.300750）成功
    src.client = FakeTencentClient(lines, fail_batches={0})
    out = src.snapshot(["sh.600036", "sz.000001", "sh.601398", "sz.300750"])
    assert len(out) == 2 and "sh.601398" in out and "sz.300750" in out
    assert src.failed_batches == 1
    assert src.parsed_count != src.requested_count
    assert any("契约(a)" in w for w in src.warnings)


def test_snapshot_retry_then_success():
    """单批失败后重试成功（session.get 层首次失败、二次成功）→ 不记失败批。"""
    text = make_line("sh.600036", "招商银行", 36.88, 37.55) + "\n" + \
             make_line("sz.000001", "平安银行", 11.78, 11.70)
    src = TencentSnapshotSource(tencent_ds_cfg()["tencent"])
    src.client.session = FlakySession(text, fail_times=1)   # 第 1 次 get 失败，重试成功
    out = src.snapshot(["sh.600036", "sz.000001"])
    assert len(out) == 2 and src.failed_batches == 0


# ===========================================================================
# 3) 除权检测器（preclose_dev 信号 / sanity / cutover / 连续0日）
# ===========================================================================
def test_detector_hits_exdate_candidate():
    """preclose_dev 超 θ → 候选；r_event=close_prevday/preclose_today。"""
    det = ExdateDetector(tencent_ds_cfg()["exdate_detector"])
    bars = make_bars({"sh.600036": ("招商银行", 36.88, 36.547, 1000)})
    cands, warns = det.detect(bars, {"sh.600036": 37.55})
    assert "sh.600036" in cands
    c = cands["sh.600036"]
    assert c.r_event == pytest.approx(37.55 / 36.547)
    assert c.sanity_ok is True
    assert abs(c.preclose_dev - (36.547 / 37.55 - 1)) < 1e-9


def test_detector_no_candidate_when_flat():
    """preclose==prev_close（dev=0）→ 非候选（v4 稳态：缓存由腾讯写入，比值恒=1）。"""
    det = ExdateDetector(tencent_ds_cfg()["exdate_detector"])
    bars = make_bars({"sh.600036": ("招商银行", 10.0, 10.0, 1000)})
    cands, _ = det.detect(bars, {"sh.600036": 10.0})
    assert cands == {}


def test_detector_misses_small_dev_below_theta():
    """|dev| < θ（0.5%）→ 漏报（设计如此：漏掉的因子误差本身<θ，周对账兜底）。"""
    det = ExdateDetector(tencent_ds_cfg()["exdate_detector"])
    bars = make_bars({"sh.600036": ("招商银行", 10.0, 9.99, 1000)})  # dev≈-0.1% < 0.5%
    cands, _ = det.detect(bars, {"sh.600036": 10.0})
    assert cands == {}


def test_detector_sanity_cap_rejects_extreme():
    """|r_event-1| > sanity 上界(30%) → 不写入、记告警（防异常昨收污染因子序列）。"""
    det = ExdateDetector(tencent_ds_cfg()["exdate_detector"])
    # preclose=10, prev=20 → dev=+100%, r_event=2.0, |r-1|=1.0 > 0.3 → sanity fail
    bars = make_bars({"sh.600036": ("招商银行", 9.0, 10.0, 1000)})
    cands, warns = det.detect(bars, {"sh.600036": 20.0})
    assert "sh.600036" not in cands
    assert any("sanity" in w for w in warns)


def test_detector_cutover_truncation():
    """切换日候选数 > cutover_max_candidates → 按|dev|降序截断 + 告警。"""
    cfg = dict(tencent_ds_cfg()["exdate_detector"])
    cfg["cutover_max_candidates"] = 2
    det = ExdateDetector(cfg)
    bars = make_bars({f"sh.60{i:04d}": ("股", 10.0, 9.0, 1000) for i in range(5)})  # dev≈+11%
    prev = {f"sh.60{i:04d}": 10.0 for i in range(5)}
    cands, warns = det.detect(bars, prev, cutover=True)
    assert len(cands) == 2
    assert any("截断" in w for w in warns)


def test_detector_should_alert_zero_consecutive():
    """连续 7 日候选=0 → 检测器失效信号（告警）；不足 7 日或含非零 → 不告警。"""
    det = ExdateDetector(tencent_ds_cfg()["exdate_detector"])
    assert det.should_alert_zero([0, 0, 0, 0, 0, 0, 0]) is True
    assert det.should_alert_zero([0, 0, 0, 0, 0, 0]) is False       # 不足 7 日
    assert det.should_alert_zero([1, 0, 0, 0, 0, 0, 0]) is False     # 含非零


# ===========================================================================
# 4) 因子推导（derive_factor_row）
# ===========================================================================
def test_derive_factor_row_accumulates():
    """new_back=old_back×r_event（6位）；fore=1.0；adjustFactor=back（BaoStock 口径）。"""
    row = derive_factor_row("sh.600036", "2026-07-10", 5.589333, 1.027444)
    assert row[0] == "sh.600036" and row[1] == "2026-07-10"
    assert row[2] == "1.0"                       # foreAdjustFactor 置 1.0
    assert row[3] == f"{round(5.589333 * 1.027444, 6):.6f}"
    assert row[3] == row[4]                      # adjustFactor == backAdjustFactor


def test_derive_factor_row_matches_tl_reference():
    """TL 已验证参考：600036 07-10 事件 r=37.55/36.547，new_back vs BaoStock 5.742256。"""
    r_event = 37.55 / 36.547
    row = derive_factor_row("sh.600036", "2026-07-10", 5.589333, r_event)
    new_back = float(row[3])
    bs_back = 5.742256
    assert abs(new_back / bs_back - 1.0) < 0.0005   # <0.05%（TL 实测吻合 0.009%）


# ===========================================================================
# 5) kline_af3_incremental 腾讯主路径：幂等 / append / 回退
# ===========================================================================
def test_kline_tencent_idempotent_when_cache_current(tmp_path):
    """缓存尾==run_day（今天 BaoStock cron 已跑过）→ 0 追加、直接读缓存。"""
    bars = make_bars({"sh.600036": ("招商银行", 40.90, 41.07, 500000)})
    f = make_fetcher(tmp_path, tencent_ds_cfg(),
                     all_stock_rows=[["sh.600036", "1", "招商银行"]], snapshot_bars=bars)
    cache = f.cache
    seed_kline_af3(cache, "sh.600036",
                   [["2026-09-07", "sh.600036", "41.0700", "0", "1"],
                    ["2026-09-08", "sh.600036", "40.9000", "0", "1"]])  # 尾==run_day
    kd = f.kline_af3_incremental("sh.600036", "2026-09-08")
    assert kd is not None and kd.current_price == pytest.approx(40.90)
    # 幂等：未走 BaoStock K线查询（all_stock 1 次/日是预期，不计入），缓存仍 2 行
    assert f.client.kline_fetches == []
    assert len(cache.get("kline_af3_sh.600036")["rows"]) == 2


def test_kline_tencent_appends_run_day_from_snapshot(tmp_path):
    """缓存尾<run_day + 快照命中 → append 当日行（close=idx3/isST=名称前缀/ts=vol规则）。"""
    bars = make_bars({"sh.600036": ("招商银行", 40.90, 41.07, 500000)})
    f = make_fetcher(tmp_path, tencent_ds_cfg(),
                     all_stock_rows=[["sh.600036", "1", "招商银行"]], snapshot_bars=bars)
    seed_kline_af3(f.cache, "sh.600036",
                   [["2026-09-07", "sh.600036", "41.0700", "0", "1"]])  # 尾<run_day
    kd = f.kline_af3_incremental("sh.600036", "2026-09-08")
    rows = f.cache.get("kline_af3_sh.600036")["rows"]
    assert len(rows) == 2                                  # 追加了当日行
    assert rows[-1][0] == "2026-09-08" and rows[-1][2] == "40.9000"
    assert kd.current_price == pytest.approx(40.90)


def test_kline_tencent_appends_st_and_suspended_flags(tmp_path):
    """append 行携带 isST=名称前缀、tradestatus=vol规则（停牌 vol=0→0）。"""
    bars = make_bars({"sz.002650": ("ST加加", 6.15, 6.10, 0)})  # ST + 停牌
    f = make_fetcher(tmp_path, tencent_ds_cfg(),
                     all_stock_rows=[["sz.002650", "1", "ST加加"]], snapshot_bars=bars)
    seed_kline_af3(f.cache, "sz.002650", [["2026-09-07", "sz.002650", "6.1000", "1", "1"]])
    f.kline_af3_incremental("sz.002650", "2026-09-08")
    row = f.cache.get("kline_af3_sz.002650")["rows"][-1]
    assert row[3] == "1"   # isST=1（ST 前缀）
    assert row[4] == "0"   # tradestatus=0（vol=0 停牌）


def test_kline_tencent_falls_back_to_baostock_when_missing(tmp_path):
    """快照缺该 code（退市/新股未入快照）→ 回退 BaoStock kline_af3_fetch。"""
    # 快照非空（含另一只）但缺 sh.600036 → 不触发 fail_fast，该股走 BaoStock 回退
    bars = make_bars({"sh.601398": ("工商银行", 8.13, 8.09, 100)})
    f = make_fetcher(tmp_path, tencent_ds_cfg(),
                     all_stock_rows=[["sh.600036", "1", "招商银行"],
                                     ["sh.601398", "1", "工商银行"]], snapshot_bars=bars)
    seed_kline_af3(f.cache, "sh.600036", [["2026-09-07", "sh.600036", "41.0700", "0", "1"]])
    f.kline_af3_incremental("sh.600036", "2026-09-08")
    assert len(f.client.kline_fetches) == 1   # 走了 BaoStock K线回退


def test_kline_baostock_path_unchanged_when_primary_baostock(tmp_path):
    """primary=baostock（显式配置）→ 原 BaoStock 路径，不碰腾讯。"""
    f = make_fetcher(tmp_path, {"primary": "baostock", "fallback": "fail_fast"},
                     all_stock_rows=[["sh.600036", "1", "招商银行"]])
    seed_kline_af3(f.cache, "sh.600036", [["2026-09-07", "sh.600036", "41.0700", "0", "1"]])
    f.kline_af3_incremental("sh.600036", "2026-09-08")
    assert len(f.client.kline_fetches) == 1   # 走 BaoStock（原行为）
    assert f._snapshot is None                # 未触发腾讯快照


def test_datasource_cfg_self_loads_tencent_from_strategy_yaml(tmp_path):
    """生产路径：DataFetcher(client, cache) 不传 cfg → 惰性从 config/strategy.yaml
    加载 datasource 段（primary=tencent）。引擎零改动下由此自动切源。"""
    client = FakeBSClient([["sh.600036", "1", "招商银行"]])
    f = DataFetcher(client, DiskCache(str(tmp_path / "cache")))  # 无 cfg 参数
    assert f.datasource_cfg["primary"] == "tencent"
    assert f._is_tencent_primary() is True


# ===========================================================================
# 6) maybe_refresh_adjfactor 腾讯主路径：命中推导 / 未命中0查询
# ===========================================================================
def test_adjfactor_tencent_candidate_derives_and_appends(tmp_path):
    """命中除权候选 → r_event=prev/preclose、new_back=old×r_event、append（零额外请求）。"""
    bars = make_bars({"sh.600036": ("招商银行", 36.88, 36.547, 1000)})
    f = make_fetcher(tmp_path, tencent_ds_cfg(),
                     all_stock_rows=[["sh.600036", "1", "招商银行"]], snapshot_bars=bars)
    f.set_run_day(__import__("datetime").date(2026, 9, 8))
    # 缓存：kline 尾=run_day（t-1 close=41.07 用于检测）+ adjfactor old_back
    seed_kline_af3(f.cache, "sh.600036",
                   [["2026-09-07", "sh.600036", "41.0700", "0", "1"],
                    ["2026-09-08", "sh.600036", "40.9000", "0", "1"]])
    # 制造除权：preclose(36.547) vs t-1 close(41.07) → dev 超 θ → 候选
    seed_adjfactor(f.cache, "sh.600036", 5.589333)
    changed = f.maybe_refresh_adjfactor("sh.600036", [])
    assert changed is True
    assert f.client.adjfactor_fetches == []   # 零 BaoStock 因子查询（全用快照+缓存推导）
    rows = f.cache.get("adjfactor_sh.600036")["rows"]
    assert len(rows) == 2                      # append 了新事件行
    new_back = float(rows[-1][3])
    r_event = 41.07 / 36.547
    assert new_back == pytest.approx(round(5.589333 * r_event, 6))


def test_adjfactor_tencent_non_candidate_zero_query(tmp_path):
    """未命中除权候选 → False、0 查询（绝大多数股票稳态）。"""
    bars = make_bars({"sh.600036": ("招商银行", 40.90, 41.07, 1000)})  # preclose≈t-1 close
    f = make_fetcher(tmp_path, tencent_ds_cfg(),
                     all_stock_rows=[["sh.600036", "1", "招商银行"]], snapshot_bars=bars)
    f.set_run_day(__import__("datetime").date(2026, 9, 8))
    seed_kline_af3(f.cache, "sh.600036",
                   [["2026-09-07", "sh.600036", "41.0700", "0", "1"],
                    ["2026-09-08", "sh.600036", "40.9000", "0", "1"]])
    seed_adjfactor(f.cache, "sh.600036", 5.589333)
    changed = f.maybe_refresh_adjfactor("sh.600036", [])
    assert changed is False
    assert f.client.adjfactor_fetches == []   # 未命中 → 0 BaoStock 因子查询
    assert len(f.cache.get("adjfactor_sh.600036")["rows"]) == 1   # 未追加


# ===========================================================================
# 7) fail_fast 语义：全批失败 → DataSourceError（绝不伪装空结果）
# ===========================================================================
def test_fail_fast_raises_on_full_batch_failure(tmp_path):
    """primary=tencent + fallback=fail_fast + 快照全批失败(0只) → DataSourceError。"""
    f = make_fetcher(tmp_path, tencent_ds_cfg(),
                     all_stock_rows=[["sh.600036", "1", "招商银行"]], snapshot_bars={})
    with pytest.raises(DataSourceError):
        f.kline_af3_incremental("sh.600036", "2026-09-08")


def test_fail_fast_not_raised_when_partial(tmp_path):
    """部分解析（非全批失败）→ 不抛 DataSourceError，缺失股回退 BaoStock。"""
    bars = make_bars({"sh.601398": ("工商银行", 8.13, 8.09, 100)})
    f = make_fetcher(tmp_path, tencent_ds_cfg(),
                     all_stock_rows=[["sh.600036", "1", "招商银行"],
                                     ["sh.601398", "1", "工商银行"]], snapshot_bars=bars)
    seed_kline_af3(f.cache, "sh.601398", [["2026-09-07", "sh.601398", "8.0900", "0", "1"]])
    kd = f.kline_af3_incremental("sh.601398", "2026-09-08")   # 不抛
    assert kd is not None and kd.current_price == pytest.approx(8.13)


# ===========================================================================
# 8) 契约监控（解析行数 / pct 一致性 / 候选数异常）
# ===========================================================================
def test_contract_row_count_warning_on_mismatch():
    """契约(a)：某批返回行数<请求 → 记"契约(a)"告警。"""
    # 只给 sh.600036 一行，但请求 2 只（sz.000001 缺失）→ 解析1 != 请求2
    src = TencentSnapshotSource(tencent_ds_cfg()["tencent"])
    src.client = FakeTencentClient({"sh.600036": make_line("sh.600036", "招商银行", 36.88, 37.55)})
    src.snapshot(["sh.600036", "sz.000001"])
    assert any("契约(a)" in w for w in src.warnings)


def test_contract_pct_consistency_detects_drift():
    """契约(b)：pct_chg 与 (close/preclose-1)*100 偏离超 tol → 告警。"""
    # 人为造 pct=5.0 但实际 close/preclose 偏差仅 ~0% → 偏离 > 0.5%
    text = make_line("sh.600036", "招商银行", 10.0, 10.0, pct=5.0)
    src = TencentSnapshotSource(tencent_ds_cfg()["tencent"])
    out = src._parse_batch(text)
    warns = src.pct_consistency_sample(out, sample_size=20, tol_pct=0.5)
    assert any("契约(b)" in w for w in warns)


def test_contract_pct_consistency_passes_when_aligned():
    """契约(b)：pct 与计算一致 → 无告警（R1-b 基线 549/549）。"""
    text = make_line("sh.600036", "招商银行", 36.88, 37.55)   # pct 自动=计算值
    src = TencentSnapshotSource(tencent_ds_cfg()["tencent"])
    out = src._parse_batch(text)
    warns = src.pct_consistency_sample(out, sample_size=20, tol_pct=0.5)
    assert warns == []


def test_contract_candidate_count_alert_via_detector():
    """契约(c)：候选数超上限 → 检测器截断告警（已在 cutover 测试覆盖，这里验证日常上限）。"""
    cfg = dict(tencent_ds_cfg()["exdate_detector"])
    cfg["max_candidates_per_day"] = 1
    det = ExdateDetector(cfg)
    bars = make_bars({f"sh.60{i:04d}": ("股", 10.0, 9.0, 1000) for i in range(3)})
    prev = {f"sh.60{i:04d}": 10.0 for i in range(3)}
    cands, warns = det.detect(bars, prev, cutover=False)
    assert len(cands) == 1
    assert any("截断" in w for w in warns)


# ===========================================================================
# 9) config datasource_cfg：默认 / 校验
# ===========================================================================
def test_datasource_cfg_defaults_to_baostock_when_absent():
    """datasource 段缺失 → primary=baostock（现有行为，217 测试 _base_cfg 不变）。"""
    dc = datasource_cfg({})
    assert dc["primary"] == "baostock" and dc["fallback"] == "fail_fast"


def test_datasource_cfg_real_strategy_yaml():
    """真实 strategy.yaml 的 datasource 段可加载且 primary=tencent。"""
    from screener.config import load_config
    path = os.path.join(os.path.dirname(__file__), "..", "config", "strategy.yaml")
    cfg = load_config(path)
    dc = datasource_cfg(cfg)
    assert dc["primary"] == "tencent" and dc["fallback"] == "fail_fast"
    assert dc["tencent"]["snapshot_batch_size"] == 200
    assert dc["exdate_detector"]["preclose_dev_threshold_pct"] == 0.5


def test_datasource_cfg_invalid_primary_raises():
    with pytest.raises(ConfigError):
        datasource_cfg({"datasource": {"primary": "yahoo", "fallback": "fail_fast"}})


def test_datasource_cfg_invalid_fallback_raises():
    with pytest.raises(ConfigError):
        datasource_cfg({"datasource": {"primary": "tencent", "fallback": "retry_forever"}})


def test_datasource_cfg_bad_theta_raises():
    with pytest.raises(ConfigError):
        datasource_cfg({"datasource": {
            "primary": "tencent", "fallback": "fail_fast",
            "exdate_detector": {"preclose_dev_threshold_pct": 0}}})


# ===========================================================================
# 10) 切换日 bootstrap（brief §6）：qfq/raw 缺口期事件检测 + K线回补 + 逐事件因子
# ===========================================================================
class FakeKlineSource:
    """假腾讯 raw+qfq K线源：按预置 (raw, qfq) 序列用与 TencentKlineSource.gap_events
    相同的逻辑算比值跳变事件（隔离网络，验证检测算法 + fetcher 接线）。

    fix round 2：实现完整 KlineSource 协议——除 gap_events 外还提供 kline_closes
    （非候选缺口回补 _backfill_noncandidate_gaps 会调用它取 raw close）。"""

    def __init__(self, spec: dict, step_pct: float = 0.5):
        # spec={code: (raw_closes, qfq_closes)}，各为 [(date, close), ...] 升序
        self._spec = {c: (list(r), list(q)) for c, (r, q) in spec.items()}
        self.event_step_pct = step_pct / 100.0
        self.calls = []

    def kline_closes(self, code):
        """最近 N 根不复权(raw)日K (date, close)，升序；无预置 → []。"""
        raw, _qfq = self._spec.get(code, ([], []))
        return list(raw)

    def gap_events(self, code, start_after, end_before):
        self.calls.append(code)
        raw, qfq = self._spec.get(code, ([], []))
        if not raw or not qfq:
            return [], []
        qmap = {d: c for d, c in qfq}
        series = [(d, qc / rc) for d, rc in raw if (qc := qmap.get(d)) and rc > 0]
        events, prev = [], None
        for d, ratio in series:
            if not (start_after < d < end_before):
                prev = ratio
                continue
            if prev and abs(ratio / prev - 1.0) > self.event_step_pct:
                events.append((d, ratio / prev))
            prev = ratio
        return raw, events


def _cutover_fetcher(tmp_path, kspec, bars, all_rows):
    """构造 primary=tencent fetcher + 注入 fake snapshot/kline 源（切换日场景）。"""
    f = make_fetcher(tmp_path, tencent_ds_cfg(), all_stock_rows=all_rows,
                     snapshot_bars=bars)
    f._kline_source = FakeKlineSource(kspec)
    return f


def test_gap_events_detects_qfq_ratio_step():
    """qfq/raw 比值在除权日跳 r_event（事件间恒=1 无漂移）→ 检出该事件。"""
    # day1 ratio=0.97，day2(事件) ratio=1.0 → step=1/0.97≈1.0309
    kspec = {"sh.600036": (
        [("2026-09-04", 10.0), ("2026-09-07", 9.8)],          # raw
        [("2026-09-04", 9.7), ("2026-09-07", 9.8)],           # qfq
    )}
    ks = FakeKlineSource(kspec)
    raw, events = ks.gap_events("sh.600036", "2026-09-04", "2026-09-08")
    assert len(events) == 1
    assert events[0][0] == "2026-09-07"
    assert events[0][1] == pytest.approx(1.0 / 0.97, abs=1e-4)


def test_gap_events_ignores_flat_and_out_of_window():
    """事件间比值恒=1（无跳变）→ 0 事件；窗口外跳变不计入。"""
    kspec = {"sh.600036": (
        [("2026-09-04", 10.0), ("2026-09-07", 9.9)],
        [("2026-09-04", 10.0), ("2026-09-07", 9.9)],          # ratio 恒=1 → 无事件
    )}
    _, events = FakeKlineSource(kspec).gap_events("sh.600036", "2026-09-04", "2026-09-08")
    assert events == []


def test_populate_cutover_backfills_gap_kline_rows(tmp_path):
    """切换日候选 → 回补缺口期缺失 K线行（tail<d<run_day，raw close）。"""
    bars = make_bars({"sh.600036": ("招商银行", 40.90, 41.07, 500000)})
    kspec = {"sh.600036": (
        [("2026-09-04", 10.0), ("2026-09-07", 41.07)],   # raw（缺口期含 09-07）
        [("2026-09-04", 10.0), ("2026-09-07", 41.07)],   # qfq=raw → 无事件
    )}
    f = _cutover_fetcher(tmp_path, kspec, bars, [["sh.600036", "1", "招商银行"]])
    seed_kline_af3(f.cache, "sh.600036", [["2026-09-04", "sh.600036", "10.0000", "0", "1"]])
    f.set_run_day(date(2026, 9, 8))
    # 手动置候选（stale tail → cutover 场景）
    f._candidates = {"sh.600036": ExdateCandidate(
        code="sh.600036", preclose_today=41.07, close_prevday=10.0,
        preclose_dev=3.107, r_event=10.0 / 41.07, sanity_ok=True)}
    f._snapshot = bars
    f._populate_cutover_events("2026-09-08")
    tail = [r[0] for r in f.cache.get("kline_af3_sh.600036")["rows"]]
    assert "2026-09-07" in tail          # 缺口行已回补
    assert f._cutover_events == {}        # qfq=raw → 无除权事件


def test_maybe_refresh_cutover_multi_event_cumulative(tmp_path):
    """切换日候选：缺口期 2 个 qfq/raw 事件 + run_day 当日 preclose 事件 → 逐事件累乘因子。"""
    bars = make_bars({"sh.600036": ("招商银行", 40.90, 41.07, 500000)})
    # 缺口期：day1 ratio=0.9412 → day2(事件r≈1.03) ratio=0.9706 → day3(事件r≈1.0303) ratio=1.0
    kspec = {"sh.600036": (
        [("2026-09-04", 10.0), ("2026-09-07", 9.8), ("2026-09-08", 40.90)],   # raw
        [("2026-09-04", 9.412), ("2026-09-07", 9.512), ("2026-09-08", 40.90)],# qfq
    )}
    f = _cutover_fetcher(tmp_path, kspec, bars, [["sh.600036", "1", "招商银行"]])
    seed_kline_af3(f.cache, "sh.600036", [["2026-09-04", "sh.600036", "10.0000", "0", "1"]])
    seed_adjfactor(f.cache, "sh.600036", 5.0)   # old_back=5.0
    f.set_run_day(date(2026, 9, 8))
    # run_day 当日 preclose 候选（preclose=41.07 vs t-1 close≈40.9 → 小 dev，但强制注入）
    f._candidates = {"sh.600036": ExdateCandidate(
        code="sh.600036", preclose_today=41.07, close_prevday=40.90,
        preclose_dev=-0.004, r_event=40.90 / 41.07, sanity_ok=True)}
    f._snapshot = bars
    # 生产路径：_ensure_snapshot 检出候选后调 _populate_cutover_events（此处显式复现）
    f._populate_cutover_events("2026-09-08")
    changed = f.maybe_refresh_adjfactor("sh.600036", [])
    assert changed is True
    rows = f.cache.get("adjfactor_sh.600036")["rows"]
    # 期望：old(2025-07-10,5.0) + 缺口期 2 事件(09-04? no: 窗口 (09-04,09-08)) + run_day(09-08)
    # gap_events 窗口 start_after=tail_date(09-04) → day2(09-07) r≈1.03；day3=09-08 在窗口外
    #   （end_before=run_day=09-08，严格 <）→ run_day 事件由 cand 提供
    dates = [r[1] for r in rows]
    assert "2026-09-07" in dates and "2026-09-08" in dates
    # 累乘：new_back(09-07)=5.0×r_gap；new_back(09-08)=new_back(09-07)×r_run
    by_date = {r[1]: float(r[3]) for r in rows}
    # 用 FakeKlineSource 同一算法复核缺口期 r_event（窗口 (tail=09-04, run_day=09-08)）
    ks = f._kline_source
    _, evs = ks.gap_events("sh.600036", "2026-09-04", "2026-09-08")
    r_gap = {d: rv for d, rv in evs}[ "2026-09-07"]
    assert by_date["2026-09-07"] == pytest.approx(5.0 * r_gap, abs=1e-4)
    r_run = 40.90 / 41.07
    assert by_date["2026-09-08"] == pytest.approx(by_date["2026-09-07"] * r_run, abs=1e-4)


def test_maybe_refresh_cutover_no_event_returns_false(tmp_path):
    """切换日候选但缺口期无除权事件、run_day 也非候选 → 0 写入、False。"""
    bars = make_bars({"sh.600036": ("招商银行", 40.90, 41.07, 500000)})
    kspec = {"sh.600036": (
        [("2026-09-04", 10.0), ("2026-09-07", 41.07)],
        [("2026-09-04", 10.0), ("2026-09-07", 41.07)],   # qfq=raw → 无事件
    )}
    f = _cutover_fetcher(tmp_path, kspec, bars, [["sh.600036", "1", "招商银行"]])
    seed_kline_af3(f.cache, "sh.600036", [["2026-09-04", "sh.600036", "10.0000", "0", "1"]])
    f.set_run_day(date(2026, 9, 8))
    # 无候选、无缺口事件 → False
    changed = f.maybe_refresh_adjfactor("sh.600036", [])
    assert changed is False
    assert f.cache.get("adjfactor_sh.600036") is None   # 未写入


def test_cutover_gap_event_sanity_cap_blocks_write(tmp_path):
    """缺口期事件 r_event 超 sanity 上界(±30%) → 不写入因子（防异常污染）。"""
    bars = make_bars({"sh.600036": ("招商银行", 40.90, 41.07, 500000)})
    # day1 ratio=0.5 → day2(事件) ratio=1.0 → r_event=2.0（>1+30%）→ sanity 拒绝
    kspec = {"sh.600036": (
        [("2026-09-04", 10.0), ("2026-09-07", 9.8)],
        [("2026-09-04", 5.0), ("2026-09-07", 9.8)],     # ratio 0.5→1.0，step=2.0
    )}
    f = _cutover_fetcher(tmp_path, kspec, bars, [["sh.600036", "1", "招商银行"]])
    seed_kline_af3(f.cache, "sh.600036", [["2026-09-04", "sh.600036", "10.0000", "0", "1"]])
    f.set_run_day(date(2026, 9, 8))
    # 无 run_day 候选，仅缺口事件（r=2.0 超 sanity）→ 全部被拒 → False
    changed = f.maybe_refresh_adjfactor("sh.600036", [])
    assert changed is False
    assert f.cache.get("adjfactor_sh.600036") is None
