# -*- coding: utf-8 -*-
"""test_lake_v615_f3_timeout —— v6.1.5 F3 墙钟硬超时（离线）。

v7.0.18：baostock 渠道移除后仅存 ``_with_timeout`` 通用墙钟工具用例
（source_pool 单一事实来源；原 BaoStockAdapter.available/Q6 探测用例随渠道移除）。
"""
from __future__ import annotations

import os
import sys
import threading
import time

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (REPO_ROOT,):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ===========================================================================
# _with_timeout 通用墙钟工具（source_pool 单一事实来源）
# ===========================================================================
def test_with_timeout_fast_path_returns_result():
    from lake.ingest.source_pool import _with_timeout

    assert _with_timeout(lambda: 42, secs=5.0) == 42


def test_with_timeout_hang_raises_within_budget():
    """fake hang（Event.wait 真阻塞，不经 time.sleep）→ 在预算内抛 TimeoutError。"""
    from lake.ingest.source_pool import _with_timeout

    ev = threading.Event()   # 永不 set → wait(3) 真阻塞 3s

    def _hang():
        ev.wait(3)
        return "should-not-return"

    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        _with_timeout(_hang, secs=1.0, name="t-hang")
    elapsed = time.monotonic() - t0
    assert elapsed < 2.0, f"hang 不得超预算（{elapsed:.1f}s）"


def test_with_timeout_propagates_non_timeout_exception():
    from lake.ingest.source_pool import _with_timeout

    def _boom():
        raise ValueError("kaboom")

    with pytest.raises(ValueError):
        _with_timeout(_boom, secs=5.0)
