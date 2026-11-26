#!/usr/bin/env python3
"""估值窗口本地推导（P1：tushare valuation 15 天 78,225 行清除后的回填）。

tushare 源全弃用（Joel 2026-11-25 拍板）。窗口 2026-10-26→11-25 内
tushare 估值行是唯一来源（无同日 tencent/em_batch 替代），直接删会致
回测尾段市值/收益门失守。本脚本从 2026-10-23（窗口前最后干净日，
bd_static/tencent 锚点）本地推导，不触网：

- ttm_profit（TTM 净利润）与 net_assets 假设 15 日内不变（无财报窗口内发布）
- shares 仅被送股（stk_div>0）事件按 ×(1+B/10) 变更（事件表）
- total_mv = ttm_profit 口径反推：anchor 日 pe_ttm 给出 profit=mv/pe；
  窗口日 mv = profit × pe_anchor × (close_d/close_a) × (shares_d/shares_a)
- pe_ttm = mv / profit（即 anchor_pe × 价格比 × 股本比）
- pb    = anchor_pb × (mv_d/mv_a) / (net_asset 近似不变，分红影响忽略并注释)
- ttm_yield_pct：复用 baidu_valuation_adapter._derive_yield 本地 SQL
  （T4 TTM DPS÷close，依赖新 af —— 必须在 af apply 之后跑）

口径与假设写死在代码注释；推导行 source='derived_from_bd'，
fetched_at=NOW()，可被后续真实源 REPLACE 覆盖。

用法：
  .venv/bin/python scripts/derive_valuation_window.py            # 全窗口
  .venv/bin/python scripts/derive_valuation_window.py --codes sh.600000
  .venv/bin/python scripts/derive_valuation_window.py --dry-run
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

sys.path.insert(0, ".")

import duckdb  # noqa: E402

DB = "data/lake/lake.duckdb"
ANCHOR = "2026-10-23"          # 窗口前最后干净估值日（tushare 覆盖从 10-26 起）
START = "2026-10-26"
END = "2026-11-25"
SRC = "derived_from_bd"
log = logging.getLogger("derive_valuation")


def _anchor_map(con, codes) -> dict:
    """每只 anchor 日（<=ANCHOR 最近有 total_mv 的日）pe/pb/mv/close。
    股本项代数上消去（shares_d/shares_a=cum 已折入 mv 公式），无需股本列。"""
    out = {}
    for code in codes:
        row = con.execute("""
            SELECT v.date, v.pe_ttm, v.pb, v.total_mv, k.close
            FROM valuation_daily v
            JOIN kline_daily k
              ON k.ts_code=v.ts_code AND k.date=v.date
            WHERE v.ts_code=? AND v.date<=?
              AND v.total_mv IS NOT NULL
            ORDER BY v.date DESC LIMIT 1
        """, [code, ANCHOR]).fetchone()
        if row:
            out[code] = row
    return out


def _bonus_shares_factor(con, code: str) -> dict:
    """窗口内送股事件的 (ex_date → 1+B/10) 因子（除权后生效）。"""
    fac = {}
    for exd, stk in con.execute("""
        SELECT ex_date, COALESCE(SUM(stk_div),0)
        FROM dividend_events
        WHERE ts_code=? AND ex_date BETWEEN ? AND ?
        GROUP BY ex_date
    """, [code, START, END]).fetchall():
        b = float(stk or 0)
        if b > 0:
            fac[str(exd)[:10]] = 1.0 + b / 10.0
    return fac


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", nargs="*", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    con = duckdb.connect(DB)
    if args.codes:
        codes = args.codes
    else:
        codes = [r[0] for r in con.execute(
            "SELECT DISTINCT ts_code FROM valuation_daily "
            f"WHERE source='tushare' AND date BETWEEN ? AND ?",
            [START, END]).fetchall()]
    log.info("codes=%d anchor=%s window=%s..%s", len(codes), ANCHOR, START, END)

    anchors = _anchor_map(con, codes)
    no_anchor = [c for c in codes if c not in anchors]
    log.info("anchor found=%d missing=%d", len(anchors), len(no_anchor))

    n_rows = 0
    for code in codes:
        if code not in anchors:
            continue
        a_date, a_pe, a_pb, a_mv, a_close = anchors[code]
        if not (a_pe and a_pb and a_mv and a_close):
            continue
        fac = _bonus_shares_factor(con, code)
        # 目标回填日 = 该只窗口内每个交易日（tushare 估值行将被本 upsert 替换）
        closes = {str(d)[:10]: c for d, c in con.execute(
            "SELECT date, close FROM kline_daily WHERE ts_code=? "
            f"AND date BETWEEN ? AND ?", [code, START, END]).fetchall()}
        bonus_dates = sorted(fac.keys())
        rows = []
        for d, c in sorted(closes.items()):
            cum = 1.0
            for bd in bonus_dates:
                if d >= bd:
                    cum *= fac[bd]
            price_ratio = c / a_close
            mv = a_mv * price_ratio * cum
            rows.append((code, d, round(a_pe * price_ratio * cum, 4),
                         round(a_pb * price_ratio * cum, 4),
                         round(mv, 2), SRC))
        if rows and not args.dry_run:
            con.executemany("""
                INSERT INTO valuation_daily
                  (ts_code, date, pe_ttm, pb, total_mv, source, fetched_at, data_version)
                VALUES (?, ?, ?, ?, ?, ?, now(), 1)
                ON CONFLICT (ts_code, date) DO UPDATE SET
                  pe_ttm=excluded.pe_ttm, pb=excluded.pb,
                  total_mv=excluded.total_mv, source=excluded.source,
                  fetched_at=excluded.fetched_at
            """, rows)
            n_rows += len(rows)
        if len(rows) % 200 == 0:
            log.info("derived %d rows so far", n_rows)

    if args.dry_run:
        print("dry-run: no writes")
    else:
        con.execute("CHECKPOINT")
        # ttm_yield_pct 本地派生（G8 门依赖；依赖新 af，须在 af apply 之后跑）
        from lake.ingest.baidu_valuation_adapter import derive_yield_for_date
        win_dates = [r[0] for r in con.execute(
            "SELECT DISTINCT date FROM kline_daily WHERE date BETWEEN ? AND ? "
            "ORDER BY date", [START, END]).fetchall()]
        for d in win_dates:
            derive_yield_for_date(con, str(d)[:10])
        n = con.execute(
            "SELECT count(*), count(*) FILTER (WHERE pe_ttm IS NOT NULL), "
            "count(*) FILTER (WHERE ttm_yield_pct IS NOT NULL) "
            f"FROM valuation_daily WHERE source=? AND date BETWEEN ? AND ?",
            [SRC, START, END]).fetchone()
        n_ts = con.execute(
            "SELECT count(*) FROM valuation_daily WHERE source='tushare' "
            f"AND date BETWEEN ? AND ?", [START, END]).fetchone()
        print(f"derived rows={n_rows} table({SRC})=total/pe/yield {list(n)} "
              f"tushare残留={n_ts[0]}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
