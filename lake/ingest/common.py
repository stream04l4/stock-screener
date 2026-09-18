# -*- coding: utf-8 -*-
"""lake.ingest.common —— ingest 共用工具（溯源字段 / upsert / 代码格式）。

设计：所有 ingest 复用现有客户端，本层只做"客户端输出 → lake 行"的薄转换 +
统一溯源三列（source/fetched_at/data_version，继承 v5.2 canonical 契约）。
"""
from __future__ import annotations

import datetime as _dt
import logging
import secrets
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence
from weakref import WeakKeyDictionary

log = logging.getLogger("lake.ingest.common")

# 溯源：数据湖 schema 版本（v6 首版）
DATA_VERSION = "v6.0"


def now_ts() -> str:
    """UTC ISO 时间戳（fetched_at 列，TIMESTAMP）。"""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def to_ts_code(code6_or_bs: str) -> str:
    """把代码统一成 ts_code 格式 sh.601398。

    - 已是 bs 格式（含点，sh.601398）→ 原样。
    - 6 位裸码（601398）→ 按前缀补 sh./sz.（6/9→sh，0/2/3→sz；北交所 4/8/92→bj）。
    """
    c = str(code6_or_bs).strip()
    if "." in c:
        return c
    c = c.zfill(6)
    if c[0] in ("5", "6", "9"):      # 沪市（含科创板 688、基金 5）
        return f"sh.{c}"
    if c[0] in ("4", "8") or c.startswith("92"):  # 北交所
        return f"bj.{c}"
    return f"sz.{c}"                  # 深市（含创业板 300）


def code6(ts_code: str) -> str:
    """sh.601398 → 601398（新浪/东财等裸码接口用）。"""
    return ts_code.split(".")[-1]


# ---------------------------------------------------------------------------
# DEFECT-HANG-1（R2）：网络取数墙钟硬超时守卫
# ---------------------------------------------------------------------------
# 为什么需要（HANG-1 根因之一）：requests ``timeout=N`` 是**单次 socket 操作**
# （connect/每段 recv）的超时，不是整请求墙钟上限——服务端半死不活时（TCP keepalive
# 心跳保活 / 慢速滴答发包 / CLOSE-WAIT 残留），每次 recv 都在 N 秒内"成功"返回少量
# 字节，整请求可无限拖长。生产实测：TencentClient timeout=15s 的 session.get 挂起
# ~70min（09-18 pid 3979536）；akshare stock_zh_a_daily 内部裸 ``requests.get(url)``
# **完全无 timeout**（源码级确认，sina 主源路径），一次 stall = 整进程永久冻结。
#
# 本守卫在 adapter 层对**每次外部取数调用**套墙钟硬上限：daemon 线程池执行 +
# ``future.result(timeout)``——超时即抛 :class:`FetchTimeoutError`（⊂ RuntimeError，
# worker 既有"该源失败→回退下一源"语义直接接住），挂死的线程随进程退出回收
# （daemon；其持有的 socket fd 在 OS 层随进程消失）。单请求硬超时 ≤30s（R2 红线）。
import concurrent.futures

# 单请求墙钟硬超时（秒）。env LAKE_FETCH_TIMEOUT_S 可覆盖（测试/运维）；缺省 30
# （brief R2：≤30s）。注意这是**整次取数调用**（含内部分页/多次 HTTP）的上限——
# 全史大请求（tdx 20000 根≈30 页）实测 ~6-15s，30s 余量充足。
def fetch_timeout_s() -> float:
    import os as _os

    try:
        return max(1.0, float(_os.environ.get("LAKE_FETCH_TIMEOUT_S", "30")))
    except ValueError:
        return 30.0


class FetchTimeoutError(RuntimeError):
    """DEFECT-HANG-1（R2）：取数调用超过墙钟硬超时。

    ⊂ RuntimeError——worker 的 ``except Exception`` 回退分支直接接住（该源失败→
    下一源；全源失败→不 mark_done，下轮续传），零新控制流。
    """


class _DaemonFetchPool:
    """R2 取数线程池：**显式 daemon 工作线程** + queue + concurrent.futures.Future。

    为什么不用 ``concurrent.futures.ThreadPoolExecutor``：其 worker 是**非 daemon**
    线程，解释器退出时 threading atexit 钩子会 **join 全部 worker**——若某 fetch
    线程正卡在 C 层 socket recv（无 OS 超时的残留路径），进程退出被永久阻塞。这
    正是 HANG-1 "shutdown-spin" 的候选机制之一（09-16：summary 已打印、进程仍不
    退）。本池 worker 恒 daemon：挂死线程随进程退出由 OS 终结，绝不参与退出 join。

    Future 用 ``concurrent.futures.Future`` 独立实例（set_result/set_exception 是
    公开 API，不依赖 executor 生命周期）；主线程 ``fut.result(timeout)`` 超时抛
    ``concurrent.futures.TimeoutError`` → 转 :class:`FetchTimeoutError`。
    """

    _SENTINEL = object()

    def __init__(self, max_workers: int = 8) -> None:
        import queue as _queue

        self._q: "_queue.Queue" = _queue.Queue()
        for i in range(max(1, int(max_workers))):
            t = threading.Thread(target=self._worker, name=f"lake-fetch-{i}",
                                 daemon=True)   # ← R2 关键：daemon，退出时不 join
            t.start()

    def _worker(self) -> None:
        while True:
            item = self._q.get()
            if item is self._SENTINEL:
                return
            fn, args, kwargs, fut = item
            try:
                fut.set_result(fn(*args, **kwargs))
            except BaseException as exc:   # noqa: BLE001 - 异常经 Future 传回主线程
                if not fut.set_exception(exc):
                    pass   # Future 已被取消（超时放弃）→ 丢弃，不刷错

    def submit(self, fn, *args, **kwargs) -> "concurrent.futures.Future":
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._q.put((fn, args, kwargs, fut))
        return fut


_FETCH_POOL_LOCK = threading.Lock()
_FETCH_POOL: List[Optional[_DaemonFetchPool]] = [None]   # 单元素列表=可变全局


def _get_fetch_pool() -> _DaemonFetchPool:
    with _FETCH_POOL_LOCK:
        if _FETCH_POOL[0] is None:
            _FETCH_POOL[0] = _DaemonFetchPool(max_workers=8)
        return _FETCH_POOL[0]


def _stop_requested() -> bool:
    """DEFECT-HANG-1（R4）：全局停止标志（lazy 兜底——backfill 不可用时恒 False）。

    与 tencent_ingest._stop_requested 同模式：lake.backfill.stop_requested 由 SIGTERM
    handler 置位。fetch_with_timeout 在等待取数结果时**轮询**本标志 → SIGTERM 到达即
    放弃等待（不等满 timeout），让主循环尽快到任务边界 break + 收尾（R4 ≤20s）。
    """
    try:
        from ..backfill import stop_requested as _sr

        return bool(_sr())
    except Exception:  # noqa: BLE001 - backfill 不可用（隔离测试）→ 不中断（原行为）
        return False


def fetch_with_timeout(fn, *args, timeout_s: Optional[float] = None, **kwargs):
    """在墙钟硬超时内执行取数调用 ``fn(*args, **kwargs)``；超时抛 FetchTimeoutError。

    :param fn: 无 lake import 依赖的取数 callable（adapter 内部方法/模块函数）。
    :param timeout_s: 墙钟上限（秒）；None → :func:`fetch_timeout_s`（env/缺省 30s）。

    实现说明：进程级共享 daemon 池（见 :class:`_DaemonFetchPool`）——超时后**不取消**
    底层线程（C 层 socket recv 不可安全中断），只放弃等待并抛错；挂死线程是 daemon，
    随进程退出由 OS 终结（fd 同回收）。正常路径零额外开销（result() 立即返回）。

    **DEFECT-HANG-1（R4）停止感知**：等待结果时按 ≤0.5s 分片轮询全局停止标志——
    SIGTERM 到达即放弃当前取数并抛错（不等满 timeout），主循环随即到任务边界 break +
    写 summary + 退出。为什么必须做：否则 SIGTERM 落在某次 ≤30s 取数中途时，主线程要
    等满该取数才检查停止标志 → 收尾可能 >20s（R4 红线）。分片轮询把"在途取数"的
    最坏等待从 timeout(≤30s) 压到 ≤0.5s。挂死线程仍 daemon 随进程退出回收。
    """
    t = fetch_timeout_s() if timeout_s is None else max(1.0, float(timeout_s))
    fut = _get_fetch_pool().submit(fn, *args, **kwargs)
    deadline = time.monotonic() + t
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FetchTimeoutError(
                f"取数调用墙钟超时 {t:.0f}s（{getattr(fn, '__name__', repr(fn))}）——"
                "挂死线程已放弃（daemon，随进程退出回收），按该源失败回退")
        if _stop_requested():
            # DEFECT-HANG-1（R4）：停止信号到达 → 立即放弃在途取数（不等满 timeout）。
            # 抛错让 worker 的 except 接住（不 mark_done），主循环下一边界 break。
            raise FetchTimeoutError(
                f"取数调用被停止信号中断（{getattr(fn, '__name__', repr(fn))}）——"
                "SIGTERM 收尾，挂死线程 daemon 随进程退出回收")
        try:
            return fut.result(timeout=min(remaining, 0.5))
        except concurrent.futures.TimeoutError:
            continue   # 分片到点 → 重查停止标志/重算剩余时间（正常完成时上面已 return）


# ---------------------------------------------------------------------------
# DEFECT-HANG-1（R3）：进度停滞看门狗（卡死自愈）
# ---------------------------------------------------------------------------
class HangWatchdogError(BaseException):
    """DEFECT-HANG-1（R3）：progress 连续 N 分钟无推进 → 看门狗在**主线程**抛出。

    **为什么是 BaseException 而非 Exception**：SIGINT handler 在主线程下一个字节码
    边界抛本异常，而此刻主线程往往正阻塞在某次取数的 ``fut.result()`` 内、且调用栈上
    有多层 adapter/worker 的 ``except Exception``（sina hfq / tdx hfq / baostock 回退等）。
    若本异常 ⊂ Exception，会被这些分支**吞掉**（记单任务失败、继续下一任务）——看门狗
    abort 语义彻底失效。BaseException 不被任何 ``except Exception`` 捕获 → 穿透整条
    worker 调用栈直达 driver run_history 的 ``except HangWatchdogError`` 收尾（干净退出）。

    副作用：Ctrl-C（SIGINT）也走本异常 → 同样干净退出（语义一致，都是"停止并续传"）。
    """


class ProgressWatchdog(threading.Thread):
    """R3 看门狗：daemon 线程周期检查 progress 文件 mtime；停滞超阈值 → 主线程 abort。

    **判据 = progress 文件 mtime**（而非内存计数）：mark_done/_update_task_view 每任务
    原子落盘一次——"无推进"的跨进程真实载体就是这个文件的修改时间（与 TL watchdog
    STALE 告警同口径，但阈值短得多：10min vs 60min）。正常最坏间隔 = 单任务墙钟上限
    （R2 30s）× 源数 + 限速 ≈ 分钟级 → 10min 阈值对健康运行**零误杀余量充足**。

    **abort 机制 = 主线程注册 SIGINT handler → 看门狗 ``os.kill(self, SIGINT)``**：
    信号在主线程的下一个字节码边界触发 handler 抛 :class:`HangWatchdogError`。即使
    主线程正阻塞在 C 层 socket recv，EINTR/timeout 返回后的下一帧即抛。配合 R2（每次
    fetch ≤30s），主线程**必然**在分钟级内经过 Python 帧 → abort 确定生效。

    为什么用信号而非 ``threading._invoke_excepthook``（私有 API）：实测该 API 只在
    **调用它自己的线程**抛异常，无法跨线程注入到主线程；SIGINT 是 CPython 唯一可靠
    的"从其他线程让主线程抛指定异常"通道（handler 在主线程执行、可抛任意异常）。
    副作用：运行期 Ctrl-C 也走本 handler（抛 HangWatchdogError 而非 KeyboardInterrupt）
    ——语义一致（都是"停止并干净退出"），driver main 统一捕获收尾。

    :param progress_path: 被监视的 progress 文件（按 db_path 派生，与 runner 同路径）。
    :param stall_minutes: 停滞阈值（分钟）；config ``hang_stall_minutes``（缺省 10，
        brief R3"建议 10min，config 可调"）。<=0 → 看门狗禁用（不启动）。
    :param check_interval_s: 检查周期（秒，缺省 30）。
    """

    def __init__(self, progress_path: str, stall_minutes: float,
                 check_interval_s: float = 30.0) -> None:
        super().__init__(name="lake-hang-watchdog", daemon=True)
        self.progress_path = progress_path
        # stall 下限 0.05min=3s：生产缺省 10min（config），但测试/R3 触发演示需要
        # 秒级阈值（smoke_test_hang1.txt 的 R3 demo）——不锁死 60s 下限。
        self.stall_s = max(0.05, float(stall_minutes)) * 60.0
        self.check_interval_s = max(2.0, float(check_interval_s))
        self._stop = threading.Event()
        self.aborted = False   # 测试/诊断：是否已触发过 abort
        # DEFECT-HANG-1（R3）：**"相对上次观测的 mtime 未变化"** 判据，而非 now-mtime。
        # 为什么不能用 now-mtime：progress 文件**跨 run 持久**——fresh run 启动时它的
        # mtime 是上一轮结束时的陈旧值（可能数小时前），now-mtime 立刻 > stall →
        # **健康 fresh run 被误杀**。改为"自看门狗观测到的最近一次 mtime 起，文件未再
        # 变化且持续超阈值"才判停滞：任何 mark_done/_update_task_view 落盘都会推进
        # mtime → 重置计时；真正卡死（mtime 恒定）才会累计到阈值触发。首次检查只记录
        # 基线不判定（给 runner 首个 save 留窗口，见 run()）。
        self._last_mtime: Optional[float] = None

    def arm(self) -> "ProgressWatchdog":
        """启动看门狗 + **在主线程**注册 SIGINT→HangWatchdogError handler。

        ⚠️ 必须从主线程调用（signal.signal 限制）：driver cmd_history 在主线程 start。
        保存原 handler 供 :meth:`stop` 恢复（run 收尾/Ctrl-C 语义还原）。非主线程
        调用 → 跳过 handler 注册（看门狗仍工作，但 abort 退化为无——不 crash）。
        """
        import signal as _sig

        self._prev_int_handler = None
        try:
            if threading.current_thread() is threading.main_thread():
                self._prev_int_handler = _sig.signal(_sig.SIGINT, self._on_hang_signal)
        except (ValueError, OSError):  # noqa: BLE001 - 非主线程/平台不支持 → 降级
            pass
        super().start()
        return self

    @staticmethod
    def _on_hang_signal(signum, frame):  # noqa: ARG001 - signal handler 签名固定
        """SIGINT（看门狗 os.kill / 用户 Ctrl-C）→ 主线程抛 HangWatchdogError。"""
        raise HangWatchdogError("收到 SIGINT（R3 看门狗 abort / 手动停止）")

    def stop(self) -> None:
        """停看门狗 + 恢复原 SIGINT handler（driver run 收尾调用）。"""
        self._stop.set()
        prev = getattr(self, "_prev_int_handler", None)
        if prev is not None:
            import signal as _sig

            try:
                if threading.current_thread() is threading.main_thread():
                    _sig.signal(_sig.SIGINT, prev)
            except (ValueError, OSError):  # noqa: BLE001 - 恢复失败不致命（进程即将退出）
                pass
            self._prev_int_handler = None

    def run(self) -> None:
        import os as _os
        import signal as _sig

        stall_since: Optional[float] = None   # 首次观测到"mtime 未变"的时刻（monotonic）
        while not self._stop.wait(self.check_interval_s):
            try:
                mtime = _os.path.getmtime(self.progress_path)   # 墙钟（epoch）
            except OSError:
                continue   # 文件暂缺（run 尚未首次落盘）→ 不判停滞
            if self._last_mtime is None:
                # 首次检查：只记录基线，不判定（给 runner 首个 save_progress 留窗口——
                # fresh run 启动到首个任务落盘之间 mtime 本就是上一轮陈旧值）。
                self._last_mtime = mtime
                continue
            if mtime != self._last_mtime:
                # progress 有推进（mtime 变化）→ 重置停滞计时。
                self._last_mtime = mtime
                stall_since = None
                continue
            # mtime 自上次观测未变 → 累计停滞时长（monotonic，避免墙钟回拨干扰）。
            now_mono = time.monotonic()
            if stall_since is None:
                stall_since = now_mono
            elif (now_mono - stall_since) >= self.stall_s:
                log.error(
                    "R3 看门狗：progress %s 已 %.0fmin 无推进（mtime 恒定，阈值 %.1fmin）"
                    "→ SIGINT abort（主线程抛 HangWatchdogError，干净退出，下次续传）",
                    self.progress_path, (now_mono - stall_since) / 60.0,
                    self.stall_s / 60.0)
                self.aborted = True
                try:
                    _os.kill(_os.getpid(), _sig.SIGINT)   # 主线程下一帧抛 HangWatchdogError
                except OSError:
                    pass
                return   # 触发一次即止（主线程应已退出；防重复注入刷屏）


# ---------------------------------------------------------------------------
# upsert：INSERT OR REPLACE（仅 PK 表；无 PK 表如 dividend_events/holders_snapshot
#   必须用 delete_where + insert_many "先删后插"——DuckDB 的 INSERT OR REPLACE
#   要求目标表有 UNIQUE/PK 约束，否则 BinderException）
# ---------------------------------------------------------------------------
class _WriteNull:
    """哨兵：显式写 NULL（与"不写该列"区分）。不可序列化、单例语义。"""

    __slots__ = ()


def write_null() -> "_WriteNull":
    """upsert 的 conflict_src 参数取值——**显式把 conflict_src 写成 NULL**。

    v6.1 DEF-1：INSERT OR REPLACE 是整行替换——若"无分歧（None）"时不写该列，
    旧行的陈旧 conflict_src 摘要会残留在新写入的行上（tester 复现的审计列错误）。
    故 kline_daily 等每次写入都必须让 conflict_src **反映本次写入**：
    有分歧→传摘要字符串；无分歧→传 ``write_null()``（显式 NULL，清掉旧值）。
    """
    return _WriteNull()


def upsert(con, table: str, columns: Sequence[str], rows: Iterable[Sequence[Any]],
           conflict_src: Optional[Any] = None) -> int:
    """批量 INSERT OR REPLACE。返回写入行数。**仅用于有 PK/UNIQUE 约束的表**。

    - 列名/值显式对应（不依赖顺序），None → NULL。
    - 空行集 → 0（no-op，幂等）。
    - 单条失败立即抛错（不静默吞；调用方按 backfill 进度语义处理）。
    - ⚠️ 无 PK 表调用本函数会抛 BinderException（"specify ON CONFLICT columns
      manually"）——请改用 delete_where + insert_many。

    v6.1：``conflict_src`` 可选参数——跨源分歧摘要（写 ``conflict_src`` 列，≤256B）。
    **仅当表 DDL 含 conflict_src 列时传**（T1-T7 有、T8/T9 无——Q5）；对无此列的
    表传非 None/哨兵会抛 BinderException，由调用方保证（load_t2/load_t7 等按 Q5
    清单的表才传）。

    v6.1 DEF-1：三态语义（向后兼容 + 审计列正确性）——
    - ``None``（缺省）= **不写该列**：旧调用方零影响（T3/T4/T8/T9 等既有行为不变；
      含 load_t2 的 legacy 单源路径与 v6.0.x 逐字节一致）。
    - 字符串 = 写分歧摘要。
    :func:`write_null` 哨兵 = **显式写 NULL**：kline_daily 每次写入必传（无分歧
      →NULL），REPLACE 后不残留上一次写入的陈旧值。

    v6.1.1 FIX-1：**临时表批量冲突**（灌数提速，语义与逐行路径逐字节等价）。

    为什么改：``executemany(INSERT OR REPLACE ... VALUES)`` 对百万行大表是**逐行
    冲突检测**——kline_daily 768 万行时单股全史（4815 行）实测 29.0s，占单股耗时
    ~85%（TL 诊断 A/B/C 对照）。改为：

      1. ``DESCRIBE {table}`` 取列类型（按 (con, table) 缓存——同连接重复 upsert
         同一表零额外查询）；
      2. 建 TEMP 表 ``_up_tmp_<随机后缀>``（**仅含本次写入的 cols**，类型取自
         DESCRIBE；无 PK——冲突检测统一交给目标表的 REPLACE，temp 只是批量缓冲）；
      3. ``executemany(INSERT INTO _up_tmp ...)`` 普通插入（无冲突检测，快）；
      4. ``INSERT OR REPLACE INTO {table} (cols) SELECT * FROM _up_tmp``——整批一次
         冲突合并（实测 4.1s vs 29.0s，~7×）；
      5. ``DROP TABLE _up_tmp``（try/finally——异常路径也清理，不留 temp 残留）。

    **等价性保证**（tests/test_lake_v611_upsert_temp.py 全表哈希对照）：
    - 同 PK 覆盖 / 无冲突行插入：REPLACE ... SELECT 与逐行 REPLACE 语义一致；
    - conflict_src 三态不变：拼列逻辑保持在下方原位（拼进 cols/rows 后才走新路径）——
      None=不写列（temp 表无该列，REPLACE 保留旧值）、字符串=摘要、write_null()=显式 NULL；
    - **批内重复 PK = last-wins**：逐行 executemany 是后行覆盖前行；而 REPLACE ...
      SELECT 对源内重复键的结果不确定（实测取首行）。真实调用方单批不产生重复 PK
      （load_t2 按 date 升序唯一、T1/T3/T5/T7 天然唯一），但为**逐字节等价**仍做
      Python 侧去重：按目标表 PK 列（duckdb_constraints 取）保留每键**最后一行**。
    - 返回行数 = 传入行数（与现状一致；批内重复键时 = 去重前行数——调用方口径不变）。

    临时表名带随机后缀（``secrets.token_hex(6)``）防并发撞名——灌数是单进程，
    属防御性处理。temp 表随连接关闭自动消失，DROP 只是及时释放。
    """
    rows = [list(r) for r in rows]
    if not rows:
        return 0
    cols = list(columns)
    if conflict_src is not None:  # 字符串摘要 或 write_null() 哨兵（→NULL）
        cols = cols + ["conflict_src"]
        val = None if isinstance(conflict_src, _WriteNull) else conflict_src
        rows = [r + [val] for r in rows]

    # ---- v6.1.1 FIX-1：临时表批量冲突（语义等价，见 docstring）----
    # 批内重复 PK → last-wins（= 逐行 executemany 顺序覆盖；REPLACE...SELECT 对源内
    # 重复键不确定，必须先去重）。PK 列取目标表约束（与本次写入 cols 求交——
    # conflict_src 等非 PK 列不参与去重键）。
    pk_cols = _table_pk_cols(con, table)
    if pk_cols and all(c in cols for c in pk_cols):
        pos = [cols.index(c) for c in pk_cols]
        seen: Dict[Any, List[Any]] = {}
        for r in rows:  # dict 赋值覆盖 → 每 PK 键保留最后一行（last-wins）
            seen[tuple(r[i] for i in pos)] = r
        deduped = list(seen.values())
    else:
        # 无 PK 约束 / 写入列不含完整 PK：不去重。前者=调用方误用（本函数契约=PK
        # 表），交给目标表抛 BinderException（与现状一致）；后者在 INSERT 阶段即被
        # NOT NULL/PK 约束拒绝（实测两路径同错，错误语义不变）。
        deduped = rows

    types = _table_col_types(con, table)
    tmp = f"_up_tmp_{secrets.token_hex(6)}"
    cols_sql = ", ".join(f'"{c}" {types[c]}' for c in cols)
    con.execute(f"CREATE TEMP TABLE {tmp} ({cols_sql})")
    try:
        ins_cols = ", ".join(f'"{c}"' for c in cols)
        placeholders = ", ".join(["?"] * len(cols))
        con.executemany(f"INSERT INTO {tmp} ({ins_cols}) VALUES ({placeholders})", deduped)
        # 整批一次冲突合并：列名显式对应（SELECT * 顺序 = temp DDL 顺序 = cols 顺序）
        con.execute(
            f'INSERT OR REPLACE INTO {table} ({ins_cols}) SELECT * FROM {tmp}')
    finally:
        # 异常路径也清理（REG-1c：upsert 中途失败不得残留 _up_tmp*）
        try:
            con.execute(f"DROP TABLE {tmp}")
        except Exception:  # noqa: BLE001 - DROP 失败不掩盖原始异常
            pass
    return len(rows)


# ---------------------------------------------------------------------------
# upsert 辅助：DESCRIBE 类型 / PK 列（按 (con, table) 缓存——同连接重复 upsert
# 同一表零额外查询；WeakKeyDictionary 随连接回收自动清，不泄漏）
# ---------------------------------------------------------------------------
_DESC_CACHE: "WeakKeyDictionary[Any, Dict[str, Dict[str, str]]]" = WeakKeyDictionary()


def _table_col_types(con, table: str) -> Dict[str, str]:
    """DESCRIBE {table} → {列名: 类型串}（缓存到连接对象上，按表名）。

    为什么用 DESCRIBE 而不是 information_schema：一次查询同时拿到**全部列**的
    顺序+类型（temp DDL 需要本次写入 cols 的类型子集）；DESCRIBE 输出稳定
    （列名/类型/null/PK/default/key 六元组，见 lake.ddl 建表实测）。
    """
    cache = _DESC_CACHE.get(con)
    if cache is None:
        cache = {}
        _DESC_CACHE[con] = cache
    t = cache.get(table)
    if t is None:
        t = {r[0]: r[1] for r in con.execute(f"DESCRIBE {table}").fetchall()}
        cache[table] = t
    return t


def _table_pk_cols(con, table: str) -> List[str]:
    """目标表 PK 列名列表（无 PK → []）。

    取 ``duckdb_constraints()`` 的 PRIMARY KEY 行 + ``constraint_column_indexes``
    （0-based 位置）映射回 DESCRIBE 列序。用于 upsert 批内重复键 last-wins 去重。
    """
    row = con.execute(
        "SELECT constraint_column_indexes FROM duckdb_constraints() "
        "WHERE table_name=? AND constraint_type='PRIMARY KEY'", [table]).fetchone()
    if row is None:
        return []
    desc_cols = [r[0] for r in con.execute(f"DESCRIBE {table}").fetchall()]
    idxs = row[0] or []
    return [desc_cols[i] for i in idxs]


def insert_many(con, table: str, columns: Sequence[str], rows: Iterable[Sequence[Any]]) -> int:
    """批量普通 INSERT（executemany）。返回写入行数。**用于无 PK 的表**。

    - 与 upsert 相同的列名/值显式对应、空行集 no-op 语义；
    - 幂等性由调用方保证：先 delete_where 删掉本批将覆盖的键，再 insert_many
      （"先删后插"——事件表无 PK，INSERT OR REPLACE 不可用）。
    """
    rows = [list(r) for r in rows]
    if not rows:
        return 0
    cols = ", ".join(f'"{c}"' for c in columns)
    placeholders = ", ".join(["?"] * len(columns))
    sql = f"INSERT INTO {table} ({cols}) VALUES ({placeholders})"
    con.executemany(sql, rows)
    return len(rows)


def delete_where(con, table: str, col: str, val: Any) -> None:
    """删除某键全部行（无 PK 事件表"先删后插"幂等用）。"""
    con.execute(f'DELETE FROM {table} WHERE "{col}" = ?', [val])


def clean_date(v: Any) -> Optional[str]:
    """'YYYY-MM-DD ...'/8位/YYYYMMDD → ISO；非法/空 → None。"""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    import re
    m = re.search(r"\d{4}-\d{2}-\d{2}", s)
    if m:
        return m.group(0)
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return None


def to_float(v: Any) -> Optional[float]:
    """数值字段 → float（null/空/非法 → None）。"""
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if not s or s.lower() in ("nan", "none", "null"):
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    return None if f != f else f  # NaN → None


def forward_fill_af(dates: Sequence[str], adj_map: Dict[str, float]) -> Dict[str, Optional[float]]:
    """adj_factor 前向填充（Q4：BaoStock 仅除权日有行 → 标准做法是事件值前向填充）。

    :param dates: 交易日序列（升序，ISO）。
    :param adj_map: {除权日: af}——事件日的复权因子。
    :return: {date: af}——每个交易日的有效 af = 该日或之前最近一个除权日的 af；
        首个除权日之前 → None（无历史 af，hfq/qfq view 该段 NULL，属预期）。

    语义（为什么前向填充）：复权因子在两次除权事件之间恒定——除权当日跳到新值，
    之后每个交易日沿用，直到下一次除权。故对升序日期序列做"最近事件日取值"扫描。
    """
    if not dates:
        return {}
    # 事件日排序（只保留落在 dates 范围内的；范围外的事件不影响本窗口填充）
    events = sorted((d, af) for d, af in adj_map.items() if af is not None)
    out: Dict[str, Optional[float]] = {}
    cur: Optional[float] = None
    ei = 0
    n_ev = len(events)
    # 逐交易日扫描：应用所有 event_date <= d 的事件，cur = 最近事件日 af（前向填充）
    for d in dates:
        while ei < n_ev and events[ei][0] <= d:
            cur = events[ei][1]
            ei += 1
        out[d] = cur
    return out
