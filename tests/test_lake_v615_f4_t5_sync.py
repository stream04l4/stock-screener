# -*- coding: utf-8 -*-
"""test_lake_v615_f4_t5_sync —— v6.1.5 F4：T5 基本面一键启动（/sync/start mode=t5）。

覆盖 brief F4"Web UI 可启动 T5"的**离线可测**部分（真实 spawn 由 TL 验收时实测）：

- ``mode=t5`` → ``start_sync(sub='history', extra_args=[--t5])``（kline_history 幂等全
  跳过、只灌 T5；sub 恒 history，--t5 是开关非独立子命令）。
- 响应回显 ``mode='t5'``。
- ``codes + t5`` 组合：extra_args = [--codes, ..., --t5]（顺序稳定）。
- **互斥/409 语义不变**：已 running → LakeSyncConflict(409)（mode=t5 不改变互斥契约）。
- 非法 mode（如 'full'）→ 400（白名单扩为 history|incremental|t5，其余仍拒）。

纪律：全离线——monkeypatch ``lake.sync_control.start_sync``（fake，零 spawn、零网络）；
conftest autouse LAKE_MULTISOURCE=0。与 v614 O2 的 /sync/start mode 用例同构。
"""
from __future__ import annotations

import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import lake.web_api as wapi  # noqa: E402


def _fake_start(captured):
    def fake_start(**kw):
        captured.update(kw)
        return {"started": True, "pid": 1234, "log_path": "/tmp/x.log"}

    return fake_start


def test_f4_sync_start_mode_t5_passthrough(monkeypatch):
    """mode=t5 → start_sync(sub='history', extra_args=[--t5])；响应回显 mode='t5'。"""
    import lake.sync_control as sc

    captured = {}
    monkeypatch.setattr(sc, "start_sync", _fake_start(captured))
    d = wapi.sync_start(mode="t5")
    assert captured.get("sub") == "history", f"t5 必须走 history 子命令，got {captured.get('sub')}"
    assert "--t5" in (captured.get("extra_args") or []), f"t5 必须透传 --t5，got {captured.get('extra_args')}"
    assert d["mode"] == "t5" and d["started"] is True


def test_f4_sync_start_mode_t5_with_codes(monkeypatch):
    """codes + t5 组合：extra_args = [--codes, <cs>, --t5]（顺序稳定）。"""
    import lake.sync_control as sc

    captured = {}
    monkeypatch.setattr(sc, "start_sync", _fake_start(captured))
    wapi.sync_start(codes="sh.601398,sz.000001", mode="t5")
    assert captured.get("sub") == "history"
    assert captured.get("extra_args") == ["--codes", "sh.601398,sz.000001", "--t5"]


def test_f4_sync_start_mode_t5_running_409_unchanged(monkeypatch):
    """已 running → 409 现状不变（mode=t5 不改变互斥语义——brief"互斥/409/锁语义与现有一致"）。"""
    import lake.sync_control as sc

    monkeypatch.setattr(
        sc, "start_sync",
        lambda **k: {"started": False, "pid": 777, "log_path": None,
                     "reason": "already_running"})
    with pytest.raises(wapi.LakeSyncConflict) as ei:
        wapi.sync_start(mode="t5")
    assert ei.value.status_code == 409


def test_f4_sync_start_bad_mode_still_400(monkeypatch):
    """非法 mode → 400（v6.1.6 白名单扩为 full|history|incremental|t5，其余仍拒）。"""
    import lake.sync_control as sc

    monkeypatch.setattr(sc, "start_sync", lambda **k: pytest.fail("非法 mode 不得 spawn"))
    with pytest.raises(wapi.HTTPException) as ei:
        wapi.sync_start(mode="p0")
    assert ei.value.status_code == 400


def test_f4_sync_start_history_incremental_unchanged(monkeypatch):
    """回归：history/incremental 行为不变（t5/full 加入不得破坏既有 mode）。"""
    import lake.sync_control as sc

    captured = {}
    monkeypatch.setattr(sc, "start_sync", _fake_start(captured))
    wapi.sync_start(mode="incremental")
    assert captured.get("sub") == "incremental"
    assert "--t5" not in (captured.get("extra_args") or [])   # incremental 不带 --t5

    captured.clear()
    d = wapi.sync_start(mode="history")   # v6.1.6：缺省已改 full，history 走显式传参
    assert captured.get("sub") == "history"
    assert "--t5" not in (captured.get("extra_args") or [])
    assert d["mode"] == "history"


def test_f4_sync_start_default_full_v616(monkeypatch):
    """v6.1.6：/sync/start 缺省 mode=full → start_sync(sub='full')；响应回显 full。"""
    import lake.sync_control as sc

    captured = {}
    monkeypatch.setattr(sc, "start_sync", _fake_start(captured))
    d = wapi.sync_start()   # FieldInfo 直调缺省 → 归一化 full
    assert captured.get("sub") == "full"
    assert d["mode"] == "full" and d["started"] is True


def test_f4_sync_start_full_with_codes(monkeypatch):
    """v6.1.6：codes + full 组合 → extra_args=[--codes, ...]（顺序稳定）。"""
    import lake.sync_control as sc

    captured = {}
    monkeypatch.setattr(sc, "start_sync", _fake_start(captured))
    wapi.sync_start(codes="sh.601398,sz.000001")   # 缺省 mode=full
    assert captured.get("sub") == "full"
    assert captured.get("extra_args") == ["--codes", "sh.601398,sz.000001"]
