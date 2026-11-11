# -*- coding: utf-8 -*-
"""crypto.models —— crypto.duckdb 表 DDL + 幂等 upsert + 统计（框架 v7.0.43）。

独立湖 data/crypto/crypto.duckdb（加密货币系统数据独立目录，与 A 股湖物理隔离）。
表：
- coin_master：币种主数据
- kline_1h：1h K 线（PK(exchange, symbol, ts)，INSERT OR REPLACE 幂等）
1d/4h 由 1h 本地聚合，不建表（避免多粒度双写不一致）。
路径一律由调用方从 crypto/config/crypto.yaml 传入（models 不读配置）。
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import List

import duckdb

SCHEMA = """
CREATE TABLE IF NOT EXISTS coin_master (
    symbol     VARCHAR PRIMARY KEY,
    name       VARCHAR,
    exchange   VARCHAR DEFAULT 'binance',
    is_active  BOOLEAN DEFAULT TRUE,
    added_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS kline_1h (
    exchange     VARCHAR,
    symbol       VARCHAR,
    ts           BIGINT,          -- 开盘时间 epoch 秒
    open         DOUBLE,
    high         DOUBLE,
    low          DOUBLE,
    close        DOUBLE,
    volume       DOUBLE,
    quote_volume DOUBLE,
    PRIMARY KEY (exchange, symbol, ts)
);

CREATE INDEX IF NOT EXISTS idx_kline_1h_symbol_ts ON kline_1h (symbol, ts);
"""


def open_db(lake_path: str) -> duckdb.DuckDBPyConnection:
    """打开湖（自动建目录 + 建表）。lake_path = 湖文件绝对路径（来自配置）。"""
    Path(lake_path).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(lake_path)
    con.execute(SCHEMA)
    return con


def upsert_coins(con, coins: List[str]) -> None:
    # DuckDB：OR REPLACE 与 ON CONFLICT 不能并用 → 用 ON CONFLICT（保留 added_at）
    for sym in coins:
        con.execute(
            "INSERT INTO coin_master (symbol, name, exchange, is_active) "
            "VALUES (?, ?, 'binance', TRUE) "
            "ON CONFLICT (symbol) DO UPDATE SET is_active = TRUE",
            [sym, sym],
        )


def deactivate_others(con, active_coins: List[str]) -> int:
    """清单外币种置 is_active=FALSE（数据保留，可恢复）。返回停用数。"""
    if not active_coins:
        return 0
    ph = ",".join("?" * len(active_coins))
    con.execute(
        f"UPDATE coin_master SET is_active = FALSE WHERE symbol NOT IN ({ph})",
        active_coins,
    )
    # DuckDB UPDATE 无直接行数 → 查差异计数
    row = con.execute(
        "SELECT COUNT(*) FROM coin_master WHERE is_active = FALSE"
    ).fetchone()
    return int(row[0]) if row else 0


def upsert_klines(con, exchange: str, symbol: str, bars: list) -> int:
    """幂等写入 1h K 线（PK 冲突替换）。返回写入行数。"""
    if not bars:
        return 0
    rows = [(exchange, symbol, b["ts"], b["open"], b["high"], b["low"],
             b["close"], b["volume"], b["quote_volume"]) for b in bars]
    con.executemany(
        "INSERT OR REPLACE INTO kline_1h "
        "(exchange, symbol, ts, open, high, low, close, volume, quote_volume) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def stats(lake_path: str) -> dict:
    """数据湖 tab 用：表清单 + 行数 + 每币 K 线范围 + 库大小。湖不存在 → 空结构。

    锁语义：read_only 连接与写锁互斥——离线同步（cron）持锁期间（秒级）
    打开会失败 → 短重试（3×0.5s），绝大多数情况第一轮即中。
    """
    if not os.path.exists(lake_path):
        return {"installed": False, "tables": [], "size_mb": 0.0}
    con = None
    for _ in range(3):
        try:
            con = duckdb.connect(lake_path, read_only=True)
            break
        except Exception:  # noqa: BLE001 - 写锁竞争
            time.sleep(0.5)
    if con is None:
        return {"installed": False, "tables": [], "size_mb": 0.0,
                "error": "湖被写入占用（稍后重试）"}
    try:
        out = {"installed": True, "size_mb": round(os.path.getsize(lake_path) / 1048576, 2),
               "tables": [], "per_symbol": []}
        tables = [r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='main' ORDER BY 1").fetchall()]
        for t in tables:
            n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            out["tables"].append({"name": t, "rows": n})
        per_sym = con.execute("""
            SELECT m.symbol, m.n_rows, m.mn, m.mx, k.close
            FROM (SELECT symbol, COUNT(*) n_rows, MIN(ts) mn, MAX(ts) mx
                  FROM kline_1h WHERE exchange='binance' GROUP BY symbol) m
            JOIN kline_1h k ON k.symbol = m.symbol AND k.ts = m.mx
                 AND k.exchange = 'binance'
            ORDER BY m.symbol
        """).fetchall()
        import datetime as _dt
        for sym, n, mn, mx, last in per_sym:
            out["per_symbol"].append({
                "symbol": sym, "rows": n,
                "from": _dt.datetime.fromtimestamp(mn).strftime("%Y-%m-%d %H:%M"),
                "to": _dt.datetime.fromtimestamp(mx).strftime("%Y-%m-%d %H:%M"),
                "last_close": last,  # 最新一根 K 线收盘价（非历史最高）
            })
        return out
    finally:
        con.close()
