# -*- coding: utf-8 -*-
"""test_lake_v612_review_fixes —— v6.1.2 数据湖页复查修复（P0-P3）回归。

覆盖 TL 复查拍板"都修了吧"的后端行为 + 前端契约：
- **P1-A** /kline 复权切换：adjust=none|qfq|hfq；非法→400；adj_factor 全 NULL→raw+note、
  部分 NULL→只用可得行+note、全非 NULL→复权无 note；**adjust=none 键集逐字节不变**
  （三态契约红线，test_lake_v606 精确断言的 {ts_code,name,rows,count} + 每行 6 列）。
- **P2-B** /stock dividends_recent：近 5 自然年（ex_date>=today-5y）+ PIT（ex_date<=today）、
  ex_date 降序、上限 12、dividend_yield_pct=cash_dps/close*100（close 缺→null）、空→[]；
  键只在 ready 态出现（stock_detail 仅 ready 可达，locked/uninitialized 上游已 409）。
- **前端契约**（离线断言 Vue 源码，防前后端脱节）：P0 行业只显示 industry_name、
  P1-A 复权三态按钮+adjust_note+标题动态、P2-B 分红小表 #lake-dividends-table、
  P1-B tasks 表灌数中不渲染（v6.1.4 O3 已反转为仅 backfill_in_progress 渲染）、
  P2-A 琥珀块总进度行、P3 market 跳页输入框。

纪律：库一律 tmp_path（**绝不触碰 data/lake/ 生产库**，p0 灌数运行中），离线；
分红日期用相对 today() 计算（不硬编码具体日期，防测试随时间过期）。
"""
from __future__ import annotations

import datetime as dt
import os
import sys

import pytest

duckdb = pytest.importorskip("duckdb")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


# ===========================================================================
# helpers（tmp 库播种）
# ===========================================================================
def _patch_db(monkeypatch, db_path: str) -> None:
    from lake import conn as lconn

    monkeypatch.setattr(lconn, "default_db_path", lambda: db_path)


def _seed_master(db_path: str, code: str = "sh.601398") -> None:
    """建 schema + 播种一行 stock_master（就绪库）。"""
    from lake import conn as lconn

    con = lconn.open(db_path)
    con.execute(
        "INSERT INTO stock_master (ts_code,name,industry_csric2,industry_name,"
        "board,is_st,source,fetched_at,data_version) VALUES "
        "('sh.601398','工商银行','J66','J66货币金融服务','主板',0,'t',"
        "'2026-09-14 00:00:00','v6.0')")
    con.close()


def _seed_kline(db_path: str, code: str = "sh.601398", adj=None) -> None:
    """播种 kline_daily 5 行（date 乱序插入，验证升序）。adj=可迭代 5 个因子或 None。

    - adj is None → 全部 adj_factor=NULL（全 NULL 降级场景）；
    - adj 为 list → 逐行赋因子（部分/全非 NULL 场景）。
    """
    from lake import conn as lconn

    con = duckdb.connect(db_path)
    rows = [
        ("2026-09-15", 10.4, 10.6, 10.3, 10.5, 1200),
        ("2026-09-11", 10.0, 10.2, 9.9, 10.1, 900),
        ("2026-09-14", 10.3, 10.5, 10.2, 10.4, 1100),
        ("2026-09-10", 10.1, 10.3, 10.0, 10.2, 1000),
        ("2026-09-13", 10.2, 10.4, 10.1, 10.0, 950),
    ]
    for i, (d, o, h, l, c, v) in enumerate(rows):
        af = adj[i] if adj is not None else None
        con.execute(
            "INSERT INTO kline_daily (ts_code,date,open,high,low,close,volume,"
            "adj_factor,source,fetched_at,data_version) VALUES "
            "(?,?,?,?,?,?,?,?,'t','2026-09-15 12:00:00','v6.0')",
            [code, d, o, h, l, c, v, af])
    con.close()


# ===========================================================================
# P1-A：/kline 复权切换
# ===========================================================================
def test_kline_adjust_none_default_keyset_byte_identical(tmp_path, monkeypatch):
    """adjust=none（缺省）→ 键集与 v6.1.1 **逐字节不变**：顶层 {ts_code,name,rows,count}、
    每行 6 列（无 adj_factor）、无 adjust_note。三态契约红线（test_lake_v606 同口径）。"""
    import lake.web_api as wapi

    p = str(tmp_path / "adj_none.duckdb")
    _seed_master(p)
    _seed_kline(p, adj=[1.0, 1.0, 1.0, 1.0, 1.0])   # 有因子，但 none 不用
    _patch_db(monkeypatch, p)

    d = wapi.stock_kline("sh.601398")               # 不传 adjust → 缺省 none
    assert set(d.keys()) == {"ts_code", "name", "rows", "count"}, f"none 键集被破坏: {d.keys()}"
    assert "adjust_note" not in d, "none 态不得带 adjust_note"
    for r in d["rows"]:
        assert set(r.keys()) == {"date", "open", "high", "low", "close", "volume"}, \
            f"none 行键集被破坏（不得含 adj_factor）: {r.keys()}"
    # none = raw 原值（不乘因子）
    first = d["rows"][0]
    assert first["close"] == 10.2   # 2026-09-10 raw close


def test_kline_adjust_invalid_400(tmp_path, monkeypatch):
    """非法 adjust → 400（brief：非法值→400，显式拒绝不静默回退）。"""
    import lake.web_api as wapi

    p = str(tmp_path / "adj_bad.duckdb")
    _seed_master(p)
    _seed_kline(p)
    _patch_db(monkeypatch, p)
    for bad in ("raw", "qf", "HFQX", "", "  ", "all"):
        with pytest.raises(wapi.HTTPException) as ei:
            wapi.stock_kline("sh.601398", adjust=bad)
        assert ei.value.status_code == 400, f"{bad!r} 应 400: {ei.value.status_code}"


def test_kline_adjust_hfq_and_qfq_math(tmp_path, monkeypatch):
    """全非 NULL：hfq=raw×af、qfq=raw×(af/max_af)；最新一根 qfq close=真实价（A股惯例）。"""
    import lake.web_api as wapi

    p = str(tmp_path / "adj_full.duckdb")
    _seed_master(p)
    # 5 行因子（升序对应 date 升序：09-10→0.8, 09-11→0.9, 09-13→0.95, 09-14→1.0, 09-15→1.2）
    # 乱序插入（_seed_kline 固定顺序），因子按 rows 顺序给：
    #   rows[0]=09-15 af=1.2 / rows[1]=09-11 af=0.9 / rows[2]=09-14 af=1.0 /
    #   rows[3]=09-10 af=0.8 / rows[4]=09-13 af=0.95
    _seed_kline(p, adj=[1.2, 0.9, 1.0, 0.8, 0.95])
    _patch_db(monkeypatch, p)

    dh = wapi.stock_kline("sh.601398", adjust="hfq")
    assert "adjust_note" not in dh, f"全非 NULL 不得带 note: {dh.get('adjust_note')}"
    by_date = {r["date"]: r for r in dh["rows"]}
    # hfq close = raw × af：09-15 raw 10.5×1.2=12.6；09-10 raw 10.2×0.8=8.16
    assert abs(by_date["2026-09-15"]["close"] - 10.5 * 1.2) < 1e-9
    assert abs(by_date["2026-09-10"]["close"] - 10.2 * 0.8) < 1e-9

    dq = wapi.stock_kline("sh.601398", adjust="qfq")
    assert "adjust_note" not in dq
    by_date_q = {r["date"]: r for r in dq["rows"]}
    max_af = 1.2   # 窗口内 max(adj_factor)
    # qfq close = raw × (af/max_af)：最新 09-15 → 10.5×(1.2/1.2)=10.5（真实价）
    assert abs(by_date_q["2026-09-15"]["close"] - 10.5) < 1e-9, "qfq 最新价必须=真实价"
    # 09-10 → 10.2×(0.8/1.2)=6.8
    assert abs(by_date_q["2026-09-10"]["close"] - 10.2 * (0.8 / 1.2)) < 1e-9


def test_kline_adjust_all_null_degrades_raw_plus_note(tmp_path, monkeypatch):
    """adj_factor 全 NULL（history 未灌完）→ 返回 raw rows + adjust_note（brief 降级纪律）。"""
    import lake.web_api as wapi

    p = str(tmp_path / "adj_allnull.duckdb")
    _seed_master(p)
    _seed_kline(p, adj=None)   # 全 NULL
    _patch_db(monkeypatch, p)

    for adjust in ("qfq", "hfq"):
        d = wapi.stock_kline("sh.601398", adjust=adjust)
        assert d["adjust_note"] == wapi._ADJUST_NOTE, f"{adjust} 全 NULL 必须带 note"
        # raw rows（不乘因子）+ 键集仍 4 顶层键 + 每行 6 列
        assert set(d.keys()) == {"ts_code", "name", "rows", "count", "adjust_note"}
        for r in d["rows"]:
            assert set(r.keys()) == {"date", "open", "high", "low", "close", "volume"}
        by_date = {r["date"]: r for r in d["rows"]}
        assert by_date["2026-09-15"]["close"] == 10.5   # raw 原值


def test_kline_adjust_partial_null_uses_available_rows_plus_note(tmp_path, monkeypatch):
    """部分 NULL → **只用可得行**（adj_factor 非 NULL 的行，已复权）+ adjust_note。"""
    import lake.web_api as wapi

    p = str(tmp_path / "adj_partial.duckdb")
    _seed_master(p)
    # rows[0]=09-15 af=1.2 / rows[3]=09-10 af=0.8 有因子；其余 3 行 NULL → 应被剔除
    _seed_kline(p, adj=[1.2, None, None, 0.8, None])
    _patch_db(monkeypatch, p)

    d = wapi.stock_kline("sh.601398", adjust="hfq")
    assert d["adjust_note"] == wapi._ADJUST_NOTE
    dates = [r["date"] for r in d["rows"]]
    # 只保留有因子的 2 行（09-15 / 09-10），升序
    assert dates == ["2026-09-10", "2026-09-15"], f"部分 NULL 应只用可得行: {dates}"
    by_date = {r["date"]: r for r in d["rows"]}
    assert abs(by_date["2026-09-15"]["close"] - 10.5 * 1.2) < 1e-9
    assert abs(by_date["2026-09-10"]["close"] - 10.2 * 0.8) < 1e-9


def test_kline_adjust_locked_still_409(tmp_path, monkeypatch):
    """locked（灌数持锁）→ 仍 LakeBackfillInProgress(409)，不因新参数改变错误语义。"""
    import lake.web_api as wapi
    from lake import conn as lconn

    p = str(tmp_path / "adj_locked.duckdb")
    with open(p, "wb") as f:
        f.write(b"\x00" * 16)
    _patch_db(monkeypatch, p)
    monkeypatch.setattr(
        lconn, "connect_existing",
        lambda path=None: (_ for _ in ()).throw(lconn.LakeLocked(p, 4321)))
    with pytest.raises(wapi.LakeBackfillInProgress) as ei:
        wapi.stock_kline("sh.601398", adjust="qfq")
    assert ei.value.status_code == 409


# ===========================================================================
# P2-B：/stock dividends_recent（近 5 年分红小表）
# ===========================================================================
def _seed_dividends(db_path: str, code: str = "sh.601398",
                    ex_dates=None, dps_map=None, closes=None) -> None:
    """播种 dividend_events + 对应 kline_daily（供 yield 计算）。

    :param ex_dates: list[str]（YYYY-MM-DD）除权日。
    :param dps_map: {ex_date: cash_dps}；缺省全 1.0。
    :param closes: {ex_date: close}；未给的 ex_date 无 kline 行 → yield=null。
    """
    from lake import conn as lconn

    con = duckdb.connect(db_path)
    for ed in ex_dates or []:
        dps = (dps_map or {}).get(ed, 1.0)
        con.execute(
            "INSERT INTO dividend_events (ts_code,ex_date,ann_date,period,cash_dps,"
            "source,fetched_at,data_version) VALUES "
            "(?,?,?,?,?,'t','2026-09-15 12:00:00','v6.0')",
            [code, ed, ed, "P", dps])
        if closes and ed in closes:
            con.execute(
                "INSERT INTO kline_daily (ts_code,date,open,high,low,close,volume,"
                "source,fetched_at,data_version) VALUES "
                "(?,?,?,?,?,?,?,'t','2026-09-15 12:00:00','v6.0')",
                [code, ed, closes[ed], closes[ed] * 1.01, closes[ed] * 0.99,
                 closes[ed], 100])
    con.close()


def test_dividends_recent_window_pit_order_and_yield(tmp_path, monkeypatch):
    """近 5 自然年窗口 + PIT（ex_date<=today）+ ex_date 降序 + yield=cash_dps/close*100。"""
    import lake.web_api as wapi

    p = str(tmp_path / "div1.duckdb")
    _seed_master(p)
    today = dt.date.today()
    d_recent = (today - dt.timedelta(days=30)).isoformat()      # 窗内（近）
    d_2y = (today - dt.timedelta(days=730)).isoformat()         # 窗内（~2 年前）
    d_6y = (today - dt.timedelta(days=2192)).isoformat()        # 窗外（>5 年，应剔除）
    d_future = (today + dt.timedelta(days=45)).isoformat()      # 未来（PIT，应剔除）

    _seed_dividends(
        p,
        ex_dates=[d_recent, d_2y, d_6y, d_future],
        dps_map={d_recent: 0.5, d_2y: 1.0},
        closes={d_recent: 20.0, d_2y: 40.0})   # 仅窗内两行给 close（yield 可算）

    _patch_db(monkeypatch, p)
    d = wapi.stock_detail("sh.601398")
    divs = d["dividends_recent"]
    # 只保留窗内且 <=today 的两行（d_6y 超窗、d_future 未来 → 剔除）
    assert [r["ex_date"] for r in divs] == [d_recent, d_2y], \
        f"窗口/PIT/降序错误: {[r['ex_date'] for r in divs]}"
    # yield = cash_dps/close*100：0.5/20*100=2.5；1.0/40*100=2.5
    assert abs(divs[0]["dividend_yield_pct"] - 2.5) < 1e-6
    assert abs(divs[1]["dividend_yield_pct"] - 2.5) < 1e-6
    assert divs[0]["cash_dps"] == 0.5


def test_dividends_recent_yield_null_when_no_close(tmp_path, monkeypatch):
    """ex_date 无 kline 行（close 缺）→ dividend_yield_pct=null（不猜、不用近似价）。"""
    import lake.web_api as wapi

    p = str(tmp_path / "div2.duckdb")
    _seed_master(p)
    today = dt.date.today()
    d1 = (today - dt.timedelta(days=60)).isoformat()
    _seed_dividends(p, ex_dates=[d1], dps_map={d1: 2.0}, closes={})   # 无 close
    _patch_db(monkeypatch, p)

    d = wapi.stock_detail("sh.601398")
    divs = d["dividends_recent"]
    assert len(divs) == 1
    assert divs[0]["dividend_yield_pct"] is None, f"无 close 应 yield=null: {divs}"
    assert divs[0]["cash_dps"] == 2.0


def test_dividends_recent_cap_12(tmp_path, monkeypatch):
    """上限 12 行：播种 15 条窗内分红 → 只返回最近 12（ex_date 降序）。"""
    import lake.web_api as wapi

    p = str(tmp_path / "div3.duckdb")
    _seed_master(p)
    today = dt.date.today()
    ex_dates = [(today - dt.timedelta(days=5 * (i + 1))).isoformat() for i in range(15)]
    # ex_dates[0] 最近（-5d）… ex_dates[14] 最远（-75d），全在窗内
    _seed_dividends(p, ex_dates=ex_dates)
    _patch_db(monkeypatch, p)

    d = wapi.stock_detail("sh.601398")
    divs = d["dividends_recent"]
    assert len(divs) == 12, f"上限 12: {len(divs)}"
    # 返回的应是最近 12（ex_dates[0..11]，降序）
    assert [r["ex_date"] for r in divs] == ex_dates[:12]


def test_dividends_recent_empty_list(tmp_path, monkeypatch):
    """无分红 → dividends_recent=[]（键仍在 ready 态出现，值为空列表）。"""
    import lake.web_api as wapi

    p = str(tmp_path / "div4.duckdb")
    _seed_master(p)   # 不播种 dividend_events
    _patch_db(monkeypatch, p)

    d = wapi.stock_detail("sh.601398")
    assert d["dividends_recent"] == []
    # ready 态键集（含新键）
    assert set(d.keys()) == {"ts_code", "base", "fundamental_latest", "holders_top10",
                             "factors", "factors_as_of", "dividends_recent"}


def test_dividends_recent_locked_409_no_key(tmp_path, monkeypatch):
    """locked → stock_detail 仍 LakeBackfillInProgress(409)（上游 _con），dividends_recent
    只在 ready 态出现（三态红线：新字段不串味到 locked/uninitialized）。"""
    import lake.web_api as wapi
    from lake import conn as lconn

    p = str(tmp_path / "div5.duckdb")
    with open(p, "wb") as f:
        f.write(b"\x00" * 16)
    _patch_db(monkeypatch, p)
    monkeypatch.setattr(
        lconn, "connect_existing",
        lambda path=None: (_ for _ in ()).throw(lconn.LakeLocked(p, 4321)))
    with pytest.raises(wapi.LakeBackfillInProgress) as ei:
        wapi.stock_detail("sh.601398")
    assert ei.value.status_code == 409


# ===========================================================================
# 前端契约（离线断言 Vue 源码，防前后端字段脱节；与 v604/v605/v606 F 段同模式）
# ===========================================================================
def test_frontend_v612_contract():
    """v6.1.2 前端契约：P0 行业单字段 / P1-A 复权三态+note+标题 / P2-B 分红小表 /
    P1-B tasks 灌数中不渲染 / P2-A 琥珀块总进度 / P3 market 跳页。"""
    from conftest_helpers import vue_src_blob, vue_src

    blob = vue_src_blob()
    card = vue_src("tabs/lake/StockPanoramaCard.vue")
    chart = vue_src("tabs/lake/LakeKlineChart.vue")
    status = vue_src("tabs/lake/LakeStatusCard.vue")
    lt = vue_src("tabs/LakeTab.vue")
    market = vue_src("tabs/lake/MarketTable.vue")

    # P0：行业只显示 industry_name（不再 join industry_csric2 → 消除 "C39 C39…" 重复）
    assert "base.industry_name" in card, "P0：行业应消费 base.industry_name"
    assert "[base.industry_csric2, base.industry_name]" not in card, \
        "P0：旧 join(csric2, name) 写法必须移除（会重复显示代码）"

    # P1-A：复权三态按钮 + adjust 入 key + adjust_note 消费 + 标题动态
    for key in ('{ key: "none"', '{ key: "qfq"', '{ key: "hfq"'):
        assert key in chart, f"P1-A：复权按钮组缺 {key}"
    assert 'lake:kline:${props.code}:${range.value}:${adjust.value}' in card, \
        "P1-A：kline 缓存键必须含 adjust（切复权重取）"
    assert "&adjust=" in card, "P1-A：/kline 请求必须带 adjust 参数"
    assert "adjust_note" in chart and "adjustNote" in card, "P1-A：未消费 adjust_note"
    assert "日K线（T2 · {{ adjustLabel }}）" in card, "P1-A：标题须动态显示复权态"
    # 默认前复权 qfq（A股惯例、最新价=真实价）
    assert 'const adjust = ref("qfq")' in card, "P1-A：默认复权态必须 qfq"

    # P2-B：分红小表 + 空态文案
    assert 'id="lake-dividends-table"' in card, "P2-B：缺分红小表 #lake-dividends-table"
    assert "dividends_recent" in card, "P2-B：未消费 dividends_recent 字段"
    assert "近 5 年无分红记录" in card, "P2-B：空态文案必须逐字"

    # v6.1.4 O3（取代 v6.1.2 P1-B）：tasks 表**仅 backfill_in_progress=true 渲染**
    # （非同步态整块隐藏）+ "对应表"列（T1~T9 ↔ tasks 映射）。
    assert 'v-if="d.backfill_in_progress"' in status, \
        "O3：#lake-tasks-table 须 v-if=backfill_in_progress（非同步态整块隐藏）"
    assert ">对应表<" in status, "O3：tasks 表缺'对应表'列（T1~T9 ↔ tasks 映射）"

    # P2-A：琥珀块总进度行（done/total 合计 + 进度条 + 预计剩余）
    assert "lake-backfill-total" in lt, "P2-A：缺琥珀块总进度行 .lake-backfill-total"
    assert "总进度" in lt and "预计剩余" in lt, "P2-A：总进度文案缺失"

    # P3：market 跳页输入框（数字 + 回车/Go，clamp[1,pages]）
    assert 'id="lake-market-jump"' in market, "P3：缺跳页输入框 #lake-market-jump"
    assert "doJump" in market and "btn-lake-market-jump" in market, "P3：跳页 Go 缺失"
