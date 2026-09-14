# -*- coding: utf-8 -*-
"""lake.conn —— DuckDB 连接管理（单文件 data/lake/lake.duckdb + schema 初始化）。

- duckdb 未装 → :func:`duckdb_available` False，:func:`get_conn` 抛 LakeUnavailable
  （调用方 lake.lake_conn() 已先行判断返回 None，正常路径不会到这里）。
- 单例：同进程复用同一 Connection（DuckDB 连接内建多线程安全；写事务串行化）。
- advisory lock：跨进程并发写保护用文件锁（fcntl.flock）——数据湖是后台分析层，
  多进程（web + backfill cron）可能同时打开单文件 DuckDB；读多写少，DuckDB 自身
  对同文件多连接支持有限，故**写路径**（ingest/backfill）持 flock 串行化。
"""
from __future__ import annotations

import fcntl
import logging
import os
from typing import Optional

log = logging.getLogger("lake.conn")


class LakeUnavailable(RuntimeError):
    """duckdb 未安装或数据库不可用（主路径不应触发；lake 内部显式失败）。"""


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

    con = duckdb.connect(db_path)
    init_schema(con)
    return con


def get_conn():
    """进程级单例（默认库）。未装 duckdb → LakeUnavailable。"""
    global _conn
    if _conn is None:
        _conn = open()
    return _conn


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
        self._fh = open(self._lock_path, "a+")
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
