# -*- coding: utf-8 -*-
"""test_lake_v603_d1 —— v6.0.3 rework D-1 回归：0 字节/无效库文件一致降级。

缺陷（tester D-1，TL 复核确认）：B-1 已覆盖「缺库文件」「未 init_schema 空库」，
唯一漏网场景 = **0 字节空文件**（存在但连 duckdb 都打不开）：
- Web 数据端点：_con() 的 exists 探测为 True → connect_existing 抛
  _duckdb.IOException("...not a valid DuckDB database file!") → 通用 except →
  **503 + detail**（绕过 LakeNotInitialized→409 顶层契约体），与 /status 的
  200 initialized=false 自相矛盾（B-1 本要消除的问题）。
- CLI：`python scripts/lake_backfill.py --db <0字节> init` → _open_db→duckdb.connect
  抛裸 traceback rc=1。

修复（R1/R2）：lake.conn 在 connect 层归一——size==0 探测 + IO Error 文案匹配 →
:class:`lake.conn.LakeInvalidFile`；web_api._con() 转 LakeNotInitialized(409)，
CLI main/_run_unlocked 转友好报错 EXIT_LAKE_UNAVAILABLE=3。

验收点（R3）：
- Web：0 字节库 × 4 数据端点 → 409 + **顶层** error=lake_not_initialized + hint、
  **无 detail 键**；/status → 200 initialized=false（单元级 + uvicorn 真 HTTP）。
- CLI：0 字节库 init/status → rc=3（≠0）、stderr 友好信息、**无裸 traceback**。

纪律：全离线，库一律 tmp_path（touch 出 0 字节文件），不碰 data/lake/lake.duckdb。
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import time

import pytest

duckdb = pytest.importorskip("duckdb")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)
PY = sys.executable or os.path.join(REPO_ROOT, ".venv", "bin", "python")


def _zero_byte_db(tmp_path, name: str = "zero.duckdb") -> str:
    """touch 出 0 字节库文件（tester repro 的等价物）。"""
    p = str(tmp_path / name)
    with open(p, "wb"):
        pass
    assert os.path.getsize(p) == 0
    return p


def _garbage_db(tmp_path, name: str = "garbage.duckdb") -> str:
    """非 0 字节但内容无效的库文件（走 catch 归一路径，而非 size 探测路径）。"""
    p = str(tmp_path / name)
    with open(p, "wb") as f:
        f.write(b"this is not a duckdb file at all, just garbage bytes\n")
    return p


# ===========================================================================
# R2 前置：conn 层归一（LakeInvalidFile）
# ===========================================================================
def test_conn_zero_byte_file_raises_lake_invalid_file(tmp_path):
    """connect_existing(0字节) → LakeInvalidFile（size 探测路径，不 connect）。"""
    from lake import conn as lconn

    p = _zero_byte_db(tmp_path)
    with pytest.raises(lconn.LakeInvalidFile) as ei:
        lconn.connect_existing(p)
    assert ei.value.db_path == p


def test_conn_garbage_file_raises_lake_invalid_file(tmp_path):
    """connect_existing(垃圾内容文件) → LakeInvalidFile（IO Error 文案归一路径）。"""
    from lake import conn as lconn

    p = _garbage_db(tmp_path)
    with pytest.raises(lconn.LakeInvalidFile):
        lconn.connect_existing(p)


def test_conn_open_zero_byte_raises_lake_invalid_file_not_traceback(tmp_path):
    """open(0字节)（CLI 写路径）→ LakeInvalidFile（不裸抛 _duckdb.IOException）。"""
    from lake import conn as lconn

    p = _zero_byte_db(tmp_path, "zero_open.duckdb")
    with pytest.raises(lconn.LakeInvalidFile):
        lconn.open(p)
    # open 不得留下半成品副作用：文件仍 0 字节（未 connect、未 init_schema）
    assert os.path.getsize(p) == 0


def test_conn_lake_invalid_file_is_lake_unavailable_subclass():
    """LakeInvalidFile ⊂ LakeUnavailable：既有 except LakeUnavailable 兜底行为不变。"""
    from lake import conn as lconn

    assert issubclass(lconn.LakeInvalidFile, lconn.LakeUnavailable)


# ===========================================================================
# R1：Web 数据端点 0 字节库 → 409（单元级）
# ===========================================================================
def test_d1_web_zero_byte_all_data_endpoints_409(tmp_path, monkeypatch):
    """0 字节库 × 4 数据端点：全部 LakeNotInitialized(409)，不再 503。"""
    import lake.web_api as wapi
    from lake import conn as lconn

    zero = _zero_byte_db(tmp_path)
    monkeypatch.setattr(lconn, "default_db_path", lambda: zero)
    for fn in (wapi.search, wapi.industries, wapi.market):
        with pytest.raises(wapi.LakeNotInitialized) as ei:
            fn()
        assert ei.value.status_code == 409
    with pytest.raises(wapi.LakeNotInitialized) as ei:
        wapi.stock_detail("sh.601398")
    assert ei.value.status_code == 409


def test_d1_web_garbage_file_all_data_endpoints_409(tmp_path, monkeypatch):
    """内容损坏（非 0 字节）库文件：同样 409（catch 归一路径）。"""
    import lake.web_api as wapi
    from lake import conn as lconn

    garbage = _garbage_db(tmp_path)
    monkeypatch.setattr(lconn, "default_db_path", lambda: garbage)
    for fn in (wapi.search, wapi.industries, wapi.market):
        with pytest.raises(wapi.LakeNotInitialized):
            fn()
    with pytest.raises(wapi.LakeNotInitialized):
        wapi.stock_detail("sh.601398")


def test_d1_web_status_zero_byte_reports_initialized_false(tmp_path, monkeypatch):
    """/status 对 0 字节库：不抛，如实 initialized=false（与数据端点 409 一致降级）。"""
    import lake.web_api as wapi
    from lake import conn as lconn

    zero = _zero_byte_db(tmp_path)
    monkeypatch.setattr(lconn, "default_db_path", lambda: zero)
    d = wapi.status()
    assert d["installed"] is True
    assert d["initialized"] is False, f"0 字节库 status 必须 initialized=false: {d}"
    assert d["coverage"] == {}
    assert d["tasks"] == []
    assert d.get("error") == "lake_not_initialized"


def test_d1_web_con_zero_byte_does_not_mutate_file(tmp_path, monkeypatch):
    """_con() 对 0 字节库只读探测：文件保持 0 字节（Web 绝不代建/改写库）。"""
    import lake.web_api as wapi
    from lake import conn as lconn

    zero = _zero_byte_db(tmp_path)
    monkeypatch.setattr(lconn, "default_db_path", lambda: zero)
    with pytest.raises(wapi.LakeNotInitialized):
        wapi._con()
    assert os.path.getsize(zero) == 0


# ===========================================================================
# R1：Web HTTP 层（uvicorn 真起 web.app）——顶层契约体 + 无 detail 键
# ===========================================================================
def test_d1_http_zero_byte_contract_body_over_real_http(tmp_path):
    """HTTP 层：0 字节库 × 4 数据端点 → 409 + **顶层** error/hint、**无 detail 键**；
    /status → 200 initialized=false（数据端点与 status 不再自相矛盾）。"""
    port = _free_port()
    zero = _zero_byte_db(tmp_path, "http_zero.duckdb")
    server_code = (
        "import sys; sys.path.insert(0, {repo!r})\n"
        "from lake import conn as _lc\n"
        "_lc.default_db_path = lambda: {db!r}\n"  # 指向 0 字节库 → D-1 未就绪
        "import uvicorn\n"
        "from web.app import app\n"
        "uvicorn.run(app, host='127.0.0.1', port={port}, log_level='error')\n"
    ).format(repo=REPO_ROOT, db=zero, port=port)
    srv = subprocess.Popen([PY, "-c", server_code],
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        ready = _wait_ready(port, "/api/lake/status")
        assert ready, "uvicorn 未在超时内就绪"

        s_status, b_status = _http_get(port, "/api/lake/status")
        assert s_status == 200, f"/status 应 200（健康检查），实际 {s_status}"
        d_status = json.loads(b_status)
        assert d_status["initialized"] is False

        # 4 个数据端点：409 + 顶层 error/hint、无 detail 键（契约体形状）
        for path in ("/api/lake/search?q=abc", "/api/lake/industries",
                     "/api/lake/market?page=1", "/api/lake/stock/sh.601398"):
            s, b = _http_get(port, path)
            assert s == 409, f"{path} 应 409，实际 {s} :: {b[:120]}"
            d = json.loads(b)
            assert d.get("error") == "lake_not_initialized", \
                f"{path} 顶层 error 缺失: {d}"
            assert "init" in d.get("hint", ""), f"{path} 顶层 hint 缺失: {d}"
            assert "detail" not in d, f"{path} 不得有 detail 键（绕过契约体）: {d}"
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=10)
        except subprocess.TimeoutExpired:
            srv.kill()
            srv.wait(timeout=10)


# ===========================================================================
# R2：CLI 0 字节库 → 友好报错 rc≠0、无裸 traceback（subprocess 真跑）
# ===========================================================================
def _run_cli(argv):
    return subprocess.run([PY, os.path.join(SCRIPTS_DIR, "lake_backfill.py"), *argv],
                          cwd=REPO_ROOT, capture_output=True, timeout=120)


def test_d1_cli_init_zero_byte_friendly_error_rc3(tmp_path):
    """`--db <0字节> init` → rc=3（≠0）、stderr 友好信息、无裸 traceback。"""
    zero = _zero_byte_db(tmp_path, "cli_zero.duckdb")
    r = _run_cli(["--db", zero, "init"])
    err = r.stderr.decode()
    assert r.returncode != 0, f"0 字节库 init 必须非零退出，实际 rc={r.returncode}"
    assert r.returncode == 3, f"应复用 EXIT_LAKE_UNAVAILABLE=3，实际 {r.returncode}"
    assert "Traceback" not in err, f"不得裸 traceback: {err[-500:]}"
    assert "_duckdb.IOException" not in err and "not a valid DuckDB database file" not in err, \
        f"不得透传 duckdb 原始报错: {err[-500:]}"
    assert "错误" in err and zero in err, f"stderr 应含友好中文报错 + 库路径: {err[-300:]}"
    # 文件未被改写（driver 只读探测后退出）
    assert os.path.getsize(zero) == 0


def test_d1_cli_status_zero_byte_friendly_error_rc3(tmp_path):
    """`--db <0字节> status`（只读路径同走 _open_db）→ rc=3、无 traceback。"""
    zero = _zero_byte_db(tmp_path, "cli_status.duckdb")
    r = _run_cli(["--db", zero, "status"])
    err = r.stderr.decode()
    assert r.returncode == 3, f"status 对 0 字节库应 rc=3，实际 {r.returncode}"
    assert "Traceback" not in err, f"不得裸 traceback: {err[-500:]}"
    assert "错误" in err and zero in err


def test_d1_cli_garbage_file_init_friendly_error_rc3(tmp_path):
    """内容损坏（非 0 字节）库文件：init 同样友好 rc=3（catch 归一路径）。"""
    garbage = _garbage_db(tmp_path, "cli_garbage.duckdb")
    r = _run_cli(["--db", garbage, "init"])
    err = r.stderr.decode()
    assert r.returncode == 3, f"垃圾文件 init 应 rc=3，实际 {r.returncode}"
    assert "Traceback" not in err, f"不得裸 traceback: {err[-500:]}"


# ===========================================================================
# HTTP helpers（复用 v6.0.3 B-1 口径）
# ===========================================================================
def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _http_get(port: int, path: str):
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
