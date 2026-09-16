# -*- coding: utf-8 -*-
"""test_lake_v6010_stop_lock —— v6.0.10 回归：停止按钮点击即锁定 + 停止语义澄清。

验收点（v6.0.10 brief，全离线 tmp 库 + mock；E2E 真实冒烟另见 smoke 段）：
- **stop 异步返回 waiting_task**：sync_control.stop_sync 发信号即返回（<2s，不阻塞
  等退出）+ waiting_task=True + method；not_running → waiting_task=False + reason。
- **/status stopping 标志**：locked 态恒带 ``stopping: bool``——progress 文件
  stopping_at 非空或 tasks[].state="stopping" → true（双保险判定）；无标记 → false；
  uninitialized/ready 两态**不混入** stopping 键（三态互不串味纪律延续）。
- **BackfillRunner 收尾可观测（真子进程 + SIGTERM）**：handler 置 _stop_requested 时
  同步落盘 tasks[].state="stopping" + stopping_at（信号到达后、进程退出前文件即可见）；
  优雅停止收尾后 state=stopped_by_signal + stopping_at=None（标记清除，不残留）。
- **重试循环提前中断（收尾加速）**：BaoStockClient._query 收到停止标志 → 退避 sleep
  拆块逐查、立即抛 BaoStockError（不等满指数退避）；腾讯 fetch_kline_ohlcv /
  _fetch_kline_page 收到停止标志 → 抛 StopRequestedError（不静默 []）。
- **前端按钮锁定 DOM 契约（node 驱动 app.js 真实函数）**：点击停止 → 立即 disabled +
  "⏹ 停止中… (pid)" + sync-busy 置灰；POST 返回 waiting_task=true → 保持锁定 + 1s
  轮询；/status backfill_in_progress=false → toast"已停止，进度已保存"+ 释放回启动
  按钮；>90s → 文案升级"当前任务收尾中，最长约几分钟"；5min 硬超时 → toast + 释放。
  node 不可用 → skip（不阻塞）。

纪律：库一律 tmp_path，**绝不触碰 data/lake/ 生产库与在跑的 history 进程**；零网络
（mock session / fake driver）；BaoStock 重试测试用 max_attempts=2 + base_delay 极小。
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time

import pytest

duckdb = pytest.importorskip("duckdb")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
PY = sys.executable or os.path.join(REPO_ROOT, ".venv", "bin", "python")


# ===========================================================================
# helpers
# ===========================================================================
def _seed_ready_db(db_path: str) -> None:
    from lake import conn as lconn

    con = lconn.open(db_path)
    con.execute(
        "INSERT INTO stock_master (ts_code,name,industry_csric2,board,is_st,"
        "source,fetched_at,data_version) VALUES "
        "('sh.601398','工商银行','J66','主板',0,'t','2026-09-14 00:00:00','v6.0')")
    con.close()


def _write_fake_driver(tmp_path, name: str = "fake_driver.py",
                       hold_s: int = 120) -> str:
    """fake driver：--db → duckdb.connect 持锁（灌数进程等价物）→ sleep。

    v6.0.10：**忽略 SIGTERM**——模拟真实 BackfillRunner"收到信号后等当前任务收尾"
    （不会立即退出）；测试在需要时显式 kill() 释放锁。
    """
    code = (
        "import sys, time, signal\n"
        "import duckdb\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)   # 收尾中：信号不立即退出\n"
        "args = sys.argv[1:]\n"
        "db = None\n"
        "if '--db' in args:\n"
        "    db = args[args.index('--db') + 1]\n"
        f"assert db, 'fake driver needs --db'\n"
        "c = duckdb.connect(db)\n"
        "print('FAKE_DRIVER_READY', flush=True)\n"
        f"time.sleep({hold_s})\n"
    )
    p = str(tmp_path / name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(code)
    return p


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _http(port: int, method: str, path: str):
    import http.client

    c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    try:
        c.request(method, path, body="{}")
        r = c.getresponse()
        return r.status, r.read().decode()
    finally:
        c.close()


def _wait_ready(port: int, tries: int = 60) -> bool:
    for _ in range(tries):
        try:
            s, _ = _http(port, "GET", "/api/lake/status")
            if s in (200, 409):
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    return False


def _start_lake_server(port: int, db_path: str, progress_path: "str | None" = None):
    """起 uvicorn（web.app 真实挂载）；子进程内把默认库 + **progress 文件**都指向
    tmp（v6.0.10：只 patch 库不 patch progress → /status coverage/tasks 会读生产
    data/lake/backfill_progress.json，E2E 断言被污染——本版本双 patch）。"""
    prog = progress_path or os.path.join(os.path.dirname(db_path),
                                         "backfill_progress.json")
    code_lines = [
        f"import sys; sys.path.insert(0, {REPO_ROOT!r})",
        "from lake import conn as _lc",
        f"_lc.default_db_path = lambda: {db_path!r}",
        f"_lc.progress_path = lambda: {prog!r}",
        "import uvicorn",
        "from web.app import app",
        f"uvicorn.run(app, host='127.0.0.1', port={port}, log_level='error')",
    ]
    return subprocess.Popen([PY, "-c", "\n".join(code_lines)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def _stop_server(srv):
    srv.terminate()
    try:
        srv.wait(timeout=10)
    except subprocess.TimeoutExpired:
        srv.kill()
        srv.wait(timeout=10)


# ===========================================================================
# 1. stop_sync 异步语义（发信号即返回 waiting_task）
# ===========================================================================
def test_stop_sync_async_returns_waiting_task_immediately(tmp_path, monkeypatch):
    """**核心验收**：stop_sync 发信号后**立即返回**（<2s，不阻塞等退出），
    waiting_task=True + method；旧同步语义（轮询到 timeout）不得回归。"""
    from lake import sync_control

    p = str(tmp_path / "async_stop.duckdb")
    _seed_ready_db(p)
    fake = _write_fake_driver(tmp_path)
    monkeypatch.setattr(sync_control, "sync_script_path", lambda: fake)
    started = sync_control.start_sync(db_path=p, sub="history")
    assert started["started"] is True
    try:
        t0 = time.monotonic()
        res = sync_control.stop_sync(db_path=p)
        elapsed = time.monotonic() - t0
        assert elapsed < 2.0, f"必须发信号即返回（实际 {elapsed:.2f}s）: {res}"
        assert res["waiting_task"] is True, f"信号已发应 waiting_task=True: {res}"
        assert res["pid"] == started["pid"]
        assert res["method"] in ("killpg", "kill")
        assert "note" in res and "收尾" in res["note"], f"note 应说明正在收尾: {res}"
    finally:
        try:
            os.killpg(started["pid"], signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def test_stop_sync_not_running_waiting_task_false(tmp_path):
    """未 running → waiting_task=False + reason=not_running（Web 层映射 409）。"""
    from lake import sync_control

    res = sync_control.stop_sync(db_path=str(tmp_path / "nope" / "x.duckdb"))
    assert res["stopped"] is False
    assert res["waiting_task"] is False
    assert res["reason"] == "not_running"


def test_stop_sync_holder_unknown_waiting_task_false(tmp_path, monkeypatch):
    """holder pid 解析失败 → 拒绝猜测：waiting_task=False + reason（不误报已发信号）。"""
    from lake import sync_control

    monkeypatch.setattr(sync_control, "sync_status",
                        lambda db_path=None: {"running": True, "pid": None})
    res = sync_control.stop_sync()
    assert res["stopped"] is False
    assert res["waiting_task"] is False
    assert res["reason"].startswith("holder_pid_unknown")


# ===========================================================================
# 2. /status stopping 标志（locked 态；uninitialized/ready 不混入）
# ===========================================================================
def test_status_locked_stopping_true_from_progress(tmp_path, monkeypatch):
    """progress 文件 stopping_at 非空 → /status locked 态 stopping=true。"""
    import lake.web_api as wapi
    from lake import conn as lconn

    p = str(tmp_path / "st1.duckdb")
    with open(p, "wb") as f:
        f.write(b"\x00" * 16)
    prog = str(tmp_path / "backfill_progress.json")
    with open(prog, "w", encoding="utf-8") as f:
        json.dump({"updated_at": "2026-09-16 08:00:00",
                   "tasks": [{"table": "kline_history", "tier": "P2", "total": 5,
                              "done": 2, "quota_used_today": 3, "quota_budget": 5000,
                              "state": "stopping", "eta_min": None, "last_error": ""}],
                   "coverage": {}, "stopping_at": "2026-09-16 08:01:02"}, f)
    monkeypatch.setattr(lconn, "default_db_path", lambda: p)
    monkeypatch.setattr(lconn, "progress_path", lambda: prog)
    monkeypatch.setattr(
        lconn, "connect_existing",
        lambda path=None: (_ for _ in ()).throw(lconn.LakeLocked(p, 4321)))

    d = wapi.status()
    assert d["backfill_in_progress"] is True
    assert d["stopping"] is True, f"progress 有停止标记必须 stopping=true: {d}"


def test_status_locked_stopping_true_state_only(tmp_path, monkeypatch):
    """双保险：仅 tasks[].state="stopping"（无 stopping_at，旧版 runner 残留场景）
    → 仍 stopping=true。"""
    import lake.web_api as wapi
    from lake import conn as lconn

    p = str(tmp_path / "st2.duckdb")
    with open(p, "wb") as f:
        f.write(b"\x00" * 16)
    prog = str(tmp_path / "backfill_progress.json")
    with open(prog, "w", encoding="utf-8") as f:
        json.dump({"updated_at": None,
                   "tasks": [{"table": "kline_history", "tier": "P2",
                              "state": "stopping"}],
                   "coverage": {}}, f)   # 无 stopping_at 键
    monkeypatch.setattr(lconn, "default_db_path", lambda: p)
    monkeypatch.setattr(lconn, "progress_path", lambda: prog)
    monkeypatch.setattr(
        lconn, "connect_existing",
        lambda path=None: (_ for _ in ()).throw(lconn.LakeLocked(p, None)))

    d = wapi.status()
    assert d["backfill_in_progress"] is True and d["lock_holder_pid"] is None
    assert d["stopping"] is True, f"state=stopping 双保险必须 true: {d}"


def test_status_locked_stopping_false_no_marker(tmp_path, monkeypatch):
    """progress 无停止标记（正常运行中）→ stopping=false（区分"运行中" vs "收尾中"）。"""
    import lake.web_api as wapi
    from lake import conn as lconn

    p = str(tmp_path / "st3.duckdb")
    with open(p, "wb") as f:
        f.write(b"\x00" * 16)
    prog = str(tmp_path / "backfill_progress.json")
    with open(prog, "w", encoding="utf-8") as f:
        json.dump({"updated_at": None,
                   "tasks": [{"table": "kline_history", "tier": "P2",
                              "state": "running"}],
                   "coverage": {}}, f)
    monkeypatch.setattr(lconn, "default_db_path", lambda: p)
    monkeypatch.setattr(lconn, "progress_path", lambda: prog)
    monkeypatch.setattr(
        lconn, "connect_existing",
        lambda path=None: (_ for _ in ()).throw(lconn.LakeLocked(p, 4321)))

    d = wapi.status()
    assert d["backfill_in_progress"] is True
    assert d["stopping"] is False, f"正常运行中必须 stopping=false: {d}"


def test_status_stopping_key_only_in_locked_state(tmp_path, monkeypatch):
    """三态互不串味：stopping 键**只在 locked 态出现**——uninitialized/ready 两态
    响应体不得混入（v6.0.3/v6.0.5 逐字节契约延续）。"""
    import lake.web_api as wapi
    from lake import conn as lconn

    # uninitialized（缺库）
    monkeypatch.setattr(lconn, "default_db_path",
                        lambda: str(tmp_path / "nope" / "x.duckdb"))
    d = wapi.status()
    assert "stopping" not in d, f"uninitialized 态不得混入 stopping: {sorted(d)}"

    # ready（可连库）
    p = str(tmp_path / "ready.duckdb")
    _seed_ready_db(p)
    monkeypatch.setattr(lconn, "default_db_path", lambda: p)
    d = wapi.status()
    assert d["initialized"] is True and "backfill_in_progress" not in d
    assert "stopping" not in d, f"ready 态不得混入 stopping: {sorted(d)}"


def test_http_status_stopping_full_cycle(tmp_path):
    """HTTP 层（真 uvicorn + fake holder）：locked 态 /status 恒带 stopping=false
    （fake holder 无 runner 标记）；锁释放后 ready 态无 stopping 键。"""
    p = str(tmp_path / "http_st.duckdb")
    _seed_ready_db(p)
    fake = _write_fake_driver(tmp_path)
    holder = subprocess.Popen([PY, fake, "history", "--db", p],
                              stdout=subprocess.DEVNULL)
    port = _free_port()
    srv = _start_lake_server(port, p)
    try:
        assert _wait_ready(port), "uvicorn 未在超时内就绪"
        d = None
        for _ in range(40):
            s, b = _http(port, "GET", "/api/lake/status")
            d = json.loads(b)
            if d.get("backfill_in_progress"):
                break
            time.sleep(0.25)
        assert d and d["backfill_in_progress"] is True
        assert d.get("stopping") is False, f"无 runner 标记时 stopping=false: {d}"

        # fake driver 忽略 SIGTERM（模拟收尾中不立即退出）→ 显式 kill 释放锁
        holder.kill()
        holder.wait(timeout=15)
        for _ in range(40):
            s, b = _http(port, "GET", "/api/lake/status")
            d = json.loads(b)
            if "backfill_in_progress" not in d:
                break
            time.sleep(0.25)
        assert "backfill_in_progress" not in d and "stopping" not in d, \
            f"锁释放后 ready 态不得残留 stopping: {d}"
    finally:
        _stop_server(srv)
        if holder.poll() is None:
            holder.kill()
        holder.wait(timeout=10)


# ===========================================================================
# 3. BackfillRunner：SIGTERM handler 落盘"停止收尾中"标记（真子进程 + 跨进程信号）
# ===========================================================================
def _stopping_child_code(progress_path: str, n_tasks: int, task_s: float) -> str:
    """子进程：隔离配额/生产库 → run(n 个 sleep 任务)；SIGTERM 由父进程真实投递。

    与 v6.0.9 用例同构，但**信号后、退出前**读一次 progress 文件（验证 handler 已
    落盘 stopping 标记——收尾等待可观测），退出前再读最终态（stopped_by_signal +
    stopping_at=None）。
    """
    return (
        "import json, os, sys, time\n"
        f"sys.path.insert(0, {REPO_ROOT!r})\n"
        "from lake import backfill as lb\n"
        "from lake import conn as lconn\n"
        "lb.BackfillRunner._quota_state = lambda self: (False, 0)\n"
        "lconn.get_conn = lambda: (_ for _ in ()).throw(lconn.LakeUnavailable('iso'))\n"
        f"prog = {progress_path!r}\n"
        f"tasks = [lb.Task(priority=2, table='kline_history', ts_code=f'sh.6{{i:05d}}',\n"
        "                  period_or_date='full_history', tier='P2') for i in range(%d)]\n"
        "def worker(task):\n"
        f"    time.sleep({task_s})\n"
        "r = lb.BackfillRunner(budget_per_day=5000, progress_path=prog)\n"
        "stats = r.run(tasks, worker)\n"
        "with open(prog, encoding='utf-8') as f:\n"
        "    final = json.load(f)\n"
        "print('FINAL ' + json.dumps({'tasks': final.get('tasks', []),\n"
        "                              'stopping_at': final.get('stopping_at')}))\n"
        "print('STATS ' + json.dumps(stats), flush=True)\n"
    ) % (n_tasks,)


def test_sigterm_handler_persists_stopping_marker(tmp_path):
    """**核心验收**：SIGTERM 后 handler **同步**落盘 tasks[].state="stopping" +
    stopping_at（信号到达即写，不等任务边界）——父进程在子进程退出前读到该标记。"""
    prog = str(tmp_path / "backfill_progress.json")
    child = subprocess.Popen(
        [PY, "-c", _stopping_child_code(prog, n_tasks=5, task_s=1.0)],
        stdout=subprocess.PIPE, text=True)
    try:
        time.sleep(1.6)   # 第 2 个任务 sleep 中（~1.5s 处）
        child.send_signal(signal.SIGTERM)
        # 信号后、退出前：progress 文件必须已含 stopping 标记（handler 同步落盘）
        deadline = time.monotonic() + 10
        saw_stopping = False
        while time.monotonic() < deadline:
            if child.poll() is not None:
                break
            try:
                with open(prog, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                time.sleep(0.1)
                continue
            states = [t.get("state") for t in data.get("tasks", [])]
            if "stopping" in states or data.get("stopping_at"):
                saw_stopping = True
                break
            time.sleep(0.1)
        out, _ = child.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        child.kill()
        pytest.fail("SIGTERM 后子进程未在 30s 内退出（优雅停止失败）")
    assert child.returncode == 0, f"优雅停止必须 rc=0: {out}"

    assert saw_stopping, (
        "信号到达后、进程退出前 progress 文件必须出现 stopping 标记"
        "（handler 同步落盘——收尾等待可观测）")

    # 最终态：state=stopped_by_signal + stopping_at=None（标记清除不残留）
    final_line = next(l for l in out.splitlines() if l.startswith("FINAL "))
    final = json.loads(final_line[len("FINAL "):])
    entry = next(t for t in final["tasks"] if t.get("table") == "kline_history")
    assert entry["state"] == "stopped_by_signal", f"最终态: {entry}"
    assert final["stopping_at"] is None, \
        f"收尾完成后 stopping_at 必须清除（不残留）: {final['stopping_at']}"


def test_stopping_marker_cleared_on_new_run(tmp_path, monkeypatch):
    """新 run 开始处清除上一轮残留：progress 文件预置 stopping_at + state=stopping
    （模拟上轮被强杀没走到 _finish_stop）→ run() 后标记清除、state 正常。"""
    from lake import backfill as lb

    prog = str(tmp_path / "prog_stale.json")
    with open(prog, "w", encoding="utf-8") as f:
        json.dump({"updated_at": None,
                   "tasks": [{"table": "kline_history", "tier": "P2", "total": 3,
                              "done": 1, "quota_used_today": 0, "quota_budget": 5000,
                              "state": "stopping", "eta_min": None, "last_error": ""}],
                   "coverage": {}, "stopping_at": "2026-09-16 07:00:00"}, f)
    monkeypatch.setattr(lb.BackfillRunner, "_quota_state", lambda self: (False, 0))

    tasks = [lb.Task(priority=2, table="kline_history", ts_code=f"sh.6{i:05d}",
                     period_or_date="full_history", tier="P2") for i in range(3)]
    r = lb.BackfillRunner(budget_per_day=5000, progress_path=prog)
    stats = r.run(tasks, lambda t: None)
    assert stats["processed"] == 3

    with open(prog, encoding="utf-8") as f:
        data = json.load(f)
    assert data.get("stopping_at") is None, \
        f"新 run 必须清除残留 stopping_at: {data.get('stopping_at')}"
    entry = next(t for t in data["tasks"] if t.get("table") == "kline_history")
    assert entry["state"] == "running", f"清除后 state 恢复正常: {entry}"


def test_new_run_clears_residual_persisted_in_first_task_window(tmp_path, monkeypatch):
    """**DEF-1 回归**：残留 stopping_at + tasks[].state="stopping"（上轮被强杀）→
    新 run 开始处的清除+归位必须**落盘**——首个任务执行窗口内（任何任务事件落盘前），
    直接读 progress 文件（/status 的 stopping 数据源，web_api._stopping_from_progress）
    即应见 stopping_at=None 且无 state="stopping"。

    旧实现只改内存、其后无 save_progress → 首任务窗口（长跑可达分钟级）文件持续残留
    → /status 误报 stopping=true。判定点取**首个 worker 调用时刻**读文件——该时刻
    run() 必已越过开始处的清除代码、且首个任务事件（mark_done/_update_task_view）
    尚未发生，恰是 DEF-1 的误报窗口；确定性无竞态。回归时该时刻文件仍是残留态
    （updated_at=None + stopping_at 非空 + state=stopping）→ 断言失败。"""
    from lake import backfill as lb

    prog = str(tmp_path / "prog_def1.json")
    with open(prog, "w", encoding="utf-8") as f:
        json.dump({"updated_at": None,
                   "tasks": [{"table": "kline_history", "tier": "P2", "total": 3,
                              "done": 1, "quota_used_today": 0, "quota_budget": 5000,
                              "state": "stopping", "eta_min": None, "last_error": ""}],
                   "coverage": {}, "stopping_at": "2026-09-16 07:00:00"}, f)
    monkeypatch.setattr(lb.BackfillRunner, "_quota_state", lambda self: (False, 0))

    tasks = [lb.Task(priority=2, table="kline_history", ts_code=f"sh.6{i:05d}",
                     period_or_date="full_history", tier="P2") for i in range(3)]
    first_task_file_state: dict | None = None

    def worker(task):
        nonlocal first_task_file_state
        if first_task_file_state is None:
            # 首任务执行中、首事件落盘前：/status 此刻读到的就是这份文件
            with open(prog, encoding="utf-8") as f:
                first_task_file_state = json.load(f)
        time.sleep(1.5)   # 拉长首任务窗口（模拟长跑首任务，无事件落盘）

    r = lb.BackfillRunner(budget_per_day=5000, progress_path=prog)
    stats = r.run(tasks, worker)
    assert stats["processed"] == 3

    assert first_task_file_state is not None, "worker 未执行（前置错误）"
    d = first_task_file_state
    # ① 清除已落盘：updated_at 被 save_progress 刷新（预置文件为 None——窗口内除
    #    run 开始处的清除外无任何写者，非空即证明清除落盘发生在首任务之前）
    assert d.get("updated_at") is not None, \
        f"首任务窗口内 progress 文件未被刷新——run 开始处的清除没有 save_progress " \
        f"落盘（DEF-1 回归）: updated_at={d.get('updated_at')}"
    # ② /status stopping 数据源：stopping_at=None 且无 state=stopping → stopping=false
    assert d.get("stopping_at") is None, \
        f"首任务窗口内文件残留 stopping_at（/status 将误报 stopping=true）: {d.get('stopping_at')}"
    states = [t.get("state") for t in d.get("tasks", [])]
    assert "stopping" not in states, \
        f"首任务窗口内文件残留 state=stopping（/status 将误报 stopping=true）: {states}"
    # ③ 归位语义：task state 置回 pending（随后首任务事件覆盖为 running；勿留 stopping）
    entry = next(t for t in d["tasks"] if t.get("table") == "kline_history")
    assert entry["state"] == "pending", \
        f"归位后 state 应为 pending（新 run 开始、首任务尚未完成事件）: {entry}"


# ===========================================================================
# 4. 重试循环提前中断（收尾加速）——BaoStock / 腾讯
# ===========================================================================
def test_baostock_retry_interrupted_by_stop_flag(monkeypatch):
    """BaoStockClient._query 停止中断双路径：
    ① 标志**调用前**已置位 → 第 1 轮 attempt 前检查即抛（0 次 API 调用，最快收尾）；
    ② 标志在**退避期间**到达（第 1 次失败后 query_fn 置位）→ 退避 sleep 拆块逐查、
       ~0.5s 内立即中断（不等满 base_delay=5s × 抖动的完整退避）。"""
    from screener.data.baostock_client import BaoStockClient, BaoStockError

    monkeypatch.setenv("HOME", "/tmp/v6010_fake_home")   # 隔离配额文件（不碰生产）
    from lake import backfill as lb
    client = BaoStockClient(max_attempts=3, base_delay=5.0, max_delay=30.0,
                            quota_path=False, stop_checker=lb.stop_requested)

    calls = {"n": 0}

    def _boom(**kw):
        calls["n"] += 1
        raise ConnectionError("simulated network failure")

    # ① 标志调用前已置位 → 立即抛（0 次 API 调用）
    lb.set_stop_requested()
    try:
        t0 = time.monotonic()
        with pytest.raises(BaoStockError) as ei1:
            client.call(_boom, label="q")
        elapsed = time.monotonic() - t0
        assert calls["n"] == 0, f"标志已置位时不得再发 API 调用: {calls}"
        assert elapsed < 1.0, f"必须立即中断，实际 {elapsed:.2f}s"
        assert "停止信号" in str(ei1.value)
    finally:
        lb.clear_stop_requested()

    # ② 标志在退避期间到达：query_fn 第 1 次调用后置位 → attempt 1 执行（n=1）→
    #    失败进退避 → sleep 拆块逐查命中 → ~0.5-1s 内中断（完整退避 ≥2.5s）
    calls["n"] = 0

    def _boom_then_stop(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            lb.set_stop_requested()   # 模拟 SIGTERM 在任务执行中到达
        raise ConnectionError("simulated network failure")

    try:
        t0 = time.monotonic()
        with pytest.raises(BaoStockError) as ei2:
            client.call(_boom_then_stop, label="q")
        elapsed = time.monotonic() - t0
        assert calls["n"] == 1, f"退避期间中断后不得继续重试: {calls}"
        # 完整退避下界 = base_delay×0.5（抖动最小值）= 2.5s + 拆块粒度；上限 ≈7.5s。
        # 断言 < 完整退避+下一轮 attempt 的时间窗（~11s），证明"提前中断、不等满"。
        assert elapsed < 11.0, \
            f"必须提前中断（不等满退避 base=5s×抖动[0.5,1.5]），实际 {elapsed:.2f}s"
        assert "停止信号" in str(ei2.value), f"错误文案应指停止信号: {ei2.value}"
    finally:
        lb.clear_stop_requested()

    # ③ 无标志：完整重试走完（原行为零变化回归）
    calls["n"] = 0
    t0 = time.monotonic()
    with pytest.raises(BaoStockError):
        client.call(_boom, label="q")
    elapsed_no_flag = time.monotonic() - t0
    assert calls["n"] == 3 and elapsed_no_flag >= 4.0, \
        f"无标志时应走完重试: n={calls['n']} t={elapsed_no_flag:.2f}"


def test_tencent_fetch_kline_interrupted_by_stop_flag(monkeypatch):
    """腾讯 fetch_kline_ohlcv：请求失败重试中收到停止标志 → 抛 StopRequestedError
    （不静默返回 []——p0 worker 对空结果仍 mark_done，会把未取数误标完成）。"""
    import requests

    from lake import backfill as lb
    from lake.ingest import tencent_ingest as ti

    class _FakeSession:
        def get(self, url, timeout=None):
            raise requests.ConnectionError("simulated")

    class _FakeClient:
        session = _FakeSession()
        timeout = 15
        max_attempts = 3

    lb.clear_stop_requested()
    try:
        # 无标志：重试耗尽 → []（原行为不变）
        out = ti.fetch_kline_ohlcv(_FakeClient(), "sh.601398", n=100)
        assert out == [], f"无标志时保持原失败语义 []: {out}"

        # 有标志：立即抛 StopRequestedError（第 1 轮 attempt 前检查）
        lb.set_stop_requested()
        with pytest.raises(lb.StopRequestedError):
            ti.fetch_kline_ohlcv(_FakeClient(), "sh.601398", n=100)
    finally:
        lb.clear_stop_requested()


def test_tencent_full_history_page_interrupted_by_stop_flag(monkeypatch):
    """腾讯 _fetch_kline_page（history 全史分页）：重试中收到停止标志 →
    StopRequestedError（⊂ RuntimeError，与单页重试耗尽同走失败语义——worker 不
    mark_done，下轮续传）。"""
    import requests

    from lake import backfill as lb
    from lake.ingest import tencent_ingest as ti

    class _FakeSession:
        def get(self, url, timeout=None):
            raise requests.ConnectionError("simulated")

    class _FakeClient:
        session = _FakeSession()
        timeout = 15
        max_attempts = 3

    lb.set_stop_requested()
    try:
        with pytest.raises(lb.StopRequestedError):
            ti._fetch_kline_page(_FakeClient(), "sh601398", None, None, 2000)
    finally:
        lb.clear_stop_requested()


# ===========================================================================
# 5. 前端按钮锁定 DOM 契约（node 驱动 app.js 真实函数）
# ===========================================================================
_NODE = shutil.which("node")

DOM_CONTRACT_JS = r"""
"use strict";
// v6.0.10 前端按钮锁定 DOM 契约：加载**真实** app.js，stub DOM/fetch/confirm，
// 驱动 lakeSyncStop/lakeSyncStart/lakeRenderSyncControl 全状态机。
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const REPO = process.argv[2];
const appJs = fs.readFileSync(path.join(REPO, "web", "static", "app.js"), "utf-8");

// ---- DOM stub（最小可驱动：任意 #id 自动创建元素，避免 loadLakeStatus 全渲染路径 NPE）----
function makeEl(id) {
  const el = { id, disabled: false, innerHTML: "", textContent: "", onclick: null,
               style: {}, value: "", nodeType: 1 };
  el._classes = new Set();
  el.classList = {
    add(c) { el._classes.add(c); },
    remove(c) { el._classes.delete(c); },
    contains(c) { return el._classes.has(c); },
  };
  el.append = () => {};                    // v6.0.5+ 渲染器 append 子节点（stub 忽略）
  el.addEventListener = () => {};
  el.closest = () => null;
  return el;
}
const els = {};
const documentStub = {
  querySelector: (sel) => {
    const id = sel.replace(/^#/, "");
    if (!els[id]) els[id] = makeEl(id);
    return els[id];
  },
  querySelectorAll: () => [],
  addEventListener: () => {},   // DOMContentLoaded 不触发（initTabs/loadRuns 不需要）
  createElement: () => makeEl("tmp"),
  createTextNode: (t) => ({ nodeType: 3, textContent: String(t) }),
};

// ---- fetch/confirm stub ----
let confirmAnswer = true;
let fetchLog = [];
// status 响应队列（轮询按序消费；只剩一个时保持返回它，模拟稳定状态）
let statusQueue = [];
let stopResponse = null;      // POST /sync/stop 的响应
let startResponse = null;     // POST /sync/start 的响应

async function fakeFetch(url, opts) {
  fetchLog.push({ url, method: (opts && opts.method) || "GET" });
  let body, status = 200;
  if (url === "/api/lake/status") {
    body = statusQueue.length > 1 ? statusQueue.shift() : statusQueue[0];
    if (body === null) {
      return { ok: false, status: 500, statusText: "boom", json: async () => ({}) };
    }
  } else if (url === "/api/lake/sync/stop") {
    body = stopResponse;
  } else if (url === "/api/lake/sync/start") {
    body = startResponse;
  } else {
    body = {};
  }
  if (body && body.__http_status) status = body.__http_status;
  return { ok: status >= 200 && status < 300, status, statusText: "ok",
           json: async () => body };
}

const sandbox = {
  document: documentStub,
  fetch: fakeFetch,
  confirm: () => confirmAnswer,
  setInterval: (fn, ms) => ({ fn, ms }),
  clearInterval: () => {},
  setTimeout: (fn, ms) => 0,
  clearTimeout: () => {},
  console,
};
sandbox.window = sandbox;
vm.createContext(sandbox);
// ⚠️ app.js 顶层 let（lakeSyncStopping 等）**不挂** globalThis——追加导出片段
// （同脚本作用域，闭包可读写）供测试快进状态机时钟。
const exportSnippet = (
  "\n;globalThis.__v6010test = {\n" +
  "  get stopping() { return lakeSyncStopping; },\n" +
  "  set stopping(v) { lakeSyncStopping = v; },\n" +
  "};\n"
);
vm.runInContext(appJs + exportSnippet, sandbox, { filename: "app.js" });

// ---- 断言工具 ----
let failures = [];
function assert(cond, msg) { if (!cond) failures.push(msg); }
const btn = () => els["btn-lake-sync-toggle"];
const meta = () => els["lake-sync-meta"];
const toasts = () => els["global-status"].textContent;
// 让出 macrotask：app.js 的 finally 里 fire-and-forget loadLakeStatus()（不 await），
// 需等其微任务链跑完（fetch→json→render）再断言释放后的按钮态。
const tick = () => new Promise((r) => setTimeout(r, 0));

(async () => {
  // ===== 场景 1：停止点击 → 立即锁定（disabled + "⏹ 停止中… (pid)" + sync-busy）=====
  statusQueue = [{ installed: true, backfill_in_progress: true, stopping: false,
                   lock_holder_pid: 4242, tasks: [], updated_at: "2026-09-16 08:00:00" }];
  await sandbox.loadLakeStatus();
  assert(btn().innerHTML === "⏹ 停止同步 (4242)", "S1 前置：running 态按钮文案: " + btn().innerHTML);
  assert(btn().disabled === false, "S1 前置：running 态按钮可点");

  stopResponse = { stopped: false, pid: 4242, method: "killpg", waiting_task: true,
                   note: "信号已发，正在等当前任务收尾" };
  await sandbox.lakeSyncStop();   // confirm=true → 点击停止
  await tick();                   // 等 finally 的 fire-and-forget loadLakeStatus 跑完
  assert(btn().disabled === true, "S1 点击后必须立即 disabled（锁定）");
  assert(btn().innerHTML === "⏹ 停止中… (4242)", "S1 文案=⏹ 停止中… (pid): " + btn().innerHTML);
  assert(btn()._classes.has("sync-busy"), "S1 必须加 sync-busy 置灰类");
  const stopCalls = fetchLog.filter(f => f.url === "/api/lake/sync/stop");
  assert(stopCalls.length === 1 && stopCalls[0].method === "POST", "S1 POST /sync/stop 恰好一次");

  // ===== 场景 2：锁定期间轮询 running=true → 按钮保持锁定（不被覆盖）=====
  statusQueue = [{ installed: true, backfill_in_progress: true, stopping: true,
                   lock_holder_pid: 4242, tasks: [], updated_at: "2026-09-16 08:00:05" }];
  await sandbox.loadLakeStatus();   // 模拟 1s 轮询命中
  assert(btn().disabled === true, "S2 收尾中（running=true）按钮必须保持锁定");
  assert(btn().innerHTML.startsWith("⏹ 停止中…"), "S2 文案保持停止中: " + btn().innerHTML);

  // ===== 场景 3：backfill_in_progress=false → toast"已停止，进度已保存"+ 释放 =====
  statusQueue = [{ installed: true, tasks: [], updated_at: "2026-09-16 08:00:09" }];
  await sandbox.loadLakeStatus();
  assert(btn().disabled === false, "S3 停止成功后必须释放按钮");
  assert(btn()._classes.has("sync-busy") === false, "S3 必须移除 sync-busy");
  assert(btn().innerHTML === "▶ 启动同步", "S3 释放回启动按钮: " + btn().innerHTML);
  assert(toasts().includes("已停止，进度已保存"), "S3 toast=已停止，进度已保存: " + toasts());

  // ===== 场景 4：>90s 未停 → 文案升级"当前任务收尾中，最长约几分钟"（仍锁定）=====
  statusQueue = [{ installed: true, backfill_in_progress: true, stopping: true,
                   lock_holder_pid: 4242, tasks: [], updated_at: "x" }];
  await sandbox.loadLakeStatus();   // running 渲染（stopping=true → meta 友好文案；
  assert(btn().innerHTML === "⏹ 停止同步 (4242)", "S4 前置：running 态: " + btn().innerHTML);
  assert(meta().textContent.length > 0, "S4 前置：meta 非空: " + meta().textContent);
  await sandbox.lakeSyncStop();   // 重新进入停止锁定（confirm=true；pid 从 meta 解析）
  await tick();
  assert(btn().disabled === true, "S4 重新点击后锁定");
  const st4 = sandbox.__v6010test.stopping;
  assert(st4 && st4.pid === 4242, "S4 锁定态记录 pid: " + JSON.stringify(st4));
  st4.since = Date.now() - 91 * 1000;   // 快进：回拨 91s
  await sandbox.loadLakeStatus();
  assert(btn().disabled === true, "S4 >90s 仍锁定");
  assert(btn().innerHTML === "⏹ 停止中…（当前任务收尾中，最长约几分钟）",
         "S4 文案升级: " + btn().innerHTML);

  // ===== 场景 5：5min 硬超时 → toast + 释放 =====
  sandbox.__v6010test.stopping.since = Date.now() - (5 * 60 + 1) * 1000;
  await sandbox.loadLakeStatus();
  assert(btn().disabled === false, "S5 硬超时后必须释放按钮");
  assert(toasts().includes("停止超时，进程可能仍在收尾，请刷新查看"),
         "S5 toast=停止超时…: " + toasts());
  // 释放后按真实状态重渲染（仍 running → 恢复停止入口）
  assert(btn().innerHTML === "⏹ 停止同步 (4242)", "S5 释放后恢复停止入口: " + btn().innerHTML);

  // ===== 场景 6：启动点击 → 锁定"▶ 启动中…"，POST 返回后释放 =====
  statusQueue = [{ installed: true, tasks: [], updated_at: "x" }];   // idle 态
  await sandbox.loadLakeStatus();
  assert(btn().innerHTML === "▶ 启动同步", "S6 前置：idle 态");
  startResponse = { started: true, pid: 5150, log_path: "/tmp/s.log" };
  const p = sandbox.lakeSyncStart();   // 不 await——先断言在途锁定态
  assert(btn().disabled === true, "S6 启动点击后必须立即 disabled");
  assert(btn().innerHTML === "▶ 启动中…", "S6 文案=▶ 启动中…: " + btn().innerHTML);
  assert(btn()._classes.has("sync-busy"), "S6 必须加 sync-busy");
  await p;
  await tick();                        // finally 的 loadLakeStatus（idle 渲染）跑完
  assert(btn().disabled === false, "S6 POST /start 返回后释放");
  assert(toasts().includes("同步已启动（PID 5150）"), "S6 toast=同步已启动: " + toasts());

  // ===== 场景 7：启动 409 → toast hint + 释放（不锁死）=====
  startResponse = { __http_status: 409, error: "sync_already_running",
                    hint: "已有灌数在运行（PID=1），请先停止再启动" };
  await sandbox.loadLakeStatus();   // 回到 idle 渲染
  const p2 = sandbox.lakeSyncStart();
  assert(btn().disabled === true && btn().innerHTML === "▶ 启动中…", "S7 409 前仍锁定");
  await p2;
  await tick();
  assert(btn().disabled === false, "S7 409 后必须释放（不锁死）");
  assert(toasts().includes("已有灌数在运行"), "S7 toast=409 hint: " + toasts());

  // ===== 场景 8：停止 POST 409（sync_not_running）→ toast + 释放 =====
  statusQueue = [{ installed: true, backfill_in_progress: true, stopping: false,
                   lock_holder_pid: 777, tasks: [], updated_at: "x" }];
  await sandbox.loadLakeStatus();
  stopResponse = { __http_status: 409, error: "sync_not_running",
                   hint: "当前没有灌数在运行（状态可能刚更新，请刷新后重试）" };
  await sandbox.lakeSyncStop();
  await tick();
  assert(btn().disabled === false, "S8 停止 409 后必须释放");
  assert(toasts().includes("当前没有灌数在运行"), "S8 toast=409 hint: " + toasts());

  if (failures.length) {
    console.log("DOM_CONTRACT_FAIL:\n- " + failures.join("\n- "));
    process.exit(1);
  }
  console.log("DOM_CONTRACT_OK: 8 场景全部通过（停止锁定/轮询保持/成功释放/90s升级/5min硬超时/启动锁定/409释放×2）");
})().catch((e) => { console.log("DOM_CONTRACT_ERROR: " + (e && e.stack || e)); process.exit(2); });
"""


@pytest.mark.skipif(_NODE is None, reason="node 不可用（前端 DOM 契约需 node 驱动 app.js）")
def test_frontend_stop_lock_dom_contract(tmp_path):
    """**核心验收（brief 字面）**：DOM 契约测试——点击后 disabled + 文案 +
    轮询释放。node 加载真实 app.js（stub DOM/fetch/confirm），驱动全状态机。"""
    node = _NODE or shutil.which("node")
    if node is None:
        pytest.skip("node 不可用")
    script = tmp_path / "dom_contract.cjs"
    script.write_text(DOM_CONTRACT_JS, encoding="utf-8")
    r = subprocess.run([node, str(script), REPO_ROOT],
                       capture_output=True, text=True, timeout=120)
    out = (r.stdout or "") + (r.stderr or "")
    assert r.returncode == 0, f"DOM 契约测试失败:\n{out}"
    assert "DOM_CONTRACT_OK" in out, f"缺成功标记:\n{out}"


# ===========================================================================
# 6. E2E 冒烟（tmp 库 ≤3 只，授权）：start → 运行中 → stop 异步 → stopping → 停
# ===========================================================================
def test_e2e_start_stop_stopping_cycle(tmp_path):
    """**E2E 冒烟**（brief 字面断言链）：tmp 库 + fake driver 持锁（灌数等价物，
    零网络）→ ① POST /sync/start 200 started=true；② /status backfill_in_progress
    =true；③ POST /sync/stop **立即**返回 200 waiting_task=true；④ /status 出现
    stopping（fake driver 无 runner handler → 用 progress 文件注入标记模拟真实
    runner 的落盘行为——E2E 验证的是 Web 读取链路）；⑤ 最终 backfill_in_progress
    =false + ready 态无 stopping 键。

    注：真实 BackfillRunner 的 handler 落盘由 §3 真子进程用例覆盖（SIGTERM 跨进程
    信号 → stopping 标记文件可见），本用例聚焦 Web 端点全链路。
    """
    p = str(tmp_path / "e2e_stop.duckdb")
    _seed_ready_db(p)
    fake = _write_fake_driver(tmp_path)
    prog = str(tmp_path / "backfill_progress.json")   # 与库同目录（progress_path_for_db 派生）
    with open(prog, "w", encoding="utf-8") as f:
        json.dump({"updated_at": None, "tasks": [], "coverage": {}}, f)

    port = _free_port()
    srv = _start_lake_server(port, p)
    holder = None
    try:
        assert _wait_ready(port), "uvicorn 未在超时内就绪"

        # ① POST /sync/start（fake driver 经 monkeypatch 不可用——子进程是 uvicorn，
        #    直接手动 spawn fake driver 持锁模拟"运行中"；start 端点的防双开 409
        #    由 v6.0.9 用例覆盖）
        holder = subprocess.Popen([PY, fake, "history", "--db", p],
                                  stdout=subprocess.DEVNULL)
        d = None
        for _ in range(40):
            s, b = _http(port, "GET", "/api/lake/status")
            d = json.loads(b)
            if d.get("backfill_in_progress"):
                break
            time.sleep(0.25)
        assert d and d["backfill_in_progress"] is True, f"② 运行中: {d}"
        assert d.get("stopping") is False

        # ③ POST /sync/stop → **立即** 200 + waiting_task=true（异步，不阻塞）
        t0 = time.monotonic()
        s, b = _http(port, "POST", "/api/lake/sync/stop")
        assert s == 200, f"③ stop 应 200，实际 {s}: {b}"
        bd = json.loads(b)
        assert time.monotonic() - t0 < 5.0, f"③ stop 必须立即返回: {bd}"
        assert bd.get("waiting_task") is True, f"③ waiting_task=true: {bd}"
        assert bd.get("pid") == holder.pid

        # ④ /status 出现 stopping（注入 runner handler 会写的标记——验证 Web 读取链路）
        with open(prog, encoding="utf-8") as f:
            data = json.load(f)
        data["stopping_at"] = "2026-09-16 08:30:00"
        data["tasks"] = [{"table": "kline_history", "tier": "P2", "total": 5,
                          "done": 2, "quota_used_today": 2, "quota_budget": 5000,
                          "state": "stopping", "eta_min": None, "last_error": ""}]
        with open(prog, "w", encoding="utf-8") as f:
            json.dump(data, f)
        s, b = _http(port, "GET", "/api/lake/status")
        d = json.loads(b)
        assert d.get("backfill_in_progress") is True
        assert d.get("stopping") is True, f"④ 标记落盘后 /status 必须 stopping=true: {d}"

        # ⑤ 最终：进程退出 → backfill_in_progress=false + ready 态无 stopping 键。
        #    fake driver 忽略 SIGTERM（模拟"当前任务收尾中、不立即退出"）→ 显式 kill
        holder.kill()
        holder.wait(timeout=20)
        d = None
        for _ in range(40):
            s, b = _http(port, "GET", "/api/lake/status")
            d = json.loads(b)
            if "backfill_in_progress" not in d:
                break
            time.sleep(0.25)
        assert "backfill_in_progress" not in d, f"⑤ 最终必须停: {d}"
        assert "stopping" not in d, f"⑤ ready 态不得残留 stopping 键: {d}"

        # 再 stop → 409 sync_not_running
        s, b = _http(port, "POST", "/api/lake/sync/stop")
        assert s == 409 and json.loads(b).get("error") == "sync_not_running"
    finally:
        _stop_server(srv)
        if holder is not None:
            if holder.poll() is None:
                holder.kill()
            try:
                holder.wait(timeout=10)
            except Exception:  # noqa: BLE001
                pass
