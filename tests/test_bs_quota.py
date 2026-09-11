# -*- coding: utf-8 -*-
"""v5.2-p2 BaoStock 每日配额守卫测试（TL D7，全离线，零真实 baostock 调用）。

覆盖 brief D7 全部 7 项：
1. QuotaGuard 增量计数 + 状态文件读写；
2. 日期翻转重置（伪造旧 date 文件 → 下次 acquire count 从 0/1 起）；
3. 耗尽即 raise BaoStockError，且不触发 query_fn（monkeypatch 断言零调用）；
4. 多进程原子性：spawn 4 个进程各 increment K 次 → 最终 count == 总次数；
5. 多线程并发 increment 同样守恒；
6. client 集成：tiny quota=3 → 第 4 次 call raise，前 3 次正常返回（假 query_fn）；
7. quota_path=False 禁用守卫不计数。

另含 D3/D4/D6 相关断言：login/logout 不计入配额、90% 告警每进程一次、
--show / --set-count CLI。所有用例用 tmp_path 隔离状态文件，绝不碰
~/.stock_screener/bs_quota.json 生产计数。
"""
from __future__ import annotations

import json
import logging
from typing import Optional
import multiprocessing as mp
import os
import sys
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import screener.data.baostock_client as bsm  # noqa: E402
from screener.data.baostock_client import (  # noqa: E402
    BaoStockClient,
    BaoStockError,
    QuotaGuard,
)


def _today_beijing() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def _yesterday_beijing() -> str:
    return (datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=1)).isoformat()


def _tomorrow_beijing() -> str:
    return (datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=1)).isoformat()


# ===========================================================================
# D7-1: QuotaGuard 增量计数 + 状态文件读写
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
# D7-2: 日期翻转重置
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
# D7-3: 耗尽即 raise BaoStockError，且不触发 query_fn（零调用）
# ===========================================================================
def test_guard_exhausted_raises_and_file_stays_at_cap(tmp_path):
    p = str(tmp_path / "bs_quota.json")
    g = QuotaGuard(daily_quota=5, path=p)
    for i in range(1, 6):
        assert g.acquire() == i
    with pytest.raises(BaoStockError, match="配额耗尽"):
        g.acquire()
    # 超限那次不计数、不落盘 → 文件停在 daily_quota
    assert g.get_state() == (_today_beijing(), 5)


class _FakeRS:
    """假 baostock ResultData：单行成功结果。"""

    def __init__(self):
        self.error_code = "0"
        self.error_msg = "success"
        self.fields = ["code", "code_name"]
        self._rows = [["sh.600000", "浦发银行"]]
        self._last: Optional[list] = None

    def next(self) -> bool:
        if self._rows:
            self._last = self._rows.pop(0)
            return True
        return False

    def get_row_data(self):
        return self._last


def _make_fake_rs_factory(calls):
    def fake_query(code="sh.600000"):
        calls.append(code)
        return _FakeRS()
    return fake_query


@pytest.fixture
def offline_bs(monkeypatch):
    """打桩 bs.login（离线）；记录 login/logout 调用次数。"""
    logins, logouts = [], []

    class _Lg:
        error_code = "0"
        error_msg = "success"

    monkeypatch.setattr(bsm.bs, "login", lambda *a, **k: (logins.append(1), _Lg())[1])
    monkeypatch.setattr(bsm.bs, "logout", lambda *a, **k: logouts.append(1))
    return {"logins": logins, "logouts": logouts}


def test_exhausted_client_call_raises_without_query_fn(tmp_path, offline_bs):
    """D7-3：配额已满 → client.call 立即 raise，query_fn 零调用、login 也不发生。"""
    p = str(tmp_path / "bs_quota.json")
    QuotaGuard(daily_quota=3, path=p).set_count(3)  # 播种到满
    calls = []
    client = BaoStockClient(max_attempts=1, daily_quota=3, quota_path=p)
    with pytest.raises(BaoStockError, match="配额耗尽"):
        client.call(_make_fake_rs_factory(calls), label="x")
    assert calls == []                      # query_fn 零调用
    assert offline_bs["logins"] == []       # acquire 在 login 之前 → 连登录都不发生


# ===========================================================================
# D7-4: 多进程原子性（spawn 4 进程 × K 次，无丢失更新）
# ===========================================================================
def _mp_worker(path: str, quota: int, k: int) -> None:
    """子进程入口：独立 QuotaGuard 实例 increment k 次（模块级可 pickle）。"""
    from screener.data.baostock_client import QuotaGuard as _G
    g = _G(daily_quota=quota, path=path)
    for _ in range(k):
        g.acquire()


def test_multiprocess_increments_conserved(tmp_path, monkeypatch):
    p = str(tmp_path / "bs_quota.json")
    n_procs, k = 4, 25
    total = n_procs * k
    # spawn 子进程需能按名 import 本测试模块 → PYTHONPATH 带上 tests/
    tests_dir = str(Path(__file__).resolve().parent)
    monkeypatch.setenv("PYTHONPATH",
                       tests_dir + os.pathsep + os.environ.get("PYTHONPATH", ""))
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
    from screener.data.baostock_client import BaoStockError as _BE, QuotaGuard as _G
    g = _G(daily_quota=quota, path=path)
    ok = 0
    for _ in range(k):
        try:
            g.acquire()
            ok += 1
        except _BE:
            break
    out_q.put(ok)


def test_multiprocess_exhaustion_stops_at_cap(tmp_path, monkeypatch):
    """跨进程共享上限：quota=10，8 进程各抢 5 次 → 恰好 10 次成功、其余 raise。"""
    p = str(tmp_path / "bs_quota.json")
    tests_dir = str(Path(__file__).resolve().parent)
    monkeypatch.setenv("PYTHONPATH",
                       tests_dir + os.pathsep + os.environ.get("PYTHONPATH", ""))

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
# D7-5: 多线程并发 increment 守恒
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
# D7-6: client 集成（tiny quota=3 → 第 4 次 raise，前 3 次正常）
# ===========================================================================
def test_client_tiny_quota_third_ok_fourth_raises(tmp_path, offline_bs):
    p = str(tmp_path / "bs_quota.json")
    calls = []
    client = BaoStockClient(max_attempts=1, daily_quota=3, quota_path=p)
    qfn = _make_fake_rs_factory(calls)
    for i in range(3):
        rows = client.call(qfn, label=f"q{i}")
        assert rows == [["sh.600000", "浦发银行"]]
    # D3：只计数据查询，不计 login/logout → 状态文件 count 恰为 3
    g = QuotaGuard(daily_quota=3, path=p)
    assert g.get_state() == (_today_beijing(), 3)
    assert len(offline_bs["logins"]) == 1           # 线程局部会话只登录一次
    # 第 4 次：显式失败，query_fn 不再被调用（仍是 3）
    with pytest.raises(BaoStockError, match="配额耗尽"):
        client.call(qfn, label="q3")
    assert len(calls) == 3


def test_client_retry_counts_each_attempt(tmp_path, offline_bs):
    """计数含重试：失败一次再成功 = 2 次真实 API 调用（D1/D3）。"""
    p = str(tmp_path / "bs_quota.json")

    class _FailRS:
        error_code = "10004001"
        error_msg = "mock fail"
        fields = []

        def next(self):
            return False

    calls = []

    def flaky_query(code="sh.600000"):
        calls.append(1)
        if len(calls) == 1:
            return _FailRS()
        return _FakeRS()

    client = BaoStockClient(max_attempts=2, base_delay=0.0, daily_quota=49900, quota_path=p)
    rows = client.call(flaky_query, label="flaky")
    assert rows == [["sh.600000", "浦发银行"]]
    g = QuotaGuard(daily_quota=49900, path=p)
    assert g.get_state() == (_today_beijing(), 2)   # 重试也是真实 API 调用


def test_client_default_guard_uses_home_path(monkeypatch):
    """D2：默认启用——quota_path=None 时守卫挂在 default_quota_path（不改调用点也生效）。"""
    client = BaoStockClient()
    assert client.quota_guard is not None
    assert client.quota_guard.path == bsm.default_quota_path()


# ===========================================================================
# D7-7: quota_path=False 禁用守卫不计数
# ===========================================================================
def test_client_disabled_guard_counts_nothing(tmp_path, offline_bs):
    p = str(tmp_path / "bs_quota.json")
    calls = []
    client = BaoStockClient(max_attempts=1, daily_quota=3, quota_path=False)
    assert client.quota_guard is None
    qfn = _make_fake_rs_factory(calls)
    for _ in range(5):  # 超过 quota=3 也不受限（守卫已禁用）
        rows = client.call(qfn, label="free")
        assert rows == [["sh.600000", "浦发银行"]]
    assert len(calls) == 5
    assert not os.path.exists(p)                    # 从未落盘计数
    # quota_path=True（非 False）不是合法禁用值 → 显式报错，不静默
    with pytest.raises(TypeError):
        BaoStockClient(quota_path=True)


# ===========================================================================
# D4 补充：90% 告警每进程一次（不刷屏）
# ===========================================================================
def test_guard_90pct_warns_once_per_process(tmp_path, caplog):
    p = str(tmp_path / "bs_quota.json")
    g = QuotaGuard(daily_quota=10, path=p)
    g.set_count(8)  # 80%
    with caplog.at_level("WARNING", logger="screener.data.bs"):
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


def test_client_call_with_fields_integration(tmp_path, offline_bs):
    """call_with_fields（fetchers 实际使用的入口）同样受守卫约束。"""
    p = str(tmp_path / "bs_quota.json")
    QuotaGuard(daily_quota=2, path=p).set_count(2)
    calls = []
    client = BaoStockClient(max_attempts=1, daily_quota=2, quota_path=p)
    with pytest.raises(BaoStockError, match="配额耗尽"):
        client.call_with_fields(_make_fake_rs_factory(calls), label="wf")
    assert calls == []


def test_cli_show_corrupt_file_shows_zero(tmp_path, capsys):
    p = tmp_path / "bs_quota.json"
    p.write_text("not json", encoding="utf-8")
    rc = bsm._main(["--show", "--path", str(p)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "used=0" in out and "remaining=49900" in out


# ===========================================================================
# D6: 运维 CLI（--show / --set-count），纯本地文件操作
# ===========================================================================
def test_cli_show_and_set_count(tmp_path, capsys):
    p = str(tmp_path / "bs_quota.json")
    rc = bsm._main(["--show", "--path", p])
    assert rc == 0
    out = capsys.readouterr().out
    assert f"date={_today_beijing()}" in out and "used=0" in out and "remaining=49900" in out

    rc = bsm._main(["--set-count", "123", "--path", p])
    assert rc == 0
    rc = bsm._main(["--show", "--path", p])
    assert rc == 0
    out = capsys.readouterr().out
    assert "used=123" in out and "remaining=49777" in out

    # --set-count 指定日期（播种昨日）
    rc = bsm._main(["--set-count", "5", "--date", _yesterday_beijing(), "--path", p])
    assert rc == 0
    with open(p, encoding="utf-8") as f:
        assert json.load(f) == {"date": _yesterday_beijing(), "count": 5}

    # 无参数 → usage + exit 2
    rc = bsm._main([])
    assert rc == 2
    # --set-count 非法值（负数）→ exit 2
    rc = bsm._main(["--set-count", "-1", "--path", p])
    assert rc == 2
