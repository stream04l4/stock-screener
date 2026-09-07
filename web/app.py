# -*- coding: utf-8 -*-
"""stock-screener Web 后端（FastAPI）。

设计原则：
- 只读复用 screener 包与文件系统产物（output/、config/、logs/），不引入数据库。
- 触发运行用「子进程」而非在 uvicorn 线程内跑（首跑可能 ~2h，不能阻塞 API）。
- PUT /api/strategy 写回前备份 .bak，并做逐项类型/范围校验 + 复用 screener.config.load_config 兜底。

路径约定：PROJECT_ROOT = web/ 的上一级目录（即 ~/stock-screener）。
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import uuid
from datetime import date as _date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from ruamel.yaml import YAML

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
WEB_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = WEB_DIR.parent
OUTPUT_DIR = PROJECT_ROOT / "output"
CONFIG_PATH = PROJECT_ROOT / "config" / "strategy.yaml"
BAK_PATH = CONFIG_PATH.with_suffix(".yaml.bak")
LOGS_DIR = PROJECT_ROOT / "logs"
STATIC_DIR = WEB_DIR / "static"
VENV_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="stock-screener web", version="1.0")


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _read_csv_rows(path: Path) -> List[Dict[str, str]]:
    """读 result_*.csv（utf-8-sig，含 BOM），返回 list[dict]。"""
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return [dict(r) for r in csv.DictReader(f)]


def _mtime_iso(p: Path) -> str:
    return datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


# ---------------------------------------------------------------------------
# 报告 markdown 解析（漏斗 / 缺失名单 / 跳过行业组）
# ---------------------------------------------------------------------------
def _split_sections(md: str) -> Dict[str, str]:
    """按 '## ' / '### ' 标题切分，返回 {标题文本: 正文}。"""
    out: Dict[str, str] = {}
    cur_key = "__head__"
    buf: List[str] = []
    for line in md.splitlines():
        m = re.match(r"^(#{2,3})\s+(.*)$", line)
        if m:
            out[cur_key] = "\n".join(buf).strip()
            cur_key = m.group(2).strip()
            buf = []
        else:
            buf.append(line)
    out[cur_key] = "\n".join(buf).strip()
    return out


def _extract_count(value_str: str) -> Optional[int]:
    """从漏斗数值列提取整数：优先 **N**，否则取最后一个整数（支持负数）。"""
    bold = re.search(r"\*\*(-?\d+)\*\*", value_str)
    if bold:
        return int(bold.group(1))
    nums = re.findall(r"-?\d+", value_str)
    return int(nums[-1]) if nums else None


def _parse_md_table(block: str) -> List[List[str]]:
    """解析 markdown 表格数据行（跳过表头与分隔行），返回 cells 列表。"""
    rows: List[List[str]] = []
    for line in block.splitlines():
        s = line.strip()
        if not (s.startswith("|") and s.endswith("|")):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        # 跳过分隔行 |---|---|
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue
        rows.append(cells)
    return rows


def _parse_kpi_table(block: str) -> Dict[str, Any]:
    """解析 v2 报告「一、KPI 概览」表（| 指标 | 值 |）→ dict。

    键：selected / avg_ttm_yield_pct / avg_roe_pct / industry_concentration(str)。
    值列可能含 '—'（无数据）→ None。
    """
    kpi: Dict[str, Any] = {
        "selected": None,
        "avg_ttm_yield_pct": None,
        "avg_roe_pct": None,
        "industry_concentration": None,
    }
    for cells in _parse_md_table(block):
        if len(cells) < 2 or cells[0] == "指标":
            continue
        label, val = cells[0].strip(), cells[1].strip()
        if val in ("", "—"):
            continue
        if label.startswith("入选数"):
            m = re.search(r"(-?\d+)", val)
            kpi["selected"] = int(m.group(1)) if m else None
        elif "TTM股息率" in label:
            m = re.search(r"-?\d+(?:\.\d+)?", val)
            kpi["avg_ttm_yield_pct"] = float(m.group(0)) if m else None
        elif "平均ROE" in label:
            m = re.search(r"-?\d+(?:\.\d+)?", val)
            kpi["avg_roe_pct"] = float(m.group(0)) if m else None
        elif "行业集中度" in label:
            kpi["industry_concentration"] = val
    return kpi


def parse_report(md: str) -> Dict[str, Any]:
    """从 report_*.md 解析：漏斗、缺失名单、跳过行业组 + v2 KPI 概览。"""
    sections = _split_sections(md)

    # --- v2 KPI 概览：标题含「KPI」的节 ---
    kpi: Dict[str, Any] = {
        "selected": None,
        "avg_ttm_yield_pct": None,
        "avg_roe_pct": None,
        "industry_concentration": None,
    }
    for key, body in sections.items():
        if "KPI" in key:
            kpi = _parse_kpi_table(body)
            break

    # --- 漏斗：找标题含「过滤漏斗」的节 ---
    funnel: List[Dict[str, Any]] = []
    for key, body in sections.items():
        if "过滤漏斗" in key:
            for cells in _parse_md_table(body):
                if len(cells) < 3 or cells[0] in ("层级", ""):
                    continue
                label = cells[0].strip()
                # 跳过表头行（首列是「层级」）
                if label == "层级":
                    continue
                funnel.append(
                    {
                        "label": label,
                        "desc": cells[1].strip(),
                        "count": _extract_count(cells[2]),
                    }
                )
            break

    # --- 缺失名单：标题含「基本面数据缺失」的节 ---
    missing: List[Dict[str, str]] = []
    for key, body in sections.items():
        if "基本面数据缺失" in key:
            for cells in _parse_md_table(body):
                if len(cells) < 3 or cells[0] == "代码":
                    continue
                missing.append(
                    {"code": cells[0], "name": cells[1], "missing": cells[2]}
                )
            break

    # --- 跳过行业组：标题含「跳过排名约束」的节，行形如 "- C13农副食品加工业: 4 只" ---
    skipped: Dict[str, int] = {}
    for key, body in sections.items():
        if "跳过排名约束" in key:
            for line in body.splitlines():
                m = re.match(r"^\s*-\s*(.+?)\s*:\s*(\d+)\s*只", line)
                if m:
                    skipped[m.group(1).strip()] = int(m.group(2))
            break

    return {"funnel": funnel, "missing": missing, "skipped_groups": skipped, "kpi": kpi}


# ---------------------------------------------------------------------------
# 策略校验（PUT /api/strategy）
# ---------------------------------------------------------------------------
# 字段级 schema：{section: {field: (type, min, max, note)}}
# type ∈ {"int","float","bool","str","enum","prefix_list","float01"}
_STRATEGY_SCHEMA: Dict[str, Dict[str, tuple]] = {
    "technical": {
        "ma_period": ("int", 20, 500),
        "return_window_days": ("int", 60, 500),
        "min_return_pct": ("float", -100, 100),
        "max_return_pct": ("float", 0, 500),
        "max_annual_volatility_pct": ("float", 1, 200),
    },
    "dividend": {
        "window_days": ("int", 30, 730),
        "min_yield_pct": ("float", 0, 50),
    },
    "industry": {
        "rank_by": ("enum", "roeAvg", None),
        "top_pct": ("float", 0.0001, 100),
        "min_group_size": ("int", 1, 50),
    },
    "fundamental": {
        "roe_min_pct": ("float", -50, 100),
        "net_profit_yoy_field": ("enum2", "YOYPNI|YOYNI", None),
        "liability_max_pct": ("float", 0, 100),
        "gross_margin_min_pct": ("float", -100, 100),
        "probe_quarters_back": ("int", 1, 8),
    },
    "universe": {
        "a_share_prefixes": ("prefix_list", None, None),
        "listing_min_trading_days": ("int", 60, 500),
        "st_name_keyword": ("str", None, None),
    },
    # v2 打分模型（权重为嵌套 dict，单独校验；和≈1 由 screener.config.load_config 兜底）
    "scoring": {
        "mode": ("enum2", "zscore|legacy", None),
        "top_n": ("int", 1, 500),
        "missing_policy": ("enum2", "neutral_renorm|neutral|drop", None),
        "weights": ("weights_dict", None, None),
        "sub_weights": ("sub_weights_dict", None, None),
    },
    # v2 Web badge 阈值（百分数口径）。高股息(绿)复用 dividend.min_yield_pct，不另设键。
    "badges": {
        "industry_top_pct": ("float", 1, 100),
        "fscore_min": ("int", 0, 9),
    },
    # v2 硬性剔除开关
    "hard_filter": {
        "st_enabled": ("bool", None, None),
        "listing_min_trading_days": ("int", 60, 500),
    },
    "data": {
        "kline_calendar_days_back": ("int", 250, 800),
        "retry_max_attempts": ("int", 1, 20),
        "cache_dir": ("str", None, None),
    },
    "crosscheck": {
        "enabled": ("bool", None, None),
        "sample_size": ("int", 0, 100),
        "price_tolerance_pct": ("float", 0.01, 5),
        "batch_size": ("int", 1, 200),
    },
}

_DIMS = ("technical", "dividend", "industry", "fundamental")

# 所有必须出现的 (section, field) —— 「所有 key 必须齐全」
_REQUIRED_KEYS = [
    (s, f) for s, fields in _STRATEGY_SCHEMA.items() for f in fields
]


def _validate_strategy(cfg: Dict[str, Any]) -> List[str]:
    """返回错误列表；空列表 = 合法。"""
    errors: List[str] = []

    # 1) 顶层必须是 dict
    if not isinstance(cfg, dict):
        return ["strategy 顶层必须是 JSON 对象(mapping)"]

    # 2) key 齐全性（允许 crosscheck 整段缺省 → 用默认，但这里要求齐全更严格）
    for section, field in _REQUIRED_KEYS:
        if section not in cfg or not isinstance(cfg[section], dict):
            errors.append(f"缺少配置段: {section}")
            continue
        if field not in cfg[section]:
            errors.append(f"缺少配置项: {section}.{field}")

    # 3) 逐项类型 + 范围
    for section, fields in _STRATEGY_SCHEMA.items():
        sec = cfg.get(section)
        if not isinstance(sec, dict):
            continue
        for field, (typ, lo, hi) in fields.items():
            if field not in sec:
                continue
            v = sec[field]
            if typ == "int":
                # bool 是 int 的子类，需显式排除
                if isinstance(v, bool) or not isinstance(v, int):
                    errors.append(f"{section}.{field} 必须是整数")
                    continue
                if not (lo <= v <= hi):
                    errors.append(f"{section}.{field}={v} 超出范围 [{lo},{hi}]")
            elif typ == "float":
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    errors.append(f"{section}.{field} 必须是数值")
                    continue
                fv = float(v)
                if not (lo <= fv <= hi):
                    errors.append(f"{section}.{field}={v} 超出范围 [{lo},{hi}]")
            elif typ == "bool":
                if not isinstance(v, bool):
                    errors.append(f"{section}.{field} 必须是布尔值")
            elif typ == "str":
                if not isinstance(v, str) or not v.strip():
                    errors.append(f"{section}.{field} 必须是非空字符串")
            elif typ == "enum":
                if v != lo:
                    errors.append(f"{section}.{field} 目前只支持 {lo!r}")
            elif typ == "enum2":
                allowed = str(lo).split("|")
                if v not in allowed:
                    errors.append(f"{section}.{field} 只能是 {'/'.join(allowed)}")
            elif typ == "prefix_list":
                if not isinstance(v, list) or not v:
                    errors.append(f"{section}.{field} 必须是非空字符串数组")
                    continue
                for p in v:
                    if not isinstance(p, str) or not re.fullmatch(r"[a-z]{2}\.\d{2}", p):
                        errors.append(
                            f"{section}.{field} 项非法: {p!r}（应形如 sh.60）"
                        )
            elif typ == "weights_dict":
                if not isinstance(v, dict) or set(v) != set(_DIMS):
                    errors.append(
                        f"scoring.weights 必须恰好含 technical/dividend/industry/fundamental"
                    )
                else:
                    for d in _DIMS:
                        wv = v[d]
                        if isinstance(wv, bool) or not isinstance(wv, (int, float)) or wv < 0:
                            errors.append(f"scoring.weights.{d} 必须是非负数值")
            elif typ == "sub_weights_dict":
                if not isinstance(v, dict):
                    errors.append("scoring.sub_weights 必须是映射（每维度→子因子权重映射）")
                else:
                    for d in _DIMS:
                        sub = v.get(d)
                        if not isinstance(sub, dict) or not sub:
                            errors.append(f"scoring.sub_weights.{d} 必须是非空映射")
                            continue
                        for k, wv in sub.items():
                            if isinstance(wv, bool) or not isinstance(wv, (int, float)) or wv < 0:
                                errors.append(
                                    f"scoring.sub_weights.{d}.{k} 必须是非负数值"
                                )

    # 4) 跨字段语义（与 screener.config.load_config 保持一致，提前给出友好报错）
    tech = cfg.get("technical", {})
    if all(k in tech for k in ("min_return_pct", "max_return_pct")):
        try:
            if float(tech["min_return_pct"]) > float(tech["max_return_pct"]):
                errors.append("technical.min_return_pct 必须 <= max_return_pct")
        except (TypeError, ValueError):
            pass

    return errors


def _atomic_write_yaml(path: Path, data: Dict[str, Any]) -> None:
    """原子写：先写同目录临时文件，再 os.replace。"""
    text = yaml.safe_dump(
        data, allow_unicode=True, sort_keys=False, default_flow_style=False
    )
    _atomic_write_text(path, text)


def _atomic_write_text(path: Path, text: str) -> None:
    """原子写文本：先写同目录临时文件，再 os.replace。

    mkstemp 创建的临时文件权限固定为 0600，os.replace 会沿用该权限——若不在替换前
    恢复原文件 mode，每次保存都会把 config/strategy.yaml 从 644 悄悄改成 600（仓库外
    的可观测状态漂移）。这里在 replace 前把临时文件 chmod 回原文件的 mode。
    """
    try:
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        mode = 0o644
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, str(path))
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _ruamel_yaml() -> YAML:
    """构造保留注释/引号的 round-trip YAML 实例。

    indent(mapping=2, sequence=4, offset=2) 与 config/strategy.yaml 的既有排版一致：
    映射缩进 2、序列项在父键之下多缩进 2（即 "key:\\n    - item"），
    这样「未改动的行」dump 后逐字节不变，git diff 只出现真正修改的行。
    """
    yml = YAML()
    yml.preserve_quotes = True
    # D1（v2 round 1）：必须设足够大的行宽。ruamel 默认 width=80 会把 strategy.yaml
    # 里的长 flow-style 行（sub_weights 的 {technical: {...}, dividend: {...}}）折成
    # 多行 → _write_strategy_preserving_comments 自检 1（round-trip 逐字节还原）恒失败
    # → 每次保存都退回 safe_dump，注释全丢（v1 D-W03 回归）。4096 覆盖当前最长行
    # （~120 字符），保证 flow-style 原样输出。
    yml.width = 4096
    yml.indent(mapping=2, sequence=4, offset=2)
    return yml


def _new_scalar_like(old, v):
    """新值沿用旧节点的字面风格（D1，v2 round 1）。

    ruamel round-trip 解析时把数字的原始字面量信息记在 ScalarFloat/ScalarInt
    子类的 _width/_prec 等属性上（"0.30" → width=4, prec=2），dump 时据此还原
    尾零。直接赋 plain float 会丢失这些信息（"0.30"→"0.3"）；替换节点时把旧
    节点的元数据拷给新值，改完再恢复原值即可逐字节还原（brief 判据：PUT 恢复
    后 git diff HEAD 为空）。同数字位数的改动/恢复完全保真；位数变化（0.30→0.5）
    后恢复只能恢复到相同精度风格（"0.3"），属表示层固有限制，注释与排版不受影响。
    """
    from ruamel.yaml.scalarfloat import ScalarFloat
    from ruamel.yaml.scalarint import ScalarInt

    if isinstance(old, ScalarFloat) and isinstance(v, float) and not isinstance(v, bool):
        node = ScalarFloat(v)
        for a in ("_width", "_prec", "_m_sign", "_m_lead0", "_exp", "_e_width", "_e_sign"):
            setattr(node, a, getattr(old, a, None))
        return node
    if isinstance(old, ScalarInt) and isinstance(v, int) and not isinstance(v, bool):
        node = ScalarInt(v)
        # 与 ruamel 内部一致：_width 是动态属性（pyright 不识别，用 setattr）
        setattr(node, "_width", getattr(old, "_width", None))
        return node
    return v


def _apply_payload(old_map, new_dict) -> None:
    """把 payload 递归应用到 ruamel round-trip 节点上（D1，v2 round 1）。

    只替换**真正变化**的叶子值；未变化的键保留原 CommentedBase 节点。原因：
    用 plain Python 标量整体覆盖会丢失 ruamel 记录在原节点上的表示信息——
    `0.30` 变 `0.3`（plain 风格）、flow mapping `{a: 1, b: 2}` 被重排成块样式、
    行尾注释错位——即使值语义未变，dump 也会产生噪声 diff。保留原节点则
    no-op 保存逐字节还原（git diff 为空），改一个权重只动那一行。

    比较规则：
    - 嵌套 dict → 递归进原 CommentedMap（保持 flow/块样式与内部注释）。
    - 其余（标量/序列）按**值**相等跳过。注意 ruamel round-trip 的数值是
      ScalarFloat 子类（保留 "0.30" 这类原始字面风格），与 plain float 类型不同
      但值相等 → 必须按值比较，不能用 type() 精确匹配，否则所有浮点行都会
      被误判为"已变化"而丢失字面风格。
    - bool 单独设防：Python 的 True==1 会把 `true`/`1` 互判为未变，
      bool↔数值跨界时仍走替换（由调用方自检 2 兜底语义）。
    """
    from ruamel.yaml.comments import CommentedMap

    for k, v in new_dict.items():
        old = old_map.get(k)
        if isinstance(v, dict):
            if isinstance(old, CommentedMap):
                _apply_payload(old, v)  # 递归：未变子键的原节点不动
            else:
                old_map[k] = v          # 原值不是 mapping → 整体替换（自检2兜底）
            continue
        if (
            old is not None
            and old == v
            and not isinstance(old, bool)
            and not isinstance(v, bool)
        ):
            continue  # 值未变 → 原节点（含标量风格/flow 表示/行尾注释）零改动
        # 值变了：_new_scalar_like 按旧节点类型沿用字面风格（非数值原样返回 v）
        old_map[k] = _new_scalar_like(old, v)


def _write_strategy_preserving_comments(payload: Dict[str, Any]) -> None:
    """把校验过的 payload 写回 config/strategy.yaml，保留原文件注释与排版（D-W03）。

    步骤：ruamel round-trip 读入原文件 → 按字段覆盖为 payload 值
    （_validate_strategy 已保证 key 集合与 schema 完全一致，无孤儿键）→ dump。
    写回前自检：round-trip 原文件必须能逐字节还原（排版假设成立），且 dump 结果
    语义等于 payload；任一不满足则退回 safe_dump（丢注释但数据正确），
    绝不把格式错乱的文件写进仓库。

    D1（v2 round 1）：覆盖逻辑改为 _apply_payload 的叶子级最小替换——
    no-op 保存逐字节还原，真实改动只产生被修改行的 diff（行尾注释保留）。
    """
    import io

    def _safe_dump_text() -> str:
        return yaml.safe_dump(
            payload, allow_unicode=True, sort_keys=False, default_flow_style=False
        )

    orig = CONFIG_PATH.read_text(encoding="utf-8")
    try:
        yml = _ruamel_yaml()
        data = yml.load(orig)
        # 自检 1：原文件 round-trip 必须逐字节还原（注释/排版可被 ruamel 完整表示）
        probe_buf = io.StringIO()
        yml.dump(yml.load(orig), probe_buf)
        if probe_buf.getvalue() != orig:
            text = _safe_dump_text()
        else:
            _apply_payload(data, payload)
            buf = io.StringIO()
            yml.dump(data, buf)
            text = buf.getvalue()
            # 自检 2：dump 结果语义必须等于 payload（防止 ruamel 表示层丢值）
            if yaml.safe_load(text) != payload:
                text = _safe_dump_text()
    except Exception:  # noqa: BLE001 - round-trip 失败时退回语义正确但丢注释的 dump
        text = _safe_dump_text()
    _atomic_write_text(CONFIG_PATH, text)


# ---------------------------------------------------------------------------
# 运行任务管理（子进程）
# ---------------------------------------------------------------------------
class TaskManager:
    """跟踪后台筛选子进程。

    - 内存里保留 Popen，便于拿 returncode；
    - 另写 pidfile（logs/.web_run.lock），服务重启后仍能判断「是否已有运行」。
    """

    def __init__(self) -> None:
        self.tasks: Dict[str, Dict[str, Any]] = {}
        self._lock_path = LOGS_DIR / ".web_run.lock"

    # -- pidfile 辅助 --
    def _write_lock(self, task_id: str, pid: int) -> None:
        self._lock_path.write_text(f"{task_id}\n{pid}\n", encoding="utf-8")

    def _read_lock(self) -> Optional[tuple]:
        try:
            lines = self._lock_path.read_text(encoding="utf-8").split()
            return lines[0], int(lines[1])
        except Exception:
            return None

    def _clear_lock(self, task_id: str) -> None:
        cur = self._read_lock()
        if cur and cur[0] == task_id:
            try:
                self._lock_path.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False

    def active_task_id(self) -> Optional[str]:
        """返回当前正在运行的 task_id（内存优先，其次 pidfile）。"""
        for tid, t in self.tasks.items():
            p = t.get("popen")
            if p is not None and p.poll() is None:
                return tid
            # 无 popen（重启后）用 pid 判断
            if p is None and self._pid_alive(t["pid"]):
                return tid
        cur = self._read_lock()
        if cur and self._pid_alive(cur[1]):
            return cur[0]
        return None

    def start(self, run_date: str) -> Dict[str, Any]:
        existing = self.active_task_id()
        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"已有运行任务在进行中: {existing}",
            )

        task_id = "web_" + datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:4]
        log_path = LOGS_DIR / f"web_run_{task_id}.log"
        cmd = [
            str(VENV_PYTHON), "-m", "screener",
            "--date", run_date,
            "--config", "config/strategy.yaml",
        ]
        logf = open(log_path, "ab")
        logf.write(
            f"\n===== 启动 {datetime.now(timezone.utc).isoformat()} cmd={' '.join(cmd)} =====\n".encode()
        )
        logf.flush()

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(PROJECT_ROOT),
                stdout=logf,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                # 独立进程组：便于整组管理；服务停止时由 systemd 清理
                start_new_session=True,
            )
        except Exception as exc:  # noqa: BLE001
            logf.close()
            raise HTTPException(status_code=500, detail=f"启动子进程失败: {exc}")

        self.tasks[task_id] = {
            "pid": proc.pid,
            "popen": proc,
            "date": run_date,
            "log_path": str(log_path),
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        self._write_lock(task_id, proc.pid)
        return {
            "task_id": task_id,
            "date": run_date,
            "status": "running",
            "pid": proc.pid,
            "log_path": str(log_path),
        }

    def status(self, task_id: str) -> Dict[str, Any]:
        t = self.tasks.get(task_id)
        # 重启后内存里没有，但 pidfile/日志还在 → 从文件系统重建最小信息
        if t is None:
            log_path = LOGS_DIR / f"web_run_{task_id}.log"
            if not log_path.exists():
                raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
            cur = self._read_lock()
            pid = cur[1] if (cur and cur[0] == task_id) else None
            t = {"pid": pid, "popen": None, "log_path": str(log_path), "date": ""}

        log_path = Path(t["log_path"])
        tail = _tail_lines(log_path, 20) if log_path.exists() else []

        # 判定状态
        p = t.get("popen")
        if p is not None:
            rc = p.poll()
            if rc is None:
                state = "running"
            else:
                state = "done" if rc == 0 else "failed"
        elif t.get("pid"):
            if self._pid_alive(t["pid"]):
                state = "running"
            else:
                # 已结束：用日志尾部 + 产物判断 done/failed
                state = _infer_terminal_state(tail, t)
        else:
            state = _infer_terminal_state(tail, t)

        out: Dict[str, Any] = {
            "task_id": task_id,
            "status": state,
            "date": t.get("date", ""),
            "log_tail": tail,
        }
        if state in ("done", "failed"):
            run_day = _result_day_for(t.get("date") or "", log_path)
            # 对外统一 ISO；_result_day_for 内部返回紧凑 YYYYMMDD（用于拼文件路径）
            out["result_date"] = (
                f"{run_day[:4]}-{run_day[4:6]}-{run_day[6:]}" if run_day else None
            )
            out["api"] = (
                f"/api/runs/{run_day[:4]}-{run_day[4:6]}-{run_day[6:]}" if run_day else None
            )
            # 清理锁（仅当是本任务）
            self._clear_lock(task_id)
        return out


def _tail_lines(path: Path, n: int) -> List[str]:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = 8192
            data = b""
            while size > 0 and len(data) < block:
                step = min(size, block - len(data))
                f.seek(-step, os.SEEK_END)
                data = f.read(step) + data
                size -= step
        return data.decode("utf-8", errors="replace").splitlines()[-n:]
    except Exception:
        return []


def _infer_terminal_state(tail: List[str], t: Dict[str, Any]) -> str:
    """无 Popen 时（服务重启后）依据日志尾部与产物推断 done/failed。"""
    joined = "\n".join(tail)
    if "筛选完成" in joined:
        return "done"
    if re.search(r"错误|Traceback|Error", joined):
        # 有报错但可能只是中途告警；若日志里有最终「筛选完成」才算 done（上面已判）
        last = tail[-1] if tail else ""
        if "运行失败" in last or "Traceback" in joined:
            return "failed"
    # 兜底：看产物是否生成且晚于任务启动
    date_s = t.get("date") or ""
    if date_s:
        day = date_s.replace("-", "")
        csv_p = OUTPUT_DIR / f"result_{day}.csv"
        if csv_p.exists():
            return "done"
    return "failed"


def _result_day_for(date_s: str, log_path) -> Optional[str]:
    """把请求日期映射到实际结果文件（处理非交易日回退）：找 output/result_*.csv 中最新且 mtime 晚于任务启动的。

    D2（v2 round 1）：log_path 可为 Path（status 端点）、日志行 list
    （SSE done 事件补全字段用——流式端点拿不到 TaskManager 上下文，只能
    从已读日志尾部抓 run_day）或 None。

    无请求日期分支只信**本任务自己的日志证据**（「运行日」行 → 产物路径行），
    不做 output/ 最新文件兜底：对 live 运行那是上一次运行的产物，对旧任务重放
    可能是后来新运行的产物——宁缺毋错。output/ 兜底只保留在有请求日期分支
    （status 端点语义：任务刚结束，最新文件 = 本任务产物）。
    """
    if not date_s:
        # 无请求日期时只能从日志尾部抓 run_day；log_path=None（调用方无上下文）→ 直接 None
        if isinstance(log_path, Path):
            tail_lines = _tail_lines(log_path, 200)
        elif log_path is None:
            return None
        else:
            tail_lines = list(log_path)[-200:]
        # 从日志里抓 run_day
        m = re.search(r"run_day=(\d{8})|运行日 (\d{4}-\d{2}-\d{2})", "\n".join(tail_lines))
        if m:
            raw = m.group(1) or m.group(2)
            # 契约：恒返回紧凑 YYYYMMDD（调用方按 [4:6] 切片拼 ISO）。
            # 原实现 len==8 才去横线，group(2) 的带横线日期会原样漏出 → 拼出 "2026--09-04"。
            return raw.replace("-", "")
        # 次选：日志里的产物路径行（引擎在最终「筛选完成 · 运行日」行之前先打印
        # 「CSV: .../result_YYYYMMDD.csv」「报告: .../report_YYYYMMDD.md」）——
        # 绑定到本任务自己的产物，比"output/ 最新文件"更准（旧任务重放时不会
        # 误拿后来新运行的日期）。
        m = re.search(r"(?:result|report)_(\d{8})\.(?:csv|md)", "\n".join(tail_lines))
        if m:
            return m.group(1)
        return None
    day = date_s.replace("-", "")
    # 直接命中
    if (OUTPUT_DIR / f"result_{day}.csv").exists():
        return day
    # 回退：找 output/ 里最新的 result_*.csv（运行刚结束，取最新即可）
    cands = sorted(OUTPUT_DIR.glob("result_*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    if cands:
        return cands[0].stem.replace("result_", "")
    return None


# ---------------------------------------------------------------------------
# Pydantic 模型
# ---------------------------------------------------------------------------
class RunRequest(BaseModel):
    date: Optional[str] = None


# ---------------------------------------------------------------------------
# API 路由
# ---------------------------------------------------------------------------
tm = TaskManager()


@app.get("/api/runs")
def list_runs() -> Dict[str, Any]:
    runs: List[Dict[str, Any]] = []
    for csv_p in sorted(OUTPUT_DIR.glob("result_*.csv")):
        m = re.match(r"result_(\d{8})\.csv$", csv_p.name)
        if not m:
            continue
        day = m.group(1)
        date_iso = f"{day[:4]}-{day[4:6]}-{day[6:]}"
        try:
            rows = _read_csv_rows(csv_p)
        except Exception:  # noqa: BLE001
            rows = []
        selected = sum(1 for r in rows if (r.get("top_n_selected") or "").strip() == "1")
        if not selected:  # legacy 报告无 top_n_selected 列 → 回退 pass_all
            selected = sum(1 for r in rows if (r.get("pass_all") or "").strip() == "是")
        report_p = OUTPUT_DIR / f"report_{day}.md"
        # v2 KPI（从报告「一、KPI 概览」解析；旧报告 → None）
        kpi: Dict[str, Any] = {}
        if report_p.exists():
            try:
                kpi = parse_report(report_p.read_text(encoding="utf-8")).get("kpi", {})
            except Exception:  # noqa: BLE001
                kpi = {}
        runs.append(
            {
                "date": date_iso,
                "selected_count": selected,
                "total_candidates": len(rows),
                "generated_at": _mtime_iso(csv_p),
                "has_report": report_p.exists(),
                "avg_ttm_yield_pct": kpi.get("avg_ttm_yield_pct"),
                "avg_roe_pct": kpi.get("avg_roe_pct"),
            }
        )
    # 倒序（最新在前）
    runs.sort(key=lambda r: r["date"], reverse=True)
    return {"runs": runs}


@app.get("/api/runs/{day}")
def run_detail(day: str) -> Dict[str, Any]:
    # 统一对外日期格式为 ISO（YYYY-MM-DD），与列表端点/CLI 一致；
    # 兼容旧客户端的紧凑格式 YYYYMMDD。两种格式都做真实日历校验。
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        compact = day.replace("-", "")
    elif re.fullmatch(r"\d{8}", day):
        compact = day
    else:
        raise HTTPException(status_code=400, detail="date 应为 YYYY-MM-DD（或兼容 YYYYMMDD）")
    try:
        _date(int(compact[:4]), int(compact[4:6]), int(compact[6:]))
    except ValueError:
        raise HTTPException(status_code=400, detail=f"非法日期: {day}")
    csv_p = OUTPUT_DIR / f"result_{compact}.csv"
    report_p = OUTPUT_DIR / f"report_{compact}.md"
    if not csv_p.exists():
        raise HTTPException(status_code=404, detail=f"无该日运行结果: {day}")

    rows = _read_csv_rows(csv_p)
    # v2(zscore) 入选 = top_n_selected==1；legacy 入选 = pass_all=="是"。
    # 优先按 top_n_selected（v2 列），为空再回退 legacy 语义，保证两种模式都正确。
    selected = [r for r in rows if (r.get("top_n_selected") or "").strip() == "1"]
    if not selected:
        selected = [r for r in rows if (r.get("pass_all") or "").strip() == "是"]

    md = ""
    parsed: Dict[str, Any] = {"funnel": [], "missing": [], "skipped_groups": {}, "kpi": {}}
    if report_p.exists():
        md = report_p.read_text(encoding="utf-8")
        parsed = parse_report(md)

    # badge 阈值全部来自 config（前端不得硬编码）。
    # 高股息(绿) 阈值 = dividend.min_yield_pct（brief R5 指定，单一事实来源）；
    # 行业TopN% / F-Score 来自 badges 段。
    try:
        _cfg_doc = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        _badges_cfg = _cfg_doc.get("badges", {}) or {}
        _div_cfg = _cfg_doc.get("dividend", {}) or {}
    except Exception:  # noqa: BLE001
        _badges_cfg, _div_cfg = {}, {}

    return {
        "date": f"{compact[:4]}-{compact[4:6]}-{compact[6:]}",
        "generated_at": _mtime_iso(csv_p),
        "funnel": parsed["funnel"],
        "kpi": parsed.get("kpi", {}),
        "badges": {
            "high_dividend_pct": float(_div_cfg.get("min_yield_pct", 0)),
            "industry_top_pct": float(_badges_cfg.get("industry_top_pct", 10)),
            "fscore_min": int(_badges_cfg.get("fscore_min", 7)),
        },
        "selected": selected,
        "survivors": rows,  # v2 CSV 全量列（zscore 模式=全体打分候选）
        "missing_fundamental": parsed["missing"],
        "skipped_groups": parsed["skipped_groups"],
        "report_md": md,
    }


@app.get("/api/strategy")
def get_strategy() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise HTTPException(status_code=404, detail="config/strategy.yaml 不存在")
    raw = CONFIG_PATH.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    return {"json": data, "raw": raw}


# ---------------------------------------------------------------------------
# v3 回测（读 output/backtest/*.csv → 结构化 JSON；前端 ECharts 画净值曲线）
# ---------------------------------------------------------------------------
BACKTEST_DIR = PROJECT_ROOT / "output" / "backtest"


@app.get("/api/backtest")
def get_backtest() -> Dict[str, Any]:
    """回测三件套 → {equity_curve:[{date,strategy,<bench...>}], metrics:{...}, holdings:[...]}。

    指标由 backtest.metrics_bt.summarize 从 equity_curve.csv 现算（与 report.md 同口径）。
    产物不存在 → 404（提示先跑 .venv/bin/python -m backtest.engine）。
    """
    eq_p = BACKTEST_DIR / "equity_curve.csv"
    mh_p = BACKTEST_DIR / "monthly_holdings.csv"
    rp_p = BACKTEST_DIR / "report.md"
    if not eq_p.exists():
        raise HTTPException(
            status_code=404,
            detail="output/backtest/ 产物不存在（先离线跑回测：.venv/bin/python -m backtest.engine）")

    with open(eq_p, encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None) or []
        rows = [r for r in reader if r]
    if not header or len(rows) < 2:
        raise HTTPException(status_code=404, detail="equity_curve.csv 为空")

    bench_cols = [c for c in header[1:] if c != "strategy_nav"]
    equity_curve: List[Dict[str, Any]] = []
    dates: List[str] = []
    navs: List[float] = []
    bench_series: Dict[str, Tuple[List[str], List[float]]] = {c: ([], []) for c in bench_cols}
    for r in rows:
        if len(r) < 2:
            continue
        d = r[0].strip()
        try:
            v = float(r[1])
        except ValueError:
            continue
        dates.append(d)
        navs.append(v)
        item: Dict[str, Any] = {"date": d, "strategy": round(v, 6)}
        for j, c in enumerate(bench_cols):
            cell = r[2 + j].strip() if len(r) > 2 + j else ""
            item[c] = round(float(cell), 6) if cell else None
            if cell:
                bench_series[c][0].append(d)
                bench_series[c][1].append(float(cell))
        equity_curve.append(item)

    # 指标（全窗口 + 1y/3y/5y 切片；基准取第一个可用序列）
    from backtest.metrics_bt import slice_window, summarize
    bt_cfg = (yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}).get("backtest") or {}
    rf = float(bt_cfg.get("risk_free_pct", 2.0))
    bd, bn = ([], [])
    for c in bench_cols:
        if len(bench_series[c][1]) >= 2:
            bd, bn = bench_series[c]
            break
    full = summarize(dates, navs, rf, bench_dates=bd or None, bench_navs=bn or None)

    def _win(days: int) -> Dict[str, Any]:
        sd, sn = slice_window(dates, navs, days)
        s = summarize(sd, sn, rf, bench_dates=bd or None, bench_navs=bn or None)
        return {"start": s.start, "end": s.end,
                "total_return_pct": _r2(s.total_return_pct),
                "annual_return_pct": _r2(s.annual_return_pct),
                "sharpe": _r2(s.sharpe), "max_drawdown_pct": _r2(s.max_drawdown_pct)}

    metrics = {
        "window": {"start": full.start, "end": full.end, "n_days": full.n_days},
        "full": _win(10**9),
        "slices": {"1y": _win(250), "3y": _win(750), "5y": _win(1250)},
        "monthly_win_rate_pct": _r2(full.monthly_win_rate_pct),
        "calmar": _r2(full.calmar), "beta": _r2(full.beta),
        "alpha_annual_pct": _r2(full.alpha_annual_pct),
        "info_ratio": _r2(full.info_ratio),
    }

    holdings: List[Dict[str, Any]] = []
    if mh_p.exists():
        with open(mh_p, encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for r in reader:
                holdings.append({
                    "date": r.get("date", ""), "code": r.get("code", ""),
                    "name": r.get("name", ""),
                    "weight": float(r["weight"]) if r.get("weight") else None,
                    "entry_price": float(r["entry_price"]) if r.get("entry_price") else None,
                    "total_score": float(r["total_score"]) if r.get("total_score") else None,
                })

    report_md = rp_p.read_text(encoding="utf-8") if rp_p.exists() else ""
    return {"equity_curve": equity_curve, "metrics": metrics,
            "holdings": holdings, "benchmarks": bench_cols, "report_md": report_md}


def _r2(v: Optional[float]) -> Optional[float]:
    return None if v is None else round(float(v), 4)


@app.put("/api/strategy")
def put_strategy(payload: Dict[str, Any]) -> Dict[str, Any]:
    errors = _validate_strategy(payload)
    if errors:
        raise HTTPException(status_code=400, detail={"errors": errors})

    # 复用 screener.config.load_config 做最终结构/语义校验（写临时文件）
    try:
        from screener import config as cfgmod
        fd, tmp = tempfile.mkstemp(suffix=".yaml")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(payload, f, allow_unicode=True, sort_keys=False)
        try:
            cfgmod.load_config(tmp)
        finally:
            os.unlink(tmp)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail={"errors": [f"配置校验失败: {exc}"]})

    # 备份（保留最近一份）
    if CONFIG_PATH.exists():
        shutil.copy2(CONFIG_PATH, BAK_PATH)

    # D-W03：写回时保留原文件注释与排版（ruamel round-trip，失败退回 safe_dump）
    _write_strategy_preserving_comments(payload)
    return {"ok": True, "backup": str(BAK_PATH)}


@app.post("/api/runs")
def trigger_run(req: RunRequest) -> Dict[str, Any]:
    run_date = req.date or datetime.now().strftime("%Y-%m-%d")
    # 日期格式校验
    try:
        datetime.strptime(run_date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail=f"date 格式应为 YYYY-MM-DD，收到 {run_date!r}")
    if not VENV_PYTHON.exists():
        raise HTTPException(status_code=500, detail=f"找不到 venv python: {VENV_PYTHON}")
    return tm.start(run_date)


@app.get("/api/runs/{task_id}/status")
def run_status(task_id: str) -> Dict[str, Any]:
    if not re.fullmatch(r"web_[A-Za-z0-9_]+", task_id):
        raise HTTPException(status_code=400, detail="非法 task_id")
    return tm.status(task_id)


# ---------------------------------------------------------------------------
# SSE 实时日志（R5：tail logs/web_run_{task_id}.log，按字节偏移增量推）
# ---------------------------------------------------------------------------
def _classify_log_line(line: str, task_date: str = "", tail_lines: Optional[List[str]] = None) -> Dict[str, Any]:
    """把一行运行日志分类为 SSE 事件（报告 R5 classify()）。

    - [PROGRESS] stage=... done=N total=M → progress（结构化进度标记，v2 新增）
    - 「筛选完成」→ done；「运行失败」/Traceback/Error → error；其余 → log。

    D2（v2 round 1）：done 事件按 R5 规格补全 result_date/api 两字段。
    解析优先级（都是"实际运行日"的证据，越靠前越精确）：
    1. tail_lines 里的「运行日 YYYY-MM-DD」（done 行自带 = 回退后的交易日）；
    2. tail_lines 里的产物路径 result_YYYYMMDD.csv / report_YYYYMMDD.md
       （CLI 在最终运行日行之前打印，绑定本任务自己的产物）；
    3. task_date（内存任务日期或日志启动横幅 --date）——仅当上面都还没有时
       用：引擎的「筛选完成: mode=...」行先于 CLI 的运行日/产物行出现，第一条
       done 事件到达时只有请求日期可用；非交易日回退场景下后续 done 行会带着
       真实运行日覆盖语义（两条 done 事件各自独立解析）。
    """
    m = re.search(r"\[PROGRESS\]\s+stage=(\S+)\s+done=(\d+)\s+total=(\d+)", line)
    if m:
        return {"type": "progress", "stage": m.group(1),
                "done": int(m.group(2)), "total": int(m.group(3))}
    if "筛选完成" in line:
        ev: Dict[str, Any] = {"type": "done"}
        run_day = _result_day_for("", tail_lines or []) or _result_day_for(task_date, None)
        if run_day:
            iso = f"{run_day[:4]}-{run_day[4:6]}-{run_day[6:]}"
            ev["result_date"] = iso
            ev["api"] = f"/api/runs/{iso}"
        return ev
    if "运行失败" in line or "Traceback" in line or re.search(r"\bError\b", line):
        return {"type": "error"}
    return {"type": "log", "text": line}


@app.get("/api/runs/{task_id}/events")
async def run_events(task_id: str, request: Request) -> StreamingResponse:
    """SSE 事件流：log / progress / done / error + 15s 心跳。

    - `id: <offset>` = 已推送到该字节偏移；断线重连带 Last-Event-ID 从该偏移续读
      （自动补发断连期间错过的日志）。无 offset → 从头补发全量历史。
    - 子进程结束且日志读完 → 发送终态事件后关闭流（前端据此停止轮询兜底）。
    - X-Accel-Buffering:no：禁止代理缓冲（内网 nginx/uvicorn 场景，报告 R5）。
    """
    if not re.fullmatch(r"web_[A-Za-z0-9_]+", task_id):
        raise HTTPException(status_code=400, detail="非法 task_id")
    log_path = LOGS_DIR / f"web_run_{task_id}.log"
    if not log_path.exists():
        raise HTTPException(status_code=404, detail=f"任务日志不存在: {task_id}")

    # D2：done 事件补全 result_date/api 需要任务上下文（请求日期 + 已读日志尾部）。
    # 内存里有该任务 → 用其 date；重启后内存为空 → 从日志首行启动横幅抓请求日期
    # （cmd=... --date YYYY-MM-DD），再由 classify 的「运行日」/产物逻辑处理非交易日回退。
    task = tm.tasks.get(task_id)
    task_date = (task or {}).get("date") or ""
    if not task_date:
        try:
            with open(log_path, "rb") as f:
                head = f.read(2048).decode("utf-8", errors="replace")
            m = re.search(r"--date (\d{4}-\d{2}-\d{2})", head)
            task_date = m.group(1) if m else ""
        except OSError:
            pass

    last_event_id = request.headers.get("Last-Event-ID") or request.query_params.get(
        "last_event_id")
    try:
        start_offset = int(last_event_id) if last_event_id else 0
    except (TypeError, ValueError):
        start_offset = 0

    async def gen():
        sent_terminal = False
        idle_ticks = 0
        offset = start_offset
        seen_lines: List[str] = []  # 本流已推送的日志行（供 done 事件抓「运行日」）
        while True:
            # 客户端断开 → 尽快退出，不空转
            if await request.is_disconnected():
                break
            try:
                size = log_path.stat().st_size
            except OSError:
                break
            if size > offset:
                with open(log_path, "rb") as f:
                    f.seek(offset)
                    chunk = f.read(size - offset)
                    offset = size
                for line in chunk.decode("utf-8", errors="replace").splitlines():
                    if not line.strip():
                        continue  # 空行（TaskManager 启动横幅前的换行）不推
                    seen_lines.append(line)  # 先入上下文：done 行自带「运行日」，classify 需要它
                    ev = _classify_log_line(line, task_date=task_date, tail_lines=seen_lines)
                    sent_terminal = sent_terminal or ev["type"] in ("done", "error")
                    yield f"id: {offset}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"
                idle_ticks = 0
            else:
                idle_ticks += 1
                if idle_ticks % 15 == 0:  # ~15s 心跳（poll=1s）
                    yield "data: {\"type\":\"heartbeat\"}\n\n"
                if sent_terminal and offset >= size:
                    break
                await asyncio.sleep(1)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
    )


# ---------------------------------------------------------------------------
# 个股明细（R5：本地稳定键缓存重建 af1 + MA，只读文件、离线可用）
# ---------------------------------------------------------------------------
@app.get("/api/stocks/{code}/detail")
def stock_detail(code: str, run_day: str) -> Dict[str, Any]:
    """GET /api/stocks/{code}/detail?run_day=YYYY-MM-DD

    响应 schema（报告 R5）：
    {code, name, industry, factors:{...四维原始因子值...}, scores:{z_*, score_*},
     kline:{dates:[], close_af1:[], close_af3:[], ma20:[], ma60:[]}}

    K线数据源 = 本地 `kline_af3_{code}` + `adjfactor_{code}` 稳定键缓存重建 af=1
    （不拉 BaoStock）；MA20/60 由 close_af1 计算。截断到 run_day（含）为止。
    """
    if not re.fullmatch(r"(sh|sz)\.\d{6}", code):
        raise HTTPException(status_code=400, detail=f"非法代码: {code}")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", run_day):
        raise HTTPException(status_code=400, detail="run_day 应为 YYYY-MM-DD")

    # 1) factors/scores：从该 run_day 的 result CSV 取（若存在）
    compact = run_day.replace("-", "")
    csv_p = OUTPUT_DIR / f"result_{compact}.csv"
    row: Optional[Dict[str, str]] = None
    if csv_p.exists():
        for r in _read_csv_rows(csv_p):
            if r.get("code") == code:
                row = r
                break

    factor_cols = [
        "ma_bullish", "window_return_pct", "annual_vol_pct", "rsi14",
        "macd_golden_cross", "ttm_dividend_yield_pct", "payout_ratio_pct",
        "industry_roe_rank_pct", "industry_yoy_pni_rank_pct", "roe_pct",
        "roe_3y_mean_pct", "roe_3y_std_pct", "liability_pct", "gross_margin_pct",
        "piotroski_fscore", "piotroski_valid",
    ]
    score_cols = ["z_technical", "z_dividend", "z_industry", "z_fundamental",
                  "score_technical", "score_dividend", "score_industry",
                  "score_fundamental", "total_score", "rank", "top_n_selected"]

    def _num(v: Optional[str]) -> Optional[float]:
        if v is None or str(v).strip() in ("", "nan"):
            return None
        try:
            f = float(v)
            return None if f != f else f  # NaN → None
        except (TypeError, ValueError):
            return None

    factors: Dict[str, Any] = {c: _num(row.get(c)) for c in factor_cols} if row else {}
    scores: Dict[str, Any] = {c: _num(row.get(c)) for c in score_cols} if row else {}

    # 2) kline：本地稳定键缓存重建（离线、纯文件读取，不实例化 BaoStock 客户端）
    kline_out: Dict[str, Any] = {"dates": [], "close_af1": [],
                                 "close_af3": [], "ma20": [], "ma60": []}
    try:
        from screener.data.cache import DiskCache, make_cache_name
        from screener.reconstruct import moving_average, rebuild_kline_series

        cache = DiskCache(str(PROJECT_ROOT / "cache"))
        kl = cache.get(make_cache_name("kline_af3", code))
        if kl and kl["rows"]:
            af = cache.get(make_cache_name("adjfactor", code))
            factor_rows = list(af["rows"]) if af else []
            rebuilt = rebuild_kline_series(kl["rows"], factor_rows, kl["columns"])
            dates, af3, af1 = (
                rebuilt["dates"], rebuilt["af3_close"], rebuilt["af1_close"])
            cut = len(dates)
            for i, d in enumerate(dates):
                if d > run_day:
                    cut = i
                    break
            ma20_full = moving_average(af1, 20)
            ma60_full = moving_average(af1, 60)
            kline_out = {
                "dates": dates[:cut],
                "close_af1": [None if v is None else round(float(v), 4) for v in af1[:cut]],
                "close_af3": [None if v is None else round(float(v), 4) for v in af3[:cut]],
                "ma20": [None if v is None else round(float(v), 4) for v in ma20_full[:cut]],
                "ma60": [None if v is None else round(float(v), 4) for v in ma60_full[:cut]],
            }
    except Exception as exc:  # noqa: BLE001 - 缓存缺失/损坏时仍返回 factors/scores
        kline_out["error"] = f"本地缓存重建失败: {exc}"

    return {
        "code": code,
        "name": (row or {}).get("name", ""),
        "industry": (row or {}).get("industry", ""),
        "run_day": run_day,
        "factors": factors,
        "scores": scores,
        "kline": kline_out,
    }


@app.get("/api/health")
def health() -> Dict[str, Any]:
    return {
        "ok": True,
        "time": datetime.now(timezone.utc).isoformat(),
        "project_root": str(PROJECT_ROOT),
        "active_task": tm.active_task_id(),
    }


# ---------------------------------------------------------------------------
# 静态前端（最后挂载，避免吞掉 /api/*）
# ---------------------------------------------------------------------------
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(str(STATIC_DIR / "index.html"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=3080)
