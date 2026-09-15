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

⚠️ 本模块**只做调度骨架 + 进度管理**，具体取数委托 ingest 层（复用客户端）。
后台慢慢补——不在筛选热路径上。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger("lake.backfill")


# ---------------------------------------------------------------------------
# 进度文件（§4 结构）
# ---------------------------------------------------------------------------
def _progress_path() -> str:
    from .conn import progress_path

    return progress_path()


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
    os.makedirs(os.path.dirname(p), exist_ok=True)
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
        self.progress_path = progress_path or _progress_path()
        self.progress = load_progress(self.progress_path)
        # done 键集合（内存缓存，避免每任务重读文件）
        self._done: set = set()
        for t in self.progress.get("tasks", []):
            pass  # tasks 是聚合视图；done 明细见下
        self._load_done()

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
    def run(self, tasks: List[Task], worker: Callable[[Task], None]) -> Dict[str, Any]:
        """按优先级消费队列；幂等跳过 done；日预算到顶停。

        :param tasks: 待补任务列表（内部按 priority 排序）。
        :param worker: 单任务取数+入库回调（由调用方注入，委托 ingest 层）。
        :return: {total, skipped_done, processed, blocked_quota, errors}。

        幂等保证：每个 task 先查 is_done → 跳过（**不耗配额**）；worker 成功后
        mark_done 落盘。中断重跑 → done 键已落盘 → 直接跳过，绝不重复调 BaoStock。
        """
        ordered = sorted(tasks)  # Task order=True：priority 升序
        stats = {"total": len(ordered), "skipped_done": 0, "processed": 0,
                 "blocked_quota": False, "errors": []}
        for task in ordered:
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
                worker(task)
                self.mark_done(task)
                stats["processed"] += 1
                self._update_task_view(task, "running", used)
            except Exception as exc:  # noqa: BLE001 - 单任务失败不中断整队列
                log.error("backfill %s 失败: %s", task_key(task), exc)
                stats["errors"].append(f"{task_key(task)}: {exc}")
                self._update_task_view(task, "error", used)
        # 收尾：写 coverage（各表行数/代码数/年份范围）
        self._refresh_coverage()
        save_progress(self.progress, self.progress_path)
        return stats

    def _update_task_view(self, task: Task, state: str, quota_used: int) -> None:
        """更新进度文件的 tasks 聚合视图（Web 读取格式，§4）。"""
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
