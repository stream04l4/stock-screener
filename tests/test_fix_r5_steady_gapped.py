# -*- coding: utf-8 -*-
"""fix round 5 回归单测（离线，不联网）——稳态日误判 gapped（NEW-DEFECT #1/#2 同源修复）+ 本地交易日历。

根因（TL 已独立确认，复现用例在 stages/03_test/test_v4_independent_r2.py 的
test_D3_STEADYMORNING_* / test_D4_STEADYDAY_*）：
  _ensure_snapshot 用 ``tail < run_day`` 判缺口。v4 生产时序下每个交易日早上全市场
  tail==D-1（前一晚 stage2 已 append 到 D-1），检测发生在任何 run-day 行 append **之前**
  → 每天 _cutover_gapped=全市场 ~5207 只：
    NEW-DEFECT#1：maybe_refresh_adjfactor 的 ``not is_gapped`` 门控对全体候选恒 False
      → 稳态日 run-day 除权因子全市场静默丢弃；
    NEW-DEFECT#2：cutover=True 每天 → _backfill_noncandidate_gaps 对全市场逐只发腾讯 K线
      请求（(D-1,D) 区间无交易日、gap_rows 恒空）→ 每日 +~5207 次请求 + ~17min sleep。

修复（TL 拍板）：
  A. 本地静态交易日历 cache/trade_calendar.csv：run_cron.sh 守卫在 query_trade_dates(today)
     成功后 append (today,is_trading)（去重/升序/~500 行；失败不写）；数据层
     fetchers._prev_trade_day 只读该文件取"上一交易日"，缺失回退前一个日历日（保守），
     绝不 live 查 BaoStock。
  B. gapped := tail < prev_td（真正陈旧）；_backfill_noncandidate_gaps 循环内
     tail_date >= prev_td → continue 不发请求（NEW-DEFECT#2 防御纵深）。
  C. RUN_TIMEOUT 5400 → 3600。

本文件覆盖 brief r5 验收要求 1 的全部场景：稳态日 / 真缺口日 / 周一早上 / 日历缺失 /
守卫写日历（真实 run_cron.sh 守卫体 + fake baostock 子进程，零 live）。
所有 fakes 独立构造（与 test_fix_r4_cutover_guards.py 同源语义，不共享对象）。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from screener.data.cache import DiskCache  # noqa: E402
from screener.data.fetchers import DataFetcher  # noqa: E402
from screener.data.sources import (  # noqa: E402
    StockBar,
    is_st_name,
    tradestatus_from_vol,
)

RUN_DAY = "2026-09-10"          # Thu（交易日历：09-07 Mon / 09-08 Tue / 09-09 Wed / 09-10 Thu）
RUN_DATE = date(2026, 9, 10)


# ===========================================================================
# helpers（独立构造）
# ===========================================================================
def make_bars(spec: Dict[str, tuple]) -> Dict[str, StockBar]:
    """spec={code:(name,close,preclose,vol)} → {code:StockBar}（派生 is_st/tradestatus）。"""
    out = {}
    for code, (name, close, preclose, vol) in spec.items():
        pct = (close / preclose - 1.0) * 100.0 if preclose else 0.0
        b = StockBar(code=code, name=name, close=close, preclose=preclose, open=preclose,
                     volume_hand=vol, ts="20260910150000", pct_chg=pct,
                     limit_up=None, limit_down=None)
        b.is_st = is_st_name(name)
        b.tradestatus = tradestatus_from_vol(vol, close)
        out[code] = b
    return out


class FakeSnapshotSource:
    def __init__(self, bars):
        self._bars = bars
        self.requested_count = len(bars)
        self.parsed_count = len(bars)
        self.failed_batches = 0
        self.total_batches = 1
        self.warnings: List[str] = []

    def snapshot(self, codes):
        return {c: self._bars[c] for c in codes if c in self._bars}

    def pct_consistency_sample(self, bars, n, tol):
        return []


class R5KlineSource:
    """spec={code:(raw_closes, qfq_closes)}；带调用计数（验证 0 请求 / N 请求）。"""

    def __init__(self, spec):
        self._spec = {c: (list(r), list(q)) for c, (r, q) in spec.items()}
        self.closes_calls: List[str] = []
        self.events_calls: List[str] = []

    def kline_closes(self, code):
        self.closes_calls.append(code)
        raw, _q = self._spec.get(code, ([], []))
        return list(raw)

    def gap_events(self, code, start_after=None, end_before=None):
        self.events_calls.append(code)
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
            if prev and abs(ratio / prev - 1.0) > 0.005:
                events.append((d, ratio / prev))
            prev = ratio
        return raw, events


class FakeBSClient:
    def __init__(self, all_stock_rows=None):
        self.all_stock_rows = list(all_stock_rows or [])
        self.live_calls = 0

    def call_with_fields(self, query_fn, *, label="", **kwargs):
        self.live_calls += 1
        if label == "all_stock":
            return ["code", "tradeStatus", "code_name"], list(self.all_stock_rows)
        return [], []

    def close(self):
        pass


def tencent_ds_cfg(**over) -> dict:
    base = {
        "primary": "tencent", "fallback": "fail_fast",
        "tencent": {"snapshot_batch_size": 200, "snapshot_interval_s": 0.0,
                    "timeout_s": 15, "max_attempts": 3, "kline_bars": 40,
                    "kline_interval_s": 0.0},
        "exdate_detector": {"preclose_dev_threshold_pct": 0.5, "factor_sanity_cap_pct": 30,
                            "max_candidates_per_day": 200, "cutover_max_candidates": 300,
                            "cutover_max_gap_backfill": 8000},
        "universe": {"stale_max_days": 7},
        "contract": {"pct_sample_size": 20, "pct_tolerance_pct": 0.5},
    }
    base.update(over)
    return base


def seed_kline_af3(cache: DiskCache, code: str, rows):
    cache.put(f"kline_af3_{code}", ["date", "code", "close", "isST", "tradestatus"], rows)


def seed_adjfactor(cache: DiskCache, code: str, events):
    """events=[(ex_date, back_factor), ...] 升序。"""
    cache.put(f"adjfactor_{code}",
              ["code", "dividOperateDate", "foreAdjustFactor", "backAdjustFactor", "adjustFactor"],
              [[code, d, "1.0", f"{b:.6f}", f"{b:.6f}"] for d, b in events])


def seed_calendar(cache: DiskCache, rows: List[List[str]]):
    """rows=[[date, is_trading], ...]（任意顺序；数据层读取不依赖顺序）。"""
    cache.put("trade_calendar", ["date", "is_trading"], rows)


def seed_allstock(cache: DiskCache, day: str, codes: List[str]):
    cache.put(f"allstock_{day}", ["code", "tradeStatus", "code_name"],
              [[c, "1", c] for c in codes])


def make_fetcher(tmp_path, ds_cfg=None, all_stock_codes=None, snapshot_bars=None):
    client = FakeBSClient([[c, "1", c] for c in (all_stock_codes or [])])
    cache = DiskCache(str(tmp_path / "cache"))
    f = DataFetcher(client, cache, ds_cfg or tencent_ds_cfg())
    if snapshot_bars is not None:
        f._snapshot_source = FakeSnapshotSource(snapshot_bars)
    return f


# 交易日历种子（2026-09 周：09-05 Sat / 09-06 Sun 非交易日）
CAL_SEP = [
    ["2026-09-04", "1"],   # Fri
    ["2026-09-05", "0"],   # Sat
    ["2026-09-06", "0"],   # Sun
    ["2026-09-07", "1"],   # Mon
    ["2026-09-08", "1"],   # Tue
    ["2026-09-09", "1"],   # Wed
    ["2026-09-10", "1"],   # Thu
]


# ===========================================================================
# A — _prev_trade_day 本地静态日历（数据层只读，零 live BaoStock）
# ===========================================================================
def test_prev_trade_day_weekday_uses_calendar(tmp_path):
    """run_day=Thu 09-10、日历含 Wed 09-09 → prev_td=09-09（而非旧实现的 run_day 基准）。"""
    f = make_fetcher(tmp_path)
    seed_calendar(f.cache, CAL_SEP)
    assert f._prev_trade_day(RUN_DATE) == date(2026, 9, 9)


def test_prev_trade_day_monday_skips_weekend_rows(tmp_path):
    """周一早上：日历显示周六/周日非交易日 → prev_td=周五（周末行不参与查找）。"""
    f = make_fetcher(tmp_path)
    cal = CAL_SEP + [["2026-09-11", "1"], ["2026-09-12", "0"], ["2026-09-13", "0"]]
    seed_calendar(f.cache, cal)
    assert f._prev_trade_day(date(2026, 9, 14)) == date(2026, 9, 11)


def test_prev_trade_day_monday_without_weekend_rows(tmp_path):
    """周一早上且日历缺周末行（守卫失败未写）→ 仍取周五（最近的 <run_day 交易日行）。"""
    f = make_fetcher(tmp_path)
    cal = [["2026-09-10", "1"], ["2026-09-11", "1"]]   # 无 09-12/09-13 周末行
    seed_calendar(f.cache, cal)
    assert f._prev_trade_day(date(2026, 9, 14)) == date(2026, 9, 11)


def test_prev_trade_day_missing_file_falls_back_to_prev_calendar_day(tmp_path):
    """日历文件缺失 → 回退"run_day 前一个日历日"（保守方向，不崩、不发 live 查询）。"""
    f = make_fetcher(tmp_path)   # cache/ 下无 trade_calendar.csv
    assert f._prev_trade_day(RUN_DATE) == date(2026, 9, 9)     # Thu→Wed（恰好同日历日）
    assert f._prev_trade_day(date(2026, 9, 14)) == date(2026, 9, 13)   # Mon→Sun（保守多触发）
    assert f.client.live_calls == 0, "回退路径绝不 live 查 BaoStock"


def test_prev_trade_day_no_earlier_row_falls_back(tmp_path):
    """日历存在但无 < run_day 的行 → 同样回退前一个日历日。"""
    f = make_fetcher(tmp_path)
    seed_calendar(f.cache, [["2026-09-10", "1"], ["2026-09-11", "1"]])
    assert f._prev_trade_day(date(2026, 9, 8)) == date(2026, 9, 7)


def test_prev_trade_day_ignores_nontrading_and_malformed(tmp_path):
    """非交易日行（is_trading=0）与畸形行不参与查找。"""
    f = make_fetcher(tmp_path)
    seed_calendar(f.cache, [
        ["2026-09-09", "0"],      # 若非交易日 → 不能选它
        ["garbage-line"],         # 畸形行
        ["not-a-date", "1"],      # 日期解析失败
        ["2026-09-08", "1"],
    ])
    assert f._prev_trade_day(RUN_DATE) == date(2026, 9, 8)


# ===========================================================================
# B — 稳态日（tail==D-1）：_cutover_gapped 为空 + 0 次 bootstrap K线请求
#    （翻转 tester test_D3_STEADYMORNING_tail_D_minus_1_retriggers_cutover_daily）
# ===========================================================================
def test_steady_day_tail_D_minus_1_no_cutover_zero_kline_requests(tmp_path):
    """稳态日早上全市场 tail==D-1（09-09）、日历含 09-09 → 无缺口、cutover=False、
    0 次 K线请求、缓存零改动（NEW-DEFECT#2 修复验证）。"""
    codes = [f"sz.002{i:04d}" for i in range(30)]
    f = make_fetcher(tmp_path, all_stock_codes=codes)
    seed_calendar(f.cache, CAL_SEP)
    seed_allstock(f.cache, RUN_DAY, codes)
    for c in codes:
        seed_kline_af3(f.cache, c, [
            ["2026-09-08", c, "10.0000", "0", "1"],
            ["2026-09-09", c, "10.0000", "0", "1"],   # tail == D-1：正常稳态
        ])
    bars = make_bars({c: (c[:6].replace(".", ""), 10.0, 10.0, 1000.0) for c in codes})
    f._snapshot_source = FakeSnapshotSource(bars)
    ksrc = R5KlineSource({})
    f._kline_source = ksrc
    f.set_run_day(RUN_DATE)
    f._ensure_snapshot(RUN_DAY)

    assert not f._cutover_gapped, (
        "NEW-DEFECT#2: tail==D-1 is the NORMAL steady state (prev night's stage2 appended "
        f"D-1; detection runs before any run-day append) — must NOT be flagged gapped. "
        f"gapped={sorted(f._cutover_gapped)[:5]}..."
    )
    assert ksrc.closes_calls == [], (
        f"NEW-DEFECT#2: steady-state day must make 0 bootstrap K-line requests, "
        f"got {len(ksrc.closes_calls)}"
    )
    assert ksrc.events_calls == []
    for c in codes:   # 缓存零改动（无空转 append）
        rows = f.cache.get(f"kline_af3_{c}")["rows"]
        assert [r[0] for r in rows] == ["2026-09-08", "2026-09-09"]


# ===========================================================================
# B' — 稳态日 run-day 除权因子照常写入（翻转 tester test_D4_STEADYDAY_*）
#      （NEW-DEFECT#1 修复验证：正确行为版断言方向）
# ===========================================================================
def test_steady_day_real_exdiv_factor_written(tmp_path):
    """稳态日（tail==D-1）真除权候选 → run-day 因子**照常写入**（NEW-DEFECT#1 修复点）。

    旧实现把 tail==D-1 误判成缺口 → ``not is_gapped`` 恒 False → 因子被静默丢弃。
    """
    code = "sz.002500"
    r = 1.04   # D=09-10 真除权比
    c_0908, c_0909 = 20.0, float(f"{20.0 * 0.999:.4f}")
    f = make_fetcher(tmp_path, all_stock_codes=[code])
    seed_calendar(f.cache, CAL_SEP)
    seed_allstock(f.cache, RUN_DAY, [code])
    seed_kline_af3(f.cache, code, [
        ["2026-09-08", code, f"{c_0908:.4f}", "0", "1"],
        ["2026-09-09", code, f"{c_0909:.4f}", "0", "1"],   # tail == D-1：REAL 稳态
    ])
    seed_adjfactor(f.cache, code, [("2025-07-10", 2.0)])
    pre = float(f"{c_0909 / r:.4f}")   # 交易所除权后昨收 → dev > θ → 候选
    bars = make_bars({code: (code[:6].replace(".", ""), round(pre * 1.001, 4), pre, 500000.0)})
    f._snapshot_source = FakeSnapshotSource(bars)
    ksrc = R5KlineSource({})
    f._kline_source = ksrc
    f.set_run_day(RUN_DATE)
    f._ensure_snapshot(RUN_DAY)

    assert code in f._candidates, "real run-day ex-div must be detected as candidate"
    assert code not in f._cutover_gapped, (
        "steady-state stock (tail==D-1) must NOT be in the gapped set after fix r5"
    )
    f.kline_af3_incremental(code, RUN_DAY)   # 生产 stage2：先 append run-day 行
    ch = f.maybe_refresh_adjfactor(code, [])
    rows = {r_[1]: float(r_[3]) for r_ in f.cache.get(f"adjfactor_{code}")["rows"]}
    assert ch is True and "2026-09-10" in rows, (
        "NEW-DEFECT#1: steady-day run-day ex-div factor was SUPPRESSED — the `not is_gapped` "
        f"gate must let steady candidates (tail==D-1) write their factor. rows={rows}"
    )
    # r_event = close(D-1)/preclose_today（preclose 经 4 位小数舍入，与精确 r=1.04 有微差）
    assert rows["2026-09-10"] == pytest.approx(round(2.0 * (c_0909 / pre), 6), abs=1e-6)
    assert abs(rows["2026-09-10"] / 2.0 - r) < 5e-4, "factor must reflect the real ex-div ratio ~r"
    assert ksrc.closes_calls == [], "steady day must make 0 K-line requests"


def test_steady_day_non_exdiv_no_factor(tmp_path):
    """稳态日无除权（dev≤θ）→ 非候选、0 因子写入、0 请求（正常路径不误伤）。"""
    code = "sz.002600"
    f = make_fetcher(tmp_path, all_stock_codes=[code])
    seed_calendar(f.cache, CAL_SEP)
    seed_allstock(f.cache, RUN_DAY, [code])
    seed_kline_af3(f.cache, code, [
        ["2026-09-08", code, "10.0000", "0", "1"],
        ["2026-09-09", code, "10.0500", "0", "1"],   # tail == D-1
    ])
    seed_adjfactor(f.cache, code, [("2025-07-10", 3.0)])
    bars = make_bars({code: (code[:6].replace(".", ""), 10.08, 10.05, 500000.0)})   # dev=+0.3%≤θ
    f._snapshot_source = FakeSnapshotSource(bars)
    ksrc = R5KlineSource({})
    f._kline_source = ksrc
    f.set_run_day(RUN_DATE)
    f._ensure_snapshot(RUN_DAY)
    assert code not in f._candidates and not f._cutover_gapped
    ch = f.maybe_refresh_adjfactor(code, [])
    assert ch is False
    rows = [r_[1] for r_ in f.cache.get(f"adjfactor_{code}")["rows"]]
    assert "2026-09-10" not in rows and ksrc.closes_calls == []


# ===========================================================================
# C — 真缺口日（tail<D-1）：bootstrap 仍触发、回补 tradestatus=1、无日期洞、幂等
# ===========================================================================
def test_real_gap_day_bootstrap_backfill_and_idempotent(tmp_path):
    """混合态：8 只 tail=09-04（真缺口，<prev_td=09-09）+ 4 只 tail=09-08（稳态）。
    → 只有 8 只走 bootstrap；回补 (tail,run_day) 全部交易日、tradestatus="1"、无日期洞；
    重跑幂等（0 新请求 / 0 重复行）。"""
    stale = [f"sz.300{i:04d}" for i in range(8)]
    fresh = [f"sz.301{i:04d}" for i in range(4)]
    codes = stale + fresh
    f = make_fetcher(tmp_path, all_stock_codes=codes)
    seed_calendar(f.cache, CAL_SEP)
    seed_allstock(f.cache, RUN_DAY, codes)
    kspec = {}
    for c in stale:
        seed_kline_af3(f.cache, c, [["2026-09-04", c, "10.0000", "0", "1"]])   # 真缺口
        # raw K线：09-04..09-09 连续（每交易日一根，轻微漂移、无除权台阶）
        kspec[c] = ([("2026-09-04", 10.0), ("2026-09-07", 9.99), ("2026-09-08", 9.98),
                     ("2026-09-09", 10.0)],
                    [("2026-09-04", 10.0), ("2026-09-07", 9.99), ("2026-09-08", 9.98),
                     ("2026-09-09", 10.0)])   # qfq==raw → 无真实除权
    for c in fresh:
        seed_kline_af3(f.cache, c, [
            ["2026-09-08", c, "10.0000", "0", "1"],
            ["2026-09-09", c, "10.0000", "0", "1"],   # tail==D-1：稳态（与 prev_td=09-09 齐平）
        ])
    bars = make_bars({c: (c[:6].replace(".", ""), 10.0, 10.0, 1000.0) for c in codes})
    f._snapshot_source = FakeSnapshotSource(bars)
    ksrc = R5KlineSource(kspec)
    f._kline_source = ksrc
    f.set_run_day(RUN_DATE)
    f._ensure_snapshot(RUN_DAY)

    # 只有真缺口股进 gapped 集合（tail=09-04 < prev_td=09-09）；稳态股不在
    assert f._cutover_gapped == set(stale), (
        f"gapped must be exactly the truly-stale stocks; got {sorted(f._cutover_gapped)}"
    )
    # bootstrap 只对 8 只真缺口股发请求（稳态 4 只 0 请求）
    assert sorted(ksrc.closes_calls) == sorted(stale), (
        f"bootstrap must request K-lines for exactly the gapped stocks; got {ksrc.closes_calls}"
    )
    # 回补：(09-04, 09-10) 全部交易日 09-07/08/09、tradestatus="1"、isST 占位 "0"
    for c in stale:
        rows = {r[0]: r for r in f.cache.get(f"kline_af3_{c}")["rows"]}
        for d in ("2026-09-07", "2026-09-08", "2026-09-09"):
            assert d in rows, f"gap day {d} not backfilled for {c}"
            assert rows[d][4] == "1", f"backfill tradestatus must be '1' (row present ⟺ traded)"
            assert rows[d][3] == "0"
        # stage2 append run-day 行后无日期洞（09-04..09-10 连续）
        f.kline_af3_incremental(c, RUN_DAY)
    for c in stale:
        dates = [r[0] for r in f.cache.get(f"kline_af3_{c}")["rows"]]
        assert dates == ["2026-09-04", "2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10"], \
            f"date holes after backfill+stage2: {dates}"
    # 稳态股缓存零改动
    for c in fresh:
        rows = [r[0] for r in f.cache.get(f"kline_af3_{c}")["rows"]]
        assert rows == ["2026-09-08", "2026-09-09"]

    # 幂等：重跑（新 fetcher、同缓存）→ tail 已=09-09==prev_td → 0 缺口、0 请求、0 重复行
    f2 = make_fetcher(tmp_path, all_stock_codes=codes)
    seed_calendar(f2.cache, CAL_SEP)
    seed_allstock(f2.cache, RUN_DAY, codes)
    f2._snapshot_source = FakeSnapshotSource(bars)
    ksrc2 = R5KlineSource(kspec)
    f2._kline_source = ksrc2
    f2.set_run_day(RUN_DATE)
    f2._ensure_snapshot(RUN_DAY)
    assert not f2._cutover_gapped and ksrc2.closes_calls == []
    for c in stale:
        rows = [r[0] for r in f2.cache.get(f"kline_af3_{c}")["rows"]]
        assert len(rows) == len(set(rows)), f"duplicate dates after rerun: {rows}"


def test_real_gap_day_candidate_gap_event_factor_only(tmp_path):
    """真缺口候选：缺口期真实除权（qfq/raw 在真 ex_date 检出）→ 只写 gap_events 因子，
    run-day 伪因子不写（fix r4 DEFECT#2 语义在 r5 新判定下保持）。"""
    code = "sh.600036"
    f = make_fetcher(tmp_path, all_stock_codes=[code])
    seed_calendar(f.cache, CAL_SEP)
    seed_allstock(f.cache, RUN_DAY, [code])
    seed_kline_af3(f.cache, code, [["2026-09-04", code, "10.0000", "0", "1"]])   # 真缺口
    seed_adjfactor(f.cache, code, [("2025-07-10", 5.0)])
    # 真实除权在 09-07（raw 台阶 10.0→9.80，r=1/0.98）；qfq 锚定最新 → 比值在 09-07 跳
    kspec = {code: (
        [("2026-09-04", 10.0), ("2026-09-07", 9.80), ("2026-09-08", 9.75), ("2026-09-09", 9.70)],
        [("2026-09-04", 9.80), ("2026-09-07", 9.80), ("2026-09-08", 9.75), ("2026-09-09", 9.70)],
    )}
    bars = make_bars({code: (code[:6].replace(".", ""), 9.70, 9.70, 500000.0)})   # dev=0 → 非候选
    f._snapshot_source = FakeSnapshotSource(bars)
    ksrc = R5KlineSource(kspec)
    f._kline_source = ksrc
    f.set_run_day(RUN_DATE)
    f._ensure_snapshot(RUN_DAY)
    assert code in f._cutover_gapped, "tail=09-04 < prev_td=09-09 → truly gapped"
    evs = f._cutover_events.get(code, [])
    assert len(evs) == 1 and evs[0][0] == "2026-09-07", f"gap ex-div at 09-07 must be detected: {evs}"
    ch = f.maybe_refresh_adjfactor(code, [])
    rows = {r_[1]: float(r_[3]) for r_ in f.cache.get(f"adjfactor_{code}")["rows"]}
    assert ch is True
    assert "2026-09-07" in rows and rows["2026-09-07"] == pytest.approx(5.0 / 0.98, abs=1e-3)
    assert "2026-09-10" not in rows, "gapped stock must NOT get a run-day factor (drift ≠ ex-div)"


# ===========================================================================
# D — 周一早上（日历含周末非交易日行；prev_td=周五；tail==周五）→ 稳态、0 请求
# ===========================================================================
def test_monday_morning_steady_zero_requests(tmp_path):
    """run_day=Mon 09-14：全市场 tail==Fri 09-11（上周五 stage2 已 append）、日历显示
    Sat/Sun 非交易日 → prev_td=Fri → 稳态、0 缺口、0 K线请求。"""
    codes = [f"sz.003{i:04d}" for i in range(12)]
    f = make_fetcher(tmp_path, all_stock_codes=codes)
    cal = CAL_SEP + [["2026-09-11", "1"], ["2026-09-12", "0"], ["2026-09-13", "0"]]
    seed_calendar(f.cache, cal)
    seed_allstock(f.cache, "2026-09-14", codes)
    for c in codes:
        seed_kline_af3(f.cache, c, [
            ["2026-09-10", c, "10.0000", "0", "1"],
            ["2026-09-11", c, "10.0000", "0", "1"],   # tail == Fri（上一交易日）
        ])
    bars = make_bars({c: (c[:6].replace(".", ""), 10.0, 10.0, 1000.0) for c in codes})
    f._snapshot_source = FakeSnapshotSource(bars)
    ksrc = R5KlineSource({})
    f._kline_source = ksrc
    f.set_run_day(date(2026, 9, 14))
    assert f._prev_trade_day(date(2026, 9, 14)) == date(2026, 9, 11)   # 周五
    f._ensure_snapshot("2026-09-14")
    assert not f._cutover_gapped, (
        "Monday morning with tail==Friday is steady state — weekend rows must not create gaps"
    )
    assert ksrc.closes_calls == [], "Monday-morning steady state must make 0 K-line requests"


# ===========================================================================
# E — 日历文件缺失：回退 prev_calendar_day 不崩、行为保守（brief A.2）
# ===========================================================================
def test_missing_calendar_fallback_conservative_no_crash(tmp_path):
    """日历缺失 + run_day=Mon 09-14 + tail==Fri 09-11 → 回退 prev_td=Sun 09-13（前一个日历日）。

    保守方向（brief A.2 明示）：Fri < Sun → 误判缺口、cutover=True → **多触发一次 bootstrap**
    （腾讯 raw K线只返回交易日行，(Fri, Mon) 区间无交易日 → gap_rows 恒空）→ **缓存零改动、
    不崩、幂等**。这是日历缺失时唯一允许的代价（"周一早上若日历缺周日行会多触发一次
    bootstrap，无害"）。"""
    codes = [f"sz.004{i:04d}" for i in range(6)]
    f = make_fetcher(tmp_path, all_stock_codes=codes)   # 不写日历文件
    seed_allstock(f.cache, "2026-09-14", codes)
    kspec = {c: ([("2026-09-10", 10.0), ("2026-09-11", 10.0)],
                 [("2026-09-10", 10.0), ("2026-09-11", 10.0)]) for c in codes}
    for c in codes:
        seed_kline_af3(f.cache, c, [
            ["2026-09-10", c, "10.0000", "0", "1"],
            ["2026-09-11", c, "10.0000", "0", "1"],   # tail == Fri
        ])
    bars = make_bars({c: (c[:6].replace(".", ""), 10.0, 10.0, 1000.0) for c in codes})
    f._snapshot_source = FakeSnapshotSource(bars)
    ksrc = R5KlineSource(kspec)
    f._kline_source = ksrc
    f.set_run_day(date(2026, 9, 14))
    assert f._prev_trade_day(date(2026, 9, 14)) == date(2026, 9, 13)   # 回退：前一个日历日(Sun)
    f._ensure_snapshot("2026-09-14")
    assert f._cutover_gapped == set(codes), "fallback is conservative: Fri tail < Sun prev_td → gapped"
    for c in codes:   # 缓存零改动（(Fri,Mon) 无交易日 → gap_rows 恒空）+ 不崩
        rows = [r[0] for r in f.cache.get(f"kline_af3_{c}")["rows"]]
        assert rows == ["2026-09-10", "2026-09-11"], (
            f"missing-calendar fallback must not append rows (no trading day in Fri..Mon): {rows}"
        )
    # 幂等：重跑同样零改动
    f2 = make_fetcher(tmp_path, all_stock_codes=codes)
    seed_allstock(f2.cache, "2026-09-14", codes)
    f2._snapshot_source = FakeSnapshotSource(bars)
    f2._kline_source = R5KlineSource(kspec)
    f2.set_run_day(date(2026, 9, 14))
    f2._ensure_snapshot("2026-09-14")
    for c in codes:
        rows = [r[0] for r in f2.cache.get(f"kline_af3_{c}")["rows"]]
        assert len(rows) == len(set(rows)) == 2


def test_backfill_defense_in_depth_skip_no_trading_day_in_window(tmp_path):
    """NEW-DEFECT#2 防御纵深（直接单测 _backfill_noncandidate_gaps）：即使 gapped 集合里混入
    tail==prev_td 的股（tail 与 run_day 之间无交易日），循环内 ``tail_date >= prev_td`` →
    continue、**0 次 K线请求**、缓存零改动。"""
    codes = [f"sz.005{i:04d}" for i in range(5)]
    f = make_fetcher(tmp_path, all_stock_codes=codes)
    seed_calendar(f.cache, CAL_SEP)          # prev_td(Thu 09-10) = Wed 09-09
    kspec = {c: ([("2026-09-08", 10.0), ("2026-09-09", 10.0)],
                 [("2026-09-08", 10.0), ("2026-09-09", 10.0)]) for c in codes}
    for c in codes:
        seed_kline_af3(f.cache, c, [
            ["2026-09-08", c, "10.0000", "0", "1"],
            ["2026-09-09", c, "10.0000", "0", "1"],   # tail == prev_td → 窗口 (tail,run_day) 无交易日
        ])
    ksrc = R5KlineSource(kspec)
    f._kline_source = ksrc
    f.set_run_day(RUN_DATE)
    # 直接调用（绕过 _ensure_snapshot 的 gapped 判定）→ 验证循环内防御独立生效
    f._backfill_noncandidate_gaps(RUN_DAY, codes)
    assert ksrc.closes_calls == [], (
        "defense-in-depth: tail >= prev_td must skip the K-line request even if the code was "
        f"mis-flagged gapped upstream; got {len(ksrc.closes_calls)} requests"
    )
    for c in codes:
        rows = [r[0] for r in f.cache.get(f"kline_af3_{c}")["rows"]]
        assert rows == ["2026-09-08", "2026-09-09"], "no rows may be appended"


# ===========================================================================
# F — run_cron.sh 守卫写 trade_calendar.csv（真实守卫体 + fake baostock 子进程，零 live）
# ===========================================================================
def _extract_guard_body() -> str:
    text = (PROJECT_ROOT / "run_cron.sh").read_text(encoding="utf-8")
    m = re.search(r"<<'PYEOF'[^\n]*\n(.*?)\nPYEOF", text, re.DOTALL)
    assert m, "run_cron.sh 中未找到 PYEOF 守卫块"
    return m.group(1)


_FAKE_BS_TMPL = '''
import os

class _Rs:
    def __init__(self, code, msg="", rows=None):
        self.error_code = code; self.error_msg = msg
        self._rows = list(rows or []); self._i = 0
    def next(self):
        if self._i < len(self._rows):
            self._i += 1; return True
        return False
    def get_row_data(self):
        return self._rows[self._i - 1]

FAIL_ATTEMPTS = int(os.environ.get("FAKE_BS_FAIL_ATTEMPTS", "0"))
MODE = os.environ.get("FAKE_BS_MODE", "trading")
_log = os.environ.get("FAKE_BS_LOG")
_calls = {"login": 0}


def _logline(s):
    if _log:
        with open(_log, "a", encoding="utf-8") as fh:
            fh.write(s + "\\n")


def login():
    _calls["login"] += 1
    _logline(f"login attempt={_calls['login']} mode={MODE}")
    if MODE == "login_fail":
        return _Rs("10002001", "网络接收错误")
    if MODE == "flaky_login" and _calls["login"] <= FAIL_ATTEMPTS:
        return _Rs("10002001", "网络接收错误")
    return _Rs("0", "success")


def query_trade_dates(*a, **k):
    _logline("query_trade_dates called")
    if MODE == "trade_err":
        return _Rs("10001011", "黑名单用户，请与管理员联系")
    if MODE == "nontrading":
        return _Rs("0", "ok", [["2026-09-05", "0"]])
    return _Rs("0", "ok", [["2026-09-07", "1"]])


def logout():
    return _Rs("0", "success")
'''


def _run_guard_body(tmp: Path, mode: str, today: str = "2026-09-07", fail_attempts: int = 0) -> tuple:
    """在隔离目录里 re-exec **真实守卫体**（fake baostock 前置遮蔽），返回 (rc, stderr)。"""
    workdir = tmp / "guard"
    fake = workdir / "fakebs"
    fake.mkdir(parents=True, exist_ok=True)
    (fake / "baostock.py").write_text(_FAKE_BS_TMPL, encoding="utf-8")
    (workdir / "guard_body.py").write_text(_extract_guard_body(), encoding="utf-8")
    env = dict(os.environ)
    env["FAKE_BS_MODE"] = mode
    env["FAKE_BS_FAIL_ATTEMPTS"] = str(fail_attempts)
    env["FAKE_BS_LOG"] = str(workdir / "bs_calls.log")
    # fake baostock 前置遮蔽真实库；PROJECT_ROOT 供 `from screener import runstatus`（sidecar）
    env["PYTHONPATH"] = f"{fake}{os.pathsep}{PROJECT_ROOT}"
    r = subprocess.run([sys.executable, "guard_body.py", today],
                       cwd=str(workdir), env=env, capture_output=True, text=True, timeout=120)
    return r.returncode, r.stderr


def _read_calendar(workdir: Path):
    p = workdir / "cache" / "trade_calendar.csv"
    if not p.exists():
        return None
    lines = [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines[0] == "date,is_trading", f"calendar header missing: {lines[:2]}"
    return [ln.split(",") for ln in lines[1:]]


def test_guard_writes_calendar_on_success(tmp_path):
    """守卫成功（交易日）→ exit 0 + cache/trade_calendar.csv 含 (today,1)。"""
    rc, err = _run_guard_body(tmp_path, "trading", today="2026-09-07")
    assert rc == 0, f"guard must exit 0 on trading day; stderr={err}"
    cal = _read_calendar(tmp_path / "guard")
    assert cal is not None, "fix r5: guard must append (today,is_trading) to cache/trade_calendar.csv"
    assert ["2026-09-07", "1"] in cal


def test_guard_writes_nontrading_row(tmp_path):
    """守卫成功（非交易日）→ exit 1 + 日历含 (today,0)（周末行也要落，供 prev_td 跳过）。"""
    rc, err = _run_guard_body(tmp_path, "nontrading", today="2026-09-05")
    assert rc == 1, f"guard must exit 1 on non-trading day; stderr={err}"
    cal = _read_calendar(tmp_path / "guard")
    assert cal is not None and ["2026-09-05", "0"] in cal


def test_guard_failure_writes_no_calendar(tmp_path):
    """守卫失败（login 3 次全挂）→ exit 3 + **不写**日历（下次成功再补）。"""
    rc, err = _run_guard_body(tmp_path, "login_fail", today="2026-09-07")
    assert rc == 3, f"guard must exit 3 on data-source failure; stderr={err}"
    cal = _read_calendar(tmp_path / "guard")
    assert cal is None or all(d != "2026-09-07" for d, _ in cal), (
        "fix r5: failed guard must NOT write the calendar row"
    )


def test_guard_trade_err_writes_no_calendar(tmp_path):
    """query_trade_dates 持续失败（login 正常）→ exit 3 + 不写日历。"""
    rc, err = _run_guard_body(tmp_path, "trade_err", today="2026-09-07")
    assert rc == 3, f"guard must exit 3 on persistent trade_err; stderr={err}"
    cal = _read_calendar(tmp_path / "guard")
    assert cal is None or all(d != "2026-09-07" for d, _ in cal)


def test_guard_dedup_and_sort(tmp_path):
    """同日重复运行 → 去重（1 行）；跨日 append → 升序。"""
    rc1, _ = _run_guard_body(tmp_path, "trading", today="2026-09-07")
    assert rc1 == 0
    # 预置更早的日历行 + 一个乱序晚行，验证重排
    cal_dir = tmp_path / "guard" / "cache"
    existing = _read_calendar(tmp_path / "guard") or []
    rows = dict(existing)
    rows["2026-09-01"] = "1"
    rows["2026-09-04"] = "1"
    (cal_dir / "trade_calendar.csv").write_text(
        "date,is_trading\n" + "".join(f"{d},{v}\n" for d, v in sorted(rows.items(), reverse=True)),
        encoding="utf-8")   # 故意乱序写入
    rc2, err = _run_guard_body(tmp_path, "trading", today="2026-09-07")
    assert rc2 == 0, f"second run must exit 0; stderr={err}"
    cal = _read_calendar(tmp_path / "guard")
    dates = [d for d, _ in cal]
    assert dates.count("2026-09-07") == 1, f"same-day rerun must dedup: {dates}"
    assert dates == sorted(dates), f"calendar must stay ascending: {dates}"
    assert ["2026-09-04", "1"] in cal and ["2026-09-07", "1"] in cal


def test_guard_keeps_last_500_rows(tmp_path):
    """日历超 ~500 行 → 保留最近 500 行（升序、旧行丢弃）。"""
    workdir = tmp_path / "guard"
    fake = workdir / "fakebs"
    fake.mkdir(parents=True, exist_ok=True)
    (fake / "baostock.py").write_text(_FAKE_BS_TMPL, encoding="utf-8")
    (workdir / "guard_body.py").write_text(_extract_guard_body(), encoding="utf-8")
    cal_dir = workdir / "cache"
    cal_dir.mkdir(parents=True, exist_ok=True)
    # 预置 620 个历史行（2024-09-01 起每日一行）
    start = date(2024, 9, 1)
    hist = [(start + timedelta(days=i)).isoformat() for i in range(620)]
    (cal_dir / "trade_calendar.csv").write_text(
        "date,is_trading\n" + "".join(f"{d},1\n" for d in hist), encoding="utf-8")
    env = dict(os.environ)
    env["FAKE_BS_MODE"] = "trading"
    env["PYTHONPATH"] = f"{fake}{os.pathsep}{PROJECT_ROOT}"
    r = subprocess.run([sys.executable, "guard_body.py", "2026-09-07"],
                       cwd=str(workdir), env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"guard must exit 0; stderr={r.stderr}"
    cal = _read_calendar(workdir)
    dates = [d for d, _ in cal]
    assert len(dates) == 500, f"calendar must keep the last ~500 rows, got {len(dates)}"
    assert "2026-09-07" in dates and hist[0] not in dates, "oldest rows must be dropped, newest kept"
    assert dates == sorted(dates)


def test_guard_flaky_login_success_still_writes_calendar_once(tmp_path):
    """回归（r4 重试语义）：login 前 2 次抖动、第 3 次成功 → exit 0 + 日历恰好 1 行。"""
    rc, err = _run_guard_body(tmp_path, "flaky_login", today="2026-09-07", fail_attempts=2)
    assert rc == 0, f"flaky login (2 fails then success) must exit 0; stderr={err}"
    cal = _read_calendar(tmp_path / "guard")
    assert cal is not None and [d for d, _ in cal].count("2026-09-07") == 1
