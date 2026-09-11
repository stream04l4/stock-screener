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
