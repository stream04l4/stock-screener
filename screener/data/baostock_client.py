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

import logging
import random
import threading
import time
from typing import Any, Callable, List, Tuple

import baostock as bs

log = logging.getLogger("screener.data.bs")


class BaoStockError(RuntimeError):
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
    ) -> None:
        self.max_attempts = max(1, int(max_attempts))
        self.base_delay = base_delay
        self.max_delay = max_delay
        self._local = threading.local()
        self.request_count = 0  # 实际发出的查询次数（含重试），用于缓存验证

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
