# -*- coding: utf-8 -*-
"""lake.migrate_conflict_col —— v6.1 幂等迁移：给已存在库补 conflict_src 列（Q5）。

背景：v6.1 在 T1-T7 DDL 追加 ``conflict_src VARCHAR``。**新库**走 init_schema 直接建出；
**旧库**（v6.0.x 建的，无此列）需要 ALTER 补列。本模块对已存在库逐表执行
``ALTER TABLE <t> ADD COLUMN IF NOT EXISTS conflict_src VARCHAR``——幂等（重复跑零副作用）。

为什么单独一个迁移模块而不是只改 ddl.py：
- init_schema 的 CREATE TABLE IF NOT EXISTS 对**已存在**的表是 no-op——旧表不会自动补列；
- 生产库 data/lake/lake.duckdb（145万行 K线）必须真实跑一遍本迁移（brief 要求），
  ALTER ADD COLUMN 在 DuckDB 是元数据操作（不重写数据），应秒级完成。

用法（CLI）::

    python -m lake.migrate_conflict_col [--db PATH]

纪律：只加列、不碰任何数据；逐表校验前后行数一致（防御性，ALTER ADD COLUMN 理论上
不可能改行数——若不一致立即报错中止，绝不静默）。
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import Any, Dict, List, Optional

log = logging.getLogger("lake.migrate_conflict_col")


def _project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _table_row_count(con, table: str) -> int:
    return int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def migrate_conflict_col(con) -> Dict[str, Any]:
    """对连接指向的库逐表补 conflict_src 列（幂等）。

    :param con: duckdb Connection（已 init_schema 或旧 schema——两种都安全）。
    :return: {table: {"added": bool, "elapsed_s": float}} + 汇总。
        added=True=本次 ALTER 实际加了列；False=列已存在（IF NOT EXISTS no-op）。

    实现说明：DuckDB 的 ``ADD COLUMN IF NOT EXISTS`` 对已存在列是静默 no-op，无法从
    语句本身区分"加了/没加"——故先查 information_schema.columns 判存在性（added 语义
    准确），再执行 ALTER（双保险：即使判定有竞态也幂等）。
    """
    from .ddl import CONFLICT_SRC_TABLES

    result: Dict[str, Any] = {}
    for table in CONFLICT_SRC_TABLES:
        t0 = time.monotonic()
        exists = con.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name=? AND column_name='conflict_src'", [table]).fetchone()
        before = _table_row_count(con, table)
        if not exists:
            con.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS conflict_src VARCHAR")
        added = not bool(exists)
        after = _table_row_count(con, table)
        # 防御性红线：ALTER ADD COLUMN 不得改行数——不一致=迁移出了预期外的事，立即抛错
        if before != after:
            raise RuntimeError(
                f"migrate_conflict_col {table}: 行数变化 {before}→{after}（不应发生）——中止")
        result[table] = {"added": added, "elapsed_s": round(time.monotonic() - t0, 3),
                         "rows_before": before, "rows_after": after}
        log.info("migrate %s: conflict_src %s (%.3fs, rows=%d)",
                 table, "ADDED" if added else "already present",
                 result[table]["elapsed_s"], after)
    result["summary"] = {
        "tables_migrated": len(result),
        "columns_added": sum(1 for v in result.values() if isinstance(v, dict) and v.get("added")),
    }
    return result


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(
        prog="python -m lake.migrate_conflict_col",
        description="v6.1 幂等迁移：T1-T7 补 conflict_src 列（只加列不碰数据）")
    p.add_argument("--db", default=None,
                   help="DuckDB 库路径（缺省 data/lake/lake.duckdb）")
    args = p.parse_args(argv)

    from . import conn as lconn
    from .ddl import CONFLICT_SRC_TABLES

    db_path = args.db or lconn.default_db_path()
    con = lconn.open(db_path)  # open() 内部 init_schema（新库直接建出；旧库 no-op）
    try:
        res = migrate_conflict_col(con)
    finally:
        try:
            con.close()
        except Exception:  # noqa: BLE001
            pass

    import json as _json
    print(f"\n===== migrate_conflict_col ({db_path}) =====")
    print(_json.dumps(res, ensure_ascii=False, indent=2))
    # 汇总打印：便于 TL/Joel 核对"只加列不碰数据"
    for t in CONFLICT_SRC_TABLES:
        r = res.get(t)
        if r and r["rows_before"] != r["rows_after"]:
            print(f"错误: {t} 行数不一致", file=sys.stderr)
            return 1
    print(f"\nOK: {res['summary']['columns_added']} 列新增 / "
          f"{res['summary']['tables_migrated'] - res['summary']['columns_added']} 列已存在")
    return 0


if __name__ == "__main__":
    sys.exit(main())
