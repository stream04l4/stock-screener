# -*- coding: utf-8 -*-
"""lake.web_api —— Web 数据湖页签 API（FastAPI APIRouter，prefix=/api/lake）。

规格（调研报告 §5 + v6.0.6 增量）：
- ``GET /search?q=``          ts_code/name 模糊匹配 T1，top20。
- ``GET /stock/{ts_code}``    stock_panorama + T5 最近季 + T6 前十大 + T8 全因子（分节 dict）。
- ``GET /kline/{ts_code}?days=``  v6.0.6：日K线 OHLCV date 升序（前端 SVG 蜡烛图；
  days 缺省 250，clamp [30,9999]，all=全量；空股 rows=[] count=0）。
- ``GET /market?industry=&soe=&sort=&page=&page_size=``  T1⋈T3 分页（v6.0.6：默认
  20/页，page_size clamp [10,50]、非法回退 20）。
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

**v6.0.5：/status 区块 C 重设计的数据源扩展**（Joel 反馈"数据库状态部分非常简陋，
信息堆在一起不够直观"→ TL 定规格）：仅在 **initialized+ready 态**追加新字段
（``tables`` 9 表逐表 state / ``views`` / ``adj_factor_coverage_pct`` / ``db`` /
``sync``），locked 与 uninitialized 两态响应体**逐字节保持 v6.0.4**（三态契约测试
test_lake_v604_lock 不得破坏）。state 判定全部运行时计算、零硬编码数据值：
- fresh/lagging 只比"相对参考最新交易日的天数差"，不比较具体日期；
- P2/P3 计划内未启动的 4 张表（T5/T6/T8/T9）即使 0 行也标 **pending** 而非 empty
  （"计划内未做" ≠ "坏了"，前端据此 muted 弱化而非红色报错）；
- 参考最新交易日 = kline_daily 全局 max(date)（无数据回退今日）。
"""
from __future__ import annotations

import datetime
import logging
import math
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


# ---------------------------------------------------------------------------
# v6.0.9：同步控制（启动/停止灌数）冲突异常——与 B-1/v6.0.4 同构的顶层契约体
#
# POST /api/lake/sync/start 已 running → 409 sync_already_running；
# POST /api/lake/sync/stop 未 running → 409 sync_not_running。
# 与 v6.0.4 LakeBackfillInProgress 同模式（自定义异常 + app 级 handler 渲染顶层
# error/hint，避免 FastAPI 把 detail 包进 "detail" 键破坏契约体形状）。
# ---------------------------------------------------------------------------
class LakeSyncConflict(HTTPException):
    """同步控制冲突（start 已 running / stop 未 running）→ 409 + 顶层契约体。"""

    def __init__(self, error: str, hint: str) -> None:
        super().__init__(status_code=409, detail=hint)
        self.error = error
        self.hint = hint


def _lake_sync_conflict_handler(_request, exc: LakeSyncConflict):
    """v6.0.9 契约体渲染：顶层 error/hint（brief v6.0.9 指定形状，无 detail 键）。"""
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=409, content={"error": exc.error, "hint": exc.hint})


def install(app) -> None:
    """把 lake 异常 handler 注册到 FastAPI app（由 web/app.py 条件挂载块调用）。

    为什么放这里而不是 web_api 模块顶层：``APIRouter.exception_handler`` 在本版 FastAPI
    不存在，handler 只能挂在 app 上；而 app 只存在于 web/app.py。此函数是 lake → web 的
    唯一额外接线点（与 router 挂载同在 duckdb 可导入的条件块内），不破坏零 import 边界。
    v6.0.4：同时注册 LakeBackfillInProgress handler（灌数持锁 → 409 新 error 值）。
    v6.0.9：同时注册 LakeSyncConflict handler（同步控制 start/stop 冲突 → 409）。
    """
    app.add_exception_handler(LakeNotInitialized, _lake_not_initialized_handler)
    app.add_exception_handler(
        LakeBackfillInProgress, _lake_backfill_in_progress_handler)
    app.add_exception_handler(LakeSyncConflict, _lake_sync_conflict_handler)


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
# GET /kline/{ts_code}（v6.0.6：个股全景日线图数据源）
# ---------------------------------------------------------------------------
@router.get("/kline/{ts_code}")
def stock_kline(ts_code: str, days: Optional[str] = Query(default=None)) -> Dict[str, Any]:
    """个股日K线 OHLCV（前端 SVG 蜡烛图数据源）。

    :param ts_code: sh/sz/bj.6位数字（非法 → 400，与 /stock/{ts_code} 一致）。
    :param days: 缺省 ``"250"``；clamp [30, 9999]（越界取边界值）；``"all"`` →
        全量。非法/非数字且非 all → 回退 250（与 market page_size 同口径：
        静默降级，不改变既有错误语义）。

    响应契约（brief v6.0.6）：``{ts_code, name, rows:[{date,open,high,low,close,
    volume}], count}``——rows **date 升序**；空股（无 K线）→ ``rows=[] count=0``
    （200，不报错）。

    连接纪律：D-4 每请求短连接（with 保证异常路径也 close）；库未就绪/locked 走
    v6.0.3/v6.0.4 既有异常路径（_con → LakeNotInitialized / LakeBackfillInProgress
    → 409 顶层契约体），**不新造错误语义**。

    name：T1 stock_master 联取（无 T1 行 → None，前端回退显示 ts_code）——
    空股判定只认 kline_daily 行数，不因 T1 缺行而 404（brief："空股→rows=[]"）。
    """
    if not _valid_ts_code(ts_code):
        raise HTTPException(status_code=400, detail=f"非法代码: {ts_code}")
    # days 解析：all → None（不加 LIMIT）；数字 clamp [30,9999]；其余回退 250。
    # ⚠️ 直接函数调用（单测）时 days 默认值是 FastAPI Query() 返回的 FieldInfo
    # 对象而非 None——isinstance 归一化，HTTP 路径（str）行为不变。
    if not isinstance(days, str):
        days = None
    d = (days or "250").strip()
    limit: Optional[int]
    if d.lower() == "all":
        limit = None
    else:
        try:
            n = int(d)
        except ValueError:
            n = 250
        limit = max(30, min(9999, n))
    # D-4：每请求短连接，with 保证异常路径也 close（查询语义不变）
    with _con() as con:
        name_row = _rows_dicts(con, "SELECT name FROM stock_master WHERE ts_code=?",
                               [ts_code])
        sql = ("SELECT date, open, high, low, close, volume FROM kline_daily "
               "WHERE ts_code=? ORDER BY date ASC")
        params: List[Any] = [ts_code]
        if limit is not None:
            # 取**最近** N 根（date 升序尾部）：内层降序 LIMIT 后外层再升序。
            sql = ("SELECT date, open, high, low, close, volume FROM "
                   "(SELECT date, open, high, low, close, volume FROM kline_daily "
                   f"WHERE ts_code=? ORDER BY date DESC LIMIT {limit}) "
                   "ORDER BY date ASC")
        rows = _rows_dicts(con, sql, params)
        for r in rows:  # DATE → 'YYYY-MM-DD'（与全 API 日期口径一致）
            r["date"] = _iso_date(r["date"])
        return {"ts_code": ts_code,
                "name": name_row[0]["name"] if name_row else None,
                "rows": rows, "count": len(rows)}


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
def _clamp_page_size(v: Any) -> int:
    """v6.0.6：market page_size clamp（brief 契约）。

    :param v: 查询参数原值。端点声明为 **str**（见下方"为什么不声明 int"）→
        HTTP 路径恒为 str/None；直接函数调用（单测）可能喂入 int/float/FieldInfo，
        一并防御。**非法一律回退 20**（不抛、不 422）。

    - None / 缺省（含 FieldInfo）→ 20（默认每页 20 条，Joel 反馈"每页太多"）；
    - <10 → 10、>50 → 50（给以后想调大留口子但不暴露任意值）；
    - 非法值（非数字字符串 / NaN / inf）→ 20。

    **为什么端点把 page_size 声明为 str 而不是 int**：FastAPI 对 ``int`` 类型参数做
    **请求级校验**——``page_size=abc`` 会在进函数前被打回 **422**，brief 要求的
    "非法值回退 20"根本执行不到。声明 str 后解析权完全在本函数：abc→20、999→50、
    30→30，HTTP 行为与 brief 契约逐字一致（响应 page_size 仍为 int 实际生效值）。
    """
    if v is None or isinstance(v, bool):
        return 20
    if isinstance(v, str):
        try:
            n = int(v.strip())
        except ValueError:
            return 20   # "abc" / "" / "12.5" → 回退 20
    elif isinstance(v, int):
        n = v
    elif isinstance(v, float):
        if not math.isfinite(v):
            return 20   # NaN/inf → 回退 20
        n = int(v)
    else:
        return 20       # FieldInfo（直接调用缺省）/ 其它垃圾 → 缺省语义
    if not (10 <= n <= 50):
        return 10 if n < 10 else 50
    return n


@router.get("/market")
def market(industry: Optional[str] = Query(default=None),
           soe: Optional[str] = Query(default=None),
           sort: str = Query(default="total_mv"),
           page: int = Query(default=1, ge=1),
           page_size: Optional[str] = Query(default=None)) -> Dict[str, Any]:
    """T1⋈T3 全市场浏览（v6.0.6：默认分页 20/页，page_size clamp [10,50]）。

    :param industry: industry_csric2 精确过滤（如 J66）。
    :param soe: all | soe | other（soe_flag='央国企' / 其余）。
    :param sort: total_mv | ttm_yield_pct（降序；NULL 排最后）。
    :param page_size: 可选每页条数——**声明为 str**（若声明 int，FastAPI 会把
        ``page_size=abc`` 请求级打回 422，brief 的"非法值回退 20"执行不到）；
        缺省 20、clamp [10,50]、非法回退 20（_clamp_page_size）。响应
        ``page_size`` 字段返回 **int 实际生效值**（brief：响应字段不变）。
    """
    # ⚠️ 直接函数调用（单测）时缺省参数值是 FastAPI Query() 返回的 FieldInfo
    # 对象而非 None/默认值——统一归一化，HTTP 路径（恒为实际值）行为不变。
    if not isinstance(industry, str):
        industry = None
    if not isinstance(soe, str):
        soe = None
    if not isinstance(sort, str):
        sort = "total_mv"
    if not isinstance(page, int) or isinstance(page, bool):
        page = 1
    # D-4：每请求短连接，with 保证异常路径也 close（查询语义不变）
    with _con() as con:
        ps = _clamp_page_size(page_size)   # 实际生效每页条数（int；page_size 参数保持 str 原值）
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

        offset = (page - 1) * ps
        rows = _rows_dicts(
            con,
            "SELECT m.ts_code,m.name,m.industry_csric2,m.industry_name,m.board,m.is_st,"
            "m.soe_flag,v.total_mv,v.float_mv,v.pe_ttm,v.pb,v.turnover_pct,v.ttm_yield_pct "
            "FROM stock_master m LEFT JOIN valuation_daily v "
            "ON v.ts_code=m.ts_code AND v.date=(SELECT MAX(date) FROM valuation_daily)"
            + where_sql +
            f" ORDER BY {sort_col} DESC NULLS LAST LIMIT {ps} OFFSET {offset}",
            params)
        return {"rows": rows, "total": total, "page": page, "page_size": ps,
                "pages": (total + ps - 1) // ps if total else 0}


# ---------------------------------------------------------------------------
# GET /status
# ---------------------------------------------------------------------------
# v6.0.5：9 表元数据（固定顺序 = 数据湖文档 T1-T9，与 lake.ddl.TABLES 一致）。
# kind 决定 state 判定分支；code_col 为"股票数"口径列（index_daily=index_code、
# macro_rf=无代码维度 → None）；date_col 为"数据区间"口径列（dividend_events 用
# ex_date——与 backfill coverage 同口径；stock_master 无日期维度 → None）。
# desc/name_cn 是**展示文案**（非数据值），与 brief 规格逐字一致。
_TABLE_META = [
    ("stock_master", "T1", "股票主档", "代码/名称/行业/上市退市日/ST/央国企标记（当前快照）",
     "snapshot", "ts_code", None),
    ("kline_daily", "T2", "日K线（含复权因子）", "OHLCV+涨跌幅+adj_factor 全史",
     "daily", "ts_code", "date"),
    ("valuation_daily", "T3", "估值日线", "总市值/流通市值/PE-TTM/PB/换手/TTM股息率",
     "daily", "ts_code", "date"),
    ("dividend_events", "T4", "分红事件", "1991→今 全史静态快照（除权日可含未来）",
     "snapshot", "ts_code", "ex_date"),
    ("fundamentals_quarterly", "T5", "季度基本面", "PIT：ROE/净利同比/OCF/毛利率/负债率",
     "pending_p2", "ts_code", "pub_date"),
    ("holders_snapshot", "T6", "前十大股东+实控人", "季度快照（controller_* 待补源）",
     "pending_p2", "ts_code", "as_of_date"),
    ("index_daily", "T7", "指数日线", "沪深300/上证/深成/中证1000 四指数",
     "daily", "index_code", "date"),
    ("factor_snapshot", "T8", "因子快照（EAV）", "加因子零 schema 变更",
     "pending_p2", "ts_code", "as_of_date"),
    ("macro_rf", "T9", "无风险利率序列", "现值起步，历史缺口显式 NULL",
     "pending_p3", None, "date"),
]

# v6.0.5：P2/P3 计划内未启动表的徽章副文案（brief 逐字）。
_PENDING_DETAIL = {
    "fundamentals_quarterly": "P2·季度基本面待补",
    "holders_snapshot": "P2·股东+实控人待补",
    "factor_snapshot": "依赖T5/T6后计算",
    "macro_rf": "P3·rf现值序列未启动",
}

# v6.0.5：3 个派生 view（展示元数据；可用性由 adj_factor_coverage_pct 表达）。
_VIEW_META = [
    ("kline_daily_hfq", "后复权K线视图", "close×adj_factor"),
    ("kline_daily_qfq", "前复权K线视图", "按最新因子归一"),
    ("stock_panorama", "个股全景视图", "T1⋈T3 拼装"),
]


def _iso_date(v) -> Optional[str]:
    """date/datetime/str → 'YYYY-MM-DD'（None 透传）。"""
    if v is None:
        return None
    s = str(v)
    return s[:10]


def _iso_ts(v) -> Optional[str]:
    """TIMESTAMP → 'YYYY-MM-DD HH:MM:SS'（None 透传）。"""
    if v is None:
        return None
    s = str(v)
    return s[:19]


def _stopping_from_progress(prog: Dict[str, Any]) -> bool:
    """v6.0.10：从 progress 文件判定"停止收尾中"（/status stopping 字段数据源）。

    BackfillRunner 的 SIGTERM handler 在置 _stop_requested 时同步落盘
    ``stopping_at`` 时间戳 + tasks[].state="stopping"（见 lake.backfill._mark_stopping）；
    优雅停止收尾（_finish_stop）/新 run 开始时清除。跨进程可观测的唯一载体是库目录
    下的 progress JSON（Web 与灌数子进程不共享内存）。

    判定：``stopping_at`` 非空 **或** 任一 task entry state="stopping"（双保险——
    handler 写文件时 tasks 视图可能尚无 entry，此时只有 stopping_at；旧版 runner
    进程（v6.0.9）被信号打断时可能只留下 state 无 stopping_at）。
    """
    if prog.get("stopping_at"):
        return True
    for t in prog.get("tasks", []):
        if isinstance(t, dict) and t.get("state") == "stopping":
            return True
    return False


def _table_stats(con, table: str, code_col: Optional[str],
                 date_col: Optional[str]) -> Dict[str, Any]:
    """单表统计：rows / codes / date_min / date_max / last_sync_at（全运行时查询）。

    任何异常 → 该表统计置零/None（防御：某表缺列不拖垮整个 /status；正常库不会触发）。
    """
    out: Dict[str, Any] = {"rows": 0, "codes": None, "date_min": None,
                           "date_max": None, "last_sync_at": None}
    try:
        code_expr = f"COUNT(DISTINCT {code_col})" if code_col else "NULL"
        min_expr = f"MIN({date_col})" if date_col else "NULL"
        max_expr = f"MAX({date_col})" if date_col else "NULL"
        r = con.execute(
            f"SELECT COUNT(*), {code_expr}, {min_expr}, {max_expr}, MAX(fetched_at) "
            f"FROM {table}").fetchone()
    except Exception:  # noqa: BLE001 - 防御性降级（正常库不触发）
        return out
    out["rows"] = int(r[0] or 0)
    out["codes"] = None if r[1] is None else int(r[1])
    out["date_min"] = _iso_date(r[2])
    out["date_max"] = _iso_date(r[3])
    out["last_sync_at"] = _iso_ts(r[4])
    return out


def _classify_state(key: str, kind: str, rows: int, date_max: Optional[str],
                    ref_date: Optional[str], dividend_as_of: Optional[str]
                    ) -> tuple:
    """state 判定（brief v6.0.5 规则，全部运行时计算、零硬编码数据值）。

    :return: (state, state_detail)
    - pending：P2/P3 计划内未启动表——**即使 0 行也标 pending 而非 empty**
      （"计划内未做" ≠ "坏了"；前端 muted 弱化、不得显示成错误）。
    - daily（kline/valuation/index）：date_max 与参考最新交易日比**天数差**
      （不比较具体日期值）：gap≤1 → fresh；>1 → lagging"滞后 N 日"。
      ⚠️ 用日历天数而非交易日数——brief 字面规则即"−1 天/落后 >1 天"，且跨周末
      的日历差 ≤2 仍会判 lagging，这是可接受的保守口径（宁可提示滞后不误导最新）。
    - snapshot：stock_master rows>0 → fresh（周更语义，detail="快照"）；
      dividend_events rows>0 → fresh + detail="快照截至<源 as_of>"（as_of 拿不到
      就 "全史静态导入"）。
    - empty：其余 0 行表（当前库实况无此态，留作防御）。
    """
    if kind in ("pending_p2", "pending_p3"):
        return "pending", _PENDING_DETAIL[key]
    if rows <= 0:
        return "empty", "暂无数据"
    if kind == "daily":
        if not date_max or not ref_date:
            # 有行但日期缺失（异常防御）→ 按滞后处理并提示核对，不猜 fresh
            return "lagging", "日期缺失待核"
        gap = (datetime.date.fromisoformat(ref_date)
               - datetime.date.fromisoformat(date_max)).days
        if gap <= 1:
            return "fresh", "最新"
        return "lagging", f"滞后 {gap} 日"
    # snapshot（stock_master / dividend_events）
    if key == "dividend_events":
        as_of = (dividend_as_of or "").strip()
        detail = f"快照截至{as_of}" if as_of else "全史静态导入"
        return "fresh", detail
    return "fresh", "快照"


def _build_status_details(con, prog: Dict[str, Any]) -> Dict[str, Any]:
    """v6.0.5 新字段（仅 initialized+ready 态追加）：tables/views/adj/db/sync。

    - tables：9 张表固定顺序（T1-T9），逐表运行时统计 + state 判定；
      参考最新交易日 = kline_daily 全局 max(date)（无数据回退今日）。
    - dividend as_of 优先级：progress 文件 T4 记录 → dividend_events max(ann_date)
      → None（前端显示"全史静态导入"）。为什么先 progress：它是灌数侧的**源快照
      口径**（T4 全史一次性导入），比表内 max(ann_date) 更接近"源截至何时"。
    - adj_factor_coverage_pct：kline_daily adj_factor 非空占比（0-100，一位小数）——
      hfq/qfq view 可用性；P2 history 补全前大面积 NULL，前端 <5% 时加提示。
    - db.size_mb：os.path.getsize（不查库，零 IO 放大）。
    - sync.quota：读 progress 文件 tasks（不受 DuckDB 锁影响）——取今日已用/预算的
      max（各 task 视图写的是同一 QuotaGuard 值；max 防御个别 task 视图滞后）。

    ⚠️ **生产库只读纪律的实现口径（v6.0.5，实测约束）**：本函数全部为 SELECT，不写
    data/lake/ 任何文件。brief 要求"read_only 连接"，但 DuckDB 1.5.5 **禁止同进程
    混开 read_only 与默认连接**（实测 ConnectionException "Can't open a connection
    to same database file with a different configuration"——即使 RO 连接立刻 close
    也失败；而本进程已持有探测短连接，且该探测必须保持默认模式才能继续识别跨进程
    LakeLocked，v6.0.4 三态契约依赖它）。故沿用与其余端点一致的每请求短连接 +
    纯 SELECT（效果等价只读）；若未来 DuckDB 支持同进程混开，可升级为显式
    read_only=True。此偏差已如实记录，待 TL/tester 知悉。
    """
    from . import conn as _conn

    # 参考最新交易日：kline_daily 全局 max(date)；无数据回退今日（brief 规则）
    try:
        ref = con.execute("SELECT MAX(date) FROM kline_daily").fetchone()[0]
    except Exception:  # noqa: BLE001 - 防御（正常库必有 kline_daily）
        ref = None
    ref_date = _iso_date(ref) or datetime.date.today().isoformat()

    # dividend 源 as_of：progress T4 记录优先，回退表内 max(ann_date)
    div_as_of: Optional[str] = None
    for t in prog.get("tasks", []):
        if isinstance(t, dict) and t.get("table") == "dividend_events":
            for k in ("as_of", "source_as_of"):
                if t.get(k):
                    div_as_of = str(t[k])[:10]
                    break
    if not div_as_of:
        try:
            r = con.execute("SELECT MAX(ann_date) FROM dividend_events").fetchone()[0]
            div_as_of = _iso_date(r)
        except Exception:  # noqa: BLE001
            div_as_of = None

    tables: List[Dict[str, Any]] = []
    for key, tier, name_cn, desc, kind, code_col, date_col in _TABLE_META:
        st = _table_stats(con, key, code_col, date_col)
        state, detail = _classify_state(key, kind, st["rows"], st["date_max"],
                                        ref_date, div_as_of)
        tables.append({
            "key": key, "tier": tier, "name_cn": name_cn, "desc": desc,
            "rows": st["rows"], "codes": st["codes"],
            "date_min": st["date_min"], "date_max": st["date_max"],
            "last_sync_at": st["last_sync_at"],
            "state": state, "state_detail": detail,
        })

    # adj_factor 非空占比（复权 view 可用性）；kline 无行 → None（前端显示 —）
    try:
        tot, nn = con.execute(
            "SELECT COUNT(*), COUNT(adj_factor) FROM kline_daily").fetchone()
        af_pct = round(100.0 * int(nn or 0) / int(tot), 1) if tot else None
    except Exception:  # noqa: BLE001
        af_pct = None

    db_path = _conn.default_db_path()
    try:
        size_mb = round(os.path.getsize(db_path) / (1024 * 1024), 1) \
            if os.path.exists(db_path) else None
    except OSError:
        size_mb = None

    # quota：progress tasks 今日已用/预算（max 防御滞后视图）；无 tasks → None
    quota_used = quota_budget = None
    for t in prog.get("tasks", []):
        if not isinstance(t, dict):
            continue
        u, b = t.get("quota_used_today"), t.get("quota_budget")
        if isinstance(u, int):
            quota_used = max(quota_used or 0, u)
        if isinstance(b, int):
            quota_budget = max(quota_budget or 0, b)

    return {
        "tables": tables,
        "views": [{"key": k, "name_cn": n, "desc": d} for k, n, d in _VIEW_META],
        "adj_factor_coverage_pct": af_pct,
        "db": {"path": db_path, "size_mb": size_mb},
        "sync": {
            "last_updated_at": prog.get("updated_at"),
            "backfill_in_progress": False,
            "quota_used_today": quota_used,
            "quota_budget": quota_budget,
        },
    }


@router.get("/status")
def status() -> Dict[str, Any]:
    """补齐进度（§4 coverage + tasks）+ duckdb 安装状态 + **B-1 initialized**
    + **v6.0.4 backfill_in_progress** + **v6.0.5 tables/views/adj/db/sync**。

    B-1：库未就绪（缺文件/空文件/未 init_schema）时**不抛 409**，如实返回
    ``initialized=false`` + coverage 全零 + tasks 空——状态端点是健康检查，
    应反映真实状态。数据端点则一致降级为 409 lake_not_initialized（见 _con）。

    v6.0.4：库被灌数进程独占写锁持有时**不再误报未初始化**——返回
    ``initialized=true`` + ``backfill_in_progress=true`` + ``lock_holder_pid``
    （尽力解析，失败 None），coverage/tasks 降级读 progress 文件
    （backfill_progress.json 是纯 JSON、不受 DuckDB 锁影响，正好是灌数进度）。

    v6.0.5：**仅 ready 态**追加 tables（9 表逐表 state）/views/adj_factor_coverage_pct/
    db/sync——uninitialized 态响应体保持 v6.0.3/v6.0.4 逐字节不变（三态契约
    test_lake_v604_lock 依赖"新字段只在对应态追加"这一纪律）。

    v6.0.10：**locked 态**追加 ``stopping: bool``（停止收尾中——收到 SIGTERM、当前
    任务收尾中；数据源=progress 文件 stopping_at/tasks[].state，见
    :func:`_stopping_from_progress`）。前端据此显示"⏹ 停止中…（当前任务收尾中）"
    而非无反应。uninitialized/ready 两态不追加（stopping 只在 backfill_in_progress
    =true 时有意义；三态互不串味纪律延续 v6.0.4/v6.0.5）。
    """
    import duckdb

    from .backfill import load_progress

    state, con, holder_pid = _status_probe()
    if state == "locked":
        # v6.0.4：灌数持锁——库是好的（正在被写入），initialized=true；
        # coverage/tasks 来自 progress 文件降级（DuckDB 连不上，但 JSON 可读）。
        # v6.0.5：本分支**不追加**新字段（brief：tables 数组可缺省，降级路径不变）。
        # v6.0.10：追加 stopping（停止收尾中标志）——backfill_in_progress=true 时新增，
        # 前端据此区分"正常运行中" vs "停止收尾中"（友好文案而非无反应）。
        prog = load_progress()
        return {
            "installed": True,
            "duckdb_version": getattr(duckdb, "__version__", "?"),
            "initialized": True,
            "backfill_in_progress": True,
            "stopping": _stopping_from_progress(prog),
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
        # v6.0.5：9 表逐表统计 + state 判定（同一短连接内完成，全部只读 SELECT）
        details = _build_status_details(con, load_progress())
    finally:
        con.close()
    prog = load_progress()
    resp: Dict[str, Any] = {
        "installed": True,
        "duckdb_version": getattr(duckdb, "__version__", "?"),
        "initialized": True,
        "coverage": prog.get("coverage", {}),
        "tasks": prog.get("tasks", []),
        "updated_at": prog.get("updated_at"),
    }
    resp.update(details)  # v6.0.5 新字段只在 ready 态追加（旧字段逐字节不动）
    return resp


# ---------------------------------------------------------------------------
# v6.0.9：同步控制（启动/停止灌数）——Web"数据湖页"按钮后端
#
# 设计（TL 拍板）："启动" = scripts/lake_backfill.py history（P2 全史补库，
# setsid 脱离长跑）；"停止" = 优雅终止持锁进程（SIGTERM → BackfillRunner 任务间
# break + state=stopped_by_signal + save_progress，rc=0）。运行状态检测复用 v6.0.4
# 三态机制（probe_db_state + LakeLocked.holder_pid + os.kill(pid,0)），**不新增
# GET 端点**——前端用现有 /status 的 backfill_in_progress/lock_holder_pid 驱动按钮态。
#
# ⚠️ 生产库纪律：Web 服务进程对默认库（data/lake/lake.duckdb）操作 start/stop——
# 这正是 Joel 要的"随时启动/停止"；E2E/单测一律显式 tmp 库，绝不触碰生产灌数进程。
# ---------------------------------------------------------------------------
@router.post("/sync/start")
def sync_start(codes: Optional[str] = Query(default=None)) -> Dict[str, Any]:
    """启动全史数据补库（``lake_backfill.py history``，后台长跑）。

    :param codes: 可选逗号分隔股票子集（透传 driver ``--codes``）——**E2E/冒烟限定
        ≤3 只用**；Web 按钮不传 → 全集（Joel"启动数据更新"的默认语义）。
    - 200 ``{started: true, pid, log_path}``：spawn 成功且 ~5s 内确认 running。
    - **409** ``{error: "sync_already_running", hint, pid}``（顶层契约体）：已有灌数
      在跑（v6.0.4 三态检测）——防双开，不 spawn。
    - 200 ``{started: false, pid, log_path, reason}``：spawn 后进程提前退出/日志现
      traceback（启动失败但非冲突；前端 toast 展示 reason，不白屏）。
    """
    from . import sync_control

    # ⚠️ 直接函数调用（单测）时缺省值是 FieldInfo 对象而非 None——isinstance 归一化
    # （与 /kline days、/market page_size 同口径；HTTP 路径恒为 str/None，行为不变）。
    if not isinstance(codes, str):
        codes = None
    extra_args: Optional[List[str]] = None
    if codes and codes.strip():
        cs = [c.strip() for c in codes.split(",") if c.strip()]
        if cs:
            extra_args = ["--codes", ",".join(cs)]
    res = sync_control.start_sync(extra_args=extra_args)   # 缺省库（生产默认）
    if not res["started"] and res.get("reason") == "already_running":
        raise LakeSyncConflict(
            "sync_already_running",
            f"已有灌数在运行（PID={res['pid'] if res['pid'] is not None else '未知'}），"
            "请先停止再启动")
    return {k: v for k, v in res.items() if v is not None}


@router.post("/sync/stop")
def sync_stop() -> Dict[str, Any]:
    """优雅停止灌数（SIGTERM；进度已保存，下次启动自动续传）。

    **v6.0.10：异步**——发信号即返回 200 + ``waiting_task=true``（**不阻塞等进程
    退出**）；完成判定由前端轮询 /status（backfill_in_progress=false = 已停，stopping
    =true = 收尾中友好文案）。旧实现同步等到 timeout=30s 才响应 → 请求挂死、前端
    "点了没反应"（Joel 实测缺陷）。

    - 200 ``{stopped: false, pid, method, waiting_task: true, note}``：信号已发，
      正在等当前任务收尾（前端锁定按钮 + 轮询 /status）。
    - **409** ``{error: "sync_not_running", hint}``（顶层契约体）：当前无灌数在跑。
    - 200 ``{stopped: false, pid, method, waiting_task: false, reason}``：holder PID
      解析失败（拒绝猜测目标进程）/ 信号发送失败。
    """
    from . import sync_control

    res = sync_control.stop_sync()
    if not res["stopped"] and res.get("reason") == "not_running":
        raise LakeSyncConflict(
            "sync_not_running", "当前没有灌数在运行（状态可能刚更新，请刷新后重试）")
    return {k: v for k, v in res.items() if v is not None}
