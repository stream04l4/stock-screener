# -*- coding: utf-8 -*-
"""v5.2 Phase 1：数据源健康度汇总（报告 §4/§6 Q5 双通道之 report 侧）。
v5.3 D1 扩展：健康度**阈值告警**（alerts）——rf 回退 / 新浪 F10 降级 / 交叉校验冲突
三类计数超阈值 → summary().["alerts"] + 报告顶部"数据源告警"块 + Web badge anomalies。

职责：
- **HealthTracker**：一次运行内的健康度记账——各源调用成功/失败次数、交叉校验冲突数、
  suspected_gap 列表、待复核标的（DPS |Δ|>stop_at）、v5.3 三类告警计数。纯内存 +
  可选 JSON 落盘（``output/data_health_{day}.json``，供 Web badge 读取；写失败不阻塞主路径）。
- **render_report_section**：生成 report.md 固定"数据源健康度"段（markdown 行列表）。
- **render_alerts_block**：v5.3 报告顶部"⚠️ 数据源告警"块（有 alerts 才渲染，零噪音）。
- **to_badge_payload**：Web badge 用结构化摘要——**仅异常时非空**（Q5：badge 只在
  异常时显示；正常日 payload 的 ``anomalies`` 为空 → 前端不渲染）。

零网络、零硬编码阈值：所有判定发生在 crosscheck.py（阈值由调用方从 config health:
段传入）与 HealthTracker.alerts（v5.3 告警阈值由 alert_cfg 构造注入，缺省=不产生
alerts——向后兼容 v5.2），本模块只做汇总/渲染。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger("screener.health")


class HealthTracker:
    """一次筛选运行的数据源健康度记账器。

    v5.3 D1：``alert_cfg`` 构造注入（config.health_cfg(cfg)["alerts"]，全部 yaml 驱动）
    → :meth:`alerts` 按阈值判定生成告警列表。**缺省 None = 不产生 alerts**（向后兼容
    v5.2：旧调用方/离线测试行为不变；纯函数风格——判定只依赖注入值与内存计数）。
    """

    # v5.3 三类告警的默认阈值（alert_cfg 缺键时兜底；单一事实来源仍是 strategy.yaml
    # health.alerts，此处仅为"传入部分键"时的容错，不得在别处硬编码第二套阈值）
    _DEFAULT_ALERTS = {"rf_fallback_max_per_run": 0, "f10_degraded_max": 5,
                       "crosscheck_conflicts_max": 2}

    def __init__(self, run_day: str = "", alert_cfg: Optional[Dict[str, int]] = None) -> None:
        self.run_day = run_day
        # v5.3：alert_cfg=None → 关闭告警判定（向后兼容）；dict（含空 dict）→ 启用，
        # 缺键按 _DEFAULT_ALERTS 兜底。
        self.alert_cfg: Optional[Dict[str, int]] = (
            None if alert_cfg is None else {**self._DEFAULT_ALERTS, **alert_cfg}
        )
        # 各源调用计数：{source: {"ok": n, "fail": n}}
        self.calls: Dict[str, Dict[str, int]] = {}
        # 交叉校验结果（按字段分组）：{field: [check dict, ...]}
        self.checks: Dict[str, List[Dict[str, Any]]] = {"close": [], "dps": [], "roe": []}
        # EmptyPayloadGuard 命中：[{source, endpoint, rows, status}]
        self.gaps: List[Dict[str, Any]] = []
        # 待复核标的（DPS review 级）：{code: reason}
        self.review: Dict[str, str] = {}
        # 自由注记（降级/熔断等事件）
        self.notes: List[str] = []
        # ---- v5.3 D1 告警计数 ----
        self.rf_fallback_count: int = 0                 # 本次运行 rf 回退次数（每次运行 rf 只取一次 → 0/1）
        self.f10_degraded: List[Dict[str, str]] = []    # [{code, category}] 接口失败/熔断路径的降级明细

    # ---------- 记账 ----------
    def note_call(self, source: str, ok: bool) -> None:
        d = self.calls.setdefault(source, {"ok": 0, "fail": 0})
        d["ok" if ok else "fail"] += 1

    def add_check(self, field: str, check: Dict[str, Any]) -> None:
        self.checks.setdefault(field, []).append(check)

    def add_gap(self, gap: Dict[str, Any]) -> None:
        self.gaps.append(gap)

    def add_review(self, code: str, reason: str) -> None:
        self.review[code] = reason

    def note(self, msg: str) -> None:
        self.notes.append(msg)

    # ---------- v5.3 D1 告警记账 ----------
    def note_rf_fallback(self, detail: str = "") -> None:
        """rf 10Y 源回退一次（meta["source"]=="fallback"）。计数+注记。"""
        self.rf_fallback_count += 1
        self.notes.append(f"⚠️ v5.3 rf 回退告警记账: {detail or 'TE 解析失败 → config fallback'}")

    def note_f10_degraded(self, code: str, category: str) -> None:
        """新浪 F10 某标的降级一次（接口失败/熔断路径取 None）。计数+明细。

        ⚠️只对**接口失败/熔断路径**记账（引擎侧接线保证）；"该股本就无数据"的正常空
        响应不记——否则阈值告警会被真实无数据的标的稀释/误触发。
        """
        self.f10_degraded.append({"code": code, "category": category})

    # ---------- v5.3 D1 告警判定（纯函数：注入阈值 × 内存计数） ----------
    @property
    def alerts(self) -> List[Dict[str, str]]:
        """按 alert_cfg 阈值判定 → [{"kind", "detail"}]；alert_cfg=None → []（兼容）。

        kind ∈ {rf_fallback, f10_degraded, crosscheck_conflicts}，严格 > 阈值才告警。
        """
        if self.alert_cfg is None:
            return []
        out: List[Dict[str, str]] = []
        n_rf = self.rf_fallback_count
        if n_rf > self.alert_cfg["rf_fallback_max_per_run"]:
            out.append({"kind": "rf_fallback",
                        "detail": f"本次运行 rf 10Y 回退 {n_rf} 次（阈值 "
                                  f"{self.alert_cfg['rf_fallback_max_per_run']}）"})
        n_f10 = len(self.f10_degraded)
        if n_f10 > self.alert_cfg["f10_degraded_max"]:
            cats: Dict[str, int] = {}
            for d in self.f10_degraded:
                cats[d["category"]] = cats.get(d["category"], 0) + 1
            cat_s = "、".join(f"{k}×{v}" for k, v in sorted(cats.items()))
            sample = "、".join(sorted({d["code"] for d in self.f10_degraded})[:5])
            more = f" 等 {n_f10} 只" if n_f10 > 5 else ""
            out.append({"kind": "f10_degraded",
                        "detail": f"当日新浪 F10 降级标的 {n_f10} 只（阈值 "
                                  f"{self.alert_cfg['f10_degraded_max']}；{cat_s}；如 {sample}{more}）"})
        n_conf = sum(self._conflicts(v) for v in self.checks.values())
        if n_conf > self.alert_cfg["crosscheck_conflicts_max"]:
            conf_s = "、".join(f"{f}×{self._conflicts(v)}"
                               for f, v in sorted(self.checks.items())
                               if self._conflicts(v) > 0)
            out.append({"kind": "crosscheck_conflicts",
                        "detail": f"三字段交叉校验冲突总数 {n_conf}（阈值 "
                                  f"{self.alert_cfg['crosscheck_conflicts_max']}；{conf_s}）"})
        return out

    # ---------- 汇总 ----------
    @staticmethod
    def _conflicts(checks: List[Dict[str, Any]]) -> int:
        """冲突数：ok=False 或 level ∈ {warn, review}（ok=None=缺值跳过不计）。"""
        n = 0
        for c in checks:
            if "level" in c:
                if c["level"] in ("warn", "review"):
                    n += 1
            elif c.get("ok") is False:
                n += 1
        return n

    def summary(self) -> Dict[str, Any]:
        """结构化汇总（report 段 / JSON sidecar / Web badge 共用）。"""
        sources = {}
        for s in sorted(self.calls):
            d = self.calls[s]
            total = d["ok"] + d["fail"]
            rate = round(d["ok"] / total * 100.0, 1) if total else None
            sources[s] = {"ok": d["ok"], "fail": d["fail"], "success_rate_pct": rate}
        n_conflicts = {f: self._conflicts(v) for f, v in self.checks.items()}
        return {
            "run_day": self.run_day,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "sources": sources,
            "checks_total": {f: len(v) for f, v in self.checks.items()},
            "conflicts": n_conflicts,
            "suspected_gaps": list(self.gaps),
            "review_codes": sorted(self.review),
            "notes": list(self.notes),
            # v5.3 D1：阈值告警（alert_cfg=None → 空列表；报告顶部块/badge 消费）
            "alerts": self.alerts,
        }

    @property
    def has_anomaly(self) -> bool:
        """Web badge 判据：任一源失败 / 冲突>0 / suspected_gap / 待复核 / v5.3 任一 alert → 异常。"""
        if any(d["fail"] > 0 for d in self.calls.values()):
            return True
        if any(v > 0 for v in (self._conflicts(c) for c in self.checks.values())):
            return True
        if self.gaps or self.review:
            return True
        if self.alerts:
            return True
        return False

    def to_badge_payload(self) -> Dict[str, Any]:
        """Web badge 结构化摘要。**仅异常时 anomalies 非空**（Q5：badge 只异常时显示）。"""
        s = self.summary()
        anomalies: List[str] = []
        for src, d in s["sources"].items():
            if d["fail"] > 0:
                anomalies.append(f"{src} 失败 {d['fail']} 次")
        for f, n in s["conflicts"].items():
            if n > 0:
                anomalies.append(f"{f} 冲突 {n}")
        if s["suspected_gaps"]:
            anomalies.append(f"疑似缺数据 {len(s['suspected_gaps'])} 项")
        if s["review_codes"]:
            anomalies.append(f"待复核 {len(s['review_codes'])} 只")
        # v5.3 D1：alert 文案追加（web/app.py 读同一 payload，无需改前端）
        for a in s["alerts"]:
            anomalies.append(f"[{a['kind']}] {a['detail']}")
        return {"run_day": s["run_day"], "anomalies": anomalies,
                "has_anomaly": self.has_anomaly, "summary": s}

    # ---------- 持久化（Web badge 读取） ----------
    def save(self, output_dir: str, run_day: Optional[str] = None) -> Optional[str]:
        """写 ``data_health_{YYYYMMDD}.json``；失败 → None（不阻塞主路径）。"""
        day = (run_day or self.run_day or "").replace("-", "")
        if not day:
            return None
        path = os.path.join(output_dir, f"data_health_{day}.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.to_badge_payload(), f, ensure_ascii=False)
            return path
        except OSError as exc:
            log.warning("健康度 JSON 落盘失败 %s: %s（不阻塞主路径）", path, exc)
            return None

    @staticmethod
    def load(output_dir: str, run_day: str) -> Optional[Dict[str, Any]]:
        """读回某日健康度 payload；不存在/损坏 → None。"""
        day = str(run_day).replace("-", "")
        path = os.path.join(output_dir, f"data_health_{day}.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None


def save_payload(output_dir: str, payload: Dict[str, Any]) -> Optional[str]:
    """落盘某日健康度 payload（Web badge 读取）；失败 → None（不阻塞主路径）。"""
    day = str(payload.get("run_day") or "").replace("-", "")
    if not day:
        return None
    path = os.path.join(output_dir, f"data_health_{day}.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        return path
    except OSError as exc:
        log.warning("健康度 JSON 落盘失败 %s: %s（不阻塞主路径）", path, exc)
        return None


def load_payload(output_dir: str, run_day: str) -> Optional[Dict[str, Any]]:
    """读回某日健康度 payload（Web badge）；不存在/损坏 → None。"""
    return HealthTracker.load(output_dir, run_day)


# ===========================================================================
# report.md 固定段（双通道之 report 侧）
# ===========================================================================
def _anomaly_from_summary(s: Dict[str, Any]) -> bool:
    if any(d.get("fail", 0) > 0 for d in s["sources"].values()):
        return True
    if any(v > 0 for v in s["conflicts"].values()):
        return True
    if s["suspected_gaps"] or s["review_codes"]:
        return True
    return False


def render_report_section_from_summary(s: Dict[str, Any]) -> List[str]:
    """从 summary dict 渲染"数据源健康度"固定段（report 层只持有 payload，不持有 tracker）。"""
    lines: List[str] = []
    ap = lines.append
    ap("## 数据源健康度")
    ap("")

    # --- 各源成功率 ---
    if s["sources"]:
        ap("| 数据源 | 成功 | 失败 | 成功率% |")
        ap("|---|---:|---:|---:|")
        for src, d in s["sources"].items():
            rate = "—" if d["success_rate_pct"] is None else f"{d['success_rate_pct']:.1f}"
            ap(f"| {src} | {d['ok']} | {d['fail']} | {rate} |")
        ap("")

    # --- 交叉校验 ---
    labels = {"close": "收盘价（腾讯 vs 新浪）", "dps": "DPS（em静态 vs akshare-em）",
              "roe": "ROE（BaoStock vs 新浪加权）"}
    any_check = False
    for f in ("close", "dps", "roe"):
        total, conf = s["checks_total"].get(f, 0), s["conflicts"].get(f, 0)
        if total == 0 and conf == 0:
            continue
        any_check = True
        state = f"冲突 {conf}" if conf else "全部一致"
        ap(f"- {labels[f]}：校验 {total} 项，{state}")
    if not any_check:
        ap("- 交叉校验：本期无可用对比样本（校验源未启用或无重叠标的）")
    ap("")

    # --- suspected_gap ---
    gaps = s["suspected_gaps"]
    if gaps:
        ap(f"### 疑似缺数据（EmptyPayloadGuard，{len(gaps)} 项——不写 canonical、不覆盖静态底表）")
        ap("")
        for g in gaps[:50]:
            ap(f"- {g.get('source')}/{g.get('endpoint')} rows={g.get('rows')} "
               f"status={g.get('status')}")
        if len(gaps) > 50:
            ap(f"- …其余 {len(gaps) - 50} 项略")
        ap("")

    # --- 待复核标的 ---
    if s["review_codes"]:
        ap(f"### 待复核标的（{len(s['review_codes'])} 只——DPS 偏差超停算阈值，本期不参与计算）")
        ap("")
        for c in s["review_codes"][:100]:
            ap(f"- {c}")
        if len(s["review_codes"]) > 100:
            ap("- …其余略")
        ap("")

    # --- 注记（降级/熔断等） ---
    if s["notes"]:
        for n in s["notes"]:
            ap(f"- {n}")
        ap("")

    if _anomaly_from_summary(s):
        lines.insert(1, "**状态：⚠️ 异常**（详见下列明细）")
    else:
        lines.insert(1, "**状态：正常**（无失败调用、无冲突、无疑似缺数据、无待复核标的）")
    return lines


def render_report_section(tracker: HealthTracker) -> List[str]:
    """生成"数据源健康度"固定段（tracker 入口；空态也显示，趋势感知）。"""
    return render_report_section_from_summary(tracker.summary())


# ===========================================================================
# v5.3 D1：报告顶部"⚠️ 数据源告警"块（标红摘要；明细仍在下方固定段）
# ===========================================================================
def render_alerts_block(alerts: List[Dict[str, str]]) -> List[str]:
    """渲染报告顶部告警块：**有 alerts 才渲染**（无 → []，零噪音）。

    位置契约（brief D1-4）：标题行之后、"一、漏斗/KPI"之前。固定段
    （render_report_section_from_summary）**保持不动**——它继续展示全量明细，
    本块只做标红摘要。
    """
    if not alerts:
        return []
    lines: List[str] = [f"## ⚠️ 数据源告警（{len(alerts)} 项）", ""]
    for a in alerts:
        lines.append(f"- **{a['kind']}**: {a['detail']}")
    lines.append("")
    return lines
