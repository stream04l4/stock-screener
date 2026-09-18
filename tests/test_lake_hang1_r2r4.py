# -*- coding:utf-8 -*-
"""DEFECT-HANG-1 回归测试（R2 墙钟硬超时 + R4 停止感知 + baostock send_msg 紧循环守卫）。

**全离线**：零真实网络。"mock socket 延迟" = 一个 sleep 远超 timeout 的 callable（模拟
半死服务端/CLOSE-WAIT 永不返回的 recv）+ 一个 ``recv`` 恒返 b"" 的假 socket（模拟对端
已关连接）。brief R2 验收字面："mock socket 延迟 → 断言在超时内抛错而非挂起"。

覆盖：
- R2：fetch_with_timeout 墙钟硬上限——慢/挂死取数在 ~timeout 内抛 FetchTimeoutError（不挂）；
      快路径零误伤；非超时异常原样透传；FetchTimeoutError ⊂ RuntimeError（worker 回退接住）。
- R4：停止感知——SIGTERM 置停止标志后，在途取数 ≤~1s 放弃（不等满 timeout），收尾才可能 ≤20s。
- baostock send_msg 守卫（09-16 shutdown-spin 根因）：CLOSE-WAIT（recv==b""）立即返回 None，
      不紧循环（原版 while True: recv 会 100% CPU 自旋）。

纪律：conftest autouse 默认 LAKE_MULTISOURCE=0（本文件不依赖多源池）；fetch_with_timeout
用的 daemon 线程池是进程级共享——挂死任务占用的 worker 是 daemon，随进程退出回收，不阻塞
测试收尾。每个用例只留 ≤1 个 30s 挂死线程（池 8 worker，远未耗尽）。
"""
from __future__ import annotations

import threading
import time

import pytest


# ===========================================================================
# R2：fetch_with_timeout 墙钟硬超时
# ===========================================================================
def test_fetch_with_timeout_raises_within_budget():
    """mock 挂死取数（sleep 远超 timeout）→ ~timeout 内抛 FetchTimeoutError，不挂起。

    这是 R2 核心回归：任何单次取数都有墙钟硬上限——半死服务端/CLOSE-WAIT 慢滴答/无超时
    裸 requests 都无法把整进程拖死（HANG-1 卡死签名）。
    """
    from lake.ingest.common import FetchTimeoutError, fetch_with_timeout

    def _hang_forever():
        time.sleep(30)   # 模拟永不返回的 socket recv
        return "should-not-reach"

    t0 = time.monotonic()
    with pytest.raises(FetchTimeoutError):
        fetch_with_timeout(_hang_forever, timeout_s=1.5)
    elapsed = time.monotonic() - t0
    # 必须在 ~timeout(1.5s)+调度余量内抛，且**不得**等满 30s sleep（否则=挂起）。
    assert elapsed < 4.0, f"应在 ~1.5s 抛错，实际 {elapsed:.2f}s（疑似挂起）"
    assert elapsed >= 1.0, f"过早（{elapsed:.2f}s），超时未生效"


def test_fetch_with_timeout_fast_path_returns_result():
    """快路径：callable 在 timeout 内返回 → 原样返回结果（零误伤、正常路径不抛）。"""
    from lake.ingest.common import fetch_with_timeout

    def _fast(x):
        return x * 2

    assert fetch_with_timeout(_fast, 21, timeout_s=5.0) == 42


def test_fetch_with_timeout_propagates_non_timeout_exception():
    """callable 抛**非超时**异常 → 原样透传（不转 FetchTimeoutError，保留真实错误语义）。"""
    from lake.ingest.common import fetch_with_timeout

    def _boom():
        raise ValueError("simulated source error")

    with pytest.raises(ValueError, match="simulated source error"):
        fetch_with_timeout(_boom, timeout_s=5.0)


def test_fetch_timeout_error_is_runtimeerror_subclass():
    """FetchTimeoutError ⊂ RuntimeError——worker 的 ``except Exception`` 回退分支直接接住
    （该源失败→下一源；全源失败→不 mark_done），零新控制流（R2 设计契约）。"""
    from lake.ingest.common import FetchTimeoutError

    assert issubclass(FetchTimeoutError, RuntimeError)


# ===========================================================================
# R4：停止感知（SIGTERM → 在途取数立即放弃，不等满 timeout）
# ===========================================================================
def test_fetch_with_timeout_stop_aware(monkeypatch):
    """SIGTERM 置停止标志后，在途取数 ≤~1s 放弃（不等满 30s timeout）。

    R4 回归：SIGTERM 后进程须 ≤20s 退出。若"在途取数"要等满单次 timeout(≤30s)，收尾就
    可能超预算。fetch_with_timeout 的 ≤0.5s 分片轮询把最坏等待压到 ~1s——本用例断言之。
    """
    from lake import backfill as lb
    from lake.ingest.common import FetchTimeoutError, fetch_with_timeout

    def _hang_forever():
        time.sleep(30)

    # 取数启动 ~0.8s 后置停止标志（模拟 SIGTERM 落在取数中途）
    def _set_stop_later():
        time.sleep(0.8)
        lb.set_stop_requested()

    t = threading.Thread(target=_set_stop_later, daemon=True)
    t.start()
    try:
        t0 = time.monotonic()
        with pytest.raises(FetchTimeoutError):
            fetch_with_timeout(_hang_forever, timeout_s=30.0)
        elapsed = time.monotonic() - t0
    finally:
        lb.clear_stop_requested()
        t.join(timeout=2.0)
    # 应在 ~0.8-1.5s（停止标志 + 0.5s 分片）内中断，**不得**等满 30s timeout。
    assert elapsed < 5.0, f"停止信号应立即中断在途取数，实际 {elapsed:.2f}s（疑似等满 timeout）"


# ===========================================================================
# baostock send_msg 紧循环守卫（09-16 shutdown-spin 根因）
# ===========================================================================
class _FakeClosedSocket:
    """模拟对端已关连接的 socket（CLOSE-WAIT）：``recv`` **立即返回 b""**（不阻塞、不超时）。

    这正是 09-16 现场签名——原版 send_msg ``while True: recv; receive += recv; if
    receive[-13:]==delim: break`` 对恒空 recv 会无限紧循环（100% CPU，strace ~327k
    recvfrom/8s）。守卫见 ``recv==b""`` 即退出。
    """

    def send(self, data):  # noqa: ANN001 - 假 socket 接口
        return len(data)

    def recv(self, n):  # noqa: ANN001 - 对端已关 → 立即空读
        return b""


def test_baostock_send_msg_closed_socket_no_spin(monkeypatch):
    """CLOSE-WAIT（recv==b""）→ send_msg **立即**返回 None，不紧循环。

    这是 09-16 shutdown-spin 的直接回归：该路径可达主线程（run_history 收尾 bs.close()→
    logout()→send_msg），原版会让"summary 已打印后进程仍不退出、持 DuckDB lock+flock →
    web 409"。守卫必须让它在毫秒级退出（上层按失败重试/回退，零新控制流）。
    """
    import baostock.common.context as bs_context

    from screener.data import baostock_client as bc

    bc._install_send_msg_patch()   # 幂等；确保守卫已装（baostock 本环境已装）
    assert bc._SEND_MSG_PATCHED, "send_msg 紧循环守卫应已安装（baostock 存在时）"

    import baostock.util.socketutil as bs_sock

    monkeypatch.setattr(bs_context, "default_socket", _FakeClosedSocket(), raising=False)
    t0 = time.monotonic()
    result = bs_sock.send_msg("fake_message")
    elapsed = time.monotonic() - t0

    assert result is None, f"关闭连接应返回 None（按失败处理），实际 {result!r}"
    # 必须立即退出（<2s）。无守卫时此处是无限紧循环（测试会超时/100% CPU）。
    assert elapsed < 2.0, f"send_msg 应对关闭连接立即退出，实际 {elapsed:.2f}s（疑似紧循环）"


def test_baostock_send_msg_normal_response_roundtrip(monkeypatch):
    """健康路径零回归：正常响应（含压缩分隔符结尾）→ send_msg 原样返回解码字符串。

    守卫只改"recv==b"" 立即退出"这一处，正常 1-3 次 recv 读到 ``<![CDATA[]]>\n`` 分隔符
    即 break——本用例断言健康响应路径逐字节不变（不误伤正常查询）。
    """
    import baostock.common.context as bs_context

    from screener.data import baostock_client as bc

    bc._install_send_msg_patch()

    class _FakeOkSocket:
        """首次 recv 给一条**不压缩**的合法消息（21B header + body，以分隔符结尾）。

        header = ``00.9.30\1`` + msg_type(``01``=LOGIN_RESPONSE，**不在**压缩 tuple) +
        ``\1`` + 10 位补零长度 = 21 字节（与 baostock ``to_message_header`` 口径一致）。
        msg_type 非压缩类型 → send_msg 走 ``bytes.decode(receive)`` 分支（零 zlib）。
        """

        def __init__(self):
            header = b"00.9.30\x01" + b"01" + b"\x01" + b"0000000007"   # 21B
            body = b"x\x01y\x01z"
            self._payload = header + body + b"<![CDATA[]]>\n"

        def send(self, data):
            return len(data)

        def recv(self, n):
            if self._payload:
                out, self._payload = self._payload[:n], self._payload[n:]
                return out
            return b""

    monkeypatch.setattr(bs_context, "default_socket", _FakeOkSocket(), raising=False)
    import baostock.util.socketutil as bs_sock

    result = bs_sock.send_msg("fake_message")
    # 不压缩路径 → bytes.decode(receive)；应含 body 片段且非 None。
    assert isinstance(result, str) and "x" in result, f"正常响应应解码返回，实际 {result!r}"
