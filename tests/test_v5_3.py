# -*- coding: utf-8 -*-
"""v5.3 单测：健康度阈值告警（D1）+ canonical version_log.csv（D3）。

纪律（brief D4/D6）：
- **零网络**：rf/f10 用 mock meta/None 注入，全部离线。
- alerts 三 kind 各有"触发/不触发"两用例；report 顶部块渲染与无告警时不渲染；
  version_log 追加+幂等+原子写（tmp_path）；config 新键校验+缺省兼容。
- 回滚硬约束：canonical.enabled=false → tracker=None / CanonicalStore=None →
  告警逻辑与 version_log 全部不生效（沿用 v5.2 A/B/C 回滚测试模式）。
"""
from __future__ import annotations

import csv
import json
import os

import pytest

from screener.config import ConfigError, health_cfg
from screener.data.canonical import (
    CanonicalStore, VERSION_LOG_COLS, VERSION_LOG_FILE, VERSION_LOG_SENTINEL,
)
from screener.health import HealthTracker, render_alerts_block

ALERT_CFG = {"rf_fallback_max_per_run": 0, "f10_degraded_max": 5,
             "crosscheck_conflicts_max": 2}


def _tracker(**kw):
    return HealthTracker(run_day="2026-09-13", alert_cfg=dict(ALERT_CFG, **kw))


# ===========================================================================
# D1-a：alerts 阈值判定——三 kind × 触发/不触发（纯函数，构造注入）
# ===========================================================================
def test_alerts_rf_fallback_trigger_and_not():
    """rf_fallback：默认阈值 0 → 一回退即告警；未回退 → 不告警。"""
    t = _tracker()
    assert [a["kind"] for a in t.alerts] == []           # 未回退 → 不触发
    t.note_rf_fallback("TE 解析失败")
    k = [a["kind"] for a in t.alerts]
    assert k == ["rf_fallback"] and "1 次" in t.alerts[0]["detail"]

    # 阈值放宽到 1 → 回退 1 次不告警（> N 才告警，严格大于）
    t2 = _tracker(rf_fallback_max_per_run=1)
    t2.note_rf_fallback()
    assert t2.alerts == []


def test_alerts_f10_degraded_trigger_and_not():
    """f10_degraded：降级数 > 5 → 告警；<=5 → 不告警。明细含 code/category。"""
    t = _tracker()
    for i in range(5):
        t.note_f10_degraded(f"sh.60{i:04d}", "holders")
    assert [a["kind"] for a in t.alerts] == []           # 恰好=阈值 → 不触发（严格大于）
    t.note_f10_degraded("sz.000001", "cf")               # 第 6 只 → 触发
    k = [a["kind"] for a in t.alerts]
    assert k == ["f10_degraded"]
    d = t.alerts[0]["detail"]
    assert "6 只" in d and "holders×5" in d and "cf×1" in d

    # 自定义阈值：>3 → 4 只即告警
    t2 = _tracker(f10_degraded_max=3)
    for i in range(4):
        t2.note_f10_degraded(f"sh.60{i:04d}", "holders")
    assert [a["kind"] for a in t2.alerts] == ["f10_degraded"]


def test_alerts_crosscheck_conflicts_trigger_and_not():
    """crosscheck_conflicts：Σ per-field 冲突 > 阈值 → 告警；<= 阈值 → 不告警。"""
    t = _tracker()
    t.add_check("close", {"ok": False})                  # 1 冲突
    t.add_check("dps", {"level": "warn"})                # 2 冲突
    assert [a["kind"] for a in t.alerts] == []           # 恰好=阈值 2 → 不触发
    t.add_check("roe", {"ok": False})                    # 3 冲突 → 触发
    k = [a["kind"] for a in t.alerts]
    assert k == ["crosscheck_conflicts"]
    d = t.alerts[0]["detail"]
    assert "总数 3" in d and "close×1" in d and "dps×1" in d and "roe×1" in d

    # ok=True / level=ok 不计冲突
    t2 = _tracker()
    for _ in range(5):
        t2.add_check("close", {"ok": True})
    assert t2.alerts == []


def test_alerts_all_three_kinds_together():
    """三类同时超阈值 → alerts 含三 kind（顺序 rf/f10/crosscheck）。"""
    t = _tracker()
    t.note_rf_fallback()
    for i in range(7):
        t.note_f10_degraded(f"sh.60{i:04d}", "holders")
    for _ in range(3):
        t.add_check("close", {"ok": False})
    assert [a["kind"] for a in t.alerts] == ["rf_fallback", "f10_degraded",
                                             "crosscheck_conflicts"]


def test_alerts_disabled_when_no_cfg_backward_compat():
    """alert_cfg=None（v5.2 旧调用方）→ alerts 恒空、has_anomaly 不受影响。"""
    t = HealthTracker(run_day="2026-09-13")              # 无 alert_cfg
    t.note_rf_fallback()
    for i in range(10):
        t.note_f10_degraded(f"sh.60{i:04d}", "holders")
    assert t.alerts == [] and "alerts" in t.summary() and t.summary()["alerts"] == []
    # 正常异常判据仍工作（源失败 → has_anomaly）
    t.note_call("tencent", False)
    assert t.has_anomaly is True


def test_alert_cfg_partial_keys_fall_back_to_defaults():
    """alert_cfg 只传部分键 → 缺键按默认阈值兜底（yaml 单一事实来源仍是 strategy.yaml）。"""
    t = HealthTracker(run_day="2026-09-13", alert_cfg={"f10_degraded_max": 99})
    assert t.alert_cfg == {"rf_fallback_max_per_run": 0, "f10_degraded_max": 99,
                           "crosscheck_conflicts_max": 2}
    for i in range(6):
        t.note_f10_degraded(f"sh.60{i:04d}", "holders")   # 6 <= 99 → 不触发
    assert [a["kind"] for a in t.alerts] == []


# ===========================================================================
# D1-b：summary / badge 含 alerts；has_anomaly 任一 alert → True
# ===========================================================================
def test_summary_and_badge_contain_alerts():
    t = _tracker()
    t.note_rf_fallback("TE 解析失败 → config fallback=2.0%")
    s = t.summary()
    assert s["alerts"] and s["alerts"][0]["kind"] == "rf_fallback"
    p = t.to_badge_payload()
    assert p["has_anomaly"] is True
    assert any(a.startswith("[rf_fallback]") for a in p["anomalies"])


def test_alert_only_makes_has_anomaly_true():
    """无源失败/冲突/gap，仅 alert 超阈值 → has_anomaly=True（badge 显示）。"""
    t = _tracker()
    t.note_rf_fallback()
    assert t.has_anomaly is True
    p = t.to_badge_payload()
    assert p["has_anomaly"] is True and len(p["anomalies"]) == 1


def test_normal_day_no_alerts_zero_noise():
    """正常日（alert_cfg 注入但未超阈值）→ alerts 空、has_anomaly=False、anomalies=[]。"""
    t = _tracker()
    t.note_call("tencent", True)
    t.add_check("close", {"ok": True})
    assert t.alerts == [] and t.has_anomaly is False
    assert t.to_badge_payload()["anomalies"] == []


# ===========================================================================
# D1-c：render_alerts_block + report 顶部块（zscore/legacy 双模式）
# ===========================================================================
def test_render_alerts_block_present_and_absent():
    """有 alerts → 标题+逐行；无 alerts → []（零噪音，整块不渲染）。"""
    assert render_alerts_block([]) == []
    lines = render_alerts_block([{"kind": "rf_fallback", "detail": "x"}])
    assert lines[0] == "## ⚠️ 数据源告警（1 项）"
    assert "- **rf_fallback**: x" in lines


def _report_cfg():
    return {
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


def _zscore_result():
    import pandas as pd
    from screener.screener import ScreenResult
    r = ScreenResult(run_day="2026-09-13", requested_date="2026-09-13", mode="zscore", top_n=50)
    r.candidates = pd.DataFrame()
    r.funnel = {"L4_TopN入选": 0}
    return r


def test_report_zscore_alerts_block_at_top(tmp_path):
    """有 alerts → 顶部块位于标题行之后、'一、KPI概览'之前；固定段照常渲染。"""
    import pandas as pd
    from screener.report import write_report
    r = _zscore_result()
    t = _tracker()
    t.note_rf_fallback("TE 解析失败 → config fallback=2.0%")
    for i in range(6):
        t.note_f10_degraded(f"sh.60{i:04d}", "holders")
    r.data_health = t.to_badge_payload()
    p = tmp_path / "report.md"
    write_report(r, _report_cfg(), str(p))
    text = p.read_text(encoding="utf-8")
    assert "## ⚠️ 数据源告警（2 项）" in text
    assert "- **rf_fallback**: " in text and "- **f10_degraded**: " in text
    # 位置契约：标题行之后、'一、KPI概览'之前
    i_title = text.index("# A股选股报告")
    i_alerts = text.index("## ⚠️ 数据源告警")
    i_kpi = text.index("## 一、KPI 概览")
    assert i_title < i_alerts < i_kpi
    # 固定段保持不动（全量明细仍在）
    assert "## 数据源健康度" in text


def test_report_zscore_no_alerts_block_absent(tmp_path):
    """data_health 存在但 alerts=[] → 顶部块整块不渲染（零噪音）。"""
    from screener.report import write_report
    r = _zscore_result()
    t = _tracker()
    t.note_call("tencent", True)
    r.data_health = t.to_badge_payload()
    p = tmp_path / "report.md"
    write_report(r, _report_cfg(), str(p))
    text = p.read_text(encoding="utf-8")
    assert "## ⚠️ 数据源告警" not in text
    assert "## 数据源健康度" in text          # 固定段照常（空态也显示）


def test_report_rollback_no_data_health_no_block(tmp_path):
    """canonical.enabled=false（data_health=None）→ 顶部块+固定段全消失（回滚逐字节不变）。"""
    from screener.report import write_report
    r = _zscore_result()
    p = tmp_path / "report.md"
    write_report(r, _report_cfg(), str(p))
    text = p.read_text(encoding="utf-8")
    assert "## ⚠️ 数据源告警" not in text
    assert "## 数据源健康度" not in text


# ===========================================================================
# D1-d：config health.alerts 解析 + 校验 + 缺省兼容
# ===========================================================================
def test_health_cfg_alerts_defaults_backward_compat():
    """旧 yaml（无 alerts 子段）→ 默认阈值 {0,5,2}，可正常加载。"""
    h = health_cfg({"health": {"enabled": True}})
    assert h["alerts"] == {"rf_fallback_max_per_run": 0, "f10_degraded_max": 5,
                           "crosscheck_conflicts_max": 2}
    # 无 health 段整体 → 同样默认
    assert health_cfg({})["alerts"]["f10_degraded_max"] == 5


def test_health_cfg_alerts_explicit_values():
    h = health_cfg({"health": {"alerts": {"rf_fallback_max_per_run": 2,
                                          "f10_degraded_max": 10,
                                          "crosscheck_conflicts_max": 4}}})
    assert h["alerts"] == {"rf_fallback_max_per_run": 2, "f10_degraded_max": 10,
                           "crosscheck_conflicts_max": 4}


def test_health_cfg_alerts_partial_keys():
    """只写一个键 → 其余按默认补齐。"""
    h = health_cfg({"health": {"alerts": {"f10_degraded_max": 9}}})
    assert h["alerts"] == {"rf_fallback_max_per_run": 0, "f10_degraded_max": 9,
                           "crosscheck_conflicts_max": 2}


@pytest.mark.parametrize("bad", [-1, "two", None])
def test_health_cfg_alerts_invalid_raises(bad):
    """负数/非整数 → ConfigError（零硬编码纪律：非法值不得静默吞掉）。"""
    with pytest.raises(ConfigError):
        health_cfg({"health": {"alerts": {"f10_degraded_max": bad}}})


# ===========================================================================
# D3：canonical version_log.csv（追加 + 幂等 + 原子写 + build_from_raw）
# ===========================================================================
def test_version_log_append_structure_and_seq(tmp_path):
    """append 落盘后 version_log 追加一行；列齐全、seq 单调递增。"""
    s = CanonicalStore(str(tmp_path), data_version="v5.3-test")
    assert s.read_version_log() == []                    # 未写 → 空（文件不存在）
    assert s.log_version("append", "tencent", 1, "2026-09-11", "2026-09-11",
                         "field=close code=sh.601398") is not None
    assert s.log_version("append", "sina", 2, "2026-09-11", "2026-09-12", "x") is not None
    rows = s.read_version_log()
    assert len(rows) == 2
    assert list(rows[0].keys()) == list(VERSION_LOG_COLS)
    assert [r["seq"] for r in rows] == ["1", "2"]
    assert rows[0]["trigger"] == "append" and rows[0]["source"] == "tencent"
    assert rows[0]["data_version"] == "v5.3-test" and rows[0]["rows_added"] == "1"
    assert rows[0]["raw_date_min"] == "2026-09-11" and rows[0]["raw_date_max"] == "2026-09-11"


def test_version_log_idempotent_same_batch(monkeypatch, tmp_path):
    """同 (ts, trigger, source, note) 重复写 → 不产生重复行（幂等）。"""
    from screener.data import canonical as canon
    monkeypatch.setattr(canon, "now_iso", lambda: "2026-09-13T08:00:00Z")
    s = CanonicalStore(str(tmp_path))
    s.log_version("append", "tencent", 1, "d", "d", "field=close code=sh.601398")
    assert s.log_version("append", "tencent", 1, "d", "d",
                         "field=close code=sh.601398") is None     # 幂等跳过
    rows = s.read_version_log()
    assert len(rows) == 1
    # 同秒但**不同**记录（note 含 field+code）→ 各记一行
    s.log_version("append", "tencent", 1, "d", "d", "field=dps code=sh.601398")
    assert len(s.read_version_log()) == 2


def test_version_log_atomic_no_partial_files(tmp_path):
    """原子写（tmp+rename）：正常写后无 .tmp.* 残留。"""
    s = CanonicalStore(str(tmp_path))
    s.log_version("append", "tencent", 1, "d", "d", "n")
    leftovers = [f for f in os.listdir(str(tmp_path)) if ".tmp." in f]
    assert leftovers == []
    # 文件头=哨兵+表头（与 canonical 同风格）
    with open(s._version_log_path(), encoding="utf-8") as f:
        first = next(csv.reader(f))
    assert first == [VERSION_LOG_SENTINEL]


def test_version_log_append_hooked_into_store_append(tmp_path):
    """CanonicalStore.append 每次批量落盘后自动追加日志行（rows_added=1）。"""
    s = CanonicalStore(str(tmp_path))
    assert s.append("close", "sh.601398", "2026-09-11", 8.11, source="tencent") is True
    rows = s.read_version_log()
    assert len(rows) == 1 and rows[0]["trigger"] == "append"
    assert rows[0]["source"] == "tencent" and rows[0]["rows_added"] == "1"
    assert "field=close code=sh.601398" in rows[0]["note"]
    # 幂等跳过（同键再 append）→ 不追加日志行
    assert s.append("close", "sh.601398", "2026-09-11", 8.11, source="tencent") is False
    assert len(s.read_version_log()) == 1


def test_version_log_build_from_raw_lines(tmp_path):
    """build_from_raw 完成后按 source 各追加一行（rows_added=本源新写条数+日期范围）。"""
    raw_root = tmp_path / "raw"
    os.makedirs(raw_root / "tencent" / "2026-09-11")
    os.makedirs(raw_root / "akshare_em" / "2026-09-10")
    with open(raw_root / "tencent/2026-09-11/snapshot.0.json", "w", encoding="utf-8") as f:
        json.dump({"source": "tencent", "endpoint": "snapshot",
                   "fetched_at": "2026-09-11T15:00:00Z", "status": "ok", "rows": 1,
                   "data": [{"code": "sh.601398", "close": 8.11, "ts": "20260911150000"}]}, f)
    with open(raw_root / "akshare_em/2026-09-10/fhps_601398.0.json", "w", encoding="utf-8") as f:
        json.dump({"source": "akshare_em", "endpoint": "fhps_601398",
                   "fetched_at": "2026-09-10T15:00:00Z", "status": "ok", "rows": 1,
                   "data": [{"code": "601398", "ex_date": "2026-05-13",
                             "dps_per_share": 0.1689}]}, f)
    s = CanonicalStore(str(tmp_path / "canonical"))
    counts = s.build_from_raw(str(raw_root), {"tencent": "close", "akshare_em": "dps"})
    assert counts == {"close": 1, "dps": 1, "roe": 0}
    rows = s.read_version_log()
    by_trigger = {}
    for r in rows:
        by_trigger.setdefault(r["trigger"], []).append(r)
    # build_from_raw 汇总行：每 source 一行，rows_added=本源新写条数、日期范围=信封日期
    assert len(by_trigger.get("build_from_raw", [])) == 2
    t_row = [r for r in by_trigger["build_from_raw"] if r["source"] == "tencent"][0]
    assert t_row["rows_added"] == "1" and t_row["raw_date_min"] == "2026-09-11" \
        and t_row["raw_date_max"] == "2026-09-11"
    e_row = [r for r in by_trigger["build_from_raw"] if r["source"] == "akshare_em"][0]
    assert e_row["rows_added"] == "1" and e_row["raw_date_min"] == "2026-09-10"
    # append 明细行也在（brief：append* 每次落盘后追加一行）
    assert len(by_trigger.get("append", [])) == 2


def test_version_log_build_from_raw_replay_no_summary_dup(monkeypatch, tmp_path):
    """幂等重放 build_from_raw（canonical 全已存在 → 零新写）→ 不产生重复汇总行。"""
    from screener.data import canonical as canon
    monkeypatch.setattr(canon, "now_iso", lambda: "2026-09-13T08:00:00Z")
    raw_root = tmp_path / "raw"
    os.makedirs(raw_root / "tencent" / "2026-09-11")
    with open(raw_root / "tencent/2026-09-11/snapshot.0.json", "w", encoding="utf-8") as f:
        json.dump({"source": "tencent", "endpoint": "snapshot", "status": "ok", "rows": 1,
                   "data": [{"code": "sh.601398", "close": 8.11, "ts": "20260911150000"}]}, f)
    s = CanonicalStore(str(tmp_path / "canonical"))
    s.build_from_raw(str(raw_root), {"tencent": "close"})
    n_after_first = len([r for r in s.read_version_log() if r["trigger"] == "build_from_raw"])
    assert n_after_first == 1
    # 重放：canonical 已含同键 → 零新写 → 汇总行不再追加（零写入源不记行）
    s.build_from_raw(str(raw_root), {"tencent": "close"})
    n_after_replay = len([r for r in s.read_version_log() if r["trigger"] == "build_from_raw"])
    assert n_after_replay == n_after_first


def test_version_log_bad_trigger_raises(tmp_path):
    with pytest.raises(ValueError):
        CanonicalStore(str(tmp_path)).log_version("bogus", "tencent", 1)


def test_canonical_disabled_zero_version_log_writes(monkeypatch, tmp_path):
    """canonical.enabled=false → CanonicalStore 根本不实例化 → version_log 零写入。"""
    from screener.data import canonical as canon
    # 强制关闭：单例 None，root 下不得出现任何文件（回滚点不变）
    monkeypatch.setattr(canon, "_FORCED_ENABLED", False)
    try:
        assert canon.canonical_store(force_init=True) is None
        files = os.listdir(str(tmp_path)) if os.path.isdir(str(tmp_path)) else []
        assert VERSION_LOG_FILE not in files
    finally:
        monkeypatch.setattr(canon, "_FORCED_ENABLED", None)
