# -*- coding: utf-8 -*-
"""test_lake_v601_bugfixes —— v6.0.1 bugfix 批次回归（D-1/D-2/D-3，全离线）。

- D-1：``lake.lake_conn(显式路径)`` 曾调用不存在的 ``lake.conn.init_schema``
  → 必抛 AttributeError。回归：真实调用成功 + schema 已初始化（9 表可查）。
- D-2：``export_parquet`` 对同一目标目录二次导出曾抛
  ``IOException: Directory ... is not empty! Enable OVERWRITE``。
  回归：同目录二次导出成功，且读回内容一致（幂等覆盖语义）。
- D-3：区块B 行业下拉无动态填充。端点侧：新增 ``GET /api/lake/industries``
  （distinct industry_csric2 + 名称映射）——直接调用 handler（monkeypatch _con，
  零网络）断言正常/空库两态 + 路由已注册；前端侧：node 最小 DOM stub 真实执行
  ``loadLakeIndustries`` 三态（正常/空/错误），断言下拉 innerHTML。
"""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

duckdb = pytest.importorskip("duckdb")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# D-1：lake_conn(显式路径) 真实调用不抛 AttributeError，schema 已初始化
# ---------------------------------------------------------------------------
def test_d1_lake_conn_explicit_path(tmp_path):
    """lake.lake_conn(tmp 路径) 成功返回可用连接（D-1 回归：原必抛 AttributeError）。"""
    import lake

    db = str(tmp_path / "sub" / "test.duckdb")  # 目录不存在 → 验证 makedirs 路径也走通
    con = lake.lake_conn(db)
    assert con is not None, "duckdb 已装时 lake_conn(显式路径) 不得返回 None"
    try:
        # schema 必须已初始化（conn.open 内部 init_schema）：9 表齐全
        from lake.ddl import TABLES

        tables = {r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='main' AND table_type='BASE TABLE'").fetchall()}
        missing = [t for t in TABLES if t not in tables]
        assert not missing, f"显式路径连接缺表: {missing}"
        # 可写可用：插一行 T1 再读回
        con.execute(
            "INSERT INTO stock_master (ts_code,name,industry_csric2,board,is_st,"
            "soe_flag,source,fetched_at,data_version) VALUES "
            "('sh.601398','工商银行','J66','主板',0,'央国企','t','2026-09-14 00:00:00','v6.0')")
        r = con.execute("SELECT name FROM stock_master WHERE ts_code='sh.601398'").fetchone()
        assert r[0] == "工商银行"
    finally:
        con.close()


# ---------------------------------------------------------------------------
# D-2：export_parquet 同目录二次导出幂等（OVERWRITE）
# ---------------------------------------------------------------------------
def test_d2_export_parquet_second_run_idempotent(tmp_path):
    """同目标目录二次 export：不抛 IOException，且读回内容一致。"""
    from lake import conn as lconn
    from lake.ddl import init_schema

    db = str(tmp_path / "lake.duckdb")
    con = duckdb.connect(db)
    init_schema(con)
    con.execute(
        "INSERT INTO kline_daily (ts_code,date,close,adj_factor) VALUES "
        "('sh.601398','2026-05-12',4.0,2.452626),('sh.601398','2026-05-13',4.1,2.5545)")

    out_dir = str(tmp_path / "parquet")
    t1 = lconn.export_parquet("kline_daily", out_dir, con=con)
    # 二次导出（目标目录此时非空）——D-2 修复前此处必抛
    # "IOException: Directory ... is not empty! Enable OVERWRITE"
    t2 = lconn.export_parquet("kline_daily", out_dir, con=con)
    assert t1 == t2

    def readback():
        return sorted(con.execute(
            f"SELECT ts_code, CAST(date AS VARCHAR), close FROM "
            f"read_parquet('{os.path.join(out_dir,'kline_daily','**','*.parquet')}', "
            "HIVE_PARTITIONING=1)").fetchall())

    r1 = readback()
    assert len(r1) == 2
    # 再导一次后内容仍一致（覆盖而非追加/翻倍）
    lconn.export_parquet("kline_daily", out_dir, con=con)
    r3 = readback()
    assert r3 == r1, f"二次导出后内容漂移: {r3} != {r1}"
    con.close()


# ---------------------------------------------------------------------------
# D-3（端点侧）：GET /api/lake/industries —— 直接调 handler，零网络
# ---------------------------------------------------------------------------
def _make_industries_con(rows):
    """构造带 stock_master 数据的 :memory: 连接（rows=[(code,name),...]）。"""
    from lake.ddl import init_schema

    con = duckdb.connect(":memory:")
    init_schema(con)
    for code, name in rows:
        con.execute(
            "INSERT INTO stock_master (ts_code,name,industry_csric2,industry_name,"
            "board,is_st,source,fetched_at,data_version) VALUES "
            "(?,?,?,?, '主板',0,'t','2026-09-14 00:00:00','v6.0')",
            [f"sh.{code}", f"股票{code}", code, name])
    return con


def test_d3_industries_endpoint_populated(monkeypatch):
    """有数据：distinct code + 名称映射，按 code 升序；空 code/NULL 被排除。"""
    import lake.web_api as wapi

    con = _make_industries_con([("J66", "货币金融服务"), ("C39", "计算机、通信和其他电子设备制造业")])
    monkeypatch.setattr(wapi, "_con", lambda: con)
    try:
        d = wapi.industries()
        assert [i["code"] for i in d["industries"]] == ["C39", "J66"], \
            f"应按 code 升序: {d['industries']}"
        by_code = {i["code"]: i["name"] for i in d["industries"]}
        assert by_code["J66"] == "货币金融服务"
        assert by_code["C39"] == "计算机、通信和其他电子设备制造业"
    finally:
        con.close()

    # 路由必须已注册（FastAPI router 上可查到 /api/lake/industries）
    paths = {getattr(r, "path", None) for r in wapi.router.routes}
    assert "/api/lake/industries" in paths, f"路由未注册: {sorted(p for p in paths if p)}"


def test_d3_industries_endpoint_empty(monkeypatch):
    """空库：返回 {"industries": []}（前端保持"全部行业"占位 + 空态文案）。"""
    import lake.web_api as wapi

    con = _make_industries_con([])
    monkeypatch.setattr(wapi, "_con", lambda: con)
    try:
        d = wapi.industries()
        assert d == {"industries": []}
    finally:
        con.close()


# ---------------------------------------------------------------------------
# D-3（前端侧）：node 最小 DOM stub 真实执行 loadLakeIndustries 三态
# ---------------------------------------------------------------------------
_NODE_DOM_TEST = r"""
"use strict";
// 从 app.js 提取 loadLakeIndustries 函数源码（到下一个顶层 async function 为止）
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const start = src.indexOf("async function loadLakeIndustries()");
if (start < 0) { console.error("FAIL: loadLakeIndustries not found in app.js"); process.exit(2); }
const end = src.indexOf("\nasync function", start + 1);
const fnSrc = src.slice(start, end > 0 ? end : undefined);

// ---- 最小 DOM stub（只覆盖本函数用到的 API）----
function makeSelect() {
  return { value: "", disabled: false, _html: "",
    set innerHTML(v) { this._html = v; }, get innerHTML() { return this._html; } };
}
function makeHint() {
  const h = { textContent: "", _hidden: true,
    classList: { hidden: true,
      add(c) { if (c === "hidden") this.hidden = true; },
      remove(c) { if (c === "hidden") this.hidden = false; } } };
  return h;
}
let sel, hint, apiImpl, errors = [];
global.$ = (s) => s === "#lake-industry-filter" ? sel : s === "#lake-industry-hint" ? hint : null;
global.esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
global.api = (p) => apiImpl(p);
global.lakeSetError = (m) => errors.push(m);

(async () => {
  // ---- 态1：正常（2 个行业）----
  sel = makeSelect(); hint = makeHint();
  apiImpl = async () => ({ industries: [
    { code: "C39", name: "计算机、通信和其他电子设备制造业" },
    { code: "J66", name: "货币金融服务" } ] });
  await eval(fnSrc + "; loadLakeIndustries()");
  let opts = [...sel._html.matchAll(/<option value="([^"]*)">([^<]*)<\/option>/g)].map(m => [m[1], m[2]]);
  if (opts.length !== 3 || opts[0][0] !== "" || opts[0][1] !== "全部行业") { console.error("FAIL state1 options:", JSON.stringify(opts)); process.exit(1); }
  if (opts[1][0] !== "C39" || !opts[1][1].startsWith("C39 ")) { console.error("FAIL state1 C39:", JSON.stringify(opts[1])); process.exit(1); }
  if (opts[2][0] !== "J66" || opts[2][1] !== "J66 货币金融服务") { console.error("FAIL state1 J66:", JSON.stringify(opts[2])); process.exit(1); }
  if (!hint.classList.hidden) { console.error("FAIL state1: hint should stay hidden"); process.exit(1); }

  // ---- 态2：空库（保持占位 + 空态文案）----
  sel = makeSelect(); hint = makeHint();
  apiImpl = async () => ({ industries: [] });
  await eval(fnSrc + "; loadLakeIndustries()");
  opts = [...sel._html.matchAll(/<option value="([^"]*)">([^<]*)<\/option>/g)].map(m => [m[1], m[2]]);
  if (opts.length !== 1 || opts[0][1] !== "全部行业") { console.error("FAIL state2 options:", JSON.stringify(opts)); process.exit(1); }
  if (hint.classList.hidden || hint.textContent !== "（暂无行业数据）") { console.error("FAIL state2 hint:", hint.textContent, hint.classList.hidden); process.exit(1); }

  // ---- 态3：错误（503 降级：红横幅 + 回退占位，select 不永久禁用）----
  sel = makeSelect(); hint = makeHint();
  apiImpl = async () => { const e = new Error("数据湖不可用：duckdb 未安装"); e.status = 503; throw e; };
  await eval(fnSrc + "; loadLakeIndustries()");
  opts = [...sel._html.matchAll(/<option value="([^"]*)">([^<]*)<\/option>/g)].map(m => [m[1], m[2]]);
  if (opts.length !== 1 || opts[0][1] !== "全部行业") { console.error("FAIL state3 options:", JSON.stringify(opts)); process.exit(1); }
  if (errors.length !== 1) { console.error("FAIL state3: error banner not raised:", errors); process.exit(1); }
  if (sel.disabled) { console.error("FAIL state3: select left disabled"); process.exit(1); }

  // ---- 态4：保留已有选择（prev 仍存在时）----
  sel = makeSelect(); sel.value = "J66"; hint = makeHint();
  apiImpl = async () => ({ industries: [
    { code: "C39", name: "电子设备" }, { code: "J66", name: "货币金融服务" } ] });
  await eval(fnSrc + "; loadLakeIndustries()");
  if (sel.value !== "J66") { console.error("FAIL state4: prev selection lost:", sel.value); process.exit(1); }

  console.log("DOM_OK state1=populated state2=empty-hint state3=error-fallback state4=keep-prev");
})().catch((e) => { console.error("FAIL uncaught:", e.message); process.exit(1); });
"""


def test_d3_frontend_dropdown_states():
    """node 真实执行 app.js::loadLakeIndustries（DOM stub）：正常/空/错误/保留选择。"""
    node = shutil.which("node")
    if not node:
        pytest.skip("node 不可用（前端 DOM 断言跳过；端点侧用例仍覆盖 D-3 API）")
    script = "/tmp/_lake_d3_dom_test.js"
    with open(script, "w", encoding="utf-8") as f:
        f.write(_NODE_DOM_TEST)
    app_js = os.path.join(REPO_ROOT, "web", "static", "app.js")
    r = subprocess.run([node, script, app_js], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"node DOM 测试失败:\nstdout={r.stdout}\nstderr={r.stderr}"
    assert "DOM_OK" in r.stdout


def test_d3_index_html_has_hint_anchor():
    """index.html 区块B 存在空态文案锚点（#lake-industry-hint，muted small hidden）。"""
    html = open(os.path.join(REPO_ROOT, "web", "static", "index.html"), encoding="utf-8").read()
    assert 'id="lake-industry-filter"' in html
    assert 'id="lake-industry-hint" class="muted small hidden"' in html
