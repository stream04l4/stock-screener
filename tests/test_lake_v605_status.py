# -*- coding: utf-8 -*-
"""test_lake_v605_status —— v6.0.5 回归：/status 区块 C 重设计数据源 + 前端契约。

规格（v6.0.5 brief）：
- /status **ready 态**追加新字段：``tables``（9 表固定顺序 T1-T9，逐表 rows/codes/
  date_min/date_max/last_sync_at/state/state_detail）/ ``views``（3 个）/
  ``adj_factor_coverage_pct`` / ``db{path,size_mb}`` / ``sync{last_updated_at,
  backfill_in_progress,quota_used_today,quota_budget}``。旧字段逐字节不动。
- **state 判定四分支**（全运行时计算、零硬编码数据值）：
  fresh（日频表 date_max ≥ 参考最新交易日−1 天；master/dividend 快照语义）/
  lagging（>1 天 → "滞后 N 日"）/ pending（P2/P3 四张表**即使有行也标 pending**——
  计划内未做 ≠ 坏了）/ empty（其余 0 行表，防御）。
- **locked / uninitialized 两态响应体保持 v6.0.4 逐字节不变**（新字段只追加在
  initialized+ready 态；test_lake_v604_lock 三态契约依赖此纪律）。
- 前端契约：index.html 区块 C 新 DOM（#lake-summary/#lake-tables/#lake-views）、
  app.js 渲染函数与四色徽章 class、style.css 对应样式；旧 #lake-coverage 已移除。

纪律：库一律 tmp_path，**绝不触碰 data/lake/ 生产库**；日期用 today() 相对偏移
（fresh/lagging 判定是相对参考交易日的天数差，不比较具体日期值）。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import socket
import subprocess
import sys
import time

import pytest

duckdb = pytest.importorskip("duckdb")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
PY = sys.executable or os.path.join(REPO_ROOT, ".venv", "bin", "python")

# 9 表固定顺序（T1-T9，与 lake.ddl.TABLES / web_api._TABLE_META 一致）
EXPECTED_TABLE_KEYS = [
    "stock_master", "kline_daily", "valuation_daily", "dividend_events",
    "fundamentals_quarterly", "holders_snapshot", "index_daily",
    "factor_snapshot", "macro_rf",
]
PENDING_KEYS = {"fundamentals_quarterly", "holders_snapshot",
                "factor_snapshot", "macro_rf"}


# ===========================================================================
# helpers
# ===========================================================================
def _write_progress(tmp_path, tasks=None, updated_at="2026-09-15 13:33:12") -> str:
    """写 backfill_progress.json（tmp）：T4 as_of + quota 字段齐全。"""
    p = str(tmp_path / "backfill_progress.json")
    data = {
        "updated_at": updated_at,
        "tasks": tasks if tasks is not None else [
            {"table": "dividend_events", "tier": "P3", "total": 0, "done": 0,
             "quota_used_today": 4, "quota_budget": 5000, "state": "running",
             "eta_min": None, "last_error": "", "as_of": "2026-09-10"},
            {"table": "kline_daily", "tier": "P3", "total": 0, "done": 0,
             "quota_used_today": 9, "quota_budget": 5000, "state": "running",
             "eta_min": None, "last_error": ""},
        ],
        "coverage": {},
    }
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return p


def _seed_v605_db(db_path: str) -> None:
    """建 schema + 播种覆盖四分支的测试数据（日期全部相对 today()）：

    - kline_daily：max(date)=today → 参考最新交易日=today；af 非空 2/4=50%。
    - valuation_daily：max=yesterday → gap=1 → fresh（边界）。
    - index_daily：max=today-5d → lagging"滞后 5 日"。
    - stock_master / dividend_events：有行 → fresh（快照语义）。
    - factor_snapshot：**有 2 行**仍 pending（P2 计划内未做 ≠ 按行数判定）。
    - fundamentals_quarterly / holders_snapshot / macro_rf：0 行 → pending。
    """
    from lake import conn as lconn

    today = dt.date.today()
    d = lambda n: (today - dt.timedelta(days=n)).isoformat()  # noqa: E731
    con = lconn.open(db_path)
    con.execute(
        "INSERT INTO stock_master (ts_code,name,industry_csric2,board,is_st,"
        "source,fetched_at,data_version) VALUES "
        "('sh.601398','工商银行','J66','主板',0,'t','2026-09-15 09:26:13','v6.0')")
    con.executemany(
        "INSERT INTO kline_daily (ts_code,date,open,high,low,close,volume,"
        "amount,pct_chg,is_st,preclose,adj_factor,source,fetched_at,data_version) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [("sh.601398", d(n), 1, 2, 0.5, 1.5, 100, 1e5, 1.0, 0, 1,
          af if af is not None else None, "t", "2026-09-15 12:42:50", "v6.0")
         for n, af in [(3, 1.0), (2, 1.0), (1, None), (0, None)]])
    con.execute(
        "INSERT INTO valuation_daily (ts_code,date,total_mv,float_mv,pe_ttm,pb,"
        "turnover_pct,ttm_yield_pct,source,fetched_at,data_version) VALUES "
        "('sh.601398',?,1e10,5e9,5.0,0.5,0.1,4.0,'t','2026-09-15 13:33:11','v6.0')",
        [d(1)])
    con.execute(
        "INSERT INTO dividend_events (ts_code,ex_date,ann_date,period,cash_dps,"
        "stk_div,source,fetched_at,data_version) VALUES "
        "('sh.601398',?,'2026-08-31','2025Q4',1.2,NULL,'em_local_static',"
        "'2026-09-15 09:27:55','v6.0')", [d(-1)])   # ex_date 可含未来除权日
    con.execute(
        "INSERT INTO index_daily (index_code,date,open,high,low,close,volume,"
        "amount,source,fetched_at,data_version) VALUES "
        "('sh000300',?,1,2,0.5,1.5,100,NULL,'t','2026-09-15 09:30:41','v6.0')",
        [d(5)])
    con.executemany(
        "INSERT INTO factor_snapshot (ts_code,as_of_date,factor_name,value,"
        "params_json,source,fetched_at,data_version) VALUES (?,?,?,?,?,?,?,?)",
        [("sh.601398", d(1), "ann_vol_5y", 0.2, "{}", "t",
          "2026-09-14 15:33:51", "v6.0"),
         ("sh.601398", d(1), "yield_pctile_own_hist", 0.8, "{}", "t",
          "2026-09-14 15:33:51", "v6.0")])
    con.close()


def _ready_status(tmp_path, monkeypatch, db_name="ready.duckdb"):
    """tmp 库播种 + monkeypatch 路径 → wapi.status()（ready 态）。"""
    import lake.web_api as wapi
    from lake import conn as lconn

    p = str(tmp_path / db_name)
    _seed_v605_db(p)
    prog = _write_progress(tmp_path)
    monkeypatch.setattr(lconn, "default_db_path", lambda: p)
    monkeypatch.setattr(lconn, "progress_path", lambda: prog)
    return wapi.status(), p


def _table(d, key):
    t = {x["key"]: x for x in d["tables"]}
    assert key in t, f"tables 缺 {key}: {[x['key'] for x in d['tables']]}"
    return t[key]


# ===========================================================================
# A. ready 态：新字段结构（9 表齐全 / views / db / sync / 旧字段兼容）
# ===========================================================================
def test_status_ready_tables_nine_fixed_order(tmp_path, monkeypatch):
    """ready 态 tables：9 张表、固定顺序 T1-T9、每行字段齐全。"""
    d, _ = _ready_status(tmp_path, monkeypatch)
    assert [t["key"] for t in d["tables"]] == EXPECTED_TABLE_KEYS, \
        f"9 表顺序必须为 T1-T9: {[t['key'] for t in d['tables']]}"
    for t in d["tables"]:
        for f in ("key", "tier", "name_cn", "desc", "rows", "codes",
                  "date_min", "date_max", "last_sync_at", "state", "state_detail"):
            assert f in t, f"{t['key']} 缺字段 {f}"
        assert t["state"] in ("fresh", "lagging", "pending", "empty")


def test_status_ready_views_three(tmp_path, monkeypatch):
    """ready 态 views：3 个（hfq/qfq/panorama），key+name_cn+desc 齐全。"""
    d, _ = _ready_status(tmp_path, monkeypatch)
    assert [v["key"] for v in d["views"]] == \
        ["kline_daily_hfq", "kline_daily_qfq", "stock_panorama"]
    for v in d["views"]:
        assert v["name_cn"] and v["desc"]


def test_status_ready_db_and_sync(tmp_path, monkeypatch):
    """ready 态 db{path,size_mb} + sync{last_updated_at,backfill_in_progress,quota}。

    quota 读 progress 文件（不受锁影响）：used 取各 task max（4/9→9）、budget=5000；
    ready 态 backfill_in_progress=false。
    """
    d, p = _ready_status(tmp_path, monkeypatch)
    assert d["db"]["path"] == p
    assert isinstance(d["db"]["size_mb"], (int, float)) and d["db"]["size_mb"] > 0
    s = d["sync"]
    assert s["last_updated_at"] == "2026-09-15 13:33:12"
    assert s["backfill_in_progress"] is False
    assert s["quota_used_today"] == 9, f"quota used 应取 max(4,9)=9: {s}"
    assert s["quota_budget"] == 5000


def test_status_ready_adj_factor_coverage(tmp_path, monkeypatch):
    """adj_factor_coverage_pct = kline_daily adj_factor 非空占比（2/4=50.0）。"""
    d, _ = _ready_status(tmp_path, monkeypatch)
    assert d["adj_factor_coverage_pct"] == 50.0


def test_status_ready_old_fields_unchanged_no_new_keys_leak(tmp_path, monkeypatch):
    """ready 态旧字段逐字节兼容：installed/duckdb_version/initialized/coverage/tasks/
    updated_at 语义不变；无 error/hint/backfill_in_progress 顶层键（三态互不串味）。"""
    d, _ = _ready_status(tmp_path, monkeypatch)
    assert d["installed"] is True
    assert d["duckdb_version"] == getattr(duckdb, "__version__", "?")
    assert d["initialized"] is True
    assert isinstance(d["coverage"], dict) and isinstance(d["tasks"], list)
    assert "error" not in d and "hint" not in d
    assert "backfill_in_progress" not in d   # 顶层键只在 locked 态出现（v6.0.4）


# ===========================================================================
# B. state 判定四分支（对播种数据逐表断言）
# ===========================================================================
def test_status_state_daily_fresh_lagging(tmp_path, monkeypatch):
    """日频表：kline(max=today)→fresh"最新"；valuation(max=yesterday,gap=1)→fresh
    （边界 ≤1 天）；index(max=today-5d)→lagging"滞后 5 日"。"""
    d, _ = _ready_status(tmp_path, monkeypatch)
    assert _table(d, "kline_daily")["state"] == "fresh"
    assert _table(d, "kline_daily")["state_detail"] == "最新"
    assert _table(d, "valuation_daily")["state"] == "fresh", \
        f"gap=1 天应 fresh（brief：≥ 参考−1 天）: {_table(d, 'valuation_daily')}"
    t = _table(d, "index_daily")
    assert t["state"] == "lagging" and t["state_detail"] == "滞后 5 日", \
        f"max=today-5d 应 lagging'滞后 5 日': {t}"


def test_status_state_snapshot_master_dividend(tmp_path, monkeypatch):
    """快照表：master rows>0→fresh detail='快照'；dividend rows>0→fresh +
    detail 用**源 as_of**（progress T4 记录 2026-09-10，优先于表内 max(ann_date)）。"""
    d, _ = _ready_status(tmp_path, monkeypatch)
    m = _table(d, "stock_master")
    assert m["state"] == "fresh" and m["state_detail"] == "快照"
    dv = _table(d, "dividend_events")
    assert dv["state"] == "fresh"
    assert dv["state_detail"] == "快照截至2026-09-10", \
        f"dividend detail 应取 progress T4 as_of: {dv}"


def test_status_state_dividend_as_of_fallback_max_ann_date(tmp_path, monkeypatch):
    """dividend as_of 回退：progress 无 T4 记录 → 用表内 max(ann_date)（2026-08-31）。"""
    import lake.web_api as wapi
    from lake import conn as lconn

    p = str(tmp_path / "divfb.duckdb")
    _seed_v605_db(p)
    prog = _write_progress(tmp_path, tasks=[
        {"table": "kline_daily", "tier": "P3", "total": 0, "done": 0,
         "quota_used_today": 1, "quota_budget": 5000, "state": "running",
         "eta_min": None, "last_error": ""}])   # 无 dividend_events task
    monkeypatch.setattr(lconn, "default_db_path", lambda: p)
    monkeypatch.setattr(lconn, "progress_path", lambda: prog)
    d = wapi.status()
    assert _table(d, "dividend_events")["state_detail"] == "快照截至2026-08-31"


def test_status_state_pending_p2p3_even_with_rows(tmp_path, monkeypatch):
    """P2/P3 四张表 → pending（detail 逐字）；**factor_snapshot 有 2 行仍 pending**
    （计划内未做 ≠ 按行数判定——brief：即使 0 行也标 pending，反之亦然）。"""
    d, _ = _ready_status(tmp_path, monkeypatch)
    expect_detail = {
        "fundamentals_quarterly": "P2·季度基本面待补",
        "holders_snapshot": "P2·股东+实控人待补",
        "factor_snapshot": "依赖T5/T6后计算",
        "macro_rf": "P3·rf现值序列未启动",
    }
    for key, detail in expect_detail.items():
        t = _table(d, key)
        assert t["state"] == "pending", f"{key} 应 pending: {t}"
        assert t["state_detail"] == detail, f"{key} detail 不符: {t}"
    assert _table(d, "factor_snapshot")["rows"] == 2   # 有行仍 pending（前提成立）


def test_status_state_empty_defensive_branch(tmp_path, monkeypatch):
    """empty 防御分支：非 P2/P3 表 0 行 → empty"暂无数据"。

    kline_daily 清空后：kline 自身 rows=0 → empty；参考交易日回退 today（brief：
    无数据时回退今日）→ valuation(max=yesterday) gap=1 仍 fresh、index(today-5d)
    lagging——验证 ref 回退路径不崩。
    """
    import lake.web_api as wapi
    from lake import conn as lconn

    p = str(tmp_path / "empty.duckdb")
    _seed_v605_db(p)
    con = duckdb.connect(p)
    con.execute("DELETE FROM kline_daily")
    con.close()
    prog = _write_progress(tmp_path)
    monkeypatch.setattr(lconn, "default_db_path", lambda: p)
    monkeypatch.setattr(lconn, "progress_path", lambda: prog)
    d = wapi.status()
    assert _table(d, "kline_daily")["state"] == "empty"
    assert _table(d, "kline_daily")["state_detail"] == "暂无数据"
    # ref 回退 today：valuation gap=1 → fresh；index gap=5 → lagging
    assert _table(d, "valuation_daily")["state"] == "fresh"
    assert _table(d, "index_daily")["state"] == "lagging"


def test_classify_state_unit_four_branches():
    """_classify_state 单元级四分支（不依赖库）：fresh/lagging/pending/empty +
    边界 gap=0/1/2 + dividend as_of 缺失回退'全史静态导入'。"""
    from lake import web_api as wapi

    ref = dt.date.today().isoformat()
    today = dt.date.today()
    # fresh：gap 0 / 1
    assert wapi._classify_state("kline_daily", "daily", 10, ref, ref, None) == \
        ("fresh", "最新")
    y = (today - dt.timedelta(days=1)).isoformat()
    assert wapi._classify_state("valuation_daily", "daily", 10, y, ref, None)[0] == "fresh"
    # lagging：gap 2 → "滞后 2 日"
    t2 = (today - dt.timedelta(days=2)).isoformat()
    assert wapi._classify_state("index_daily", "daily", 10, t2, ref, None) == \
        ("lagging", "滞后 2 日")
    # pending：P2/P3 不论行数
    assert wapi._classify_state("macro_rf", "pending_p3", 0, None, ref, None) == \
        ("pending", "P3·rf现值序列未启动")
    assert wapi._classify_state("factor_snapshot", "pending_p2", 99, "2026-01-01",
                                ref, None)[0] == "pending"
    # empty：非 pending 表 0 行
    assert wapi._classify_state("kline_daily", "daily", 0, None, ref, None) == \
        ("empty", "暂无数据")
    # snapshot：master / dividend（as_of 有/无）
    assert wapi._classify_state("stock_master", "snapshot", 5, None, ref, None) == \
        ("fresh", "快照")
    assert wapi._classify_state("dividend_events", "snapshot", 5, None, ref,
                                "2026-09-10") == ("fresh", "快照截至2026-09-10")
    assert wapi._classify_state("dividend_events", "snapshot", 5, None, ref,
                                None) == ("fresh", "全史静态导入")


# ===========================================================================
# C. locked / uninitialized 向后兼容（新字段只追加在 ready 态）
# ===========================================================================
def test_status_locked_backward_compat_no_new_fields(tmp_path, monkeypatch):
    """locked 态响应体 = v6.0.4 键集**精确相等**：不得混入 tables/views/adj/db/sync
    （brief：锁被持有时保持降级路径不变，tables 数组可缺省）。"""
    import lake.web_api as wapi
    from lake import conn as lconn

    p = str(tmp_path / "locked.duckdb")
    with open(p, "wb") as f:
        f.write(b"\x00" * 16)
    prog = _write_progress(tmp_path)
    monkeypatch.setattr(lconn, "default_db_path", lambda: p)
    monkeypatch.setattr(lconn, "progress_path", lambda: prog)
    monkeypatch.setattr(
        lconn, "connect_existing",
        lambda path=None: (_ for _ in ()).throw(lconn.LakeLocked(p, 4321)))

    d = wapi.status()
    assert set(d.keys()) == {"installed", "duckdb_version", "initialized",
                             "backfill_in_progress", "lock_holder_pid",
                             "coverage", "tasks", "updated_at"}, \
        f"locked 态键集必须与 v6.0.4 精确一致: {sorted(d.keys())}"
    assert d["backfill_in_progress"] is True and d["lock_holder_pid"] == 4321


def test_status_uninitialized_backward_compat_no_new_fields(tmp_path, monkeypatch):
    """uninitialized 态（缺文件）响应体 = v6.0.3/v6.0.4 基准键集精确相等。"""
    import lake.web_api as wapi
    from lake import conn as lconn

    missing = str(tmp_path / "nope" / "lake.duckdb")
    monkeypatch.setattr(lconn, "default_db_path", lambda: missing)
    d = wapi.status()
    assert set(d.keys()) == {"installed", "duckdb_version", "initialized",
                             "error", "hint", "coverage", "tasks", "updated_at"}
    assert d["initialized"] is False and d["error"] == "lake_not_initialized"


# ===========================================================================
# D. HTTP 层（真 uvicorn + web/app.py 真实挂载）：ready 态新字段端到端
# ===========================================================================
def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _http_get(port: int, path: str):
    import http.client

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


def test_http_status_ready_new_fields_end_to_end(tmp_path):
    """HTTP 层 ready 态（tmp 库 + web/app.py 真实挂载）：/status 200 + tables 9 表 +
    views/db/sync/adj 齐全；旧字段同在（向后兼容端到端证据）。"""
    import json as _json

    p = str(tmp_path / "http_ready.duckdb")
    _seed_v605_db(p)
    prog = _write_progress(tmp_path)
    port = _free_port()
    code_lines = [
        f"import sys; sys.path.insert(0, {REPO_ROOT!r})",
        "from lake import conn as _lc",
        f"_lc.default_db_path = lambda: {p!r}",
        f"_lc.progress_path = lambda: {prog!r}",
        "import uvicorn",
        "from web.app import app",
        f"uvicorn.run(app, host='127.0.0.1', port={port}, log_level='error')",
    ]
    srv = subprocess.Popen([PY, "-c", "\n".join(code_lines)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        assert _wait_ready(port, "/api/lake/status"), "uvicorn 未在超时内就绪"
        s, b = _http_get(port, "/api/lake/status")
        assert s == 200, f"/status ready 应 200，实际 {s}"
        d = _json.loads(b)
        # 新字段
        assert [t["key"] for t in d["tables"]] == EXPECTED_TABLE_KEYS
        assert len(d["views"]) == 3
        assert d["adj_factor_coverage_pct"] == 50.0
        assert d["db"]["path"] == p and d["db"]["size_mb"] > 0
        assert d["sync"]["quota_used_today"] == 9 and d["sync"]["quota_budget"] == 5000
        # 旧字段（向后兼容）
        assert d["initialized"] is True and d["installed"] is True
        assert "coverage" in d and "tasks" in d and "updated_at" in d
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=10)
        except subprocess.TimeoutExpired:
            srv.kill()
            srv.wait(timeout=10)


# ===========================================================================
# E. 前端契约（离线断言 DOM/JS/CSS，防前后端字段脱节）
# ===========================================================================
def test_frontend_v605_dom_contract():
    """index.html 区块 C：新 DOM 三件套齐全、旧 #lake-coverage 已移除；
    app.js 渲染函数 + 四色徽章 class + state_detail 消费；style.css 对应样式。"""
    html = open(os.path.join(REPO_ROOT, "web", "static", "index.html"),
                encoding="utf-8").read()
    js = open(os.path.join(REPO_ROOT, "web", "static", "app.js"),
              encoding="utf-8").read()
    css = open(os.path.join(REPO_ROOT, "web", "static", "style.css"),
               encoding="utf-8").read()

    # index.html：新 DOM（汇总条/表清单/视图区）+ 保留 tasks 表与灌数中块
    for sel in ('id="lake-summary"', 'id="lake-tables"', 'id="lake-views"',
                'id="lake-tasks-table"', 'id="lake-backfill"'):
        assert sel in html, f"index.html 缺 {sel}"
    assert 'id="lake-coverage"' not in html, "旧 #lake-coverage 应已移除（v6.0.5 重设计）"

    # app.js：渲染函数 + 徽章 class + 新字段消费
    for fn in ("lakeRenderSummary", "lakeRenderTables", "lakeRenderViews"):
        assert f"function {fn}(" in js, f"app.js 缺 {fn}"
    for cls in ("lake-st-fresh", "lake-st-lagging", "lake-st-pending", "lake-st-empty"):
        assert cls in js and cls in css, f"徽章 class {cls} 前后端/CSS 脱节"
    for field in ("state_detail", "adj_factor_coverage_pct", "size_mb",
                  "quota_used_today", "last_sync_at"):
        assert field in js, f"app.js 未消费新字段 {field}"
    assert "lake-row-muted" in js and "lake-row-muted" in css, \
        "零数据表 muted 弱化 class 前后端/CSS 脱节"
    # v6.0.4 三态契约不回退（AC3）：灌数中块/未初始化文案/error 识别仍在
    assert "backfill_in_progress" in js and "lock_holder_pid" in js
    assert "lake_backfill_in_progress" in js and "数据灌入中" in html
