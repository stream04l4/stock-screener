# -*- coding: utf-8 -*-
"""test_lake_v602_n1_driver —— v6.0.2 回归：N-1 LakeLock 修复 + backfill driver CLI。

**验收点（v6.0.2 brief）**：
1. N-1：``with LakeLock(tmp_db) as _:`` 真实进入+退出成功（flock 加解锁不抛）；
   且锁文件确由 **builtins.open** 创建（行为等价证明：patch builtins.open 记录调用，
   若仍走模块级 open() 遮蔽 → duckdb.connect("a+") 必抛，记录为空 → fail）。
2. conn.py 无其它被遮蔽的内置函数调用残留（AST 扫描：模块内重定义 builtin 名后，
   def 之后的同名裸 Name 调用必须不存在——即全部显式 qualified 或已删除）。
3. driver 各子命令**离线可导入/参数解析**（不触网、不碰真实库）：
   init/p0/history/status 的 argparse 结构 + 缺参报错 + LakeUnavailable 友好退出。
4. p0 冒烟路径（零网络，fake ingest）：--skip-t1 + --codes + --t4-codes 走通
   T4/T7/T2/T3 编排，库内行数正确；T2 done 键**中断续跑幂等**（重跑 skipped_done，
   未完成股才重新 fetch）。

纪律：本文件全部离线——BaoStock/腾讯均 monkeypatch fake，零真实网络调用、
不碰 data/lake/lake.duckdb（driver 一律 --db tmp_path；progress 重定向 tmp；
get_conn 隔离 → _refresh_coverage fail-open 不打开真实库）。
"""
from __future__ import annotations

import ast
import builtins as _builtins
import json
import os
import sys

import pytest

pytest.importorskip("duckdb")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)


# ===========================================================================
# N-1：LakeLock 真实加解锁 + builtins.open 行为等价证明
# ===========================================================================
def test_n1_lakelock_enter_exit_real(tmp_path):
    """with LakeLock(tmp_db) as _: 真实进入+退出（flock LOCK_EX/UN 不抛）。

    N-1 根因：模块级 ``def open(db_path=None)`` 遮蔽内置 open，旧代码
    ``open(self._lock_path, "a+")`` 会把 "a+" 当 DuckDB db_path → duckdb.connect("a+")
    必抛。修复后必须走 builtins.open——本用例直接验证 with 语句全程无异常，
    且锁文件被真实创建（"a+" append 模式语义）。
    """
    from lake.conn import LakeLock

    db_path = str(tmp_path / "lake.duckdb")
    lock = LakeLock(db_path)
    assert lock._lock_path == db_path + ".write.lock"
    with lock as _entered:          # N-1：旧代码在此行必抛（duckdb.connect("a+")）
        assert _entered is lock
        # 持锁期间锁文件必须存在（builtins.open "a+" 创建语义）
        assert os.path.exists(lock._lock_path), "锁文件未创建（open 模式异常?）"
    # __exit__ 后句柄已关闭、状态复位
    assert lock._fh is None
    # 二次进入/退出仍成功（幂等，无残留句柄）
    with lock:
        pass


def test_n1_lakelock_uses_builtins_open(tmp_path, monkeypatch):
    """行为等价证明：LakeLock.__enter__ 的锁文件打开必须经 builtins.open。

    方法：monkeypatch builtins.open 记录 (path, mode)；若 __enter__ 仍被模块级
    open() 遮蔽 → 走 duckdb.connect("a+") 抛异常，记录为空 → 断言失败。
    """
    from lake import conn as lconn

    calls = []
    real_open = _builtins.open

    def spy(path, mode="r", *a, **kw):
        if "write.lock" in str(path):
            calls.append((str(path), mode))
        return real_open(path, mode, *a, **kw)

    monkeypatch.setattr(_builtins, "open", spy)
    db_path = str(tmp_path / "lake.duckdb")
    with lconn.LakeLock(db_path):
        pass
    assert calls == [(db_path + ".write.lock", "a+")], (
        f"锁文件未由 builtins.open(path,'a+') 打开（N-1 遮蔽残留?）calls={calls}"
    )


def test_n1_conn_no_other_shadowed_builtin_calls():
    """conn.py 全文件 AST 核查：模块内重定义 builtin 名后，def 之后的裸调用只允许白名单。

    N-1 brief："检查 conn.py 全文件是否还有其它被遮蔽的内置函数调用（open/abs/
    input 等），一并修"。本测试把该约定固化为回归：凡 lake/conn.py 顶层重定义的
    builtin 名，其 def 行号之后的同名**裸 Name 调用**必须在白名单内（= 明确意图
    调模块自身函数，而非误用被遮蔽的 builtin）；白名单外的新出现 → fail，强制
    评审（是 builtins.xxx qualified？还是新增遮蔽？）。

    白名单语义（为什么不是"def 之后零裸调用"）：``get_conn`` 里 ``_conn = open()``
    就是**有意**调模块级 DuckDB 工厂——合法。N-1 bug 的本质是"想调 builtin 却被
    遮蔽"，AST 无法推断意图，故用白名单把每个合法调用点显式钉死。
    """
    import builtins

    conn_path = os.path.join(ROOT, "lake", "conn.py")
    with open(conn_path, encoding="utf-8") as f:
        tree = ast.parse(f.read())

    builtin_names = {n for n in dir(builtins) if not n.startswith("_")}
    # 顶层重定义的函数名（def/assign）∩ builtin 名 = 遮蔽集合
    shadow_def_line = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in builtin_names:
                shadow_def_line[node.name] = node.lineno
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in builtin_names:
                    shadow_def_line[t.id] = node.lineno

    # 白名单：(name, func_name) —— def 之后有意调模块自身函数的合法裸调用点。
    # open @ get_conn：_conn = open() → 模块级 DuckDB 工厂（N-1 修复后全文件唯一
    # 合法裸调用；LakeLock.__enter__ 已改 builtins.open qualified）。
    ALLOWED_BARE_CALLS = {("open", "get_conn")}

    violations = []
    for name, def_line in shadow_def_line.items():
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == name and node.lineno > def_line):
                continue
            # 定位所属顶层函数（白名单按函数名匹配，行号漂移不影响）
            func_name = None
            for top in tree.body:
                if isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and top.lineno <= node.lineno <= (top.end_lineno or 0):
                    func_name = top.name
                    break
            if (name, func_name) not in ALLOWED_BARE_CALLS:
                violations.append(
                    f"line {node.lineno} ({func_name}): 裸调 {name}()（被模块级定义遮蔽；"
                    f"若意图 builtin → builtins.{name}，若有意调模块函数 → 加白名单并注释）")
    assert not violations, "conn.py 存在被遮蔽的内置调用残留:\n  " + "\n  ".join(violations)


# ===========================================================================
# driver：离线可导入 / 参数解析 / 友好报错
# ===========================================================================
def _import_driver():
    import lake_backfill

    return lake_backfill


def test_driver_import_offline():
    """driver 模块可导入（顶层不触网、不建库）。"""
    mod = _import_driver()
    assert hasattr(mod, "main") and hasattr(mod, "build_parser")
    for fn in ("cmd_init", "cmd_p0", "cmd_history", "cmd_status"):
        assert callable(getattr(mod, fn))


def test_driver_subcommands_parse():
    """四个子命令均可解析；p0 默认 days=250、--codes 逗号拆分。"""
    mod = _import_driver()
    p = mod.build_parser()

    a = p.parse_args(["init"])
    assert a.cmd == "init" and a.func is mod.cmd_init

    a = p.parse_args(["p0"])
    assert a.cmd == "p0" and a.days == 250 and a.codes is None and not a.skip_t1

    a = p.parse_args(["p0", "--days", "10", "--codes", "sh.601398,sz.000001",
                      "--skip-t1", "--t4-codes", "sh.601398"])
    assert a.days == 10 and a.codes == "sh.601398,sz.000001" and a.skip_t1
    assert mod._parse_codes(a.codes) == ["sh.601398", "sz.000001"]

    a = p.parse_args(["history", "--start-date", "2000-01-01"])
    assert a.cmd == "history" and a.start_date == "2000-01-01"

    a = p.parse_args(["status"])
    assert a.cmd == "status" and a.func is mod.cmd_status


def test_driver_missing_subcommand_usage_error():
    """无子命令 → argparse 报错 exit 2（用法错误，不崩）。"""
    mod = _import_driver()
    with pytest.raises(SystemExit) as ei:
        mod.build_parser().parse_args([])
    assert ei.value.code == 2


def test_driver_lake_unavailable_friendly(tmp_path, monkeypatch, capsys):
    """duckdb 缺失（LakeUnavailable）→ 友好报错 exit 3，无 traceback。"""
    mod = _import_driver()
    from lake.conn import LakeUnavailable

    def fake_open(db_path=None):
        raise LakeUnavailable("duckdb 未安装（uv sync --extra lake）")

    monkeypatch.setattr(mod, "_open_db", fake_open)
    # v6.0.4：显式 --db tmp 库。为什么：main() 对写命令（init/p0/history）整段持
    # LakeLock(flock)（B-4），缺省 db → **生产** data/lake/lake.duckdb.write.lock——
    # p0 灌数运行期间该 flock 被灌数进程持有，本用例会阻塞等锁直到超时（实测全量
    # pytest 卡死在此）。断言目标（_open_db 抛 LakeUnavailable → rc=3 + 友好报错）
    # 与库路径无关（fake_open 在任何路径都抛），故 tmp 库不改变测试语义，只把锁文件
    # 移离生产目录（与其他 driver 用例 --db tmp 口径一致）。
    rc = mod.main(["--db", str(tmp_path / "lake.duckdb"), "init"])
    assert rc == mod.EXIT_LAKE_UNAVAILABLE
    err = capsys.readouterr().err
    assert "数据湖不可用" in err and "uv sync --extra lake" in err


# ===========================================================================
# p0 冒烟路径（零网络，fake ingest）+ T2 done 键中断续跑幂等
# ===========================================================================
class _FakeInterrupt(Exception):
    """模拟进程中断（worker 抛此异常 = 该任务未完成、未标 done）。"""


def _make_fake_env(monkeypatch, tmp_path, interrupt_code=None, prog_path=None):
    """monkeypatch driver 内 ingest 依赖 → 全离线 fake。返回计数 dict。

    - T1：整函数替换（--skip-t1 冒烟不触发；触发则计数，零网络）。
    - T4 load_dividends：写 1 行分红（零网络）。
    - 腾讯 K线/快照：fake 固定 3 天 OHLCV / 固定估值。
    - BackfillRunner._quota_state → (False, 0)（隔离真实配额文件）。
    - _progress_path → tmp（不碰 data/lake/backfill_progress.json）。
    - lconn.get_conn → LakeUnavailable（_refresh_coverage fail-open，不打开真实库）。
    """
    import lake_backfill as drv
    from lake import backfill as lb
    from lake import conn as lconn

    calls = {"kline": [], "snap": [], "t1": 0, "div": 0}

    monkeypatch.setattr(lb.BackfillRunner, "_quota_state", lambda self: (False, 0))
    if prog_path:
        monkeypatch.setattr(lb, "_progress_path", lambda: prog_path)
    # _refresh_coverage 的 get_conn() 不得打开真实 data/lake/lake.duckdb（测试零副作用）
    monkeypatch.setattr(lconn, "get_conn", lambda: (_ for _ in ()).throw(
        lconn.LakeUnavailable("test isolation")))

    # T1 fake（整函数替换：BaoStock 2 次调用折叠为计数，零网络）
    def fake_run_t1(con, db_path, quota_before):
        calls["t1"] += 1
        return {"table": "stock_master", "rows_written": 0, "name_isst_enriched": 0,
                "baostock_calls_this_step": 2, "note": "fake"}

    monkeypatch.setattr(drv, "run_t1", fake_run_t1)

    # T4 fake（零网络）
    import lake.ingest.em_dividend_ingest as emi

    def fake_load_dividends(con, cache_dir, ts_codes=None, source="em_local_static"):
        calls["div"] += 1
        from lake.ingest.common import DATA_VERSION, now_ts

        code = (ts_codes or ["sh.601398"])[0]
        con.execute('DELETE FROM dividend_events WHERE "ts_code" = ?', [code])
        con.execute(
            'INSERT INTO dividend_events (ts_code, ex_date, ann_date, period, cash_dps,'
            ' stk_div, source, fetched_at, data_version) VALUES (?,?,?,?,?,?,?,?,?)',
            [code, "2026-05-13", "2026-04-01", "2025", 1.2, None,
             source, now_ts(), DATA_VERSION])
        return 1

    monkeypatch.setattr(emi, "load_dividends", fake_load_dividends)

    # 腾讯 fake：K线固定 3 天；快照固定估值（interrupt_code → 模拟中断）
    import lake.ingest.tencent_ingest as ti

    def fake_fetch_kline(client, ts_code, n):
        calls["kline"].append(ts_code)
        if interrupt_code and ts_code == interrupt_code:
            raise _FakeInterrupt("simulated crash")
        return [{"date": f"2026-09-{d:02d}", "open": 10.0, "high": 11.0,
                 "low": 9.5, "close": 10.5, "volume": 100.0} for d in (10, 11, 12)]

    def fake_fetch_snapshot(client, ts_codes):
        out = {}
        for c in ts_codes:
            calls["snap"].append(c)
            if interrupt_code and c == interrupt_code:
                raise _FakeInterrupt("simulated crash")
            out[c] = {"name": "测试", "price": 10.5, "timestamp": "20260912150000",
                      "pct_chg": 0.5, "turnover": 1.0, "pe_ttm": 8.0,
                      "float_mv_yi": 100.0, "total_mv_yi": 200.0, "pb": 0.8,
                      "ttm_yield_pct": 4.0}
        return out

    monkeypatch.setattr(ti, "fetch_kline_ohlcv", fake_fetch_kline)
    monkeypatch.setattr(ti, "fetch_snapshot", fake_fetch_snapshot)
    return calls


def _seed_universe(con, codes):
    """stock_master 播种（p0 T2/T3 universe 来源；--skip-t1 冒烟用）。"""
    from lake.ingest.common import DATA_VERSION, now_ts

    for c in codes:
        con.execute(
            "INSERT OR REPLACE INTO stock_master (ts_code,name,industry_csric2,"
            "industry_name,list_date,delist_date,board,is_st,st_since,soe_flag,"
            "soe_basis,source,fetched_at,data_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [c, "测试股", None, None, "2010-01-01", None, "主板", 0, None,
             None, None, "test_seed", now_ts(), DATA_VERSION])


def test_p0_smoke_path_offline(tmp_path, monkeypatch):
    """p0 冒烟路径（--skip-t1 + 2 只 × 3 天）：T4/T7/T2/T3 全走通，库内行数正确。

    零网络：BaoStock/腾讯全 fake；库 = tmp_path（不碰 data/lake/lake.duckdb）。
    """
    import lake_backfill as drv
    from lake import conn as lconn

    db = str(tmp_path / "smoke.duckdb")
    con = lconn.open(db)
    _seed_universe(con, ["sh.601398", "sz.000001"])
    calls = _make_fake_env(monkeypatch, tmp_path,
                           prog_path=str(tmp_path / "progress.json"))

    rc = drv.main(["--db", db, "p0", "--days", "3", "--skip-t1",
                   "--codes", "sh.601398,sz.000001", "--t4-codes", "sh.601398"])
    assert rc == drv.EXIT_OK

    # T2：2 只 K线各灌 3 天（fake fetch 被调 2 次；T7 指数 code 无点，过滤掉）
    stock_kl = [c for c in calls["kline"] if "." in c]
    assert sorted(stock_kl) == ["sh.601398", "sz.000001"], f"T2 K线 fetch 异常: {stock_kl}"
    rows = con.execute("SELECT COUNT(*) FROM kline_daily").fetchone()[0]
    assert rows == 6, f"kline_daily 应 6 行（2 股×3 天），实际 {rows}"
    # T3：快照 2 只 → valuation_daily 2 行
    vrows = con.execute("SELECT COUNT(*) FROM valuation_daily").fetchone()[0]
    assert vrows == 2
    # T4：sh.601398 分红 1 行（fake，零网络）
    drows = con.execute(
        "SELECT COUNT(*) FROM dividend_events WHERE ts_code='sh.601398'").fetchone()[0]
    assert drows == 1
    # T7：4 指数各 3 天（fake K线对指数 code 同样生效）
    irows = con.execute("SELECT COUNT(*) FROM index_daily").fetchone()[0]
    assert irows == 12
    # BaoStock 零真实调用（--skip-t1；fake run_t1 未触发）
    assert calls["t1"] == 0
    con.close()


def test_p0_t2_resume_idempotent_skips_done(tmp_path, monkeypatch):
    """p0 T2 中断续跑幂等：Run1 在 sz.000001（排序最后）处"中断"→ A/B 已 done 落盘；
    Run2 重跑 → A/B skipped_done（fake fetch 不再被调），只补 sz.000001。

    验收标准："p0 中断续跑幂等（done 键跳过）"——复用 BackfillRunner 幂等，
    本用例验证 driver 编排层正确接入（progress 重定向 tmp 隔离）。

    ⚠️ Task 排序 = (priority, table, ts_code) 升序 → sh.600036 < sh.601398 < sz.000001，
    故中断点必须选排序最后的 sz.000001，Run1 才会先完成 A/B。
    """
    import lake_backfill as drv
    from lake import conn as lconn

    db = str(tmp_path / "resume.duckdb")
    prog_path = str(tmp_path / "progress.json")
    codes = ["sh.601398", "sz.000001", "sh.600036"]

    con = lconn.open(db)
    _seed_universe(con, codes)
    con.close()

    # ---- Run 1：sz.000001 处中断（前两只成功）----
    calls1 = _make_fake_env(monkeypatch, tmp_path, interrupt_code="sz.000001",
                            prog_path=prog_path)
    rc1 = drv.main(["--db", db, "p0", "--days", "3", "--skip-t1",
                    "--codes", ",".join(codes), "--t4-codes", "sh.601398"])
    # 中断异常被 BackfillRunner.run 捕获记 error（单任务失败不中断整队列）→ exit 0
    assert rc1 == drv.EXIT_OK
    stock_kl1 = [c for c in calls1["kline"] if "." in c]  # T7 指数 code 无点，过滤
    assert sorted(stock_kl1) == sorted(codes)   # Run1：3 只都尝试了
    con = lconn.open(db)
    rows1 = con.execute("SELECT COUNT(*) FROM kline_daily").fetchone()[0]
    assert rows1 == 6, f"Run1 后应 6 行（2 股×3 天），实际 {rows1}"
    con.close()

    # ---- Run 2：重启重跑 → A/B skipped_done，只补 sz.000001 ----
    calls2 = _make_fake_env(monkeypatch, tmp_path, prog_path=prog_path)  # 无中断
    rc2 = drv.main(["--db", db, "p0", "--days", "3", "--skip-t1",
                    "--codes", ",".join(codes), "--t4-codes", "sh.601398"])
    assert rc2 == drv.EXIT_OK
    # 幂等核心断言：已 done 的 A/B 不再触发 fetch，只有未完成的 sz.000001 被补
    # （T7 指数 code 无点，过滤掉——Run2 T7 仍会 fetch 4 指数但 upsert 幂等）
    stock_kl2 = [c for c in calls2["kline"] if "." in c]
    assert stock_kl2 == ["sz.000001"], (
        f"Run2 T2 应只 fetch 未完成的 sz.000001，实际 {stock_kl2}")

    # done 键落盘验证：progress 文件含三只股的 kline_daily done 键
    with open(prog_path, encoding="utf-8") as f:
        prog = json.load(f)
    done_keys = {tuple(k) for k in prog.get("done", [])}
    for c in codes:
        assert ("kline_daily", c, "3") in done_keys, f"缺 done 键 {c}"

    # 库内数据完整：3 股 × 3 天 = 9 行（Run1 A/B + Run2 C，无重复灌入）
    con = lconn.open(db)
    rows = con.execute("SELECT COUNT(*) FROM kline_daily").fetchone()[0]
    assert rows == 9, f"kline_daily 应 9 行（中断续跑不重灌），实际 {rows}"
    con.close()


def test_status_offline(tmp_path, monkeypatch):
    """status：coverage + progress 摘要，零网络、零真实库副作用。"""
    import lake_backfill as drv
    from lake import conn as lconn

    db = str(tmp_path / "st.duckdb")
    con = lconn.open(db)
    _seed_universe(con, ["sh.601398"])
    con.execute(
        "INSERT INTO kline_daily (ts_code,date,open,high,low,close,volume,amount,"
        "pct_chg,is_st,preclose,adj_factor,source,fetched_at,data_version) "
        "VALUES ('sh.601398','2026-09-11',10,11,9.5,10.5,10000,NULL,NULL,0,NULL,NULL,"
        "'test','2026-09-14 00:00:00','v6.0')")
    con.close()

    _make_fake_env(monkeypatch, tmp_path, prog_path=str(tmp_path / "progress.json"))
    rc = drv.main(["--db", db, "status"])
    assert rc == drv.EXIT_OK


def test_init_offline(tmp_path):
    """init：幂等建 schema（tmp 库），表清单齐全；二次运行不报错。"""
    import duckdb

    import lake_backfill as drv
    from lake.ddl import TABLES

    db = str(tmp_path / "init.duckdb")
    rc1 = drv.main(["--db", db, "init"])
    assert rc1 == drv.EXIT_OK
    # 幂等：二次 init 不抛（IF NOT EXISTS）
    rc2 = drv.main(["--db", db, "init"])
    assert rc2 == drv.EXIT_OK
    con = duckdb.connect(db, read_only=True)
    tables = {r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='main' AND table_type='BASE TABLE'").fetchall()}
    assert set(TABLES) <= tables
    con.close()
