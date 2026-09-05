# -*- coding: utf-8 -*-
"""stock-screener Web 后端（FastAPI）。

设计原则：
- 只读复用 screener 包与文件系统产物（output/、config/、logs/），不引入数据库。
- 触发运行用「子进程」而非在 uvicorn 线程内跑（首跑可能 ~2h，不能阻塞 API）。
- PUT /api/strategy 写回前备份 .bak，并做逐项类型/范围校验 + 复用 screener.config.load_config 兜底。

路径约定：PROJECT_ROOT = web/ 的上一级目录（即 ~/stock-screener）。
"""
from __future__ import annotations

import csv
import io
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
from typing import Any, Dict, List, Optional

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
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


def parse_report(md: str) -> Dict[str, Any]:
    """从 report_*.md 解析：漏斗、缺失名单、跳过行业组。"""
    sections = _split_sections(md)

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

    return {"funnel": funnel, "missing": missing, "skipped_groups": skipped}


# ---------------------------------------------------------------------------
# 策略校验（PUT /api/strategy）
# ---------------------------------------------------------------------------
# 字段级 schema：{section: {field: (type, min, max, note)}}
# type ∈ {"int","float","bool","str","enum","prefix_list"}
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
    yml.indent(mapping=2, sequence=4, offset=2)
    return yml


def _write_strategy_preserving_comments(payload: Dict[str, Any]) -> None:
    """把校验过的 payload 写回 config/strategy.yaml，保留原文件注释与排版（D-W03）。

    步骤：ruamel round-trip 读入原文件 → 按字段覆盖为 payload 值
    （_validate_strategy 已保证 key 集合与 schema 完全一致，无孤儿键）→ dump。
    写回前自检：round-trip 原文件必须能逐字节还原（排版假设成立），且 dump 结果
    语义等于 payload；任一不满足则退回 safe_dump（丢注释但数据正确），
    绝不把格式错乱的文件写进仓库。
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
            for section, fields in payload.items():
                if isinstance(fields, dict):
                    for k, v in fields.items():
                        data[section][k] = v
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


def _result_day_for(date_s: str, log_path: Path) -> Optional[str]:
    """把请求日期映射到实际结果文件（处理非交易日回退）：找 output/result_*.csv 中最新且 mtime 晚于任务启动的。"""
    if not date_s:
        # 从日志里抓 run_day
        m = re.search(r"run_day=(\d{8})|运行日 (\d{4}-\d{2}-\d{2})", "\n".join(_tail_lines(log_path, 200)))
        if m:
            raw = m.group(1) or m.group(2)
            return raw.replace("-", "") if len(raw) == 8 else raw
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
        selected = sum(1 for r in rows if (r.get("pass_all") or "").strip() == "是")
        report_p = OUTPUT_DIR / f"report_{day}.md"
        runs.append(
            {
                "date": date_iso,
                "selected_count": selected,
                "total_candidates": len(rows),
                "generated_at": _mtime_iso(csv_p),
                "has_report": report_p.exists(),
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
    selected = [r for r in rows if (r.get("pass_all") or "").strip() == "是"]

    md = ""
    parsed = {"funnel": [], "missing": [], "skipped_groups": {}}
    if report_p.exists():
        md = report_p.read_text(encoding="utf-8")
        parsed = parse_report(md)

    return {
        "date": f"{compact[:4]}-{compact[4:6]}-{compact[6:]}",
        "generated_at": _mtime_iso(csv_p),
        "funnel": parsed["funnel"],
        "selected": selected,
        "survivors": rows,  # 全部技术面幸存者（CSV 全量）
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
