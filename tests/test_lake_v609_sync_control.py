# -*- coding: utf-8 -*-
"""test_lake_v609_sync_control —— v6.0.9 回归：数据湖"同步控制"（启动/停止灌数）。

验收点（v6.0.9 brief，全离线 tmp 库 + mock；E2E 真实网络冒烟另见 smoke 脚本）：
- **sync_status 三态**：缺库/可连库 → running=False；真跨进程持锁 → running=True +
  holder 真实 PID（复用 v6.0.4 probe_db_state + LakeLocked.holder_pid 机制，零新探测）。
- **start_sync 防双开**：已 running → started=False + reason=already_running，
  **不 spawn**（Popen 未被调用）；spawn 成功路径（fake driver 持锁 setsid 脱离）→
  started=True + pid + log_path；进程提前退出 / 日志 traceback → started=False + reason。
- **stop_sync pid 解析/降级路径**：未 running → not_running；holder_pid=None →
  holder_pid_unknown（拒绝猜测，不 kill 未知进程）；killpg 成功（setsid 子进程）→
  method=killpg；非组长进程（无 setsid）→ 降级 os.kill → method=kill；
  SIGTERM 被忽略（trap）+ timeout → stopped=False（**不升级 SIGKILL**，保守）。
- **SIGTERM 优雅停止（BackfillRunner）**：真子进程 + 假 worker sleep 循环 →
  SIGTERM 后断言 rc=0、progress state=stopped_by_signal、done 明细落盘（已处理部分
  保留，未处理不标 done）、stats.stopped=True；handler 在 run() 结束恢复原 handler。
- **Web 端点**：start 已 running → 409 sync_already_running / stop 未 running →
  409 sync_not_running（顶层契约体，无 detail 键）；HTTP 层真 uvicorn 全链路
  （fake holder 持锁 tmp 库 → POST start 409 → POST stop 200 stopped=true 杀掉
  holder → 再 POST stop 409）。

纪律：库一律 tmp_path，**绝不触碰 data/lake/ 生产库与生产灌数进程（pid 1548390）**；
零网络（fake driver 只持锁不取数）；/status 三态契约由 test_lake_v604_lock 守门。
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
    """建 schema + 播种一行 stock_master（就绪库，/status ready 态用）。"""
    from lake import conn as lconn

    con = lconn.open(db_path)
    con.execute(
        "INSERT INTO stock_master (ts_code,name,industry_csric2,board,is_st,"
        "source,fetched_at,data_version) VALUES "
        "('sh.601398','工商银行','J66','主板',0,'t','2026-09-14 00:00:00','v6.0')")
    con.close()


def _write_fake_driver(tmp_path, name: str = "fake_driver.py",
                       hold_s: int = 120) -> str:
    """写 fake driver：解析 --db → duckdb.connect 持锁（= 灌数进程等价物）→ sleep。

    参数形态与真 driver 一致（``<script> history --db <path> [extra...]``），
    start_sync 的 cmd 拼装可原样驱动它——零网络、不取数，只验证 spawn/持锁/停止链路。
    """
    code = (
        "import sys, time\n"
        "import duckdb\n"
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


def _start_lake_server(port: int, db_path: str):
    """起 uvicorn（web.app 真实挂载）；子进程内把默认库指向 tmp db。"""
    code_lines = [
        f"import sys; sys.path.insert(0, {REPO_ROOT!r})",
        "from lake import conn as _lc",
        f"_lc.default_db_path = lambda: {db_path!r}",
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
# 1. sync_status 三态（复用 v6.0.4 机制：probe + holder_pid + os.kill(pid,0)）
# ===========================================================================
def test_sync_status_not_running_missing_db(tmp_path):
    """缺库文件 → running=False（probe=unavailable，不猜）。"""
    from lake import sync_control

    st = sync_control.sync_status(str(tmp_path / "nope" / "x.duckdb"))
    assert st == {"running": False, "pid": None}


def test_sync_status_not_running_ok_db(tmp_path):
    """可正常连接的库（无锁）→ running=False（probe=ok）。"""
    from lake import sync_control

    p = str(tmp_path / "ok.duckdb")
    _seed_ready_db(p)
    st = sync_control.sync_status(p)
    assert st == {"running": False, "pid": None}


def test_sync_status_running_real_cross_process_lock(tmp_path):
    """**真跨进程持锁**（duckdb RW 连接，零网络）→ running=True + holder 真实 PID。"""
    from lake import sync_control

    p = str(tmp_path / "locked.duckdb")
    _seed_ready_db(p)
    holder_code = (
        "import duckdb, sys, time\n"
        "c = duckdb.connect(sys.argv[1])\n"
        "print('READY', flush=True)\n"
        "time.sleep(60)\n"
    )
    holder = subprocess.Popen([PY, "-c", holder_code, p],
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "READY"
        # 等锁生效（connect 完成到文件锁落盘有毫秒级窗口）
        st = None
        for _ in range(40):
            st = sync_control.sync_status(p)
            if st["running"]:
                break
            time.sleep(0.25)
        assert st["running"] is True, f"持锁期间必须 running=True: {st}"
        assert st["pid"] == holder.pid, \
            f"pid 应=持锁子进程真实 PID {holder.pid}: {st}"
    finally:
        holder.terminate()
        try:
            holder.wait(timeout=10)
        except subprocess.TimeoutExpired:
            holder.kill()


def test_sync_status_locked_but_holder_dead_not_running(tmp_path, monkeypatch):
    """锁被持有但 holder 进程已死（内核释放 flock 的毫秒级窗口）→ running=False。

    为什么必须这样判：按 running 报会让 stop 去 kill 死 pid、start 误判双开；
    flock 会自动释放，稍后探测自然恢复。
    """
    from lake import conn as lconn
    from lake import sync_control

    p = str(tmp_path / "deadholder.duckdb")
    with open(p, "wb") as f:
        f.write(b"\x00" * 16)
    # 拿一个**确定已死**的 pid（spawn + 立即退出）
    dead = subprocess.Popen([PY, "-c", "pass"])
    dead.wait(timeout=10)

    monkeypatch.setattr(lconn, "probe_db_state", lambda path=None: "locked")
    monkeypatch.setattr(
        lconn, "connect_existing",
        lambda path=None: (_ for _ in ()).throw(lconn.LakeLocked(p, dead.pid)))
    st = sync_control.sync_status(p)
    assert st == {"running": False, "pid": None}, \
        f"holder 已死不得报 running（防误 kill/误判双开）: {st}"


def test_sync_status_locked_pid_parse_failure_still_running(tmp_path, monkeypatch):
    """锁被持有但 PID 解析失败（None）→ **仍** running=True（与 v6.0.4 降级口径一致：
    锁确实被某进程持有 = 有灌数在跑，不得退回误报未运行）。"""
    from lake import conn as lconn
    from lake import sync_control

    p = str(tmp_path / "nopid.duckdb")
    with open(p, "wb") as f:
        f.write(b"\x00" * 16)
    monkeypatch.setattr(lconn, "probe_db_state", lambda path=None: "locked")
    monkeypatch.setattr(
        lconn, "connect_existing",
        lambda path=None: (_ for _ in ()).throw(lconn.LakeLocked(p, None)))
    st = sync_control.sync_status(p)
    assert st == {"running": True, "pid": None}


# ===========================================================================
# 2. start_sync：防双开 / spawn 成功 / 失败路径
# ===========================================================================
def test_start_sync_already_running_no_spawn(tmp_path, monkeypatch):
    """已 running → started=False + reason=already_running，**Popen 不被调用**（防双开）。"""
    from lake import sync_control

    p = str(tmp_path / "x.duckdb")
    monkeypatch.setattr(sync_control, "sync_status",
                        lambda db_path=None: {"running": True, "pid": 4242})

    def _boom(*a, **k):
        raise AssertionError("已 running 时不得 spawn（防双开）")

    monkeypatch.setattr(sync_control.subprocess, "Popen", _boom)
    res = sync_control.start_sync(db_path=p)
    assert res["started"] is False
    assert res["reason"] == "already_running"
    assert res["pid"] == 4242


def test_start_sync_spawn_success_setsid_and_log(tmp_path, monkeypatch):
    """spawn 成功（fake driver 持锁）→ started=True + pid + log_path；
    子进程 setsid 脱离（pgid==pid，Web 重启不影响）；日志 append 到 sync.log。"""
    from lake import sync_control

    p = str(tmp_path / "e2e.duckdb")
    _seed_ready_db(p)
    fake = _write_fake_driver(tmp_path)
    monkeypatch.setattr(sync_control, "sync_script_path", lambda: fake)

    res = sync_control.start_sync(db_path=p, sub="history")
    assert res["started"] is True, f"spawn 应成功: {res}"
    pid = res["pid"]
    assert isinstance(pid, int) and pid > 0
    assert res["log_path"] == str(tmp_path / "sync.log")
    try:
        # setsid 脱离：子进程自成会话（pgid==pid）——stop_sync killpg 的前提
        pgid = os.getpgid(pid)
        assert pgid == pid, f"必须 start_new_session=True（pgid {pgid} != pid {pid}）"
        # 日志落盘（fake driver 的 READY 行 append 到 sync.log）
        deadline = time.monotonic() + 5
        content = ""
        while time.monotonic() < deadline:
            with open(res["log_path"], encoding="utf-8") as f:
                content = f.read()
            if "FAKE_DRIVER_READY" in content:
                break
            time.sleep(0.2)
        assert "FAKE_DRIVER_READY" in content, f"sync.log 应含 fake driver 输出: {content!r}"
        # 运行中状态可探测（真锁）
        st = sync_control.sync_status(p)
        assert st["running"] is True and st["pid"] == pid
    finally:
        try:
            os.killpg(pid, signal.SIGKILL)   # fake driver 无 handler，直接清理
        except (ProcessLookupError, PermissionError):
            pass


def test_start_sync_process_exits_early_failed(tmp_path, monkeypatch):
    """spawn 后进程提前退出（rc=3，模拟库无效）→ started=False + reason 含 rc。"""
    from lake import sync_control

    p = str(tmp_path / "fail.duckdb")
    fake = str(tmp_path / "fail_driver.py")
    with open(fake, "w", encoding="utf-8") as f:
        f.write("import sys\nprint('错误: 数据湖不可用 —— 模拟', file=sys.stderr)\nsys.exit(3)\n")
    monkeypatch.setattr(sync_control, "sync_script_path", lambda: fake)

    res = sync_control.start_sync(db_path=p, sub="history")
    assert res["started"] is False, f"提前退出必须 started=False: {res}"
    assert "rc=3" in res.get("reason", ""), f"reason 应含退出码: {res}"


def test_start_sync_traceback_in_log_failed(tmp_path, monkeypatch):
    """spawn 后日志现 Traceback → started=False + reason（不静默吞启动崩溃）。

    fake driver 打印**真** traceback 头再 exit(0)——进程"存活但已崩"的窗口里由
    日志扫描分支捕获（若等它退出走 rc 分支，reason 就不含 traceback 了；两条路
    都是 started=False，本用例专测日志扫描路径）。
    """
    from lake import sync_control

    p = str(tmp_path / "tb.duckdb")
    fake = str(tmp_path / "tb_driver.py")
    with open(fake, "w", encoding="utf-8") as f:
        f.write(
            "import sys, time\n"
            "print('Traceback (most recent call last):', file=sys.stderr)\n"
            "print('  File \"x\", line 1', file=sys.stderr)\n"
            "print('RuntimeError: simulated startup crash', file=sys.stderr)\n"
            "sys.stdout.flush(); sys.stderr.flush()\n"
            "time.sleep(30)   # 崩了但进程苟活——只能靠日志扫描判死\n")
    monkeypatch.setattr(sync_control, "sync_script_path", lambda: fake)

    res = sync_control.start_sync(db_path=p, sub="history")
    assert res["started"] is False, f"traceback 必须 started=False: {res}"
    assert "traceback" in res.get("reason", "").lower(), f"reason 应指 traceback: {res}"
    try:   # 清理苟活进程
        import os as _os, signal as _sig

        _os.killpg(res["pid"], _sig.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def test_start_sync_always_passes_resolved_db(tmp_path, monkeypatch):
    """cmd 恒带 ``--db <解析后库路径>``（Web 端点 db_path=None 时透传服务进程默认库，
    防子进程 driver 落到它自己的生产默认库）。"""
    from lake import sync_control

    p = str(tmp_path / "resolved.duckdb")
    _seed_ready_db(p)
    fake = _write_fake_driver(tmp_path)
    monkeypatch.setattr(sync_control, "sync_script_path", lambda: fake)
    captured = {}

    real_popen = sync_control.subprocess.Popen

    def spy(cmd, *a, **k):
        captured["cmd"] = list(cmd)
        return real_popen(cmd, *a, **k)

    monkeypatch.setattr(sync_control.subprocess, "Popen", spy)
    res = sync_control.start_sync(db_path=p, sub="history")
    assert res["started"] is True
    try:
        cmd = captured["cmd"]
        # --db 是 driver 全局 flag（子命令之前）：[py, script, --db, <path>, history]
        assert "--db" in cmd and cmd[cmd.index("--db") + 1] == p, \
            f"必须显式透传解析后库路径: {cmd}"
        assert cmd[2] == "--db" and cmd[4] == "history", \
            f"--db 必须在子命令之前（argparse 全局 flag）: {cmd}"
    finally:
        try:
            os.killpg(res["pid"], signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


# ===========================================================================
# 3. stop_sync：not_running / pid 未知拒绝 / killpg / 降级 kill / timeout
# ===========================================================================
def test_stop_sync_not_running(tmp_path):
    """未 running → stopped=False + reason=not_running（Web 层映射 409）。"""
    from lake import sync_control

    res = sync_control.stop_sync(db_path=str(tmp_path / "nope" / "x.duckdb"))
    assert res["stopped"] is False
    assert res["reason"] == "not_running"
    assert res["method"] is None


def test_stop_sync_holder_pid_unknown_refuses(tmp_path, monkeypatch):
    """holder PID 解析失败（None）→ 拒绝猜测目标进程（不 kill 未知 pid）。"""
    from lake import sync_control

    monkeypatch.setattr(sync_control, "sync_status",
                        lambda db_path=None: {"running": True, "pid": None})
    res = sync_control.stop_sync()
    assert res["stopped"] is False
    assert res["reason"].startswith("holder_pid_unknown")


def test_stop_sync_killpg_setsid_process(tmp_path, monkeypatch):
    """本按钮 spawn 的进程（setsid，pgid==pid）→ killpg 一次干净，method=killpg。

    v6.0.10 异步语义：stop_sync **发信号即返回**（waiting_task=True、不阻塞等退出）；
    完成判定 = 轮询 sync_status 到 running=False（锁释放）。
    """
    from lake import sync_control

    p = str(tmp_path / "stop1.duckdb")
    _seed_ready_db(p)
    fake = _write_fake_driver(tmp_path)
    monkeypatch.setattr(sync_control, "sync_script_path", lambda: fake)
    started = sync_control.start_sync(db_path=p, sub="history")
    assert started["started"] is True, f"前置 spawn 应成功: {started}"

    t0 = time.monotonic()
    res = sync_control.stop_sync(db_path=p)
    # v6.0.10：立即返回（<2s——不等进程退出）+ waiting_task=True + method=killpg
    assert time.monotonic() - t0 < 2.0, f"stop 必须异步立即返回: {res}"
    assert res["waiting_task"] is True, f"信号已发应 waiting_task=True: {res}"
    assert res["pid"] == started["pid"]
    assert res["method"] == "killpg", f"setsid 进程应走 killpg: {res}"
    # 完成判定：轮询到进程退出（fake driver 默认 SIGTERM → 干净退出）
    deadline = time.monotonic() + 15
    st = None
    while time.monotonic() < deadline:
        st = sync_control.sync_status(p)
        if not st["running"]:
            break
        time.sleep(0.2)
    assert st and st["running"] is False, f"停止后不得残留 running: {st}"


def test_stop_sync_degrades_to_kill_non_group_leader(tmp_path):
    """**非本按钮启动**的进程（无 setsid，pid 非组长）→ killpg ESRCH → 降级
    os.kill(pid, SIGTERM)，method=kill——brief 明指的覆盖路径。

    v6.0.10 异步语义：发信号即返回 waiting_task=True；完成判定 = 轮询到锁释放。
    """
    from lake import sync_control

    p = str(tmp_path / "stop2.duckdb")
    _seed_ready_db(p)
    fake = _write_fake_driver(tmp_path)
    # 手动 spawn（**不** start_new_session）→ 子进程在 pytest 的进程组里，pid≠pgid
    holder = subprocess.Popen(
        [PY, fake, "history", "--db", p], stdout=subprocess.DEVNULL)
    try:
        st = None
        for _ in range(40):
            st = sync_control.sync_status(p)
            if st["running"]:
                break
            time.sleep(0.25)
        assert st and st["running"] is True, f"fake holder 应持锁: {st}"

        res = sync_control.stop_sync(db_path=p)
        assert res["waiting_task"] is True, f"信号已发应 waiting_task=True: {res}"
        assert res["method"] == "kill", f"非组长进程必须降级 os.kill: {res}"
        # 完成判定：轮询到锁释放（fake driver 默认 SIGTERM → 干净退出）
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if not sync_control.sync_status(p)["running"]:
                break
            time.sleep(0.2)
        assert holder.poll() is not None, f"降级 kill 后进程应退出: {res}"
    finally:
        if holder.poll() is None:
            holder.kill()
        holder.wait(timeout=10)


def test_stop_sync_timeout_no_sigkill_escalation(tmp_path):
    """SIGTERM 被忽略（trap）→ v6.0.10 异步语义下 stop_sync **立即返回**
    waiting_task=True（不再同步等到 timeout）；进程仍活着 = **未升级 SIGKILL**
    （保守：可能正在写事务）。完成与否由前端轮询 /status 判定。"""
    from lake import sync_control

    p = str(tmp_path / "stop3.duckdb")
    _seed_ready_db(p)
    fake = str(tmp_path / "trap_driver.py")
    with open(fake, "w", encoding="utf-8") as f:
        f.write(
            "import sys, time, signal\n"
            "import duckdb\n"
            "db = sys.argv[sys.argv.index('--db') + 1]\n"
            "c = duckdb.connect(db)\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)   # 模拟不响应 SIGTERM\n"
            "print('TRAP_READY', flush=True)\n"
            "time.sleep(120)\n"
        )
    holder = subprocess.Popen([PY, fake, "history", "--db", p],
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "TRAP_READY"
        t0 = time.monotonic()
        res = sync_control.stop_sync(db_path=p)
        # v6.0.10：异步立即返回（不等 trap 进程退出）+ waiting_task=True
        assert time.monotonic() - t0 < 2.0, f"stop 必须异步立即返回: {res}"
        assert res["waiting_task"] is True, f"{res}"
        assert res["method"] in ("killpg", "kill")
        # **未升级 SIGKILL**：稍等片刻进程仍活着（保守语义的直接证据）
        time.sleep(1.0)
        assert holder.poll() is None, "不得升级 SIGKILL——trap 进程必须仍存活"
    finally:
        holder.kill()
        holder.wait(timeout=10)


# ===========================================================================
# 4. BackfillRunner SIGTERM 优雅停止（真子进程 + 假 worker sleep 循环）
# ===========================================================================
def _runner_child_code(progress_path: str, n_tasks: int, task_s: float) -> str:
    """子进程：隔离配额/生产库 → run(n 个 sleep 任务) → 打印 stats JSON 退出。

    与 driver 同构（单线程主循环调 run()）——SIGTERM 由父进程真实投递（跨进程信号，
    非 os.kill(self) 线程内模拟），验证 handler 注册/任务间 break/进度落盘全链路。
    """
    return (
        "import json, sys, time\n"
        f"sys.path.insert(0, {REPO_ROOT!r})\n"
        "from lake import backfill as lb\n"
        "from lake import conn as lconn\n"
        # 隔离：配额恒不阻塞 + coverage 绝不碰生产库（get_conn 抛错 fail-open）
        "lb.BackfillRunner._quota_state = lambda self: (False, 0)\n"
        "lconn.get_conn = lambda: (_ for _ in ()).throw(lconn.LakeUnavailable('isolation'))\n"
        f"prog = {progress_path!r}\n"
        # ⚠️ 本函数是 f-string：用 {{i:05d}} 转义出**字面** f'sh.6{i:05d}' 给子进程
        # （两行都必须是 f-string——首行若为普通字符串，{{ }} 不会被折叠，子进程的
        # f-string 会把 {i:05d} 当字面量 → 全部任务同 code，幂等跳过 4/5）
        f"tasks = [lb.Task(priority=2, table='kline_history', ts_code=f'sh.6{{i:05d}}',\n"
        f"                  period_or_date='full_history', tier='P2') for i in range({n_tasks})]\n"
        "def worker(task):\n"
        f"    time.sleep({task_s})   # 假 worker：模拟取数耗时（信号到达时让它跑完）\n"
        "r = lb.BackfillRunner(budget_per_day=5000, progress_path=prog)\n"
        "stats = r.run(tasks, worker)\n"
        "print('STATS ' + json.dumps(stats), flush=True)\n"
    )


def test_sigterm_graceful_stop_cross_process(tmp_path):
    """**核心验收**（brief 字面）：tmp 库 + 假 worker sleep 循环，SIGTERM 后断言——
    进程干净退出 rc=0、progress state=stopped_by_signal、done 明细落盘（已处理保留、
    未处理不标 done）、stats.stopped=True。"""
    prog = str(tmp_path / "backfill_progress.json")
    child = subprocess.Popen(
        [PY, "-c", _runner_child_code(prog, n_tasks=5, task_s=1.0)],
        stdout=subprocess.PIPE, text=True)
    try:
        # 等 run() 进入任务循环（第 1 个任务 sleep 中，~1.2s 处信号 → 任务边界 break）
        time.sleep(1.5)
        child.send_signal(signal.SIGTERM)
        out, _ = child.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        child.kill()
        pytest.fail("SIGTERM 后子进程未在 30s 内退出（优雅停止失败）")
    assert child.returncode == 0, \
        f"优雅停止必须 rc=0（driver 正常收尾），实际 {child.returncode}: {out}"

    # stats.stopped=True + processed < total（剩余任务未启动）
    stats_line = next((l for l in out.splitlines() if l.startswith("STATS ")), None)
    assert stats_line, f"子进程应打印 STATS: {out!r}"
    stats = json.loads(stats_line[len("STATS "):])
    assert stats.get("stopped") is True, f"stats 必须含 stopped=True: {stats}"
    assert stats["total"] == 5
    assert 0 <= stats["processed"] < 5, \
        f"信号到达后应停止处理剩余任务: {stats}"

    # progress 落盘：state=stopped_by_signal + done 明细 = 已处理部分（幂等键不毒化）
    with open(prog, encoding="utf-8") as f:
        prog_data = json.load(f)
    entry = next((t for t in prog_data["tasks"]
                  if t.get("table") == "kline_history" and t.get("tier") == "P2"), None)
    assert entry is not None, f"tasks 视图缺 kline_history/P2: {prog_data['tasks']}"
    assert entry["state"] == "stopped_by_signal", \
        f"state 必须=stopped_by_signal: {entry}"
    done_keys = [k for k in prog_data.get("done", []) if k[0] == "kline_history"]
    assert len(done_keys) == stats["processed"], \
        f"done 明细应=已处理任务数（未处理不标 done，下次续传）: {done_keys} vs {stats}"


def test_sigterm_during_last_task_still_graceful(tmp_path):
    """边界：信号在**最后一个任务执行期间**到达（无下一个任务边界可查）→ 循环自然
    走完后补查标志，仍走优雅停止收尾（state=stopped_by_signal + stopped=True）。"""
    prog = str(tmp_path / "prog_last.json")
    child = subprocess.Popen(
        [PY, "-c", _runner_child_code(prog, n_tasks=1, task_s=1.0)],
        stdout=subprocess.PIPE, text=True)
    try:
        time.sleep(0.5)   # 唯一任务 sleep 中
        child.send_signal(signal.SIGTERM)
        out, _ = child.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        child.kill()
        pytest.fail("SIGTERM 后子进程未退出")
    assert child.returncode == 0
    stats_line = next((l for l in out.splitlines() if l.startswith("STATS ")), None)
    stats = json.loads(stats_line[len("STATS "):])
    assert stats.get("stopped") is True, f"最后任务期间信号也要优雅收尾: {stats}"
    with open(prog, encoding="utf-8") as f:
        prog_data = json.load(f)
    entry = next(t for t in prog_data["tasks"] if t.get("table") == "kline_history")
    assert entry["state"] == "stopped_by_signal"


def test_no_signal_run_unaffected_and_handler_restored(tmp_path, monkeypatch):
    """无信号 run：行为与 v6.0.8 完全一致（无 stopped 键、state=running）；
    run() 结束 SIGTERM handler **恢复原值**（不留副作用）。"""
    from lake import backfill as lb

    prog = str(tmp_path / "prog_nosig.json")
    monkeypatch.setattr(lb.BackfillRunner, "_quota_state", lambda self: (False, 0))
    monkeypatch.setattr(lconn_module(), "get_conn",
                        lambda: (_ for _ in ()).throw(
                            lconn_module().LakeUnavailable("isolation")))
    prev_handler = signal.getsignal(signal.SIGTERM)

    tasks = [lb.Task(priority=2, table="kline_history", ts_code=f"sh.6{i:05d}",
                     period_or_date="full_history", tier="P2") for i in range(3)]
    r = lb.BackfillRunner(budget_per_day=5000, progress_path=prog)
    stats = r.run(tasks, lambda t: None)

    assert "stopped" not in stats, f"无信号 run 不得有 stopped 键: {stats}"
    assert stats["processed"] == 3
    with open(prog, encoding="utf-8") as f:
        prog_data = json.load(f)
    entry = next(t for t in prog_data["tasks"] if t.get("table") == "kline_history")
    assert entry["state"] == "running", f"无信号 run state 保持 running: {entry}"
    # handler 恢复（run 收尾 finally 还原原值）
    assert signal.getsignal(signal.SIGTERM) is prev_handler, \
        "run() 结束必须恢复原 SIGTERM handler（不留副作用）"


def lconn_module():
    from lake import conn

    return conn


def test_sigterm_resume_next_run_skips_done(tmp_path):
    """停止后续传：Run2（新 runner，同 progress 文件）→ 已 done 任务跳过零重取，
    剩余补完——"下次启动自动续传"的进度侧证据。"""
    prog = str(tmp_path / "prog_resume.json")
    child_code = _runner_child_code(prog, n_tasks=4, task_s=0.5)
    child = subprocess.Popen([PY, "-c", child_code], stdout=subprocess.PIPE, text=True)
    time.sleep(1.2)   # ~2 个任务完成后信号
    child.send_signal(signal.SIGTERM)
    out, _ = child.communicate(timeout=30)
    assert child.returncode == 0
    stats_line = next(l for l in out.splitlines() if l.startswith("STATS "))
    run1_stats = json.loads(stats_line[len("STATS "):])
    assert run1_stats["stopped"] is True and 0 < run1_stats["processed"] < 4

    # Run2：同文件新进程跑完剩余（无信号）
    child2_code = (
        f"import json, sys\nsys.path.insert(0, {REPO_ROOT!r})\n"
        "from lake import backfill as lb\n"
        "from lake import conn as lconn\n"
        "lb.BackfillRunner._quota_state = lambda self: (False, 0)\n"
        "lconn.get_conn = lambda: (_ for _ in ()).throw(lconn.LakeUnavailable('iso'))\n"
        f"prog = {prog!r}\n"
        # 同 _runner_child_code：{{i:05d}} 转义出字面 f-string（必须 f-string 行）
        f"tasks = [lb.Task(priority=2, table='kline_history', ts_code=f'sh.6{{i:05d}}',\n"
        "                  period_or_date='full_history', tier='P2') for i in range(4)]\n"
        "r = lb.BackfillRunner(budget_per_day=5000, progress_path=prog)\n"
        "stats = r.run(tasks, lambda t: None)\n"
        "print('STATS ' + json.dumps(stats), flush=True)\n"
    )
    child2 = subprocess.Popen([PY, "-c", child2_code], stdout=subprocess.PIPE, text=True)
    out2, _ = child2.communicate(timeout=30)
    assert child2.returncode == 0
    stats2_line = next(l for l in out2.splitlines() if l.startswith("STATS "))
    run2_stats = json.loads(stats2_line[len("STATS "):])
    assert run2_stats["skipped_done"] == run1_stats["processed"], \
        f"Run2 应跳过 Run1 已 done 部分（零重取）: {run2_stats} vs {run1_stats}"
    assert run2_stats["processed"] == 4 - run1_stats["processed"]
    with open(prog, encoding="utf-8") as f:
        prog_data = json.load(f)
    entry = next(t for t in prog_data["tasks"] if t.get("table") == "kline_history")
    assert (entry["done"], entry["total"]) == (4, 4), f"续传后应补完 4/4: {entry}"


# ===========================================================================
# 5. Web 端点：409 契约体（单元级）+ HTTP 全链路（真 uvicorn + fake holder）
# ===========================================================================
def test_web_sync_start_already_running_409_contract(monkeypatch):
    """start 已 running → LakeSyncConflict(409)；handler 渲染**顶层** error/hint，
    无 detail 键（v6.0.3 顶层契约体风格）。"""
    import lake.web_api as wapi
    from lake import sync_control

    monkeypatch.setattr(
        sync_control, "start_sync",
        lambda **k: {"started": False, "pid": 777, "log_path": "/tmp/s.log",
                     "reason": "already_running"})
    with pytest.raises(wapi.LakeSyncConflict) as ei:
        wapi.sync_start()
    assert ei.value.status_code == 409

    resp = wapi._lake_sync_conflict_handler(
        None, wapi.LakeSyncConflict("sync_already_running", "已有灌数在运行"))
    body = json.loads(resp.body.decode())
    assert resp.status_code == 409
    assert body["error"] == "sync_already_running"
    assert "hint" in body and body["hint"]
    assert "detail" not in body, f"不得有 detail 键（绕过契约体）: {body}"


def test_web_sync_stop_not_running_409_contract(monkeypatch):
    """stop 未 running → LakeSyncConflict(409) sync_not_running。"""
    import lake.web_api as wapi
    from lake import sync_control

    monkeypatch.setattr(
        sync_control, "stop_sync",
        lambda **k: {"stopped": False, "pid": None, "method": None,
                     "reason": "not_running"})
    with pytest.raises(wapi.LakeSyncConflict) as ei:
        wapi.sync_stop()
    assert ei.value.status_code == 409 and ei.value.error == "sync_not_running"


def test_web_sync_start_success_200_shape(monkeypatch):
    """start 成功 → 200 {started, pid, log_path}（None 值字段被过滤，不出现 null）。"""
    import lake.web_api as wapi
    from lake import sync_control

    monkeypatch.setattr(
        sync_control, "start_sync",
        lambda **k: {"started": True, "pid": 999, "log_path": "/tmp/s.log"})
    d = wapi.sync_start()
    assert d == {"started": True, "pid": 999, "log_path": "/tmp/s.log"}


def test_web_sync_stop_async_waiting_task_200(monkeypatch):
    """v6.0.10：stop 异步语义——信号已发 → **200** + waiting_task=true + note
    （不阻塞等退出；前端锁定按钮 + 轮询 /status 判完成）。"""
    import lake.web_api as wapi
    from lake import sync_control

    monkeypatch.setattr(
        sync_control, "stop_sync",
        lambda **k: {"stopped": False, "pid": 888, "method": "killpg",
                     "waiting_task": True,
                     "note": "信号已发，正在等当前任务收尾（进度已保存，下次启动自动续传）"})
    d = wapi.sync_stop()
    assert d["stopped"] is False and d["pid"] == 888 and d["method"] == "killpg"
    assert d["waiting_task"] is True, f"异步 stop 必须 waiting_task=true: {d}"
    assert "收尾" in d["note"], f"note 应说明正在收尾: {d}"


def test_web_sync_stop_signal_failed_200_reason(monkeypatch):
    """v6.0.10：信号未发出（holder pid 未知/发送失败）→ 200 + waiting_task=false
    + reason（前端 toast 提示 + 释放按钮，不误报"已发信号"）。"""
    import lake.web_api as wapi
    from lake import sync_control

    monkeypatch.setattr(
        sync_control, "stop_sync",
        lambda **k: {"stopped": False, "pid": None, "method": None,
                     "waiting_task": False,
                     "reason": "holder_pid_unknown（锁文案未解析出 PID，拒绝猜测目标进程）"})
    d = wapi.sync_stop()
    assert d["stopped"] is False and d["waiting_task"] is False
    assert d["reason"].startswith("holder_pid_unknown")


def test_http_sync_full_cycle_real_server(tmp_path):
    """**HTTP 全链路**（真 uvicorn + fake holder 持锁 tmp 库，零网络）：
    ① /status backfill_in_progress=true（v6.0.4 契约不变）；
    ② POST start → **409 sync_already_running**（顶层契约体）；
    ③ POST stop → **200 stopped=true**（SIGTERM 优雅杀掉 holder，降级 kill 路径——
       holder 非组长）；
    ④ /status 恢复无 backfill 键；再 POST stop → **409 sync_not_running**。"""
    p = str(tmp_path / "http_sync.duckdb")
    _seed_ready_db(p)
    fake = _write_fake_driver(tmp_path)
    holder = subprocess.Popen([PY, fake, "history", "--db", p],
                              stdout=subprocess.DEVNULL)
    port = _free_port()
    srv = _start_lake_server(port, p)
    try:
        assert _wait_ready(port), "uvicorn 未在超时内就绪"

        # 等 holder 持锁生效 → /status backfill_in_progress=true（v6.0.4 三态不变）
        d = None
        for _ in range(40):
            s, b = _http(port, "GET", "/api/lake/status")
            assert s == 200
            d = json.loads(b)
            if d.get("backfill_in_progress"):
                break
            time.sleep(0.25)
        assert d and d["backfill_in_progress"] is True, f"持锁期 /status 契约: {d}"
        assert d["lock_holder_pid"] == holder.pid

        # ② POST start → 409 sync_already_running（防双开，顶层契约体）
        s, b = _http(port, "POST", "/api/lake/sync/start")
        assert s == 409, f"已 running 时 start 必须 409，实际 {s}: {b}"
        bd = json.loads(b)
        assert bd.get("error") == "sync_already_running", f"{bd}"
        assert "hint" in bd and "detail" not in bd

        # ③ POST stop → **200 + waiting_task=true**（v6.0.10 异步：发信号即返回，
        #    SIGTERM；holder 无 setsid → 降级 kill）→ 轮询 /status 到锁释放
        t0 = time.monotonic()
        s, b = _http(port, "POST", "/api/lake/sync/stop")
        assert s == 200, f"stop 应 200，实际 {s}: {b}"
        bd = json.loads(b)
        assert time.monotonic() - t0 < 5.0, f"stop 必须异步立即返回: {bd}"
        assert bd.get("waiting_task") is True, f"信号已发应 waiting_task=true: {bd}"
        assert bd.get("pid") == holder.pid
        assert bd.get("method") in ("killpg", "kill")

        # ④ 轮询 /status 到锁释放（fake driver 默认 SIGTERM → 干净退出）；
        #    locked 态恒带 stopping 键（v6.0.10 三态契约扩展；fake holder 无 runner
        #    handler → progress 无标记 → stopping=false）
        d = None
        for _ in range(60):
            s, b = _http(port, "GET", "/api/lake/status")
            d = json.loads(b)
            if d.get("backfill_in_progress"):
                assert d.get("stopping") is False, f"无 runner 标记时 stopping=false: {d}"
            else:
                break
            time.sleep(0.25)
        assert "backfill_in_progress" not in d, f"停止后不得残留 backfill 键: {d}"

        s, b = _http(port, "POST", "/api/lake/sync/stop")
        assert s == 409, f"未 running 时 stop 必须 409，实际 {s}: {b}"
        bd = json.loads(b)
        assert bd.get("error") == "sync_not_running"
        assert "detail" not in bd
    finally:
        _stop_server(srv)
        if holder.poll() is None:
            holder.kill()
        holder.wait(timeout=10)


def test_http_status_three_state_contract_unchanged_v604(tmp_path):
    """/status 三态契约回归护栏（v6.0.9 不得破坏 v6.0.4）：uninitialized 态响应体
    与 v6.0.3/v6.0.4 逐字节一致，且**不混入**任何 sync 相关新键。"""
    import lake.web_api as wapi

    missing = str(tmp_path / "nope" / "lake.duckdb")
    port = _free_port()
    srv = _start_lake_server(port, missing)
    try:
        assert _wait_ready(port), "uvicorn 未在超时内就绪"
        s, b = _http(port, "GET", "/api/lake/status")
        assert s == 200
        d = json.loads(b)
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
        assert d == expect, f"未初始化 /status 必须逐字节不变: {d}"
    finally:
        _stop_server(srv)


# ===========================================================================
# 6. progress 路径派生（B-2 补全：自定义 --db 不显式传 progress_path 时落库目录）
# ===========================================================================
def test_progress_path_for_db_derivation():
    """progress_path_for_db：自定义 db → 同目录派生；缺省库/None → None（走
    _progress_path()，保留 monkeypatch 注入 + 原行为逐字节不变）。"""
    from lake import backfill as lb
    from lake import conn as lconn

    assert lb.progress_path_for_db(None) is None
    assert lb.progress_path_for_db("/tmp/custom/x.db") == "/tmp/custom/backfill_progress.json"
    assert lb.progress_path_for_db(lconn.default_db_path()) is None


def test_runner_custom_db_without_explicit_progress_writes_local(tmp_path):
    """BackfillRunner(db_path=tmp) **不显式传 progress_path** → 进度落 tmp 目录
    （E2E/冒烟自定义库绝不写生产 data/lake/backfill_progress.json）。"""
    from lake import backfill as lb

    prog_default = lb._progress_path()   # 生产路径（不得被写）
    db = str(tmp_path / "custom.duckdb")
    r = lb.BackfillRunner(budget_per_day=5000, db_path=db)
    assert r.progress_path == str(tmp_path / "backfill_progress.json"), \
        f"自定义 --db 进度必须落库目录: {r.progress_path}"
    assert r.progress_path != prog_default

    # 缺省库（db_path=None）→ 原行为不变（_progress_path()）
    r2 = lb.BackfillRunner(budget_per_day=5000)
    assert r2.progress_path == prog_default


# ===========================================================================
# 7. 前端契约（防 JS/HTML 与后端端点脱节，v6.0.4 F 段同模式）
# ===========================================================================
def test_frontend_sync_control_contract():
    """index.html 有同步控制区 + app.js 接两个 POST 端点 + confirm 文案 +
    backfill_in_progress 驱动按钮二态。

    v6.0.10：追加按钮锁定契约——停止点击即 disabled + "⏹ 停止中…" + 置灰类；
    启动锁定 "▶ 启动中…"；收尾轮询读 stopping；90s 文案升级 + 5min 硬超时释放。"""
    html = open(os.path.join(REPO_ROOT, "web", "static", "index.html"),
                encoding="utf-8").read()
    js = open(os.path.join(REPO_ROOT, "web", "static", "app.js"),
              encoding="utf-8").read()
    css = open(os.path.join(REPO_ROOT, "web", "static", "style.css"),
               encoding="utf-8").read()
    assert 'id="lake-sync-control"' in html, "缺同步控制区 #lake-sync-control"
    assert 'id="btn-lake-sync-toggle"' in html, "缺按钮 #btn-lake-sync-toggle"
    assert "/api/lake/sync/start" in js, "app.js 未接 POST start"
    assert "/api/lake/sync/stop" in js, "app.js 未接 POST stop"
    assert 'method: "POST"' in js
    assert "将启动全史数据补库（后台长跑，每日配额 5000 到顶自停）。确认启动？" in js
    assert "停止后进度已保存，下次启动自动续传。确认停止？" in js
    assert "backfill_in_progress" in js
    assert "▶ 启动同步" in js and "停止同步" in js, "按钮二态文案缺失"
    # v6.0.10 锁定契约（DOM 行为由 test_lake_v6010_stop_lock 的 node 契约测试覆盖）
    assert 'btn.disabled = true' in js, "点击后必须 disabled（锁定）"
    assert "⏹ 停止中…" in js, "停止锁定文案缺失"
    assert "▶ 启动中…" in js, "启动锁定文案缺失"
    assert "已停止，进度已保存" in js, "停止成功 toast 文案缺失"
    assert "停止超时，进程可能仍在收尾，请刷新查看" in js, "硬超时 toast 文案缺失"
    assert "当前任务收尾中，最长约几分钟" in js, "90s 文案升级缺失"
    assert "d.stopping === true" in js, "未读 /status stopping 标志（收尾友好文案）"
    assert "waiting_task" in js, "未处理 stop 异步响应 waiting_task"
    assert "sync-busy" in js and "sync-busy" in css, "锁定置灰类缺失（js/css 脱节）"


def test_install_registers_sync_conflict_handler():
    """install() 同时注册 v6.0.9 LakeSyncConflict handler（与 B-1/v6.0.4 并列）。"""
    from fastapi import FastAPI

    import lake.web_api as wapi

    app = FastAPI()
    wapi.install(app)
    handlers = getattr(app, "exception_handlers", {})
    assert wapi.LakeNotInitialized in handlers
    assert wapi.LakeBackfillInProgress in handlers
    assert wapi.LakeSyncConflict in handlers
