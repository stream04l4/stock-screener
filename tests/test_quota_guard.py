# -*- coding: utf-8 -*-
"""v7.0.18 通用日配额守卫测试（全离线，零网络调用）。

QuotaGuard 自 baostock_client.py 迁至 lake/ingest/common.py（渠道移除，守卫泛化保留）；
本文件承接原 tests/test_bs_quota.py 中**与 BaoStockClient 无关**的守卫行为用例
（计数/日期翻转/耗尽/多进程/多线程/90% 告警/坏文件 fail-open）。
Client 集成与运维 CLI 用例随 baostock 渠道整体移除（守卫已无生产计数方）。
所有用例用 tmp_path 隔离状态文件，绝不碰 ~/.stock_screener/bs_quota.json 生产计数。
"""
from __future__ import annotations

import json
import logging
import multiprocessing as mp
import os
import sys
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lake.ingest.common import QuotaExhaustedError, QuotaGuard  # noqa: E402


def _today_beijing() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def _yesterday_beijing() -> str:
    return (datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=1)).isoformat()


def _tomorrow_beijing() -> str:
    return (datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=1)).isoformat()


# ===========================================================================
# 1. QuotaGuard 增量计数 + 状态文件读写
# ===========================================================================
def test_guard_increment_and_state_file(tmp_path):
    p = str(tmp_path / "bs_quota.json")
    g = QuotaGuard(daily_quota=49900, path=p)
    assert g.acquire() == 1
    assert g.acquire() == 2
    # 状态文件格式 {"date": "YYYY-MM-DD", "count": N}（北京时间日期）
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    assert data == {"date": _today_beijing(), "count": 2}
    # get_state 读回一致
    assert g.get_state() == (_today_beijing(), 2)


def test_guard_set_count_seed_and_reset(tmp_path):
    p = str(tmp_path / "bs_quota.json")
    g = QuotaGuard(daily_quota=49900, path=p)
    g.set_count(12345)
    assert g.get_state() == (_today_beijing(), 12345)
    assert g.acquire() == 12346  # 播种后从 12345 继续累加
    g.set_count(0)               # 重置
    assert g.get_state() == (_today_beijing(), 0)


def test_guard_corrupt_file_fails_open(tmp_path):
    """状态文件损坏 → 视为 0（fail-open：宁可从 0 重计也不阻断运行）。"""
    p = tmp_path / "bs_quota.json"
    p.write_text("{not valid json", encoding="utf-8")
    g = QuotaGuard(daily_quota=10, path=str(p))
    assert g.acquire() == 1


def test_guard_bad_quota_rejected(tmp_path):
    with pytest.raises(ValueError):
        QuotaGuard(daily_quota=0, path=str(tmp_path / "q.json"))
    with pytest.raises(ValueError):
        QuotaGuard(daily_quota=-5, path=str(tmp_path / "q.json"))


# ===========================================================================
# 2. 日期翻转重置
# ===========================================================================
def test_guard_date_flip_resets_count(tmp_path):
    p = str(tmp_path / "bs_quota.json")
    Path(p).write_text(json.dumps({"date": _yesterday_beijing(), "count": 49000}), encoding="utf-8")
    g = QuotaGuard(daily_quota=49900, path=p)
    # 旧日期的大计数不得延续 → 从 0 起，第一次 acquire = 1
    assert g.acquire() == 1
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    assert data["date"] == _today_beijing() and data["count"] == 1


def test_guard_future_date_file_also_resets(tmp_path):
    p = str(tmp_path / "bs_quota.json")
    Path(p).write_text(json.dumps({"date": _tomorrow_beijing(), "count": 999}), encoding="utf-8")
    g = QuotaGuard(daily_quota=49900, path=p)
    assert g.acquire() == 1


# ===========================================================================
# 3. 耗尽即 raise QuotaExhaustedError
# ===========================================================================
def test_guard_exhausted_raises_and_file_stays_at_cap(tmp_path):
    p = str(tmp_path / "bs_quota.json")
    g = QuotaGuard(daily_quota=5, path=p)
    for i in range(1, 6):
        assert g.acquire() == i
    with pytest.raises(QuotaExhaustedError, match="配额耗尽"):
        g.acquire()
    # 超限那次不计数、不落盘 → 文件停在 daily_quota
    assert g.get_state() == (_today_beijing(), 5)


# ===========================================================================
# 4. 多进程原子性（spawn 4 进程 × K 次，无丢失更新）
# ===========================================================================
def _mp_worker(path: str, quota: int, k: int) -> None:
    """子进程入口：独立 QuotaGuard 实例 increment k 次（模块级可 pickle）。"""
    from lake.ingest.common import QuotaGuard as _G
    g = _G(daily_quota=quota, path=path)
    for _ in range(k):
        g.acquire()


def test_multiprocess_increments_conserved(tmp_path, monkeypatch):
    p = str(tmp_path / "bs_quota.json")
    n_procs, k = 4, 25
    total = n_procs * k
    # spawn 子进程需能 import lake 包 → PYTHONPATH 带上仓库根
    root = str(Path(__file__).resolve().parent.parent)
    monkeypatch.setenv("PYTHONPATH",
                       root + os.pathsep + os.environ.get("PYTHONPATH", ""))
    ctx = mp.get_context("spawn")
    procs = [ctx.Process(target=_mp_worker, args=(p, 49900, k)) for _ in range(n_procs)]
    for pr in procs:
        pr.start()
    for pr in procs:
        pr.join(timeout=120)
    assert all(pr.exitcode == 0 for pr in procs), [pr.exitcode for pr in procs]
    # 无丢失更新：最终 count == 总次数
    g = QuotaGuard(daily_quota=49900, path=p)
    assert g.get_state() == (_today_beijing(), total)


def _mp_worker_cap(path: str, quota: int, k: int, out_q) -> None:
    """子进程入口（模块级可 pickle）：抢 k 次配额，成功几次就 put 几次。"""
    from lake.ingest.common import QuotaExhaustedError as _QE, QuotaGuard as _G
    g = _G(daily_quota=quota, path=path)
    ok = 0
    for _ in range(k):
        try:
            g.acquire()
            ok += 1
        except _QE:
            break
    out_q.put(ok)


def test_multiprocess_exhaustion_stops_at_cap(tmp_path, monkeypatch):
    """跨进程共享上限：quota=10，8 进程各抢 5 次 → 恰好 10 次成功、其余 raise。"""
    p = str(tmp_path / "bs_quota.json")
    root = str(Path(__file__).resolve().parent.parent)
    monkeypatch.setenv("PYTHONPATH",
                       root + os.pathsep + os.environ.get("PYTHONPATH", ""))

    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    procs = [ctx.Process(target=_mp_worker_cap, args=(p, 10, 5, q)) for _ in range(8)]
    for pr in procs:
        pr.start()
    results = [q.get(timeout=120) for _ in procs]
    for pr in procs:
        pr.join(timeout=120)
    assert all(pr.exitcode == 0 for pr in procs)
    assert sum(results) == 10                       # 全局恰好放行 quota 次
    g = QuotaGuard(daily_quota=10, path=p)
    assert g.get_state() == (_today_beijing(), 10)   # 文件停在 cap，不越界


# ===========================================================================
# 5. 多线程并发 increment 守恒
# ===========================================================================
def test_threaded_increments_conserved(tmp_path):
    p = str(tmp_path / "bs_quota.json")
    n_threads, k = 8, 25
    g = QuotaGuard(daily_quota=49900, path=p)
    errors = []

    def _worker():
        try:
            for _ in range(k):
                g.acquire()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=_worker) for _ in range(n_threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=120)
    assert not errors
    assert g.get_state() == (_today_beijing(), n_threads * k)


# ===========================================================================
# 6. 90% 告警每进程一次（不刷屏）
# ===========================================================================
def test_guard_90pct_warns_once_per_process(tmp_path, caplog):
    p = str(tmp_path / "bs_quota.json")
    g = QuotaGuard(daily_quota=10, path=p)
    g.set_count(8)  # 80%
    with caplog.at_level("WARNING", logger="lake.ingest.common"):
        g.acquire()  # → 9（90%）：触发告警
        g.acquire()  # → 10：不得再告警
    warns = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warns) == 1
    assert "配额" in warns[0].getMessage()


def test_guard_creates_missing_parent_dirs(tmp_path):
    """状态文件父目录不存在 → 自动创建（prewarm/migrate 首跑场景）。"""
    p = str(tmp_path / "deep" / "nested" / "bs_quota.json")
    g = QuotaGuard(daily_quota=10, path=p)
    assert g.acquire() == 1
    assert Path(p).exists()


def test_get_state_corrupt_file_returns_zero(tmp_path):
    p = tmp_path / "bs_quota.json"
    p.write_text("garbage", encoding="utf-8")
    g = QuotaGuard(daily_quota=10, path=str(p))
    assert g.get_state() == (_today_beijing(), 0)
