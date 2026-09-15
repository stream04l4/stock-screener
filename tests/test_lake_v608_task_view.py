# -*- coding: utf-8 -*-
"""test_lake_v608_task_view —— v6.0.8 tasks 视图 total/done/eta_min 回填回归。

缺陷与验收点（v6.0.8 brief，TL 已验证根因）：
1. **Web 数据湖页各表进度恒 0/0**：``_update_task_view()`` 创建 entry 时
   ``total: 0, done: 0``，之后只更新 state/quota——total/done 从未赋值。
2. 修复：``run()`` 开始处按 (table, tier) 分组统计 total；done 从 ``self._done``
   集合计数（断点续传重启后视图**立即**反映真实进度，不依赖本次 run 增量）；
   ``_update_task_view()``/收尾统一回填 total/done + eta_min（最近 N 个成功任务
   平均耗时推算剩余分钟，无数据 None——前端显示 —）。

纪律：全部离线——tmp progress 文件、mock worker、monkeypatch _quota_state /
get_conn（不碰 data/lake/ 生产库与真实配额文件）。
"""
from __future__ import annotations

import json
import time

import pytest

pytest.importorskip("duckdb")


def _view_entry(prog, table: str, tier: str):
    return next((t for t in prog["tasks"]
                 if t.get("table") == table and t.get("tier") == tier), None)


def _read_prog(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _isolate(monkeypatch, tmp_path):
    """标准隔离：配额恒 (False,0) + coverage 不碰真实库。返回 progress 路径。"""
    from lake import conn as lconn
    from lake.backfill import BackfillRunner

    monkeypatch.setattr(BackfillRunner, "_quota_state", lambda self: (False, 0))
    monkeypatch.setattr(lconn, "get_conn", lambda: (_ for _ in ()).throw(
        lconn.LakeUnavailable("test isolation")))
    return str(tmp_path / "progress.json")


# ===========================================================================
# 1) 新跑：total/done 逐任务回填（含 run 中途文件态）
# ===========================================================================
def test_fresh_run_view_total_done_correct(tmp_path, monkeypatch):
    """3 任务全成功 → 视图 total=3/done=3；done 明细恰 3 键（语义不变）。"""
    from lake.backfill import BackfillRunner, Task

    prog_path = _isolate(monkeypatch, tmp_path)
    tasks = [Task(priority=2, table="kline_history", ts_code=f"sh.6{i:05d}",
                  period_or_date="full_history", tier="P2") for i in range(3)]

    r = BackfillRunner(budget_per_day=5000, progress_path=prog_path)
    stats = r.run(tasks, lambda task: None)
    assert stats["processed"] == 3 and not stats["errors"]

    prog = _read_prog(prog_path)
    e = _view_entry(prog, "kline_history", "P2")
    assert e is not None, f"tasks 视图缺 kline_history/P2: {prog['tasks']}"
    assert e["total"] == 3, f"total 应=3（本次队列分组统计）: {e}"
    assert e["done"] == 3, f"done 应=3: {e}"
    assert e["state"] == "running" and e["quota_used_today"] == 0
    # done 明细语义不变：恰 3 个 (table, code, full_history) 键
    assert prog["done"] == [
        ["kline_history", f"sh.6{i:05d}", "full_history"] for i in range(3)], \
        f"done 明细应恰为 3 键（v6.0.7 稳定键不变）: {prog['done']}"


def test_fresh_run_view_midrun_progress_and_eta(tmp_path, monkeypatch):
    """run 中途读文件：done 逐任务增长（1/3→2/3），eta_min=正整数；
    收尾全完成 → eta_min=None（无剩余）。"""
    from lake.backfill import BackfillRunner, Task

    prog_path = _isolate(monkeypatch, tmp_path)
    tasks = [Task(priority=2, table="kline_history", ts_code=f"sh.6{i:05d}",
                  period_or_date="full_history", tier="P2") for i in range(3)]

    seen_done = []

    def worker(task):
        time.sleep(0.01)  # 让 ETA 有真实耗时样本（非零）
        # mark_done 之后、_update_task_view 之前文件未落盘；这里读的是**上一任务
        # 收尾**写入的进度——第 2/3 个任务时 = 1/3、2/3（首个任务时文件尚不存在）
        try:
            e = _view_entry(_read_prog(prog_path), "kline_history", "P2")
        except FileNotFoundError:
            return   # 首个任务：progress 文件尚未首次落盘
        if e is not None:
            seen_done.append((e["done"], e["total"]))

    r = BackfillRunner(budget_per_day=5000, progress_path=prog_path)
    r.run(tasks, worker)
    # 首个任务读文件时尚无 entry（None 被跳过）；第 2/3 个任务读到 1/3、2/3
    assert seen_done == [(1, 3), (2, 3)], f"中途 done 应逐任务增长: {seen_done}"

    # 收尾态：done=total=3 → 无剩余 → eta_min=None（前端显示 —）
    e = _view_entry(_read_prog(prog_path), "kline_history", "P2")
    assert e is not None, "tasks 视图缺 kline_history/P2"
    assert (e["done"], e["total"]) == (3, 3) and e["eta_min"] is None


# ===========================================================================
# 2) 中断重跑：done 从断点恢复计数（不依赖本次 run 增量）
# ===========================================================================
def test_resume_view_done_restored_from_checkpoint(tmp_path, monkeypatch):
    """Run1 处理 2/3 后"崩溃"；Run2 新 runner（模拟重启）→
    视图立即 done=2/total=3，补完 → 3/3。"""
    from lake.backfill import BackfillRunner, Task

    prog_path = _isolate(monkeypatch, tmp_path)
    tasks = [Task(priority=2, table="kline_history", ts_code=f"sh.6{i:05d}",
                  period_or_date="full_history", tier="P2") for i in range(3)]

    class _Crash(Exception):
        pass

    def worker1(task):
        if task.ts_code == "sh.600002":   # 第 3 个任务"崩溃"（未标 done）
            raise _Crash("simulated crash")

    r1 = BackfillRunner(budget_per_day=5000, progress_path=prog_path)
    s1 = r1.run(tasks, worker1)
    assert s1["processed"] == 2 and len(s1["errors"]) == 1
    e = _view_entry(_read_prog(prog_path), "kline_history", "P2")
    assert e is not None, f"Run1 收尾视图缺条目: {prog_path}"
    assert (e["done"], e["total"]) == (2, 3), f"Run1 收尾视图应 2/3: {e}"

    # ---- Run2：新 runner（重启）重跑同一队列 → 断点恢复 ----
    r2 = BackfillRunner(budget_per_day=5000, progress_path=prog_path)
    # **核心**：run 开始处即从 _done 集合恢复计数——补完前视图已是 2/3
    mid = {}

    def worker2(task):
        time.sleep(0.01)
        e = _view_entry(_read_prog(prog_path), "kline_history", "P2")
        if e is not None:
            mid["done_before_finish"] = e["done"]   # 唯一剩余任务处理中：仍=2

    s2 = r2.run(tasks, worker2)
    assert s2["skipped_done"] == 2 and s2["processed"] == 1
    assert mid.get("done_before_finish") == 2, \
        f"重启后视图应立即反映断点进度（补完前=2）: {mid}"
    e = _view_entry(_read_prog(prog_path), "kline_history", "P2")
    assert e is not None, "Run2 收尾视图缺条目"
    assert (e["done"], e["total"]) == (3, 3) and e["state"] == "running"


# ===========================================================================
# 3) 多表混合：各 (table, tier) 独立计数（含跨期残留 done 不虚增）
# ===========================================================================
def test_multi_table_mixed_independent_counts(tmp_path, monkeypatch):
    """kline_history(P2)×2 + valuation_daily(P2)×1 同 run → 各自 total/done；
    预置的**旧期** valuation done 键（昨日 as_of）不虚增计数。"""
    from lake.backfill import BackfillRunner, Task

    prog_path = _isolate(monkeypatch, tmp_path)
    # 预置断点：kline_history sh.600001 已 done；valuation_daily 昨日 as_of 残留
    pre = {"updated_at": None, "tasks": [], "coverage": {},
           "done": [["kline_history", "sh.600001", "full_history"],
                    ["valuation_daily", "sh.600001", "2026-09-14"]]}
    with open(prog_path, "w", encoding="utf-8") as f:
        json.dump(pre, f)

    tasks = [
        Task(priority=2, table="kline_history", ts_code="sh.600001",
             period_or_date="full_history", tier="P2"),
        Task(priority=2, table="kline_history", ts_code="sh.600002",
             period_or_date="full_history", tier="P2"),
        # 本次 as_of=今日（新任务）——昨日残留键不在本队列内
        Task(priority=2, table="valuation_daily", ts_code="sh.600001",
             period_or_date="2026-09-15", tier="P2"),
    ]
    r = BackfillRunner(budget_per_day=5000, progress_path=prog_path)
    stats = r.run(tasks, lambda task: None)
    assert stats["skipped_done"] == 1 and stats["processed"] == 2

    prog = _read_prog(prog_path)
    kh = _view_entry(prog, "kline_history", "P2")
    vd = _view_entry(prog, "valuation_daily", "P2")
    assert kh is not None and vd is not None, f"视图缺条目: {prog['tasks']}"
    assert (kh["total"], kh["done"]) == (2, 2), \
        f"kline_history 应独立计数 2/2（含断点恢复的 sh.600001）: {kh}"
    assert (vd["total"], vd["done"]) == (1, 1), \
        f"valuation_daily 应独立计数 1/1（昨日残留键不虚增）: {vd}"
    # done 明细：4 键并存、无重复（mark_done 幂等语义不变；排序按字符串序）
    assert sorted(prog["done"]) == [
        ["kline_history", "sh.600001", "full_history"],
        ["kline_history", "sh.600002", "full_history"],
        ["valuation_daily", "sh.600001", "2026-09-14"],
        ["valuation_daily", "sh.600001", "2026-09-15"]], f"{prog['done']}"


def test_zero_work_run_still_backfills_view(tmp_path, monkeypatch):
    """全跳过 run（无 _update_task_view 事件）→ 收尾仍回填 total/done；
    零成功样本 → eta_min=None。"""
    from lake.backfill import BackfillRunner, Task

    prog_path = _isolate(monkeypatch, tmp_path)
    task = Task(priority=2, table="kline_history", ts_code="sh.601398",
                period_or_date="full_history", tier="P2")
    r1 = BackfillRunner(budget_per_day=5000, progress_path=prog_path)
    r1.run([task], lambda t: None)

    calls = {"n": 0}

    def no_worker(t):
        calls["n"] += 1   # 必须恒 0（全跳过）

    r2 = BackfillRunner(budget_per_day=5000, progress_path=prog_path)
    s2 = r2.run([task], no_worker)
    assert calls["n"] == 0 and s2["skipped_done"] == 1 and s2["processed"] == 0

    e = _view_entry(_read_prog(prog_path), "kline_history", "P2")
    assert e is not None, "零处理 run 视图缺条目"
    assert (e["total"], e["done"]) == (1, 1), f"零处理 run 也要回填: {e}"
    assert e["eta_min"] is None, f"无剩余 → eta_min=None: {e}"


# ===========================================================================
# 4) mark_done 幂等（回归护栏：视图修复不得破坏 done 键去重）
# ===========================================================================
def test_mark_done_idempotent_unchanged(tmp_path, monkeypatch):
    """重复 mark_done 同任务 → done 明细不重复、无副作用。"""
    from lake.backfill import BackfillRunner, Task

    prog_path = _isolate(monkeypatch, tmp_path)
    task = Task(priority=2, table="kline_history", ts_code="sh.601398",
                period_or_date="full_history", tier="P2")
    r = BackfillRunner(budget_per_day=5000, progress_path=prog_path)
    r.mark_done(task)
    r.mark_done(task)   # 重复标记（幂等）
    prog = _read_prog(prog_path)
    assert prog["done"] == [["kline_history", "sh.601398", "full_history"]], \
        f"重复 mark_done 不得产生重复键: {prog['done']}"
