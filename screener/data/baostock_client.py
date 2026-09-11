# -*- coding: utf-8 -*-
"""BaoStock 会话管理：登录态、失败重试（指数退避）、统一手动翻页。

关键坑（调研报告 §0，已实测确认）：
1. baostock 的 ``ResultData.get_data()`` 在结果 >2000 行需要翻页时调用
   ``DataFrame.append()``——pandas 2.x 已移除该方法，会抛 AttributeError。
   因此本项目**统一用 ``while rs.next(): rows.append(rs.get_row_data())``
   手动翻页**（pandas 版本无关，无需钉 pandas<2.0）。
2. 每次使用必须 login/logout；登录失败看 error_code（"0"=成功）。
3. 返回的数值列是字符串、缺失值是空串 ''（不是 NaN）——本层只做原始行采集，
   类型转换统一放在 fetchers 里做。
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import random
import sys
import tempfile
import threading
import time
from datetime import datetime
from typing import Any, Callable, List, Optional, Tuple, Union
from zoneinfo import ZoneInfo

import baostock as bs

log = logging.getLogger("screener.data.bs")

# 北京时间自然日（BaoStock 官网限流口径=每 IP 每日；日期翻转即重置计数）
_TZ_BEIJING = ZoneInfo("Asia/Shanghai")


def default_quota_path() -> str:
    """配额状态文件默认路径：仓库外、CWD 无关（跨进程共享同一份计数）。"""
    return os.path.join(os.path.expanduser("~"), ".stock_screener", "bs_quota.json")


class QuotaGuard:
    """BaoStock 每日调用配额守卫（v5.2-p2，TL D1-D4）。

    背景：官网限流 = 每 IP 每日 5 万次调用，超限封禁 6h×年内已封次数（09-06/07
    本 IP 实测被封 2 次，表现为 login ``10001011 黑名单用户``）。守卫硬上限默认
    49900（留 100 余量），任何运行方式（CLI/web/prewarm/migrate）共享同一计数。

    - 状态文件：JSON ``{"date": "YYYY-MM-DD", "count": N}``，日期口径=北京时间
      自然日；读到的 date ≠ 今日 → count 从 0 重置（次日自动恢复）。
    - **跨进程原子**：prewarm/migrate 是多进程（每 worker 独立 client），用
      ``fcntl.flock`` 锁文件做 read-modify-write，tmp+rename 原子落盘；
      同进程多线程并发同理安全。
    - 超限行为（D4）：立即 raise BaoStockError——显式失败，不静默降级、
      不 sleep 等到午夜（项目纪律）。
    - count 达到 90% 时 log.warning 一次（每进程一次，防刷屏）。
    """

    def __init__(self, daily_quota: int = 49900, path: Optional[str] = None) -> None:
        if not isinstance(daily_quota, int) or isinstance(daily_quota, bool) or daily_quota < 1:
            raise ValueError(f"daily_quota 必须是正整数（当前 {daily_quota!r}）")
        if path is not None and not isinstance(path, str):
            raise TypeError(f"quota_path 必须是字符串路径或 None（当前 {path!r}）")
        self.daily_quota = daily_quota
        self.path = path or default_quota_path()
        self._lock = threading.Lock()  # 同进程多线程串行化（flock 只管跨进程）
        self._warned_90 = False  # D4：90% 告警每进程一次（防刷屏）

    # ---------- 内部 ----------
    @staticmethod
    def _today_beijing() -> str:
        return datetime.now(_TZ_BEIJING).strftime("%Y-%m-%d")

    def _lock_path(self) -> str:
        return self.path + ".lock"

    def _load(self) -> int:
        """读状态文件；缺失/损坏 → 0（fail-open：宁可从 0 重计也不阻断运行）。"""
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if str(data.get("date")) == self._today_beijing():
                return max(0, int(data.get("count", 0)))
            return 0  # 日期翻转（或文件 date 非法）→ 重置
        except (OSError, ValueError, TypeError):
            return 0

    def _atomic_write(self, date_s: str, count: int) -> None:
        """tmp+rename 原子落盘（同目录 tmp 保证 rename 是原子操作）。"""
        d = os.path.dirname(self.path) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".bs_quota_", suffix=".tmp", dir=d)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"date": date_s, "count": count}, f)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _with_lock(self, fn):
        """跨进程 flock（LOCK_EX）+ 同进程 threading.Lock 双重串行化。"""
        d = os.path.dirname(self.path) or "."
        os.makedirs(d, exist_ok=True)
        with self._lock:
            with open(self._lock_path(), "a+", encoding="utf-8") as lf:
                fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
                try:
                    return fn()
                finally:
                    fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

    # ---------- 对外 ----------
    def acquire(self) -> int:
        """放行一次真实 API 调用并计数，返回计后 count。

        已达硬上限 → raise BaoStockError（D4：显式失败，不重试、不等待）。
        """
        def _step() -> int:
            date_s = self._today_beijing()
            count = self._load() + 1
            if count > self.daily_quota:
                # 注意：超限的那次**不计数、不落盘**——直接拒绝，保持文件停在
                # daily_quota（运维 --show 显示"恰好用满"而非越界值）。
                raise BaoStockError(
                    f"baostock 当日配额耗尽 ({self.daily_quota}/{self.daily_quota})，"
                    "北京时间次日 0 点自动恢复；禁止继续调用"
                )
            self._atomic_write(date_s, count)
            if not self._warned_90 and count >= int(self.daily_quota * 0.9):
                self._warned_90 = True  # 每进程（每实例）只告警一次，防刷屏
                log.warning(
                    "baostock 当日配额已达 %.0f%% (%d/%d)——接近硬上限，"
                    "继续调用将触发 BaoStockError（北京时间次日 0 点重置）",
                    count / self.daily_quota * 100, count, self.daily_quota,
                )
            return count

        return self._with_lock(_step)

    def get_state(self) -> Tuple[str, int]:
        """当前 (北京时间日期, 已用次数)；文件缺失/损坏 → (今日, 0)。"""
        date_s = self._today_beijing()
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if str(data.get("date")) == date_s:
                return date_s, max(0, int(data.get("count", 0)))
        except (OSError, ValueError, TypeError):
            pass
        return date_s, 0

    def set_count(self, count: int, date_s: Optional[str] = None) -> None:
        """手动播种/重置计数（运维 CLI --set-count；date 缺省=今日北京时间）。"""
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError(f"count 必须是非负整数（当前 {count!r}）")
        date_s = date_s or self._today_beijing()

        def _step() -> None:
            self._atomic_write(date_s, count)

        self._with_lock(_step)


class DataSourceError(RuntimeError):
    """数据源级失败的基类。

    语义：主数据源（BaoStock）不可用/返回无效数据，导致本次运行**无法产出可信结果**。
    与"筛选后 0 只入选"（合法空结果）严格区分——数据源级失败必须显式失败
    （非零退出 + sidecar），绝不写成误导性空 result/report。生产路径失败守卫
    （9/7 缺陷修复）按本类型（含子类）路由到失败路径。
    """


class BaoStockError(DataSourceError):
    """BaoStock 调用失败（重试耗尽或登录失败）。"""


class BaoStockClient:
    """线程局部会话 + 指数退避重试 + 手动翻页的 BaoStock 封装。

    - baostock 的连接是进程级单例，多线程共用会串包；这里用 threading.local
      让每个工作线程持有独立 login/logout。
    - ``call`` / ``call_with_fields`` 统一入口：检查 error_code、失败重试
      （指数退避+抖动）、手动翻页取全量行。
    """

    def __init__(
        self,
        max_attempts: int = 5,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
        daily_quota: int = 49900,
        quota_path: Optional[Union[str, bool]] = None,
    ) -> None:
        self.max_attempts = max(1, int(max_attempts))
        self.base_delay = base_delay
        self.max_delay = max_delay
        self._local = threading.local()
        self.request_count = 0  # 实际发出的查询次数（含重试），用于缓存验证
        # v5.2-p2 每日配额守卫（TL D1/D2）：默认启用（quota_path=None → 默认路径，
        # 不改任何调用点也自动生效）；只有显式 quota_path=False 才禁用（单测隔离用）。
        # 计数口径（D3）：只计数据查询（query_fn 调用），不计 login/logout。
        if quota_path is False:
            self.quota_guard = None
        elif quota_path is None or isinstance(quota_path, str):
            self.quota_guard = QuotaGuard(daily_quota=daily_quota, path=quota_path)
        else:
            raise TypeError(f"quota_path 必须是字符串路径、None 或 False（当前 {quota_path!r}）")

    # ---------- 登录态 ----------
    def _ensure_login(self) -> None:
        if getattr(self._local, "logged_in", False):
            return
        lg = bs.login()
        if lg.error_code != "0":
            raise BaoStockError(f"baostock login 失败: {lg.error_msg}")
        self._local.logged_in = True
        log.info("baostock login ok (thread=%s)", threading.current_thread().name)

    def close(self) -> None:
        """当前线程登出（主线程退出时调用；worker 线程退出时各自关闭）。"""
        if getattr(self._local, "logged_in", False):
            bs.logout()
            self._local.logged_in = False

    # ---------- 核心：带重试的查询 + 手动翻页 ----------
    def _query(
        self,
        query_fn: Callable[..., Any],
        *,
        label: str = "",
        **kwargs: Any,
    ) -> Tuple[List[str], List[List[str]]]:
        """执行一次 baostock 查询，返回 (列名, 全量行)。失败指数退避重试。"""
        last_err = "unknown"
        for attempt in range(1, self.max_attempts + 1):
            try:
                # v5.2-p2（TL D1）：每次真正调用 query_fn **之前** acquire——
                # 成功才放行；计数含重试（重试也是真实 API 调用）。超限 →
                # BaoStockError 立即显式失败（D4，不 sleep 等到午夜）。
                if self.quota_guard is not None:
                    self.quota_guard.acquire()
                self._ensure_login()
                rs = query_fn(**kwargs)
                self.request_count += 1
                if rs.error_code != "0":
                    last_err = f"{label or query_fn.__name__} error_code={rs.error_code} {rs.error_msg}"
                    # 登录态可能失效，重试前重新登录
                    self._local.logged_in = False
                else:
                    rows: List[List[str]] = []
                    while rs.next():
                        rows.append(rs.get_row_data())
                    return list(rs.fields), rows
            except BaoStockError:
                raise
            except Exception as exc:  # noqa: BLE001 - 网络/协议异常统一重试
                last_err = f"{label or query_fn.__name__} {type(exc).__name__}: {exc}"
                self._local.logged_in = False
            if attempt < self.max_attempts:
                delay = min(self.max_delay, self.base_delay * (2 ** (attempt - 1)))
                delay *= 0.5 + random.random()  # 抖动，避免多 worker 同步重试
                log.warning(
                    "retry %d/%d after %.1fs (%s)", attempt, self.max_attempts, delay, last_err
                )
                time.sleep(delay)
        raise BaoStockError(f"baostock 查询失败（重试 {self.max_attempts} 次）: {last_err}")

    def call(self, query_fn: Callable[..., Any], *, label: str = "", **kwargs: Any) -> List[List[str]]:
        """同 :meth:`_query`，只返回行。"""
        return self._query(query_fn, label=label, **kwargs)[1]

    def call_with_fields(
        self, query_fn: Callable[..., Any], *, label: str = "", **kwargs: Any
    ) -> Tuple[List[str], List[List[str]]]:
        """同 :meth:`_query`，返回 (列名, 行)。"""
        return self._query(query_fn, label=label, **kwargs)


# ---------------------------------------------------------------------------
# 运维 CLI（TL D6）：查看/播种当日配额计数。
#   python -m screener.data.baostock_client --show
#   python -m screener.data.baostock_client --set-count N [--date YYYY-MM-DD]
# 纯本地文件操作，零网络调用。
# ---------------------------------------------------------------------------

def _main(argv: Optional[List[str]] = None) -> int:
    import argparse

    p = argparse.ArgumentParser(
        prog="python -m screener.data.baostock_client",
        description="BaoStock 每日配额守卫运维（本地状态文件，零网络调用）",
    )
    p.add_argument("--show", action="store_true", help="打印当前日期/已用/剩余")
    p.add_argument("--set-count", type=int, metavar="N",
                   help="手动播种/重置当日计数（N 为非负整数）")
    p.add_argument("--date", metavar="YYYY-MM-DD", default=None,
                   help="配合 --set-count：指定日期（缺省=今日北京时间）")
    p.add_argument("--path", default=None,
                   help=f"状态文件路径（缺省 {default_quota_path()}）")
    args = p.parse_args(argv)

    if not args.show and args.set_count is None:
        p.print_usage(sys.stderr)
        return 2
    guard = QuotaGuard(path=args.path)
    if args.set_count is not None:
        try:
            guard.set_count(args.set_count, date_s=args.date)
        except ValueError as exc:
            print(f"错误: {exc}", file=sys.stderr)
            return 2
        # 直接回显写入值（--date 指定非今日时 get_state 按今日口径会显示 0，有歧义）
        print(f"已设置: date={args.date or guard._today_beijing()} count={args.set_count} "
              f"path={guard.path}")
        return 0
    # --show
    date_s, count = guard.get_state()
    remaining = max(0, guard.daily_quota - count)
    print(f"date={date_s} used={count} quota={guard.daily_quota} remaining={remaining} "
          f"path={guard.path}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
