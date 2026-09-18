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
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from zoneinfo import ZoneInfo

import baostock as bs

log = logging.getLogger("screener.data.bs")

# 北京时间自然日（BaoStock 官网限流口径=每 IP 每日；日期翻转即重置计数）
_TZ_BEIJING = ZoneInfo("Asia/Shanghai")


# ---------------------------------------------------------------------------
# v6.1（Q6 前置必修）：baostock socket 超时补丁
# ---------------------------------------------------------------------------
# baostock 0.9.30 ``util/socketutil.py`` 创建的 socket **无 timeout**——EU 链路
# 抖动时 recv 永久 hang（实测 10/10 hang），login/query 都救不回来。修复方式：
# monkey-patch ``SocketUtil.connect``，在其原逻辑执行后给
# ``baostock.common.context.default_socket`` 补 settimeout(N)。researcher 已验证
# 该路径生效（patch 后 gettimeout() 非 None）。
#
# **开关（brief 红线：必须有开关可关、默认开）**：
# - 构造参数 ``socket_timeout: Optional[float]``——None=按环境变量缺省（开）；
#   显式传值=用该值；传负数/0=禁用 patch（回退 baostock 原生无超时行为）。
# - 环境变量 ``BS_SOCKET_TIMEOUT_MS``：毫秒整数，覆盖缺省 15000ms；设 0=禁用。
#   为什么放环境变量而非 config.yaml：本模块是 screener 层，主路径零 import lake
#   是硬边界（test_lake_zero_import），不能读 lake.config；screener 自身配置面
#   （strategy.yaml）也不该为数据层基础设施加 accessor。env var 是最小侵入的开关。
def _socket_timeout_default_ms() -> int:
    try:
        return int(os.environ.get("BS_SOCKET_TIMEOUT_MS", "15000"))
    except ValueError:
        return 15000


_PATCHED = False          # 模块级幂等标记：同进程只 patch 一次
_SOCKET_TIMEOUT_S: Optional[float] = None  # connect 时实际生效的超时（后构造的 client 覆盖）


def _install_socket_timeout_patch() -> None:
    """Patch ``SocketUtil.connect``：连接建立后给 default_socket settimeout。

    - 幂等（_PATCHED 标记）：重复构造 client 不叠加包装；wrapper 在 **connect 时**
      读模块级 ``_SOCKET_TIMEOUT_S``（后构造的 client 可覆盖，如 Q6 探测用 10s）。
    - baostock 未装/结构变化 → 静默跳过（fail-open：退回原生行为，不阻断）。
    - patch 内异常全部吞掉并 log——超时补丁失败不应影响数据查询主流程。
    """
    global _PATCHED
    if _PATCHED:
        return
    try:
        import baostock.common.context as bs_context
        from baostock.util.socketutil import SocketUtil

        orig_connect = SocketUtil.connect

        def _connect_with_timeout(self):  # noqa: ANN001 - baostock 内部签名
            orig_connect(self)
            try:
                tmo = _SOCKET_TIMEOUT_S
                sock = getattr(bs_context, "default_socket", None)
                if sock is not None and tmo is not None and tmo > 0:
                    sock.settimeout(tmo)
            except Exception as exc:  # noqa: BLE001 - 补丁失败不阻断（见模块注释）
                log.warning("baostock socket timeout patch 生效失败: %s", exc)

        SocketUtil.connect = _connect_with_timeout
        _PATCHED = True
        log.info("baostock socket timeout patch 已安装")
    except Exception as exc:  # noqa: BLE001 - baostock 结构变化 → fail-open
        log.warning("baostock socket timeout patch 安装失败（退回原生行为）: %s", exc)


def reset_socket_patch_for_test() -> None:
    """测试隔离：清 _PATCHED/超时标记（下次构造 client 重新 patch）。"""
    global _PATCHED, _SOCKET_TIMEOUT_S, _SEND_MSG_PATCHED
    _PATCHED = False
    _SOCKET_TIMEOUT_S = None
    _SEND_MSG_PATCHED = False


# ---------------------------------------------------------------------------
# DEFECT-HANG-1（R2/R4）：baostock send_msg 紧循环补丁（09-16 shutdown-spin 根因）
# ---------------------------------------------------------------------------
# baostock ``util/socketutil.py::send_msg`` 的读响应循环是**裸 while True**：
#   receive = b""
#   while True:
#       recv = default_socket.recv(8192)
#       receive += recv
#       if receive[-13:] == b"<![CDATA[]]>\n": break
# 当**对端已关连接（CLOSE-WAIT）**时，``recv`` **立即返回 b""**（不是阻塞、不超时）→
# ``receive`` 恒为 b"" → ``b""[-13:]`` 永不匹配分隔符 → **无限紧循环 100% CPU**。
# 这正是 09-16 pid 2146668 的 shutdown-spin 签名（strace ~327k recvfrom/8s on
# CLOSE-WAIT socket、~100% CPU、零 I/O）。且该路径可达**主线程**：run_history 收尾
# ``bs.close()``→``bs.logout()``→send_msg——summary 已打印后进程仍不退出（持 DuckDB
# lock + flock → web 数据端点 409）。
#
# 为什么 socket-timeout patch 救不了它：CLOSE-WAIT 下 recv 是**立即返回空**，不是
# "等超时"——settimeout 只对阻塞中的 recv 生效。必须在循环里显式识别 ``recv==b""``
# （对端已关）并退出。
#
# 修复 = monkey-patch send_msg：① ``recv==b""`` → break（返回 None，上层按失败重试/
#   回退——login/logout/query 全走此语义，零新控制流）；② 迭代硬上限兜底任何"慢滴答
#   但永不给分隔符"的半死连接。健康路径逐字节不变（正常响应 1-3 次 recv 即读到分隔符
#   break）。fail-open：baostock 未装/结构变化 → 静默退回原生行为，不阻断。
_SEND_MSG_PATCHED = False
_SEND_MSG_MAX_ITERS = 1000   # 单条消息迭代上限（正常 1-3 次；1000×8KB=8MB≫任何单响应）


def _install_send_msg_patch() -> None:
    """Patch ``baostock.util.socketutil.send_msg``：CLOSE-WAIT 紧循环守卫（见上注释）。"""
    global _SEND_MSG_PATCHED
    if _SEND_MSG_PATCHED:
        return
    try:
        import zlib as _zlib

        import baostock.common.context as bs_context
        import baostock.common.contants as bs_cons
        from baostock.util import socketutil as bs_sock

        def _send_msg_guarded(msg):
            """原生 send_msg 的守卫版：recv==b""（对端关连接）→ 立即退出，不紧循环。"""
            try:
                if hasattr(bs_context, "default_socket"):
                    default_socket = getattr(bs_context, "default_socket")
                    if default_socket is not None:
                        msg = msg + "\n"   # 消息结尾分隔符（不压缩时）
                        default_socket.send(bytes(msg, encoding="utf-8"))
                        receive = b""
                        for _ in range(_SEND_MSG_MAX_ITERS):
                            recv = default_socket.recv(8192)
                            if not recv:    # DEFECT-HANG-1：对端已关连接 → 退出（不紧循环）
                                break
                            receive += recv
                            if receive[-13:] == b"<![CDATA[]]>\n":   # 压缩时结尾分隔符
                                break
                        else:
                            # 迭代上限耗尽仍未读到完整消息（半死连接慢滴答）→ 按失败
                            log.warning("baostock send_msg 迭代上限 %d 次耗尽 → 按失败处理",
                                        _SEND_MSG_MAX_ITERS)
                            return None
                        if not receive:     # recv==b"" 提前退出（连接已关）→ 失败
                            return None
                        head_bytes = receive[0:bs_cons.MESSAGE_HEADER_LENGTH]
                        head_str = bytes.decode(head_bytes)
                        head_arr = head_str.split(bs_cons.MESSAGE_SPLIT)
                        if head_arr[1] in bs_cons.COMPRESSED_MESSAGE_TYPE_TUPLE:
                            head_inner_length = int(head_arr[2])
                            body_str = bytes.decode(_zlib.decompress(
                                receive[bs_cons.MESSAGE_HEADER_LENGTH:
                                       bs_cons.MESSAGE_HEADER_LENGTH + head_inner_length]))
                            return head_str + body_str
                        return bytes.decode(receive)   # 不压缩
                    return None
                print("you don't login.")
            except Exception as ex:   # noqa: BLE001 - 与原生一致：异常打印后返回 None
                print(ex)
                print("接收数据异常，请稍后再试。")

        bs_sock.send_msg = _send_msg_guarded
        _SEND_MSG_PATCHED = True
        log.info("baostock send_msg 紧循环守卫已安装（DEFECT-HANG-1）")
    except Exception as exc:   # noqa: BLE001 - baostock 结构变化 → fail-open
        log.warning("baostock send_msg patch 安装失败（退回原生行为）: %s", exc)


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
        stop_checker: Optional[Callable[[], bool]] = None,
        socket_timeout: Optional[float] = None,
    ) -> None:
        self.max_attempts = max(1, int(max_attempts))
        self.base_delay = base_delay
        # v6.0.10：停止检查钩子（依赖注入，见 _query 注释）——lake driver 传入
        # lake.backfill.stop_requested；None → 恒不中断（纯 screener 场景原行为）。
        self._stop_checker: Optional[Callable[[], bool]] = stop_checker
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
        # v6.1：socket 超时补丁（Q6 前置）。socket_timeout=None → 按 env/缺省 15s（默认开）；
        # <=0 → 禁用 patch（回退原生无超时行为，开关可关）。patch 幂等（模块级标记），
        # 多 client 只装一次。fail-open：baostock 结构变化时静默退回原生行为。
        if socket_timeout is None:
            self.socket_timeout = _socket_timeout_default_ms() / 1000.0
        else:
            self.socket_timeout = float(socket_timeout)
        if self.socket_timeout > 0:
            global _SOCKET_TIMEOUT_S
            _SOCKET_TIMEOUT_S = self.socket_timeout
            _install_socket_timeout_patch()
        # DEFECT-HANG-1（R2/R4）：send_msg 紧循环守卫与 socket-timeout patch 同源
        # （baostock 协议层缺陷），**独立安装**——即使 socket_timeout<=0 禁用前者，
        # CLOSE-WAIT 紧循环守卫仍必须生效（09-16 shutdown-spin 根因，与超时开关无关）。
        _install_send_msg_patch()

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
        # v6.0.10：全局停止检查（Web"停止同步"按钮 → SIGTERM handler 置位）。
        # **依赖注入**（stop_checker 由调用方传入）：本模块是 screener 层，主路径
        # 零 import lake 是硬边界（test_lake_zero_import AST 扫描，lazy import 也
        # 会被命中）——lake driver 构造时注入 lake.backfill.stop_requested；非 lake
        # 场景不传 → None → 行为零变化。用途：① 每轮 attempt 前检查 → 提前中断
        # 当前重试；② 退避 sleep 拆 0.5s 块、逐块检查 → 信号在退避期间到达也立即
        # 中断（BaoStock 网络不稳时单任务可能卡几分钟——Joel 实测"停止没反应"的
        # 根因之一）。
        _checker = self._stop_checker

        def _stop_now() -> bool:
            return bool(_checker is not None and _checker())

        last_err = "unknown"
        for attempt in range(1, self.max_attempts + 1):
            if _stop_now():
                raise BaoStockError("baostock 重试中收到停止信号，提前中断收尾")
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
                # v6.0.10：退避 sleep 拆 0.5s 块、逐块检查停止标志——信号在退避期间
                # 到达立即中断（原行为 time.sleep(delay) 最长等满 max_delay=30s）。
                slept = 0.0
                while slept < delay:
                    step = min(0.5, delay - slept)
                    time.sleep(step)
                    slept += step
                    if _stop_now():
                        raise BaoStockError("baostock 退避等待中收到停止信号，提前中断收尾")
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
# v6.1（Q6）：BaoStock 恢复探测——灌数启动时探一次 query_all_stock 判活
# ---------------------------------------------------------------------------
def probe_baostock_alive(timeout_s: float = 10.0,
                         wall_budget_s: Optional[float] = None) -> Dict[str, Any]:
    """Q6 恢复探测：一次 ``query_all_stock`` 判定 BaoStock EU 链路是否存活。

    :return: ``{"alive": bool, "elapsed_s": float|None, "detail": str}``——**绝不抛异常**
        （探测失败/超时 = dead，由调用方写日志+progress 后按"死"处理）。
    :param timeout_s: socket 层超时（brief：10s 判活）——经 v6.1 socket 超时补丁生效
        （login/query 的 recv 都受此约束；无补丁时 EU hang 会永久卡死，正是本探测要防的）。
    :param wall_budget_s: 墙钟硬预算（默认 3×timeout+10s）——baostock 协议层若吞掉
        socket.timeout 进入内部循环时兜底：daemon 线程 join 超时即判 dead，绝不阻塞灌数启动。

    设计取舍：
    - ``max_attempts=1``：探测不重试（fail fast；重试会把"死"拖成分钟级）。
    - ``quota_path=False``：探测**不消耗日预算**——它是存活检查而非数据取数，且若当日
      预算已耗尽时探测会被 QuotaGuard 拒绝 → 误判 dead（语义错误）；1 次/启动对服务器
      侧 5万/日/IP 限流是噪声级。数据路径的 QuotaGuard 纪律不受影响。
    - 线程隔离：socket timeout 覆盖 recv，但 baostock 0.9.30 协议层行为不可全控
      （send_msg 吞异常返回 None 后上层可能循环）→ daemon 线程 + join 硬预算双保险。
    """
    t_start = time.monotonic()
    box: Dict[str, Any] = {"alive": False, "elapsed_s": None, "detail": ""}

    def _do_probe() -> None:
        t0 = time.monotonic()
        client = BaoStockClient(max_attempts=1, base_delay=0.0,
                                quota_path=False, socket_timeout=timeout_s)
        try:
            import baostock as bs

            fields, rows = client.call_with_fields(
                bs.query_all_stock, label="lake_probe_alive")
            box["alive"] = True
            box["detail"] = f"query_all_stock ok rows={len(rows)}"
        except Exception as exc:  # noqa: BLE001 - 探测语义：任何异常=dead
            box["alive"] = False
            box["detail"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - close 失败不影响探测结论
                pass
            box["elapsed_s"] = round(time.monotonic() - t0, 2)

    th = threading.Thread(target=_do_probe, name="bs-probe", daemon=True)
    th.start()
    budget = wall_budget_s if wall_budget_s is not None else timeout_s * 3 + 10
    th.join(budget)
    if th.is_alive():
        # 硬预算耗尽：socket hang 未被协议层释放 → 判 dead（daemon 线程随进程退出，无泄漏）
        box = {"alive": False, "elapsed_s": round(time.monotonic() - t_start, 2),
               "detail": f"探测超过 {budget:.0f}s 硬预算（socket hang）→ 判 dead"}
    return box


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
