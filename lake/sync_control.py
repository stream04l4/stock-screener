# -*- coding: utf-8 -*-
"""lake.sync_control —— 数据湖同步控制（v6.0.9：Web"启动/停止灌数"按钮后端）。

设计（TL 拍板，brief v6.0.9）：
- **"启动"** = ``scripts/lake_backfill.py history``（P2 全史补库；driver 已支持
  ``--db``/``--codes``）。``subprocess.Popen(..., start_new_session=True)`` setsid
  脱离——Web 服务重启不影响灌数进程；stdout/stderr append 到 sync.log。
- **"停止"** = 优雅终止持锁进程（SIGTERM；BackfillRunner v6.0.9 注册 handler →
  任务间 break + state=stopped_by_signal + save_progress，rc=0）。**不升级 SIGKILL**
  （保守：DuckDB 语句原子性兜底，宁可让用户重试也不强杀可能正在写事务的进程）。
  **v6.0.10：异步语义**——发信号即返回（waiting_task=true），不再同步阻塞等退出；
  完成判定交给前端轮询 /status（backfill_in_progress=false=已停，stopping=true=收尾中）。
- **运行状态检测复用 v6.0.4 三态机制**：:func:`lake.conn.probe_db_state` +
  :class:`LakeLocked.holder_pid`（内部 :func:`parse_lock_holder_pid`）+
  ``os.kill(pid, 0)`` 活性确认。flock 锁持锁进程死亡自动释放，无 stale lock 问题；
  唯一要防的是"进程刚死、内核尚未释放锁"的毫秒级窗口——pid 活性确认后按**未运行**
  处理（避免误报 running 导致 stop 去 kill 一个已死的 pid）。

纪律：
- 本模块只读探测 + spawn/信号，**不直接写库**；对生产库的 start/stop 只能经 Web
  端点显式触发（E2E/测试一律 tmp 库 + 显式 db_path，绝不碰 data/lake/ 生产灌数）。
- sync.log 路径与 progress 文件同口径按库目录派生（B-2 模式）：缺省库 →
  ``data/lake/sync.log``（brief 指定路径）；自定义 --db /tmp/x.duckdb →
  ``/tmp/sync.log``（E2E 不污染生产目录）。
"""
from __future__ import annotations

import builtins
import os
import signal
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

__all__ = ["sync_status", "start_sync", "stop_sync"]


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
def _repo_root() -> str:
    # lake/ 的上一级 = 仓库根（~/stock-screener）
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def sync_script_path() -> str:
    """被 spawn 的 driver 脚本：scripts/lake_backfill.py。"""
    return os.path.join(_repo_root(), "scripts", "lake_backfill.py")


def _sync_log_path(db_path: Optional[str]) -> str:
    """sync.log 与库同目录派生（B-2 模式，见模块 docstring）。

    缺省库 → data/lake/sync.log（brief v6.0.9 指定路径，逐字一致）；
    自定义 --db → <db_dir>/sync.log（E2E tmp 库不写生产目录）。
    """
    from . import conn as lconn

    d = db_path or lconn.default_db_path()
    return os.path.join(os.path.dirname(os.path.abspath(d)), "sync.log")


def _pid_alive(pid: Optional[int]) -> bool:
    """os.kill(pid, 0) 活性探测：不存在→False；存在（含无权限的他人进程）→True。

    ⚠️ **僵尸进程（Z/defunct）必须判死**：进程已退出但父进程未 wait 收尸时，
    os.kill(pid,0) 对僵尸仍成功——Web 服务 spawn 灌数进程后从不调 Popen.wait()
    （Popen 对象随请求结束被 GC），停止/自然结束后若按"存活"判，stop_sync 的等待
    循环会空转到 timeout（实测复现）。Linux 读 /proc/<pid>/stat 的 state 字段识别：
    'Z' = defunct = 已死（flock 随进程退出已释放，DuckDB 侧无副作用）。
    非 Linux（/proc 不存在）→ 回退纯 os.kill 语义。
    """
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # 进程存在但不是我们的（同用户下罕见；保守按存活）
    try:
        with builtins.open(f"/proc/{pid}/stat", encoding="ascii") as f:
            # 格式 "pid (comm) state ..."——comm 可含空格/括号，rsplit(')',1) 取尾部
            state = f.read().rsplit(")", 1)[1].split()[0]
        if state == "Z":
            return False   # defunct：已退出未收尸 → 按死处理
    except (OSError, IndexError, ValueError):
        pass   # 非 Linux / 恰被回收 / 解析异常 → 维持 os.kill 结果
    return True


def _read_new(log_path: str, offset: int) -> str:
    """读日志 offset 之后的新增内容（spawn 后检测 traceback 用）。失败 → ""。"""
    try:
        size = os.path.getsize(log_path)
        if size <= offset:
            return ""
        with builtins.open(log_path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            return f.read()
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# 子进程回收（防僵尸）
# ---------------------------------------------------------------------------
# start_sync spawn 的 Popen 对象随请求结束被 GC；若此后子进程才退出，无人
# waitpid → **僵尸**（os.kill(pid,0) 对僵尸仍成功 → _pid_alive 误判存活，
# stop_sync 等待循环空转到 timeout——实测复现）。这里保留 pid→Popen 注册表，
# 探测/停止路径顺带 poll() 收尸（poll 内部 waitpid(WNOHANG)，已退出即回收）。
_CHILDREN: Dict[int, subprocess.Popen] = {}


def _reap_children() -> None:
    """回收已退出的子进程（poll() 触发 waitpid）；清掉注册表死项。"""
    for pid in list(_CHILDREN):
        try:
            if _CHILDREN[pid].poll() is not None:
                del _CHILDREN[pid]
        except Exception:  # noqa: BLE001 - 收尸失败不影响主流程
            pass


# ---------------------------------------------------------------------------
# 状态探测
# ---------------------------------------------------------------------------
def sync_status(db_path: Optional[str] = None) -> Dict[str, Any]:
    """同步任务运行状态：``{"running": bool, "pid": int|None}``。

    判定链（复用 v6.0.4 三态机制，不新造探测逻辑）：
    1. duckdb 未装 → 无法探测锁 → ``running=False``（不猜）。
    2. :func:`probe_db_state` ≠ "locked"（缺文件/坏库/可正常连接）→ 未运行。
    3. locked → 再经 :func:`connect_existing` 捕获 :class:`LakeLocked` 拿 holder_pid
       （内部走 ``parse_lock_holder_pid``，解析失败 None）：
       - pid 非空但**已死亡**（os.kill(pid,0) ESRCH）→ 未运行。为什么：持锁进程刚死、
         内核释放 flock 有毫秒级窗口；此时按 running 报会让 stop 去 kill 死 pid、
         让 start 误判双开——flock 会自动释放，稍后探测自然恢复。
       - pid=None（文案解析失败）→ **仍报 running=True**（与 v6.0.4 "降级 None 不猜"
         一致：锁确实被某进程持有 = 有灌数在跑）。
    """
    from . import conn as lconn

    db = db_path or lconn.default_db_path()
    _reap_children()   # 顺带收尸（防僵尸误判存活，见 _CHILDREN 注释）
    if not lconn.duckdb_available():
        return {"running": False, "pid": None}
    if lconn.probe_db_state(db) != "locked":
        return {"running": False, "pid": None}
    try:
        con = lconn.connect_existing(db)
    except lconn.LakeLocked as exc:
        pid = exc.holder_pid
    except Exception:  # noqa: BLE001 - probe 与 connect 之间锁释放/其它 IO 错 → 未运行
        return {"running": False, "pid": None}
    else:
        # probe 说 locked 但 connect 成功（窗口内锁已释放）→ 未运行
        try:
            con.close()
        except Exception:  # noqa: BLE001
            pass
        return {"running": False, "pid": None}
    if pid is not None and not _pid_alive(pid):
        return {"running": False, "pid": None}
    return {"running": True, "pid": pid}


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------
def start_sync(db_path: Optional[str] = None, sub: str = "history",
               extra_args: Optional[List[str]] = None) -> Dict[str, Any]:
    """启动灌数（``python scripts/lake_backfill.py <sub>``，setsid 脱离长跑）。

    :param db_path: 非空 → 追加 ``--db <path>``（缺省=生产默认库）。
    :param sub: 子命令（Web 按钮固定 "history"；driver 已支持 init/p0/history/status）。
    :param extra_args: 透传参数（如 ["--codes", "sh.601398"]，测试/E2E 用）。

    :return: ``{"started": bool, "pid": int|None, "log_path": str, "reason": str?}``
    - 已 running → ``started=False, reason="already_running"``（**不 spawn**，防双开；
      Web 层映射 409）。
    - spawn 后轮询 ~5s（每 0.5s）确认 running：锁出现（probe=locked）**或** pid 存活
      + 日志无 traceback（brief 字面条件——driver import/建连需数秒，不能只认锁）。
      失败（进程提前退出 / 日志现 Traceback）→ ``started=False`` + reason。
    """
    from . import conn as lconn

    db = db_path or lconn.default_db_path()
    st = sync_status(db)
    if st["running"]:
        return {"started": False, "pid": st["pid"],
                "log_path": _sync_log_path(db), "reason": "already_running"}

    script = sync_script_path()
    # ⚠️ 参数顺序：driver 的 --db 是**全局 flag（子命令之前）**——argparse 定义在顶层
    # parser，``history --db x`` 会报 "unrecognized arguments"（E2E 实测 rc=2）。
    # 恒传解析后的实际库路径：Web 端点以"服务进程默认库"调用 start_sync()
    # （db_path=None → db=default_db_path()，可能被测试/部署 patch）——若不显式透传，
    # 子进程 driver 会用它**自己的**默认库（生产 data/lake/），与父进程探测的库不一致。
    # 对真实缺省库：--db <default> 与 driver 缺省行为逐字等价（progress 派生亦回退）。
    cmd: List[str] = [sys.executable, script, "--db", db, sub]
    if extra_args:
        cmd += list(extra_args)

    log_path = _sync_log_path(db)
    log_fh = None
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        log_fh = builtins.open(log_path, "a", encoding="utf-8")
        offset0 = os.path.getsize(log_path)
        proc = subprocess.Popen(
            cmd, start_new_session=True,   # setsid 脱离：Web 重启不影响灌数进程
            stdout=log_fh, stderr=subprocess.STDOUT)
    except OSError as exc:
        return {"started": False, "pid": None, "log_path": log_path,
                "reason": f"spawn_failed: {exc}"}
    finally:
        # Popen 已为子进程 dup fd；父进程副本立即关闭（防泄漏，子进程不受影响）
        if log_fh is not None:
            try:
                log_fh.close()
            except Exception:  # noqa: BLE001
                pass

    pid = proc.pid
    _CHILDREN[pid] = proc   # 注册表持有引用：请求结束后仍可 poll() 收尸（防僵尸）
    deadline = time.monotonic() + 5.0
    confirmed = False
    reason: Optional[str] = None
    while time.monotonic() < deadline:
        time.sleep(0.5)
        rc = proc.poll()
        if rc is not None:
            # 进程提前退出（参数错/库无效 rc=3 / 异常 rc=1）→ 启动失败
            tail = _read_new(log_path, offset0).strip().splitlines()
            reason = (f"process exited within 5s (rc={rc})"
                      + (f"; last: {tail[-1][:200]}" if tail else ""))
            break
        new_log = _read_new(log_path, offset0)
        if "Traceback (most recent call last)" in new_log:
            reason = f"traceback in log: {new_log.strip().splitlines()[-1][:200]}"
            break
        # brief 确认条件：锁出现 或 pid 存活+日志无 traceback（此处 rc is None=存活）
        if lconn.duckdb_available() and lconn.probe_db_state(db) == "locked":
            confirmed = True
            break
        confirmed = True   # pid 存活 + 无 traceback → 视为 running（driver 尚在启动期）
        break
    if not confirmed:
        return {"started": False, "pid": pid, "log_path": log_path, "reason": reason}
    return {"started": True, "pid": pid, "log_path": log_path}


# ---------------------------------------------------------------------------
# 停止
# ---------------------------------------------------------------------------
def stop_sync(db_path: Optional[str] = None) -> Dict[str, Any]:
    """优雅停止灌数（SIGTERM；BackfillRunner 任务间 break + 进度落盘，rc=0）。

    **v6.0.10：异步语义**——发信号即返回，**不再同步阻塞等进程退出**。为什么：
    SIGTERM 后 runner 要等"当前正在执行的任务跑完"才 break（BaoStock/腾讯重试
    退避时单任务可能卡几分钟），旧实现轮询到 timeout=30s 才响应 → Web 请求挂死、
    前端无反馈（Joel 实测"点了没反应"）。现改为：发信号 + 记录 pid 立即返回，
    **完成判定交给前端轮询 /status**（backfill_in_progress=false = 已停；stopping
    =true = 收尾中友好文案）。

    :return: ``{"stopped": bool, "pid": int|None, "method": str|None,
      "waiting_task": bool, "note": str?, "reason": str?}``
    - 未 running → ``stopped=False, waiting_task=False, reason="not_running"``
      （Web 层映射 409）。
    - holder pid 解析失败（None）→ **拒绝猜测**：``stopped=False,
      waiting_task=False, reason="holder_pid_unknown"``（不 kill 未知进程）。
    - 信号发送成功 → **立即** ``stopped=False, waiting_task=True`` + note
      （"信号已发，正在等当前任务收尾"）——前端据此显示友好文案并轮询 /status。
    - 信号发送失败（进程恰在窗口内退出等）→ ``stopped=False, waiting_task=False,
      reason="signal failed: ..."``。

    信号策略（不变）：先 ``os.killpg(pid, SIGTERM)``（本按钮 spawn 的进程 setsid
    自成组，pgid==pid，整组一次干净）；ProcessLookupError/PermissionError → 降级
    ``os.kill(pid, SIGTERM)``——覆盖**非本按钮启动**的进程。**不升级 SIGKILL**
    （保守：DuckDB 语句原子性兜底）。
    """
    st = sync_status(db_path)   # 内部已 _reap_children（收尸后再判活性）
    if not st["running"]:
        return {"stopped": False, "pid": st["pid"], "method": None,
                "waiting_task": False, "reason": "not_running"}
    pid = st["pid"]
    if pid is None:
        return {"stopped": False, "pid": None, "method": None,
                "waiting_task": False,
                "reason": "holder_pid_unknown（锁文案未解析出 PID，拒绝猜测目标进程）"}

    method: Optional[str] = None
    try:
        os.killpg(pid, signal.SIGTERM)
        method = "killpg"
    except (ProcessLookupError, PermissionError):
        # 非本按钮启动的进程（不是组长/组已散）→ 单发信号覆盖
        try:
            os.kill(pid, signal.SIGTERM)
            method = "kill"
        except (ProcessLookupError, PermissionError) as exc:
            return {"stopped": False, "pid": pid, "method": None,
                    "waiting_task": False, "reason": f"signal failed: {exc}"}

    # v6.0.10：发信号即返回（不等退出）。进程退出与否由前端轮询 /status 判定——
    # backfill_in_progress=false = 已停；stopping=true = 收尾中（progress 文件标记，
    # BackfillRunner SIGTERM handler 落盘）。stopped 字段语义保留但恒 False（异步下
    # 本调用不确认退出）；前端契约以 waiting_task + /status 为准。
    return {"stopped": False, "pid": pid, "method": method, "waiting_task": True,
            "note": "信号已发，正在等当前任务收尾（进度已保存，下次启动自动续传）"}
