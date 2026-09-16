# -*- coding: utf-8 -*-
"""pytest 配置：把 tests/ 目录加入 sys.path（便于导入 conftest_helpers）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

# ===========================================================================
# v5.2 Phase 1：raw/canonical 层测试隔离（autouse）
# 生产钩子在 canonical.enabled=true（默认）时写 <repo>/data/raw、data/canonical。
# 离线管线单测跑 run_screener 会触发这些钩子 → 必须重定向到 tmp_path，
# 否则测试套件向仓库目录落盘（污染工作区 + 跨测试状态泄漏）。
# ===========================================================================
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _v52_isolate_raw_canonical(tmp_path, monkeypatch):
    from screener.data import rawstore as rs
    from screener.data import canonical as cs

    rs.reset_raw_store()
    cs.reset_canonical_store()
    monkeypatch.setattr(rs, "_resolve_root", lambda: str(tmp_path / "raw"))
    monkeypatch.setattr(cs, "_project_root", lambda: str(tmp_path))
    yield
    rs.reset_raw_store()
    cs.reset_canonical_store()


# ===========================================================================
# v6.1 多源资源池：测试隔离门（autouse，默认关）
# ---------------------------------------------------------------------------
# v6.1 给 run_history/run_t7/run_p0 引入 source_pool.resolve_source()——它会懒加载
# **真实** adapter 注册表并调 available()（EU 网络自检）。既有离线单测（v602/v603/
# v607）全部 monkeypatch 腾讯/BaoStock 走 legacy 路径、契约是"零真实网络"——若多源
# 池默认开启，resolve_source 会对真实 adapter 发 EU 自检请求 → 破坏离线契约 + 拖慢套件。
#
# 故本 autouse fixture 默认置 LAKE_MULTISOURCE=0：所有既有测试走 legacy 单源路径
# （行为逐字节不变，零回归）。v6.1 多源专属用例（test_lake_v61_multisource）**显式**
# monkeypatch.delenv("LAKE_MULTISOURCE") + 注入 mock adapter 注册表来开启多源。
# ===========================================================================
@pytest.fixture(autouse=True)
def _v61_isolate_multisource(monkeypatch):
    monkeypatch.setenv("LAKE_MULTISOURCE", "0")
    yield

