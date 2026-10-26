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
        "import os; os.environ['WEB_TEST_NO_AUTH'] = '1'",  # v7.0.14 测试钩子：跳过登录门禁
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
    from lake import conn as lconn

    prog = str(tmp_path / "prog_stale.json")
    with open(prog, "w", encoding="utf-8") as f:
        json.dump({"updated_at": None,
                   "tasks": [{"table": "kline_history", "tier": "P2", "total": 3,
                              "done": 1, "quota_used_today": 0, "quota_budget": 5000,
                              "state": "stopping", "eta_min": None, "last_error": ""}],
                   "coverage": {}, "stopping_at": "2026-09-16 07:00:00"}, f)
    monkeypatch.setattr(lb.BackfillRunner, "_quota_state", lambda self: (False, 0))
    # 隔离真实库：_refresh_coverage 不得打开 data/lake/lake.duckdb（本文件纪律：
    # 库一律 tmp，绝不触碰生产库；§3 子进程用例 L354 同隔离）
    monkeypatch.setattr(lconn, "get_conn", lambda: (_ for _ in ()).throw(
        lconn.LakeUnavailable("test isolation")))

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
    from lake import conn as lconn

    prog = str(tmp_path / "prog_def1.json")
    with open(prog, "w", encoding="utf-8") as f:
        json.dump({"updated_at": None,
                   "tasks": [{"table": "kline_history", "tier": "P2", "total": 3,
                              "done": 1, "quota_used_today": 0, "quota_budget": 5000,
                              "state": "stopping", "eta_min": None, "last_error": ""}],
                   "coverage": {}, "stopping_at": "2026-09-16 07:00:00"}, f)
    monkeypatch.setattr(lb.BackfillRunner, "_quota_state", lambda self: (False, 0))
    # 隔离真实库（同上方用例纪律：in-process run 不得触碰生产库）
    monkeypatch.setattr(lconn, "get_conn", lambda: (_ for _ in ()).throw(
        lconn.LakeUnavailable("test isolation")))

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
# v7.0.18：原 BaoStockClient._query 停止中断用例随 baostock 渠道整体移除
#（腾讯侧停止感知用例保留——重试提前中断语义同口径适用）。
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