# -*- coding:utf-8 -*-
"""DEFECT-HANG-1 回归测试（R2 墙钟硬超时 + R4 停止感知；v7.0.18 后 baostock 守卫段随渠道移除）。

**全离线**：零真实网络。"mock socket 延迟" = 一个 sleep 远超 timeout 的 callable（模拟
半死服务端/CLOSE-WAIT 永不返回的 recv）+ 一个 ``recv`` 恒返 b"" 的假 socket（模拟对端
已关连接）。brief R2 验收字面："mock socket 延迟 → 断言在超时内抛错而非挂起"。

覆盖：
- R2：fetch_with_timeout 墙钟硬上限——慢/挂死取数在 ~timeout 内抛 FetchTimeoutError（不挂）；
      快路径零误伤；非超时异常原样透传；FetchTimeoutError ⊂ RuntimeError（worker 回退接住）。
- R4：停止感知——SIGTERM 置停止标志后，在途取数 ≤~1s 放弃（不等满 timeout），收尾才可能 ≤20s。
- （v7.0.18：原 baostock send_msg 守卫覆盖随渠道移除。）

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

# v7.0.18：原 baostock send_msg 紧循环守卫两用例随 baostock 渠道整体移除
#（09-16 shutdown-spin 根因已无复现路径——BaoStockClient/bs.logout 不复存在）。
