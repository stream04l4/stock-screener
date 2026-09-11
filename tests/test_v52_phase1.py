# -*- coding: utf-8 -*-
"""v5.2 Phase 1 单测：raw 层 + canonical 统一 Schema + 交叉校验 v1 + 健康度。

纪律（brief 硬约束）：
- **零网络**：全部离线——fixture=stages/01_research/evidence 留样（已复制进
  tests/fixtures/v52/）+ mock；akshare 客户端用 sys.modules 假模块注入（不 import 真包）。
- 覆盖 rawstore / canonical / crosscheck(三字段+EmptyPayloadGuard) / health /
  akshare 客户端 / report 健康度段 / 回滚路径（canonical.enabled=false → no-op）。
"""
from __future__ import annotations

import csv
import json
import os
import sys

import pytest

from screener.data import rawstore, canonical, crosscheck as xc
from screener.data.rawstore import RawStore
from screener.data.canonical import CanonicalStore
from screener.health import HealthTracker, render_report_section_from_summary

V52 = os.path.join(os.path.dirname(__file__), "fixtures", "v52")


# ===========================================================================
# rawstore：append-only 原始响应层
# ===========================================================================
def test_rawstore_record_json_envelope(tmp_path):
    """落盘信封结构：source/endpoint/fetched_at/rows/status/data 齐全，目录={source}/{date}。"""
    s = RawStore(str(tmp_path))
    p = s.record_json("tencent", "snapshot", [{"code": "sh.601398", "close": 8.11, "ts": "20260911150000"}],
                      date_s="2026-09-11")
    assert p is not None and os.path.exists(p)
    assert p.startswith(str(tmp_path / "tencent" / "2026-09-11"))
    env = s.read_envelope(p)
    assert env["source"] == "tencent" and env["endpoint"] == "snapshot"
    assert env["rows"] == 1 and env["status"] == "ok"
    assert env["data"][0]["close"] == 8.11
    assert env["fetched_at"].endswith("Z")  # UTC ISO


def test_rawstore_idempotent_same_source_date_endpoint(tmp_path):
    """文件级幂等：同 (source, endpoint, date) 重复写 → 跳过不重写（内容保持首次）。"""
    s = RawStore(str(tmp_path))
    p1 = s.record_json("akshare_em", "fhps_601398", [{"v": 1}], date_s="2026-09-11")
    p2 = s.record_json("akshare_em", "fhps_601398", [{"v": 2}], date_s="2026-09-11")
    assert p1 == p2
    assert s.skip_count == 1 and s.write_count == 1
    env = s.read_envelope(p1)
    assert env["data"] == [{"v": 1}]  # 首次内容保持（append-only，不覆盖）


def test_rawstore_atomic_no_partial_files(tmp_path):
    """原子写：落盘后无 .tmp 残留；list_files 只认 .json。"""
    s = RawStore(str(tmp_path))
    s.record_json("sina", "cf_601398_p1", {"result": {}}, date_s="2026-09-11")
    files = s.list_files(source="sina")
    assert len(files) == 1 and ".tmp." not in os.path.basename(files[0])


def test_rawstore_history_nonempty_for_guard(tmp_path):
    """EmptyPayloadGuard 依赖：历史非空判断（跨日期扫描、按 endpoint 过滤）。"""
    s = RawStore(str(tmp_path))
    assert not s.history_nonempty("akshare_em", "fhps_601398", "2026-09-11")
    s.record_json("akshare_em", "fhps_601398", [{"a": 1}], date_s="2026-09-01")
    assert s.history_nonempty("akshare_em", "fhps_601398", "2026-09-11")
    # 不同 endpoint 不算同类接口历史
    assert not s.history_nonempty("akshare_em", "daily_601398", "2026-09-11")


def test_rawstore_hook_noop_when_disabled(monkeypatch, tmp_path):
    """回滚语义：canonical.enabled=false → record_response 全 no-op（不建目录、不写文件）。"""
    monkeypatch.setattr(rawstore, "_enabled_from_cfg", lambda: False)
    monkeypatch.setattr(rawstore, "_resolve_root", lambda: str(tmp_path))
    rawstore.reset_raw_store()
    try:
        assert rawstore.record_response("tencent", "snapshot", [{"x": 1}], date_s="2026-09-11") is None
        # no-op：连目录都不创建（最严格的逐字节不变证明）
        assert not os.path.exists(str(tmp_path)) or not os.listdir(str(tmp_path))
    finally:
        rawstore.reset_raw_store()


def test_rawstore_hook_writes_when_enabled(monkeypatch, tmp_path):
    """启用时钩子落盘（root 指向 tmp，不污染仓库）。"""
    monkeypatch.setattr(rawstore, "_enabled_from_cfg", lambda: True)
    monkeypatch.setattr(rawstore, "_resolve_root", lambda: str(tmp_path))
    rawstore.reset_raw_store()
    try:
        p = rawstore.record_response("baostock", "profit_sh.601398_2025Q4",
                                     [["sh.601398", "0.0897"]], date_s="2026-09-11")
        assert p is not None and os.path.exists(p)
    finally:
        rawstore.reset_raw_store()


# ===========================================================================
# canonical：统一 Schema 派生层（close/dps/roe + source/fetched_at/data_version）
# ===========================================================================
def test_canonical_append_close_with_trace_fields(tmp_path):
    """close 记录列契约：code/date/value + source/fetched_at/data_version（brief 三溯源字段）。"""
    c = CanonicalStore(str(tmp_path))
    assert c.columns_for("close") == ["code", "date", "value", "source", "fetched_at", "data_version"]
    assert c.append("close", "sh.601398", "2026-09-11", 8.11, source="tencent") is True
    rows = c.read("close")
    assert len(rows) == 1
    r = rows[0]
    assert r["code"] == "sh.601398" and r["date"] == "2026-09-11" and r["value"] == "8.11"
    assert r["source"] == "tencent" and r["data_version"] == "v5.2-p1"
    assert r["fetched_at"].endswith("Z")


def test_canonical_idempotent_same_key(tmp_path):
    """幂等：同 (code, date) 不重写（append-only + 去重）。"""
    c = CanonicalStore(str(tmp_path))
    assert c.append("close", "sh.601398", "2026-09-11", 8.11, source="tencent") is True
    assert c.append("close", "sh.601398", "2026-09-11", 9.99, source="sina") is False  # 跳过
    rows = c.read("close")
    assert len(rows) == 1 and rows[0]["value"] == "8.11"  # 首值保持


def test_canonical_dps_and_roe_keys(tmp_path):
    """dps 键=ex_date（事件驱动）；roe 键=period(YYYYQn)（报告期口径）。"""
    c = CanonicalStore(str(tmp_path))
    assert c.append("dps", "601398", "2026-05-13", 0.1689, source="em_static") is True
    assert c.append("roe", "sh.601398", "2025Q4", 8.9739, source="baostock") is True
    assert c.read("dps")[0]["ex_date"] == "2026-05-13"
    assert c.read("roe")[0]["period"] == "2025Q4"


def test_canonical_unknown_field_raises(tmp_path):
    c = CanonicalStore(str(tmp_path))
    with pytest.raises(ValueError):
        c.append("pe_ttm", "sh.601398", "2026-09-11", 1.0, source="x")


def test_canonical_build_from_raw_envelopes(tmp_path):
    """canonical 从 raw 构建（不直接读网络）：tencent→close / akshare_em→dps / sina→roe。"""
    raw_root = tmp_path / "raw"
    os.makedirs(raw_root / "tencent" / "2026-09-11")
    os.makedirs(raw_root / "akshare_em" / "2026-09-11")
    with open(raw_root / "tencent/2026-09-11/snapshot.0.json", "w", encoding="utf-8") as f:
        json.dump({"source": "tencent", "endpoint": "snapshot", "fetched_at": "2026-09-11T15:00:00Z",
                   "status": "ok", "rows": 1, "data": [
                       {"code": "sh.601398", "close": 8.11, "ts": "20260911150000"}]}, f)
    with open(raw_root / "akshare_em/2026-09-11/fhps_601398.0.json", "w", encoding="utf-8") as f:
        json.dump({"source": "akshare_em", "endpoint": "fhps_601398", "fetched_at": "2026-09-11T15:01:00Z",
                   "status": "ok", "rows": 1, "data": [
                       {"code": "601398", "ex_date": "2026-05-13", "dps_per_share": 0.1689}]}, f)
    c = CanonicalStore(str(tmp_path / "canonical"))
    counts = c.build_from_raw(str(raw_root), {"tencent": "close", "akshare_em": "dps"})
    assert counts == {"close": 1, "dps": 1, "roe": 0}
    r = c.read("close")[0]
    assert r["value"] == "8.11" and r["source"] == "tencent"
    # fetched_at 继承自 raw 信封（溯源链完整）
    assert r["fetched_at"] == "2026-09-11T15:00:00Z"


def test_canonical_build_skips_error_envelopes(tmp_path):
    """status=error 的 raw 信封不派生 canonical（失败响应不进统一 Schema）。"""
    raw_root = tmp_path / "raw"
    os.makedirs(raw_root / "akshare_em" / "2026-09-11")
    with open(raw_root / "akshare_em/2026-09-11/fhps_601398.0.json", "w", encoding="utf-8") as f:
        json.dump({"source": "akshare_em", "endpoint": "fhps_601398", "status": "error",
                   "rows": 0, "data": None}, f)
    c = CanonicalStore(str(tmp_path / "canonical"))
    counts = c.build_from_raw(str(raw_root), {"akshare_em": "dps"})
    assert counts["dps"] == 0


# ===========================================================================
# crosscheck：三字段校验（阈值按实测校准）+ EmptyPayloadGuard
# ===========================================================================
def test_check_close_within_tolerance():
    """非除权日 |Δ|≤0.1% → ok（evidence: 52/52 零偏差基线）。"""
    r = xc.check_close("sh.601398", 8.11, 8.11, tol_pct=0.1)
    assert r["ok"] is True and r["diff_pct"] == 0.0
    # 边界内：8.11 vs 8.117（0.086%）
    r2 = xc.check_close("sh.601398", 8.11, 8.117, tol_pct=0.1)
    assert r2["ok"] is True


def test_check_close_exceeds_tolerance():
    """|Δ|>0.1% → 冲突（0.5% 太松——报告 §4 校准）。"""
    r = xc.check_close("sh.601398", 8.11, 8.15, tol_pct=0.1)  # evidence 当日未收盘差 0.49%
    assert r["ok"] is False and abs(r["diff_pct"] - 0.4932) < 0.01


def test_check_close_missing_values_skip():
    """缺值 → ok=None（无法比较，不计冲突）。"""
    assert xc.check_close("x", None, 8.11, 0.1)["ok"] is None
    assert xc.check_close("x", 8.11, 0.0, 0.1)["ok"] is None


def test_check_r_event_consistency():
    """除权日改校验 r_event 一致性（不校验 close 绝对值——腾讯 idx4 系统性口径差）。"""
    assert xc.check_r_event("sh.601398", 1.02, 1.02, tol_pct=1.0)["ok"] is True
    bad = xc.check_r_event("sh.601398", 1.02, 1.05, tol_pct=1.0)
    assert bad["ok"] is False


def test_check_dps_levels():
    """DPS 三级：|Δ|<warn→ok；≥warn→warn；>stop→review（停算待复核）。"""
    ok = xc.check_dps("601398", 0.1689, 0.1689, warn_at=0.01, stop_at=0.05)
    assert ok["level"] == "ok" and ok["diff"] == 0.0
    warn = xc.check_dps("601398", 0.1689, 0.175, warn_at=0.01, stop_at=0.05)  # Δ=0.0061<0.01? → ok
    assert warn["level"] == "ok"
    warn2 = xc.check_dps("601398", 0.1689, 0.18, warn_at=0.01, stop_at=0.05)   # Δ=0.0111≥0.01
    assert warn2["level"] == "warn"
    rev = xc.check_dps("601398", 0.1689, 0.25, warn_at=0.01, stop_at=0.05)     # Δ=0.0811>0.05
    assert rev["level"] == "review"
    skip = xc.check_dps("601398", None, 0.17, warn_at=0.01, stop_at=0.05)
    assert skip["level"] == "skip"


def test_fhps_bonus_per10_to_dps():
    """akshare '现金分红比例'=每10股口径 → /10（boundary_probe_round5 实测）。"""
    assert xc.fhps_bonus_to_dps(1.689) == pytest.approx(0.1689)
    assert xc.fhps_bonus_to_dps(None) is None
    assert xc.fhps_bonus_to_dps("bad") is None


def test_check_roe_baseline_601398():
    """ROE 换算：BaoStock 小数×100 vs 新浪百分数。601398 实测 8.9739% vs 9.45% = 0.48pp
    → ≤1pp 阈值内 ok（口径差非错误；阈值勿收紧到 <0.5pp——会误报全部正常股）。"""
    r = xc.check_roe("sh.601398", 0.089739, 9.45, tol_pp=1.0)
    assert r["ok"] is True
    assert abs(r["diff_pp"] - 0.4761) < 0.001
    # 超阈值 → 冲突
    bad = xc.check_roe("sh.601398", 0.0897, 12.5, tol_pp=1.0)
    assert bad["ok"] is False


def test_empty_payload_guard_suspected_gap():
    """(a) 调用成功 rows==0 且历史非空 → suspected_gap（不写 canonical、不覆盖静态底表）。"""
    r = xc.empty_payload_guard("akshare_em", "fhps_601398", 0, history_nonempty=True)
    assert r["status"] == "suspected_gap"
    # 历史也为空（真新股/无分红股）→ ok，不告警
    assert xc.empty_payload_guard("akshare_em", "fhps_301999", 0, history_nonempty=False)["status"] == "ok"
    # 非空 → ok
    assert xc.empty_payload_guard("akshare_em", "fhps_601398", 23, history_nonempty=True)["status"] == "ok"


def test_empty_payload_guard_market_level():
    """(b) 市场级接口(all_stock)交易日 rows<5000 → source_anomaly（复用 universe 守卫语义）。"""
    r = xc.empty_payload_guard("baostock", "all_stock", 0, history_nonempty=True,
                               market_level=True, min_market_rows=5000)
    assert r["status"] == "source_anomaly"
    assert xc.empty_payload_guard("baostock", "all_stock", 7373, True,
                                  market_level=True, min_market_rows=5000)["status"] == "ok"


# ===========================================================================
# health：健康度汇总 + report 段 + badge payload
# ===========================================================================
def test_health_tracker_summary_and_badge():
    """成功率/冲突数/suspected_gap/待复核标的 汇总；badge 仅异常时 anomalies 非空。"""
    t = HealthTracker(run_day="2026-09-11")
    t.note_call("tencent", True); t.note_call("tencent", False)
    t.note_call("akshare_sina", True)
    t.add_check("close", {"code": "a", "ok": False})
    t.add_check("dps", {"code": "b", "level": "review"})
    t.add_gap({"source": "akshare_em", "endpoint": "fhps_x", "rows": 0, "status": "suspected_gap"})
    t.add_review("sh.601398", "dps Δ>stop")
    s = t.summary()
    assert s["sources"]["tencent"] == {"ok": 1, "fail": 1, "success_rate_pct": 50.0}
    assert s["conflicts"] == {"close": 1, "dps": 1, "roe": 0}
    assert s["suspected_gaps"][0]["status"] == "suspected_gap"
    assert s["review_codes"] == ["sh.601398"]
    p = t.to_badge_payload()
    assert p["has_anomaly"] is True and len(p["anomalies"]) >= 3


def test_health_tracker_normal_day_no_badge():
    """正常日：has_anomaly=False、anomalies 空（Web badge 不显示——Q5）。"""
    t = HealthTracker(run_day="2026-09-11")
    t.note_call("tencent", True)
    t.add_check("close", {"code": "a", "ok": True})
    p = t.to_badge_payload()
    assert p["has_anomaly"] is False and p["anomalies"] == []


def test_health_report_section_render(tmp_path):
    """report 固定段：异常态含状态行/源表/gap/待复核；正常态标注'正常'。"""
    t = HealthTracker(run_day="2026-09-11")
    t.note_call("tencent", True)
    t.add_check("close", {"code": "a", "ok": False})
    t.add_gap({"source": "akshare_em", "endpoint": "fhps_x", "rows": 0, "status": "suspected_gap"})
    t.add_review("sh.601398", "x")
    lines = render_report_section_from_summary(t.summary())
    text = "\n".join(lines)
    assert "## 数据源健康度" in text
    assert "**状态：⚠️ 异常**" in text
    assert "| tencent | 1 | 0 |" in text
    assert "疑似缺数据" in text and "待复核标的" in text

    t2 = HealthTracker(run_day="2026-09-11")
    t2.note_call("tencent", True)
    lines2 = render_report_section_from_summary(t2.summary())
    assert "**状态：正常**" in "\n".join(lines2)


def test_health_payload_save_load_roundtrip(tmp_path):
    """sidecar JSON 落盘/读回（Web badge 通道）。"""
    t = HealthTracker(run_day="2026-09-11")
    t.note_call("tencent", False)
    p = t.save(str(tmp_path))
    assert p is not None and os.path.exists(p)
    loaded = HealthTracker.load(str(tmp_path), "2026-09-11")
    assert loaded is not None and loaded["has_anomaly"] is True
    assert HealthTracker.load(str(tmp_path), "2020-01-01") is None  # 无该日 → None


# ===========================================================================
# akshare 客户端（mock 注入，零网络）：口径换算 / breaker 降级 / 契约漂移
# ===========================================================================
class _FakeDF:
    """最小 DataFrame 替身（len + iterrows）。"""

    def __init__(self, rows, columns):
        self._rows = rows
        self.columns = list(columns)

    def __len__(self):
        return len(self._rows)

    def iterrows(self):
        for i, r in enumerate(self._rows):
            yield i, dict(r)


def _install_fake_akshare(monkeypatch, daily_df=None, fhps_df=None, raise_on=None):
    """sys.modules 注入假 akshare（pin 版本匹配；不 import 真包、零网络）。"""
    import types

    fake = types.ModuleType("akshare")
    fake.__version__ = "1.18.88"

    def _daily(symbol, start_date, end_date):
        if raise_on == "daily":
            raise ConnectionError("mock RemoteDisconnected")
        return daily_df

    def _fhps(symbol):
        if raise_on == "fhps":
            raise ConnectionError("mock RemoteDisconnected")
        return fhps_df

    fake.stock_zh_a_daily = _daily
    fake.stock_fhps_detail_em = _fhps
    monkeypatch.setitem(sys.modules, "akshare", fake)


def test_akshare_daily_closes_parse(monkeypatch):
    """daily 解析：[{date, close}] 升序、NaN→None 行剔除。"""
    import math
    df = _FakeDF([
        {"date": "2026-09-10", "close": 8.10},
        {"date": "2026-09-11", "close": 8.11},
        {"date": float("nan"), "close": 9.9},  # NaN 行剔除
    ], ["date", "close"])
    _install_fake_akshare(monkeypatch, daily_df=df)
    from screener.data.akshare_src import AkshareValidationClient
    c = AkshareValidationClient({"akshare_interval_s": 0, "akshare_max_attempts": 2,
                                 "akshare_throttle_enabled": False, "akshare_retry_sleep_s": 0})
    out = c.daily_closes("601398", "20260901", "20260911")
    assert out == [{"date": "2026-09-10", "close": 8.1}, {"date": "2026-09-11", "close": 8.11}]


def test_akshare_fhps_bonus_div10(monkeypatch):
    """fhps：'现金分红比例'(每10股) /10 → dps_per_share；NaT 除权日 → ex_date=None。"""
    df = _FakeDF([
        {"现金分红-现金分红比例": 1.689, "除权除息日": "2026-05-13",
         "方案进度": "实施分配", "报告期": "2025-12-31"},
        {"现金分红-现金分红比例": 1.511, "除权除息日": None,
         "方案进度": "董事会决议通过", "报告期": "2026-06-30"},
    ], ["现金分红-现金分红比例", "除权除息日", "方案进度", "报告期"])
    _install_fake_akshare(monkeypatch, fhps_df=df)
    from screener.data.akshare_src import AkshareValidationClient
    c = AkshareValidationClient({"akshare_interval_s": 0, "akshare_throttle_enabled": False,
                                 "akshare_retry_sleep_s": 0})
    out = c.fhps_detail("601398")
    assert out[0]["dps_per_share"] == pytest.approx(0.1689)
    assert out[0]["ex_date"] == "2026-05-13"
    assert out[1]["ex_date"] is None  # 未实施（NaT）→ PIT 过滤用


def test_akshare_breaker_degrades_none(monkeypatch):
    """连续失败 >=breaker → 熔断；熔断后调用**降级 None**（不抛、不阻塞主路径）。"""
    _install_fake_akshare(monkeypatch, raise_on="daily")
    from screener.data.akshare_src import AkshareValidationClient
    c = AkshareValidationClient({"akshare_interval_s": 0, "akshare_max_attempts": 1,
                                 "akshare_breaker": 3, "akshare_throttle_enabled": False,
                                 "akshare_retry_sleep_s": 0})
    for _ in range(3):
        assert c.daily_closes("601398", "20260901", "20260911") is None
    # 已熔断 → 直接 None（零请求）
    before = c.request_count
    assert c.daily_closes("601398", "20260901", "20260911") is None
    assert c.request_count == before


def test_akshare_fhps_contract_drift_degrades(monkeypatch):
    """列契约漂移（akshare 非官方接口风险 §7.1）→ 降级 None + 不重试死磕。"""
    df = _FakeDF([{"某新列": 1}], ["某新列"])  # 缺 "现金分红-现金分红比例"
    _install_fake_akshare(monkeypatch, fhps_df=df)
    from screener.data.akshare_src import AkshareValidationClient
    c = AkshareValidationClient({"akshare_interval_s": 0, "akshare_throttle_enabled": False,
                                 "akshare_retry_sleep_s": 0})
    assert c.fhps_detail("601398") is None


# ===========================================================================
# sina.parse_cf_report v5.2 附加字段（ROEWEIGHTED 捕获，零回归验证）
# ===========================================================================
def test_parse_cf_report_captures_roe_weighted():
    """CF JSON 解析新增 roe_weighted_pct（百分数,加权）；OCF 首匹配语义不变。"""
    from screener.data import sina as sinamod
    payload = {"result": {"data": {"report_list": {
        "20251231": {
            "publish_date": "2026-03-28",
            "data": [
                {"item_field": "MANANETR", "item_value": "1890530000000.0"},
                {"item_field": "ROEWEIGHTED", "item_value": "9.45"},
            ],
        },
    }}}}
    reports = sinamod.parse_cf_report(payload)
    assert len(reports) == 1
    r = reports[0]
    assert abs(r["ocf"] - 1890530000000.0) < 1.0
    assert r["roe_weighted_pct"] == pytest.approx(9.45)
    # latest_annual_ocf 语义不变（PIT + 年报定位）
    ann = sinamod.latest_annual_ocf(reports, "2026-09-10")
    assert ann is not None and ann["report_date"] == "2025-12-31"


# ===========================================================================
# report：数据源健康度固定段（双通道之 report 侧）
# ===========================================================================
def test_report_zscore_has_health_section_disabled(tmp_path):
    """canonical.enabled=false（data_health=None）→ 健康度段整段消失（回滚=报告逐字节不变）。"""
    import pandas as pd
    from screener.report import write_report
    from screener.screener import ScreenResult

    r = ScreenResult(run_day="2026-09-11", requested_date="2026-09-11", mode="zscore", top_n=50)
    r.candidates = pd.DataFrame()
    r.funnel = {"L4_TopN入选": 0}
    cfg = {
        "technical": {"ma_period": 200, "return_window_days": 250, "min_return_pct": 0,
                      "max_return_pct": 100, "max_annual_volatility_pct": 45},
        "dividend": {"window_days": 365, "min_yield_pct": 3},
        "industry": {"top_pct": 30, "min_group_size": 5},
        "universe": {"listing_min_trading_days": 250},
        "fundamental": {"net_profit_yoy_field": "YOYPNI"},
        "scoring": {"mode": "zscore", "top_n": 50, "missing_policy": "neutral_renorm",
                    "weights": {"technical": 0.25, "dividend": 0.30, "industry": 0.15,
                                "fundamental": 0.30}},
        "badges": {"industry_top_pct": 10, "fscore_min": 7},
        "hard_filter": {"st_enabled": True, "listing_min_trading_days": 250},
    }
    p = tmp_path / "report.md"
    write_report(r, cfg, str(p))
    text = p.read_text(encoding="utf-8")
    # 回滚语义：无 data_health → 整段不渲染（与 v5.1 报告逐字节一致）
    assert "## 数据源健康度" not in text


def test_report_zscore_has_health_section_anomaly(tmp_path):
    """data_health 有异常 → 报告段渲染明细（状态/源表/gap）。"""
    import pandas as pd
    from screener.report import write_report
    from screener.screener import ScreenResult

    r = ScreenResult(run_day="2026-09-11", requested_date="2026-09-11", mode="zscore", top_n=50)
    r.candidates = pd.DataFrame()
    r.funnel = {"L4_TopN入选": 0}
    t = HealthTracker(run_day="2026-09-11")
    t.note_call("akshare_em", False)
    t.add_gap({"source": "akshare_em", "endpoint": "fhps_601398", "rows": 0,
               "status": "suspected_gap"})
    r.data_health = t.to_badge_payload()
    cfg = {
        "technical": {"ma_period": 200, "return_window_days": 250, "min_return_pct": 0,
                      "max_return_pct": 100, "max_annual_volatility_pct": 45},
        "dividend": {"window_days": 365, "min_yield_pct": 3},
        "industry": {"top_pct": 30, "min_group_size": 5},
        "universe": {"listing_min_trading_days": 250},
        "fundamental": {"net_profit_yoy_field": "YOYPNI"},
        "scoring": {"mode": "zscore", "top_n": 50, "missing_policy": "neutral_renorm",
                    "weights": {"technical": 0.25, "dividend": 0.30, "industry": 0.15,
                                "fundamental": 0.30}},
        "badges": {"industry_top_pct": 10, "fscore_min": 7},
        "hard_filter": {"st_enabled": True, "listing_min_trading_days": 250},
    }
    p = tmp_path / "report.md"
    write_report(r, cfg, str(p))
    text = p.read_text(encoding="utf-8")
    assert "## 数据源健康度" in text
    assert "**状态：⚠️ 异常**" in text
    assert "akshare_em/fhps_601398 rows=0 status=suspected_gap" in text


# ===========================================================================
# _v52_crosscheck 集成分支（close/dps/roe 全链路，mock akshare——补 Round-2 blackhole
# 下未执行的 close 分支）
# ===========================================================================
def test_v52_crosscheck_close_dps_roe_full_branch(monkeypatch, tmp_path):
    """_v52_crosscheck：close(腾讯 vs 新浪)/dps(em vs akshare-em)/roe(BaoStock vs 新浪)
    三分支同跑——mock akshare 客户端返回 evidence 口径数据，断言判定与 canonical 写入。"""
    from types import SimpleNamespace
    from datetime import date
    from screener.screener import _v52_crosscheck, ScreenResult
    from screener import config as cfgmod

    repo = "/home/ubuntu/stock-screener"
    cfg = cfgmod.load_config(f"{repo}/config/strategy.yaml")
    # em 静态底表（tmp：哨兵+表头+601398 一行，与生产缓存同格式）
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / "em_dividend_all.csv").write_text(
        "stock-screener-em-cache-v1\r\n"
        "code,report_date,plan_notice_date,ex_date,dps_pretax,progress\r\n"
        "601398,2025-12-31,2026-04-30,2026-05-13,0.168900,实施分配\r\n", encoding="utf-8")
    cfg["data"]["cache_dir"] = str(cache_dir)

    # mock akshare 客户端（evidence 口径：daily close=8.11；fhps /10 后=0.1689）
    class _FakeAk:
        request_count = 0
        def __init__(self, cfg):
            pass
        def daily_closes(self, c6, s, e):
            return [{"date": "2026-09-11", "close": 8.11}]
        def fhps_detail(self, c6):
            return [{"ex_date": "2026-05-13", "dps_per_share": 0.1689,
                     "progress": "实施分配", "report_date": "2025-12-31"}]

    from screener.data import akshare_src
    monkeypatch.setattr(akshare_src, "AkshareValidationClient", _FakeAk)
    # raw/canonical 关闭（本测只验校验判定；落盘路径另有单测）
    from screener.data import rawstore as rs, canonical as cs
    rs.set_enabled(False); cs.set_enabled(False)

    bar = SimpleNamespace(code="sh.601398", close=8.11, preclose=8.10, is_st=False,
                          tradestatus=1, ts="20260911150000", name="工商银行", pct_chg=0.12)
    fetcher = SimpleNamespace(
        _snapshot={"sh.601398": bar}, _candidates={},
        adjfactor_history=lambda c: None,
        profit_data=lambda c, y, q: {"roeAvg": 0.089739},
    )
    # 新浪 ROEWEIGHTED：raw CF 信封（sina 源）——启用 rawstore 到 tmp 以验证 roe 分支
    rs.reset_raw_store()
    monkeypatch.setattr(rs, "_enabled_from_cfg", lambda: True)
    monkeypatch.setattr(rs, "_resolve_root", lambda: str(tmp_path / "raw"))
    cf_payload = {"result": {"data": {"report_list": {
        "20251231": {"publish_date": "2026-03-28", "data": [
            {"item_field": "MANANETR", "item_value": "1890530000000.0"},
            {"item_field": "ROEWEIGHTED", "item_value": "9.45"}]}},}}}
    rs.record_response("sina", "cf_601398_p1", cf_payload, date_s="2026-09-11")

    result = ScreenResult(run_day="2026-09-11", requested_date="2026-09-11", mode="zscore", top_n=50)
    result.funnel["L4_TopN入选"] = 1
    scored = [SimpleNamespace(code="sh.601398", top_n_selected=True)]
    tracker = HealthTracker(run_day="2026-09-11")

    _v52_crosscheck(cfg, fetcher, None, result, scored, 2025, date(2026, 9, 11), tracker)

    # close：腾讯 8.11 vs 新浪 8.11 → ok
    assert len(tracker.checks["close"]) == 1 and tracker.checks["close"][0]["ok"] is True
    # dps：em 0.1689 vs akshare 0.1689 → ok（同源一致性）
    assert len(tracker.checks["dps"]) == 1 and tracker.checks["dps"][0]["level"] == "ok"
    # roe：BaoStock 8.9739% vs 新浪加权 9.45% → Δ=0.48pp ≤1pp → ok（口径差非错误）
    assert len(tracker.checks["roe"]) == 1 and tracker.checks["roe"][0]["ok"] is True
    assert abs(tracker.checks["roe"][0]["diff_pp"] - 0.4761) < 0.001
    # 无冲突 → 非异常日
    s = tracker.summary()
    assert s["conflicts"] == {"close": 0, "dps": 0, "roe": 0}
    rs.reset_raw_store()  # monkeypatch 自动还原 _enabled_from_cfg/_resolve_root


# ===========================================================================
# evidence fixture 交叉验证（离线留样 → 校验函数口径自洽）
# ===========================================================================
def test_evidence_close_crosscheck_zero_deviation():
    """evidence/crosscheck_sina_daily_vs_af3_601398.json：留样 15 日中 14 日零偏差（末行=当日未收盘）。"""
    d = json.load(open(os.path.join(V52, "crosscheck_sina_daily_vs_af3_601398.json"), encoding="utf-8"))
    rows = d["rows"]
    assert len(rows) >= 14
    zero = [r for r in rows if r["diff_pct"] == 0.0]
    # 除"当日未收盘"（腾讯盘中价 vs akshare 收盘价）外零偏差 → 阈值 0.1% 有充足余量
    assert len(zero) >= len(rows) - 1
    # 用留样值跑 check_close：零偏差日必须 ok
    r = xc.check_close("sh.601398", zero[0]["akshare_sina"], float(zero[0]["local_af3"]), tol_pct=0.1)
    assert r["ok"] is True


def test_evidence_dps_em_vs_fhps_consistency():
    """evidence/dps_crosscheck_multi.json：em 静态 vs akshare-em 逐股 mismatch=0（同源一致性）。"""
    d = json.load(open(os.path.join(V52, "dps_crosscheck_multi.json"), encoding="utf-8"))
    total_common = 0
    for code, cmp_ in d.items():
        assert cmp_["mismatch"] == 0, f"{code} mismatch={cmp_['mismatch']}（同源应零偏差）"
        total_common += cmp_["common"]
    assert total_common >= 40  # 留样覆盖多股多年分红事件

    # fhps fixture：每10股口径 /10 后与 em 底表对齐（601398 2026-05-13: 1.689/10=0.1689）
    fhps = json.load(open(os.path.join(V52, "akshare_fhps_detail_601398.json"), encoding="utf-8"))
    row = [r for r in fhps["rows"] if str(r.get("除权除息日", "")) == "2026-05-13"][0]
    dps_per_share = xc.fhps_bonus_to_dps(row["现金分红-现金分红比例"])
    assert dps_per_share == pytest.approx(0.1689)
    chk = xc.check_dps("601398", 0.1689, dps_per_share, warn_at=0.01, stop_at=0.05)
    assert chk["level"] == "ok"


def test_evidence_roe_baseline_within_1pp():
    """evidence/sina_cf_full_fields_and_cross.json：3 股 ROE 口径差均 <1pp（阈值校准依据）。"""
    d = json.load(open(os.path.join(V52, "sina_cf_full_fields_and_cross.json"), encoding="utf-8"))
    for item in d["roe_cross_2025_annual"]:
        r = xc.check_roe(item["code"], float(item["bs_roeAvg"]),
                         float(item["sina_ROEWEIGHTED"]), tol_pp=1.0)
        assert r["ok"] is True, f"{item['code']} Δ={r['diff_pp']}pp 超 1pp（阈值需重校）"


# ===========================================================================
# 回滚路径：canonical.enabled=false → 主路径行为逐字节不变（brief 硬约束）
# ===========================================================================
def test_rollback_canonical_disabled_no_raw_or_canonical_writes(monkeypatch, tmp_path):
    """enabled=false：raw/canonical 单例均 None；钩子 no-op；不创建 data/ 目录。"""
    monkeypatch.setattr(rawstore, "_enabled_from_cfg", lambda: False)
    monkeypatch.setattr(canonical, "canonical_store", lambda force_init=False: None)
    rawstore.reset_raw_store()
    try:
        assert rawstore.raw_store(force_init=True) is None
        assert canonical.canonical_store(force_init=True) is None
        assert rawstore.record_response("tencent", "snapshot", [{"x": 1}], date_s="2026-09-11") is None
        # 主路径关键行为不变：DiskCache 读写语义零改动（哨兵/原子写照旧）
        from screener.data.cache import DiskCache
        dc = DiskCache(str(tmp_path / "cache"))
        dc.put("kline_af3_sh.601398", ["date", "close"], [["2026-09-11", "8.11"]])
        hit = dc.get("kline_af3_sh.601398")
        assert hit is not None and hit["rows"] == [["2026-09-11", "8.11"]]
    finally:
        rawstore.reset_raw_store()


def test_rollback_delete_dirs_returns_to_v51_state(tmp_path):
    """删 data/raw + data/canonical 目录 → 系统回到 v5.1 状态（无残留依赖）。"""
    root = tmp_path / "data"
    s = RawStore(str(root / "raw"))
    s.record_json("tencent", "snapshot", [{"x": 1}], date_s="2026-09-11")
    c = CanonicalStore(str(root / "canonical"))
    c.append("close", "sh.601398", "2026-09-11", 8.11, source="tencent")
    assert s.stats()["files"] == 1 and c.stats()["close"] == 1
    # 回滚：整删目录
    import shutil
    shutil.rmtree(root)
    # 删除后：canonical 读空、raw 列空（不崩溃）——v5.1 主路径从不依赖这些目录
    assert c.read("close") == []
    assert s.list_files() == []
