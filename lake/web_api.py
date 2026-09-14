# -*- coding: utf-8 -*-
"""lake.web_api —— Web 数据湖页签 API（FastAPI APIRouter，prefix=/api/lake）。

规格（调研报告 §5）：
- ``GET /search?q=``          ts_code/name 模糊匹配 T1，top20。
- ``GET /stock/{ts_code}``    stock_panorama + T5 最近季 + T6 前十大 + T8 全因子（分节 dict）。
- ``GET /market?industry=&soe=&sort=&page=``  T1⋈T3 分页 50/页。
- ``GET /status``             §4 coverage + tasks（补齐进度）。

**挂载约定（零 import 保证）**：web/app.py **不** import lake——由 app.py 在
duckdb 可导入时 ``from lake.web_api import router`` 条件挂载；未装 duckdb 时
app.py 注册一个轻量 stub router（/api/lake/status 返回 installed=false），
前端页签显示"数据湖未安装"。本模块所有端点在库不可用 → 503 + 原因（不白屏）。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

log = logging.getLogger("lake.web_api")

try:
    from fastapi import APIRouter, HTTPException, Query
except ImportError:  # fastapi 未装（极端情况）——app.py 本就不会挂载
    APIRouter = object  # type: ignore
    HTTPException = Exception  # type: ignore
    Query = lambda *a, **k: None  # type: ignore

router = APIRouter(prefix="/api/lake", tags=["lake"])


# ---------------------------------------------------------------------------
# 连接获取（每请求新开短连接——DuckDB 文件库支持多连接并发读；避免长连接占锁）
# ---------------------------------------------------------------------------
def _con():
    from . import conn as _conn

    if not _conn.duckdb_available():
        raise HTTPException(status_code=503, detail="数据湖不可用：duckdb 未安装（uv sync --extra lake）")
    try:
        return _conn.get_conn()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"数据湖不可用：{exc}")


def _rows_dicts(con, sql: str, params: Optional[List[Any]] = None) -> List[Dict[str, Any]]:
    """查询 → list[dict]（列名取描述）。"""
    cur = con.execute(sql, params or [])
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _valid_ts_code(ts_code: str) -> bool:
    return bool(re.fullmatch(r"(sh|sz|bj)\.\d{6}", ts_code or ""))


# ---------------------------------------------------------------------------
# GET /search
# ---------------------------------------------------------------------------
@router.get("/search")
def search(q: str = Query(default="")) -> Dict[str, Any]:
    """ts_code/name 模糊匹配 T1，top20。空 q → 最近 20 只（按代码序）。"""
    con = _con()
    q = (q or "").strip()
    if not q:
        rows = _rows_dicts(con, "SELECT ts_code,name,industry_name,board,is_st,soe_flag "
                                 "FROM stock_master ORDER BY ts_code LIMIT 20")
        return {"results": rows}
    like = f"%{q}%"
    rows = _rows_dicts(
        con,
        "SELECT ts_code,name,industry_name,board,is_st,soe_flag FROM stock_master "
        "WHERE ts_code LIKE ? OR name LIKE ? ORDER BY ts_code LIMIT 20",
        [like, like])
    return {"results": rows}


# ---------------------------------------------------------------------------
# GET /stock/{ts_code}
# ---------------------------------------------------------------------------
@router.get("/stock/{ts_code}")
def stock_detail(ts_code: str) -> Dict[str, Any]:
    """个股全景：panorama + T5 最近季 + T6 前十大 + T8 全因子（分节 dict）。"""
    if not _valid_ts_code(ts_code):
        raise HTTPException(status_code=400, detail=f"非法代码: {ts_code}")
    con = _con()

    pano = _rows_dicts(con, "SELECT * FROM stock_panorama WHERE ts_code=?", [ts_code])
    if not pano:
        raise HTTPException(status_code=404, detail=f"数据湖无此股: {ts_code}（T1 未灌入）")
    base = pano[0]

    # T5 最近季（全部列，含 ocf/roe_weighted）
    t5 = _rows_dicts(con,
                     "SELECT period,pub_date,roe_avg,roe_weighted,yoy_pni,npi,ocf,"
                     "gross_margin,liability_pct FROM fundamentals_quarterly "
                     "WHERE ts_code=? ORDER BY period DESC LIMIT 1", [ts_code])

    # T6 前十大（最新 as_of_date）
    t6 = _rows_dicts(con,
                     "SELECT holder_rank,holder_name,hold_ratio,share_nature,as_of_date "
                     "FROM holders_snapshot WHERE ts_code=? "
                     "AND as_of_date=(SELECT MAX(as_of_date) FROM holders_snapshot "
                     "WHERE ts_code=?) ORDER BY holder_rank", [ts_code, ts_code])

    # T8 全因子（最新 as_of_date）
    t8 = _rows_dicts(con,
                     "SELECT factor_name,value,params_json,as_of_date FROM factor_snapshot "
                     "WHERE ts_code=? AND as_of_date=(SELECT MAX(as_of_date) "
                     "FROM factor_snapshot WHERE ts_code=?) ORDER BY factor_name",
                     [ts_code, ts_code])

    return {
        "ts_code": ts_code,
        "base": base,                 # 区块A 基础卡 + 估值行（panorama 全列）
        "fundamental_latest": t5[0] if t5 else None,   # 缺 → None（前端"暂无数据"）
        "holders_top10": t6,          # 缺 → []（前端"暂无数据（后台补齐中）"）
        "factors": {r["factor_name"]: r["value"] for r in t8},  # T8 全因子
        "factors_as_of": t8[0]["as_of_date"] if t8 else None,
    }


# ---------------------------------------------------------------------------
# GET /industries（v6.0.1 D-3：区块B 行业下拉动态填充）
# ---------------------------------------------------------------------------
@router.get("/industries")
def industries() -> Dict[str, Any]:
    """stock_master distinct industry_csric2 + 名称映射（code→name，取众数）。

    供前端"全市场浏览"行业下拉动态填充；库为空 → ``{"industries": []}``
    （前端保持"全部行业"占位 + 空态文案）。排序按 code 升序（稳定、可断言）。
    """
    con = _con()
    rows = _rows_dicts(
        con,
        "SELECT industry_csric2 AS code, "
        "ANY_VALUE(industry_name) FILTER (industry_name IS NOT NULL) AS name "
        "FROM stock_master WHERE industry_csric2 IS NOT NULL AND industry_csric2 <> '' "
        "GROUP BY industry_csric2 ORDER BY code")
    return {"industries": [{"code": r["code"], "name": r["name"] or ""} for r in rows]}


# ---------------------------------------------------------------------------
# GET /market
# ---------------------------------------------------------------------------
@router.get("/market")
def market(industry: Optional[str] = Query(default=None),
           soe: Optional[str] = Query(default=None),
           sort: str = Query(default="total_mv"),
           page: int = Query(default=1, ge=1)) -> Dict[str, Any]:
    """T1⋈T3 全市场浏览（分页 50/页）。

    :param industry: industry_csric2 精确过滤（如 J66）。
    :param soe: all | soe | other（soe_flag='央国企' / 其余）。
    :param sort: total_mv | ttm_yield_pct（降序；NULL 排最后）。
    """
    con = _con()
    page_size = 50
    where: List[str] = []
    params: List[Any] = []
    if industry:
        where.append("m.industry_csric2 = ?")
        params.append(industry)
    if soe == "soe":
        where.append("m.soe_flag = '央国企'")
    elif soe == "other":
        where.append("(m.soe_flag IS NULL OR m.soe_flag <> '央国企')")
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    sort_col = {"total_mv": "v.total_mv", "ttm_yield_pct": "v.ttm_yield_pct"}.get(sort, "v.total_mv")
    total = con.execute(
        f"SELECT COUNT(*) FROM stock_master m LEFT JOIN valuation_daily v "
        f"ON v.ts_code=m.ts_code AND v.date=(SELECT MAX(date) FROM valuation_daily)"
        + where_sql, params).fetchone()[0]

    offset = (page - 1) * page_size
    rows = _rows_dicts(
        con,
        "SELECT m.ts_code,m.name,m.industry_csric2,m.industry_name,m.board,m.is_st,"
        "m.soe_flag,v.total_mv,v.float_mv,v.pe_ttm,v.pb,v.turnover_pct,v.ttm_yield_pct "
        "FROM stock_master m LEFT JOIN valuation_daily v "
        "ON v.ts_code=m.ts_code AND v.date=(SELECT MAX(date) FROM valuation_daily)"
        + where_sql +
        f" ORDER BY {sort_col} DESC NULLS LAST LIMIT {page_size} OFFSET {offset}",
        params)
    return {"rows": rows, "total": total, "page": page, "page_size": page_size,
            "pages": (total + page_size - 1) // page_size if total else 0}


# ---------------------------------------------------------------------------
# GET /status
# ---------------------------------------------------------------------------
@router.get("/status")
def status() -> Dict[str, Any]:
    """补齐进度（§4 coverage + tasks）+ duckdb 安装状态。"""
    import duckdb

    con = _con()
    from .backfill import load_progress

    prog = load_progress()
    return {
        "installed": True,
        "duckdb_version": getattr(duckdb, "__version__", "?"),
        "coverage": prog.get("coverage", {}),
        "tasks": prog.get("tasks", []),
        "updated_at": prog.get("updated_at"),
    }
