# -*- coding: utf-8 -*-
"""stock-screener v7.0 Web 后端（FastAPI）。

v7.0 定位（策略核心替换后）：
- 查询/按需回测界面：/api/bt/runs* 族（异步 TaskManager + SSE）+ /api/positions
  + /api/pool（同步，全池 ~2s）；
- strategy 读写/schema 校验 + 策略库（保留 v6.2 机制，schema 换 v7 slim）；
- 数据湖页签 /api/lake/*（lake router 原样挂载）；
- 已删除：/api/runs 族（v6 日跑）、/api/backtest（多因子回测产物）、
  /api/stocks/{code}/detail（因子明细）、report md 解析器。

设计原则：
- 触发回测用「子进程」而非 uvicorn 线程内跑（不能阻塞 API）；
- PUT /api/strategy 写回前备份 .bak，逐项类型/范围校验 + martingale.config.
  load_config 语义校验兜底。

路径约定：PROJECT_ROOT = web/ 的上一级目录（即 ~/stock-screener）。
"""
from __future__ import annotations

import asyncio
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
from fastapi import FastAPI, HTTPException, Request, Response
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
# "我的持仓"用户持仓账本（本机状态，/data/ 已 gitignore；单用户单进程写，tmp+rename 原子落盘）
USER_POSITIONS_PATH = PROJECT_ROOT / "data" / "user_positions.json"
_TS_CODE_RE = re.compile(r"^[a-z]{2}\.\d{6}$")
# v7 回测产物根（CLI 默认 out；Web 任务用 runs/<task_id> 子目录隔离）
BT_OUT_DIR = OUTPUT_DIR / "backtest_martingale"
BT_RUNS_DIR = BT_OUT_DIR / "runs"
STRATEGIES_DIR = PROJECT_ROOT / "data" / "strategies"
LOGS_DIR = PROJECT_ROOT / "logs"
DIST_DIR = WEB_DIR / "dist"   # Vue3+Vite 构建产物（`npm run build` @ web/frontend → ../dist）
VENV_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)
BT_RUNS_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="stock-screener web", version="7.0.0")


# ---------------------------------------------------------------------------
# 登录门禁（v7.0.14）：/api/* 全部要登录，白名单 = login/logout/health。
# 会话 = web/auth.py 无状态签名 cookie；页面壳（/）公开，前端未登录时渲染登录页。
# ---------------------------------------------------------------------------
from web.auth import (COOKIE_NAME, TOKEN_TTL_S, check_credentials,  # noqa: E402
                      load_auth_config, lock_status, make_token,
                      parse_token, record_fail, record_ok)

_AUTH_PUBLIC_PATHS = {"/api/login", "/api/logout", "/api/health"}

# 测试钩子：lake HTTP 契约测试以子进程起**真实** web.app（uvicorn），子进程内置
# WEB_TEST_NO_AUTH=1 跳过登录门禁（那些用例测的是 lake 同步语义，不是登录；
# 登录门禁本身由 tests/test_web_auth.py 用 TestClient 全量覆盖）。
# 生产 systemd 环境不设该变量，钩子恒关。
_AUTH_DISABLED = os.environ.get("WEB_TEST_NO_AUTH") == "1"


@app.middleware("http")
async def _auth_gate(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and not _AUTH_DISABLED and path not in _AUTH_PUBLIC_PATHS:
        _u, _p, secret = load_auth_config()
        if parse_token(request.cookies.get(COOKIE_NAME), secret) is None:
            return JSONResponse({"detail": "未登录或会话已过期，请先登录"}, status_code=401)
    return await call_next(request)


# ---------------------------------------------------------------------------
# 登录/登出/当前用户
# ---------------------------------------------------------------------------
class _LoginBody(BaseModel):
    username: str = ""
    password: str = ""


@app.post("/api/login")
def login(body: _LoginBody, request: Request, response: Response) -> Dict[str, Any]:
    """登录成功则写签名 cookie（HttpOnly / SameSite=Lax / 7 天）。

    失败 401；连续 5 次失败锁 120s 返回 429；凭据未配置 500 显式失败（不静默放行）。
    """
    admin_user, admin_pass, secret = load_auth_config()
    if not admin_user or not admin_pass:
        raise HTTPException(status_code=500,
                            detail="登录凭据未配置（.env WEB_ADMIN_USER/WEB_ADMIN_PASS）")
    ip = request.client.host if request.client else "unknown"
    if lock_status(ip):
        raise HTTPException(status_code=429, detail="失败次数过多，请 2 分钟后再试")
    if not check_credentials(body.username, body.password, admin_user, admin_pass):
        record_fail(ip)
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    record_ok(ip)
    response.set_cookie(COOKIE_NAME, make_token(admin_user, secret), max_age=TOKEN_TTL_S,
                        httponly=True, samesite="lax", path="/")
    return {"ok": True, "user": admin_user}


@app.post("/api/logout")
def logout(response: Response) -> Dict[str, Any]:
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@app.get("/api/auth/me")
def auth_me(request: Request) -> Dict[str, Any]:
    _u, _p, secret = load_auth_config()
    user = parse_token(request.cookies.get(COOKIE_NAME), secret)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return {"user": user}


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _read_csv_rows(path: Path) -> List[Dict[str, str]]:
    """读 CSV（utf-8-sig，含 BOM），返回 list[dict]。"""
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return [dict(r) for r in csv.DictReader(f)]


def _read_csv_rows_floats(path: Path) -> List[Dict[str, Any]]:
    """读 CSV 并把可解析的数值列转 float（回测结果 JSON 化；NaN → None）。"""
    out: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            row: Dict[str, Any] = {}
            for k, v in r.items():
                if v is None or v == "":
                    row[k] = None
                    continue
                try:
                    fv = float(v)
                    row[k] = None if fv != fv else fv  # NaN → None
                except ValueError:
                    row[k] = v
            out.append(row)
    return out


def _mtime_iso(p: Path) -> str:
    return datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


def _atomic_write_text(path: Path, text: str) -> None:
    """原子写文本：先写同目录临时文件，再 os.replace。

    mkstemp 创建的临时文件权限固定为 0600，os.replace 会沿用该权限——若不在替换前
    恢复原文件 mode，每次保存都会把 config/strategy.yaml 从 644 悄悄改成 600。
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
    """构造保留注释/引号的 round-trip YAML 实例（排版与 strategy.yaml 一致）。"""
    yml = YAML()
    yml.preserve_quotes = True
    yml.width = 4096
    yml.indent(mapping=2, sequence=4, offset=2)
    return yml


def _float_style_renders_canonical(node, v) -> bool:
    """旧节点的字面格式能否按 **canonical 字面**（str(v)）精确表达新值。

    represent_scalar_float 用节点上的 _width/_prec/_exp（加载期捕获的**当前文件
    字面量**格式）渲染——
    - 把 1.1 的格式套到 1.15 上会渲染成 "1.1"（静默改值，D1 测试抓到的真 bug）；
    - 把 1.15 的格式套回 1.1 上会渲染成 "1.10"（值对但字面风格漂移，改→恢复
      不再逐字节还原）。
    两种都不沿用旧格式，退回裸值（canonical 渲染）。
    """
    import io

    from ruamel.yaml import YAML

    probe = YAML()
    buf = io.StringIO()
    probe.dump({"v": node}, buf)
    try:
        rendered = buf.getvalue().split(":", 1)[1].strip()
        return rendered == str(v)
    except Exception:  # noqa: BLE001 - 渲染异常视同不可精确表达
        return False


def _new_scalar_like(old, v):
    """新值沿用旧节点的字面风格（ruamel 表示层保真），但仅限格式可 canonical 表达新值。"""
    from ruamel.yaml.scalarfloat import ScalarFloat
    from ruamel.yaml.scalarint import ScalarInt

    if isinstance(old, ScalarFloat) and isinstance(v, float) and not isinstance(v, bool):
        node = ScalarFloat(v)
        for a in ("_width", "_prec", "_m_sign", "_m_lead0", "_exp", "_e_width", "_e_sign"):
            setattr(node, a, getattr(old, a, None))
        if _float_style_renders_canonical(node, v):
            return node
        return v  # 旧格式装不下/会漂移新值 → canonical 渲染（宁可丢风格不可改值/漂字面）
    if isinstance(old, ScalarInt) and isinstance(v, int) and not isinstance(v, bool):
        node = ScalarInt(v)
        setattr(node, "_width", getattr(old, "_width", None))
        # 十进制整数的 _width 只影响 0o/0x 前缀分支（独立 representer），
        # 普通整数按 str(value) 渲染，宽度不足也截不掉位 → 安全沿用
        return node
    return v


def _apply_payload(old_map, new_dict) -> None:
    """把 payload 递归应用到 ruamel round-trip 节点上（叶子级最小替换）。

    只替换**真正变化**的叶子值；未变化的键保留原节点（no-op 保存逐字节还原）。
    """
    from ruamel.yaml.comments import CommentedMap

    for k, v in new_dict.items():
        old = old_map.get(k)
        if isinstance(v, dict):
            if isinstance(old, CommentedMap):
                _apply_payload(old, v)
            else:
                old_map[k] = v
            continue
        if (
            old is not None
            and old == v
            and not isinstance(old, bool)
            and not isinstance(v, bool)
        ):
            continue
        old_map[k] = _new_scalar_like(old, v)


def _write_strategy_preserving_comments(payload: Dict[str, Any]) -> None:
    """把校验过的 payload 写回 config/strategy.yaml，保留原文件注释与排版。

    步骤：ruamel round-trip 读入原文件 → 按字段覆盖为 payload 值 → dump。
    自检：round-trip 原文件必须能逐字节还原，且 dump 结果语义等于 payload；
    任一不满足则退回 safe_dump（丢注释但数据正确）。
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
        probe_buf = io.StringIO()
        yml.dump(yml.load(orig), probe_buf)
        if probe_buf.getvalue() != orig:
            text = _safe_dump_text()
        else:
            _apply_payload(data, payload)
            buf = io.StringIO()
            yml.dump(data, buf)
            text = buf.getvalue()
            if yaml.safe_load(text) != payload:
                text = _safe_dump_text()
    except Exception:  # noqa: BLE001 - round-trip 失败时退回语义正确但丢注释的 dump
        text = _safe_dump_text()
    _atomic_write_text(CONFIG_PATH, text)


# ---------------------------------------------------------------------------
# 策略校验（PUT /api/strategy）—— v7 slim schema
# ---------------------------------------------------------------------------
# 字段级 schema：{section: {field: (type, min/enum, max)}}
# type ∈ {"int","float","str","bool","enum","enum2","code_list","str_list"}
_STRATEGY_SCHEMA: Dict[str, Dict[str, tuple]] = {
    "strategy": {
        "name": ("enum", "drawdown_martingale", None),
        "initial_amount": ("float", 1, 1e9),
        "base_add_amount": ("float", 1, 1e9),
        "first_drawdown_pct": ("float", 1, 90),
        "add_step_pct": ("float", 0.1, 50),
        "growth_ratio": ("float", 1.0, 10),
        "rearm_mode": ("enum2", "bounce|newhigh", None),
        "rearm_pct": ("float", 1, 100),
        "accounting": ("enum", "cash", None),   # 固定 cash（只读；传其他值 → 400）
    },
    "universe": {
        "industry_whitelist_csric2": ("code_list", None, None),
        "soe_keywords": ("str_list", None, None),
        "min_total_mv_yi": ("float", 1, 1e5),
        "min_consecutive_div_years": ("int", 1, 20),
        "listing_min_trading_days": ("int", 20, 1200),
        # v7.0.12 候选池选股门槛
        "backtest_positive": ("bool", None, None),
        "backtest_window_years": ("int", 1, 30),
        "min_ttm_yield_pct": ("float", 0, 100),
        "price_min_yuan": ("float", 0, 1e4),
        "price_max_yuan": ("float", 0, 1e5),
    },
    # v7.0.13 选股时机（**可选段**：策略库旧快照无此段，PUT 缺省不报错）
    "timing": {
        "price_window_years": ("int", 1, 30),
        "price_pctile_max": ("float", 1, 100),
        "yield_min_pct": ("float", 0, 30),
        "below_ma250": ("bool", None, None),
        "ma_period": ("int", 20, 1200),
    },
}

# 所有必须出现的 (section, field) —— 「所有 key 必须齐全」。
# timing 段**不在必选清单**（可缺省 → martingale.config 默认值）；段存在时 5 键须齐全。
_REQUIRED_KEYS = [
    (s, f) for s, fields in _STRATEGY_SCHEMA.items()
    if s in ("strategy", "universe") for f in fields
]
# 可缺省的配置段（段缺失合法，段存在时字段仍须齐全 + 过类型/范围校验）。
_OPTIONAL_SECTIONS = frozenset({"timing"})


def _validate_strategy(cfg: Dict[str, Any]) -> List[str]:
    """返回错误列表；空列表 = 合法。"""
    errors: List[str] = []

    if not isinstance(cfg, dict):
        return ["strategy 顶层必须是 JSON 对象(mapping)"]

    # 1) key 齐全性（timing 段可选：策略库旧快照/老 yaml 无此段 → 用默认值，不报错）
    for section, fields in _STRATEGY_SCHEMA.items():
        if section not in cfg or not isinstance(cfg[section], dict):
            if section not in _OPTIONAL_SECTIONS:
                errors.append(f"缺少配置段: {section}")
            continue
        for field in fields:
            if field not in cfg[section]:
                errors.append(f"缺少配置项: {section}.{field}")

    # 2) 逐项类型 + 范围
    for section, fields in _STRATEGY_SCHEMA.items():
        sec = cfg.get(section)
        if not isinstance(sec, dict):
            continue
        for field, (typ, lo, hi) in fields.items():
            if field not in sec:
                continue
            v = sec[field]
            if typ == "int":
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
            elif typ == "str":
                if not isinstance(v, str) or not v.strip():
                    errors.append(f"{section}.{field} 必须是非空字符串")
            elif typ == "bool":
                if not isinstance(v, bool):
                    errors.append(f"{section}.{field} 必须是布尔（true/false）")
            elif typ == "enum":
                if v != lo:
                    errors.append(f"{section}.{field} 目前只支持 {lo!r}")
            elif typ == "enum2":
                allowed = str(lo).split("|")
                if v not in allowed:
                    errors.append(f"{section}.{field} 只能是 {'/'.join(allowed)}")
            elif typ == "code_list":
                if not isinstance(v, list) or not v:
                    errors.append(f"{section}.{field} 必须是非空字符串数组")
                    continue
                for p in v:
                    if not isinstance(p, str) or not re.fullmatch(r"[A-Z]\d{2}", p):
                        errors.append(f"{section}.{field} 项非法: {p!r}（应形如 B06）")
            elif typ == "str_list":
                if not isinstance(v, list) or not v:
                    errors.append(f"{section}.{field} 必须是非空字符串数组")
                    continue
                for p in v:
                    if not isinstance(p, str) or not p.strip():
                        errors.append(f"{section}.{field} 项非法: {p!r}（应是非空字符串）")

    # 3) 跨字段语义
    st = cfg.get("strategy", {})
    if all(k in st for k in ("first_drawdown_pct", "add_step_pct")):
        try:
            if float(st["first_drawdown_pct"]) + float(st["add_step_pct"]) >= 100:
                errors.append("strategy.first_drawdown_pct + add_step_pct 必须 < 100")
        except (TypeError, ValueError):
            pass
    un = cfg.get("universe", {})
    if isinstance(un, dict) and all(k in un for k in ("price_min_yuan", "price_max_yuan")):
        try:
            if float(un["price_min_yuan"]) > float(un["price_max_yuan"]):
                errors.append("universe.price_min_yuan 不能大于 price_max_yuan")
        except (TypeError, ValueError):
            pass

    return errors


# ---------------------------------------------------------------------------
# 回测任务管理（子进程；cmd + 完成标记参数化）
# ---------------------------------------------------------------------------
class TaskManager:
    """跟踪后台回测子进程（409 单任务 + pidfile 锁）。

    - 内存里保留 Popen，便于拿 returncode；
    - 另写 pidfile（logs/.web_bt_run.lock），服务重启后仍能判断「是否已有运行」。
    """

    def __init__(self) -> None:
        self.tasks: Dict[str, Dict[str, Any]] = {}
        self._lock_path = LOGS_DIR / ".web_bt_run.lock"

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
            if p is None and self._pid_alive(t["pid"]):
                return tid
        cur = self._read_lock()
        if cur and self._pid_alive(cur[1]):
            return cur[0]
        return None

    def start(self, cmd: List[str], meta: Dict[str, Any],
              done_markers: List[str], task_id: Optional[str] = None) -> Dict[str, Any]:
        """启动一个后台任务（cmd = 完整 argv；meta 存任务上下文）。

        :param task_id: 调用方可预生成（Web 回测需要 task_id 拼产物目录）；
            缺省则自动生成。
        """
        existing = self.active_task_id()
        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"已有回测任务在进行中: {existing}",
            )

        task_id = task_id or (
            "bt_" + datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:4]
        )
        log_path = LOGS_DIR / f"web_bt_{task_id}.log"
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
                start_new_session=True,
            )
        except Exception as exc:  # noqa: BLE001
            logf.close()
            raise HTTPException(status_code=500, detail=f"启动子进程失败: {exc}")

        self.tasks[task_id] = {
            "pid": proc.pid,
            "popen": proc,
            "meta": meta,
            "done_markers": done_markers,
            "log_path": str(log_path),
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        self._write_lock(task_id, proc.pid)
        return {
            "task_id": task_id,
            "status": "running",
            "pid": proc.pid,
            "log_path": str(log_path),
        }

    def status(self, task_id: str) -> Dict[str, Any]:
        t = self.tasks.get(task_id)
        # 重启后内存里没有，但 pidfile/日志还在 → 从文件系统重建最小信息
        if t is None:
            log_path = LOGS_DIR / f"web_bt_{task_id}.log"
            if not log_path.exists():
                raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
            cur = self._read_lock()
            pid = cur[1] if (cur and cur[0] == task_id) else None
            t = {"pid": pid, "popen": None, "log_path": str(log_path), "meta": {}}

        log_path = Path(t["log_path"])
        tail = _tail_lines(log_path, 20) if log_path.exists() else []
        markers: List[str] = t.get("done_markers") or ["回测完成"]

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
                state = _infer_terminal_state(tail, markers)
        else:
            state = _infer_terminal_state(tail, markers)

        out: Dict[str, Any] = {
            "task_id": task_id,
            "status": state,
            "meta": t.get("meta") or {},
            "log_tail": tail,
        }
        if state in ("done", "failed"):
            out["results_api"] = f"/api/bt/results/{task_id}" if state == "done" else None
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


def _infer_terminal_state(tail: List[str], done_markers: List[str]) -> str:
    """无 Popen 时（服务重启后）依据日志尾部推断 done/failed。

    日志尾部 8KB 恒含末行 → 正常结束的任务必有完成标记；否则按 failed 兜底
    （前端显示失败 + 日志尾部，用户可查因——宁严勿松）。
    """
    joined = "\n".join(tail)
    if any(m in joined for m in done_markers):
        return "done"
    return "failed"


# ---------------------------------------------------------------------------
# Pydantic 模型
# ---------------------------------------------------------------------------
class BtRunRequest(BaseModel):
    """POST /api/bt/runs：{start?, end?, codes? | pool, strategy?}。

    - start/end: YYYY-MM-DD（缺省 = 各股数据起点/终点）；
    - codes: 逗号分隔字符串或数组（与 pool 互斥，都不给 = 股票池）；
    - pool: true 显式股票池（与 codes 同时给 → 400）；
    - strategy: 策略库名（data/strategies/<name>.yaml，策略页"另存为"的快照）；
      缺省/"current" = 当前活动策略 config/strategy.yaml；不存在 → 400。
    """
    start: Optional[str] = None
    end: Optional[str] = None
    codes: Optional[Any] = None   # str 或 list[str]
    pool: Optional[bool] = None
    strategy: Optional[str] = None


class ImportStrategyRequest(BaseModel):
    """POST /api/strategy/import 请求体 {raw: <yaml 文本>}。"""
    raw: str


class SaveStrategyRequest(BaseModel):
    """POST /api/strategies 请求体 {name}（可缺省 → 默认名带时间戳）。"""
    name: Optional[str] = None


# ---------------------------------------------------------------------------
# API 路由
# ---------------------------------------------------------------------------
tm = TaskManager()

_CODE_RE = re.compile(r"^(?:sh|sz)\.\d{6}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _validate_bt_dates(start: Optional[str], end: Optional[str]) -> None:
    for label, v in (("start", start), ("end", end)):
        if v is None:
            continue
        if not _DATE_RE.fullmatch(str(v)):
            raise HTTPException(status_code=400, detail=f"{label} 应为 YYYY-MM-DD（收到 {v!r}）")
        try:
            datetime.strptime(str(v), "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail=f"非法日期: {label}={v!r}")


def _normalize_codes(codes: Any) -> List[str]:
    if codes is None:
        return []
    if isinstance(codes, str):
        items = [c.strip() for c in codes.split(",")]
    elif isinstance(codes, list):
        items = [str(c).strip() for c in codes]
    else:
        raise HTTPException(status_code=400, detail="codes 应为逗号分隔字符串或数组")
    items = [c for c in items if c]
    if not items:
        return []
    for c in items:
        if not _CODE_RE.fullmatch(c):
            raise HTTPException(status_code=400, detail=f"非法 ts_code: {c!r}（应形如 sh.601088）")
    # 去重保序
    return list(dict.fromkeys(items))


# ---------------------------------------------------------------------------
# 回测任务（/api/bt/runs 族）
# ---------------------------------------------------------------------------
@app.post("/api/bt/runs")
def trigger_bt_run(req: BtRunRequest) -> Dict[str, Any]:
    """触发一次按需回测（子进程 python -m martingale backtest，SSE 推进度）。

    strategy 语义（策略选择）：缺省/"current" = 活动策略 config/strategy.yaml；
    策略库名 → 解析 data/strategies/<name>.yaml 并以 --config 传给引擎（不存在 → 400）。
    """
    _validate_bt_dates(req.start, req.end)
    codes = _normalize_codes(req.codes)
    if codes and req.pool:
        raise HTTPException(status_code=400, detail="codes 与 pool 互斥（codes 优先，请二选一）")

    strategy = (req.strategy or "").strip()
    cfg_arg: Optional[str] = None
    if strategy and strategy != "current":
        cfg_path = _strategy_path(_sanitize_strategy_name(strategy))
        if not cfg_path.is_file():
            raise HTTPException(status_code=400, detail={"error": "strategy_not_found", "name": strategy})
        cfg_arg = str(cfg_path)

    cmd = [str(VENV_PYTHON), "-m", "martingale", "backtest"]
    if req.start:
        cmd += ["--start", req.start]
    if req.end:
        cmd += ["--end", req.end]
    if codes:
        cmd += ["--codes", ",".join(codes)]
    if cfg_arg:
        cmd += ["--config", cfg_arg]
    # 无 --codes = 默认股票池（pool=true 显式同义）
    if not VENV_PYTHON.exists():
        raise HTTPException(status_code=500, detail=f"找不到 venv python: {VENV_PYTHON}")

    # task_id 预生成（产物目录 output/backtest_martingale/runs/<task_id> 与之一致）
    task_id = "bt_" + datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:4]
    cmd += ["--out", str(BT_RUNS_DIR / task_id)]
    res = tm.start(cmd, {"start": req.start, "end": req.end,
                         "codes": codes or None, "pool": bool(req.pool) or not codes,
                         "strategy": strategy or "current"},
                   ["回测完成"], task_id=task_id)
    res["out_dir"] = str(BT_RUNS_DIR / task_id)
    return res


@app.get("/api/bt/runs/{task_id}/status")
def bt_run_status(task_id: str) -> Dict[str, Any]:
    if not re.fullmatch(r"bt_[A-Za-z0-9_]+", task_id):
        raise HTTPException(status_code=400, detail="非法 task_id")
    return tm.status(task_id)


@app.get("/api/bt/results/{task_id}")
def bt_results(task_id: str) -> Dict[str, Any]:
    """回测产物 → JSON（per_stock / buys / equity_curve / summary / status）。"""
    if not re.fullmatch(r"bt_[A-Za-z0-9_]+", task_id):
        raise HTTPException(status_code=400, detail="非法 task_id")
    out_dir = BT_RUNS_DIR / task_id
    if not out_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"回测产物不存在: {task_id}")
    res: Dict[str, Any] = {"task_id": task_id}
    ps = out_dir / "per_stock.csv"
    res["per_stock"] = _read_csv_rows_floats(ps) if ps.exists() else []
    buys = out_dir / "buys.csv"
    res["buys"] = _read_csv_rows_floats(buys) if buys.exists() else []
    eq = out_dir / "equity_curve.csv"
    if eq.exists():
        rows = _read_csv_rows_floats(eq)
        res["equity_curve"] = rows[-5000:]   # 截尾（前端画曲线够用；完整 CSV 在盘上）
        res["equity_curve_truncated"] = len(rows) > 5000
    else:
        res["equity_curve"] = []
    sm = out_dir / "summary.md"
    res["summary"] = sm.read_text(encoding="utf-8") if sm.exists() else ""
    sj = out_dir / "status.json"
    if sj.exists():
        try:
            res["run_meta"] = json.loads(sj.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            res["run_meta"] = None
    return res


# ---------------------------------------------------------------------------
# SSE 实时日志（回测任务）
# ---------------------------------------------------------------------------
def _classify_log_line(line: str, done_markers: List[str]) -> Dict[str, Any]:
    """把一行运行日志分类为 SSE 事件。

    - [PROGRESS] stage=... done=N total=M → progress（结构化进度标记）
    - 完成标记（"回测完成"）→ done；"错误:"/Traceback → error；其余 → log。
    """
    m = re.search(r"\[PROGRESS\]\s+stage=(\S+)\s+done=(\d+)\s+total=(\d+)", line)
    if m:
        return {"type": "progress", "stage": m.group(1),
                "done": int(m.group(2)), "total": int(m.group(3))}
    if any(mk in line for mk in done_markers):
        return {"type": "done"}
    if "Traceback" in line or "错误:" in line or re.search(r"\bError\b", line):
        return {"type": "error"}
    return {"type": "log", "text": line}


@app.get("/api/bt/runs/{task_id}/events")
async def bt_run_events(task_id: str, request: Request) -> StreamingResponse:
    """SSE 事件流：log / progress / done / error + ~15s 心跳。

    - `id: <offset>` = 已推送到该字节偏移；断线重连带 Last-Event-ID 从该偏移续读。
    - 子进程结束且日志读完 → 发送终态事件后关闭流（前端据此停止轮询兜底）。
    """
    if not re.fullmatch(r"bt_[A-Za-z0-9_]+", task_id):
        raise HTTPException(status_code=400, detail="非法 task_id")
    log_path = LOGS_DIR / f"web_bt_{task_id}.log"
    if not log_path.exists():
        raise HTTPException(status_code=404, detail=f"任务日志不存在: {task_id}")

    task = tm.tasks.get(task_id)
    markers = (task or {}).get("done_markers") or ["回测完成"]

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
        while True:
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
                        continue
                    ev = _classify_log_line(line, markers)
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
# 持仓状态 / 股票池（同步；全池 ~2s，调研实测 CONFIRMED）
# ---------------------------------------------------------------------------
def _lake_or_raise() -> None:
    """数据湖不可用（缺失/损坏/锁）→ 503 带归一化中文原因。"""
    try:
        from martingale import data as mdata
        con = mdata.connect_readonly(None)
        con.close()
    except Exception as exc:  # noqa: BLE001 - 归一化异常（LakeMissing/Locked/Invalid）
        raise HTTPException(status_code=503, detail=f"数据湖不可用: {exc}")


def _load_pool_ctx(cfg_path: Optional[str] = None):
    """配置 + 连接 + 候选池（/api/positions 与 /api/pool 共用）。

    cfg_path 缺省 = 活动策略 config/strategy.yaml；策略库快照路径由调用方解析后传入。
    v7.0.12：build_pool（基础 5 条件）→ build_candidate_pool（∩ 十年回测为正
    ∩ 股息率 ∩ 价格区间 ∪ 手动添加；G7 读预计算表 O(1)，请求路径零回测）。
    """
    from martingale.config import load_config as mload
    from martingale.data import connect_readonly
    from martingale.selection import build_candidate_pool, load_manual_pool

    try:
        sp, up, _tp = mload(cfg_path or str(CONFIG_PATH))
    except Exception as exc:  # noqa: BLE001 - ConfigError → 400
        raise HTTPException(status_code=400, detail=f"策略配置错误: {exc}")
    con = connect_readonly(None)
    try:
        manual = load_manual_pool()
        pool = build_candidate_pool(con, up, manual=manual)
    except Exception as exc:  # noqa: BLE001
        con.close()
        raise HTTPException(status_code=500, detail=f"候选池构建失败: {exc}")
    return sp, up, con, pool


@app.get("/api/pool")
def get_pool() -> Dict[str, Any]:
    """候选池：漏斗计数 + 成员（含十年回测/股息率/价格/手动标记；同步 ~1s）。

    候选池 ≠ 买入信号：入选只代表进入策略候选；买不买看价格是否到策略触发线
    （「我的持仓」页信号 / 微信提醒，同口径）。
    """
    _sp, _up, con, pool = _load_pool_ctx()
    try:
        members = [
            {
                "ts_code": m.ts_code,
                "name": m.name,
                "industry_csric2": m.industry_csric2,
                "soe_basis": m.soe_basis,
                "total_mv": m.total_mv,
                "ttm_yield_pct": m.ttm_yield_pct,
                "n_bars": m.n_bars,
                "list_date": m.list_date,
                "bt_pnl_pct": (round(m.bt_pnl_pct * 100.0, 2)
                               if m.bt_pnl_pct is not None else None),
                "bt_n_buys": m.bt_n_buys,
                "price_latest": m.price_latest,
                "is_manual": m.is_manual,
            }
            for m in pool.members
        ]
    finally:
        con.close()
    return {
        "funnel": pool.funnel,
        "n_members": len(members),
        "members": members,
    }


# ---------------------------------------------------------------------------
# 候选池手动添加（GET/POST/DELETE /api/pool/manual → data/manual_pool.json）
# ---------------------------------------------------------------------------
@app.get("/api/pool/manual")
def get_pool_manual() -> Dict[str, Any]:
    """读手动池（策略页签股票池卡片的手动录入条用；零湖访问，只读 json）。"""
    from martingale.selection import load_manual_pool
    stocks = load_manual_pool()
    return {
        "n_manual": len(stocks),
        "stocks": {
            code: {"name": e.get("name", ""), "added_at": e.get("added_at", "")}
            for code, e in sorted(stocks.items())
        },
    }


@app.post("/api/pool/manual")
def add_pool_manual(payload: Dict[str, Any]) -> Dict[str, Any]:
    """手动加入候选池（绕过回测/股息率/价格门槛；存在且未退市才允许）。"""
    from martingale.data import connect_readonly
    from martingale.selection import add_manual_code

    con = connect_readonly(None)
    try:
        try:
            entry = add_manual_code(con, str(payload.get("ts_code", "")))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    finally:
        con.close()
    return {"ok": True, "entry": entry}


@app.delete("/api/pool/manual")
def remove_pool_manual(payload: Dict[str, Any]) -> Dict[str, Any]:
    """手动移出候选池（幂等：不在池 → ok=False）。"""
    from martingale.selection import normalize_ts_code, remove_manual_code

    try:
        code = normalize_ts_code(str(payload.get("ts_code", "")))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    removed = remove_manual_code(code)
    return {"ok": removed, "ts_code": code}


# ---------------------------------------------------------------------------
# 选股时机（v7.0.13）：GET /api/timing → 今日选股（观察口径 OR）+ 提醒口径标记
# 读湖表 selection_timing（预计算 O(1)）；请求路径零计算、零网络。
# ---------------------------------------------------------------------------
@app.get("/api/timing")
def get_timing() -> Dict[str, Any]:
    """今日选股：候选池内已启用时机条件的 OR 命中（in_watchlist）。

    - push_eligible = ①且②（③启用时叠加）——微信"新进入买入区"提醒口径；
    - 湖表缺失（未跑过 timing 预计算）→ 空列表 + as_of=null（前端提示等待
      每日湖更新，fail-closed 不误导）；湖持锁/缺失 → 503（灌数中）。
    """
    from martingale.config import load_config as mload
    from martingale.data import connect_readonly
    from martingale.timing import load_timing_map

    try:
        _sp, _up, tp = mload(str(CONFIG_PATH))
    except Exception as exc:  # noqa: BLE001 - ConfigError → 400
        raise HTTPException(status_code=400, detail=f"策略配置错误: {exc}")
    try:
        con = connect_readonly(None)
        try:
            tmap = load_timing_map(con)
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001 - 湖锁定/缺失 → 503（灌数持锁中）
        raise HTTPException(status_code=503, detail=f"数据湖不可用: {exc}")

    watch = [r for r in tmap.values() if r.get("in_watchlist")]
    # 排序：会提醒的（push_eligible）优先 → 股息率降序 → 代码
    watch.sort(key=lambda r: (not r.get("push_eligible"),
                              -(r.get("ttm_yield_pct") or 0.0), r["ts_code"]))
    as_of = next((str(r["as_of_date"]) for r in tmap.values()), None)
    return {
        "as_of": as_of,
        "n_pool": len(tmap),
        "n_watchlist": len(watch),
        "n_push": sum(1 for r in tmap.values() if r.get("push_eligible")),
        "params": {
            "price_window_years": tp.price_window_years,
            "price_pctile_max": tp.price_pctile_max,
            "yield_min_pct": tp.yield_min_pct,
            "below_ma250": tp.below_ma250,
            "ma_period": tp.ma_period,
        },
        "rows": watch,
    }


def _strategy_state_rows(sp, con, codes, end=None) -> Dict[str, Dict[str, Any]]:
    """对 codes 逐股跑状态机 → {code: 持仓行}（/api/positions 与 /api/my_positions 共用）。

    end=YYYY-MM-DD 截窗口（含）；缺省跑到各股最新 bar。湖中无数据的代码不产出行。
    """
    from martingale.engine import load, run_stock

    names, data, divs = load(con, codes=list(codes), start=None, end=end)
    rows: Dict[str, Dict[str, Any]] = {}
    for c in codes:
        series = data.get(c)
        if not series or len(series[0]) < 2:
            continue
        dates, sig, exec_c = series
        _stat, state, _v, _c, _f, _buys = run_stock(
            c, dates, sig, exec_c, divs.get(c), sp, verbose=False)
        rows[c] = {
            "ts_code": c,
            "name": names.get(c, ""),
            "in_pos": bool(state["in_pos"]),
            "shares": round(float(state["shares"]), 2),
            "avg_cost": round(float(state["avg_cost"]), 4),
            "last_price": float(state["last_price"]),
            "market_value": round(float(state["market_value"]), 2),
            "cash_div": round(float(state["cash_div"]), 2),
            "unreal_pct": round(float(state["unreal_pct"]) * 100.0, 2),
            "k_level": int(state["k_level"]),
            "entry_high": float(state["entry_high"]),
            "low_since_high": float(state["low_since_high"]),
            # 触发价原始口径（_raw）：引擎的 next_add/next_rearm 基于 hfq 信号价，
            # 而 last_price 是原始收盘——直接比较会因复权因子（分红股 F 可达数十倍）
            # 产生假信号。raw = hfq / F，F = sig[-1]/exec_c[-1]（最新 bar 复权因子）。
            "next_add_price": round(float(state["next_add_price"]), 4)
                if state["next_add_price"] is not None else None,
            "next_rearm_price": round(float(state["next_rearm_price"]), 4)
                if state["next_rearm_price"] is not None else None,
            "end_date": str(dates[-1])[:10],
        }
        f = float(sig[-1]) / float(exec_c[-1]) if float(exec_c[-1]) > 0 else 1.0
        if rows[c]["next_add_price"] is not None:
            rows[c]["next_add_price_raw"] = round(rows[c]["next_add_price"] / f, 4)
        else:
            rows[c]["next_add_price_raw"] = None
        if rows[c]["next_rearm_price"] is not None:
            rows[c]["next_rearm_price_raw"] = round(rows[c]["next_rearm_price"] / f, 4)
        else:
            rows[c]["next_rearm_price_raw"] = None
    return rows


@app.get("/api/positions")
def get_positions(as_of: Optional[str] = None) -> Dict[str, Any]:
    """当前持仓状态（逐股状态机投影；同步，全池 ~2s）。

    as_of=YYYY-MM-DD：把窗口截到该日（含）——默认各股数据起点→最新 bar。
    每只股独立状态机从**该股数据起点**（或 --start 语义的数据起点）跑到 as_of。
    """
    if as_of is not None and not _DATE_RE.fullmatch(str(as_of)):
        raise HTTPException(status_code=400, detail="as_of 应为 YYYY-MM-DD")
    _sp, _up, con, pool = _load_pool_ctx()
    t0 = time.time()
    try:
        rows = _strategy_state_rows(_sp, con, pool.codes, end=as_of)
    finally:
        con.close()
    positions = [rows[c] for c in sorted(rows)]
    latest = max((r["end_date"] for r in positions), default=None)
    return {
        "as_of": as_of or latest,
        "n": len(positions),
        "elapsed_s": round(time.time() - t0, 2),
        "positions": positions,
    }


# ---------------------------------------------------------------------------
# 我的持仓（用户实际持仓账本 + 策略状态机参考）
# ---------------------------------------------------------------------------
def _up_read() -> Dict[str, Any]:
    """读用户持仓账本 → {code: {shares, cost_price, updated_at}}；缺省/损坏 → {}。"""
    if not USER_POSITIONS_PATH.exists():
        return {}
    try:
        d = json.loads(USER_POSITIONS_PATH.read_text(encoding="utf-8"))
        pos = d.get("positions", {})
        return pos if isinstance(pos, dict) else {}
    except Exception:  # noqa: BLE001 - 账本损坏不 500，按空账本 + 提示
        return {}


def _up_write(positions: Dict[str, Any]) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    USER_POSITIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"updated_at": now, "positions": positions}
    tmp = USER_POSITIONS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, USER_POSITIONS_PATH)  # 原子替换，防半截账本


@app.get("/api/my_positions")
def get_my_positions(strategy: Optional[str] = None) -> Dict[str, Any]:
    """我的持仓 = 用户账本（股数/成本）× 湖最新价 + 策略状态机参考（档/触发价）+ 加仓信号。

    - 只列用户账本里的代码；策略参考对池外股同样有效（状态机不挑池）。
    - 空账本 → 不连湖直接返回（灌数持锁期间页面仍可用）。
    - strategy：策略选择（同 /api/bt/runs 语义）——缺省/"current" = 活动策略
      config/strategy.yaml；策略库名 → data/strategies/<name>.yaml；不存在 → 400。
    - signals：买卖提示——现价已跌破所选策略的下一触发价位：
      持仓中（in_pos，第 k 档）→ action="add"（加仓一档，base×growth^k）；
      未持仓（never，k=0）→ action="buy"（首仓触发，initial_amount）。
      现价 = 腾讯实时行情（price_source="spot"）；行情失败回退湖最后价
      （price_source="lake"，只漏报不误报）。状态机「触线即买」→ 湖最后 bar 处
      湖价恒高于触发价，故必须用实时价判定。
    """
    user = _up_read()
    strategy_name = (strategy or "current").strip() or "current"
    cfg_path: Optional[str] = None
    if strategy_name != "current":
        cfg_file = _strategy_path(_sanitize_strategy_name(strategy_name))
        if not cfg_file.is_file():
            raise HTTPException(status_code=400, detail={"error": "strategy_not_found", "name": strategy_name})
        cfg_path = str(cfg_file)
    if not user:
        return {"as_of": None, "n": 0, "positions": [], "signals": [], "strategy": strategy_name,
                "totals": {"market_value": 0, "cost": 0, "pnl": 0, "pnl_pct": None}}
    # 湖锁定/缺失 → 降级：只列账本（无价格/无策略参考），前端显示灌数提示条
    from martingale.data import LakeInvalidFile, LakeLocked, LakeMissing

    lake_locked = False
    rows: Dict[str, Dict[str, Any]] = {}
    _sp = None
    try:
        _sp, _up, con, pool = _load_pool_ctx(cfg_path)
        try:
            rows = _strategy_state_rows(_sp, con, sorted(set(pool.codes) | set(user.keys())))
        finally:
            con.close()
    except (LakeLocked, LakeMissing, LakeInvalidFile):
        lake_locked = True
        rows = {}
    positions: List[Dict[str, Any]] = []
    tot_mv = tot_cost = 0.0
    for c in sorted(user):
        u = user[c] if isinstance(user[c], dict) else {}
        s = rows.get(c, {})
        shares = float(u.get("shares") or 0)
        cost = float(u.get("cost_price") or 0)
        last = s.get("last_price")
        mv = shares * last if last is not None else None
        cost_total = shares * cost
        pnl = (mv - cost_total) if mv is not None else None
        if mv is not None:
            tot_mv += mv
        tot_cost += cost_total
        positions.append({
            "ts_code": c,
            "name": s.get("name") or c,
            "shares": shares,
            "cost_price": cost,
            "last_price": last,
            "market_value": round(mv, 2) if mv is not None else None,
            "pnl": round(pnl, 2) if pnl is not None else None,
            "pnl_pct": round(pnl / cost_total * 100.0, 2) if (pnl is not None and cost_total > 0) else None,
            "updated_at": u.get("updated_at"),
            # 策略状态机参考（池外股/湖中无数据时字段为 None）
            "in_pos": s.get("in_pos"),
            "strat_shares": s.get("shares"),
            "k_level": s.get("k_level"),
            "next_add_price": s.get("next_add_price"),
            "next_rearm_price": s.get("next_rearm_price"),
            "next_add_price_raw": s.get("next_add_price_raw"),
            "next_rearm_price_raw": s.get("next_rearm_price_raw"),
            "end_date": s.get("end_date"),
        })
    has_price = any(r["market_value"] is not None for r in positions)
    # 加仓信号（实时价口径）：
    # 状态机在湖最后 bar「触线即买」→ 湖最后 bar 处 最新价 > 下一档触发价 恒成立，
    # 用湖价判"已触线"永远为假。故信号用**实时价**（腾讯行情）对比触发价；
    # 实时行情失败 → 回退湖最后价（price_source="lake"，此时信号只会漏报不会误报）。
    spot: Dict[str, Dict[str, Any]] = {}
    try:
        from screener.data.tencent import TencentClient
        spot = TencentClient(timeout=5.0, max_attempts=1).fetch(
            [p["ts_code"] for p in positions]) or {}
    except Exception:  # noqa: BLE001 - 行情失败不阻塞页面
        spot = {}
    price_source = "spot" if any(v.get("price") is not None for v in spot.values()) else "lake"
    spot_time = next((v.get("timestamp") for v in spot.values() if v.get("timestamp")), None)
    for pos in positions:
        p_now = (spot.get(pos["ts_code"]) or {}).get("price")
        pos["spot_price"] = p_now
    # 信号：现价（实时优先）≤ 下一档触发价（原始口径）
    # - in_pos（持仓中，第 k 档）→ "add"：加仓到第 k+1 档，金额 = base×growth^k
    # - 未持仓（last_status=never，k=0）→ "buy"：首仓买入触发，金额 = initial_amount
    # （状态机「触线即买」→ 湖最后 bar 处湖价恒高于触发价，必须实时价判定）
    # v7.0.11：口径抽至 martingale.signals.build_signals（微信点位提醒脚本共用，
    # 单一定义点防漂移）
    from martingale.signals import build_signals

    signals = build_signals(positions, _sp)
    return {
        "as_of": max((r["end_date"] for r in positions if r["end_date"]), default=None),
        "lake_locked": lake_locked,
        "n": len(positions),
        "positions": positions,
        "signals": signals,
        "strategy": strategy_name,
        "price_source": price_source,
        "spot_time": spot_time,
        "totals": {
            "market_value": round(tot_mv, 2) if has_price else None,
            "cost": round(tot_cost, 2),
            "pnl": round(tot_mv - tot_cost, 2) if has_price else None,
            "pnl_pct": round((tot_mv - tot_cost) / tot_cost * 100.0, 2) if (has_price and tot_cost > 0) else None,
        },
    }


@app.put("/api/my_positions")
def put_my_positions(payload: Dict[str, Any]) -> Dict[str, Any]:
    """upsert 单只持仓：shares=0 为删除。"""
    code = str(payload.get("ts_code", "")).strip()
    if not _TS_CODE_RE.fullmatch(code):
        raise HTTPException(status_code=400, detail="ts_code 应为 sh.600000 / sz.000001 形式")
    shares = payload.get("shares")
    cost = payload.get("cost_price")
    try:
        shares_f = float(shares) if shares is not None else -1.0
        cost_f = float(cost) if cost is not None else 0.0
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="shares/cost_price 应为数字")
    if shares_f < 0 or cost_f < 0:
        raise HTTPException(status_code=400, detail="shares/cost_price 不能为负")
    positions = _up_read()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if shares_f == 0:
        positions.pop(code, None)
    else:
        positions[code] = {"shares": shares_f, "cost_price": cost_f, "updated_at": now}
    _up_write(positions)
    return {"ok": True, "ts_code": code, "n": len(positions)}


# ---------------------------------------------------------------------------
# 股票模糊搜索（/api/stocks/search）——「我的持仓」录入条候选下拉
# ---------------------------------------------------------------------------
# 数据源 = stock_master（ts_code/name/industry），进程内建拼音索引：
# - 拼音全拼（zhongguoshenhua）+ 首字母（zgsh），pypinyin 一次性算好缓存；
# - 缓存键 = (路径, mtime_ns, size)：lake-sync 更新 master → 下次查询自动重建。
_STOCK_SEARCH_CACHE: Dict[str, Any] = {"key": None, "index": None}


def _stock_search_index() -> List[Dict[str, str]]:
    """stock_master → 内存搜索索引。湖不可用 → 抛 Lake* 异常（端点归一为 503）。"""
    from pypinyin import lazy_pinyin

    import martingale.data as mdata
    p = Path(mdata.default_db_path())
    try:
        st = p.stat()
        key = (str(p), st.st_mtime_ns, st.st_size)
    except FileNotFoundError:
        from martingale.data import LakeMissing
        raise LakeMissing(str(p))
    if _STOCK_SEARCH_CACHE["key"] == key and _STOCK_SEARCH_CACHE["index"] is not None:
        return _STOCK_SEARCH_CACHE["index"]
    from martingale.data import connect_readonly
    con = connect_readonly(None)
    try:
        rows = con.execute(
            "SELECT ts_code, name, industry_name FROM stock_master "
            "WHERE delist_date IS NULL ORDER BY ts_code").fetchall()
    finally:
        con.close()
    index: List[Dict[str, str]] = []
    for ts_code, name, industry in rows:
        name = name or ""
        pys = lazy_pinyin(name) or []
        index.append({
            "ts_code": ts_code, "name": name, "industry": industry or "",
            "code_digits": ts_code.split(".", 1)[1],
            "py_full": "".join(pys).lower(),
            "py_initials": "".join((c[0] if c else "") for c in pys).lower(),
        })
    _STOCK_SEARCH_CACHE["key"] = key
    _STOCK_SEARCH_CACHE["index"] = index
    return index


_CODE_QUERY_RE = re.compile(r"^(?:sh|sz|bj)?\.?(\d+)$")


def _search_stocks(q: str) -> List[Dict[str, str]]:
    """模糊匹配：代码数字 / 中文名子串 / 拼音首字母 / 全拼子串。

    评分（小=优先）：代码精确 0 → 代码前缀 1 → 代码包含 2 → 名精确 0 → 名前缀 1
    → 名包含 2 → 首字母前缀 3 → 首字母包含 4 → 全拼前缀 5 → 全拼包含 6；
    同分按 ts_code 排序；取前 10。
    """
    q = (q or "").strip().lower()
    m = _CODE_QUERY_RE.fullmatch(q)
    digit_mode = bool(m)
    if digit_mode:
        q = m.group(1)
    out: List[Tuple[int, Dict[str, str]]] = []
    for it in _stock_search_index():
        score: Optional[int] = None
        if digit_mode:
            cd = it["code_digits"]
            if cd == q:
                score = 0
            elif cd.startswith(q):
                score = 1
            elif q in cd:
                score = 2
        else:
            name = it["name"]
            if name == q:
                score = 0
            elif name.startswith(q):
                score = 1
            elif q in name:
                score = 2
            if score is None and it["py_initials"].startswith(q):
                score = 3
            if score is None and q in it["py_initials"]:
                score = 4
            if score is None and it["py_full"].startswith(q):
                score = 5
            if score is None and q in it["py_full"]:
                score = 6
        if score is not None:
            out.append((score, it))
    out.sort(key=lambda x: (x[0], x[1]["ts_code"]))
    return [it for _, it in out[:10]]


@app.get("/api/stocks/search")
def search_stocks(q: str = "") -> Dict[str, Any]:
    """候选股票模糊搜索（录入条下拉用）。

    支持：代码数字（600036 / sh.600036，≥3 位）、中文名子串（电力）、
    拼音首字母（zgsh）、全拼子串（shenhua）。湖被灌数进程锁定 → 503 lake_locked
    （前端降级为手动输入代码）。
    """
    q = (q or "").strip()
    if not q:
        raise HTTPException(status_code=400, detail="q 为空")
    m = _CODE_QUERY_RE.fullmatch(q.lower())
    if m and len(m.group(1)) < 3:
        raise HTTPException(status_code=400, detail="纯数字代码至少 3 位")
    try:
        results = _search_stocks(q)
    except Exception as exc:  # noqa: BLE001 - 归一化为 503（锁/缺失/坏文件）
        from martingale.data import LakeInvalidFile, LakeLocked, LakeMissing
        if isinstance(exc, (LakeLocked, LakeMissing, LakeInvalidFile)):
            raise HTTPException(status_code=503, detail={"error": "lake_locked", "message": str(exc)})
        raise
    return {"q": q, "results": results}


# ---------------------------------------------------------------------------
# 策略读写（保留 v6.2 机制；schema 换 v7 slim）
# ---------------------------------------------------------------------------
@app.get("/api/strategy")
def get_strategy() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise HTTPException(status_code=404, detail="config/strategy.yaml 不存在")
    raw = CONFIG_PATH.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    # v7.0.13：旧 yaml/策略库快照无 timing 段 → 用默认值补齐（前端选股策略卡片
    # 恒渲染；下次 PUT 保存后 timing 段固化进 yaml）
    if isinstance(data, dict) and "timing" not in data:
        import dataclasses
        from martingale.config import TimingParams
        data["timing"] = dataclasses.asdict(TimingParams())
    return {"json": data, "raw": raw}


def _apply_strategy_write(payload: Any) -> Tuple[str, List[str]]:
    """PUT /api/strategy 与 POST /api/strategy/import 共用的「校验 → 备份 → 写盘」。

    1) _validate_strategy 逐项类型/范围校验（v7 slim schema）；
    2) martingale.config.load_config 最终结构/语义校验（写临时文件）；
    3) .bak 备份（保留最近一份，shutil.copy2 保留 mtime）；
    4) ruamel round-trip 保留注释与排版写回（失败退回 safe_dump）。
    返回 (backup_path_str, sections)。
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail={"errors": ["strategy 顶层必须是 JSON 对象(mapping)"]})

    errors = _validate_strategy(payload)
    if errors:
        raise HTTPException(status_code=400, detail={"errors": errors})

    # martingale.config.load_config 做最终结构/语义校验（写临时文件）
    try:
        from martingale import config as mcfg
        fd, tmp = tempfile.mkstemp(suffix=".yaml")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(payload, f, allow_unicode=True, sort_keys=False)
        try:
            mcfg.load_config(tmp)
        finally:
            os.unlink(tmp)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail={"errors": [f"配置校验失败: {exc}"]})

    # 备份（保留最近一份）
    bak_path = CONFIG_PATH.with_suffix(".yaml.bak")
    if CONFIG_PATH.exists():
        shutil.copy2(CONFIG_PATH, bak_path)

    _write_strategy_preserving_comments(payload)
    return str(bak_path), list(payload.keys())


@app.put("/api/strategy")
def put_strategy(payload: Dict[str, Any]) -> Dict[str, Any]:
    backup, _ = _apply_strategy_write(payload)
    return {"ok": True, "backup": backup}


@app.post("/api/strategy/import")
def import_strategy(req: ImportStrategyRequest) -> Dict[str, Any]:
    """加载策略文件——body={raw}（yaml 文本），复用 PUT 同一套校验。"""
    try:
        data = yaml.safe_load(req.raw)
    except Exception as exc:  # noqa: BLE001 - yaml 语法错误 → 400（不写盘）
        raise HTTPException(status_code=400, detail={"errors": [f"YAML 解析失败: {exc}"]})
    backup, sections = _apply_strategy_write(data)
    return {"ok": True, "backup": backup, "sections": sections}


# ---------------------------------------------------------------------------
# 策略库：本机命名保存 + 下拉加载 + 删除（data/strategies/*.yaml）
# ---------------------------------------------------------------------------
def _sanitize_strategy_name(name: str) -> str:
    """策略名 → 安全文件名（去路径分隔符/..；空格→下划线；保留 CJK/字母数字/-_）。"""
    s = str(name or "").strip()
    s = s.replace("/", "_").replace("\\", "_")
    s = s.replace("..", "")
    s = s.replace(" ", "_")
    out = "".join(ch for ch in s if ch.isalnum() or ch in "-_" or "\u4e00" <= ch <= "\u9fff")
    return out


def _strategy_path(name: str) -> Path:
    return STRATEGIES_DIR / f"{name}.yaml"


def _default_strategy_name() -> str:
    return "策略_" + datetime.now().strftime("%Y%m%d_%H%M")


@app.post("/api/strategies", status_code=201)
def save_strategy(req: SaveStrategyRequest) -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise HTTPException(status_code=404, detail="config/strategy.yaml 不存在")
    name = _sanitize_strategy_name(req.name) if req.name else ""
    if not name:
        name = _default_strategy_name()
    path = _strategy_path(name)
    if path.exists():
        raise HTTPException(status_code=409, detail={"error": "strategy_exists", "name": name})
    STRATEGIES_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, CONFIG_PATH.read_text(encoding="utf-8"))
    return {
        "name": name,
        "path": str(path),
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


@app.get("/api/strategies")
def list_strategies() -> List[Dict[str, Any]]:
    if not STRATEGIES_DIR.is_dir():
        return []
    out: List[Dict[str, Any]] = []
    for p in STRATEGIES_DIR.glob("*.yaml"):
        if not p.is_file():
            continue
        st = p.stat()
        out.append({
            "name": p.stem,
            "saved_at": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "size_bytes": st.st_size,
        })
    out.sort(key=lambda x: x["saved_at"], reverse=True)
    return out


@app.delete("/api/strategies/{name}", status_code=204)
def delete_strategy(name: str) -> Response:
    path = _strategy_path(_sanitize_strategy_name(name))
    if not (path.is_file()):
        raise HTTPException(status_code=404, detail={"error": "not_found", "name": name})
    path.unlink()
    return Response(status_code=204)


@app.post("/api/strategies/{name}/load")
def load_strategy(name: str) -> Dict[str, Any]:
    path = _strategy_path(_sanitize_strategy_name(name))
    if not path.is_file():
        raise HTTPException(status_code=404, detail={"error": "not_found", "name": name})
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail={"errors": [f"YAML 解析失败: {exc}"]})
    backup, _ = _apply_strategy_write(data)
    return {"ok": True, "backup": backup}


# ---------------------------------------------------------------------------
# 健康检查
# ---------------------------------------------------------------------------
@app.get("/api/health")
def health() -> Dict[str, Any]:
    return {
        "ok": True,
        "version": "7.0.0",
        "time": datetime.now(timezone.utc).isoformat(),
        "project_root": str(PROJECT_ROOT),
        "active_task": tm.active_task_id(),
    }


# ---------------------------------------------------------------------------
# v6 数据湖页签（独立分析层）：仅当 duckdb 可导入时挂载 /api/lake/* router；
# 否则注册轻量 stub。零 import 保证：martingale/* 绝不 import lake；
# 本段是 web/app.py 唯一与 lake 相关的接线点，全部在 try/except 内。
# ---------------------------------------------------------------------------
_lake_router = None
try:
    import duckdb as _duckdb_probe  # noqa: F401 - 仅探测可导入性
except ImportError:
    _duckdb_probe = None

if _duckdb_probe is not None:
    try:
        if str(PROJECT_ROOT) not in sys.path:
            sys.path.insert(0, str(PROJECT_ROOT))
        from lake import web_api as _lake_web_api
        app.include_router(_lake_web_api.router)
        _lake_web_api.install(app)
        _lake_router = _lake_web_api.router
    except Exception:  # noqa: BLE001 - lake 包缺失/损坏 → 降级 stub，不阻断主路径
        _lake_router = None

if _lake_router is None:
    @app.get("/api/lake/status")
    def _lake_status_stub() -> Dict[str, Any]:
        return {"installed": False, "detail": "数据湖未安装（uv sync --extra lake）"}


# ---------------------------------------------------------------------------
# 静态前端（最后挂载，避免吞掉 /api/*）
# ---------------------------------------------------------------------------
if DIST_DIR.exists():
    assets_dir = DIST_DIR / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(str(DIST_DIR / "index.html"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9090)
