# -*- coding: utf-8 -*-
"""lake.backfill —— 补齐任务调度（优先级队列 + 幂等断点续传 + 进度落盘，报告 §4）。

设计（调研报告 §4）：
- **优先级队列**：P1 候选池 > P2 白名单行业全量 > P3 其余市场。队列元素
  ``(priority, table, ts_code, period_or_date)``，按 priority 升序消费；每表独立游标。
- **限速/守卫集成**：BaoStock→BaoStockClient（QuotaGuard）+ lake 侧**日预算上限**
  （默认 5,000 次/日，config 可调），到顶当日停、次日续；新浪/腾讯走各自客户端限速。
- **幂等断点续传**：进度文件 ``data/lake/backfill_progress.json``，键
  ``(table, ts_code, period_or_date)``→done；落盘即标记；中断重跑先读 progress
  跳过 done 键 → **不重复耗配额**。写入 INSERT OR REPLACE（PK 表）幂等。
- **v6.0.8 tasks 视图回填**：run() 开始处按 (table, tier) 分组统计 total、done
  （从 ``self._done`` 集合计数——断点续传重启后视图立即反映真实进度）；
  ``_update_task_view()``/收尾同步写 entry 的 total/done + eta_min（最近 N 个成功
  任务平均耗时推算剩余分钟，无数据 None），并逐任务落盘（Web 3s 轮询可见）。

⚠️ 本模块**只做调度骨架 + 进度管理**，具体取数委托 ingest 层（复用客户端）。
后台慢慢补——不在筛选热路径上。
"""
from __future__ import annotations

import json
import logging
import math
import os
import signal
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

log = logging.getLogger("lake.backfill")


# ---------------------------------------------------------------------------
# 进度文件（§4 结构）
# ---------------------------------------------------------------------------
def _progress_path() -> str:
    from .conn import progress_path

    return progress_path()


def _production_default_progress() -> str:
    """真实生产默认 progress 路径（**不经** :func:`_progress_path`——不可被 monkeypatch）。

    v6.0.9 派生判定专用：只有 ``_progress_path()`` 返回的就是这个真实生产路径时，
    自定义 --db 才改派生到库目录；测试 patch 了 _progress_path（指向 tmp）→ 不派生，
    patch 继续生效（v6.0.2 冒烟续跑 / v6.0.7 history 离线用例依赖它）。
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "data", "lake", "backfill_progress.json")


def progress_path_for_db(db_path: Optional[str]) -> Optional[str]:
    """**v6.0.9（B-2 补全）**：progress 文件路径按库目录派生。

    - ``db_path`` 非空且不是缺省库 → ``<db_dir>/backfill_progress.json``（自定义 --db
      的进度与库同目录，不再落到生产 data/lake/）。
    - ``db_path`` 为 None / 缺省库路径 → **返回 None**（调用方走 :func:`_progress_path`
      = data/lake/backfill_progress.json——与原硬编码逐字节一致，且保留测试对
      _progress_path 的 monkeypatch 注入能力）。

    为什么放在 backfill 模块而非只在 driver：BackfillRunner(db_path=tmp) **不显式传
    progress_path** 时（driver run_p0/run_history 正是这种构造）也必须落到 tmp 目录——
    否则 E2E/冒烟的自定义库进度会写进生产 data/lake/backfill_progress.json。

    ⚠️ 判定基准是 **_progress_path() 的实际返回值**：测试 patch 了 _progress_path →
    返回 None（不派生，patch 生效）；只有真实生产默认路径 + 自定义 --db 才派生。
    """
    if not db_path:
        return None
    base = _progress_path()
    if os.path.abspath(base) != _production_default_progress():
        return None   # _progress_path 被 patch（测试注入）→ 不派生，保持原行为
    d = os.path.dirname(os.path.abspath(db_path))
    derived = os.path.join(d, "backfill_progress.json")
    if os.path.abspath(derived) == base:
        return None   # 缺省库：回退 _progress_path()（保持原行为 + 可 patch）
    return derived


def load_progress(path: Optional[str] = None) -> Dict[str, Any]:
    """读进度文件；缺失/损坏 → 空结构（fail-open：从头补，INSERT OR REPLACE 幂等）。"""
    p = path or _progress_path()
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data.setdefault("tasks", [])
            data.setdefault("coverage", {})
            return data
    except (OSError, ValueError, TypeError):
        pass
    return {"updated_at": None, "tasks": [], "coverage": {}}


def save_progress(progress: Dict[str, Any], path: Optional[str] = None) -> None:
    """原子写进度文件（tmp+rename）。"""
    import datetime as _dt

    p = path or _progress_path()
    progress["updated_at"] = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    # v6.0.8：dirname 为空（裸文件名路径）时 makedirs('') 会 ENOENT → 回退 "."
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".lake_prog_", suffix=".tmp", dir=os.path.dirname(p))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(progress, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# 队列元素 + 幂等键
# ---------------------------------------------------------------------------
@dataclass(order=True)
class Task:
    """优先级队列元素。order=True → 按 (priority, table, ts_code, key) 排序消费。"""

    priority: int          # P1=1 < P2=2 < P3=3（升序消费）
    table: str = field(compare=True)
    ts_code: str = field(compare=True)
    period_or_date: str = field(compare=True, default="")
    tier: str = field(compare=False, default="P1")


def task_key(task: Task) -> Tuple[str, str, str]:
    """幂等键 (table, ts_code, period_or_date)。"""
    return (task.table, task.ts_code, task.period_or_date)


# ---------------------------------------------------------------------------
# v6.0.10：全局停止标志（"收尾等待"可观测 + 重试循环提前中断）
# ---------------------------------------------------------------------------
# Web"停止同步"按钮 → sync_control.stop_sync 发 SIGTERM → BackfillRunner handler
# 置 _stop_requested **并**置本模块级标志。为什么需要模块级（handler 在灌数子进程、
# Web 进程读不到它的内存）：
# - **可观测**：handler 同步把 progress tasks[].state="stopping" + save_progress，
#   Web /status 读 progress 文件即可区分"正常运行中" vs "停止收尾中"（跨进程共享
#   的只有库目录下的 JSON 文件）；
# - **加速收尾**（brief §3 可选项，实现成本低故做）：BaoStock/腾讯重试循环的退避
#   sleep 前检查本标志 → 收到停止信号立即中断当前重试（不必等满指数退避），
#   单任务从"卡几分钟"降到秒级收尾。
# 生命周期：run() 开始处清零（防上一轮残留毒化新 run）；进程退出即消失（模块级，
# 无持久状态）。
_STOP_FLAG = False


def set_stop_requested() -> None:
    """置全局停止标志（SIGTERM handler / 测试用）。"""
    global _STOP_FLAG
    _STOP_FLAG = True


def clear_stop_requested() -> None:
    """清全局停止标志（run() 开始处调用，防上一轮残留）。"""
    global _STOP_FLAG
    _STOP_FLAG = False


def stop_requested() -> bool:
    """当前是否已请求停止。"""
    return _STOP_FLAG


class StopRequestedError(RuntimeError):
    """v6.0.10：重试循环收到停止信号 → 提前中断（不等满退避）。

    与网络失败同走 worker 的 except 分支（记 errors、**不 mark_done**）——下轮重跑
    幂等续传；语义上"用户主动停止"不是数据错误，但复用既有失败路径最安全（不新造
    控制流）。
    """


def _check_stop() -> None:
    """重试循环 sleep 前调用：已请求停止 → 抛 StopRequestedError 提前中断。"""
    if _STOP_FLAG:
        raise StopRequestedError("收到停止信号，提前中断当前重试（收尾加速）")


# ---------------------------------------------------------------------------
# 调度器
# ---------------------------------------------------------------------------
class BackfillRunner:
    """优先级队列消费 + 幂等断点续传 + 日预算守卫。

    :param budget_per_day: BaoStock 日预算上限（默认 5000，config 可调；Q2）。
        到顶当日停（state=blocked_quota），次日自动续（QuotaGuard 日期翻转重置）。
    :param progress_path: 进度文件路径（测试可注入 tmp）。
    :param db_path: **B-2（v6.0.3）** runner 自身库路径。缺省 None = coverage 沿用进程级
        单例 get_conn()（默认库，行为不变）；传自定义 --db 时 _refresh_coverage 改读该库
        （见 :meth:`_coverage_conn`）。⚠️ **不**据此派生 progress 路径——progress 仍走
        ``progress_path or _progress_path()``（保持 v6.0.2 冒烟测试对 _progress_path 的
        monkeypatch 注入 + 缺省行为逐字节不变；status 报表的 progress 路径由 driver 侧
        run_status 按 db_path 派生，见 brief B-2）。
    """

    def __init__(self, budget_per_day: Optional[int] = None,
                 progress_path: Optional[str] = None,
                 db_path: Optional[str] = None) -> None:
        # Q2：日预算默认 5000（config 可调）——缺省从 strategy.yaml lake 段读
        if budget_per_day is None:
            from .config import lake_cfg

            budget_per_day = int(lake_cfg().get("baostock_daily_budget", 5000))
        self.budget_per_day = int(budget_per_day)
        # B-2：runner 自身库路径（None=缺省库，coverage 走 get_conn 单例；自定义 --db 时
        # _refresh_coverage 改读该库）。progress 路径逻辑保持不变（见 docstring 说明）。
        self.db_path = db_path
        # v6.0.9（B-2 补全）：显式 progress_path 优先（测试注入/原行为不变）；未显式传
        # 且自定义 --db → 按库目录派生（E2E/tmp 库进度不写生产 data/lake/）；缺省库 →
        # _progress_path()（与原硬编码逐字节一致，保留 monkeypatch 注入能力）。
        # ⚠️ 判定基准是 **_progress_path() 的实际返回值**而非字面默认值：测试对
        # _progress_path 的 monkeypatch（v6.0.2 冒烟续跑 / v6.0.7 history 离线用例）
        # 必须继续生效——只有"返回真实生产默认路径 + 自定义 --db"才派生。
        base_progress = progress_path or _progress_path()
        if (not progress_path and db_path
                and os.path.abspath(base_progress) == _production_default_progress()):
            base_progress = os.path.join(
                os.path.dirname(os.path.abspath(db_path)), "backfill_progress.json")
        self.progress_path = base_progress
        self.progress = load_progress(self.progress_path)
        # done 键集合（内存缓存，避免每任务重读文件）
        self._done: set = set()
        for t in self.progress.get("tasks", []):
            pass  # tasks 是聚合视图；done 明细见下
        self._load_done()
        # v6.0.8：tasks 视图 total/done 回填 + ETA 所需状态。
        # _view_groups：(table, tier) → {"total": int, "done_at_start": int}——
        #   run() 开始处按本次队列分组统计；done 从 self._done 集合计数（断点续传
        #   重启后视图立即反映真实进度，不依赖本次 run 增量）。
        # _recent_durs：最近 N 个成功任务耗时（秒）环形缓冲 → eta_min 推算。
        self._view_groups: Dict[Tuple[str, str], Dict[str, int]] = {}
        self._recent_durs: Deque[float] = deque(maxlen=20)
        # v6.0.9：SIGTERM 优雅停止标志（Web"停止同步"按钮 → sync_control.stop_sync
        # killpg/kill SIGTERM）。run() 注册 handler 置位；主循环**任务间**检查 break。
        self._stop_requested = False
        # v6.0.10：SIGTERM handler 置 _stop_requested 时同步落盘的"停止收尾中"标记
        # （progress 顶层 stopping_at，Web /status 读 progress 文件得 stopping=true——
        # 跨进程可观测的唯一载体是库目录下的 JSON 文件）。run() 开始处清除。
        self.progress.setdefault("stopping_at", None)

    # ---------- done 键管理 ----------
    def _load_done(self) -> None:
        """从进度文件读 done 明细（self.progress['done'] = [key,...]）。"""
        self._done = set(tuple(k) for k in self.progress.get("done", []))

    def mark_done(self, task: Task) -> None:
        """落盘即标记 done（幂等：重复标记无副作用；写进度文件）。"""
        key = task_key(task)
        if key not in self._done:
            self._done.add(key)
            self.progress.setdefault("done", [])
            # 去重后写回（list of [table, ts_code, period]）
            existing = set(tuple(x) for x in self.progress["done"])
            existing.add(key)
            self.progress["done"] = [list(k) for k in sorted(existing)]
            save_progress(self.progress, self.progress_path)

    def is_done(self, task: Task) -> bool:
        return task_key(task) in self._done

    # ---------- 日预算守卫 ----------
    def _quota_state(self) -> Tuple[bool, int]:
        """返回 (是否到顶, 今日已用)。读 QuotaGuard 状态文件（跨进程共享计数）。"""
        try:
            from screener.data.baostock_client import QuotaGuard

            guard = QuotaGuard(path=self._quota_path())
            date_s, used = guard.get_state()
            return (used >= self.budget_per_day, used)
        except Exception:  # noqa: BLE001 - 守卫不可用时不阻断（fail-open，靠 QuotaGuard 硬上限兜底）
            return (False, 0)

    def _quota_path(self) -> Optional[str]:
        """复用默认配额文件路径（~/.stock_screener/bs_quota.json）。"""
        try:
            from screener.data.baostock_client import default_quota_path

            return default_quota_path()
        except Exception:  # noqa: BLE001
            return None

    def quota_used_today(self) -> int:
        _, used = self._quota_state()
        return used

    # ---------- 主循环 ----------
    def _install_stop_handler(self) -> Optional[Any]:
        """v6.0.9：注册 SIGTERM handler（仅主线程）→ 置 ``_stop_requested``。

        - **只置标志、不做事**：handler 内不碰 runner 状态/文件——信号可能在 worker
          执行中途到达，正在执行的任务让它跑完（不中断事务），主循环在**任务间**检查
          标志 break；DuckDB 语句原子性兜底（已提交的不回滚、未提交的随语句结束）。
        - **仅主线程安全场景**：driver 是单线程主循环（run() 在主线程调用）；非主线程
          注册 signal handler 会抛 ValueError → 降级默认行为（SIGTERM 直接杀进程，
          不 crash、不改变既有语义——brief："注册失败 → 降级默认行为"）。
        - 返回原 handler（None=未注册/已保存 SIG_DFL），run() 收尾恢复。
        """
        if threading.current_thread() is not threading.main_thread():
            return None
        try:
            prev = signal.signal(signal.SIGTERM, self._on_stop_signal)
        except (ValueError, OSError):  # noqa: BLE001 - 非主线程/平台不支持 → 降级默认
            log.warning("SIGTERM handler 注册失败（%s）→ 降级默认行为",
                        threading.current_thread().name)
            return None
        self._prev_term_handler = prev
        return prev

    def _on_stop_signal(self, signum, frame):  # noqa: ARG002 - signal handler 签名固定
        """SIGTERM → 置停止标志（主循环任务间检查）。

        v6.0.10：同时置**模块级**停止标志（BaoStock/腾讯重试循环的退避 sleep 前检查
        → 提前中断当前重试，收尾从"等满指数退避几分钟"降到秒级）+ 落盘"停止收尾中"
        标记（progress tasks[].state="stopping" + stopping_at + save_progress——Web
        /status 据此报 stopping=true，前端显示友好文案而非"没反应"）。

        handler 仍保持最简：不碰 DuckDB/网络（信号可能到达于任意指令之间）；只改内存
        标志 + 一次纯 JSON 文件写（save_progress 是 tmp+rename 原子写，无事务风险）。
        """
        self._stop_requested = True
        set_stop_requested()   # v6.0.10：模块级标志（重试循环提前中断）
        self._mark_stopping()
        log.info("收到 SIGTERM（signum=%s）→ 将在当前任务完成后优雅停止", signum)

    def _mark_stopping(self) -> None:
        """v6.0.10：落盘"停止收尾中"标记（handler 同步调用，Web /status 可观测）。

        - progress 顶层 ``stopping_at`` = 当前 UTC 时间戳（非空=收尾中；run() 开始处
          与 _finish_stop 收尾时清除）——/status 的 stopping 字段读它；
        - tasks[].state="stopping"（**全部** entry，含尚未创建 entry 的组不在此列——
          首个任务执行中被信号打断时 tasks 视图可能尚无 entry，此时只有 stopping_at
          生效，/status 照样报 stopping=true）；
        - save_progress 原子落盘（tmp+rename）。失败不抛（handler 上下文：任何异常
          都会让进程直接死掉，比"标记没写上"严重得多——最坏情况退化为 v6.0.9 行为：
          前端靠轮询 backfill_in_progress 判完成，只是少了友好文案）。
        """
        import datetime as _dt

        try:
            self.progress["stopping_at"] = _dt.datetime.now(
                _dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            for entry in self.progress.get("tasks", []):
                if isinstance(entry, dict):
                    entry["state"] = "stopping"
            save_progress(self.progress, self.progress_path)
        except Exception as exc:  # noqa: BLE001 - handler 上下文：落盘失败不致命
            log.warning("停止标记落盘失败（不影响优雅停止本身）: %s", exc)

    def _restore_stop_handler(self) -> None:
        """run() 收尾恢复原 SIGTERM handler（不留副作用给后续代码/测试）。"""
        prev = getattr(self, "_prev_term_handler", None)
        if prev is None:
            return
        try:
            signal.signal(signal.SIGTERM, prev)
        except (ValueError, OSError):  # noqa: BLE001 - 恢复失败不致命（进程即将退出）
            pass
        self._prev_term_handler = None

    def _finish_stop(self, stats: Dict[str, Any]) -> None:
        """v6.0.9：SIGTERM 优雅停止收尾——state=stopped_by_signal + save_progress + 日志。

        先 :meth:`_refresh_task_view`（v6.0.8）确保 entry 存在且 total/done 回填
        （信号在首个任务执行中到达时 tasks 视图可能尚无任何 entry），再对全部 entry
        置 state="stopped_by_signal" + save_progress（brief 字面）。state 不走
        _update_task_view——"停止"是 run 级事件而非单任务事件；后续收尾的
        _refresh_task_view 不覆盖 state（v6.0.8 语义），最终文件即 stopped 态。
        done 明细不受影响（已落盘即有效，下次启动自动续传）。

        v6.0.10：同时清除 ``stopping_at``（收尾完成 = 不再"停止中"；进程即将退出、
        锁释放后 /status 回 ready 态，但文件是最后状态——不留残留标记给下一轮/读取方）。
        """
        done_total = sum(
            g["done_at_start"] + g["done_in_run"] for g in self._view_groups.values())
        self._refresh_task_view()
        for entry in self.progress.get("tasks", []):
            if isinstance(entry, dict):
                entry["state"] = "stopped_by_signal"
        self.progress["stopping_at"] = None   # v6.0.10：收尾完成，清除"停止中"标记
        save_progress(self.progress, self.progress_path)
        stats["stopped"] = True
        log.info("STOPPED by signal, progress saved (done=%d)", done_total)

    def run(self, tasks: List[Task], worker: Callable[[Task], None]) -> Dict[str, Any]:
        """按优先级消费队列；幂等跳过 done；日预算到顶停。

        :param tasks: 待补任务列表（内部按 priority 排序）。
        :param worker: 单任务取数+入库回调（由调用方注入，委托 ingest 层）。
        :return: {total, skipped_done, processed, blocked_quota, errors}。

        幂等保证：每个 task 先查 is_done → 跳过（**不耗配额**）；worker 成功后
        mark_done 落盘。中断重跑 → done 键已落盘 → 直接跳过，绝不重复调 BaoStock。

        v6.0.8：run 开始处按 (table, tier) 分组统计 total/done_at_start（done 从
        ``self._done`` 集合计数）→ tasks 视图立即回填真实进度（修复 Web 恒 0/0）。

        v6.0.9：**SIGTERM 优雅停止**（Web"停止同步"按钮 → sync_control.stop_sync
        发 SIGTERM）。run() 注册 handler 置 ``_stop_requested``；主循环每任务边界
        （worker 之前）检查 → break，写 state="stopped_by_signal" + save_progress +
        stats["stopped"]=True（driver 正常收尾 rc=0）。正在执行的任务不中断——信号
        到达时让它跑完（DuckDB 语句原子性兜底）。handler 在 run() 结束恢复原 handler；
        非主线程/注册失败 → 降级默认行为（不 crash）。
        """
        self._install_stop_handler()
        try:
            return self._run_inner(tasks, worker)
        finally:
            self._restore_stop_handler()

    def _run_inner(self, tasks: List[Task], worker: Callable[[Task], None]) -> Dict[str, Any]:
        """run() 主体（v6.0.9：从 run() 拆出以便 try/finally 恢复 SIGTERM handler）。"""
        ordered = sorted(tasks)  # Task order=True：priority 升序
        # v6.0.10：新 run 开始处清除上一轮残留——progress stopping_at（若上轮被强杀、
        # _finish_stop 没跑到，文件可能残留"停止中"标记，/status 会误报 stopping=true）
        # + 模块级停止标志（同进程多轮 run 的测试场景防串味）。
        self.progress["stopping_at"] = None
        clear_stop_requested()
        stats = {"total": len(ordered), "skipped_done": 0, "processed": 0,
                 "blocked_quota": False, "errors": []}
        # v6.0.8：视图分组统计——done_at_start 只数**本次队列内**已 done 的任务：
        # 断点续传重启后立即反映真实进度，同时避免同表旧期（如 valuation_daily
        # 昨日 as_of）残留 done 键虚增计数。
        self._view_groups = {}
        for task in ordered:
            g = self._view_groups.setdefault(
                (task.table, f"P{task.priority}"),
                {"total": 0, "done_at_start": 0, "done_in_run": 0})
            g["total"] += 1
            if self.is_done(task):
                g["done_at_start"] += 1
        for task in ordered:
            # v6.0.9：任务边界检查停止标志（worker 之前）——正在执行的任务跑完，
            # 下一个任务不再启动。放在 is_done 之前：停止后连"跳过"也不再发生，
            # 立即进入收尾（进度已落盘的部分下次续传）。
            if self._stop_requested:
                log.info("SIGTERM 优雅停止：任务 %s 前 break（done 已落盘部分下次续传）",
                         task_key(task))
                self._finish_stop(stats)
                break
            if self.is_done(task):
                stats["skipped_done"] += 1
                continue
            blocked, used = self._quota_state()
            if blocked:
                log.warning("日预算到顶 (%d/%d) → 当日停，次日续（state=blocked_quota）",
                            used, self.budget_per_day)
                stats["blocked_quota"] = True
                self._update_task_view(task, "blocked_quota", used)
                break
            try:
                t0 = time.monotonic()
                worker(task)
                self.mark_done(task)
                stats["processed"] += 1
                # v6.0.8：耗时入环形缓冲（eta_min 推算）+ 分组 done 计数
                self._recent_durs.append(time.monotonic() - t0)
                g = self._view_groups.get((task.table, f"P{task.priority}"))
                if g is not None:
                    g["done_in_run"] += 1
                self._update_task_view(task, "running", used)
            except Exception as exc:  # noqa: BLE001 - 单任务失败不中断整队列
                log.error("backfill %s 失败: %s", task_key(task), exc)
                stats["errors"].append(f"{task_key(task)}: {exc}")
                self._update_task_view(task, "error", used)
        # v6.0.9：信号在**最后一个任务执行期间**到达的边界——主循环已自然走完（没有
        # 下一个任务边界可检查），此处补查停止标志 → 同样走优雅停止收尾。
        if self._stop_requested:
            self._finish_stop(stats)
        # 收尾：v6.0.8 视图回填（含零处理 run——全跳过/预算到顶也要刷新 total/done）
        self._refresh_task_view()
        # 收尾：写 coverage（各表行数/代码数/年份范围）
        self._refresh_coverage()
        save_progress(self.progress, self.progress_path)
        return stats

    def _view_done_count(self, table: str, tier: str) -> int:
        """v6.0.8：该 (table, tier) 已完成数 = run 开始已 done + 本次 run 新增。"""
        g = self._view_groups.get((table, tier))
        if g is None:
            return 0
        return g["done_at_start"] + g["done_in_run"]

    def _eta_min(self) -> Optional[int]:
        """v6.0.8：ETA（分钟）= 剩余任务数 × 最近 N 个成功任务平均耗时；
        无成功样本 → None（前端显示 —）。取整向上，最小 1。"""
        if not self._recent_durs:
            return None
        avg = sum(self._recent_durs) / len(self._recent_durs)
        remaining = 0
        for g in self._view_groups.values():
            remaining += max(0, g["total"] - g["done_at_start"] - g["done_in_run"])
        if remaining <= 0:
            return None
        eta = math.ceil(remaining * avg / 60.0)
        return max(eta, 1)

    def _refresh_task_view(self) -> None:
        """v6.0.8：run 收尾统一回填 tasks 视图 total/done/eta_min。

        逐组写入（含零处理 run）——修复"entry 创建后 total/done 从未赋值 →
        Web 恒 0/0"。state/quota 仍由 :meth:`_update_task_view` 按事件更新，
        本方法不覆盖 state。
        """
        for (table, tier), g in self._view_groups.items():
            entry = next((t for t in self.progress["tasks"]
                          if t.get("table") == table and t.get("tier") == tier), None)
            if entry is None:
                entry = {"table": table, "tier": tier, "total": 0, "done": 0,
                         "quota_used_today": 0, "quota_budget": self.budget_per_day,
                         "state": "idle", "eta_min": None, "last_error": ""}
                self.progress["tasks"].append(entry)
            entry["total"] = g["total"]
            entry["done"] = self._view_done_count(table, tier)
            entry["eta_min"] = self._eta_min()

    def _update_task_view(self, task: Task, state: str, quota_used: int) -> None:
        """更新进度文件的 tasks 聚合视图（Web 读取格式，§4）。

        v6.0.8：同步写入 ``total``/``done``（done = 该 table+tier 已完成任务数，
        含本次 run 新增；run() 开始处已按分组统计）+ ``eta_min``（最近 N 个成功
        任务平均耗时推算剩余分钟，无数据 None——前端显示 —）。
        """
        tier = f"P{task.priority}"
        entry = next((t for t in self.progress["tasks"]
                      if t.get("table") == task.table and t.get("tier") == tier), None)
        if entry is None:
            entry = {"table": task.table, "tier": tier, "total": 0, "done": 0,
                     "quota_used_today": quota_used, "quota_budget": self.budget_per_day,
                     "state": state, "eta_min": None, "last_error": ""}
            self.progress["tasks"].append(entry)
        entry["state"] = state
        entry["quota_used_today"] = quota_used
        entry["quota_budget"] = self.budget_per_day
        # v6.0.8：total/done 回填（修复 Web 恒 0/0）+ eta_min
        g = self._view_groups.get((task.table, tier))
        if g is not None:
            entry["total"] = g["total"]
            entry["done"] = self._view_done_count(task.table, tier)
        entry["eta_min"] = self._eta_min()
        # v6.0.8：立即落盘——长跑灌数期间 Web 3s 轮询 /status，若只改内存、
        # run 收尾才 save_progress，中途读到的仍是旧值（total/done 恒 0/0 的
        # 第二层根因）。mark_done 每任务已落盘一次，此处同频不增额外开销。
        save_progress(self.progress, self.progress_path)

    def _coverage_conn(self):
        """**B-2（v6.0.3）**：返回 coverage 统计所用的连接。

        - ``self.db_path`` 非空（自定义 --db）→ **新开该库的短连接**（用完由调用方
          close）。修复前此方法恒用 get_conn() 进程级单例（=默认生产库），导致
          ``--db tmp`` 时 coverage 读的是生产库口径、status 报表误导。
        - ``self.db_path`` 为 None（缺省库）→ 沿用 get_conn() 单例（**行为不变**，
          返回 None 表示"借用单例，调用方不得 close"）。

        为什么自定义 --db 用短连接而非复用单例：get_conn() 只管理默认库那一个单例，
        无法代表任意 --db；且 Web/写路径对单例有并发约束（D-4），这里开独立短连接最干净。
        """
        if self.db_path is None:
            from .conn import get_conn

            return get_conn(), False  # (con, owned)：owned=False → 不 close
        from .conn import connect_existing

        return connect_existing(self.db_path), True  # owned=True → 调用方必须 close

    def _refresh_coverage(self) -> None:
        """各表 coverage（rows/codes/date_min/date_max）——Web 区块 C 读取。

        **B-2**：连接改走 runner 自身 db_path（见 :meth:`_coverage_conn`），不再恒用
        get_conn() 生产库单例；缺省库行为不变。
        """
        try:
            con, owned = self._coverage_conn()
        except Exception:  # noqa: BLE001 - 库不可用（duckdb 未装等）→ 不阻断进度落盘
            return
        try:
            cov: Dict[str, Any] = {}
            for table, code_col, date_col in [
                ("kline_daily", "ts_code", "date"),
                ("valuation_daily", "ts_code", "date"),
                ("fundamentals_quarterly", "ts_code", None),
                ("dividend_events", "ts_code", "ex_date"),
                ("index_daily", "index_code", "date"),
            ]:
                try:
                    rows = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    codes = con.execute(
                        f"SELECT COUNT(DISTINCT {code_col}) FROM {table}").fetchone()[0]
                    if date_col:
                        rng = con.execute(
                            f"SELECT MIN({date_col}), MAX({date_col}) FROM {table}"
                        ).fetchone()
                        dmin, dmax = (rng[0], rng[1]) if rng else (None, None)
                        cov[table] = {"rows": rows, "codes": codes,
                                      "date_min": str(dmin) if dmin else None,
                                      "date_max": str(dmax) if dmax else None}
                    else:
                        cov[table] = {"rows": rows, "codes": codes}
                except Exception:  # noqa: BLE001 - 表空/不存在时跳过该表 coverage
                    continue
            self.progress["coverage"] = cov
        except Exception:  # noqa: BLE001 - 库不可用时不阻断进度落盘
            pass
        finally:
            if owned:
                try:
                    con.close()
                except Exception:  # noqa: BLE001
                    pass
