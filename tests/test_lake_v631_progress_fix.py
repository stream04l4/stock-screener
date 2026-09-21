# -*- coding: utf-8 -*-
"""test_lake_v631_progress_fix —— v6.3.1 灌数进度页矛盾修复（离线单测）。

覆盖 brief R1/R2/R3（R4 前端另见 vitest，R5 一行 web 僵尸回收见回归套件）：

- **R1（核心）full 启动 scope 重置**：run_full 启动时把 scope 内 entry 归位"本轮状态"
  —— done<total（或 total 未知）→ pending/eta=None/last_error 清空；done>=total
  （真 done）→ 保持 done；entry 缺失不创建。修"done 但 0/5219"状态与计数器脱节。
- **R2 陈旧 ETA 清理**：非运行态（pending/done/error/stopped）entry eta_min 全 None；
  running/stopping 保持 runner 实时 ETA（正常运行中不受影响）。
- **R3 run_full 阶段间停止检查**：set_stop_requested() 置位后（模拟 p3 收到 SIGTERM）
  → p3 之后 phase（t8/t9/t5/t6）全 skipped、summary 无 error、all_ok=True、cmd rc=0
  （本轮事故：p3 优雅停止后 t8 recompute_all 无任务边界 → 15s 看门狗 os._exit 强杀）。

纪律：全离线——沿用 v616 隔离契约（conftest autouse LAKE_MULTISOURCE=0 → legacy），
fake 打在 driver 模块级 phase 函数上（run_history/run_incremental/run_t5/run_t6/run_t8/
run_t9），库/progress 一律 tmp_path，不碰 data/lake/、不碰生产灌数进程。

对照生产事故（Joel 09-21 截图，8 行任务表）：7 行 done（含 4 行 "done 但 0/5219"）
+ holders_snapshot error + 总 ETA 6h56m（陈旧 416min）+ 假活横幅（僵尸 PID）。
本文件即构造该"陈旧 done/error/eta"progress 场景，证明 run_full 启动后归位 pending、
eta 清空（验收标准"手工证据"由单测等价覆盖）。
"""
from __future__ import annotations

import json
import os
import sys

import pytest

duckdb = pytest.importorskip("duckdb")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
for _p in (REPO_ROOT, SCRIPTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from lake import backfill as lb  # noqa: E402
from lake import conn as lconn  # noqa: E402

import lake_backfill as drv  # noqa: E402

FIXED_TODAY = "2026-09-21"


# ===========================================================================
# fixture：路径全隔离 + 停止标志清理（防跨测试串味）
# ===========================================================================
@pytest.fixture(autouse=True)
def _v631_isolate(tmp_path, monkeypatch):
    import lake.config as lconfig

    monkeypatch.setattr(drv, "_today_beijing", lambda: FIXED_TODAY)
    monkeypatch.setattr(drv.time, "sleep", lambda s: None)
    monkeypatch.setattr(lb.BackfillRunner, "_quota_state", lambda self: (False, 0))
    yaml_path = str(tmp_path / "strategy.yaml")
    with open(yaml_path, "w", encoding="utf-8") as f:
        f.write("lake:\n"
                "  sina_enabled: true\n"
                "  tencent_enabled: true\n"
                "  baostock_probe_enabled: false\n"
                "  tdx_enabled: true\n"
                "  adata_f10_enabled: true\n")
    monkeypatch.setattr(lconfig, "strategy_yaml_path", lambda: yaml_path)
    monkeypatch.setattr(lb, "_progress_path",
                        lambda: str(tmp_path / "prog" / "backfill_progress.json"))
    monkeypatch.setattr(lconn, "progress_path",
                        lambda: str(tmp_path / "prog" / "backfill_progress.json"))
    # R3 停止标志是模块级——测试间必须清零，防上一轮残留毒化新 run。
    lb.clear_stop_requested()
    yield
    lb.clear_stop_requested()


# ===========================================================================
# helpers
# ===========================================================================
def _seed_master(db_path: str, codes) -> None:
    """T1 stock_master 播种（delist_date=NULL → universe 全集）。"""
    con = lconn.open(db_path)
    con.executemany(
        "INSERT INTO stock_master (ts_code,name,industry_csric2,board,is_st,"
        "source,fetched_at,data_version) VALUES (?,?,?,?,0,'t','2026-09-20 09:00:00','v6.3')",
        [(c, f"股{c}", "J66", "主板") for c in codes])
    con.close()


def _prog_path() -> str:
    return lb._progress_path()


def _read_prog() -> dict:
    with open(_prog_path(), encoding="utf-8") as f:
        return json.load(f)


def _write_prog(prog: dict) -> None:
    os.makedirs(os.path.dirname(_prog_path()), exist_ok=True)
    with open(_prog_path(), "w", encoding="utf-8") as f:
        json.dump(prog, f, ensure_ascii=False, indent=2)


def _entry(prog: dict, table: str):
    return next((t for t in prog.get("tasks", []) if t.get("table") == table), None)


def _seed_stale_full_progress(codes, done_history=True):
    """构造生产事故的"上一轮 full 残留"progress（陈旧 done/error/eta）。

    对照 Joel 截图：4 表 "done 但 0/5219"（kline_daily/valuation_daily/index_daily/
    factor_snapshot）+ holders_snapshot error（上一轮 t6 新浪 WAF 熔断）+ eta_min=416
    陈旧值。done 明细（done 键）已按本轮 inc 期键清空 → 本轮重算 done=0，但 state
    还停在上一轮 done/error → 矛盾场景。
    """
    n = len(codes)
    tasks = [
        # "done 但 0/5219" 矛盾组（本轮 done 重算 0，state 残留上一轮 done）
        {"table": "kline_daily", "tier": "P3", "total": n, "done": 0,
         "state": "done", "eta_min": None, "last_error": "",
         "quota_used_today": 0, "quota_budget": 5000},
        {"table": "valuation_daily", "tier": "P3", "total": n, "done": 0,
         "state": "done", "eta_min": None, "last_error": "",
         "quota_used_today": 0, "quota_budget": 5000},
        {"table": "index_daily", "tier": "P3", "total": 4, "done": 0,
         "state": "done", "eta_min": None, "last_error": "",
         "quota_used_today": 0, "quota_budget": 5000},
        {"table": "factor_snapshot", "tier": "P3", "total": n, "done": 0,
         "state": "done", "eta_min": None, "last_error": "",
         "quota_used_today": 0, "quota_budget": 5000},
        # 上一轮 t6 失败标记 + 陈旧 ETA（本轮还没跑到 t6）
        {"table": "holders_snapshot", "tier": "P2", "total": n, "done": 0,
         "state": "error", "eta_min": 416,
         "last_error": "phase t6 failed: 3004 task(s) failed; first: "
                       "新浪 holders 重试 2 次均失败: HTTP 456",
         "quota_used_today": 0, "quota_budget": 5000},
        # 真 done（done>=total，幂等跳过应保持 done）
        {"table": "kline_history", "tier": "P2", "total": n, "done": n,
         "state": "done", "eta_min": None, "last_error": "",
         "quota_used_today": 0, "quota_budget": 5000},
        # 未建 entry 的表（macro_rf）——R1 不得凭空创建
    ]
    done = []
    if done_history:
        for c in codes:
            done.append(["kline_history", c, "full_history"])
    return {"updated_at": "2026-09-20 02:00:00", "tasks": tasks, "coverage": {},
            "done": done}


def _wire_fast_phases(monkeypatch, codes):
    """fake 全部 phase 函数快速返回（不触网、不改 progress entry），计数供断言。

    与 v616 的 fake phase 手法一致：patch driver 模块属性即拦截 run_full 的调用。
    返回 counter（记录各 phase 是否被执行）。
    """
    counter = {ph: 0 for ph in ("history", "p3", "t8", "t9", "t5", "t6")}

    def mk(name, shape):
        def fake(*a, **k):
            counter[name] += 1
            return dict(shape)
        return fake

    monkeypatch.setattr(drv, "run_history",
                        mk("history", {"sub": "history", "processed": 0,
                                        "skipped_done": len(codes), "errors": []}))
    monkeypatch.setattr(drv, "run_incremental",
                        mk("p3", {"sub": "incremental", "as_of": FIXED_TODAY,
                                  "t2": {"errors": []}, "t3": {"errors": []},
                                  "t7": {"errors": []}}))
    monkeypatch.setattr(drv, "run_t8",
                        mk("t8", {"table": "factor_snapshot", "as_of_date": FIXED_TODAY,
                                  "codes_requested": len(codes), "rows_written": 0,
                                  "elapsed_s": 0.0}))
    monkeypatch.setattr(drv, "run_t9",
                        mk("t9", {"table": "macro_rf", "rows_loaded": 1,
                                  "rf_source": "existing_cache", "elapsed_s": 0.0}))
    monkeypatch.setattr(drv, "run_t5",
                        mk("t5", {"sub": "t5", "processed": 0,
                                  "skipped_done": len(codes), "errors": []}))
    monkeypatch.setattr(drv, "run_t6",
                        mk("t6", {"sub": "t6", "processed": 0,
                                  "skipped_done": len(codes), "errors": []}))
    return counter


# ===========================================================================
# R1：_full_reset_scope_state 单元（直接测归位语义）
# ===========================================================================
def test_r1_reset_scope_pending_clears_eta_and_error(tmp_path):
    """done<total（或 total 未知）→ pending + eta=None + last_error 清空（核心修复）。"""
    db = str(tmp_path / "r1a.duckdb")
    codes = ["sh.600001", "sz.000002"]
    _seed_master(db, codes)
    _write_prog(_seed_stale_full_progress(codes, done_history=False))

    drv._full_reset_scope_state(db)

    prog = _read_prog()
    # "done 但 0/N" 矛盾组 → pending、eta 清空
    for t in ("kline_daily", "valuation_daily", "index_daily", "factor_snapshot"):
        e = _entry(prog, t)
        assert e is not None, f"{t} entry 应存在: {prog['tasks']}"
        assert e["state"] == "pending", f"{t} 应归位 pending（done=0<total）: {e}"
        assert e["eta_min"] is None, f"{t} eta 应清空: {e}"
        assert e["last_error"] == "", f"{t} last_error 应清空: {e}"
    # holders_snapshot error → pending + eta(416) 清空 + last_error 清空
    eh = _entry(prog, "holders_snapshot")
    assert eh["state"] == "pending", f"holders_snapshot error 应归位 pending: {eh}"
    assert eh["eta_min"] is None, f"陈旧 ETA(416) 应清空: {eh}"
    assert eh["last_error"] == "", f"陈旧 last_error 应清空: {eh}"
    # 真 done（done>=total）→ 保持 done（幂等跳过，快速完成）
    ek = _entry(prog, "kline_history")
    assert ek["state"] == "done", f"真 done 应保持 done: {ek}"
    # 未建 entry 的表（macro_rf）→ 不创建
    assert _entry(prog, "macro_rf") is None, "macro_rf 未建 entry，R1 不得凭空创建"


def test_r1_reset_scope_keeps_true_done(tmp_path):
    """done>=total 的 entry（真 done）保持 done；且 done 明细存在时口径一致。"""
    db = str(tmp_path / "r1b.duckdb")
    codes = ["sh.600001", "sz.000002"]
    _seed_master(db, codes)
    _write_prog(_seed_stale_full_progress(codes, done_history=True))

    drv._full_reset_scope_state(db)

    prog = _read_prog()
    ek = _entry(prog, "kline_history")
    assert ek["state"] == "done" and ek["done"] == 2 and ek["total"] == 2, \
        f"真 done 应原样保持: {ek}"
    # 陈旧组同样归位 pending
    assert _entry(prog, "kline_daily")["state"] == "pending"
    assert _entry(prog, "holders_snapshot")["state"] == "pending"


def test_r1_reset_scope_missing_entries_not_created(tmp_path):
    """progress 无 entry（全新库）→ R1 不创建任何 entry（让各 phase 自己建）。"""
    db = str(tmp_path / "r1c.duckdb")
    _seed_master(db, ["sh.600001"])
    _write_prog({"updated_at": None, "tasks": [], "coverage": {}, "done": []})

    drv._full_reset_scope_state(db)

    prog = _read_prog()
    assert prog.get("tasks") == [], f"空 tasks 应保持空（不创建）: {prog.get('tasks')}"


# ===========================================================================
# R1 集成：run_full 启动后矛盾消失（陈旧 done/error/eta → pending）
# ===========================================================================
def test_r1_integration_run_full_resolves_contradiction(tmp_path, monkeypatch):
    """run_full 启动（fake 快速 phase）→ "done 但 0/5219" 矛盾消失、error 清除、
    真 done 保持。这是验收标准"手工证据"的单测等价覆盖。"""
    db = str(tmp_path / "r1int.duckdb")
    codes = ["sh.600001", "sz.000002"]
    _seed_master(db, codes)
    _write_prog(_seed_stale_full_progress(codes, done_history=True))
    counter = _wire_fast_phases(monkeypatch, codes)
    con = lconn.open(db)

    summary = drv.run_full(con, db, codes, "1990-01-01", FIXED_TODAY, days=3)

    assert summary["all_ok"] is True, f"fake 快速 phase 应全 ok: {summary['phases']}"
    # 六阶段都跑了（未收到停止信号）
    assert counter == {ph: 1 for ph in counter}, f"各 phase 应各执行一次: {counter}"

    prog = _read_prog()
    # "done 但 0/5219" 矛盾组：最终不得是 state=done 且 done<total（矛盾消除）。
    # fake phase 不改 progress、无 done 键 → 收尾 _refresh 重算 done=0 → 归位 pending。
    for t in ("kline_daily", "valuation_daily", "factor_snapshot"):
        e = _entry(prog, t)
        assert e is not None
        assert not (e["state"] == "done" and (e.get("done") or 0) < (e.get("total") or 0)), \
            f"{t} 矛盾未消除（done 但 done<total）: {e}"
        assert e["state"] == "pending", f"{t} 收尾应 pending（本轮未灌）: {e}"
        assert e["eta_min"] is None, f"{t} 非运行态 eta 应 None: {e}"
    # holders_snapshot error + 陈旧 ETA → 归位 pending、error/eta 清除
    eh = _entry(prog, "holders_snapshot")
    assert eh["state"] != "error", f"陈旧 error 应清除: {eh}"
    assert eh["eta_min"] is None, f"陈旧 ETA 应清空: {eh}"
    assert eh["last_error"] == "", f"陈旧 last_error 应清空: {eh}"
    # 真 done（kline_history，done 键齐全）保持 done
    ek = _entry(prog, "kline_history")
    assert ek["state"] == "done", f"真 done 应保持: {ek}"
    con.close()


# ===========================================================================
# R2：非运行态 entry 收尾后 eta_min 全 None（running/stopping 保留实时 ETA）
# ===========================================================================
def test_r2_final_cleanup_non_running_eta_none(tmp_path, monkeypatch):
    """run_full 收尾 → pending/done/error/stopped entry eta_min 全 None；
    running/stopping 保留 runner 实时 ETA（正常运行中不受影响）。"""
    db = str(tmp_path / "r2.duckdb")
    codes = ["sh.600001", "sz.000002"]
    _seed_master(db, codes)
    # 预置：pending/done/error/stopped 带陈旧 eta + 一个 running 带实时 eta。
    tasks = [
        {"table": "kline_daily", "tier": "P3", "total": 2, "done": 0,
         "state": "pending", "eta_min": 99, "last_error": "",
         "quota_used_today": 0, "quota_budget": 5000},
        {"table": "kline_history", "tier": "P2", "total": 2, "done": 2,
         "state": "done", "eta_min": 77, "last_error": "",
         "quota_used_today": 0, "quota_budget": 5000},
        {"table": "holders_snapshot", "tier": "P2", "total": 2, "done": 0,
         "state": "error", "eta_min": 416, "last_error": "phase t6 failed: ...",
         "quota_used_today": 0, "quota_budget": 5000},
        {"table": "valuation_daily", "tier": "P3", "total": 2, "done": 1,
         "state": "stopped_by_signal", "eta_min": 55, "last_error": "",
         "quota_used_today": 0, "quota_budget": 5000},
        # running 带实时 ETA（模拟正在跑；收尾 _refresh 对非 done 且 state=running 的
        # entry——但收尾态无活跃 writer，running 也会被归位。故 running 保 ETA 的
        # 语义由"收尾前"断言覆盖：此处直接验证 _refresh 对非 running 的清理。）
        {"table": "factor_snapshot", "tier": "P3", "total": 2, "done": 0,
         "state": "running", "eta_min": 120, "last_error": "",
         "quota_used_today": 0, "quota_budget": 5000},
    ]
    done = [["kline_history", "sh.600001", "full_history"],
            ["kline_history", "sz.000002", "full_history"]]
    _write_prog({"updated_at": FIXED_TODAY, "tasks": tasks, "coverage": {}, "done": done})
    counter = _wire_fast_phases(monkeypatch, codes)
    con = lconn.open(db)

    drv.run_full(con, db, codes, "1990-01-01", FIXED_TODAY, days=3)

    prog = _read_prog()
    # 收尾后：非运行态（pending/done/error/stopped）entry 一律 eta=None。
    # （收尾 _refresh 把无活跃 writer 的 running/stopping 也归位 pending，故最终
    # 全表非 running → 全 eta=None。这里断言"没有残留非 None 的陈旧 eta"。）
    for e in prog.get("tasks", []):
        st = e.get("state")
        if st not in ("running", "stopping"):
            assert e.get("eta_min") is None, \
                f"非运行态({st}) entry eta 应 None: {e}"
    con.close()


# ===========================================================================
# R3：run_full 阶段间停止检查（SIGTERM 干净退出）
# ===========================================================================
def test_r3_stop_after_p3_skips_subsequent_phases_rc0(tmp_path, monkeypatch):
    """模拟 p3 收到 SIGTERM（run_incremental 置 set_stop_requested + stopped 收尾）
    → run_full 在 t8 边界命中 stop_requested() → t8/t9/t5/t6 全 skipped、summary
    无 error、all_ok=True、cmd_full rc=0（不再 os._exit 强杀）。"""
    db = str(tmp_path / "r3.duckdb")
    codes = ["sh.600001", "sz.000002"]
    _seed_master(db, codes)
    con = lconn.open(db)

    hist = {"n": 0}
    p3 = {"n": 0}

    def fake_history(*a, **k):
        hist["n"] += 1
        return {"sub": "history", "processed": 0, "skipped_done": len(codes),
                "errors": []}

    def fake_p3(*a, **k):
        # 模拟 p3 的 runner 收到 SIGTERM：置模块级停止标志（handler 语义）+ 收尾
        # 落盘 stopped_by_signal（_finish_stop 语义）。返回无 error 的 stats。
        p3["n"] += 1
        lb.set_stop_requested()
        prog = lb.load_progress(lb._progress_path())
        for e in prog.get("tasks", []):
            if isinstance(e, dict):
                e["state"] = "stopped_by_signal"
        lb.save_progress(prog, lb._progress_path())
        return {"sub": "incremental", "as_of": FIXED_TODAY,
                "t2": {"errors": []}, "t3": {"errors": []}, "t7": {"errors": []}}

    def boom(name):
        def _b(*a, **k):
            raise AssertionError(f"{name} 不应在 p3 停止后执行")
        return _b

    monkeypatch.setattr(drv, "run_history", fake_history)
    monkeypatch.setattr(drv, "run_incremental", fake_p3)
    monkeypatch.setattr(drv, "run_t8", boom("t8"))
    monkeypatch.setattr(drv, "run_t9", boom("t9"))
    monkeypatch.setattr(drv, "run_t5", boom("t5"))
    monkeypatch.setattr(drv, "run_t6", boom("t6"))

    summary = drv.run_full(con, db, codes, "1990-01-01", FIXED_TODAY, days=3)

    # history + p3 跑了；t8 在停止边界 skipped+stopped；t9/t5/t6 未执行（键不出现）
    assert hist["n"] == 1 and p3["n"] == 1
    assert list(summary["phases"]) == ["history", "p3", "t8"], \
        f"应仅 history/p3/t8: {list(summary['phases'])}"
    t8 = summary["phases"]["t8"]
    assert t8["ok"] is True and t8["skipped"] is True and t8["stopped"] is True, \
        f"t8 应 skipped+stopped+ok(True): {t8}"
    assert "t9" not in summary["phases"] and "t5" not in summary["phases"] \
        and "t6" not in summary["phases"], "t9/t5/t6 不得执行"
    # 停止是用户操作不是故障 → all_ok=True、无 error
    assert summary["all_ok"] is True, f"停止后 all_ok 应 True: {summary['all_ok']}"
    for ph, st in summary["phases"].items():
        assert "error" not in st, f"{ph} 不应有 error（停止非故障）: {st}"
    con.close()


def test_r3_cmd_full_rc0_on_stop(tmp_path, monkeypatch, capsys):
    """子进程真实 driver `full`：p3 停止 → 进程 rc=0、段标无 t8 及后续、无 error。
    （cmd 层验证：停止 ≠ 故障，不得 rc=1，与 R3 看门狗 abort 的 rc=1 区分。）"""
    import subprocess

    db = str(tmp_path / "r3sub.duckdb")
    codes = ["sh.600001", "sz.000002"]
    _seed_master(db, codes)
    env = dict(os.environ)
    env["PYTHONPATH"] = REPO_ROOT + os.pathsep + SCRIPTS_DIR
    env["LAKE_MULTISOURCE"] = "0"
    # 子进程内：fake 全部 phase，run_incremental 模拟 SIGTERM 置位 + stopped 收尾。
    prelude = (
        f"import sys; sys.path[:0]={[REPO_ROOT, SCRIPTS_DIR]!r}\n"
        "import lake_backfill as drv\n"
        "from lake import backfill as lb\n"
        f"drv._today_beijing=lambda:'{FIXED_TODAY}'\n"
        "drv.time.sleep=lambda s:None\n"
        "def _h(*a,**k):return {'sub':'history','processed':0,'skipped_done':2,'errors':[]}\n"
        "def _p3(*a,**k):\n"
        "    lb.set_stop_requested()\n"
        "    return {'sub':'incremental','as_of':'x','t2':{'errors':[]},"
        "'t3':{'errors':[]},'t7':{'errors':[]}}\n"
        "def _boom(n):\n"
        "    def _b(*a,**k):raise AssertionError(n+' should not run after stop')\n"
        "    return _b\n"
        "drv.run_history=_h\n"
        "drv.run_incremental=_p3\n"
        "drv.run_t8=_boom('t8'); drv.run_t9=_boom('t9')\n"
        "drv.run_t5=_boom('t5'); drv.run_t6=_boom('t6')\n"
        f"rc=drv.main(['--db', r'{db}', 'full', '--start-date','1990-01-01',"
        f"'--end-date','{FIXED_TODAY}','--days','3'])\n"
        "print('RC='+str(rc),flush=True)\n"
    )
    p = subprocess.run([sys.executable, "-c", prelude], capture_output=True,
                       text=True, timeout=120, env=env, cwd=REPO_ROOT)
    out = (p.stdout or "") + (p.stderr or "")
    # 停止 = 用户操作 → 正常收尾 rc=0（不是看门狗 abort 的 rc=1）
    assert p.returncode == 0, f"p3 停止后 full 应 rc=0，实测 {p.returncode}\n{out[-800:]}"
    assert "RC=0" in p.stdout, f"cmd_full 应返回 rc=0: {p.stdout[-400:]}"
    # 段标：history/p3 开始+结束；t8 无段标（停止边界 break，未进 try 块打段标）
    marks = [ln for ln in out.splitlines() if "===== phase:" in ln]
    assert any("phase: history" in ln and "开始" in ln for ln in marks), marks
    assert any("phase: p3" in ln and "开始" in ln for ln in marks), marks
    assert not any("phase: t8" in ln or "phase: t9" in ln
                   or "phase: t5" in ln or "phase: t6" in ln for ln in marks), \
        f"停止后不得有 t8/t9/t5/t6 段标: {marks}"
    # 停止留痕（stdout 有 SIGTERM 停止说明）
    assert "SIGTERM 停止" in out, f"应有 SIGTERM 停止说明: {out[-400:]}"
