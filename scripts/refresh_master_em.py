#!/usr/bin/env python3
"""T1 stock_master 东财 clist 重取（P1：tushare 主档 5,224 行替换；P2 转正）。

tushare 源全弃用（Joel 2026-11-25 拍板）。主档全集改东财 clist 快照
（fetch_clist_snapshot：56 页 ~1-2 分钟，EU→SG 出口链路内置）：
- universe = sh/sz（68→科创、30→创业、其余主板；北交所不在 clist fs 内）
- name/industry_name/list_date 取 EM 值；delist_date/soe_*/industry_csric2/
  st_since 沿用 stock_master 现存值（与 run_t1 口径一致：退市股不在 clist，
  旧行原样保留）
- name 带 XD/DR/XR 前缀（除息日）跳过覆盖（run_t1 v7.0.18.3 同守卫）
- is_st = 名称含 ST（v5 口径）
- source='em_clist'

用法：
  .venv/bin/python scripts/refresh_master_em.py            # 全市场
  .venv/bin/python scripts/refresh_master_em.py --dry-run
"""
from __future__ import annotations

import argparse
import logging
import re
import sys

sys.path.insert(0, ".")

import duckdb  # noqa: E402

DB = "data/lake/lake.duckdb"
log = logging.getLogger("refresh_master_em")

_MARKET_BOARD_PREFIX = ((("68", "科创"), ("30", "创业")))


def _board(ts_code: str) -> str:
    code6 = ts_code.split(".")[1]
    for pref, board in _MARKET_BOARD_PREFIX:
        if code6.startswith(pref):
            return board
    return "主板"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    from lake.ingest.common import DATA_VERSION, clean_date, now_ts, upsert
    from lake.ingest.em_batch_ingest import fetch_clist_snapshot

    rows_raw = fetch_clist_snapshot()
    log.info("clist rows=%d", len(rows_raw))

    con = duckdb.connect(DB)
    prev_map = {
        r[0]: r[1:] for r in con.execute(
            "SELECT ts_code, industry_csric2, delist_date, is_st, st_since, "
            "soe_flag, soe_basis FROM stock_master").fetchall()
    }
    n_total = len(prev_map)
    rows = []
    skipped_xd = 0
    for x in rows_raw:
        ts = x.get("ts_code")
        if not ts:
            continue
        name = str(x.get("name") or "")
        if re.match(r"^(XD|DR|XR)", name):
            skipped_xd += 1
            continue
        ind = (str(x.get("industry")).strip() or None) if x.get("industry") else None
        # 湖内无旧行 = 新上市股 → 扩展列全 NULL 新建（run_t1 upsert 同口径）
        p_ind2, p_delist, p_st, p_st_since, p_soe_f, p_soe_b = prev_map.get(
            ts, (None, None, None, None, None, None))
        rows.append([
            ts,
            name,                 # EM 名（除息日 XD 行已跳过）
            p_ind2,               # 保留既有行业二级码
            ind,                  # EM 行业（东财板块口径，替代 tushare 粗分类）
            clean_date(x.get("list_date")),
            p_delist,             # 退市日沿用（退市股不在 clist，此处恒非退市）
            _board(ts),
            1 if "ST" in name.upper() else 0,
            p_st_since, p_soe_f, p_soe_b,
            "em_clist", now_ts(), DATA_VERSION,
        ])
    log.info("rows to write=%d (skipped XD=%d, lake master=%d)",
             len(rows), skipped_xd, n_total)
    if args.dry_run:
        print("dry-run: no writes")
        return 0
    n = upsert(con, "stock_master", [
        "ts_code", "name", "industry_csric2", "industry_name", "list_date",
        "delist_date", "board", "is_st", "st_since", "soe_flag", "soe_basis",
        "source", "fetched_at", "data_version"], rows)
    con.execute("CHECKPOINT")
    mix = con.execute(
        "SELECT source, count(*) FROM stock_master GROUP BY source "
        "ORDER BY 2 DESC").fetchall()
    print(f"written={n} source_mix={[(s, c) for s, c in mix]}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
