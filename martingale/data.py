# -*- coding: utf-8 -*-
"""martingale.data —— 数据湖只读访问（独立于 lake 包，零 import lake）。

语义同 lake.conn 的**读路径**（connect_existing / probe_db_state），但本模块
自包含——martingale 与 lake 是平级包，互不 import（test_lake_zero_import 双向守卫）。

错误归一（为什么需要：CLI/Web 都要把 duckdb 裸 IO Error 转成可展示的中文文案）：
- duckdb 未装 → MartingaleUnavailable
- 库文件不存在 → LakeMissing（"数据湖尚未建立"，不是 bug）
- 0 字节/内容损坏 → LakeInvalidFile（schema 从未建立）
- 被另一进程独占写锁持有（lake-sync/backfill 灌数中）→ LakeLocked（"灌数进行中，
  稍后重试"——调研 ⑤ 风险 6：Web 展示 + 可重试）

本包全部查询走 read_only=True：回测/查询绝不写湖。
"""
from __future__ import annotations

import os
from typing import Optional


class MartingaleUnavailable(RuntimeError):
    """duckdb 未安装或数据库不可用。"""


class LakeMissing(MartingaleUnavailable):
    """数据湖文件不存在（尚未建立）。"""

    def __init__(self, db_path: str) -> None:
        super().__init__(f"数据湖不存在：{db_path}——请先运行 lake 初始化/灌数")
        self.db_path = db_path


class LakeInvalidFile(MartingaleUnavailable):
    """库文件存在但不是合法 DuckDB 库（0 字节/损坏，schema 从未建立）。"""

    def __init__(self, db_path: str) -> None:
        super().__init__(
            f"数据湖不是有效的 DuckDB 数据库（0 字节空文件或内容损坏）：{db_path}")
        self.db_path = db_path


class LakeLocked(MartingaleUnavailable):
    """库文件被另一进程独占写锁持有（lake-sync/backfill 灌数进行中）。"""

    def __init__(self, db_path: str, holder_pid: Optional[int] = None) -> None:
        super().__init__(
            f"数据湖被其他进程锁定（backfill 灌数进行中，持锁 PID={holder_pid if holder_pid is not None else '未知'}）："
            f"{db_path}——稍后重试")
        self.db_path = db_path
        self.holder_pid = holder_pid


# 与 lake.conn 同文案模式（两个子串同时命中才判 locked，避免误伤其它 IO Error）
_INVALID_DB_MARKERS = ("not a valid DuckDB database file",)
_LOCKED_DB_MARKERS = ("Could not set lock", "Conflicting lock")


def _is_invalid_db_error(exc: BaseException) -> bool:
    return any(m in str(exc) for m in _INVALID_DB_MARKERS)


def _is_locked_db_error(exc: BaseException) -> bool:
    return all(m in str(exc) for m in _LOCKED_DB_MARKERS)


def _parse_lock_holder_pid(msg: Optional[str]) -> Optional[int]:
    import re as _re

    m = _re.search(r"\(PID\s+(\d+)\)", msg or "")
    return int(m.group(1)) if m else None


def _project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def default_db_path() -> str:
    """单文件库路径：data/lake/lake.duckdb（相对仓库根）。"""
    return os.path.join(_project_root(), "data", "lake", "lake.duckdb")


def connect_readonly(db_path: Optional[str] = None):
    """打开 read-only 短连接；错误归一后上抛（调用方转 CLI 退出码 / Web 409-503）。

    :param db_path: 缺省 = data/lake/lake.duckdb；测试可传 tmp 合成库。
    """
    try:
        import duckdb
    except ImportError:
        raise MartingaleUnavailable("duckdb 未安装（uv pip install duckdb）")

    p = db_path or default_db_path()
    if not os.path.exists(p):
        raise LakeMissing(p)
    if os.path.getsize(p) == 0:
        raise LakeInvalidFile(p)
    try:
        return duckdb.connect(p, read_only=True)
    except Exception as exc:  # noqa: BLE001 - 仅归一两类已知 IO Error，其余透传
        if _is_locked_db_error(exc):
            raise LakeLocked(p, _parse_lock_holder_pid(str(exc))) from exc
        if _is_invalid_db_error(exc):
            raise LakeInvalidFile(p) from exc
        raise
