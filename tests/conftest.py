# -*- coding: utf-8 -*-
"""pytest 配置：把 tests/ 目录加入 sys.path（便于导入 conftest_helpers）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

# ===========================================================================
# v6.1 多源资源池：测试隔离门（autouse，默认关）
# ---------------------------------------------------------------------------
# v6.1 给 run_history/run_t7/run_p0 引入 source_pool.resolve_source()——它会懒加载
# **真实** adapter 注册表并调 available()（EU 网络自检）。既有离线单测（v602/v603/
# v607）全部 monkeypatch 腾讯/BaoStock 走 legacy 路径、契约是"零真实网络"——若多源
# 池默认开启，resolve_source 会对真实 adapter 发 EU 自检请求 → 破坏离线契约 + 拖慢套件。
#
# 故本 autouse fixture 默认置 LAKE_MULTISOURCE=0：所有既有测试走 legacy 单源路径
# （行为逐字节不变，零回归）。多源专属用例显式 delenv 来开启多源。
#
# 注（v7.0）：原 v5.2 raw/canonical 隔离 fixture 随 screener.data.{rawstore,canonical}
# 删除而移除（v7 无 raw/canonical 层）。
# ===========================================================================
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _v61_isolate_multisource(monkeypatch):
    monkeypatch.setenv("LAKE_MULTISOURCE", "0")
    yield


# ===========================================================================
# v7.0 生产湖锁泄漏守卫（autouse）
# ---------------------------------------------------------------------------
# lake.conn.get_conn()/open() 的进程级 RW 单例一旦对生产 data/lake/lake.duckdb
# 打开就**永不自动关闭**（只有 reset_for_test() 能重置）。任一测试经
# BackfillRunner._coverage_conn()（db_path=None 缺省路径）等间接触发 → 生产湖
# 被本进程 RW 锁占住 → 其后所有 read_only 连接报
# "different configuration than existing connections"，红集无声扩散。
# 既有两个泄漏实例（v7.0 修复）：test_lake_backfill_idempotent
# ::test_backfill_done_persists_across_instances、test_lake_v6010_stop_lock
# ::test_stopping_marker_cleared_on_new_run / ::test_new_run_clears_residual_
# persisted_in_first_task_window（in-process run() 未隔离 get_conn）。
# 本守卫在**每个**测试结束后检查单例：仍开着 → 立即失败该测试并清理，把泄漏
# 变成当场可见的红，而不是污染后续测试。
# ===========================================================================
@pytest.fixture(autouse=True)
def _no_production_lake_leak():
    yield
    try:
        from lake import conn as _lconn
    except Exception:  # noqa: BLE001 - 无 lake 环境/导入异常：无守卫对象
        return
    con = getattr(_lconn, "_conn", None)
    if con is None:
        return
    try:
        con.close()
    except Exception:  # noqa: BLE001
        pass
    _lconn._conn = None
    pytest.fail(
        "测试留下 lake.conn 进程级 RW 单例未关闭（生产湖锁泄漏 → 其后 read_only "
        "连接将失败）。在测试内 monkeypatch lconn.get_conn 或传 db_path 隔离。")

