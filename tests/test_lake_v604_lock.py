# -*- coding: utf-8 -*-
"""test_lake_v604_lock —— v6.0.4 回归：Web 区分"灌数持锁"与"真未初始化"。

缺陷（TL 定位，Joel 反馈）：p0 灌数进程持有 DuckDB 单文件独占写锁期间，
web_api._ensure_initialized 探测 information_schema 连不上库 → 误报
initialized:false + error:"lake_not_initialized"，页面在灌数期间（可能 1h+）
持续误导显示"不可用"。

修复（v6.0.4 brief）：
- **conn 层**：IO Error 文案匹配 "Could not set lock" + "Conflicting lock"（复用
  D-1 的 markers + str(exc) 模式）→ :class:`lake.conn.LakeLocked`（持锁 PID 尽力
  解析，失败 None）；新增 :func:`lake.conn.probe_db_state` 三态探测
  （unavailable / locked / ok）。
- **web_api**：数据端点 locked → 409 + 顶层 {"error":"lake_backfill_in_progress",
  "hint":"灌数进行中，稍后重试"}（与 lake_not_initialized 区分开）；/status locked
  → 200 + initialized=true + backfill_in_progress=true + lock_holder_pid +
  coverage/tasks 降级读 progress 文件（backfill_progress.json 不受 DuckDB 锁影响）。

验收点（brief §4 回归单测，全离线、零网络）：
- locked → /status 200 + backfill_in_progress + tasks 来自 progress 文件；
  数据端点 409 新 error 值（单元级 + FastAPI TestClient HTTP 层）。
- 真未初始化（缺文件/空库/0字节/损坏）→ /status 与数据端点行为与 v6.0.3 **逐字节
  一致**（防 D-1/B-1 修复被回退；locked 响应不得混入 error/hint，uninitialized
  响应不得混入 backfill_in_progress——三态互不串味）。
- locked 时 lock_holder_pid 解析（含解析失败降级 None，且仍报 backfill_in_progress）。

纪律：库一律 tmp_path，**绝不触碰 data/lake/ 生产库**（p0 灌数运行中）；锁的两种
来源——① monkeypatch connect_existing 抛 LakeLocked（fake 锁文案，零子进程）；
② subprocess 真跨进程持锁 tmp 库（仍零网络、不碰生产库）。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from typing import Optional

import pytest

duckdb = pytest.importorskip("duckdb")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
PY = sys.executable or os.path.join(REPO_ROOT, ".venv", "bin", "python")

# 生产 p0 持锁期间实测抓取的 duckdb 锁报错原文（2026-09-15，PID 为当时灌数进程）：
LOCK_MSG_REAL = (
    'IO Error: Could not set lock on file "/home/ubuntu/stock-screener/data/lake/lake.duckdb": '
    "Conflicting lock is held in /usr/bin/python3.10 (PID 1003318) by user ubuntu. "
    "See also https://duckdb.org/docs/stable/connect/concurrency"
)
# 无 PID 子串的变体（解析失败降级路径）
LOCK_MSG_NO_PID = (
    'IO Error: Could not set lock on file "/tmp/x.duckdb": Conflicting lock is held'
)
# D-1 的坏文件文案（必须**不**被判为 locked——判定顺序回归）
INVALID_DB_MSG = "IO Error: Not a DuckDB database file: not a valid DuckDB database file!"


# ===========================================================================
# helpers
# ===========================================================================
def _nonzero_db_file(tmp_path, name: str = "locked.duckdb") -> str:
    """存在且非 0 字节的库文件（fake-duckdb 路径下不要求是合法库）。"""
    p = str(tmp_path / name)
    with open(p, "wb") as f:
        f.write(b"\x00" * 16)
    return p


def _write_progress(tmp_path, tasks=None, coverage=None) -> str:
    """写 backfill_progress.json（tmp，monkeypatch progress_path 指向它）。"""
    p = str(tmp_path / "backfill_progress.json")
    data = {
        "updated_at": "2026-09-15 10:00:00",
        "tasks": tasks if tasks is not None else [
            {"table": "kline_daily", "tier": "P3", "total": 100, "done": 40,
             "quota_used_today": 12, "quota_budget": 5000, "state": "running",
             "eta_min": 7, "last_error": ""},
        ],
        "coverage": coverage if coverage is not None else {
            "kline_daily": {"rows": 4000, "codes": 20,
                            "date_min": "2026-01-01", "date_max": "2026-09-15"},
        },
    }
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return p


def _seed_ready_db(db_path: str) -> None:
    """建 schema + 播种一行 stock_master（就绪库）。"""
    from lake import conn as lconn

    con = lconn.open(db_path)
    con.execute(
        "INSERT INTO stock_master (ts_code,name,industry_csric2,board,is_st,"
        "source,fetched_at,data_version) VALUES "
        "('sh.601398','工商银行','J66','主板',0,'t','2026-09-14 00:00:00','v6.0')")
    con.close()


class _FakeDuckDBModule:
    """sys.modules['duckdb'] 替身：connect 按配置抛错/返回 fake 连接。

    为什么用 sys.modules 注入而不是 monkeypatch conn._connect_or_raise_invalid：
    归一逻辑**就在** _connect_or_raise_invalid 里，要测的就是它——只能从 duckdb.connect
    这一层注入失败文案（与 D-1 测试的 fake 思路一致）。抛的是**真** _duckdb.IOException
    （duckdb 包真实异常类），保证 str(exc) 文案匹配路径与生产完全一致。
    """

    __version__ = "1.5.5-fake"

    def __init__(self, fail_exc=None):
        self.fail_exc = fail_exc
        self.connect_calls = []

    def connect(self, path, *a, **k):
        self.connect_calls.append(path)
        if self.fail_exc is not None:
            raise self.fail_exc
        return _FakeCon()


class _FakeCon:
    def close(self):
        pass


def _patch_fake_duckdb(monkeypatch, fail_exc=None):
    fake = _FakeDuckDBModule(fail_exc)
    monkeypatch.setitem(sys.modules, "duckdb", fake)
    return fake


# ===========================================================================
# A. conn 层：文案匹配 / PID 解析 / 三态探测（fake 锁，零子进程）
# ===========================================================================
def test_conn_locked_markers_match_real_message():
    """实测生产锁文案 → _is_locked_db_error True；坏文件/普通 IO Error → False。"""
    from lake import conn as lconn

    assert lconn._is_locked_db_error(duckdb.IOException(LOCK_MSG_REAL)) is True
    assert lconn._is_locked_db_error(duckdb.IOException(LOCK_MSG_NO_PID)) is True
    # D-1 坏文件文案不得误判为 locked（判定顺序回归）
    assert lconn._is_locked_db_error(duckdb.IOException(INVALID_DB_MSG)) is False
    # 只命中单个 marker 的普通 IO Error → 不是锁
    assert lconn._is_locked_db_error(
        duckdb.IOException("IO Error: Could not set lock on fd")) is False


def test_conn_parse_lock_holder_pid():
    """PID 解析：实测文案 → 1003318；无 PID / 空 → None（降级，不猜不抛）。"""
    from lake import conn as lconn

    assert lconn.parse_lock_holder_pid(LOCK_MSG_REAL) == 1003318
    assert lconn.parse_lock_holder_pid(LOCK_MSG_NO_PID) is None
    assert lconn.parse_lock_holder_pid("") is None
    assert lconn.parse_lock_holder_pid(None) is None


def test_conn_connect_existing_locked_message_raises_lake_locked(tmp_path, monkeypatch):
    """connect_existing 收到锁文案 → LakeLocked（holder_pid=1003318，db_path 透传）。"""
    from lake import conn as lconn

    p = _nonzero_db_file(tmp_path)
    _patch_fake_duckdb(monkeypatch, duckdb.IOException(LOCK_MSG_REAL))
    with pytest.raises(lconn.LakeLocked) as ei:
        lconn.connect_existing(p)
    assert ei.value.db_path == p
    assert ei.value.holder_pid == 1003318


def test_conn_connect_existing_locked_no_pid_degrades_none(tmp_path, monkeypatch):
    """锁文案无 PID → LakeLocked.holder_pid=None（解析失败降级，不抛）。"""
    from lake import conn as lconn

    p = _nonzero_db_file(tmp_path, "noid.duckdb")
    _patch_fake_duckdb(monkeypatch, duckdb.IOException(LOCK_MSG_NO_PID))
    with pytest.raises(lconn.LakeLocked) as ei:
        lconn.connect_existing(p)
    assert ei.value.holder_pid is None


def test_conn_locked_not_confused_with_invalid_file(tmp_path, monkeypatch):
    """判定顺序：坏文件文案仍归一 LakeInvalidFile（D-1 不回退），锁文案才归一 LakeLocked。"""
    from lake import conn as lconn

    p = _nonzero_db_file(tmp_path, "order.duckdb")
    _patch_fake_duckdb(monkeypatch, duckdb.IOException(INVALID_DB_MSG))
    with pytest.raises(lconn.LakeInvalidFile):
        lconn.connect_existing(p)


def test_conn_probe_db_state_three_states(tmp_path, monkeypatch):
    """probe_db_state：缺文件/坏库 → unavailable；锁文案 → locked；可连 → ok。"""
    from lake import conn as lconn

    missing = str(tmp_path / "nope" / "m.duckdb")
    assert lconn.probe_db_state(missing) == "unavailable"  # 不 connect（B-1 约定）

    p = _nonzero_db_file(tmp_path, "probe.duckdb")
    _patch_fake_duckdb(monkeypatch, duckdb.IOException(LOCK_MSG_REAL))
    assert lconn.probe_db_state(p) == "locked"

    _patch_fake_duckdb(monkeypatch, duckdb.IOException(INVALID_DB_MSG))
    assert lconn.probe_db_state(p) == "unavailable"

    fake = _patch_fake_duckdb(monkeypatch, None)  # connect 成功
    assert lconn.probe_db_state(p) == "ok"
    assert len(fake.connect_calls) >= 1


def test_conn_lake_locked_is_lake_unavailable_subclass():
    """LakeLocked ⊂ LakeUnavailable：既有 except LakeUnavailable 兜底行为不变。"""
    from lake import conn as lconn

    assert issubclass(lconn.LakeLocked, lconn.LakeUnavailable)


# ===========================================================================
# B. web_api 单元级：locked → 数据端点 409 新 error / status 200 backfill_in_progress
# ===========================================================================
def test_web_locked_all_data_endpoints_409_new_error(tmp_path, monkeypatch):
    """locked × 4 数据端点：全部 LakeBackfillInProgress(409)（不是 LakeNotInitialized）。"""
    import lake.web_api as wapi
    from lake import conn as lconn

    p = _nonzero_db_file(tmp_path, "web_locked.duckdb")
    monkeypatch.setattr(lconn, "default_db_path", lambda: p)
    monkeypatch.setattr(
        lconn, "connect_existing",
        lambda path=None: (_ for _ in ()).throw(lconn.LakeLocked(p, 12345)))
    for fn in (wapi.search, wapi.industries, wapi.market):
        with pytest.raises(wapi.LakeBackfillInProgress) as ei:
            fn()
        assert ei.value.status_code == 409
        assert ei.value.holder_pid == 12345
    with pytest.raises(wapi.LakeBackfillInProgress) as ei:
        wapi.stock_detail("sh.601398")
    assert ei.value.status_code == 409


def test_web_locked_handler_contract_body_top_level():
    """handler 契约体：顶层 error/hint/lock_holder_pid，**无 detail 键**。"""
    import lake.web_api as wapi

    resp = wapi._lake_backfill_in_progress_handler(
        None, wapi.LakeBackfillInProgress("/tmp/x.duckdb", 12345))
    assert resp.status_code == 409
    body = json.loads(resp.body.decode())
    assert body["error"] == "lake_backfill_in_progress"
    assert body["hint"] == "灌数进行中，稍后重试"
    assert body["lock_holder_pid"] == 12345
    assert "detail" not in body, f"不得有 detail 键（绕过契约体）: {body}"


def test_web_status_locked_200_backfill_in_progress_tasks_from_progress_file(
        tmp_path, monkeypatch):
    """/status locked → 200 + initialized=true + backfill_in_progress=true +
    lock_holder_pid；coverage/tasks/updated_at **来自 progress 文件**（不受锁影响）。"""
    import lake.web_api as wapi
    from lake import conn as lconn

    p = _nonzero_db_file(tmp_path, "status_locked.duckdb")
    prog = _write_progress(tmp_path)
    monkeypatch.setattr(lconn, "default_db_path", lambda: p)
    monkeypatch.setattr(lconn, "progress_path", lambda: prog)
    monkeypatch.setattr(
        lconn, "connect_existing",
        lambda path=None: (_ for _ in ()).throw(lconn.LakeLocked(p, 4321)))

    d = wapi.status()
    assert d["installed"] is True
    assert d["initialized"] is True, f"灌数持锁 ≠ 未初始化: {d}"
    assert d["backfill_in_progress"] is True
    assert d["lock_holder_pid"] == 4321
    # coverage/tasks 必须来自 progress 文件（降级读取，正是灌数进度）
    with open(prog, "r", encoding="utf-8") as f:
        expect = json.load(f)
    assert d["tasks"] == expect["tasks"]
    assert d["coverage"] == expect["coverage"]
    assert d["updated_at"] == expect["updated_at"]
    # 三态互不串味：locked 响应不得带"未初始化"的 error/hint 键
    assert "error" not in d and "hint" not in d, f"locked 状态混入未初始化字段: {d}"


def test_web_status_locked_pid_parse_failure_still_backfill(tmp_path, monkeypatch):
    """PID 解析失败（None）→ 仍报 backfill_in_progress=true + lock_holder_pid=None，
    **不得**退回误报 initialized=false（brief：降级 None，不猜）。"""
    import lake.web_api as wapi
    from lake import conn as lconn

    p = _nonzero_db_file(tmp_path, "status_nopid.duckdb")
    monkeypatch.setattr(lconn, "default_db_path", lambda: p)
    monkeypatch.setattr(lconn, "progress_path", lambda: str(tmp_path / "absent.json"))
    monkeypatch.setattr(
        lconn, "connect_existing",
        lambda path=None: (_ for _ in ()).throw(lconn.LakeLocked(p, None)))

    d = wapi.status()
    assert d["initialized"] is True
    assert d["backfill_in_progress"] is True
    assert d["lock_holder_pid"] is None
    # progress 文件缺失 → fail-open 空结构（load_progress 既有语义）
    assert d["tasks"] == [] and d["coverage"] == {}


# ===========================================================================
# C. 真未初始化回归：v6.0.3 行为逐字节一致（防 D-1/B-1 被回退）
# ===========================================================================
def _v603_uninitialized_status_body() -> dict:
    """v6.0.3 的 /status 未就绪响应体（逐字节基准）。"""
    import lake.web_api as wapi

    return {
        "installed": True,
        "duckdb_version": getattr(duckdb, "__version__", "?"),
        "initialized": False,
        "error": "lake_not_initialized",
        "hint": wapi.INIT_HINT,
        "coverage": {},
        "tasks": [],
        "updated_at": None,
    }


@pytest.mark.parametrize("kind", ["missing", "empty_schema", "zero_byte", "garbage"])
def test_web_status_uninitialized_byte_identical_v603(tmp_path, monkeypatch, kind):
    """真未初始化 ×4 种（缺文件/空库/0字节/损坏）：/status 响应体与 v6.0.3 **逐字节一致**，
    且不得混入 backfill_in_progress / lock_holder_pid（三态互不串味）。"""
    import lake.web_api as wapi
    from lake import conn as lconn

    if kind == "missing":
        p = str(tmp_path / "nope" / "lake.duckdb")
    elif kind == "empty_schema":
        # 合法库但**无 core 表 stock_master**（v6.0.3 "空库/未 init" 口径）
        p = str(tmp_path / f"{kind}.duckdb")
        con = duckdb.connect(p)
        con.execute("CREATE TABLE other_table(x INT)")
        con.close()
    elif kind == "zero_byte":
        p = str(tmp_path / f"{kind}.duckdb")
        open(p, "wb").close()
    else:  # garbage
        p = str(tmp_path / f"{kind}.duckdb")
        with open(p, "wb") as f:
            f.write(b"not a duckdb file at all\n")

    monkeypatch.setattr(lconn, "default_db_path", lambda: p)
    d = wapi.status()
    assert d == _v603_uninitialized_status_body(), \
        f"{kind}: /status 未就绪响应必须与 v6.0.3 逐字节一致:\n{d}"
    assert "backfill_in_progress" not in d and "lock_holder_pid" not in d


@pytest.mark.parametrize("kind", ["missing", "zero_byte", "garbage"])
def test_web_data_endpoints_uninitialized_unchanged_v603(tmp_path, monkeypatch, kind):
    """真未初始化 × 数据端点：仍 LakeNotInitialized(409)（不是 LakeBackfillInProgress），
    handler 契约体与 v6.0.3 一致。"""
    import lake.web_api as wapi
    from lake import conn as lconn

    if kind == "missing":
        p = str(tmp_path / "nope2" / "lake.duckdb")
    elif kind == "zero_byte":
        p = str(tmp_path / f"{kind}2.duckdb")
        open(p, "wb").close()
    else:
        p = str(tmp_path / f"{kind}2.duckdb")
        with open(p, "wb") as f:
            f.write(b"garbage bytes here\n")

    monkeypatch.setattr(lconn, "default_db_path", lambda: p)
    for fn in (wapi.search, wapi.industries, wapi.market):
        with pytest.raises(wapi.LakeNotInitialized) as ei:
            fn()
        assert ei.value.status_code == 409
    # handler 契约体逐字节（v6.0.3 B-1/D-1 形状）
    resp = wapi._lake_not_initialized_handler(None, wapi.LakeNotInitialized(p))
    body = json.loads(resp.body.decode())
    assert body == {"error": "lake_not_initialized", "hint": wapi.INIT_HINT,
                    "db_path": p}


def test_web_install_registers_both_handlers():
    """install() 同时注册两个 handler（app 级，FastAPI 0.141 router 无 exception_handler）。"""
    from fastapi import FastAPI

    import lake.web_api as wapi

    app = FastAPI()
    wapi.install(app)
    handlers = getattr(app, "exception_handlers", {})
    assert wapi.LakeNotInitialized in handlers
    assert wapi.LakeBackfillInProgress in handlers


# ===========================================================================
# D. HTTP 层（真 uvicorn + stdlib http.client，与 v6.0.3 测试同口径）：三态契约体
#
# 为什么不用 FastAPI TestClient：TestClient 依赖 httpx/httpx2，而两者都不在
# pyproject/uv.lock（fresh `uv sync` 后不可用，测试会在 tester 环境 skip/error）。
# 仓库既有 lake HTTP 测试（test_lake_v603_*）统一走 subprocess uvicorn + http.client——
# 零新增依赖、且验证的是 web/app.py 真实挂载路径（比 TestClient 独立 app 更强证据）。
# ===========================================================================
def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _http_get(port: int, path: str):
    import http.client

    c = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        c.request("GET", path)
        r = c.getresponse()
        return r.status, r.read().decode()
    finally:
        c.close()


def _wait_ready(port: int, path: str, tries: int = 60) -> bool:
    for _ in range(tries):
        try:
            s, _ = _http_get(port, path)
            if s in (200, 409):
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    return False


def _start_lake_server(port: int, db_path: str, progress_path: Optional[str],
                       fake_lock_pid: object = "__none__"):
    """起 uvicorn（web.app 真实挂载）；子进程内按参数注入库路径 / progress / fake 锁。

    :param fake_lock_pid: "__none__"=不注入（走真实 connect）；int/None=fake 锁——
        connect_existing 直接抛 LakeLocked(db, pid)（None 模拟 PID 解析失败降级）。
    """
    # 逐行 f-string 插值（不用 .format()——避免与代码里的 {…} 占位符互相干扰）
    code_lines = [
        f"import sys; sys.path.insert(0, {REPO_ROOT!r})",
        "from lake import conn as _lc",
        f"_lc.default_db_path = lambda: {db_path!r}",
    ]
    if progress_path is not None:
        code_lines.append(f"_lc.progress_path = lambda: {progress_path!r}")
    if fake_lock_pid != "__none__":
        code_lines.append(
            f"_lc.connect_existing = lambda path=None: (_ for _ in ()).throw("
            f"_lc.LakeLocked({db_path!r}, {fake_lock_pid!r}))")
    code_lines += [
        "import uvicorn",
        "from web.app import app",
        f"uvicorn.run(app, host='127.0.0.1', port={port}, log_level='error')",
    ]
    srv = subprocess.Popen(
        [PY, "-c", "\n".join(code_lines)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return srv


def test_http_locked_status_200_and_data_endpoints_409_new_error(tmp_path):
    """HTTP 层 locked：/status → 200 backfill_in_progress（tasks 来自 progress 文件）；
    4 数据端点 → 409 + **顶层** error=lake_backfill_in_progress、无 detail 键。"""
    import json as _json

    p = _nonzero_db_file(tmp_path, "http_locked.duckdb")
    prog = _write_progress(tmp_path)
    port = _free_port()
    srv = _start_lake_server(port, p, prog, fake_lock_pid=777)
    try:
        assert _wait_ready(port, "/api/lake/status"), "uvicorn 未在超时内就绪"

        s, b = _http_get(port, "/api/lake/status")
        assert s == 200, f"/status locked 应 200，实际 {s}"
        d = _json.loads(b)
        assert d["initialized"] is True
        assert d["backfill_in_progress"] is True
        assert d["lock_holder_pid"] == 777
        with open(prog, "r", encoding="utf-8") as f:
            expect = _json.load(f)
        assert d["tasks"] == expect["tasks"], "tasks 必须来自 progress 文件"
        assert d["coverage"] == expect["coverage"]

        for path in ("/api/lake/search?q=abc", "/api/lake/industries",
                     "/api/lake/market?page=1", "/api/lake/stock/sh.601398"):
            s, b = _http_get(port, path)
            assert s == 409, f"{path} locked 应 409，实际 {s}"
            bd = _json.loads(b)
            assert bd.get("error") == "lake_backfill_in_progress", \
                f"{path} 顶层 error 错误: {bd}"
            assert bd.get("hint") == "灌数进行中，稍后重试"
            assert bd.get("lock_holder_pid") == 777
            assert "detail" not in bd, f"{path} 不得有 detail 键: {bd}"
    finally:
        _stop_server(srv)


def test_http_locked_pid_parse_failure_still_backfill(tmp_path):
    """HTTP 层 locked 且 PID 解析失败（None）：/status 仍 backfill_in_progress=true +
    lock_holder_pid=null，**不**退回误报 initialized=false。"""
    import json as _json

    p = _nonzero_db_file(tmp_path, "http_nopid.duckdb")
    port = _free_port()
    srv = _start_lake_server(port, p, None, fake_lock_pid=None)
    try:
        assert _wait_ready(port, "/api/lake/status"), "uvicorn 未在超时内就绪"
        s, b = _http_get(port, "/api/lake/status")
        assert s == 200
        d = _json.loads(b)
        assert d["initialized"] is True, f"PID 解析失败不得退回未初始化: {d}"
        assert d["backfill_in_progress"] is True
        assert d["lock_holder_pid"] is None
    finally:
        _stop_server(srv)


def test_http_uninitialized_contract_unchanged_no_crosstalk(tmp_path):
    """HTTP 层真未初始化（缺库）：/status 200 initialized=false（v6.0.3 逐字节），
    数据端点 409 lake_not_initialized；**两态响应互不串味**。"""
    import json as _json

    import lake.web_api as wapi

    missing = str(tmp_path / "nope" / "lake.duckdb")
    port = _free_port()
    srv = _start_lake_server(port, missing, None)
    try:
        assert _wait_ready(port, "/api/lake/status"), "uvicorn 未在超时内就绪"

        s, b = _http_get(port, "/api/lake/status")
        assert s == 200
        d = _json.loads(b)
        expect = {
            "installed": True,
            "duckdb_version": getattr(duckdb, "__version__", "?"),
            "initialized": False,
            "error": "lake_not_initialized",
            "hint": wapi.INIT_HINT,
            "coverage": {},
            "tasks": [],
            "updated_at": None,
        }
        assert d == expect, f"未初始化 /status 必须与 v6.0.3 逐字节一致: {d}"
        assert "backfill_in_progress" not in d and "lock_holder_pid" not in d

        for path in ("/api/lake/search?q=abc", "/api/lake/industries",
                     "/api/lake/market?page=1", "/api/lake/stock/sh.601398"):
            s, b = _http_get(port, path)
            assert s == 409
            bd = _json.loads(b)
            assert bd.get("error") == "lake_not_initialized", \
                f"{path} 未初始化必须仍是 lake_not_initialized: {bd}"
            assert "backfill_in_progress" not in bd and "lock_holder_pid" not in bd
    finally:
        _stop_server(srv)


def _stop_server(srv):
    srv.terminate()
    try:
        srv.wait(timeout=10)
    except subprocess.TimeoutExpired:
        srv.kill()
        srv.wait(timeout=10)


# ===========================================================================
# E. 真跨进程锁（holder 子进程 RW 持锁 tmp 库）：真实 duckdb 锁文案 + 真实 PID 解析
# ===========================================================================
def test_real_cross_process_lock_full_cycle(tmp_path):
    """端到端（零网络、tmp 库）：子进程 RW 持锁 → /status backfill_in_progress=true +
    lock_holder_pid == 子进程真实 PID（**真 duckdb 锁文案**解析）+ 数据端点 409 新 error；
    持锁释放后 → /status 恢复 initialized=true 无 backfill 键、/market 200。"""
    import json as _json

    p = str(tmp_path / "real_lock.duckdb")
    _seed_ready_db(p)
    prog = _write_progress(tmp_path)
    port = _free_port()

    holder_code = (
        "import duckdb, sys, time\n"
        "c = duckdb.connect(sys.argv[1])\n"
        "print('READY', flush=True)\n"
        "time.sleep(30)\n"
    )
    holder = subprocess.Popen([PY, "-c", holder_code, p],
                              stdout=subprocess.PIPE, text=True)
    srv = _start_lake_server(port, p, prog)  # 无 fake 锁——走**真实** duckdb 锁文案
    try:
        line = holder.stdout.readline()
        assert line.strip() == "READY", f"holder 未就绪: {line!r}"
        assert _wait_ready(port, "/api/lake/status"), "uvicorn 未在超时内就绪"

        # 轮询等锁生效（holder connect 完成到 flock 落盘有毫秒级窗口）
        d = None
        for _ in range(40):
            s, b = _http_get(port, "/api/lake/status")
            assert s == 200
            d = _json.loads(b)
            if d.get("backfill_in_progress"):
                break
            time.sleep(0.25)
        assert d and d.get("backfill_in_progress") is True, \
            f"持锁期间 /status 必须 backfill_in_progress=true: {d}"
        assert d["initialized"] is True
        # **真实** duckdb 锁文案 → 解析出的 PID 必须等于 holder 子进程 PID
        assert d["lock_holder_pid"] == holder.pid, \
            f"lock_holder_pid 应=持锁子进程 PID {holder.pid}，实际 {d['lock_holder_pid']}"

        s, b = _http_get(port, "/api/lake/market?page=1")
        assert s == 409
        bd = _json.loads(b)
        assert bd["error"] == "lake_backfill_in_progress"
        assert bd["lock_holder_pid"] == holder.pid
    finally:
        _stop_server(srv)
        holder.terminate()
        try:
            holder.wait(timeout=15)
        except subprocess.TimeoutExpired:
            holder.kill()
            holder.wait(timeout=15)

    # 锁释放 → 恢复就绪态（无 backfill 键），数据端点 200
    srv = _start_lake_server(port, p, prog)
    try:
        assert _wait_ready(port, "/api/lake/status"), "uvicorn 未在超时内就绪"
        d = None
        for _ in range(40):
            s, b = _http_get(port, "/api/lake/status")
            d = _json.loads(b)
            if "backfill_in_progress" not in d:
                break
            time.sleep(0.25)
        assert "backfill_in_progress" not in d, f"锁释放后不得残留 backfill 键: {d}"
        assert d["initialized"] is True
        s, _ = _http_get(port, "/api/lake/market?page=1")
        assert s == 200, f"锁释放后 /market 应 200，实际 {s}"
    finally:
        _stop_server(srv)


# ===========================================================================
# F. 前端三态文案契约（防 JS/HTML 与后端字段脱节）
# ===========================================================================
def test_frontend_contract_backfill_block_and_error_value():
    """index.html 有 #lake-backfill 状态块；app.js 读 backfill_in_progress /
    lock_holder_pid 并识别 lake_backfill_in_progress error 值。"""
    html = open(os.path.join(REPO_ROOT, "web", "static", "index.html"),
                encoding="utf-8").read()
    js = open(os.path.join(REPO_ROOT, "web", "static", "app.js"),
              encoding="utf-8").read()
    assert 'id="lake-backfill"' in html, "index.html 缺灌数中状态块 #lake-backfill"
    assert "数据灌入中" in js and "数据灌入中" in html
    assert "backfill_in_progress" in js
    assert "lock_holder_pid" in js
    assert "lake_backfill_in_progress" in js, "app.js 未识别新 error 值（三态会串味）"
