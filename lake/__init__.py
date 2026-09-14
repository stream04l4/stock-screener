# -*- coding: utf-8 -*-
"""lake —— v6 本地 DuckDB 统一数据湖（独立分析层，非筛选热路径）。

设计边界（v6 brief + 调研报告 §2）：
- **单向依赖**：``lake/*`` 可 import ``screener.data.*``（只读复用现有客户端），
  但 ``screener/*``、``web/app.py``、``backtest/*`` **绝不** import lake。
  web/app.py 仅在 duckdb 可导入时条件挂载 router（否则页签显示"数据湖未安装"）。
- 零网络/优雅降级：duckdb 未装 → :func:`lake_conn` 返回 None，主路径行为逐字节不变。
- 存储：单文件 ``data/lake/lake.duckdb``（原生表 + PK upsert）+ 按需 Parquet(zstd) 导出。

⚠️ T6 ``holders_snapshot.controller_*`` 三列**本期无源**（TL Q1 拍板：不引入新源），
保留 NULL + 文档标注"待补源"；soe 识别继续用 v5 规则（关键词 + 股本性质）。
"""
from __future__ import annotations

import logging

log = logging.getLogger("lake")

__version__ = "6.0"


def lake_conn(db_path=None):
    """惰性单例连接：返回 duckdb Connection；**duckdb 未装 → None（优雅降级）**。

    :param db_path: 可选显式路径（测试用 tmp 库）；缺省 = data/lake/lake.duckdb。
        传了显式 path 则不复用单例（每次新开，便于隔离）。
    """
    from . import conn as _conn

    if not _conn.duckdb_available():
        return None
    if db_path is None:
        return _conn.get_conn()
    # 显式路径：open() 内部已调用 ddl.init_schema（IF NOT EXISTS，幂等），
    # 此处**不再**重复调 init_schema——v6.0.1 D-1 修复：原代码误写
    # ``_conn.init_schema``（lake.conn 无此属性，必抛 AttributeError）；
    # 删除该行而非改 import，因为 conn.open 已覆盖 schema 初始化职责。
    return _conn.open(db_path)


def duckdb_available() -> bool:
    """duckdb 是否可导入（未装 → False，主路径据此降级）。"""
    from . import conn as _conn

    return _conn.duckdb_available()


def export_parquet(table: str, out_dir: str) -> str:
    """把单表 COPY 成按 date hive 分区 Parquet(zstd)。返回输出目录。"""
    from . import conn as _conn

    return _conn.export_parquet(table, out_dir)
