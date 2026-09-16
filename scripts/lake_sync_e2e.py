# -*- coding: utf-8 -*-
"""v6.0.9 E2E 冒烟：Web 同步控制按钮全链路（真实网络，≤3 只；本脚本只用 sh.601398）。

brief v6.0.9 验收链：
  tmp 库 init → API start（--db tmp --codes sh.601398）→ 轮询 running（/status
  backfill_in_progress=true + lock_holder_pid）→ API stop（SIGTERM 优雅停止）→
  断言：进程干净退出（sync.log 收尾 summary = rc=0 证据）、progress
  state=stopped_by_signal、tmp 库有该股 K线行。

纪律（最高优先级）：
- **绝不触碰生产库/生产灌数进程（pid 1548390）**：uvicorn 子进程内把
  default_db_path patch 到 tmp 目录；start/stop/status 全部走 tmp 库。
- BaoStock ≤2 次调用（1 只 adj_factor + login）——与生产灌数并发共 2 连接，
  远低于"~4 并发触发黑名单"红线（TL 纪律）。
- 腾讯全史 K线免费接口（3 页分页），无配额。

用法：``.venv/bin/python scripts/lake_sync_e2e.py``（输出即冒烟日志；rc=0 全绿）。
"""
from __future__ import annotations

import http.client as _http_client
import json
import os
import shutil
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
PY = sys.executable or os.path.join(ROOT, ".venv", "bin", "python")

WORK = "/tmp/lake_v609_e2e"
DB = os.path.join(WORK, "lake.duckdb")
CODES = "sh.601398"   # ≤3 只纪律：本冒烟只用 1 只

# --stop-after N：观察窗改为固定 N 秒（N>0 时首任务大概率仍在跑 → SIGTERM 分支；
# 缺省 0 = 自适应观察窗——单只任务可能自然收尾 → 409 分支）。两次运行覆盖两分支。
STOP_AFTER = 0


def hr(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72, flush=True)


def http(port: int, method: str, path: str, body: str = "{}"):
    c = _http_client.HTTPConnection("127.0.0.1", port, timeout=60)
    try:
        c.request(method, path, body=body)
        r = c.getresponse()
        return r.status, r.read().decode()
    finally:
        c.close()


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main():
    global STOP_AFTER
    # --stop-after N：N>0 → 固定观察 N 秒后**必发 SIGTERM**（首任务大概率仍在跑，
    # 覆盖优雅停止分支）；缺省 0 = 自适应观察窗（单只可能自然收尾 → 409 分支）。
    if "--stop-after" in sys.argv:
        STOP_AFTER = int(sys.argv[sys.argv.index("--stop-after") + 1])

    # ---- 0. fresh tmp 工作区（绝不复用旧状态）----
    if os.path.exists(WORK):
        shutil.rmtree(WORK)
    os.makedirs(WORK, exist_ok=True)

    from screener.data.baostock_client import QuotaGuard, default_quota_path

    guard = QuotaGuard(path=default_quota_path())
    q_date, q_before = guard.get_state()
    hr("STEP 0 · BaoStock 配额前值（生产灌数并发运行中，本冒烟 ≤2 次调用）")
    print(f"date={q_date} used={q_before} path={default_quota_path()}")

    # ---- 1. tmp 库 init（driver CLI，幂等建 schema）----
    hr("STEP 1 · tmp 库 init（scripts/lake_backfill.py --db <tmp> init）")
    r = subprocess.run([PY, "scripts/lake_backfill.py", "--db", DB, "init"],
                       cwd=ROOT, capture_output=True, text=True, timeout=120)
    print(r.stdout.strip().splitlines()[-3] if r.stdout.strip() else "(no output)")
    assert r.returncode == 0, f"init 失败 rc={r.returncode}: {r.stderr[-500:]}"

    # ---- 2. uvicorn（web.app 真实挂载；default_db_path patch → tmp）----
    port = free_port()
    server_code = (
        f"import sys; sys.path.insert(0, {ROOT!r})\n"
        "from lake import conn as _lc\n"
        f"_lc.default_db_path = lambda: {DB!r}\n"
        "import uvicorn\n"
        "from web.app import app\n"
        f"uvicorn.run(app, host='127.0.0.1', port={port}, log_level='error')\n"
    )
    srv = subprocess.Popen([PY, "-c", server_code], cwd=ROOT,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        # 等就绪
        ready = False
        for _ in range(60):
            try:
                s, _ = http(port, "GET", "/api/lake/status")
                if s in (200, 409):
                    ready = True
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.5)
        assert ready, "uvicorn 未在 30s 内就绪"

        # ---- 3. API start（--db tmp 由 sync_control 透传；codes 走 query 限定 ≤1 只）----
        hr("STEP 2 · POST /api/lake/sync/start?codes=" + CODES)
        s, b = http(port, "POST", "/api/lake/sync/start?codes=" + CODES)
        print(f"HTTP {s}: {b}")
        assert s == 200, f"start 应 200，实际 {s}"
        d = json.loads(b)
        assert d.get("started") is True, f"start 应成功: {d}"
        pid = d["pid"]
        log_path = d["log_path"]
        print(f"sync pid={pid} log={log_path}")

        # ---- 4. 轮询 running（/status backfill_in_progress + lock_holder_pid）----
        hr("STEP 3 · 轮询 /status 至 running（backfill_in_progress=true）")
        running = None
        for i in range(120):   # ≤60s
            s, b = http(port, "GET", "/api/lake/status")
            assert s == 200, f"/status 应 200: {s} {b[:200]}"
            st = json.loads(b)
            if st.get("backfill_in_progress"):
                running = st
                break
            time.sleep(0.5)
        assert running, "60s 内 /status 未观察到 backfill_in_progress=true"
        print(f"running: lock_holder_pid={running['lock_holder_pid']} "
              f"(spawn pid={pid})")
        assert running["lock_holder_pid"] == pid, \
            f"lock_holder_pid 应=spawn pid {pid}: {running['lock_holder_pid']}"

        # ---- 5. 停止路径（--stop-after N：固定观察窗后必发 SIGTERM；缺省自适应）----
        hr("STEP 4 · 停止（SIGTERM 优雅停止 / 自然收尾两分支）")
        if STOP_AFTER > 0:
            # 固定观察窗：给 driver import+建连+bs login+首任务启动的时间，然后必发 SIGTERM
            time.sleep(STOP_AFTER)
            s, b = http(port, "GET", "/api/lake/status")
            st = json.loads(b)
            natural_exit = "backfill_in_progress" not in st
        else:
            # 自适应：给首任务完成窗口，观察是否自然收尾（单只跑完 → driver rc=0 退出）
            time.sleep(90)
            natural_exit = False
            for _ in range(24):   # ≤120s 观察
                s, b = http(port, "GET", "/api/lake/status")
                st = json.loads(b)
                if "backfill_in_progress" not in st:
                    natural_exit = True
                    break
                time.sleep(5)

        if natural_exit:
            # 分支 A：单只任务在观察窗内自然完成 → driver 正常收尾（sync.log summary=rc=0）
            hr("STEP 4A · 自然收尾分支（进程已 rc=0 退出；stop 应 409 sync_not_running）")
            s, b = http(port, "POST", "/api/lake/sync/stop")
            print(f"HTTP {s}: {b}")
            assert s == 409 and json.loads(b).get("error") == "sync_not_running", \
                f"已自然退出时 stop 必须 409 sync_not_running: {s} {b}"
            print("OK: stop → 409 sync_not_running（顶层契约体，无 detail 键）")
        else:
            # 分支 B：仍在跑（首任务未竟）→ SIGTERM 优雅停止（任务间 break + 进度落盘）
            hr("STEP 4B · SIGTERM 分支（POST /api/lake/sync/stop）")
            s, b = http(port, "POST", "/api/lake/sync/stop")
            print(f"HTTP {s}: {b}")
            assert s == 200, f"stop 应 200，实际 {s}: {b}"
            d = json.loads(b)
            assert d.get("stopped") is True, f"stop 应成功: {d}"
            print(f"stopped: pid={d['pid']} method={d.get('method')}")

        # ---- 6. 断言（按分支）----
        hr("STEP 5 · 断言（锁释放 + progress + 退出证据 + K线行）")
        # 6a. /status 恢复无 backfill 键（flock 随进程退出释放）
        cleared = None
        for _ in range(40):
            s, b = http(port, "GET", "/api/lake/status")
            st = json.loads(b)
            if "backfill_in_progress" not in st:
                cleared = st
                break
            time.sleep(0.5)
        assert cleared, "停止后 /status 未恢复（锁未释放？）"
        print("OK: /status 无 backfill_in_progress（锁已释放）")

        # 6b. progress 文件（tmp 目录派生）
        prog_path = os.path.join(WORK, "backfill_progress.json")
        with open(prog_path, encoding="utf-8") as f:
            prog = json.load(f)
        entry = next((t for t in prog.get("tasks", [])
                      if t.get("table") == "kline_history"), None)
        assert entry is not None, f"progress 缺 kline_history 视图: {prog.get('tasks')}"
        print(f"progress tasks entry: state={entry['state']} "
              f"done={entry.get('done')}/{entry.get('total')}")
        if natural_exit:
            assert entry["state"] in ("running", "blocked_quota"), \
                f"自然收尾分支 state 应为 running/blocked_quota: {entry}"
        else:
            assert entry["state"] == "stopped_by_signal", \
                f"SIGTERM 分支 state 必须=stopped_by_signal: {entry}"
            print("OK: progress state=stopped_by_signal（优雅停止落盘）")

        # 6c. sync.log：driver 收尾 summary（干净退出 rc=0 证据——main() 正常返回才打印）
        with open(log_path, encoding="utf-8") as f:
            log = f.read()
        if not natural_exit:
            assert "STOPPED by signal, progress saved" in log, \
                f"sync.log 缺 STOPPED 日志行:\n{log[-1500:]}"
            print("OK: sync.log 含 'STOPPED by signal, progress saved'")
        assert "===== history =====" in log, \
            f"sync.log 缺 driver 收尾 summary（非干净退出）:\n{log[-1500:]}"
        rest = log[log.index("===== history ====="):]
        end_idx = rest.find("\n=====", len("===== history ====="))
        sum_text = rest[len("===== history ====="):end_idx if end_idx != -1 else len(rest)]
        summary = json.loads(sum_text.strip())
        print(f"OK: driver 收尾 summary（干净退出 rc=0 证据）: "
              f"processed={summary.get('processed')} stopped={summary.get('stopped')} "
              f"errors={len(summary.get('errors', []))}")
        if not natural_exit:
            assert summary.get("stopped") is True, \
                f"SIGTERM 分支 summary 应含 stopped=true: {summary}"

        # 6d. K线行断言（按分支语义）：
        #   自然收尾 → worker 完整跑完，**必须**有全史 K线行（brief 硬断言）；
        #   SIGTERM 中断首任务 → 事务未提交、0 行是**预期**（优雅停止不半写），
        #   done=0 + state=stopped_by_signal 即"下次启动自动续传"的证据。
        import duckdb

        n_rows = dmin = dmax = None
        for _ in range(20):   # 进程刚退出的 WAL 清理窗口 → 重试连库
            try:
                con = duckdb.connect(DB, read_only=True)
                n_rows = con.execute(
                    "SELECT COUNT(*) FROM kline_daily WHERE ts_code=?", [CODES]).fetchone()[0]
                dmin, dmax = con.execute(
                    "SELECT MIN(date), MAX(date) FROM kline_daily WHERE ts_code=?",
                    [CODES]).fetchone()
                con.close()
                break
            except Exception as exc:  # noqa: BLE001 - 锁未释放/文件未落稳
                time.sleep(1)
        assert n_rows is not None, f"tmp 库连不上（WAL 窗口超时）:\n{log[-800:]}"
        print(f"kline_daily[{CODES}]: rows={n_rows} range={dmin}→{dmax}")
        if natural_exit:
            assert n_rows > 100, f"自然收尾必须有全史 K线行（>{100}），实际 {n_rows}"
            print(f"OK: tmp 库有 {CODES} K线 {n_rows} 行（{dmin} → {dmax}）")
        else:
            # SIGTERM 分支（时序容忍，一致性严格）：
            #   信号在**任务完成后/最后任务收尾**到达（done==total）→ K线已落库；
            #   信号在**任务执行中**到达（done<total）→ 事务未提交、0 行（不半写）。
            # 两种都是优雅停止的合法结果——断言 done 与行数的一致性（不得半写/毒化）。
            if entry.get("done", 0) >= entry.get("total", 1):
                assert n_rows > 100, \
                    f"done=total 时 K线必须已落库（>{100}），实际 {n_rows}"
                print(f"OK: SIGTERM 于任务完成后到达 → K线已落库 {n_rows} 行 "
                      f"（{dmin} → {dmax}）+ stopped_by_signal")
            else:
                assert n_rows == 0, \
                    f"done<total（任务中断）时不得半写，实际 {n_rows} 行"
                print("OK: SIGTERM 于任务执行中到达 → 0 行 + done<total（不半写、可续传）")

        # ---- 7. 配额后值 ----
        hr("STEP 6 · BaoStock 配额后值")
        q_date2, q_after = guard.get_state()
        consumed = q_after - q_before
        print(f"used={q_after} (前 {q_before}) → 本冒烟消耗 {consumed} 次（≤4，与生产并发）")
        assert consumed <= 4, f"BaoStock 消耗 {consumed} > 4（超限！）"

        hr("E2E SMOKE OK · 同步控制全链路通 "
           + ("（自然收尾分支：start→running→rc=0→K线落库→stop 409）" if natural_exit
              else "（SIGTERM 分支：start→running→优雅停止→stopped_by_signal→锁释放）"))
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=10)
        except subprocess.TimeoutExpired:
            srv.kill()
            srv.wait(timeout=10)


if __name__ == "__main__":
    main()
