# -*- coding: utf-8 -*-
"""test_lake_v603_bugfixes —— v6.0.3 灌数前 bug 修复批次回归（全离线，零网络/零真实数据源）。

缺陷与验收点（v6.0.3 brief）：
- **B-1**（N-2）库文件缺失时行为不一致：缺库/空库 → 数据端点一致 409 lake_not_initialized
  （顶层 error/hint 契约体），/status 如实报 initialized=false + coverage 全零。
- **B-2** _refresh_coverage 走错连接 + progress 路径硬编码：BackfillRunner(db_path=tmp)
  → _refresh_coverage 读 tmp 库（非 get_conn 生产单例）；progress 与 db_path 同目录派生。
- **B-3** p0 重跑 T7 四指数无条件重取：T7 加 done 键，同参二次 run 幂等跳过（fake client
  计数断言零重复网络调用），--force 强制重取。
- **B-4** LakeLock 未接线进 driver 写路径：main() 写命令（init/p0/history）整段持 LakeLock；
  两进程并发写不冲突（tmp 库 + subprocess 最小验证）。

纪律：全部离线——腾讯/BaoStock 均 monkeypatch fake（计数断言零真实网络），库一律 tmp_path，
不碰 data/lake/lake.duckdb。缺省库行为逐字节兼容（现有 473 用例零回归另由全量 pytest 保证）。
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import time

import pytest

duckdb = pytest.importorskip("duckdb")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)
PY = sys.executable or os.path.join(REPO_ROOT, ".venv", "bin", "python")


# ===========================================================================
# B-1：缺库/空库行为一致（数据端点 409 / status initialized=false）
# ===========================================================================
def _seed_db(db_path: str, with_data: bool = True):
    """建 schema（+可选播种 stock_master 一行）。返回 None。"""
    from lake import conn as lconn

    con = lconn.open(db_path)
    if with_data:
        con.execute(
            "INSERT INTO stock_master (ts_code,name,industry_csric2,board,is_st,"
            "source,fetched_at,data_version) VALUES "
            "('sh.601398','工商银行','J66','主板',0,'t','2026-09-14 00:00:00','v6.0')")
    con.close()


def test_b1_missing_db_data_endpoint_raises_409(tmp_path, monkeypatch):
    """缺库文件：数据端点 _con() 抛 LakeNotInitialized(409)，且**不自动创建空库**。"""
    import lake.web_api as wapi
    from lake import conn as lconn

    missing = str(tmp_path / "nope" / "lake.duckdb")
    monkeypatch.setattr(lconn, "default_db_path", lambda: missing)
    assert not os.path.exists(missing)
    with pytest.raises(wapi.LakeNotInitialized) as ei:
        wapi._con()
    assert ei.value.status_code == 409
    # 关键：_con 不得自动创建空库文件（旧行为由此产生 status 200 / 数据端点 500 矛盾）
    assert not os.path.exists(missing), "缺库时 _con 不应 connect（会 auto-create 空库）"


def test_b1_missing_db_all_data_endpoints_consistent(tmp_path, monkeypatch):
    """缺库：search/industries/market/stock 全部一致抛 LakeNotInitialized(409)（不再 500）。"""
    import lake.web_api as wapi
    from lake import conn as lconn

    missing = str(tmp_path / "missing.duckdb")
    monkeypatch.setattr(lconn, "default_db_path", lambda: missing)
    for fn in (wapi.search, wapi.industries, wapi.market):
        with pytest.raises(wapi.LakeNotInitialized) as ei:
            fn()
        assert ei.value.status_code == 409
    # stock_detail：非法代码先 400；合法代码 → _con 抛 409（LakeNotInitialized）
    with pytest.raises(wapi.LakeNotInitialized):
        wapi.stock_detail("sh.601398")


def test_b1_empty_db_file_data_endpoint_409(tmp_path, monkeypatch):
    """空库文件（存在但无 core 表 stock_master）：数据端点同样 409。"""
    import lake.web_api as wapi
    from lake import conn as lconn

    empty = str(tmp_path / "empty.duckdb")
    # duckdb.connect 建一个**未 init_schema** 的库（文件存在、0 表）→ 模拟"空/未初始化库"
    con = duckdb.connect(empty)
    con.close()
    monkeypatch.setattr(lconn, "default_db_path", lambda: empty)
    with pytest.raises(wapi.LakeNotInitialized) as ei:
        wapi._con()
    assert ei.value.status_code == 409


def test_b1_status_reports_initialized_false_when_missing(tmp_path, monkeypatch):
    """缺库：/status 不抛，如实返回 initialized=false + coverage 全零 + error/hint。"""
    import lake.web_api as wapi
    from lake import conn as lconn

    missing = str(tmp_path / "missing.duckdb")
    monkeypatch.setattr(lconn, "default_db_path", lambda: missing)
    d = wapi.status()
    assert d["installed"] is True
    assert d["initialized"] is False, f"缺库时 status 必须 initialized=false: {d}"
    assert d["coverage"] == {}, "未就绪 coverage 必须全零（空 dict）"
    assert d["tasks"] == []
    assert d.get("error") == "lake_not_initialized"
    assert "init" in d.get("hint", "")


def test_b1_status_reports_initialized_true_when_ready(tmp_path, monkeypatch):
    """就绪库：/status 返回 initialized=true（happy path 不回归）。"""
    import lake.web_api as wapi
    from lake import conn as lconn

    db = str(tmp_path / "ready.duckdb")
    _seed_db(db, with_data=True)
    monkeypatch.setattr(lconn, "default_db_path", lambda: db)
    d = wapi.status()
    assert d["initialized"] is True
    assert d["installed"] is True


def test_b1_http_contract_body_over_real_http(tmp_path):
    """HTTP 层（uvicorn 真起 web.app）：缺库时 /status→200 initialized=false，
    数据端点 →409 + **顶层** error/hint 契约体（brief B-1 指定形状）。"""
    port = _free_port()
    missing = str(tmp_path / "http_missing" / "lake.duckdb")
    server_code = (
        "import sys; sys.path.insert(0, {repo!r})\n"
        "from lake import conn as _lc\n"
        "_lc.default_db_path = lambda: {db!r}\n"  # 指向不存在的库 → B-1 未就绪
        "import uvicorn\n"
        "from web.app import app\n"
        "uvicorn.run(app, host='127.0.0.1', port={port}, log_level='error')\n"
    ).format(repo=REPO_ROOT, db=missing, port=port)
    srv = subprocess.Popen([PY, "-c", server_code],
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        ready = _wait_ready(port, "/api/lake/status")
        assert ready, "uvicorn 未在超时内就绪"

        s_status, b_status = _http_get(port, "/api/lake/status")
        assert s_status == 200, f"/status 缺库应 200（健康检查），实际 {s_status}"
        d_status = json.loads(b_status)
        assert d_status["initialized"] is False

        # 数据端点：409 + 顶层 error/hint（契约体，非 {"detail":{...}}）
        s_search, b_search = _http_get(port, "/api/lake/search?q=abc")
        assert s_search == 409, f"/search 缺库应 409，实际 {s_search} :: {b_search[:120]}"
        d_search = json.loads(b_search)
        assert d_search.get("error") == "lake_not_initialized", f"顶层 error 缺失: {d_search}"
        assert "init" in d_search.get("hint", ""), f"顶层 hint 缺失: {d_search}"

        s_mkt, b_mkt = _http_get(port, "/api/lake/market?page=1")
        assert s_mkt == 409 and json.loads(b_mkt).get("error") == "lake_not_initialized"
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=10)
        except subprocess.TimeoutExpired:
            srv.kill()
            srv.wait(timeout=10)


# ===========================================================================
# B-2：_refresh_coverage 走 runner 自身 db_path + progress 同目录派生
# ===========================================================================
def test_b2_refresh_coverage_reads_custom_db_not_production(tmp_path, monkeypatch):
    """BackfillRunner(db_path=tmp自定义库) → _refresh_coverage 读 tmp 库（非 get_conn 生产单例）。

    证明手法：
    - 注入数据到 tmp 自定义库（kline_daily 3 行）；
    - monkeypatch get_conn → **抛错**（若 _refresh_coverage 误走生产单例会 fail-open → coverage 空）；
    - 断言 coverage 数值 == 注入的 3 行（证明读的是 tmp 库，不是 get_conn）。
    """
    from lake import backfill as lb
    from lake import conn as lconn

    custom_db = str(tmp_path / "custom.duckdb")
    _seed_db(custom_db, with_data=False)
    con = duckdb.connect(custom_db)
    for d in ("2026-09-10", "2026-09-11", "2026-09-12"):
        con.execute(
            "INSERT INTO kline_daily (ts_code,date,open,high,low,close,volume,"
            "source,fetched_at,data_version) VALUES "
            "('sh.601398',?,?,?,?,?,1000,'t','2026-09-14 00:00:00','v6.0')",
            [d, 10.0, 11.0, 9.5, 10.5])
    con.close()

    # get_conn（生产单例）若被误用 → 抛错，使 coverage fail-open 为空
    monkeypatch.setattr(lconn, "get_conn", lambda: (_ for _ in ()).throw(
        lconn.LakeUnavailable("production db must NOT be touched by custom --db coverage")))

    prog_path = str(tmp_path / "progress.json")
    runner = lb.BackfillRunner(budget_per_day=5000, progress_path=prog_path,
                               db_path=custom_db)
    runner._refresh_coverage()
    cov = runner.progress.get("coverage", {})
    assert "kline_daily" in cov, f"coverage 未读到 tmp 库（误走 get_conn?）: {cov}"
    assert cov["kline_daily"]["rows"] == 3, f"kline_daily 应 3 行（注入值），实际 {cov['kline_daily']}"
    assert cov["kline_daily"]["codes"] == 1
    assert cov["kline_daily"]["date_min"] == "2026-09-10"
    assert cov["kline_daily"]["date_max"] == "2026-09-12"


def test_b2_production_db_file_untouched_by_custom_runner(tmp_path, monkeypatch):
    """自定义 --db 的 _refresh_coverage **不触碰生产库文件**（mtime + 内容指纹不变）。"""
    import hashlib

    from lake import backfill as lb
    from lake import conn as lconn

    prod_db = str(tmp_path / "prod.duckdb")
    custom_db = str(tmp_path / "custom.duckdb")
    _seed_db(prod_db, with_data=True)      # 生产库有数据（若被误读会污染 coverage）
    _seed_db(custom_db, with_data=False)   # 自定义库空

    def fingerprint(p):
        st = os.stat(p)
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return (st.st_mtime_ns, h.hexdigest())

    before = fingerprint(prod_db)
    monkeypatch.setattr(lconn, "get_conn", lambda: (_ for _ in ()).throw(
        lconn.LakeUnavailable("production db must NOT be opened")))
    runner = lb.BackfillRunner(budget_per_day=5000,
                               progress_path=str(tmp_path / "p.json"),
                               db_path=custom_db)
    runner._refresh_coverage()
    after = fingerprint(prod_db)
    assert before == after, "生产库文件被触碰（mtime/内容指纹变化）——B-2 未修复"


def test_b2_default_runner_uses_singleton_get_conn(monkeypatch):
    """缺省库（db_path=None）：_coverage_conn 仍走 get_conn 单例、不 close（行为不变）。"""
    from lake import backfill as lb
    from lake import conn as lconn

    sentinel = object()
    # _coverage_conn 内以 `from .conn import get_conn` 延迟导入 → patch conn 模块属性即生效
    monkeypatch.setattr(lconn, "get_conn", lambda: sentinel)
    runner = lb.BackfillRunner(budget_per_day=5000,
                               progress_path="/tmp/_b2_default_prog.json")
    con, owned = runner._coverage_conn()
    assert con is sentinel, "缺省库必须沿用 get_conn 单例（行为不变）"
    assert owned is False, "借用单例不得 close（owned=False）"


def test_b2_progress_path_derived_from_db_dir():
    """driver._progress_for_db：自定义 --db → 同目录派生；缺省库 → None（走 _progress_path）。"""
    import lake_backfill as drv

    # 自定义 db → 派生到其目录
    assert drv._progress_for_db("/tmp/custom/x.db") == "/tmp/custom/backfill_progress.json"
    # 缺省库 → None（保留测试对 _progress_path 的 monkeypatch + 原行为）
    from lake import conn as lconn

    assert drv._progress_for_db(lconn.default_db_path()) is None


# ===========================================================================
# B-3：T7 四指数 done 键幂等 + --force
# ===========================================================================
def _make_counting_kline_fake(monkeypatch, counter):
    """monkeypatch tencent_ingest.fetch_kline_ohlcv → 计数 fake（固定 3 天 OHLCV，零网络）。"""
    import lake.ingest.tencent_ingest as ti

    def fake_fetch_kline(client, ts_code, n):
        counter["kline"] += 1
        return [{"date": f"2026-09-{d:02d}", "open": 10.0, "high": 11.0,
                 "low": 9.5, "close": 10.5, "volume": 100.0} for d in (10, 11, 12)]

    monkeypatch.setattr(ti, "fetch_kline_ohlcv", fake_fetch_kline)


def test_b3_t7_second_run_idempotent_skip(tmp_path, monkeypatch):
    """同参二次 run：Run2 skipped_done=4、processed=0、**零重复网络调用**（fake 计数断言）。"""
    import lake_backfill as drv
    from lake import backfill as lb
    from lake.ingest.tencent_ingest import INDEX_CODES

    db = str(tmp_path / "t7.duckdb")
    _seed_db(db, with_data=False)
    con = duckdb.connect(db)
    prog = str(tmp_path / "progress.json")
    monkeypatch.setattr(lb.BackfillRunner, "_quota_state", lambda self: (False, 0))

    counter = {"kline": 0}
    _make_counting_kline_fake(monkeypatch, counter)

    # ---- Run 1：首次 → 4 指数各 fetch 一次（计数 4），index_daily 灌 4×3=12 行 ----
    r1 = lb.BackfillRunner(budget_per_day=5000, progress_path=prog, db_path=db)
    s1 = drv.run_t7(con, db, days=3, runner=r1, force=False)
    assert counter["kline"] == len(INDEX_CODES), f"Run1 应 fetch {len(INDEX_CODES)} 次: {counter}"
    assert s1["processed"] == len(INDEX_CODES) and s1["skipped_done"] == 0
    rows1 = con.execute("SELECT COUNT(*) FROM index_daily").fetchone()[0]
    assert rows1 == len(INDEX_CODES) * 3, f"index_daily 应 {len(INDEX_CODES)*3} 行: {rows1}"

    # ---- Run 2：同参重跑 → 全 skipped_done，零 fetch（幂等核心断言）----
    counter["kline"] = 0
    r2 = lb.BackfillRunner(budget_per_day=5000, progress_path=prog, db_path=db)
    s2 = drv.run_t7(con, db, days=3, runner=r2, force=False)
    assert s2["skipped_done"] == len(INDEX_CODES), f"Run2 应全跳过: {s2}"
    assert s2["processed"] == 0, f"Run2 不应重取: {s2}"
    assert counter["kline"] == 0, f"Run2 零网络调用（fake 计数），实际 {counter['kline']} 次 fetch"
    # upsert 幂等：行数不翻倍
    rows2 = con.execute("SELECT COUNT(*) FROM index_daily").fetchone()[0]
    assert rows2 == rows1, f"重跑后行数应不变（upsert 幂等）: {rows2} != {rows1}"
    con.close()


def test_b3_t7_force_refetch(tmp_path, monkeypatch):
    """--force：清空 index_daily done 键 → 强制重取（计数恢复 4）。"""
    import lake_backfill as drv
    from lake import backfill as lb
    from lake.ingest.tencent_ingest import INDEX_CODES

    db = str(tmp_path / "t7f.duckdb")
    _seed_db(db, with_data=False)
    con = duckdb.connect(db)
    prog = str(tmp_path / "progress.json")
    monkeypatch.setattr(lb.BackfillRunner, "_quota_state", lambda self: (False, 0))
    counter = {"kline": 0}
    _make_counting_kline_fake(monkeypatch, counter)

    r1 = lb.BackfillRunner(budget_per_day=5000, progress_path=prog, db_path=db)
    drv.run_t7(con, db, days=3, runner=r1, force=False)
    assert counter["kline"] == len(INDEX_CODES)

    # --force：重取（计数再 +4），processed 恢复 4
    counter["kline"] = 0
    r2 = lb.BackfillRunner(budget_per_day=5000, progress_path=prog, db_path=db)
    s2 = drv.run_t7(con, db, days=3, runner=r2, force=True)
    assert s2["processed"] == len(INDEX_CODES), f"--force 应强制重取: {s2}"
    assert counter["kline"] == len(INDEX_CODES), f"--force 后应 fetch {len(INDEX_CODES)} 次: {counter}"
    con.close()


def test_b3_t7_changed_days_refetch(tmp_path, monkeypatch):
    """改 --days → done 键不同（period=days=N）→ 视为新任务重取（非误跳过）。"""
    import lake_backfill as drv
    from lake import backfill as lb
    from lake.ingest.tencent_ingest import INDEX_CODES

    db = str(tmp_path / "t7d.duckdb")
    _seed_db(db, with_data=False)
    con = duckdb.connect(db)
    prog = str(tmp_path / "progress.json")
    monkeypatch.setattr(lb.BackfillRunner, "_quota_state", lambda self: (False, 0))
    counter = {"kline": 0}
    _make_counting_kline_fake(monkeypatch, counter)

    r1 = lb.BackfillRunner(budget_per_day=5000, progress_path=prog, db_path=db)
    drv.run_t7(con, db, days=3, runner=r1, force=False)
    assert counter["kline"] == len(INDEX_CODES)

    # 改 days=5 → period="days=5" ≠ "days=3" → 重取
    counter["kline"] = 0
    r2 = lb.BackfillRunner(budget_per_day=5000, progress_path=prog, db_path=db)
    s2 = drv.run_t7(con, db, days=5, runner=r2, force=False)
    assert s2["processed"] == len(INDEX_CODES), f"改 --days 应重取: {s2}"
    assert counter["kline"] == len(INDEX_CODES)
    con.close()


# ===========================================================================
# B-4：LakeLock 接线进 driver 写路径 + 两进程并发写不冲突
# ===========================================================================
def test_b4_main_acquires_lakelock_for_write_cmds_not_status(tmp_path, monkeypatch):
    """main()：写命令（init/p0/history）持 LakeLock，只读 status 不持锁。

    证明手法：monkeypatch lake.conn.LakeLock 为记录型子类，断言 init 进入/退出各一次、
    status 零次。init/status 均离线（无网络），可安全真跑。
    """
    import lake_backfill as drv
    from lake import conn as lconn

    events = []

    class _RecordingLock(lconn.LakeLock):
        def __enter__(self):
            events.append("enter")
            return self  # 测试不真加 flock（避免与真实锁文件交互），只记录
        def __exit__(self, *a):
            events.append("exit")
            return False

    monkeypatch.setattr(lconn, "LakeLock", _RecordingLock)

    db = str(tmp_path / "b4.duckdb")
    # 写命令 init → LakeLock enter/exit 各一次
    events.clear()
    rc = drv.main(["--db", db, "init"])
    assert rc == drv.EXIT_OK
    assert events == ["enter", "exit"], f"init 应持锁一次: {events}"

    # 只读 status → 不持锁（0 次 enter）
    events.clear()
    rc = drv.main(["--db", db, "status"])
    assert rc == drv.EXIT_OK
    assert events == [], f"status 不应持锁: {events}"


def test_b4_two_processes_concurrent_write_no_conflict(tmp_path):
    """两进程并发写同一 tmp 库（走 driver 的 _write_lock+_open_db 路径）→ 均成功、无丢行。

    B-4 最小验证（brief：tmp 库 + subprocess 即可，离线）。每个子进程循环 N 次
    {持 LakeLock → connect → insert 1 行 → close}——connect 在锁内（关键：若不在锁内，
    两进程会同时 duckdb.connect 同一文件 → 第二个 "Could not set lock on file" 崩溃）。
    断言：两进程 rc 均 0、总行数 = 2N（无冲突、无丢行）。
    """
    db = str(tmp_path / "b4_concurrent.duckdb")
    n = 15
    # 用字符串拼接注入 repo/scripts（避免 .format() 与子脚本内 f-string 的 {} 冲突）
    child = (
        "import sys, time\n"
        "sys.path.insert(0, " + repr(REPO_ROOT) + "); sys.path.insert(0, " + repr(SCRIPTS_DIR) + ")\n"
        "import lake_backfill as drv\n"
        "db = sys.argv[1]; n = int(sys.argv[2]); base = int(sys.argv[3])\n"
        "for i in range(n):\n"
        "    with drv._write_lock(db):          # B-4 统一写路径入口（LakeLock flock）\n"
        "        con = drv._open_db(db)         # connect 在锁内 → 并发 writer 阻塞等锁而非崩溃\n"
        "        try:\n"
        '            con.execute("INSERT INTO stock_master (ts_code,name,source,fetched_at,"\n'
        '                        "data_version) VALUES (?, \'t\', \'t\', \'2026-09-14 00:00:00\',\'v6.0\')",\n'
        '                        [f"sh.{base + i}"])   # base 区分两进程 → PK 不冲突\n'
        "        finally:\n"
        "            con.close()\n"
        "    time.sleep(0.03)                    # 制造重叠窗口（无锁时必撞）\n"
        'print("child ok")\n'
    )

    p1 = subprocess.Popen([PY, "-c", child, db, str(n), "900000"],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p2 = subprocess.Popen([PY, "-c", child, db, str(n), "910000"],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out1, err1 = p1.communicate(timeout=120)
    out2, err2 = p2.communicate(timeout=120)

    assert p1.returncode == 0, f"进程1 失败 rc={p1.returncode}: {err1.decode()[-400:]}"
    assert p2.returncode == 0, f"进程2 失败 rc={p2.returncode}: {err2.decode()[-400:]}"

    con = duckdb.connect(db, read_only=True)
    total = con.execute("SELECT COUNT(*) FROM stock_master").fetchone()[0]
    con.close()
    assert total == 2 * n, f"两进程并发写应共 {2*n} 行（无冲突/丢行），实际 {total}"


# ===========================================================================
# HTTP helpers（B-1 用，复用 D-4 口径）
# ===========================================================================
def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _http_get(port: int, path: str):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        c.request("GET", path)
        r = c.getresponse()
        return r.status, r.read().decode()
    finally:
        c.close()


def _wait_ready(port: int, path: str, tries: int = 60) -> bool:
    for _ in range(tries):
        try:
            s, _ = _http_get(port, path)
            if s in (200, 409):
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    return False
