# -*- coding: utf-8 -*-
"""fix round 4 回归单测（离线，不联网）——对应 tester 打回的 2 个 major + TL 守卫抖动单点。

DEFECT #1 [major]：切换日缺口回补行 tradestatus 写死 "0" → 应为 "1"
  （腾讯 fqkline 只在交易日返回行、停牌日无行 → 有行⟺交易日）。
DEFECT #2 [major]：缺口股把 stale-tail→D-1 的多日漂移当 run-day 除权因子写入
  （伪因子 / 真实缺口事件重复计数）→ run-day preclose 事件仅对稳态候选写入。
守卫单点（TL 新发现）：run_cron.sh 交易日历守卫 login 一次瞬时抖动就 exit 3 跳过
  整个 cron → login + query_trade_dates 加重试（最多 3 次、指数退避 2s/5s）。

所有 fakes 独立构造（与 test_v4_datasource.py / tester conftest 同源语义，不共享对象）。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

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

RUN_DAY = "2026-09-08"


# ===========================================================================
# helpers（独立构造）
# ===========================================================================
def make_bars(spec: Dict[str, tuple]) -> Dict[str, StockBar]:
    """spec={code:(name,close,preclose,vol)} → {code:StockBar}（派生 is_st/tradestatus）。"""
    out = {}
    for code, (name, close, preclose, vol) in spec.items():
        pct = (close / preclose - 1.0) * 100.0 if preclose else 0.0
        b = StockBar(code=code, name=name, close=close, preclose=preclose, open=preclose,
                     volume_hand=vol, ts="20260908150000", pct_chg=pct,
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


class FakeKlineSource:
    """spec={code:(raw_closes, qfq_closes)}；qfq 锚定最新价 → 事件间比值恒=1、除权日跳 r。"""

    def __init__(self, spec, step_pct: float = 0.5):
        self._spec = {c: (list(r), list(q)) for c, (r, q) in spec.items()}
        self.step = step_pct / 100.0

    def kline_closes(self, code):
        raw, _q = self._spec.get(code, ([], []))
        return list(raw)

    def gap_events(self, code, start_after=None, end_before=None):
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
            if prev and abs(ratio / prev - 1.0) > self.step:
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
                            "cutover_max_gap_backfill": 3000},
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


def make_fetcher(tmp_path, ds_cfg=None, all_stock_rows=None, snapshot_bars=None):
    client = FakeBSClient(all_stock_rows)
    cache = DiskCache(str(tmp_path / "cache"))
    f = DataFetcher(client, cache, ds_cfg or tencent_ds_cfg())
    if snapshot_bars is not None:
        f._snapshot_source = FakeSnapshotSource(snapshot_bars)
    return f


# ===========================================================================
# DEFECT #1 — 缺口回补行 tradestatus 必须为 "1"（有行⟺交易日）
# ===========================================================================
def test_r4_gap_backfill_tradestatus_1_noncandidate(tmp_path):
    """非候选缺口股：腾讯 raw K线在缺口日有行（=当日实际交易）→ 回补行 tradestatus="1"。"""
    f = make_fetcher(tmp_path, tencent_ds_cfg(), all_stock_rows=[["sz.000001", "1", "平安银行"]],
                     snapshot_bars=make_bars({"sz.000001": ("平安银行", 11.70, 11.85, 400000)}))
    seed_kline_af3(f.cache, "sz.000001", [
        ["2026-09-03", "sz.000001", "11.8000", "0", "1"],
        ["2026-09-04", "sz.000001", "11.8500", "0", "1"],   # tail（BaoStock 停更）
    ])

    class Ks:
        def kline_closes(self, code):
            return [("2026-09-04", 11.85), ("2026-09-05", 11.82), ("2026-09-06", 11.79),
                    ("2026-09-07", 11.85)]
        def gap_events(self, code, start_after=None, end_before=None):
            return self.kline_closes(code), []
    f._kline_source = Ks()
    f.set_run_day(date(2026, 9, 8))
    f._ensure_snapshot(RUN_DAY)

    rows = {r[0]: r for r in f.cache.get("kline_af3_sz.000001")["rows"]}
    for d in ("2026-09-05", "2026-09-06", "2026-09-07"):
        assert d in rows, f"gap day {d} missing (no backfill)"
        assert float(rows[d][2]) > 0
        assert rows[d][4] == "1", (
            f"DEFECT#1: gap backfill wrote tradestatus={rows[d][4]!r} for {d} — a day the stock "
            f"actually TRADED (raw-kline row present). Present raw row ⟺ trading day; v3 PIT "
            f"exec_price reads ts=0 → treats the trading day as suspended."
        )
        assert rows[d][3] == "0"   # isST 仍占位 0（M2：历史日无快照可派生戴帽状态）


def test_r4_gap_backfill_tradestatus_1_candidate_path(tmp_path):
    """候选缺口股（_populate_cutover_events 路径）回补行同样 tradestatus="1"。"""
    f = make_fetcher(tmp_path, tencent_ds_cfg(), all_stock_rows=[["sh.600036", "1", "招商银行"]],
                     snapshot_bars=make_bars({"sh.600036": ("招商银行", 9.65, 9.70, 500000)}))
    seed_kline_af3(f.cache, "sh.600036", [["2026-09-04", "sh.600036", "10.0000", "0", "1"]])
    kspec = {"sh.600036": (
        [("2026-09-04", 10.0), ("2026-09-05", 9.80), ("2026-09-06", 9.75), ("2026-09-07", 9.70)],
        [("2026-09-04", 9.80), ("2026-09-05", 9.80), ("2026-09-06", 9.75), ("2026-09-07", 9.70)],
    )}
    f._kline_source = FakeKlineSource(kspec)
    f.set_run_day(date(2026, 9, 8))
    f._ensure_snapshot(RUN_DAY)
    assert "sh.600036" in f._candidates
    rows = {r[0]: r for r in f.cache.get("kline_af3_sh.600036")["rows"]}
    for d in ("2026-09-05", "2026-09-06", "2026-09-07"):
        assert rows[d][4] == "1", f"DEFECT#1: candidate-path backfill ts={rows[d][4]!r} for {d}"


def test_r4_gap_backfill_no_date_holes_and_idempotent(tmp_path):
    """回补后 tail→run_day 无日期洞；重跑（新 fetcher、同缓存）零重复行。"""
    def build():
        f = make_fetcher(tmp_path, tencent_ds_cfg(), all_stock_rows=[["sz.000001", "1", "平安银行"]],
                         snapshot_bars=make_bars({"sz.000001": ("平安银行", 11.70, 11.85, 400000)}))
        seed_kline_af3(f.cache, "sz.000001", [["2026-09-04", "sz.000001", "11.8500", "0", "1"]])

        class Ks:
            def kline_closes(self, code):
                return [("2026-09-05", 11.82), ("2026-09-06", 11.79), ("2026-09-07", 11.85)]
            def gap_events(self, code, start_after=None, end_before=None):
                return self.kline_closes(code), []
        f._kline_source = Ks()
        f.set_run_day(date(2026, 9, 8))
        return f

    f1 = build()
    f1._ensure_snapshot(RUN_DAY)
    n1 = len(f1.cache.get("kline_af3_sz.000001")["rows"])
    # 无日期洞：09-04..09-07 连续（run-day 行由 kline_af3_incremental 追加）
    f1.kline_af3_incremental("sz.000001", RUN_DAY)
    dates = [r[0] for r in f1.cache.get("kline_af3_sz.000001")["rows"]]
    assert dates == ["2026-09-04", "2026-09-05", "2026-09-06", "2026-09-07", "2026-09-08"], \
        f"date holes or missing run-day row: {dates}"
    # 幂等：重跑 bootstrap（tail 已含缺口行但仍<run_day）→ 零重复日期
    f2 = build()
    f2._ensure_snapshot(RUN_DAY)
    dates2 = [r[0] for r in f2.cache.get("kline_af3_sz.000001")["rows"]]
    assert len(dates2) == len(set(dates2)), f"duplicate dates after rerun: {dates2}"
    assert len(f2.cache.get("kline_af3_sz.000001")["rows"]) >= n1


# ===========================================================================
# DEFECT #2 — run-day preclose 事件仅对稳态候选写入
# ===========================================================================
def test_r4_gapped_candidate_no_spurious_run_day_factor(tmp_path):
    """缺口股漂移>θ（无真实除权）→ 不得写 run-day 伪因子（qfq/raw 无事件 → 0 写入）。"""
    f = make_fetcher(tmp_path, tencent_ds_cfg(), all_stock_rows=[["sh.600036", "1", "招商银行"]],
                     snapshot_bars=make_bars({"sh.600036": ("招商银行", 40.90, 41.07, 500000)}))
    seed_kline_af3(f.cache, "sh.600036", [
        ["2026-09-03", "sh.600036", "41.0700", "0", "1"],
        ["2026-09-04", "sh.600036", "41.6900", "0", "1"],   # stale tail
    ])
    seed_adjfactor(f.cache, "sh.600036", [("2026-07-10", 5.742256)])
    kspec = {"sh.600036": (
        [("2026-09-04", 41.69), ("2026-09-05", 41.50), ("2026-09-06", 41.30),
         ("2026-09-07", 41.07)],
        [("2026-09-04", 41.69), ("2026-09-05", 41.50), ("2026-09-06", 41.30),
         ("2026-09-07", 41.07)],   # qfq==raw → 无真实除权
    )}
    f._kline_source = FakeKlineSource(kspec)
    f.set_run_day(date(2026, 9, 8))
    f._ensure_snapshot(RUN_DAY)
    assert "sh.600036" in f._candidates            # drift -1.49% > θ → 候选
    assert f._cutover_events.get("sh.600036") in (None, [])
    changed = f.maybe_refresh_adjfactor("sh.600036", [])
    rows = [r[1] for r in f.cache.get("adjfactor_sh.600036")["rows"]]
    assert "2026-09-08" not in rows, (
        f"DEFECT#2: spurious run-day factor written for a gapped stock with NO real ex-div; "
        f"cand.r_event=close(tail)/close(D-1) is multi-day drift. rows={rows}"
    )
    assert changed is False and len(rows) == 1     # 未追加任何事件


def test_r4_steady_candidate_still_writes_run_day_factor(tmp_path):
    """稳态候选（tail==run_day）→ run-day preclose 事件照常写入（修复不得误伤正常路径）。"""
    f = make_fetcher(tmp_path, tencent_ds_cfg(), all_stock_rows=[["sh.600036", "1", "招商银行"]],
                     snapshot_bars=make_bars({"sh.600036": ("招商银行", 36.88, 36.547, 1000)}))
    seed_kline_af3(f.cache, "sh.600036", [
        ["2026-09-07", "sh.600036", "41.0700", "0", "1"],
        ["2026-09-08", "sh.600036", "36.8800", "0", "1"],   # tail==run_day（稳态）
    ])
    seed_adjfactor(f.cache, "sh.600036", [("2025-07-10", 5.589333)])
    f.set_run_day(date(2026, 9, 8))
    changed = f.maybe_refresh_adjfactor("sh.600036", [])
    assert changed is True
    rows = {r[1]: float(r[3]) for r in f.cache.get("adjfactor_sh.600036")["rows"]}
    r_event = 41.07 / 36.547   # close(D-1)/preclose_today = 真当日除权比
    assert rows["2026-09-08"] == pytest.approx(round(5.589333 * r_event, 6), abs=1e-6)


def test_r4_gapped_real_exdiv_not_double_counted(tmp_path):
    """缺口期真实除权（qfq/raw 在真 ex_date 检出）→ 只记一次，run-day 事件不重复计数。"""
    f = make_fetcher(tmp_path, tencent_ds_cfg(), all_stock_rows=[["sh.600036", "1", "招商银行"]],
                     snapshot_bars=make_bars({"sh.600036": ("招商银行", 9.65, 9.70, 500000)}))
    seed_kline_af3(f.cache, "sh.600036", [["2026-09-04", "sh.600036", "10.0000", "0", "1"]])
    seed_adjfactor(f.cache, "sh.600036", [("2025-07-10", 5.0)])
    # 真实除权在 09-05（raw 台阶 10.0→9.80）；qfq 锚定最新 → 比值在 09-05 跳 1/0.98
    kspec = {"sh.600036": (
        [("2026-09-04", 10.0), ("2026-09-05", 9.80), ("2026-09-06", 9.75), ("2026-09-07", 9.70)],
        [("2026-09-04", 9.80), ("2026-09-05", 9.80), ("2026-09-06", 9.75), ("2026-09-07", 9.70)],
    )}
    f._kline_source = FakeKlineSource(kspec)
    f.set_run_day(date(2026, 9, 8))
    f._ensure_snapshot(RUN_DAY)
    assert "sh.600036" in f._candidates   # drift -1% > θ → 候选（run-day 事件本会被写）
    evs = f._cutover_events.get("sh.600036", [])
    assert len(evs) == 1 and evs[0][0] == "2026-09-05"
    changed = f.maybe_refresh_adjfactor("sh.600036", [])
    rows = {r[1]: float(r[3]) for r in f.cache.get("adjfactor_sh.600036")["rows"]}
    assert changed is True
    assert "2026-09-05" in rows, "real gap ex-div at 09-05 must be recorded"
    assert rows["2026-09-05"] == pytest.approx(5.0 * (1.0 / 0.98), abs=1e-3)
    assert "2026-09-08" not in rows, (
        f"DEFECT#2 corollary: run-day event double-counts the 09-05 ex-div already applied at "
        f"the real ex_date. rows={rows}"
    )


def test_r4_production_seq_gapped_still_suppressed_after_run_day_append(tmp_path):
    """生产时序回归：stage2 kline_af3_incremental 已把 run-day 行 append（tail 变==run_day）
    → maybe_refresh 不得因此把缺口股误判成稳态而写伪因子（须用检测时缺口集合判定）。"""
    f = make_fetcher(tmp_path, tencent_ds_cfg(), all_stock_rows=[["sh.600036", "1", "招商银行"]],
                     snapshot_bars=make_bars({"sh.600036": ("招商银行", 40.90, 41.07, 500000)}))
    seed_kline_af3(f.cache, "sh.600036", [
        ["2026-09-03", "sh.600036", "41.0700", "0", "1"],
        ["2026-09-04", "sh.600036", "41.6900", "0", "1"],   # stale tail（cutover 检测时缺口）
    ])
    seed_adjfactor(f.cache, "sh.600036", [("2026-07-10", 5.742256)])
    kspec = {"sh.600036": (
        [("2026-09-04", 41.69), ("2026-09-05", 41.50), ("2026-09-06", 41.30),
         ("2026-09-07", 41.07)],
        [("2026-09-04", 41.69), ("2026-09-05", 41.50), ("2026-09-06", 41.30),
         ("2026-09-07", 41.07)],
    )}
    f._kline_source = FakeKlineSource(kspec)
    f.set_run_day(date(2026, 9, 8))
    f._ensure_snapshot(RUN_DAY)
    assert "sh.600036" in f._candidates
    # 模拟生产 stage2：引擎循环先跑 kline_af3_incremental → append run-day 行
    kd = f.kline_af3_incremental("sh.600036", RUN_DAY)
    assert kd is not None and kd.current_price == pytest.approx(40.90)
    # 此时缓存尾==run_day；若用当下 tail 判定会误写伪因子（旧逻辑的缺陷现场）
    changed = f.maybe_refresh_adjfactor("sh.600036", [])
    rows = [r[1] for r in f.cache.get("adjfactor_sh.600036")["rows"]]
    assert "2026-09-08" not in rows, (
        f"DEFECT#2 (production sequence): run-day factor written after stage2 appended the "
        f"run-day row — gapped status must come from cutover detection time, not current tail. "
        f"rows={rows}"
    )
    assert changed is False


# ===========================================================================
# 守卫单点 — login 抖动重试（真实 run_cron.sh 守卫体 + fake baostock，零 live）
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
    if MODE == "trade_err":
        return _Rs("10001011", "黑名单用户，请与管理员联系")
    if MODE == "nontrading":
        return _Rs("0", "ok", [["2026-09-05", "0"]])
    return _Rs("0", "ok", [["2026-09-07", "1"]])


def logout():
    return _Rs("0", "success")
'''


def _run_guard_body(tmp: Path, mode: str, fail_attempts: int = 0) -> tuple:
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
    r = subprocess.run([sys.executable, "guard_body.py", "2026-09-07"],
                       cwd=str(workdir), env=env, capture_output=True, text=True, timeout=120)
    return r.returncode, r.stderr


def test_r4_guard_retry_login_flaky_then_success(tmp_path):
    """守卫重试：login 前 2 次抖动失败、第 3 次成功 → exit 0（交易日），不跳过 cron。"""
    rc, err = _run_guard_body(tmp_path, "flaky_login", fail_attempts=2)
    assert rc == 0, f"flaky login (2 fails then success) must exit 0, got {rc}; stderr={err}"
    log = (tmp_path / "guard" / "bs_calls.log").read_text(encoding="utf-8")
    attempts = [ln for ln in log.splitlines() if ln.startswith("login attempt=")]
    assert len(attempts) == 3, f"expected exactly 3 login attempts, got {attempts}"


def test_r4_guard_retry_exhausted_fails_with_sidecar(tmp_path):
    """守卫重试：login 3 次全失败 → exit 3 + 写 failed sidecar（现有 fail 语义保留）。"""
    rc, err = _run_guard_body(tmp_path, "login_fail", fail_attempts=99)
    assert rc == 3, f"all-retry-failure must exit 3, got {rc}; stderr={err}"
    log = (tmp_path / "guard" / "bs_calls.log").read_text(encoding="utf-8")
    attempts = [ln for ln in log.splitlines() if ln.startswith("login attempt=")]
    assert len(attempts) == 3, f"expected exactly 3 login attempts, got {attempts}"
    sidecars = list((tmp_path / "guard" / "output").glob("run_status_*.json"))
    assert sidecars, "retry-exhausted failure must write run_status sidecar"
    import json
    sc = json.loads(sidecars[0].read_text(encoding="utf-8"))
    assert sc["status"] == "failed" and "重试 3 次均失败" in sc["error"]


def test_r4_guard_trade_err_retry_exhausted(tmp_path):
    """query_trade_dates 持续失败（login 正常）→ 重试 3 次后 exit 3 + sidecar。"""
    rc, err = _run_guard_body(tmp_path, "trade_err")
    assert rc == 3, f"persistent trade_err must exit 3 after retries, got {rc}; stderr={err}"
    log = (tmp_path / "guard" / "bs_calls.log").read_text(encoding="utf-8")
    attempts = [ln for ln in log.splitlines() if ln.startswith("login attempt=")]
    assert len(attempts) == 3, f"trade_err must retry the full login+query unit, got {attempts}"
    sidecars = list((tmp_path / "guard" / "output").glob("run_status_*.json"))
    assert sidecars, "retry-exhausted failure must write run_status sidecar"


def test_r4_guard_nontrading_still_exit_1(tmp_path):
    """回归：非交易日（数据源正常）→ exit 1，语义不变。"""
    rc, err = _run_guard_body(tmp_path, "nontrading")
    assert rc == 1, f"non-trading day must exit 1, got {rc}; stderr={err}"
