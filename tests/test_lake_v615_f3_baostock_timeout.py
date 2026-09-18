# -*- coding: utf-8 -*-
"""test_lake_v615_f3_baostock_timeout —— v6.1.5 F3：baostock fail-fast 硬超时（离线）。

覆盖 brief F3"任何代码路径在 baostock 上阻塞不得超过 20s"的**离线可测**部分
（真实 EU hang 实测只进 smoke 证据，不发真实请求）：

- ``_with_timeout`` 通用墙钟工具（source_pool 单一事实来源）：
  快路径返回 / hang→TimeoutError(≤预算) / 非超时异常透传。
- ``BaoStockAdapter.available()`` 三级短路 + 硬超时：
  - fake probe hang（>上限）→ 在**上限内**返回 False、detail="timeout"（不挂死）；
  - fake probe 快 alive → True + 懒缓存（二次调用零探测）；
  - Q6 已探过（baostock_probed=True）→ 复用共享结果，**不再重复探网**（防双连接黑名单）。
- ``_available_timeout_s`` env 覆盖（BS_AVAILABLE_TIMEOUT_S）+ 缺省 <20s 红线。
- Q6 启动探测（scripts/lake_backfill._run_bs_probe）≤20s 硬超时：fake hang → alive=False
  detail="probe timeout >Ns"；快 alive → 正常透传 + 共享状态回写；_q6_probe_timeout_s
  缺省 20.0（brief 红线值）+ env BS_Q6_PROBE_TIMEOUT_S 覆盖（仅测试提速）。

纪律：全离线——monkeypatch ``screener.data.baostock_client.probe_baostock_alive``，
零真实网络；hang 用 threading.Event.wait（不经 time.sleep，autouse patch 不到 → 真阻塞）。
conftest autouse 默认 LAKE_MULTISOURCE=0（本文件 available() 不读该门，直接探网被 fake 拦截；
Q6 用例显式 delenv 开门 + monkeypatch lake_cfg/progress 全隔离 tmp）。
"""
from __future__ import annotations

import os
import sys
import threading
import time

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")   # Q6 用例 import lake_backfill（同 v61_multisource）
for _p in (REPO_ROOT, SCRIPTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ===========================================================================
# 1) _with_timeout 通用墙钟工具（source_pool 单一事实来源）
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


# ===========================================================================
# 2) BaoStockAdapter.available() —— F3 硬超时真探（fake probe，零网络）
# ===========================================================================
def _reset_bs_state():
    from lake.ingest.source_pool import reset_baostock_state_for_test

    reset_baostock_state_for_test()


def test_available_hang_returns_false_within_budget(monkeypatch):
    """fake probe hang（>上限）→ available() 在**上限内**返回 False、detail='timeout'。

    这是 F3 关键证据的离线版：EU IP socket hang 场景下 available() 绝不长挂，
    在硬超时（缺省 15s，此处 env 压到 1s）内必返回 False + detail='timeout'。
    """
    import screener.data.baostock_client as bsc
    from lake.ingest.baostock_adapter import BaoStockAdapter

    monkeypatch.setenv("BS_AVAILABLE_TIMEOUT_S", "1")   # 测试提速（生产缺省 15s<20s）
    _reset_bs_state()

    ev = threading.Event()   # 永不 set → probe 内 wait(3) 真阻塞（>1s 上限）

    def fake_probe(timeout_s=10.0, wall_budget_s=None):
        ev.wait(3)   # 模拟 baostock socket hang（不经 time.sleep，patch 不到）
        return {"alive": True, "elapsed_s": None, "detail": "should-not-return"}

    monkeypatch.setattr(bsc, "probe_baostock_alive", fake_probe)
    ad = BaoStockAdapter()
    t0 = time.monotonic()
    ok = ad.available()
    elapsed = time.monotonic() - t0
    assert ok is False, f"hang 场景必须判死（False），got {ok}"
    assert elapsed < 2.5, f"available() 不得超硬超时（{elapsed:.1f}s > ~1s）"
    from lake.ingest.source_pool import baostock_alive

    assert baostock_alive() is False   # 共享状态回写为死


def test_available_fast_alive_caches(monkeypatch):
    """fake probe 快 alive → True + 懒缓存（二次调用零探测，防重复探网）。"""
    import screener.data.baostock_client as bsc
    from lake.ingest.baostock_adapter import BaoStockAdapter

    _reset_bs_state()
    calls = {"n": 0}

    def fake_probe(timeout_s=10.0, wall_budget_s=None):
        calls["n"] += 1
        return {"alive": True, "elapsed_s": 0.1, "detail": "query_all_stock ok rows=2"}

    monkeypatch.setattr(bsc, "probe_baostock_alive", fake_probe)
    ad = BaoStockAdapter()
    assert ad.available() is True
    assert ad.available() is True   # 懒缓存命中
    assert calls["n"] == 1, f"二次调用必须走懒缓存零探测，got {calls['n']} 次"


def test_available_reuses_q6_shared_result_no_reprobe(monkeypatch):
    """Q6 已探过（baostock_probed=True）→ available() 复用共享结果，**不再重复探网**。

    为什么必须测：灌数启动 _run_bs_probe 已探一次后，resolve_source 调 available()
    若再探 = 双 BaoStock 连接 → 触发服务端黑名单（团队纪律）。本用例断言零二次探测。
    """
    import screener.data.baostock_client as bsc
    from lake.ingest.baostock_adapter import BaoStockAdapter
    from lake.ingest.source_pool import set_baostock_alive

    _reset_bs_state()
    set_baostock_alive(True, "Q6 启动探测 ok")   # 模拟灌数启动已探过

    calls = {"n": 0}

    def fake_probe(timeout_s=10.0, wall_budget_s=None):
        calls["n"] += 1
        return {"alive": False, "elapsed_s": None, "detail": "must-not-be-called"}

    monkeypatch.setattr(bsc, "probe_baostock_alive", fake_probe)
    ad = BaoStockAdapter()
    assert ad.available() is True   # 复用 Q6 共享结果（True）
    assert calls["n"] == 0, f"Q6 已探过不得重复探网，got {calls['n']} 次"


def test_available_probe_exception_returns_false(monkeypatch):
    """probe 抛异常 → available() False（保守按死，不 crash）。"""
    import screener.data.baostock_client as bsc
    from lake.ingest.baostock_adapter import BaoStockAdapter

    _reset_bs_state()

    def fake_probe(timeout_s=10.0, wall_budget_s=None):
        raise RuntimeError("baostock login 失败")

    monkeypatch.setattr(bsc, "probe_baostock_alive", fake_probe)
    ad = BaoStockAdapter()
    assert ad.available() is False


# ===========================================================================
# 3) _available_timeout_s env 覆盖 + <20s 红线
# ===========================================================================
def test_available_timeout_default_under_20s(monkeypatch):
    from lake.ingest.baostock_adapter import BaoStockAdapter

    monkeypatch.delenv("BS_AVAILABLE_TIMEOUT_S", raising=False)
    assert BaoStockAdapter._available_timeout_s() == 15.0   # 缺省 15s（<20s 红线）


def test_available_timeout_env_override(monkeypatch):
    from lake.ingest.baostock_adapter import BaoStockAdapter

    monkeypatch.setenv("BS_AVAILABLE_TIMEOUT_S", "3")
    assert BaoStockAdapter._available_timeout_s() == 3.0
    monkeypatch.setenv("BS_AVAILABLE_TIMEOUT_S", "not-a-number")
    assert BaoStockAdapter._available_timeout_s() == 15.0   # 非法值回退缺省


# ===========================================================================
# 4) Q6 启动探测（scripts/lake_backfill._run_bs_probe）—— F3 ≤20s 硬超时
# ===========================================================================
def _q6_setup(monkeypatch, tmp_path):
    """Q6 用例公共隔离：开门 + config/progress 全 monkeypatch（零生产副作用）。"""
    import lake_backfill as drv
    from lake.ingest.source_pool import reset_baostock_state_for_test

    monkeypatch.delenv("LAKE_MULTISOURCE", raising=False)      # 多源门开（否则 Q6 直接跳过）
    monkeypatch.delenv("LAKE_DISABLE_BAOSTOCK", raising=False)
    from lake import config as lconfig

    monkeypatch.setattr(lconfig, "lake_cfg", lambda: {"baostock_probe_enabled": True})
    monkeypatch.setattr(drv, "_progress_for_db", lambda db_path=None: str(tmp_path / "p.json"))
    from lake import backfill as lb

    monkeypatch.setattr(lb, "load_progress", lambda path=None: {})
    monkeypatch.setattr(lb, "save_progress", lambda prog, path=None: None)
    reset_baostock_state_for_test()
    return drv


def test_q6_probe_hang_returns_dead_within_budget(monkeypatch, tmp_path):
    """fake probe hang（>上限）→ Q6 在**预算内**返回 alive=False、detail='probe timeout >Ns'。

    F3 关键路径离线版：灌数启动绝不因 baostock hang 长挂；超时记 alive=false
    detail="probe timeout >20s"（brief 逐字；此处 env 压到 1s → 'probe timeout >1s'）。
    """
    import screener.data.baostock_client as bsc

    drv = _q6_setup(monkeypatch, tmp_path)
    monkeypatch.setenv("BS_Q6_PROBE_TIMEOUT_S", "1")   # 测试提速（生产缺省 20s）

    ev = threading.Event()   # 永不 set → wait(3) 真阻塞 >1s 上限

    def fake_probe(timeout_s=10.0, wall_budget_s=None):
        ev.wait(3)
        return {"alive": True, "elapsed_s": None, "detail": "should-not-return"}

    monkeypatch.setattr(bsc, "probe_baostock_alive", fake_probe)
    t0 = time.monotonic()
    res = drv._run_bs_probe()
    elapsed = time.monotonic() - t0
    assert res["enabled"] is True
    assert res["alive"] is False, f"hang 必须判死，got {res}"
    assert res["detail"] == "probe timeout >1s", f"detail 逐字契约，got {res['detail']!r}"
    assert elapsed < 2.5, f"Q6 不得超硬预算（{elapsed:.1f}s > ~1s）"
    from lake.ingest.source_pool import baostock_alive, baostock_probed

    assert baostock_alive() is False and baostock_probed() is True   # 共享状态回写


def test_q6_probe_fast_alive_passthrough(monkeypatch, tmp_path):
    """fake probe 快 alive → Q6 正常透传 + 共享状态回写（回归：正常路径不被超时逻辑误伤）。"""
    import screener.data.baostock_client as bsc

    drv = _q6_setup(monkeypatch, tmp_path)

    def fake_probe(timeout_s=10.0, wall_budget_s=None):
        assert wall_budget_s == 15.0, f"内层预算须显式收紧到 15s（<20s 红线），got {wall_budget_s}"
        return {"alive": True, "elapsed_s": 0.3, "detail": "query_all_stock ok rows=2"}

    monkeypatch.setattr(bsc, "probe_baostock_alive", fake_probe)
    res = drv._run_bs_probe()
    assert res["alive"] is True and res["detail"] == "query_all_stock ok rows=2"
    from lake.ingest.source_pool import baostock_alive

    assert baostock_alive() is True


def test_q6_timeout_default_is_20s(monkeypatch):
    """缺省 20.0（brief F3 红线逐字值）；env BS_Q6_PROBE_TIMEOUT_S 仅测试提速。"""
    import lake_backfill as drv

    monkeypatch.delenv("BS_Q6_PROBE_TIMEOUT_S", raising=False)
    assert drv._q6_probe_timeout_s() == 20.0
    monkeypatch.setenv("BS_Q6_PROBE_TIMEOUT_S", "1")
    assert drv._q6_probe_timeout_s() == 1.0
    monkeypatch.setenv("BS_Q6_PROBE_TIMEOUT_S", "bad")
    assert drv._q6_probe_timeout_s() == 20.0   # 非法值回退缺省
