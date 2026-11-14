# -*- coding: utf-8 -*-
"""stock-screener v7.0 Web 后端（FastAPI）。

v7.0 定位（策略核心替换后）：
- 查询/按需回测界面：/api/bt/runs* 族（异步 TaskManager + SSE）+ /api/positions
  + /api/pool（同步，全池 ~2s）；
- strategy 读写/schema 校验（策略库 v7.0.29 退役：另存为/列表/加载/删除全删，
  回测/持仓/信号统一当前策略 config/strategy.yaml）；
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
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
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
# "我的持仓"实盘交易历史（v7.0.17）：追加式买卖登记日志，/data/ 已 gitignore
USER_TRADES_PATH = PROJECT_ROOT / "data" / "my_trades.json"
# 点位提醒微信推送去重状态（scripts/position_alert.py 原子写；信号中心只读，
# 用于历史信号行标「已推送」）
PUSH_ALERT_STATE_PATH = PROJECT_ROOT / "data" / "alerts" / "pushplus_alert_state.json"
# 盘中触碰事件状态（scripts/signal_touch.py 收盘后（12:05 UTC / 北京 20:05，
# lake-sync 灌完当日 bar 后）原子写；信号中心只读，
# 历史信号 = 状态机回放 buys ∪ 触碰事件，同 代码|日期|档 去重，buy 优先）
TOUCH_STATE_PATH = PROJECT_ROOT / "data" / "alerts" / "signal_touch_state.json"
_TS_CODE_RE = re.compile(r"^[a-z]{2}\.\d{6}$")
# v7 回测产物根（CLI 默认 out；Web 任务用 runs/<task_id> 子目录隔离）
BT_OUT_DIR = OUTPUT_DIR / "backtest_martingale"
BT_RUNS_DIR = BT_OUT_DIR / "runs"
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
        # v7.0.28 手数口径（C 方案）：替代 initial_amount/base_add_amount/growth_ratio
        "lot_size": ("int", 1, 10000),
        "first_lots": ("int", 1, 10000),
        "add_lots_step": ("int", 0, 10000),
        "add_step_pct": ("float", 0.1, 50),
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
        # v7.0.39：十年回测净收益率下限（%，百分数口径；0 = 原「必须为正」语义）
        "backtest_min_return_pct": ("float", 0, 1e3),
        "min_ttm_yield_pct": ("float", 0, 100),
        "price_min_yuan": ("float", 0, 1e4),
        "price_max_yuan": ("float", 0, 1e5),
    },
    # v7.0.13 选股时机（**可选段**：v7.0.26 之前的老 yaml 无此段，PUT 缺省不报错）。
    # v7.0.26：first_drawdown_pct 自 strategy 段移入（首仓建仓回撤 = 选股时机
    # 参数）；② 股息率 / ③ 250 日线参数删除。
    "timing": {
        "price_window_years": ("int", 1, 30),
        "price_pctile_max": ("float", 1, 100),
        "first_drawdown_pct": ("float", 1, 90),
    },
}

# 所有必须出现的 (section, field) —— 「所有 key 必须齐全」。
# timing 段**不在必选清单**（可缺省 → martingale.config 默认值）；段存在时 3 键须齐全。
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

    # 1) key 齐全性（timing 段可选：v7.0.26 之前的老 yaml 无此段 → 用默认值，不报错）
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
                # v8.0.8：["*"] = 行业白名单全放开（与 martingale.config 层同语义）；
                # 与行业码混用非法（config 层同样拒绝）。
                if v != ["*"]:
                    for p in v:
                        if not isinstance(p, str) or not re.fullmatch(r"[A-Z]\d{2}", p):
                            errors.append(f"{section}.{field} 项非法: {p!r}"
                                          "（应形如 B06；全放开用 [\"*\"]）")
            elif typ == "str_list":
                if not isinstance(v, list) or not v:
                    errors.append(f"{section}.{field} 必须是非空字符串数组")
                    continue
                for p in v:
                    if not isinstance(p, str) or not p.strip():
                        errors.append(f"{section}.{field} 项非法: {p!r}（应是非空字符串）")

    # 3) 跨字段语义
    # v7.0.26：first_drawdown_pct 居 timing 段（旧布局兼容读 strategy 段）
    st = cfg.get("strategy")
    if not isinstance(st, dict):
        st = {}
    tm = cfg.get("timing")
    if not isinstance(tm, dict):
        tm = {}
    dd_v = tm.get("first_drawdown_pct", st.get("first_drawdown_pct"))
    if dd_v is not None and "add_step_pct" in st:
        try:
            if float(dd_v) + float(st["add_step_pct"]) >= 100:
                errors.append("first_drawdown_pct + add_step_pct 必须 < 100")
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
    """POST /api/bt/runs：{start?, end?, codes? | pool}（v7.0.29 起固定当前策略）。

    - start/end: YYYY-MM-DD（缺省 = 各股数据起点/终点）；
    - codes: 逗号分隔字符串或数组（与 pool 互斥，都不给 = 股票池）；
    - pool: true 显式股票池（与 codes 同时给 → 400）；
    - dynamic_pool: v8.0.13 动态池回测（池每月重算 PIT，出池冻结开仓/加仓；
      与 codes 互斥；耗时 ≈ 20~30 分钟/十年）；
    - 策略 = 当前活动策略 config/strategy.yaml（策略库已退役，不再支持选快照）。
    """
    start: Optional[str] = None
    end: Optional[str] = None
    codes: Optional[Any] = None   # str 或 list[str]
    pool: Optional[bool] = None
    dynamic_pool: Optional[bool] = None   # v8.0.13


class ImportStrategyRequest(BaseModel):
    """POST /api/strategy/import 请求体 {raw: <yaml 文本>}。"""
    raw: str


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

    策略固定 = 当前活动策略 config/strategy.yaml（v7.0.29：策略库退役）。
    """
    _validate_bt_dates(req.start, req.end)
    codes = _normalize_codes(req.codes)
    if codes and req.pool:
        raise HTTPException(status_code=400, detail="codes 与 pool 互斥（codes 优先，请二选一）")
    if req.dynamic_pool and (codes or req.pool):
        raise HTTPException(status_code=400, detail="dynamic_pool 与 codes/pool 互斥（v8.0.13 动态池=月度重算，无手动标的）")

    cmd = [str(VENV_PYTHON), "-m", "martingale", "backtest"]
    if req.start:
        cmd += ["--start", req.start]
    if req.end:
        cmd += ["--end", req.end]
    if req.dynamic_pool:
        cmd += ["--dynamic-pool"]   # v8.0.13
    elif codes:
        cmd += ["--codes", ",".join(codes)]
    # 无 --codes = 默认股票池（pool=true 显式同义）
    if not VENV_PYTHON.exists():
        raise HTTPException(status_code=500, detail=f"找不到 venv python: {VENV_PYTHON}")

    # task_id 预生成（产物目录 output/backtest_martingale/runs/<task_id> 与之一致）
    task_id = "bt_" + datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:4]
    cmd += ["--out", str(BT_RUNS_DIR / task_id)]
    res = tm.start(cmd, {"start": req.start, "end": req.end,
                         "codes": codes or None, "pool": bool(req.pool) or not codes,
                         "dynamic_pool": bool(req.dynamic_pool)},
                   ["回测完成"], task_id=task_id)
    res["out_dir"] = str(BT_RUNS_DIR / task_id)
    return res


@app.get("/api/bt/runs/{task_id}/status")
def bt_run_status(task_id: str) -> Dict[str, Any]:
    if not re.fullmatch(r"bt_[A-Za-z0-9_]+", task_id):
        raise HTTPException(status_code=400, detail="非法 task_id")
    return tm.status(task_id)


@app.get("/api/bt/runs")
def bt_runs_list(limit: int = 30) -> Dict[str, Any]:
    """历史回测列表（v7.0.34，Joel 2026-11-10：「回测页面最好有个历史回测
    结果读取功能」）：扫描 BT_RUNS_DIR 各产物目录的 status.json 摘要。

    只读零副作用；task_id 含时间戳 → 降序 = 最新在前。运行中/不完整（无
    status.json）的任务 generated_at=None，前端标「运行中/不完整」。
    limit 上限 100（防一次拉太宽）。
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 30
    if limit < 1:
        limit = 30
    limit = min(limit, 100)
    runs: List[Dict[str, Any]] = []
    if BT_RUNS_DIR.is_dir():
        for d in BT_RUNS_DIR.iterdir():
            if not d.is_dir() or not re.fullmatch(r"bt_[A-Za-z0-9_]+", d.name):
                continue
            meta = None
            sj = d / "status.json"
            if sj.exists():
                try:
                    meta = json.loads(sj.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001 - status.json 损坏 → 按不完整展示
                    meta = None
            if not isinstance(meta, dict):
                meta = {}
            w = meta.get("window") or {}
            runs.append({
                "task_id": d.name,
                "generated_at": meta.get("generated_at"),
                "window_start": w.get("start"),
                "window_end": w.get("end"),
                "n_dates": w.get("n_dates"),
                "n_stocks": meta.get("n_stocks"),
                "n_triggered": meta.get("n_triggered"),
                "n_buys_total": meta.get("n_buys_total"),
                "params": meta.get("params") or {},
            })
    runs.sort(key=lambda r: r["task_id"], reverse=True)
    return {"runs": runs[:limit], "total": len(runs)}


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
    # v8.0.13 动态池产物（静态回测无此文件 → 键缺省，前端 v-if 隐藏）
    pt = out_dir / "pool_timeline.csv"
    if pt.exists():
        res["pool_timeline"] = _read_csv_rows(pt)
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

    cfg_path 缺省 = 活动策略 config/strategy.yaml（v7.0.29 策略库退役后恒为缺省；
    参数保留仅为外部调用方/测试桩兼容）。
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
                # v8.0.11：分红/上涨占比拆解（百分数口径，同 bt_pnl_pct；
                # None = 总盈亏≤0 或旧预计算表无该列 → 前端显示 —）
                "bt_div_pool": m.bt_div_pool,
                "bt_price_pnl": m.bt_price_pnl,
                "bt_dividends": m.bt_dividends,  # v8.0.12 累计分红（元，原值）
                "bt_div_share": (round(m.bt_div_share * 100.0, 2)
                                 if m.bt_div_share is not None else None),
                "bt_price_share": (round(m.bt_price_share * 100.0, 2)
                                   if m.bt_price_share is not None else None),
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
    """今日选股：候选池内时机门（v7.0.24：① 价格分位单条件）命中（in_watchlist）。

    - push_eligible = ①（与观察同值，页面徽标用）；v7.0.25 起时机**无独立
      微信推送**——微信统一为「进入当前信号才推」（价破线，策略点位提醒）；
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
    # 排序：分位升序（越低越深）→ 代码
    watch.sort(key=lambda r: (r.get("pctile_window") or 1e9, r["ts_code"]))
    as_of = next((str(r["as_of_date"]) for r in tmap.values()), None)
    return {
        "as_of": as_of,
        "n_pool": len(tmap),
        "n_watchlist": len(watch),
        "n_push": sum(1 for r in tmap.values() if r.get("push_eligible")),
        "params": {
            "price_window_years": tp.price_window_years,
            "price_pctile_max": tp.price_pctile_max,
        },
        "rows": watch,
    }


def _beijing_today():
    """北京时区今日（A 股交易日参照；ex_date 是北京时间日期，勿用 UTC date——
    16:00-24:00 UTC 期间 UTC 日期落后北京一天）。v7.0.32 除权日修正参照日。"""
    return datetime.now(timezone(timedelta(hours=8))).date()


def _strategy_state_rows(sp, con, codes, end=None, include_buys: bool = False,
                         now=None, tp=None, since_map=None) -> Dict[str, Dict[str, Any]]:
    """状态机投影（单一口径，防漂移）。

    v7.0.20：实现下沉至 martingale.engine.state_rows（scripts/signal_touch.py 共用
    同一函数）。now=参照日（实时路径传当天）→ 除权日盘中触发价按 pending 除权
    事件修正到除权后口径（v7.0.32）；回测/回放不传 → 零修正。
    v8.1.0：tp=首仓双条件②参数（timing 段）——缺省从 CONFIG_PATH 加载
    （生产路径统一带分位门；显式传 tp=None 关闭 = 仅单元测试）。
    v8.2.0：since_map=用户建仓日 {code: date}（仅持仓判断路径）→ 行内附
    shares_since（建仓日起策略买入累计）。
    """
    from martingale.engine import state_rows
    if tp is None:
        from martingale.config import load_config as _mload
        try:
            _s, _u, tp = _mload(str(CONFIG_PATH))
        except Exception:  # noqa: BLE001 - 配置损坏 → fail-open 无门（页面仍可读）
            tp = None
    return state_rows(sp, con, codes, end=end, include_buys=include_buys,
                      now=now, tp=tp, since_map=since_map)


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
        # now=参照日：回放（as_of 指定）→ as_of 当日口径（不套其后的除权修正）；
        # 实时（as_of 缺省）→ 北京今日（除权日盘中触发价修正，v7.0.32）
        _now = (date.fromisoformat(as_of) if as_of else _beijing_today())
        rows = _strategy_state_rows(_sp, con, pool.codes, end=as_of, now=_now)
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


# ---------------------------------------------------------------------------
# v8.2.0 补 页面刷新提速（Joel 2026-11-13：我的持仓每次刷新十几秒）
# 我的持仓页每 30s 轮询 /api/my_positions + /api/strategy_signals，原先每次
# 请求都重跑 候选池构建(~1s) + 全历史状态机投影(池∪持仓 ~3.5s) + 实时行情
# 网络取价(1~10s)。两处进程内缓存，不新增配置键：
# 1) 状态机投影缓存：键 = 湖/策略配置/持仓账本/手动池 的 mtime+size + 北京日期。
#    数据不变 → 零重算；lake-sync 灌数 / 改配置 / 改账本 → mtime 变 → 自动重算。
#    两接口共享同一投影（include_buys + since_map 一次算齐，strategy_signals
#    多出的 shares_since 字段无害；tmap/bmap 快照同缓存）。
# 2) 实时行情 TTL 15s 缓存：页面 30s 轮询 → 多数轮次命中；取价失败不写缓存
#    （有空缓存则 best-effort 返回，比回退湖价新鲜）。
# ---------------------------------------------------------------------------
_STATE_CACHE: Dict[str, Any] = {"key": None, "value": None}
_SPOT_CACHE: Dict[str, Any] = {"ts": 0.0, "data": {}}
_STATE_LOCK = threading.Lock()
_SPOT_LOCK = threading.Lock()
# TTL 35s > 页面轮询 30s：隔轮共享一次取价（两接口共享同一缓存），
# 价格至多滞后 ~35s（30s 轮询页面上不可感知）；盘中每轮平均网络 ≈ 1 次。
_SPOT_TTL_S = 35.0


def _state_cache_key() -> tuple:
    """投影缓存键：湖(mtime+size) + 小文件内容哈希 + 北京日期。

    本机文件系统 mtime 量化粗（实测 10ms 内两次写同 mtime）→ 小文件（策略
    配置/持仓账本/手动池，KB 级）直接内容哈希，任何改动即失效；湖是大文件
    （仅 lake-sync 日写），mtime+size 足够。
    """
    from martingale.data import default_db_path
    try:
        st = Path(default_db_path()).stat()
        lake_sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        lake_sig = None

    def _sig(p: Path):
        try:
            return hashlib.sha1(p.read_bytes()).hexdigest()
        except OSError:
            return None

    return (lake_sig, _sig(CONFIG_PATH), _sig(USER_POSITIONS_PATH),
            _sig(Path(PROJECT_ROOT) / "data" / "manual_pool.json"),
            str(_beijing_today()))


def _state_projection(need_since: bool = False) -> Dict[str, Any]:
    """候选池 + 状态机投影（池∪持仓，include_buys）共享缓存。

    两份投影共享同一数据键（各自每数据键至多算一次，惰性）：
    - rows（无 since_map）→ /api/strategy_signals（与 v8.2.0 前调用签名一致）；
    - rows_since（带 since_map，v8.2.0 建仓日锚定）→ /api/my_positions 持仓判断路径。
    慢路径持锁（并发轮询串行化）；湖锁定/缺失 → 降级值**不入缓存**（瞬态，
    fail-fast 重试）；HTTPException（配置 400 / 池构建 500）原样上抛不缓存。
    """
    user = _up_read()
    key = _state_cache_key()
    _today = str(_beijing_today())[:10]
    with _STATE_LOCK:
        entry = _STATE_CACHE["value"] if _STATE_CACHE["key"] == key else None
        if entry is not None and entry.get("rows") is not None and (
                not need_since or entry.get("rows_since") is not None):
            return entry
        from martingale.data import LakeInvalidFile, LakeLocked, LakeMissing
        # v8.2.0：用户建仓日锚定（缺省=今天）；仅账本股携带
        since_map: Dict[str, str] = {}
        for c, u in user.items():
            u = u if isinstance(u, dict) else {}
            since_map[c] = str(u.get("since_date") or "")[:10] or _today
        try:
            _sp, _up, con, pool = _load_pool_ctx()
        except (LakeLocked, LakeMissing, LakeInvalidFile):
            # 湖锁定/缺失 → 降级值**不入缓存**：锁定是瞬态的（lake-sync 灌数
            # 持锁数分钟），缓存会让降级卡到 mtime 变化；每次请求 fail-fast
            # 重试（连接层即失败，代价 <1s）。
            return {"sp": None, "pool_codes": [],
                    "universe": sorted(user.keys()), "rows": {}, "rows_since": {},
                    "tmap": {}, "bmap": {}, "lake_locked": True}
        try:
            if entry is None:
                universe = sorted(set(pool.codes) | set(user.keys()))
                entry = {"sp": _sp, "pool_codes": list(pool.codes),
                         "universe": universe, "rows": None, "rows_since": None,
                         "tmap": None, "bmap": None, "lake_locked": False}
            if entry["rows"] is None:
                entry["rows"] = _strategy_state_rows(
                    _sp, con, entry["universe"], include_buys=True, now=_today)
            if need_since and entry["rows_since"] is None:
                entry["rows_since"] = _strategy_state_rows(
                    _sp, con, entry["universe"], include_buys=True, now=_today,
                    since_map=since_map)
            if entry["tmap"] is None:
                from martingale.selection import load_backtest_map
                from martingale.timing import load_timing_map
                entry["tmap"] = load_timing_map(con)
                entry["bmap"] = load_backtest_map(con)
        finally:
            con.close()
        _STATE_CACHE["key"] = key
        _STATE_CACHE["value"] = entry
        return entry


def _fetch_spot_cached(codes: List[str]) -> Dict[str, Dict[str, Any]]:
    """fetch_spot + 15s TTL 缓存（v8.2.0 补）。

    命中（TTL 内且已有价）→ 零网络；未命中取缺失集合并合并回缓存；
    取价失败/空 → 返回仍可用的缓存价（含过期，best-effort 比回退湖价新鲜）；
    首取失败且无缓存 → {}（调用方回退湖价，与旧语义一致）。
    """
    if not codes:
        return {}
    need = list(dict.fromkeys(codes))  # 去重保序
    with _SPOT_LOCK:
        ts = _SPOT_CACHE["ts"]
        cached = dict(_SPOT_CACHE["data"])
    missing = [c for c in need
               if time.monotonic() - ts >= _SPOT_TTL_S
               or (cached.get(c) or {}).get("price") is None]
    if not missing:
        return {c: cached[c] for c in need}
    try:
        from screener.data.realtime import fetch_spot
        fresh, _ = fetch_spot(missing, timeout=5.0, max_attempts=1)
    except Exception:  # noqa: BLE001 - 行情失败不阻塞页面
        fresh = {}
    if fresh:
        with _SPOT_LOCK:
            merged = dict(_SPOT_CACHE["data"])
            merged.update(fresh)
            _SPOT_CACHE["data"] = merged
            _SPOT_CACHE["ts"] = time.monotonic()
        out = {c: cached[c] for c in need if c in cached and c not in fresh}
        out.update(fresh)
    else:
        out = {c: cached[c] for c in need if c in cached}
    return out


@app.get("/api/my_positions")
def get_my_positions() -> Dict[str, Any]:
    """我的持仓 = 用户账本（股数/成本）× 展示价 + 策略状态机参考（档/触发价）+ 加仓信号。

    展示价（v7.0.40）：实时行情优先（Gate 3 一主一备：腾讯主 → push2delay 备）；
    行情缺失/失败按股回退湖最新价。最新价/市值/浮动盈亏/汇总均按展示价口径
    （状态机参考仍按湖历史，数据截至日 = 湖日期，不受实时价影响）。

    - 只列用户账本里的代码；策略参考对池外股同样有效（状态机不挑池）。
    - 空账本 → 不连湖直接返回（灌数持锁期间页面仍可用）。
    - 策略固定 = 当前活动策略 config/strategy.yaml（v7.0.29：策略库退役）。
    - signals：买卖提示——现价已跌破所选策略的下一触发价位（v7.0.28 手数口径）：
      持仓中（in_pos，第 k 档）→ action="add"（加仓，first_lots+k×add_lots_step 手）；
      未持仓（never，k=0）→ action="buy"（首仓触发，first_lots 手）。
      现价 = 腾讯实时行情（price_source="spot"）；行情失败回退湖最后价
      （price_source="lake"，只漏报不误报）。状态机「触线即买」→ 湖最后 bar 处
      湖价恒高于触发价，故必须用实时价判定。
    """
    user = _up_read()
    if not user:
        return {"as_of": None, "n": 0, "positions": [], "signals": [], "strategy": "current",
                "totals": {"market_value": 0, "cost": 0, "pnl": 0, "pnl_pct": None}}
    # v8.2.0 补：状态机投影走共享缓存（数据哈希+日期为键，见 _state_projection）
    # ——页面 30s 轮询不再每次重跑全状态机；rows_since 带 since_map（v8.2.0
    # 建仓日锚定，缺省=今天；旧账本升级兼容）。
    proj = _state_projection(need_since=True)
    lake_locked = proj["lake_locked"]
    rows: Dict[str, Dict[str, Any]] = proj["rows_since"]
    _sp = proj["sp"]
    positions: List[Dict[str, Any]] = []
    tot_cost = 0.0
    for c in sorted(user):
        u = user[c] if isinstance(user[c], dict) else {}
        s = rows.get(c, {})
        shares = float(u.get("shares") or 0)
        cost = float(u.get("cost_price") or 0)
        # v8.2.0 累计差值口径（Joel 2026-11-13 定案）：理论持仓 = 用户建仓日起
        # 策略买入的累计股数（shares_since，状态机真实口径——含建仓后所有轮次
        # 的买入；建仓早于时机/晚于多轮均正确）；实际 = 账本股数；差值 =
        # max(0, 理论−实际)。湖中无数据 → 三字段 None。实时新档触发的补差见
        # signals（build_signals 同口径 + 本档手数）。
        strat_shares = s.get("shares")  # 状态机全历史模拟累计股数（仅展示参考）
        shares_since = s.get("shares_since")  # v8.2.0：建仓日起策略买入累计
        lot = int(_sp.lot_size) if _sp is not None else 100
        theory_lots = (round(shares_since / lot, 2)
                       if shares_since is not None else None)
        actual_lots = round(shares / lot, 2)
        gap_lots = (round(max(0.0, theory_lots - actual_lots), 2)
                    if theory_lots is not None else None)
        positions.append({
            "ts_code": c,
            "name": s.get("name") or c,
            "shares": shares,
            "cost_price": cost,
            "last_price": s.get("last_price"),  # 湖最新价（下方实时行情覆盖，v7.0.40）
            "market_value": None,  # 行情覆盖后按展示价统一计算
            "pnl": None,
            "pnl_pct": None,
            "updated_at": u.get("updated_at"),
            "since_date": u.get("since_date"),  # v8.2.0：建仓日（理论持仓锚点）
            "buys": list(u.get("buys") or []),  # v8.2.0：手动加仓时间线（自动记录）
            # 策略状态机参考（池外股/湖中无数据时字段为 None）
            "in_pos": s.get("in_pos"),
            "strat_shares": strat_shares,
            "strat_shares_since": shares_since,  # v8.2.0：建仓日起策略买入累计
            "theory_lots": theory_lots,
            "actual_lots": actual_lots,
            "gap_lots": gap_lots,
            "k_level": s.get("k_level"),
            "next_add_price": s.get("next_add_price"),
            "next_rearm_price": s.get("next_rearm_price"),
            "next_add_price_raw": s.get("next_add_price_raw"),
            "next_rearm_price_raw": s.get("next_rearm_price_raw"),
            "end_date": s.get("end_date"),
        })
        tot_cost += shares * cost
    # 加仓信号（实时价口径）：
    # 状态机在湖最后 bar「触线即买」→ 湖最后 bar 处 最新价 > 下一档触发价 恒成立，
    # 用湖价判"已触线"永远为假。故信号用**实时价**（腾讯行情）对比触发价；
    # 实时行情失败 → 回退湖最后价（price_source="lake"，此时信号只会漏报不会误报）。
    # v1.1（Gate 3，设计 §5）：一主一备统一入口（腾讯主 → push2delay 备，整批切换）
    # v8.2.0 补：15s TTL 缓存（页面 30s 轮询 → 多数命中，不再每次网络取价）
    spot: Dict[str, Dict[str, Any]] = _fetch_spot_cached(
        [p["ts_code"] for p in positions])
    price_source = "spot" if any(v.get("price") is not None for v in spot.values()) else "lake"
    spot_time = next((v.get("timestamp") for v in spot.values() if v.get("timestamp")), None)
    for pos in positions:
        p_now = (spot.get(pos["ts_code"]) or {}).get("price")
        pos["spot_price"] = p_now
        # v7.0.40：展示价 = 实时行情优先（缺失按股回退湖价）；市值/盈亏/
        # 汇总统一按展示价口径（下方重算）。状态机参考（档/触发价）不受影响。
        if p_now is not None:
            pos["last_price"] = p_now
    # 市值 / 浮动盈亏 / 汇总（v7.0.40：行情覆盖后按展示价计算）
    tot_mv = 0.0
    has_price = False
    for pos in positions:
        shares = pos["shares"]
        cost_total = shares * pos["cost_price"]
        last = pos["last_price"]
        mv = shares * last if last is not None else None
        pnl = (mv - cost_total) if mv is not None else None
        pos["market_value"] = round(mv, 2) if mv is not None else None
        pos["pnl"] = round(pnl, 2) if pnl is not None else None
        pos["pnl_pct"] = round(pnl / cost_total * 100.0, 2) if (pnl is not None and cost_total > 0) else None
        if mv is not None:
            tot_mv += mv
            has_price = True
    # 信号：现价（实时优先）≤ 下一档触发价（原始口径，v7.0.28 手数）
    # - in_pos（持仓中，第 k 档）→ "add"：加仓到第 k+1 档，
    #   手数 = first_lots + k×add_lots_step
    # - 未持仓（last_status=never，k=0）→ "buy"：首仓买入触发，手数 = first_lots
    # （状态机「触线即买」→ 湖最后 bar 处湖价恒高于触发价，必须实时价判定）
    # v7.0.11：口径抽至 martingale.signals.build_signals（微信点位提醒脚本共用，
    # 单一定义点防漂移）
    # v8.2.0 补：首档（k=0）= 建仓（再武装后新一轮 = 二次建仓）→ 须过当前
    # 时机门才提示（与策略中心同口径 _held_first_timing：池内=快照，池外=
    # 实时现算，fail-closed）；轮内加仓档（k≥1）不受约束
    from martingale.signals import build_signals

    _tmap = proj["tmap"]
    timing_map: Dict[str, bool] = {}
    for c in user:
        trow = _tmap.get(c)
        if trow is not None:
            timing_map[c] = bool(trow.get("push_eligible"))
        else:
            _p = ((spot.get(c) or {}).get("price")
                  or (rows.get(c) or {}).get("last_price"))
            timing_map[c] = _held_first_timing(c, _tmap, _p)[0]
    signals = build_signals(positions, _sp, timing_map=timing_map)
    return {
        "as_of": max((r["end_date"] for r in positions if r["end_date"]), default=None),
        "lake_locked": lake_locked,
        "n": len(positions),
        "positions": positions,
        "signals": signals,
        "strategy": "current",
        "price_source": price_source,
        "spot_time": spot_time,
        "totals": {
            "market_value": round(tot_mv, 2) if has_price else None,
            "cost": round(tot_cost, 2),
            "pnl": round(tot_mv - tot_cost, 2) if has_price else None,
            "pnl_pct": round((tot_mv - tot_cost) / tot_cost * 100.0, 2) if (has_price and tot_cost > 0) else None,
        },
    }


# ---------------------------------------------------------------------------
# 策略信号中心（v7.0.19）：当前信号 / 即将进入信号区 / 历史信号 + 持仓·非持仓
# ---------------------------------------------------------------------------
# 历史信号保留窗口（天）：只保留近 N 日的状态机触发记录（Joel 2026-11-07
# 定案——全量回放历史自 2000 年起动辄数千笔，页面无用且拖慢传输）
SIG_HISTORY_WINDOW_DAYS = 90


def _timing_breakdown(trow: Optional[Dict[str, Any]], tp) -> str:
    """选股时机明细（仅展示用）。

    v7.0.24 起时机门 = ① 单条件（分位带 ✓/✗ + 数值 + 阈值）；
    v7.0.26：② 股息率 / ③ 年线参考展示一并删除。
    无 timing 行 → 「无时机数据」（fail-closed 同口径）。
    """
    if not trow:
        return "无时机数据"
    parts: List[str] = []
    p = trow.get("pctile_window")
    ok1 = bool(trow.get("cond_price_low"))
    v1 = "数据不足" if p is None else f"{p:.1f}%"
    parts.append(f"{'✓' if ok1 else '✗'} {tp.price_window_years}年分位 "
                 f"{v1}（需≤{tp.price_pctile_max:g}%）")
    return " · ".join(parts)


def _held_first_timing(c: str, tmap: Dict[str, Dict[str, Any]],
                       price: Optional[float]):
    """v8.2.0 补（Joel 2026-11-13 二次定案）：持仓股首档（k=0 = 再武装后新一轮
    「二次建仓」/ 模拟从未持仓轮次的首仓）的时机判定——策略中心与我的持仓
    共用（单一定义点，与微信 position_alert 通道同口径对齐）。

    池内（tmap 有行）= 预计算快照 push_eligible（O(1)，与非持仓时机门同源）；
    池外/无行（自选持仓股，如中粮糖业）→ 现算**实时分位**（与微信持仓通道的
    live_pctile_map 同一口径：现价替换窗口末值重算 5 年分位 ≤ 40%）——否则
    池外持仓股 fail-closed 恒不过门，微信能推而页面不显示，两处不一致。
    失败/湖占用/无数据 → (False, None, 原因)（fail-closed：未过）。

    :return: (ok, pctile, reasons)
    """
    trow = tmap.get(c)
    if trow is not None:
        return (bool(trow.get("push_eligible")),
                trow.get("pctile_window"), _timing_breakdown(trow, tp_obj_global()))
    # 无时机行（池外持仓）→ 现算实时分位（微信同口径，fail-closed）
    # 300s TTL 进程内缓存：页面 30s 轮询不每次开湖（5 年分位对现价变动不敏感，
    # 边界抖动与微信实时通道同量级）
    now_ts = time.time()
    hit = _HELD_PCT_CACHE.get(c)
    if hit is not None and now_ts - hit[0] < 300.0:
        pct = hit[1]
    else:
        try:
            from martingale.data import connect_readonly
            from martingale.timing import live_pctile_map
            con = connect_readonly(None)
            try:
                pm: Dict[str, Optional[float]] = {c: price} if price is not None else {}
                pcts = live_pctile_map(con, [c], pm, tp_obj_global())
            finally:
                con.close()
            pct = pcts.get(c)
        except Exception:  # noqa: BLE001 - 湖占用/结构异常 → fail-closed（不缓存）
            return False, None, "无时机数据（fail-closed）"
        if pct is not None:
            _HELD_PCT_CACHE[c] = (now_ts, pct)
    if pct is None:
        return False, None, "无时机数据（fail-closed）"
    tp = tp_obj_global()
    ok = pct <= tp.price_pctile_max
    return ok, round(pct, 1), (f"实时 {tp.price_window_years}年分位 {pct:.1f}%"
                               f"（需≤{tp.price_pctile_max:g}%，池外现算）")


def tp_obj_global():
    """v8.2.0 补：timing 参数（模块级缓存；配置热改后按 mtime 重读）。"""
    global _TP_CACHE
    p = str(CONFIG_PATH)
    try:
        mtime = os.path.getmtime(p)
    except OSError:
        mtime = None
    if _TP_CACHE is None or _TP_CACHE[0] != mtime:
        from martingale.config import load_config as _mload  # 与端点同源
        _s0, _u0, _tp = _mload(p)
        _TP_CACHE = (mtime, _tp)
    return _TP_CACHE[1]


_TP_CACHE: Optional[tuple] = None
# v8.2.0 补：池外持仓股 k=0 现算实时分位的 TTL 缓存 {code: (ts, pct|None)}
_HELD_PCT_CACHE: Dict[str, tuple] = {}


@app.get("/api/strategy_signals")
def get_strategy_signals() -> Dict[str, Any]:
    """策略信号中心（Joel 2026-11-07：我的持仓页新增信号模块）。

    对 **股票池 ∪ 我的持仓** 逐股给「现价 vs 下一档触发价」的完整状态：
    - status: active（现价 ≤ 触发价）/ **touched（盘中触碰，v7.0.20：
      最新行情日最低价已触线、现价回出信号区——信号今日已发生，行情日
      滚动后自动移出，次日在 history 段可见）** / approaching（即将进入
      信号区，触发价上方 approach_pct% 带内）/ idle（远离）/ no_data（无价或
      无触发价）；status 是**价格状态事实**；**「当前信号」段的归属由前端
      约束（v8.2.0 补，Joel 2026-11-13 定案）= (active|touched) 且
      action 非空**——无动作（时机未过/持仓已够/非持仓时机门未过）一律
      不入当前信号；价格已在区但无信号且 40%&lt;分位≤50%（临近时机线）
      的行由前端移入「即将进入」段展示。touched 行的动作/手数按 active
      同口径补齐（信号今日已发生，v7.0.28 手数口径）。
    - held = 在用户账本（持仓），否则非持仓（池内非持仓股同样有策略参考——
      状态机不挑池）；触发价口径：按 in_pos 分支（与 held 无关）——
      in_pos（模拟持仓已建立）= 模拟周期 next 档（next_add_price_raw，加仓
      第 N 档；非持仓股此口径 = 「从当前状态加入策略」的触发线）；未 in_pos
      （模拟尚未建仓）= 首仓线（first_level_raw = 本周期 H×(1−first_drawdown)，
      动作恒为买入·首仓、手数 = first_lots，v7.0.28）。
    - **持仓股动作/手数走建仓日锚定判定（v8.2.0 补，Joel 2026-11-13：两处信号
      一致）**：judge_held_lots 与 /api/my_positions、微信 position_alert 同一
      单一定义点——有策略跟上的仓位（shares_since>0）：理论（建仓日起策略买入
      累计 + 本档手数）≤ 实际持仓 → action=None（持仓已够，不提醒）；差值 >0
      → 建议补差值手数；**建仓日起策略零买入（心血来潮/高位建仓）或数据缺失
      → 正常信号（本档手数，照常提醒）**。非持仓股完全不变（正常逻辑 + 时机门）。
      判定明细见行内 theory_lots/actual_lots/gap_lots（未判定为 None）。
    - 时机门（v7.0.24 定案：① 价格分位单条件）：非持仓股的买入信号须先过
      选股时机提醒口径（timing.push_eligible = ① 5年分位≤40%，读
      selection_timing 快照 O(1)）——到买入时机才给买入信号，否则 action=None
      只展示价格状态 + timing_reasons（① 带 ✓/✗ 数值+阈值；v7.0.26：②③ 删除）
      （持仓股不受时机限制（持仓策略管加仓）。
      timing_ok/timing_reasons 逐股标注。
    - 十年回测参考（v7.0.19 迭代）：推荐买入股标注历史回测——bt_pnl_pct
      （10 年回测收益 %）/ bt_n_buys（回测触发档数），读 selection_backtest_10y；
      池外/无回测 → None。
    - history = 状态机历史触发逐笔（buys：日期/触发价/档位金额/档下标/是否本轮
      首笔/是否已推送微信）∪ **盘中触碰事件（v7.0.20：scripts/signal_touch.py
      收盘后（12:05 UTC / 北京 20:05，lake-sync 灌完当日 bar 后）判定「最新行情日
      最低价≤触发线」落盘 data/alerts/
      signal_touch_state.json，90 天滚动窗口）**，**仅保留近 SIG_HISTORY_WINDOW_DAYS
      日**（全量回放历史自 2000 年起动辄数千笔，页面无用），日期降序（同日同股
      按档位先后，buy 行在 touch 行前）；同 (代码,日期,档) 去重 buy 优先（收盘
      过线那天的触碰回放已记，不重复展示）；pushed = 该档「代码|动作|档」前缀
      命中 scripts/position_alert.py 的推送去重态（data/alerts/
      pushplus_alert_state.json）→ 已推送过微信点位提醒。
    - 策略固定 = 当前活动策略（v7.0.29）；价源 / 湖降级语义与 /api/my_positions 一致；分类走
      martingale.signals.classify_signal（与 /api/my_positions、微信点位提醒
      同一单一定义点，防口径漂移）。
    """
    from martingale.signals import classify_signal, judge_held_lots

    user = _up_read()

    # 时机阈值（仅拼「未达条件」文案用；配置错误已在 _load_pool_ctx 400，
    # 此处兜底默认值防降级路径（湖锁定）仍可读）
    try:
        from martingale.config import load_config as _mload
        _s0, _u0, tp_obj = _mload(str(CONFIG_PATH))
    except Exception:  # noqa: BLE001
        from martingale.config import TimingParams
        tp_obj = TimingParams()

    # v8.2.0 补：与 /api/my_positions 共享同一投影缓存（不再每次重跑全状态机 +
    # 时机/回测快照）；湖锁定 → 投影缓存已按账本降级。rows（无 since_map）供
    # 非持仓股（正常逻辑）；rows_since（建仓日锚定）供持仓股判定（v8.2.0 补：
    # 两处信号一致，Joel 2026-11-13）。
    proj = _state_projection(need_since=True)
    lake_locked = proj["lake_locked"]
    rows: Dict[str, Dict[str, Any]] = proj["rows"]
    rows_since: Dict[str, Dict[str, Any]] = proj["rows_since"]
    sp_obj = proj["sp"]
    universe: List[str] = proj["universe"]
    tmap: Dict[str, Dict[str, Any]] = proj["tmap"]
    bmap: Dict[str, Dict[str, Any]] = proj["bmap"]
    as_of = max((r["end_date"] for r in rows.values() if r.get("end_date")),
                default=None)
    if not universe:
        return {"as_of": None, "strategy": "current", "lake_locked": lake_locked,
                "approach_pct": tp_obj.approach_pct,
                "spot_preselect_pct": tp_obj.spot_preselect_pct,
                "spot_fetched": 0, "price_source": "lake",
                "spot_time": None, "n_stocks": 0, "stocks": [], "history": [],
                "totals": {"n_stocks": 0, "n_active": 0, "n_touched": 0,
                           "n_approaching": 0,
                           "n_idle": 0, "n_no_data": 0, "n_active_held": 0,
                           "n_active_unheld": 0, "n_history": 0}}
    # 实时价取价集合（v7.0.30，Joel 定案）：持仓 ∪ 湖最后收盘价距触发线 ≤
    # spot_preselect_pct%（默认 10，Joel：「大于10%的一定不会在当天达标」）。
    # 精确边界：主板跌停 −10% 下，昨收 +x% 的股当日最低可到 (1+x)×0.9−1，
    # x=10% → −1.0%（恰破线），x∈(10%, 11.11%] + 当日跌停是仅剩的漏取窗口
    # （大市值央国企主板股单日跌停本身极罕见）；该窗口的信号由微信推送兜底
    # ——position_alert.py 仍取全 池∪持仓（推送通道零漏），次日 11:30 批量
    # 落库后 web 展示恢复。其余 >10% 的股价回退湖最后收盘（状态恒 idle；
    # 5% 展示带远于 10%，「即将进入」展示不受影响）。持仓股无条件取
    # （浮盈/加仓信号不限距离）。收窄只作用于 web 页面实时覆盖。
    from martingale import touches as _touches
    fetch_codes: List[str] = []
    # 湖锁定 → 全部 no_data（无触发价），不取行情（与历史降级行为一致）
    if not lake_locked:
        for c in universe:
            if c in user:
                fetch_codes.append(c)
                continue
            r = rows.get(c)
            if r is None:
                continue  # 湖中无数据 → no_data，无触发价，不必取行情
            trig = _touches.trigger_price(r, held=False)
            last = r.get("last_price")
            if trig is None or last is None:
                continue
            if (last - trig) / trig * 100.0 <= tp_obj.spot_preselect_pct:
                fetch_codes.append(c)
    # 实时价（与 /api/my_positions 同口径：spot 优先，失败回退湖最后价——
    # 只漏报不误报）。v1.1（Gate 3，设计 §5）：一主一备统一入口
    # v8.2.0 补：15s TTL 缓存（两接口共享，多数轮次零网络）
    spot: Dict[str, Dict[str, Any]] = _fetch_spot_cached(fetch_codes)
    price_source = "spot" if any(v.get("price") is not None for v in spot.values()) else "lake"
    spot_time = next((v.get("timestamp") for v in spot.values() if v.get("timestamp")), None)
    # 最新行情日（v7.0.20 盘中触碰判定基准）：全池最大 quote_date——停牌股行情
    # 停在旧日，day_low 是旧日数据，不得算「今日触碰」；只有行情日=最新日才算
    quote_dates = [_touches.quote_date_of((v or {}).get("timestamp"))
                  for v in spot.values()]
    ref_quote_date = max((d for d in quote_dates if d), default=None)

    # 微信推送状态（scripts/position_alert.py 点位提醒去重态，原子替换写）→
    # 历史信号按「代码|动作|档下标」前缀匹配标「已推送」。读取失败按未推送
    # 处理（只标注不影响主流程）。
    pushed_keys: set = set()
    try:
        with open(PUSH_ALERT_STATE_PATH, "r", encoding="utf-8") as f:
            pushed_keys = {str(k) for k in json.load(f).get("alerts", {})}
    except (OSError, ValueError):
        pushed_keys = set()
    pushed_pfx = {k.rsplit("|", 1)[0] + "|" for k in pushed_keys}
    # 盘中触碰事件（v7.0.20）：收盘 job（12:05 UTC / 北京 20:05）落盘的快照（只读；缺失/损坏 → 无
    # 事件，不影响主流程）→ 合并进历史信号（同 代码|日期|档 去重，buy 优先）
    touch_state = _touches.load_state(str(TOUCH_STATE_PATH))
    # 历史信号保留窗口：近 N 日（Joel 2026-11-07 定案——不展示状态机自 2000
    # 年起的全量回放历史，只留近期可核对的触发记录）
    hist_cutoff: Optional[str] = None
    if as_of:
        hist_cutoff = (datetime.fromisoformat(as_of).date()
                       - timedelta(days=SIG_HISTORY_WINDOW_DAYS)).isoformat()

    stocks: List[Dict[str, Any]] = []
    history: List[Dict[str, Any]] = []
    n_active = n_appr = n_idle = n_nodata = n_act_held = n_act_unheld = n_touched = 0
    for c in universe:
        held = c in user
        r = rows.get(c)
        price = (spot.get(c) or {}).get("price")
        if price is None and r is not None:
            price = r.get("last_price")
        # 触发价口径（v7.0.23 定案，Joel 2026-11-09：「非持仓首仓 = 时机成熟
        # 且已距高点回撤到位，双条件」）：
        # - 持仓股：in_pos → 模拟 next 档（加仓线）；未 in_pos → 首仓线（不变，
        #   持仓策略加仓不受时机限制）；
        # - 非持仓股：**恒用首仓线**（first_level_raw）——价跌破首仓线（距轮高
        #   回撤 first_drawdown）= 「已距高点下跌到位」；时机门（timing_ok）=
        #   「时机成熟」；两者都过才有买入·首仓信号。时机门同时兜住老股幽灵
        #   信号（锚高老轮首仓线远低于市价 → 价条件恒真，靠分位>40% 拦住）。
        #   建仓后由用户记入持仓账本 → 转持仓策略加仓档。
        in_pos = bool((r or {}).get("in_pos"))
        trigger = _touches.trigger_price(r or {}, held=held)
        k = (r.get("k_level") or 0) if r else 0
        # 非持仓的动作/金额按首仓口径（buy/initial）——simulate in_pos 仅持仓
        # 股影响档位语义
        cls = classify_signal(price, trigger, in_pos if held else False, k, sp_obj,
                              approach_pct=tp_obj.approach_pct)
        # 盘中触碰（v7.0.20，Joel 2026-11-08：「盘中达成信号又涨回出区的，保留
        # 到当日，第二天移入历史信号」）：最新行情日最低价已触线、现价回出信号区
        # → 信号今日已发生，保留在当前段（status=touched）；行情日滚动后自动
        # 移出（收盘 job 已把事件落盘 → 次日在历史段可见）
        q = spot.get(c) or {}
        day_low = q.get("low")
        touched = (trigger is not None and day_low is not None
                   and _touches.quote_date_of(q.get("timestamp")) == ref_quote_date
                   and day_low <= trigger and cls["status"] != "active")
        # 时机门（v7.0.24 定案：① 价格分位单条件；v7.0.26：②③ 彻底删除）：非持仓股
        # 的买入信号须先过策略选股时机提醒口径（5年分位≤40%；selection_timing
        # 预计算快照，O(1)）——到买入时机才给买入信号；持仓股不受时机限制
        # （持仓策略负责加仓档）。无时机行（timing 未跑/池外）→ fail-closed
        # 不给买入信号，只展示价格状态与原因。
        timing_ok: Optional[bool] = None
        timing_reasons: Optional[str] = None
        if not held:
            trow = tmap.get(c)
            timing_ok = bool((trow or {}).get("push_eligible"))
            # 逐条件 ✓/✗ 明细（含数值+阈值）：整体过/未过都列明哪条达成哪条未达
            # （Joel 2026-11-08 定案；前端不再叠加总判定前缀，防 ✗✗ 误读）
            timing_reasons = _timing_breakdown(trow, tp_obj)
        action = cls["action"]
        suggested = cls["suggested_lots"]
        if touched and action is None and sp_obj is not None:
            # 已触回（现价已高于触发线 → cls 无动作）：信号今日已发生，动作/
            # 手数按 active 同口径补齐（持仓股 in_pos=加仓 first+k×step 手；
            # 非持仓/未 in_pos=首仓 first_lots 手，v7.0.23/v7.0.28 手数口径）
            if in_pos and held:
                action = "add"
                suggested = int(sp_obj.first_lots + k * sp_obj.add_lots_step)
            else:
                action = "buy"
                suggested = int(sp_obj.first_lots)
        if not held:
            # 非持仓股的动作统一为「买入」（next 档对非持仓用户是加入策略的
            # 买入位，不是加仓）；时机门未过 → 无动作（价格状态照展示）
            if action is not None:
                action = "buy"
            if timing_ok is not True:
                action = None
                suggested = None
        held_first = held and action is not None and k == 0
        held_first_pctile: Optional[float] = None
        if held_first:
            # v8.2.0 补（Joel 2026-11-13 二次定案「二次建仓也要符合时机门
            # 才能提示」）：持仓股首档（k=0）触线 = 再武装后新一轮的建仓
            # （二次建仓，提醒应为「建仓 N 手」而非「加仓」）→ 须过**当前**
            # 时机门才提醒（「不受约束」只豁免轮内加仓档 k≥1）；未过 → 无动作
            # （前端标「时机未过」，价格状态照展示）；池外持仓无时机行 →
            # 现算实时分位（微信同口径），无数据 → fail-closed
            timing_ok, held_first_pctile, timing_reasons = _held_first_timing(
                c, tmap, price)
            if timing_ok is not True:
                action, suggested = None, None
        held_extra: Optional[Dict[str, float]] = None
        if held and action is not None and sp_obj is not None:
            # v8.2.0 补（Joel 2026-11-13：两处信号一致）：持仓股遵循建仓日锚定
            # 判定逻辑（与 /api/my_positions、微信 position_alert 同一口径，
            # judge_held_lots 单一定义点）——有策略跟上的仓位（shares_since>0）：
            # 理论（建仓日起策略买入累计 + 本档）≤ 实际 → 不产动作（持仓已够）；
            # 差值 >0 → 建议补差值手数；建仓日起策略零买入（心血来潮/高位建仓）
            # 或数据缺失 → 正常信号（本档手数，照常提醒）。非持仓股不受影响。
            adj, held_extra, judged = judge_held_lots(
                sp_obj, k, in_pos, suggested,
                (rows_since.get(c) or {}).get("shares_since"),
                float((user.get(c) or {}).get("shares") or 0))
            if judged:
                if adj is None:
                    action, suggested = None, None  # 持仓已够 → 不提醒
                else:
                    suggested = int(adj)
        status = "touched" if touched else cls["status"]
        bt = bmap.get(c) or {}
        stocks.append({
            "ts_code": c,
            "name": (r or {}).get("name") or c,
            "held": held,
            "in_pos": (r or {}).get("in_pos"),
            "k_level": (r or {}).get("k_level"),
            "price": price,
            "next_add_price_raw": trigger,
            "status": status,
            "touched": touched,
            "day_low": (round(float(day_low), 4)
                        if day_low is not None else None),
            "distance_pct": cls["distance_pct"],
            "action": action,
            "suggested_lots": suggested,
            # v8.2.0 补：持仓股首档触线（再武装后新一轮 = 二次建仓）且动作在
            # → 前端标「二次建仓」而非「加仓」
            "new_round": bool(held_first and action is not None),
            # v8.2.0 补：持仓股差值判定明细（与我的持仓页同口径；非持仓/未判定 → None）
            "theory_lots": (held_extra or {}).get("theory_lots"),
            "actual_lots": (held_extra or {}).get("actual_lots"),
            "gap_lots": (held_extra or {}).get("gap_lots"),
            "timing_ok": timing_ok,
            # v7.0.31（C 案，Joel 定案）：5 年分位数值——前端「即将进入」段
            # 对非持仓股按 ≤50% 展示（timing 40% 门未过但价格临区 = 最有提前
            # 量价值的信号；持仓股轮内加仓档不受时机限制 → None，但首档
            # 二次建仓须过时机门 → 展示分位明细：池内=快照，池外=实时现算）
            "timing_pctile": (held_first_pctile if held_first else
                              ((tmap.get(c) or {}).get("pctile_window")
                               if not held else None)),
            "timing_reasons": timing_reasons,
            # 十年回测参考（推荐买入时展示；池外/无回测 → None）
            "bt_pnl_pct": (round(bt["pnl_pct"] * 100.0, 2)
                           if bt.get("pnl_pct") is not None else None),
            "bt_n_buys": bt.get("n_buys"),
            "n_history": len((r or {}).get("buys", [])),
            "end_date": (r or {}).get("end_date"),
        })
        if cls["status"] == "active":
            n_active += 1
            if held:
                n_act_held += 1
            else:
                n_act_unheld += 1
        elif touched:
            n_touched += 1
        elif cls["status"] == "approaching":
            n_appr += 1
        elif cls["status"] == "idle":
            n_idle += 1
        else:
            n_nodata += 1
        for i, b in enumerate((r or {}).get("buys", [])):
            if hist_cutoff is not None and b["date"] < hist_cutoff:
                continue  # 超出保留窗口（不展示全量回放历史）
            baction = "buy" if b["first_of_round"] else "add"
            history.append({
                "ts_code": c, "name": (r or {}).get("name") or c, "held": held,
                "date": b["date"], "price": b["price"], "amount": b["amount"],
                "lots": b.get("lots"),   # v7.0.28 手数（本笔手数）
                "level": b["level"], "first_of_round": b["first_of_round"],
                # 该档位是否已被微信点位提醒推送过（代码|动作|档 前缀匹配）
                "pushed": f"{c}|{baction}|{b['level']}|" in pushed_pfx,
                "_seq": i,
            })
    # 盘中触碰事件（v7.0.20）：收盘 job（12:05 UTC）落盘快照 → 并入历史信号
    # （90 天窗口同口径；同 代码|日期|档 与回放 buys 去重——收盘过线那天的
    # 触碰回放已记，buy 优先不重复展示；_seq 大值保证同日 buy 行在前）
    tseq = 0
    for tc in sorted(touch_state, key=lambda x: str(touch_state[x].get("date") or "")):
        ev = touch_state[tc]
        d = ev.get("date")
        if not d or tc not in universe or (hist_cutoff is not None and d < hist_cutoff):
            continue
        ev_action = "buy" if ev.get("first_of_round") else "add"
        history.append({
            "ts_code": tc, "name": ev.get("name") or tc, "held": tc in user,
            "date": d, "price": ev.get("level_price"), "amount": ev.get("amount"),
            "lots": ev.get("lots"),   # v7.0.28 手数（旧快照无此键 → None）
            "level": ev.get("level"), "first_of_round": bool(ev.get("first_of_round")),
            "pushed": f"{tc}|{ev_action}|{ev.get('level')}|" in pushed_pfx,
            "source": "touch", "_seq": 10 ** 6 + tseq,
        })
        tseq += 1
    history = _touches.dedupe_buys(history)
    # 日期降序；稳定排序保留同日同股「档位先后」（先首仓后加仓）
    history.sort(key=lambda h: (h["ts_code"], h["_seq"]))
    history.sort(key=lambda h: h["date"], reverse=True)
    for h in history:
        del h["_seq"]
    return {
        "as_of": as_of,
        "strategy": "current",
        "lake_locked": lake_locked,
        "approach_pct": tp_obj.approach_pct,
        "spot_preselect_pct": tp_obj.spot_preselect_pct,
        "price_source": price_source,
        "spot_time": spot_time,
        "spot_fetched": len(fetch_codes),
        "n_stocks": len(universe),
        "history_window_days": SIG_HISTORY_WINDOW_DAYS,
        "stocks": stocks,
        "history": history,
        "totals": {
            "n_stocks": len(universe),
            "n_active": n_active,
            "n_touched": n_touched,
            "n_approaching": n_appr,
            "n_idle": n_idle,
            "n_no_data": n_nodata,
            "n_active_held": n_act_held,
            "n_active_unheld": n_act_unheld,
            "n_history": len(history),
        },
    }


def _up_entry(old: Optional[Dict[str, Any]], shares: float, cost: float,
              now: str, buy_event: Optional[Dict[str, Any]] = None,
              since_override: Optional[str] = None) -> Dict[str, Any]:
    """v8.2.0 持仓条目构造（Joel 2026-11-13 定案）。

    - since_date = 建仓日锚点（理论持仓起算点）：
      ① since_override 显式传入（用户手填）→ 覆盖并记 manual；
      ② 旧条目已 manual → 沿用；
      ③ 有 buys 时间线 → 最早加仓日（真实建仓日，自动）；
      ④ 无 → 今天。
    - buys = 手动加仓时间线 [{date, shares, price}]：buy_event 非空时追加
      （每次加仓自动记录，时间锚定计算 + 详情展示用）。
    - 清仓重买 = 新条目新锚点（调用方 pop 后重建）。
    """
    old = old if isinstance(old, dict) else {}
    e: Dict[str, Any] = {"shares": shares, "cost_price": cost, "updated_at": now}
    buys = list(old.get("buys") or [])
    if buy_event:
        buys.append(buy_event)
    if since_override:
        e["since_date"] = str(since_override)[:10]
        e["since_date_manual"] = True
    elif old.get("since_date_manual") and old.get("since_date"):
        e["since_date"] = str(old["since_date"])[:10]
        e["since_date_manual"] = True
    elif buys:
        e["since_date"] = min(str(b.get("date", ""))[:10] for b in buys)
        e["since_date_manual"] = False
    else:
        e["since_date"] = str(_beijing_today())[:10]
        e["since_date_manual"] = False
    if buys:
        e["buys"] = buys
    return e


@app.put("/api/my_positions")
def put_my_positions(payload: Dict[str, Any]) -> Dict[str, Any]:
    """upsert 单只持仓：shares=0 为删除。

    v8.2.0：可选 since_date（建仓日 YYYY-MM-DD）——理论持仓锚点；显式传入
    （非空）→ 覆盖（含修正旧持仓的真实建仓日），未传 → 沿用既有/首建当天。
    """
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
    since_raw = str(payload.get("since_date") or "").strip()[:10]
    if since_raw and not _DATE_RE.fullmatch(since_raw):
        raise HTTPException(status_code=400, detail="since_date 应为 YYYY-MM-DD")
    positions = _up_read()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if shares_f == 0:
        positions.pop(code, None)
    else:
        old = positions.get(code) or {}
        # v8.2.0：加仓自动记录（Joel 2026-11-13）——股数增加 → buys 时间线
        # 追加一笔（日期=建仓日输入，未填=今天）；旧条目首次填建仓日 →
        # 按「自该日起持有当前股数」回填一笔（不造虚假增量）。
        old_buys = list(old.get("buys") or [])
        buy_event = None
        if since_raw and not old_buys and not old.get("since_date_manual"):
            buy_event = {"date": since_raw, "shares": round(shares_f, 2),
                         "price": cost_f}
        elif shares_f > float(old.get("shares") or 0):
            buy_event = {"date": since_raw or str(_beijing_today())[:10],
                         "shares": round(shares_f - float(old.get("shares") or 0), 2),
                         "price": cost_f}
        positions[code] = _up_entry(old, shares_f, cost_f, now,
                                    buy_event=buy_event,
                                    since_override=since_raw or None)
    _up_write(positions)
    return {"ok": True, "ts_code": code, "n": len(positions)}


# ---------------------------------------------------------------------------
# 交易历史（v7.0.17）：实盘买卖登记账，与当前持仓账本分离
# - data/my_trades.json：追加式日志 {"updated_at":..., "trades":[...]}（原子替换落盘）
# - 登记 = 更新当前持仓（买：加权成本；卖：减股数成本不变，清仓移除）+ 追加历史，
#   单次请求原子完成；手工 upsert（PUT /api/my_positions）是"修正"语义，不写历史
# - 删除记录仅移除日志，**不回改当前持仓**（持仓账本是唯一状态源）
# - 口径同 v7.0.15：成本 3 位小数、不含手续费；买整手 100 股倍数，卖允许零股
#   （A 股卖出可零股一次了结）
# ---------------------------------------------------------------------------
def _trades_read() -> List[Dict[str, Any]]:
    if not USER_TRADES_PATH.exists():
        return []
    try:
        d = json.loads(USER_TRADES_PATH.read_text(encoding="utf-8"))
        tr = d.get("trades", [])
        return tr if isinstance(tr, list) else []
    except Exception:  # noqa: BLE001 - 账本损坏不 500，按空日志
        return []


def _trades_write(trades: List[Dict[str, Any]]) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    USER_TRADES_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"updated_at": now, "trades": trades}
    tmp = USER_TRADES_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, USER_TRADES_PATH)  # 原子替换，防半截账本


@app.post("/api/my_trades")
def record_trade(payload: Dict[str, Any]) -> Dict[str, Any]:
    """登记一笔实盘交易：shares 正=买入（整手）、负=卖出。

    原子更新当前持仓 + 追加 data/my_trades.json；卖出记录含已实现盈亏
    （=（卖价−登记时成本）×股数，快照值，供历史回看）。
    """
    code = str(payload.get("ts_code", "")).strip()
    if not _TS_CODE_RE.fullmatch(code):
        raise HTTPException(status_code=400, detail="ts_code 应为 sh.600000 / sz.000001 形式")
    shares = payload.get("shares")
    price = payload.get("price")
    try:
        shares_i = int(shares)
        price_f = float(price)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="shares/price 应为数字")
    if float(shares_i) != float(shares):  # 拒非整数股数（1.5 手之类）
        raise HTTPException(status_code=400, detail="shares 必须为整数")
    if shares_i == 0:
        raise HTTPException(status_code=400, detail="shares 不能为 0（正=买入，负=卖出）")
    if not price_f > 0:
        raise HTTPException(status_code=400, detail="price 必须为正数")
    date_s = str(payload.get("date") or "").strip()
    if not date_s:
        date_s = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    elif not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_s):
        raise HTTPException(status_code=400, detail="date 应为 YYYY-MM-DD")
    name = str(payload.get("name") or "").strip()[:32]

    positions = _up_read()
    cur = positions.get(code) or {}
    old_shares = float(cur.get("shares") or 0)
    old_cost = float(cur.get("cost_price") or 0)
    realized_pnl: Optional[float] = None
    if shares_i > 0:
        if shares_i % 100 != 0:
            raise HTTPException(status_code=400, detail="A 股买入按整手（100 股倍数）")
        new_shares = old_shares + shares_i
        new_cost = round((old_shares * old_cost + shares_i * price_f) / new_shares, 3)
    else:
        n_sell = -shares_i
        if old_shares <= 0:
            raise HTTPException(status_code=400, detail="未持有该股，不能卖出（先登记持仓）")
        if n_sell > old_shares:
            raise HTTPException(
                status_code=400,
                detail=f"卖出不能超过持仓：当前 {old_shares:.0f} 股，拟卖 {n_sell} 股")
        new_shares = old_shares - n_sell
        new_cost = old_cost  # 卖出后剩余持仓均价不变（券商口径）
        realized_pnl = round((price_f - old_cost) * n_sell, 2)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if new_shares <= 0:
        positions.pop(code, None)
    else:
        # v8.2.0：登记买入 → 加仓时间线自动记录（日期=登记日，支持回填）
        _bev = ({"date": date_s, "shares": float(shares_i), "price": price_f}
                if shares_i > 0 else None)
        positions[code] = _up_entry(positions.get(code), new_shares, new_cost, now,
                                    buy_event=_bev)
    _up_write(positions)

    trades = _trades_read()
    tid = "t" + str(int(datetime.now(timezone.utc).timestamp() * 1000))
    while any(t.get("id") == tid for t in trades):  # 同毫秒连笔 → 加后缀保唯一
        tid += "x"
    trade = {
        "id": tid, "ts_code": code, "name": name,
        "direction": "buy" if shares_i > 0 else "sell",
        "shares": abs(shares_i), "price": price_f,
        "amount": round(abs(shares_i) * price_f, 2),
        "date": date_s, "realized_pnl": realized_pnl, "created_at": now,
    }
    trades.append(trade)
    _trades_write(trades)
    return {"ok": True, "trade": trade,
            "position": {"ts_code": code, "shares": max(new_shares, 0.0),
                         "cost_price": new_cost if new_shares > 0 else None,
                         "removed": new_shares <= 0}}


@app.get("/api/my_trades")
def list_trades(ts_code: Optional[str] = None) -> Dict[str, Any]:
    """交易历史（最新在前）；ts_code 可选过滤单股。"""
    trades = _trades_read()
    if ts_code:
        code = ts_code.strip().lower()
        trades = [t for t in trades if t.get("ts_code") == code]
    trades = sorted(trades, key=lambda t: (t.get("created_at") or "", t.get("id") or ""),
                    reverse=True)
    return {"n": len(trades), "trades": trades}


@app.delete("/api/my_trades/{trade_id}")
def delete_trade(trade_id: str) -> Dict[str, Any]:
    """删除一条历史记录。仅移除日志，**不回改当前持仓**（持仓是唯一状态源）。"""
    trades = _trades_read()
    for i, t in enumerate(trades):
        if t.get("id") == trade_id:
            removed = trades.pop(i)
            _trades_write(trades)
            return {"ok": True, "ts_code": removed.get("ts_code"), "n": len(trades)}
    raise HTTPException(status_code=404, detail="记录不存在")


# ---------------------------------------------------------------------------
# 股票模糊搜索（/api/stocks/search）——「我的持仓」录入条候选下拉
# ---------------------------------------------------------------------------
# 数据源 = stock_master（ts_code/name/industry），进程内建拼音索引：
# - 拼音全拼（zhongguoshenhua）+ 首字母（zgsh），pypinyin 一次性算好缓存；
# - 缓存键 = (路径, mtime_ns, size)：lake-sync 更新 master → 下次查询自动重建。
_STOCK_SEARCH_CACHE: Dict[str, Any] = {"key": None, "index": None}


def _stock_search_index() -> List[Dict[str, Any]]:
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
            "SELECT ts_code, name, industry_name, delist_date FROM stock_master "
            "ORDER BY ts_code").fetchall()
    finally:
        con.close()
    index: List[Dict[str, Any]] = []
    for ts_code, name, industry, dl in rows:
        name = name or ""
        pys = lazy_pinyin(name) or []
        index.append({
            "ts_code": ts_code, "name": name, "industry": industry or "",
            "code_digits": ts_code.split(".", 1)[1],
            "py_full": "".join(pys).lower(),
            "py_initials": "".join((c[0] if c else "") for c in pys).lower(),
            "delisted": dl is not None,  # v8.0.9：退市标记（默认过滤；include_delisted 放行）
        })
    _STOCK_SEARCH_CACHE["key"] = key
    _STOCK_SEARCH_CACHE["index"] = index
    return index


_CODE_QUERY_RE = re.compile(r"^(?:sh|sz|bj)?\.?(\d+)$")


def _search_stocks(q: str, include_delisted: bool = False) -> List[Dict[str, str]]:
    """模糊匹配：代码数字 / 中文名子串 / 拼音首字母 / 全拼子串。

    评分（小=优先）：代码精确 0 → 代码前缀 1 → 代码包含 2 → 名精确 0 → 名前缀 1
    → 名包含 2 → 首字母前缀 3 → 首字母包含 4 → 全拼前缀 5 → 全拼包含 6；
    同分按 ts_code 排序；取前 10。
    v8.0.9：include_delisted=False（默认）跳过退市股（持仓录入不该建议退市）；
    True = 数据湖/回测搜索栏（历史回测可含退市股，对齐旧 /api/lake/search 全表语义）。
    """
    q = (q or "").strip().lower()
    m = _CODE_QUERY_RE.fullmatch(q)
    digit_mode = bool(m)
    if digit_mode:
        q = m.group(1)
    out: List[Tuple[int, Dict[str, str]]] = []
    for it in _stock_search_index():
        if it["delisted"] and not include_delisted:
            continue
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
def search_stocks(q: str = "", include_delisted: bool = False) -> Dict[str, Any]:
    """候选股票模糊搜索（录入条下拉用）。

    支持：代码数字（600036 / sh.600036，≥3 位）、中文名子串（电力）、
    拼音首字母（zgsh）、全拼子串（shenhua）。湖被灌数进程锁定 → 503 lake_locked
    （前端降级为手动输入代码）。
    v8.0.9：include_delisted=1 → 候选含退市股（数据湖/回测搜索栏用；
    持仓录入/手动池保持缺省 False——不该建议退市股）。
    """
    q = (q or "").strip()
    if not q:
        raise HTTPException(status_code=400, detail="q 为空")
    m = _CODE_QUERY_RE.fullmatch(q.lower())
    if m and len(m.group(1)) < 3:
        raise HTTPException(status_code=400, detail="纯数字代码至少 3 位")
    try:
        results = _search_stocks(q, include_delisted=include_delisted)
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
    # v7.0.13：v7.0.26 之前的老 yaml 无 timing 段 → 用默认值补齐（前端选股策略卡片
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
    写盘成功后的预计算重算由各入口（put/import）依次调
    _rescan_selection_after_save → _rescan_timing_after_save
    （v7.0.27 时机 / v7.0.41 十年回测；顺序不可换：时机表按候选池算，
    候选池 G7 读十年回测表）。
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


def _rescan_selection_after_save() -> Dict[str, Any]:
    """v7.0.41：策略配置写盘成功后立即重算候选池十年回测表（selection_backtest_10y）。

    G7 十年回测门槛按「当前生产策略参数」回放预计算（v7.0.40 前仅 11:30 UTC
    lake-sync 刷新）——改策略参数（回撤%/手数等）不重算，G7 停在旧参数快照
    直到次日（Joel 2026-11-11「改策略是应该触发重算」）。
    重算 ~2s（102 股×十年回放实测 2.2s）；失败（如 11:30 同步窗口湖被锁）
    不影响保存结果 → 降级 refreshed=false，新参数于下次 11:30 刷新自动生效。
    须在时机表重算**之前**执行：时机表按候选池算，而候选池 G7 读本表。
    """
    try:
        import duckdb

        from martingale.config import load_config
        from martingale.data import default_db_path
        from martingale.selection import refresh_selection

        _sp, up, _tp = load_config(str(CONFIG_PATH))
        p = default_db_path()
        if not os.path.exists(p):
            return {"refreshed": False, "reason": "数据湖不存在（新值下次同步生效）"}
        con = duckdb.connect(p)  # 写连接：湖被锁时立即失败（已实测不挂起）
        try:
            out = refresh_selection(con, _sp, up, tp=_tp)
        finally:
            con.close()
        return {"refreshed": True, "as_of": out.get("as_of"),
                "n_codes": out.get("n_codes"), "n_written": out.get("n_written")}
    except Exception as exc:  # noqa: BLE001 - 保存本身已成功，重算降级不阻断
        return {"refreshed": False, "reason": str(exc)[:200]}


def _rescan_timing_after_save() -> Dict[str, Any]:
    """v7.0.27：策略配置写盘成功后立即重算选股时机表（selection_timing）。

    时机门 / 今日选股读的是预计算表（历史仅 11:30 UTC lake-sync 刷新）——
    编辑模式改了 timing 参数（分位上限/窗口/首仓建仓回撤）不重算，新值要
    次日才生效（Joel 2026-11-09 反馈「编辑改动后没有生效重新扫描」）。
    重算 ~1s；失败（如 11:30 同步窗口湖被锁，DuckDB 写连接立即报锁冲突）
    不影响保存结果 → 降级 refreshed=false，新值于下次 11:30 刷新自动生效。
    """
    try:
        import duckdb

        from martingale.config import load_config
        from martingale.data import default_db_path
        from martingale.timing import refresh_timing

        _sp, up, tp = load_config(str(CONFIG_PATH))
        p = default_db_path()
        if not os.path.exists(p):
            return {"refreshed": False, "reason": "数据湖不存在（新值下次同步生效）"}
        con = duckdb.connect(p)  # 写连接：湖被锁时立即失败（已实测不挂起）
        try:
            out = refresh_timing(con, up, tp)
        finally:
            con.close()
        return {"refreshed": True, "as_of": out.get("as_of"),
                "n_watchlist": out.get("n_watchlist")}
    except Exception as exc:  # noqa: BLE001 - 保存本身已成功，重算降级不阻断
        return {"refreshed": False, "reason": str(exc)[:200]}


@app.put("/api/strategy")
def put_strategy(payload: Dict[str, Any]) -> Dict[str, Any]:
    backup, _ = _apply_strategy_write(payload)
    return {"ok": True, "backup": backup,
            "selection_rescan": _rescan_selection_after_save(),
            "timing_rescan": _rescan_timing_after_save()}


@app.post("/api/strategy/import")
def import_strategy(req: ImportStrategyRequest) -> Dict[str, Any]:
    """加载策略文件——body={raw}（yaml 文本），复用 PUT 同一套校验。"""
    try:
        data = yaml.safe_load(req.raw)
    except Exception as exc:  # noqa: BLE001 - yaml 语法错误 → 400（不写盘）
        raise HTTPException(status_code=400, detail={"errors": [f"YAML 解析失败: {exc}"]})
    backup, sections = _apply_strategy_write(data)
    return {"ok": True, "backup": backup, "sections": sections,
            "selection_rescan": _rescan_selection_after_save(),
            "timing_rescan": _rescan_timing_after_save()}


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
