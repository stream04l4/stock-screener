# -*- coding: utf-8 -*-
"""test_lake_d4_concurrency —— v6.0.1 D-4 并发回归（全离线，零网络/零真实数据源）。

缺陷：lake.web_api ``_con()`` 原返回 lake.conn.get_conn() **进程级单例**，
FastAPI 默认线程池下多个 /api/lake/* 请求并发 execute 同一 Connection 对象 →
结果集交错（/industries 返回 market 的行、列名被套上 industries 别名）/空响应/
HTTP 500。tester 量化：3 并发 ×40 轮 → 40/40 损坏；TL 独立复现 1/40 correct。

修复（TL 拍板方案 a）：每请求短连接——``conn.connect_existing()`` 仅
duckdb.connect（**不执行 init_schema**），端点统一 ``with _con() as con:``
保证用完即 close。本文件回归两件事：
1. **单元级**：connect_existing 不跑 DDL、库缺失抛错（由 _con 转 503）、
   同一文件多短连接并发读互不干扰；
2. **端到端**（与 tester/TL 同口径）：tmp fixture 库 + uvicorn 真起 web.app
   （随机端口），3 线程并发 status+industries+market ×40 轮，断言每轮
   /industries codes 集合精确相等、无 500；/market 行结构正确（ts_code 列必须
   是 ts_code 值而非别的查询的列——WRONG-CODES 交错的直接判据）。

fixture：6 股 × C39/J66 两行业 + valuation_daily（与 tester diag / TL 核验同构）。
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time

import pytest

duckdb = pytest.importorskip("duckdb")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable or os.path.join(REPO_ROOT, ".venv", "bin", "python")

EXPECTED_INDUSTRIES = {"C39", "J66"}


# ---------------------------------------------------------------------------
# 单元级：connect_existing 行为
# ---------------------------------------------------------------------------
def test_connect_existing_no_init_schema(tmp_path):
    """仅 duckdb.connect，不执行 init_schema（空文件库无表；不触碰单例）。"""
    from lake import conn as lconn

    db = str(tmp_path / "empty.duckdb")
    before = lconn._conn  # 套件中其他测试可能已设置单例——只断言"不被本函数改动"
    con = lconn.connect_existing(db)
    try:
        # 空文件库：无任何表 → 证明没跑 init_schema（open() 会建 9 表）
        tables = [r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='main'").fetchall()]
        assert tables == [], f"connect_existing 不应初始化 schema: {tables}"
    finally:
        con.close()
    # 不触碰进程级单例（写路径隔离）：调用前后必须是同一对象（含同为 None）
    assert lconn._conn is before, "connect_existing 不得设置/读取单例 _conn"


def test_connect_existing_missing_db_raises(tmp_path):
    """文件不存在 → duckdb 抛错（web_api._con 据此转 503）。"""
    from lake import conn as lconn

    with pytest.raises(Exception):
        con = lconn.connect_existing(str(tmp_path / "nope" / "missing.duckdb"))
        try:
            con.execute("SELECT 1")
        finally:
            con.close()


def test_connect_existing_per_request_isolation():
    """两次 connect_existing 返回不同 Connection 对象（每请求独立，非单例）。"""
    from lake import conn as lconn

    c1 = lconn.connect_existing(":memory:")
    c2 = lconn.connect_existing(":memory:")
    try:
        assert c1 is not c2, "必须每请求新开连接，不得复用同一对象"
    finally:
        c1.close()
        c2.close()


def test_multi_short_conn_concurrent_reads(tmp_path):
    """同一文件库多个短连接跨线程并发读（不同 SQL）→ 结果互不交错。

    D-4 根因隔离口径：无 HTTP，纯"多 Connection 并发 execute"。修复前用**同一**
    Connection 对象并发 execute 会交错；每请求独立短连接后必须全部正确。
    """
    from lake import conn as lconn
    from lake.ddl import init_schema

    db = str(tmp_path / "lake.duckdb")
    con = duckdb.connect(db)
    init_schema(con)
    for i in range(6):
        ind = "J66" if i % 2 == 0 else "C39"
        con.execute(
            "INSERT INTO stock_master (ts_code,name,industry_csric2,board,is_st,"
            "source,fetched_at,data_version) VALUES "
            "(?, ?, ?, '主板', 0, 't', '2026-09-14 00:00:00', 'v6.0')",
            [f"sh.{700000 + i}", f"股票{i:02d}", ind])
        con.execute(
            "INSERT INTO valuation_daily (ts_code,date,total_mv) VALUES (?, '2026-09-14', ?)",
            [f"sh.{700000 + i}", 1e9 * (i + 1)])
    con.close()

    results = {}
    errors = []

    def run(name, fn):
        try:
            results[name] = fn()
        except Exception as e:  # noqa: BLE001
            errors.append((name, repr(e)))

    def q_industries():
        c = lconn.connect_existing(db)
        try:
            rows = c.execute(
                "SELECT industry_csric2 FROM stock_master "
                "WHERE industry_csric2 IS NOT NULL GROUP BY 1 ORDER BY 1").fetchall()
            return sorted(r[0] for r in rows)
        finally:
            c.close()

    def q_market_codes():
        c = lconn.connect_existing(db)
        try:
            rows = c.execute(
                "SELECT m.ts_code FROM stock_master m LEFT JOIN valuation_daily v "
                "ON v.ts_code=m.ts_code AND v.date=(SELECT MAX(date) FROM valuation_daily) "
                "ORDER BY v.total_mv DESC").fetchall()
            return [r[0] for r in rows]
        finally:
            c.close()

    threads = []
    for i in range(30):
        threads.append(threading.Thread(target=run, args=(f"ind{i}", q_industries)))
        threads.append(threading.Thread(target=run, args=(f"mkt{i}", q_market_codes)))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发短连接读出错: {errors[:3]}"
    for k, v in results.items():
        if k.startswith("ind"):
            assert v == ["C39", "J66"], f"{k} 结果交错/损坏: {v}"
        else:
            assert len(v) == 6 and all(x.startswith("sh.7") for x in v), \
                f"{k} market 行结构错乱（疑似别的查询列）: {v[:4]}"


# ---------------------------------------------------------------------------
# 端到端：uvicorn 真起 web.app + 3 线程并发 ×40 轮（tester/TL 同口径）
# ---------------------------------------------------------------------------
def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


_SERVER_CODE_TEMPLATE = r"""
import sys
sys.path.insert(0, {repo!r})
from lake import conn as _lc
# 重定向默认库路径到 tmp fixture（只改 Web 读路径用的 default_db_path；
# connect_existing() 每次调用现取该值，故无需预开单例）
_lc.default_db_path = lambda: {db!r}
import uvicorn
from web.app import app
uvicorn.run(app, host="127.0.0.1", port={port}, log_level="error")
"""


def _build_fixture(db_path: str) -> None:
    """tmp fixture：6 股 × C39/J66 + valuation_daily（与 tester/TL 同构）。"""
    from lake import conn as lconn
    from lake.ddl import init_schema

    con = duckdb.connect(db_path)
    init_schema(con)
    for i in range(6):
        ind = "J66" if i % 2 == 0 else "C39"
        tc = f"sh.{700001 + i}"
        con.execute(
            "INSERT INTO stock_master (ts_code,name,industry_csric2,industry_name,"
            "board,is_st,source,fetched_at,data_version) VALUES "
            "(?, ?, ?, ?, '主板', 0, 't', '2026-09-14 00:00:00', 'v6.0')",
            [tc, f"d4股{i + 1:02d}", ind, "行业" + ind])
        con.execute(
            "INSERT INTO valuation_daily (ts_code,date,total_mv) VALUES (?, '2026-09-14', ?)",
            [tc, 1e9 * (i + 1)])
    n = con.execute("SELECT count(*) FROM stock_master").fetchone()[0]
    assert n == 6
    con.close()


def _http_get(port: int, path: str):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        c.request("GET", path)
        r = c.getresponse()
        return r.status, r.read().decode()
    except Exception as e:  # noqa: BLE001
        return "EXC", str(e)
    finally:
        c.close()


def test_d4_concurrent_endpoints_no_interleaving(tmp_path):
    """3 线程并发 status+industries+market ×40 轮：/industries 每轮 codes 精确相等，
    无 EMPTY/WRONG-CODES/500；/market 行必须带合法 ts_code（交错判据）。"""
    port = _free_port()
    db = str(tmp_path / "lake.duckdb")
    _build_fixture(db)

    server_code = _SERVER_CODE_TEMPLATE.format(repo=REPO_ROOT, db=db, port=port)
    srv = subprocess.Popen([PY, "-c", server_code],
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        ready = False
        for _ in range(60):
            if srv.poll() is not None:
                err = srv.stderr.read().decode(errors="replace") if srv.stderr else ""
                raise AssertionError(f"uvicorn 提前退出 rc={srv.returncode}: {err[-500:]}")
            try:
                s, _ = _http_get(port, "/api/lake/status")
                if s == 200:
                    ready = True
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.5)
        assert ready, "uvicorn 未在超时内就绪"

        dist = {"correct": 0, "EMPTY": 0, "WRONG": 0, "HTTP500": 0, "OTHER": 0}
        market_bad = 0
        samples = []

        for i in range(40):
            res = {}
            barrier = threading.Barrier(3)

            def w(name, path):
                barrier.wait()
                res[name] = _http_get(port, path)

            ts = [threading.Thread(target=w, args=("status", "/api/lake/status")),
                  threading.Thread(target=w, args=("industries", "/api/lake/industries")),
                  threading.Thread(target=w, args=("market", "/api/lake/market?page=1&sort=code"))]
            for t in ts:
                t.start()
            for t in ts:
                t.join()

            # --- /industries 判定（tester 口径：codes 集合精确相等）---
            s, b = res["industries"]
            if s == 200:
                try:
                    d = json.loads(b)
                    codes = {x["code"] for x in d.get("industries", [])}
                    if codes == EXPECTED_INDUSTRIES:
                        dist["correct"] += 1
                    elif not d.get("industries"):
                        dist["EMPTY"] += 1
                    else:
                        dist["WRONG"] += 1
                        samples.append(f"round{i} WRONG-CODES {sorted(codes)[:4]} :: {b[:120]}")
                except Exception:  # noqa: BLE001
                    dist["OTHER"] += 1
                    samples.append(f"round{i} UNPARSEABLE :: {b[:120]}")
            elif s == 500:
                dist["HTTP500"] += 1
                samples.append(f"round{i} HTTP500 :: {b[:120]}")
            else:
                dist["OTHER"] += 1
                samples.append(f"round{i} status={s} :: {str(b)[:120]}")

            # --- /market 结构判定（WRONG-CODES 交错时列名被套，ts_code 值会消失）---
            ms, mb = res["market"]
            if ms == 200:
                try:
                    md = json.loads(mb)
                    rows = md.get("rows", [])
                    if not (len(rows) == 6 and all(
                            str(r.get("ts_code", "")).startswith("sh.7") for r in rows)):
                        market_bad += 1
                        samples.append(f"round{i} MARKET-BAD :: {mb[:120]}")
                except Exception:  # noqa: BLE001
                    market_bad += 1
                    samples.append(f"round{i} MARKET-UNPARSEABLE :: {mb[:120]}")
            elif ms == 500:
                dist["HTTP500"] += 1
                samples.append(f"round{i} MARKET-HTTP500 :: {mb[:120]}")

        assert dist["correct"] == 40, \
            f"/industries 并发损坏（D-4 未修复）: dist={dist}\nsamples: {'; '.join(samples[:5])}"
        assert market_bad == 0, f"/market 行结构错乱: {market_bad}/40\nsamples: {'; '.join(samples[:5])}"
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=10)
        except subprocess.TimeoutExpired:
            srv.kill()
            srv.wait(timeout=10)
