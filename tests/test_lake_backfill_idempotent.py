# -*- coding: utf-8 -*-
"""test_lake_backfill_idempotent —— 补齐任务中断重跑不重复耗配额。

**验收点（brief）**：fake client 中断重跑，progress 文件跳过 done 键 → 已完成的
任务**不再调 BaoStock**（不重复耗配额）。

设计：
- ``BackfillRunner`` 用注入的 tmp progress_path（隔离真实 data/lake/）。
- fake worker 按 task 计数调用次数；"中断"= worker 在第 3 个任务抛异常（模拟进程被杀）。
- monkeypatch ``_quota_state`` → (False, 0)：本测试验证**进度幂等**，不依赖真实配额文件。
- 断言：重跑后 A/B 不再被调（skipped_done，**不重复耗配额**），只有 C 被补；
  已完成任务两跑合计恰调 1 次。
"""
from __future__ import annotations

import pytest

pytest.importorskip("duckdb")


class _FakeInterrupt(Exception):
    """模拟进程中断（worker 抛此异常 = 该任务未完成、未标 done）。"""


def test_backfill_resume_skips_done(tmp_path, monkeypatch):
    from lake import conn as lconn
    from lake.backfill import BackfillRunner, Task

    prog_path = str(tmp_path / "progress.json")

    # 隔离真实配额文件：本测试只验证进度幂等，不读 ~/.stock_screener/bs_quota.json
    monkeypatch.setattr(BackfillRunner, "_quota_state", lambda self: (False, 0))
    # 隔离真实库：_refresh_coverage 不得打开 data/lake/lake.duckdb（测试零副作用）
    monkeypatch.setattr(lconn, "get_conn", lambda: (_ for _ in ()).throw(
        lconn.LakeUnavailable("test isolation")))

    tasks = [Task(priority=1, table="fundamentals_quarterly", ts_code=f"sh.6{i:05d}",
                  period_or_date="2026Q2", tier="P1") for i in range(3)]
    keys = [t.ts_code for t in tasks]  # A/B/C

    # ---- Run 1：worker 处理 A、B 成功后在 C 处"中断"（抛异常）----
    calls_run1 = {k: 0 for k in keys}

    def worker1(task):
        calls_run1[task.ts_code] += 1
        if task.ts_code == keys[2]:      # C：模拟中断
            raise _FakeInterrupt("simulated crash")

    r1 = BackfillRunner(budget_per_day=5000, progress_path=prog_path)
    stats1 = r1.run(tasks, worker1)
    assert calls_run1[keys[0]] == 1 and calls_run1[keys[1]] == 1
    assert calls_run1[keys[2]] == 1       # C 被调了但失败（未标 done）
    assert stats1["processed"] == 2        # A、B 成功
    assert len(stats1["errors"]) == 1      # C 记错

    # ---- Run 2：新 runner（模拟重启）重跑同一队列 ----
    calls_run2 = {k: 0 for k in keys}

    def worker2(task):
        calls_run2[task.ts_code] += 1     # 全部成功

    r2 = BackfillRunner(budget_per_day=5000, progress_path=prog_path)
    stats2 = r2.run(tasks, worker2)

    # 核心断言：A/B 已 done → 重跑**不再调**（不重复耗配额）；只有 C 被补
    assert calls_run2[keys[0]] == 0, "A 已完成却被重跑（重复耗配额！）"
    assert calls_run2[keys[1]] == 0, "B 已完成却被重跑（重复耗配额！）"
    assert calls_run2[keys[2]] == 1, "C 中断后应被补一次"
    assert stats2["skipped_done"] == 2     # A、B 跳过
    assert stats2["processed"] == 1        # 只有 C

    # 核心幂等保证：**已完成**的任务（A/B）重跑不再调（不重复耗配额）；
    # 中断未完成的任务（C）被合法重试一次。两跑合计：A=1, B=1, C=2(1失败+1成功)。
    total = {k: calls_run1[k] + calls_run2[k] for k in keys}
    assert total[keys[0]] == 1, f"A 已完成却重复调用（{total[keys[0]]} 次）"
    assert total[keys[1]] == 1, f"B 已完成却重复调用（{total[keys[1]]} 次）"
    assert total[keys[2]] == 2, f"C 中断应重试一次（合计 {total[keys[2]]} 次）"


def test_backfill_done_persists_across_instances(tmp_path, monkeypatch):
    """done 键落盘：新 runner 实例能读到（跨进程/重启幂等的文件基础）。"""
    from lake.backfill import BackfillRunner, Task, load_progress

    monkeypatch.setattr(BackfillRunner, "_quota_state", lambda self: (False, 0))
    prog_path = str(tmp_path / "progress.json")
    task = Task(priority=2, table="kline_daily", ts_code="sh.601398",
                period_or_date="2026-05-13", tier="P2")

    def worker(task):
        pass

    r = BackfillRunner(budget_per_day=5000, progress_path=prog_path)
    r.run([task], worker)

    # 新实例（模拟重启）应判定该任务 done
    r2 = BackfillRunner(budget_per_day=5000, progress_path=prog_path)
    assert r2.is_done(task), "done 键未落盘/未被新实例读到"
    prog = load_progress(prog_path)
    assert ("kline_daily", "sh.601398", "2026-05-13") in [
        tuple(x) for x in prog.get("done", [])]
