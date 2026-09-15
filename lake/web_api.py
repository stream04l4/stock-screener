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

**B-1（v6.0.3）缺库/空库行为一致**：原 ``_con()`` 走 ``connect_existing()``，
而 duckdb.connect 对**不存在**的文件会**自动创建空库** → /status 返回 200（能
SELECT 1），但数据端点查表报 "table not found" → 500，状态自相矛盾。现统一：
- **缺库文件**：``_con()`` 先探测文件存在性再 connect（不再自动建空文件）→
  数据端点一致返回 **409** + ``{"error":"lake_not_initialized","hint":...}``；
- **空/未初始化库**（文件在但无 core 表 stock_master，含手工 touch 出的空文件）：
  ``_ensure_initialized`` 探测 information_schema → 数据端点同样 **409**；
- **D-1（v6.0.3 rework）0 字节/无效库文件**（存在但连 duckdb 都打不开，
  ``conn.LakeInvalidFile``）：与"缺文件/未 init_schema"同属"未就绪" → 数据端点
  同样 **409**（修复前漏网：duckdb IO Error 被通用 except 转成 503 + detail，
  与 /status 的 initialized=false 自相矛盾——正是 B-1 要消除的问题）。
- **/status** 例外：不抛 409，而是如实返回 200 + ``initialized=false`` + coverage
  全零（brief B-1 方案 b）——状态端点是"健康检查"，应反映真实状态而非报错。
  这样"库未就绪"在 status 可见、在数据端点一致降级，不再自相矛盾。

**v6.0.4：区分"灌数持锁"与"真未初始化"**（TL 定位真 bug：p0 灌数进程持有 DuckDB
单文件独占写锁期间，web_api 探测连不上库 → 误报 initialized=false +
lake_not_initialized，页面持续误导显示"不可用"）：
- conn 层 ``connect_existing`` 把 duckdb IO Error "Could not set lock … Conflicting
  lock is held (PID n)" 归一为 :class:`lake.conn.LakeLocked`（复用 D-1 文案匹配模式）。
- **数据端点**：locked → **409** + ``{"error":"lake_backfill_in_progress",
  "hint":"灌数进行中，稍后重试"}``（顶层契约体，与 lake_not_initialized 区分开）。
- **/status**：locked → **200** + ``initialized=true`` + ``backfill_in_progress=true``
  + ``lock_holder_pid``（尽力解析，失败 None）+ coverage/tasks 降级读 progress 文件
  （backfill_progress.json 不受 DuckDB 锁影响，正好是灌数进度）；真未初始化保持
  v6.0.3 行为逐字节不变。
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional

log = logging.getLogger("lake.web_api")

# B-1：core 表（init_schema 必建的第一张表）。用它判定"库是否已初始化"——
# 缺文件 / 空文件 / 未跑 init_schema 的库都没有它；正常灌数库必有。
_CORE_TABLE = "stock_master"

try:
    from fastapi import APIRouter, HTTPException, Query
except ImportError:  # fastapi 未装（极端情况）——app.py 本就不会挂载
    APIRouter = object  # type: ignore
    HTTPException = Exception  # type: ignore
    Query = lambda *a, **k: None  # type: ignore

router = APIRouter(prefix="/api/lake", tags=["lake"])


# ---------------------------------------------------------------------------
# B-1（v6.0.3）：库未就绪（缺文件 / 空文件 / 未 init_schema）的一致降级
#
# 为什么用自定义异常 + router 级 exception_handler，而不是在各端点里 raise
# HTTPException(409, detail={"error":...,"hint":...})：
#   FastAPI 把 HTTPException.detail 序列化进 **顶层 "detail"** 键 → 响应体变成
#   {"detail":{"error":...,"hint":...}}，而 brief 要求**顶层** error/hint 契约体。
#   自定义异常 + @router.exception_handler 才能精确控制 JSON 形状（顶层 error/hint）。
#   代价：直接调 handler 的单测需自行 catch LakeNotInitialized（见 test_lake_v603），
#   HTTP 层行为由 router 挂载后统一转 409（与前端 api() 的 !res.ok 分支兼容——
#   前端读 body.detail 为空时回退 res.statusText="Conflict"，不白屏）。
# ---------------------------------------------------------------------------
INIT_HINT = "python scripts/lake_backfill.py init"


class LakeNotInitialized(HTTPException):
    """库文件缺失 / 空文件 / 未跑 init_schema（无 core 表 stock_master）。

    数据端点命中 → 409 + {"error":"lake_not_initialized","hint":...}（顶层契约体，
    由下方 handler 渲染）；/status 命中 → 不抛，改报 initialized=false。

    ⚠️ FastAPI 0.141 的 ``APIRouter`` **没有** ``exception_handler``（那是 app 方法），
    故 handler 由 web/app.py 条件挂载块经 :func:`install` 注册到 app；未注册时本异常
    作为 HTTPException 子类仍被 FastAPI 默认处理器兜底为 409（detail="数据湖未初始化"）
    ——行为仍一致降级，只是 body 形状不同。
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        super().__init__(status_code=409, detail="数据湖未初始化")
        self.db_path = db_path or ""


def _lake_not_initialized_handler(_request, exc: LakeNotInitialized):
    """B-1 契约体渲染：顶层 error/hint（brief B-1 指定形状）。

    为什么用自定义 handler 而非 HTTPException(detail={...})：FastAPI 把 detail 序列化进
    **顶层 "detail"** 键 → body 变 {"detail":{...}}，而 brief 要求**顶层** error/hint。
    """
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=409,
        content={"error": "lake_not_initialized", "hint": INIT_HINT,
                 "db_path": exc.db_path})


# ---------------------------------------------------------------------------
# v6.0.4：灌数持锁（DuckDB 单文件独占写锁被 backfill 进程持有）的一致降级
#
# 与 B-1 的 LakeNotInitialized 同构：自定义异常 + app 级 handler 渲染**顶层**
# error/hint 契约体。区别只在语义——库是好的、正在被写入（initialized=true），
# 不是"未就绪"；前端据此显示"⏳ 数据灌入中"而非"未初始化"。
# ---------------------------------------------------------------------------
BACKFILL_HINT = "灌数进行中，稍后重试"


class LakeBackfillInProgress(HTTPException):
    """库被另一进程独占写锁持有（v6.0.4：backfill/灌数运行中）。

    数据端点命中 → 409 + {"error":"lake_backfill_in_progress","hint":"灌数进行中，
    稍后重试"}（顶层契约体，由下方 handler 渲染）；/status 命中 → 不抛，改报
    backfill_in_progress=true + lock_holder_pid（见 status）。

    holder_pid：conn.parse_lock_holder_pid 尽力解析自 duckdb 锁报错文案
    （"(PID <n>) by user …"），解析失败为 None——前端显示"未知"，不猜。
    """

    def __init__(self, db_path: Optional[str] = None,
                 holder_pid: Optional[int] = None) -> None:
        super().__init__(status_code=409, detail="数据湖灌数进行中")
        self.db_path = db_path or ""
        self.holder_pid = holder_pid


def _lake_backfill_in_progress_handler(_request, exc: LakeBackfillInProgress):
    """v6.0.4 契约体渲染：顶层 error/hint（brief v6.0.4 指定形状）。"""
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=409,
        content={"error": "lake_backfill_in_progress", "hint": BACKFILL_HINT,
                 "db_path": exc.db_path, "lock_holder_pid": exc.holder_pid})


def install(app) -> None:
    """把 lake 异常 handler 注册到 FastAPI app（由 web/app.py 条件挂载块调用）。

    为什么放这里而不是 web_api 模块顶层：``APIRouter.exception_handler`` 在本版 FastAPI
    不存在，handler 只能挂在 app 上；而 app 只存在于 web/app.py。此函数是 lake → web 的
    唯一额外接线点（与 router 挂载同在 duckdb 可导入的条件块内），不破坏零 import 边界。
    v6.0.4：同时注册 LakeBackfillInProgress handler（灌数持锁 → 409 新 error 值）。
    """
    app.add_exception_handler(LakeNotInitialized, _lake_not_initialized_handler)
    app.add_exception_handler(
        LakeBackfillInProgress, _lake_backfill_in_progress_handler)


def _ensure_initialized(con) -> None:
    """B-1：探测 core 表（stock_master）是否存在；缺失 → LakeNotInitialized。

    用 information_schema 查询而非直接 SELECT——后者对"表不存在"抛 CatalogException，
    语义上等价但多一层 try/except；information_schema 恒可查、返回空集即可判定。
    """
    try:
        n = con.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema='main' AND table_name=?", [_CORE_TABLE]).fetchone()[0]
    except Exception:  # noqa: BLE001 - 连接异常等 → 视为未就绪（由 _con 的 503 兜底）
        n = 0
    if not n:
        raise LakeNotInitialized()


def _con():
    """每请求短连接 + **B-1 就绪探测**（数据端点统一入口）。

    - duckdb 未装 → 503（不变）。
    - **库文件不存在** → 直接抛 LakeNotInitialized（409），**不再 connect**。
      为什么：duckdb.connect(不存在的文件) 会**自动创建空库文件**——旧行为由此产生
      "status 200 / 数据端点 500"的自相矛盾，且会在生产目录留下一个 0 表脏文件。
      先 os.path.exists 探测即可避免副作用（Web 只读，绝不代建库）。
    - 文件存在但无 core 表（空文件 / 未 init）→ connect 后 _ensure_initialized 抛 409。
    - **D-1：0 字节/无效库文件**（conn.LakeInvalidFile，duckdb 打不开）→ 同样
      LakeNotInitialized(409)——"连 duckdb 都打不开 = schema 从未建立 = 未就绪"，
      不是服务不可用(503)。修复前此场景漏网：IO Error 落进下方通用 except → 503。
    - **v6.0.4：库被灌数进程独占写锁持有**（conn.LakeLocked）→ 抛
      LakeBackfillInProgress(409) + lake_backfill_in_progress——库是好的、正在被写入，
      与"未初始化"语义不同，前端显示"⏳ 数据灌入中"。必须放在通用 except 之前
      （同 D-1：否则会被转成 503 + detail，绕过 409 契约体）。
    - 就绪 → 返回连接（happy path 行为与 D-4 修复后完全一致：每请求独立短连接）。
    """
    from . import conn as _conn

    if not _conn.duckdb_available():
        raise HTTPException(status_code=503, detail="数据湖不可用：duckdb 未安装（uv sync --extra lake）")
    db_path = _conn.default_db_path()
    if not os.path.exists(db_path):
        # B-1：缺库文件 → 409（不 connect，避免 duckdb 自动建空库的副作用）
        raise LakeNotInitialized(db_path)
    try:
        con = _conn.connect_existing(db_path)
    except _conn.LakeLocked as exc:  # noqa: BLE001
        # v6.0.4：灌数持锁 → 409 lake_backfill_in_progress（先于 LakeInvalidFile/通用
        # except——锁被持有 ≠ 坏文件，也 ≠ 服务不可用）
        raise LakeBackfillInProgress(exc.db_path, exc.holder_pid) from exc
    except _conn.LakeInvalidFile as exc:  # noqa: BLE001
        # D-1：0 字节/无效库文件 = 未就绪（409），与缺文件/未 init_schema 一致。
        # 必须放在通用 except 之前——否则会被转成 503 + detail，绕过 409 契约体。
        raise LakeNotInitialized(exc.db_path) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"数据湖不可用：{exc}")
    try:
        _ensure_initialized(con)
    except LakeNotInitialized:
        con.close()  # 未就绪 → 不泄漏连接，交 exception_handler 渲染 409
        raise
    return con


def _status_probe():
    """/status 专用探测（B-1 + v6.0.4）：返回 ``(state, con, holder_pid)``。

    - ``("unavailable", None, None)`` → 库未就绪（duckdb 未装 / 缺文件 / 空文件 /
      无 core 表）→ 调用方报 initialized=false（v6.0.3 行为不变）。
    - ``("locked", None, pid_or_None)`` → **v6.0.4：库被灌数进程独占写锁持有**
      （conn.LakeLocked）→ 调用方报 initialized=true + backfill_in_progress=true +
      lock_holder_pid，coverage/tasks 降级读 progress 文件（不受 DuckDB 锁影响）。
    - ``("ready", con, None)`` → 就绪；``con`` 为短连接（**调用方负责 close**）。

    ⚠️ 为什么用显式 state 字符串而不是 (con, holder_pid) 二元组：locked 且 PID 解析
    失败时 holder_pid=None，与"未就绪"的 (None, None) 无法区分——而 brief 要求
    "解析失败降级 None"后仍必须报 backfill_in_progress=true（不能退回误报未初始化）。

    与 _con 的区别：**不抛** LakeNotInitialized / LakeBackfillInProgress——状态端点是
    健康检查，应如实报告真实状态，而不是像数据端点那样报 409。
    """
    from . import conn as _conn

    if not _conn.duckdb_available():
        return "unavailable", None, None
    db_path = _conn.default_db_path()
    if not os.path.exists(db_path):
        return "unavailable", None, None
    try:
        con = _conn.connect_existing(db_path)
    except _conn.LakeLocked as exc:  # noqa: BLE001
        # v6.0.4：灌数持锁 → ("locked", None, holder_pid)；holder_pid 尽力解析，失败 None
        return "locked", None, exc.holder_pid
    except Exception:  # noqa: BLE001 - 打不开（含 D-1 无效文件）→ 视为未就绪（status 不抛 503）
        return "unavailable", None, None
    try:
        n = con.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema='main' AND table_name=?", [_CORE_TABLE]).fetchone()[0]
    except Exception:  # noqa: BLE001
        con.close()
        return "unavailable", None, None
    if not n:
        con.close()
        return "unavailable", None, None
    return "ready", con, None


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
    # D-4：每请求短连接，with 保证异常路径也 close（查询语义不变）
    with _con() as con:
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
    # D-4：每请求短连接，with 保证异常路径也 close（查询语义不变）
    with _con() as con:
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
    # D-4：每请求短连接，with 保证异常路径也 close（查询语义不变）
    with _con() as con:
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
    # D-4：每请求短连接，with 保证异常路径也 close（查询语义不变）
    with _con() as con:
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
    """补齐进度（§4 coverage + tasks）+ duckdb 安装状态 + **B-1 initialized**
    + **v6.0.4 backfill_in_progress**。

    B-1：库未就绪（缺文件/空文件/未 init_schema）时**不抛 409**，如实返回
    ``initialized=false`` + coverage 全零 + tasks 空——状态端点是健康检查，
    应反映真实状态。数据端点则一致降级为 409 lake_not_initialized（见 _con）。

    v6.0.4：库被灌数进程独占写锁持有时**不再误报未初始化**——返回
    ``initialized=true`` + ``backfill_in_progress=true`` + ``lock_holder_pid``
    （尽力解析，失败 None），coverage/tasks 降级读 progress 文件
    （backfill_progress.json 是纯 JSON、不受 DuckDB 锁影响，正好是灌数进度）。
    """
    import duckdb

    from .backfill import load_progress

    state, con, holder_pid = _status_probe()
    if state == "locked":
        # v6.0.4：灌数持锁——库是好的（正在被写入），initialized=true；
        # coverage/tasks 来自 progress 文件降级（DuckDB 连不上，但 JSON 可读）。
        prog = load_progress()
        return {
            "installed": True,
            "duckdb_version": getattr(duckdb, "__version__", "?"),
            "initialized": True,
            "backfill_in_progress": True,
            "lock_holder_pid": holder_pid,
            "coverage": prog.get("coverage", {}),
            "tasks": prog.get("tasks", []),
            "updated_at": prog.get("updated_at"),
        }
    if con is None:
        # 库未就绪：coverage 全零、tasks 空（progress 文件即便存在也不代表库可用）
        return {
            "installed": True,
            "duckdb_version": getattr(duckdb, "__version__", "?"),
            "initialized": False,
            "error": "lake_not_initialized",
            "hint": INIT_HINT,
            "coverage": {},
            "tasks": [],
            "updated_at": None,
        }
    try:
        # 端点可达性证明：对库执行一条轻量查询（进度本身来自 JSON 文件）。
        con.execute("SELECT 1").fetchone()
    finally:
        con.close()
    prog = load_progress()
    return {
        "installed": True,
        "duckdb_version": getattr(duckdb, "__version__", "?"),
        "initialized": True,
        "coverage": prog.get("coverage", {}),
        "tasks": prog.get("tasks", []),
        "updated_at": prog.get("updated_at"),
    }
