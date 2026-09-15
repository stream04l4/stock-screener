# -*- coding: utf-8 -*-
"""lake.conn —— DuckDB 连接管理（单文件 data/lake/lake.duckdb + schema 初始化）。

- duckdb 未装 → :func:`duckdb_available` False，:func:`get_conn` 抛 LakeUnavailable
  （调用方 lake.lake_conn() 已先行判断返回 None，正常路径不会到这里）。
- 单例：同进程复用同一 Connection，**仅供 backfill/ingest 写路径**（写事务由
  LakeLock flock 跨进程串行化）。⚠️ 该单例**不得跨线程并发 execute**——实测
  同一 Connection 对象并发执行不同 SQL 会交错结果集（v6.0.1 D-4）；Web 读路径
  一律用 :func:`connect_existing` 每请求短连接。
- advisory lock：跨进程并发写保护用文件锁（fcntl.flock）——数据湖是后台分析层，
  多进程（web + backfill cron）可能同时打开单文件 DuckDB；读多写少，DuckDB 自身
  对同文件多连接支持有限，故**写路径**（ingest/backfill）持 flock 串行化。
"""
from __future__ import annotations

import builtins
import fcntl
import logging
import os
from typing import Optional

log = logging.getLogger("lake.conn")


class LakeUnavailable(RuntimeError):
    """duckdb 未安装或数据库不可用（主路径不应触发；lake 内部显式失败）。"""


# D-1（v6.0.3 rework）：duckdb 对"存在但非合法库文件"（0 字节 touch / 内容损坏）
# 抛 _duckdb.IOException，消息含此子串。用它把"连 duckdb 都打不开的库文件"归一为
# LakeInvalidFile——语义 = schema 从未建立 = "未就绪"（Web→409），不是"服务不可用"
# （503）。在 connect 层统一判定，Web 与 CLI 各自转译（见 web_api._con / lake_backfill）。
_INVALID_DB_MARKERS = ("not a valid DuckDB database file",)


def _is_invalid_db_error(exc: BaseException) -> bool:
    """duckdb IO Error 消息匹配 → 该文件不是合法库文件（0 字节/损坏）。"""
    return any(m in str(exc) for m in _INVALID_DB_MARKERS)


class LakeInvalidFile(LakeUnavailable):
    """库文件存在但**不是合法 DuckDB 库**（0 字节空文件 / 内容损坏，duckdb 打不开）。

    D-1（v6.0.3 rework）：与"缺文件/未 init_schema"同属"未就绪"——schema 从未建立。
    Web 数据端点 → LakeNotInitialized(409)；CLI → 友好报错退出码 3（不崩 traceback）。
    继承 LakeUnavailable 使既有 ``except LakeUnavailable`` 兜底路径行为不变（仍友好
    降级，只是消息更准）；精确处理方 catch 本类。
    """

    def __init__(self, db_path: str) -> None:
        super().__init__(
            f"库文件不是有效的 DuckDB 数据库（0 字节空文件或内容损坏）：{db_path}"
            "——schema 从未建立，请先执行 init")
        self.db_path = db_path


def _check_invalid_db_file(db_path: str) -> None:
    """connect 前探测：文件存在且 **size==0** → LakeInvalidFile（不 connect）。

    为什么先探测而不是只靠 catch：duckdb.connect(0字节文件) 直接抛 IO Error，catch
    也能兜住，但 size 探测零成本、消息更直白，且不依赖 duckdb 错误文案稳定性
    （双保险：探测 + catch 两条路都归一到 LakeInvalidFile）。
    """
    if os.path.exists(db_path) and os.path.getsize(db_path) == 0:
        raise LakeInvalidFile(db_path)


def _connect_or_raise_invalid(duckdb, db_path: str):
    """duckdb.connect 的归一包装：IO Error "not a valid DuckDB database file" →
    LakeInvalidFile（覆盖非 0 字节但内容损坏的文件）。其余异常原样上抛。"""
    try:
        return duckdb.connect(db_path)
    except Exception as exc:  # noqa: BLE001 - 仅归一"无效库文件"，其余透传
        if _is_invalid_db_error(exc):
            raise LakeInvalidFile(db_path) from exc
        raise


def duckdb_available() -> bool:
    try:
        import duckdb  # noqa: F401

        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
def _project_root() -> str:
    # lake/ 的上一级 = 仓库根（~/stock-screener）
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def default_db_path() -> str:
    """单文件库路径：data/lake/lake.duckdb（相对仓库根）。"""
    return os.path.join(_project_root(), "data", "lake", "lake.duckdb")


def progress_path() -> str:
    """补齐进度文件：data/lake/backfill_progress.json（§4 结构）。"""
    return os.path.join(_project_root(), "data", "lake", "backfill_progress.json")


# ---------------------------------------------------------------------------
# 连接
# ---------------------------------------------------------------------------
_conn = None  # 进程级单例（默认库）


def open(db_path: Optional[str] = None):
    """打开（或创建）DuckDB 文件库并初始化 schema。未装 duckdb → LakeUnavailable。"""
    global _conn
    if not duckdb_available():
        raise LakeUnavailable("duckdb 未安装（uv sync --extra lake）")
    import duckdb

    db_path = db_path or default_db_path()
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    from .ddl import init_schema

    # D-1：0 字节/无效库文件 → LakeInvalidFile（不 connect，避免裸 IO Error traceback）
    _check_invalid_db_file(db_path)
    con = _connect_or_raise_invalid(duckdb, db_path)
    init_schema(con)
    return con


def get_conn():
    """进程级单例（默认库）。未装 duckdb → LakeUnavailable。

    ⚠️ 语义不变（D-4 修复说明）：本函数仍供 **backfill/ingest 写路径**使用
    （单例 + LakeLock flock 串行化）。**Web 读路径不得跨线程共用该单例**——
    实测同一 Connection 对象并发 execute(不同 SQL) 会交错结果集（v6.0.1 D-4，
    旧 docstring "DuckDB 连接内建多线程安全" 不成立）。Web 端点一律走
    :func:`connect_existing` 每请求短连接。
    """
    global _conn
    if _conn is None:
        _conn = open()
    return _conn


def connect_existing(db_path: Optional[str] = None):
    """轻量只读短连接（Web 端点每请求专用）：仅 duckdb.connect，**不执行 init_schema**。

    v6.0.1 D-4：lake.web_api 原用 get_conn() 进程级单例跨 FastAPI 线程池并发
    execute → 结果集交错/500。改为每请求新开短连接（DuckDB 文件库支持多连接
    并发读），用完即 close。

    - **不走 :func:`open`**：open() 每次执行 init_schema 全量 DDL，每请求跑一遍
      不可接受；Web 只读，schema 由 backfill（写路径）负责建立。
    - 文件不存在/打不开 → duckdb 抛错，由调用方（web_api._con）转 503。
    - **D-1（v6.0.3 rework）**：0 字节/无效库文件 → :class:`LakeInvalidFile`
      （connect 前 size 探测 + connect IO Error 归一，双保险）。
    - 不触碰进程级单例 ``_conn``——与写路径完全隔离。

    :param db_path: 缺省 = default_db_path()；测试可传 tmp 库路径。
    """
    if not duckdb_available():
        raise LakeUnavailable("duckdb 未安装（uv sync --extra lake）")
    import duckdb

    p = db_path or default_db_path()
    _check_invalid_db_file(p)
    return _connect_or_raise_invalid(duckdb, p)


def reset_for_test() -> None:
    """测试隔离：丢弃单例（下次 get_conn 重开）。"""
    global _conn
    if _conn is not None:
        try:
            _conn.close()
        except Exception:  # noqa: BLE001
            pass
    _conn = None


class LakeLock:
    """跨进程写锁（fcntl.flock）：ingest/backfill 持锁串行化对单文件库的写。

    DuckDB 同文件多连接并发写可能冲突；数据湖读多写少，写路径持此锁即可。
    读路径（web_api 查询）不持锁——DuckDB 读对已提交快照一致。
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._lock_path = (db_path or default_db_path()) + ".write.lock"
        self._fh = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self._lock_path), exist_ok=True)
        # N-1 修复（v6.0.2）：必须用 builtins.open——本模块定义了模块级
        # ``def open(db_path=None)``（DuckDB 连接工厂），裸调 open() 会被遮蔽，
        # 把 "a+" 当作 DuckDB db_path 去 duckdb.connect("a+") → 必抛异常，
        # LakeLock 完全不可用。同理检查过全文件：无其它被遮蔽的内置调用
        # （abs/input 等未被本模块重定义）。
        self._fh = builtins.open(self._lock_path, "a+")
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if self._fh is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None
        return False


# ---------------------------------------------------------------------------
# Parquet 导出（Q5：按 date hive 分区 + zstd）
# ---------------------------------------------------------------------------
def export_parquet(table: str, out_dir: Optional[str] = None, con=None) -> str:
    """COPY table → out_dir/<table>/date=YYYY-MM-DD/*.parquet（zstd）。返回目录。

    :param con: 可选显式连接（测试用 tmp 库）；缺省用单例 get_conn()。
    """
    con = con or get_conn()
    out_dir = out_dir or os.path.join(
        _project_root(), "data", "lake", "parquet", table)
    os.makedirs(out_dir, exist_ok=True)
    target = os.path.join(out_dir, table)
    # hive 分区：PARTITION_BY(date)；zstd 压缩（Q5）。
    # OVERWRITE：目标目录非空时先清空再写——v6.0.1 D-2 修复：无此选项时
    # 对同一目标二次导出必抛 "Directory ... is not empty! Enable OVERWRITE"，
    # 破坏 P2 快照"同目录重导 = 幂等覆盖"的语义。
    con.execute(
        f"COPY (SELECT * FROM {table}) TO '{target}' "
        "(FORMAT PARQUET, PARTITION_BY (date), COMPRESSION zstd, OVERWRITE)"
    )
    return target


def read_parquet(table: str, out_dir: Optional[str] = None, con=None):
    """读回 hive 分区 Parquet（HIVE_PARTITIONING=1）。返回 DuckDB Relation。"""
    con = con or get_conn()
    base = out_dir or os.path.join(_project_root(), "data", "lake", "parquet")
    pattern = os.path.join(base, table, "**", "*.parquet")
    return con.execute(
        f"SELECT * FROM read_parquet('{pattern}', HIVE_PARTITIONING=1)"
    )
