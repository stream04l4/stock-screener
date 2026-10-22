#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v7.0.7 生产湖迁移：砍 T5 roe_avg/ocf + T6 controller_* 死列（Joel 2026-10-22 拍板）。

背景：五列全表恒 NULL（源无可靠口径：adata 无简单平均 ROE、ocf 只有每股值；
sina F10 无实控人字段），当前 martingale 策略零引用——不留死字段。

纪律：
- 幂等：逐列探测存在性，已砍 → skip（重复跑安全）。
- 锁：写模式连接（DuckDB 单文件锁；增量/T3 持锁期间本脚本拿不到锁会阻塞等待，
  由外部调度器保证只在锁空窗跑，不自行强抢）。
- 迁移只动 schema（ALTER DROP COLUMN，无数据改写）；迁移后旧 /tmp 副本不受影响。

用法：.venv/bin/python scripts/lake_migrate_v707.py
"""
from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
DB = os.path.join(REPO, "data", "lake", "lake.duckdb")

DROPS = [
    ("fundamentals_quarterly", "roe_avg"),
    ("fundamentals_quarterly", "ocf"),
    ("holders_snapshot", "controller_name"),
    ("holders_snapshot", "controller_type"),
    ("holders_snapshot", "controller_ratio"),
]


def main() -> int:
    import duckdb

    print(f"[migrate-v707] 连接生产湖（写模式，等锁）: {DB}")
    con = duckdb.connect(DB)
    try:
        for table, col in DROPS:
            cols = {r[0] for r in con.execute(f'DESCRIBE "{table}"').fetchall()}
            if col not in cols:
                print(f"[migrate-v707] {table}.{col}: 已不存在 → skip")
                continue
            con.execute(f'ALTER TABLE "{table}" DROP COLUMN "{col}"')
            print(f"[migrate-v707] {table}.{col}: 已砍")
        # 迁移后自检：生产库列集与新 DDL 建出的内存湖完全一致
        from lake import ddl as lddl
        mem = duckdb.connect()  # 内存湖，无锁竞争
        try:
            for stmt in list(lddl.DDL_STATEMENTS) + list(lddl.INDEX_STATEMENTS):
                mem.execute(stmt)
            for table in ("fundamentals_quarterly", "holders_snapshot"):
                prod = [r[0] for r in con.execute(f'DESCRIBE "{table}"').fetchall()]
                fresh = [r[0] for r in mem.execute(f'DESCRIBE "{table}"').fetchall()]
                assert prod == fresh, f"{table} 列集不一致:\n prod={prod}\n fresh={fresh}"
                print(f"[migrate-v707] {table}: 列集与新 DDL 一致（{len(prod)} 列）")
        finally:
            mem.close()
        con.close()
        print("[migrate-v707] 完成")
        return 0
    finally:
        try:
            con.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
