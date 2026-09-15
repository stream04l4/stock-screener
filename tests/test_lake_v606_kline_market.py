# -*- coding: utf-8 -*-
"""test_lake_v606_kline_market —— v6.0.6 回归：market 分页 20/页 + /kline 日线端点。

规格（v6.0.6 brief）：
- **任务1** GET /market 默认 **20 条/页**；可选 ``page_size`` clamp [10,50]、
  非法值回退 20；响应字段不变（page_size 返回实际生效值，pages 按生效值计算）。
- **任务2** GET /kline/{ts_code}?days=N：N 缺省 250、clamp [30,9999]、``all``=全量；
  从 kline_daily **raw** 表取 date **升序** OHLCV；契约体
  ``{ts_code, name, rows:[{date,open,high,low,close,volume}], count}``；
  空股 → rows=[] count=0（200 不报错）；未初始化/locked → v6.0.3/v6.0.4 既有
  409 契约路径（LakeNotInitialized / LakeBackfillInProgress，不新造错误语义）。

纪律：库一律 tmp_path（**绝不触碰 data/lake/ 生产库**），离线；日期用固定值
（端点不做相对判定，无 today() 依赖）。
"""
from __future__ import annotations

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
def _seed_market_db(db_path: str, n_stocks: int = 45) -> None:
    """建 schema + 播种 stock_master N 行（ts_code 递增，便于断言分页切片）。"""
    from lake import conn as lconn

    con = lconn.open(db_path)
    con.executemany(
        "INSERT INTO stock_master (ts_code,name,board,is_st,source,fetched_at,"
        "data_version) VALUES (?,?,?,?,?, '2026-09-15 09:00:00', 'v6.0')",
        [(f"sh.{600000 + i}", f"测试股{i}", "主板", 0, "t") for i in range(n_stocks)])
    con.close()


def _seed_kline_db(db_path: str) -> None:
    """建 schema + 播种：

    - sh.601398（T1 有行）：kline_daily **5 行**，date 乱序插入（验证升序输出）；
    - sz.000002（T1 有行、无 K线）：空股场景；
    - bj.430047（**T1 无行**、有 K线 1 行）：name=None + rows 非空的边界。
    """
    from lake import conn as lconn

    con = lconn.open(db_path)
    con.executemany(
        "INSERT INTO stock_master (ts_code,name,board,is_st,source,fetched_at,"
        "data_version) VALUES (?,?,?,?,?, '2026-09-15 09:00:00', 'v6.0')",
        [("sh.601398", "工商银行", "主板", 0, "t"),
         ("sz.000002", "万科A", "主板", 0, "t")])
    # 乱序插入（2026-09-15 / 09-11 / 09-14 / 09-10 / 09-13）→ 输出必须升序
    con.executemany(
        "INSERT INTO kline_daily (ts_code,date,open,high,low,close,volume,"
        "source,fetched_at,data_version) VALUES (?,?,?,?,?,?,?, 't',"
        "'2026-09-15 12:00:00','v6.0')",
        [("sh.601398", d, o, h, l, c, v) for d, o, h, l, c, v in [
            ("2026-09-15", 10.4, 10.6, 10.3, 10.5, 1200),   # 涨（红）
            ("2026-09-11", 10.0, 10.2, 9.9, 10.1, 900),     # 涨
            ("2026-09-14", 10.3, 10.5, 10.2, 10.4, 1100),   # 涨
            ("2026-09-10", 10.1, 10.3, 10.0, 10.2, 1000),   # 涨
            ("2026-09-13", 10.2, 10.4, 10.1, 10.0, 950),    # 跌（绿）
        ]])
    con.execute(
        "INSERT INTO kline_daily (ts_code,date,open,high,low,close,volume,"
        "source,fetched_at,data_version) VALUES "
        "('bj.430047','2026-09-10',1,2,0.5,1.5,100,'t','2026-09-15 12:00:00','v6.0')")
    con.close()


def _patch_db(monkeypatch, db_path: str) -> None:
    from lake import conn as lconn

    monkeypatch.setattr(lconn, "default_db_path", lambda: db_path)


# ===========================================================================
# 任务1：/market 默认 20 条/页 + page_size clamp [10,50]
# ===========================================================================
def test_market_default_page_size_20(tmp_path, monkeypatch):
    """缺省 page_size → 每页 20 行；total=45 → pages=ceil(45/20)=3。"""
    import lake.web_api as wapi

    _seed_market_db(str(tmp_path / "m1.duckdb"), 45)
    _patch_db(monkeypatch, str(tmp_path / "m1.duckdb"))
    d = wapi.market()
    assert d["page_size"] == 20, f"默认必须 20/页: {d['page_size']}"
    assert len(d["rows"]) == 20
    assert d["total"] == 45
    assert d["pages"] == 3, f"pages=ceil(45/20)=3: {d['pages']}"
    # 第 3 页 = 尾行 5 条（45-40）
    d3 = wapi.market(page=3)
    assert len(d3["rows"]) == 5 and d3["page"] == 3


def test_market_page_size_30_effective(tmp_path, monkeypatch):
    """page_size=30（区间内）→ 生效 30；45 行 → pages=ceil(45/30)=2。"""
    import lake.web_api as wapi

    _seed_market_db(str(tmp_path / "m2.duckdb"), 45)
    _patch_db(monkeypatch, str(tmp_path / "m2.duckdb"))
    d = wapi.market(page_size=30)
    assert d["page_size"] == 30
    assert len(d["rows"]) == 30
    assert d["pages"] == 2


def test_market_page_size_999_clamped_to_50(tmp_path, monkeypatch):
    """page_size=999 → clamp 50（不暴露任意值）；45 行 → pages=1。"""
    import lake.web_api as wapi

    _seed_market_db(str(tmp_path / "m3.duckdb"), 45)
    _patch_db(monkeypatch, str(tmp_path / "m3.duckdb"))
    d = wapi.market(page_size=999)
    assert d["page_size"] == 50, f"999 应 clamp 到 50: {d['page_size']}"
    assert len(d["rows"]) == 45   # 一页装下全部
    assert d["pages"] == 1


def test_market_page_size_abc_fallback_20(tmp_path, monkeypatch):
    """非法值（FastAPI 把 page_size=abc 解析为 None）→ 回退 20。"""
    import lake.web_api as wapi

    _seed_market_db(str(tmp_path / "m4.duckdb"), 45)
    _patch_db(monkeypatch, str(tmp_path / "m4.duckdb"))
    d = wapi.market(page_size=None)   # 等价于 URL page_size=abc（解析失败→None）
    assert d["page_size"] == 20
    assert len(d["rows"]) == 20


def test_clamp_page_size_unit_boundaries():
    """_clamp_page_size 单元级边界：缺省/区间内/越界上下/非法类型。"""
    from lake import web_api as wapi

    assert wapi._clamp_page_size(None) == 20      # 缺省
    assert wapi._clamp_page_size(10) == 10        # 下边界（含）
    assert wapi._clamp_page_size(50) == 50        # 上边界（含）
    assert wapi._clamp_page_size(9) == 10         # <10 → 10
    assert wapi._clamp_page_size(51) == 50        # >50 → 50
    assert wapi._clamp_page_size(999) == 50       # brief 用例
    assert wapi._clamp_page_size("abc") == 20     # 非法字符串 → 20
    assert wapi._clamp_page_size(float("nan")) == 20   # NaN（int() 抛 ValueError）
    assert wapi._clamp_page_size(float("inf")) == 20   # inf（int() 抛 OverflowError）


def test_market_pages_calc_with_filters(tmp_path, monkeypatch):
    """pages 按**生效 page_size + 过滤后 total** 计算（industry/soe 过滤不破坏口径）。"""
    import lake.web_api as wapi

    _seed_market_db(str(tmp_path / "m5.duckdb"), 45)
    _patch_db(monkeypatch, str(tmp_path / "m5.duckdb"))
    # soe=other（播种数据 soe_flag 全 NULL → 全部命中）：total=45，默认 20/页 → 3 页
    d = wapi.market(soe="other", page_size=10)
    assert d["total"] == 45 and d["page_size"] == 10 and d["pages"] == 5
    # 无匹配行业 → total=0、pages=0（既有口径：total else 0）
    d2 = wapi.market(industry="J99")
    assert d2["total"] == 0 and d2["pages"] == 0 and d2["rows"] == []


# ===========================================================================
# 任务2：/kline/{ts_code} 日线端点
# ===========================================================================
def test_kline_ascending_and_contract_shape(tmp_path, monkeypatch):
    """正常返回：date **升序**（乱序插入被纠正）+ 契约字段齐全 + name 联取 T1。"""
    import lake.web_api as wapi

    _seed_kline_db(str(tmp_path / "k1.duckdb"))
    _patch_db(monkeypatch, str(tmp_path / "k1.duckdb"))
    d = wapi.stock_kline("sh.601398")
    assert set(d.keys()) == {"ts_code", "name", "rows", "count"}
    assert d["ts_code"] == "sh.601398" and d["name"] == "工商银行"
    assert d["count"] == 5 and len(d["rows"]) == 5
    dates = [r["date"] for r in d["rows"]]
    assert dates == sorted(dates), f"必须 date 升序: {dates}"
    assert dates[0] == "2026-09-10" and dates[-1] == "2026-09-15"
    for r in d["rows"]:
        assert set(r.keys()) == {"date", "open", "high", "low", "close", "volume"}


def test_kline_days_clamp(tmp_path, monkeypatch):
    """days clamp [30,9999]：5 行库——days=30 → 全 5 行（取最近 N，N>行数）；
    days=1 → clamp 30 → 仍 5 行；days=99999 → clamp 9999 → 5 行。"""
    import lake.web_api as wapi

    _seed_kline_db(str(tmp_path / "k2.duckdb"))
    _patch_db(monkeypatch, str(tmp_path / "k2.duckdb"))
    for days in ("30", "1", "99999"):
        d = wapi.stock_kline("sh.601398", days=days)
        assert d["count"] == 5, f"days={days} clamp 后应取全 5 行: {d['count']}"


def test_kline_days_tail_n_and_all(tmp_path, monkeypatch):
    """days=N 取**最近 N 根**（date 升序尾部）；days=all → 全量。

    播种 sz.300001 **40 行**（唯一 ts_code，日期连续不重复）：
    days=30 → 最后 30 根（首行 = 按日期排序后的第 11 个）；all → 40 根。
    """
    import lake.web_api as wapi

    p = str(tmp_path / "k3.duckdb")
    _seed_kline_db(p)   # 先建 schema + T1
    import datetime as dt

    base = dt.date(2026, 7, 1)
    con = duckdb.connect(p)
    con.execute(
        "INSERT INTO stock_master (ts_code,name,board,is_st,source,fetched_at,"
        "data_version) VALUES ('sz.300001','长历史股','主板',0,'t',"
        "'2026-09-15 09:00:00','v6.0')")
    con.executemany(
        "INSERT INTO kline_daily (ts_code,date,open,high,low,close,volume,"
        "source,fetched_at,data_version) VALUES (?,?,?,?,?,?,?, 't',"
        "'2026-09-15 12:00:00','v6.0')",
        [("sz.300001", (base + dt.timedelta(days=i)).isoformat(), 1, 2, 0.5, 1.5, 100)
         for i in range(40)])   # 40 个连续不重复日期
    con.close()
    _patch_db(monkeypatch, p)

    d30 = wapi.stock_kline("sz.300001", days="30")
    assert d30["count"] == 30 and len(d30["rows"]) == 30
    dates30 = [r["date"] for r in d30["rows"]]
    assert dates30 == sorted(dates30)
    # 最近 30 根 = 全部 40 根的尾部（首行应为按日期排序后的第 11 个）
    all_dates = sorted(r["date"] for r in wapi.stock_kline("sz.300001", days="all")["rows"])
    assert dates30 == all_dates[-30:], "days=30 必须取最近 30 根（升序尾部）"

    dall = wapi.stock_kline("sz.300001", days="all")
    assert dall["count"] == 40, f"days=all 应全量 40 行: {dall['count']}"


def test_kline_default_days_250(tmp_path, monkeypatch):
    """days 缺省 → 250（clamp 后仍 >行数）→ 返回全部 5 行（不报错）。"""
    import lake.web_api as wapi

    _seed_kline_db(str(tmp_path / "k4.duckdb"))
    _patch_db(monkeypatch, str(tmp_path / "k4.duckdb"))
    d = wapi.stock_kline("sh.601398")   # 不传 days
    assert d["count"] == 5


def test_kline_empty_stock_rows_empty_no_error(tmp_path, monkeypatch):
    """空股（T1 有行、无 K线）→ rows=[] count=0，200 不报错。"""
    import lake.web_api as wapi

    _seed_kline_db(str(tmp_path / "k5.duckdb"))
    _patch_db(monkeypatch, str(tmp_path / "k5.duckdb"))
    d = wapi.stock_kline("sz.000002")
    assert d["rows"] == [] and d["count"] == 0
    assert d["ts_code"] == "sz.000002" and d["name"] == "万科A"


def test_kline_no_t1_row_name_none(tmp_path, monkeypatch):
    """T1 无行但 kline 有数据 → name=None（不 404；空股判定只认 kline 行数）。"""
    import lake.web_api as wapi

    _seed_kline_db(str(tmp_path / "k6.duckdb"))
    _patch_db(monkeypatch, str(tmp_path / "k6.duckdb"))
    d = wapi.stock_kline("bj.430047")
    assert d["name"] is None and d["count"] == 1


def test_kline_invalid_ts_code_400(tmp_path, monkeypatch):
    """非法代码 → 400（与 /stock/{ts_code} 同口径，先于库探测）。"""
    import lake.web_api as wapi

    _seed_kline_db(str(tmp_path / "k7.duckdb"))
    _patch_db(monkeypatch, str(tmp_path / "k7.duckdb"))
    for bad in ("sh.123", "601398", "xx.601398", ""):
        with pytest.raises(wapi.HTTPException) as ei:
            wapi.stock_kline(bad)
        assert ei.value.status_code == 400, f"{bad!r} 应 400: {ei.value.status_code}"


def test_kline_missing_db_409_lake_not_initialized(tmp_path, monkeypatch):
    """未初始化（缺库文件）→ LakeNotInitialized(409)（v6.0.3 既有异常路径）。"""
    import lake.web_api as wapi

    missing = str(tmp_path / "nope" / "lake.duckdb")
    _patch_db(monkeypatch, missing)
    with pytest.raises(wapi.LakeNotInitialized) as ei:
        wapi.stock_kline("sh.601398")
    assert ei.value.status_code == 409


def test_kline_locked_409_backfill_in_progress(tmp_path, monkeypatch):
    """locked（灌数持锁）→ LakeBackfillInProgress(409)（v6.0.4 既有异常路径）。"""
    import lake.web_api as wapi
    from lake import conn as lconn

    p = str(tmp_path / "locked.duckdb")
    with open(p, "wb") as f:
        f.write(b"\x00" * 16)
    _patch_db(monkeypatch, p)
    monkeypatch.setattr(
        lconn, "connect_existing",
        lambda path=None: (_ for _ in ()).throw(lconn.LakeLocked(p, 4321)))
    with pytest.raises(wapi.LakeBackfillInProgress) as ei:
        wapi.stock_kline("sh.601398")
    assert ei.value.status_code == 409
    assert ei.value.holder_pid == 4321


# ===========================================================================
# 前端契约（离线断言 DOM/JS/CSS，防前后端字段脱节）
# ===========================================================================
def test_frontend_v606_dom_contract():
    """index.html/app.js/style.css：蜡烛图 DOM + JS 渲染函数 + A股配色变量。

    - index.html：个股全景卡片结构不变（K线区由 app.js 在 #lake-stock-body 内动态
      注入，故断言锚点 = #lake-stock-card/#lake-stock-body）。
    - app.js：/api/lake/kline 消费 + 渲染函数 + 区间按钮组 + 空态文案逐字。
    - style.css：--lake-up/--lake-down CSS 变量（红涨绿跌）+ .lk-up/.lk-down。
    """
    html = open(os.path.join(REPO_ROOT, "web", "static", "index.html"),
                encoding="utf-8").read()
    js = open(os.path.join(REPO_ROOT, "web", "static", "app.js"),
              encoding="utf-8").read()
    css = open(os.path.join(REPO_ROOT, "web", "static", "style.css"),
               encoding="utf-8").read()

    # index.html：全景卡片锚点仍在（K线区注入其内）
    for sel in ('id="lake-stock-card"', 'id="lake-stock-body"'):
        assert sel in html, f"index.html 缺 {sel}"

    # app.js：kline 端点消费 + 渲染/加载函数 + 区间组 + 空态文案
    assert "/api/lake/kline/" in js, "app.js 未消费 /api/lake/kline 端点"
    for fn in ("lakeKlineRenderChart", "loadLakeKline", "lakeKlineRenderBar"):
        assert f"function {fn}(" in js, f"app.js 缺 {fn}"
    for label in ('{ key: "60"', '{ key: "120"', '{ key: "250"', '{ key: "all"'):
        assert label in js, f"区间按钮组缺 {label}"
    assert "该股暂无K线数据" in js, "空态文案必须逐字（brief）"
    assert "id=\"lake-kline-bar\"" in js and "id=\"lake-kline-chart\"" in js
    # 红涨绿跌判定逻辑存在（close≥open → up）
    assert "Number(r.close) >= Number(r.open)" in js, "缺 A股红涨绿跌判定"

    # style.css：CSS 变量 + 蜡烛配色 class（前后端/CSS 不脱节）
    assert "--lake-up:" in css and "--lake-down:" in css, \
        "style.css 缺 --lake-up/--lake-down 变量"
    for cls in (".lk-up", ".lk-down", ".lake-kline-tip", ".lake-kline-range"):
        assert cls in css, f"style.css 缺 {cls}"
    # 红涨绿跌：--lake-up 必须是红色系（#dc2626），--lake-down 绿色系（#16a34a）
    up_line = next(l for l in css.splitlines() if l.strip().startswith("--lake-up"))
    down_line = next(l for l in css.splitlines() if l.strip().startswith("--lake-down"))
    assert "#dc2626" in up_line, f"--lake-up 应为红: {up_line}"
    assert "#16a34a" in down_line, f"--lake-down 应为绿: {down_line}"
