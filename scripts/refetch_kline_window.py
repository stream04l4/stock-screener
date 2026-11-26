#!/usr/bin/env python3
"""K线窗口重取（P1：清除 tushare K线行 10-26→11-25，19 交易日 93,378 行）。

tushare 源全弃用（Joel 2026-11-25 拍板）：窗口内全部 OHLCV 从
sina raw（单调用/只，akshare stock_zh_a_daily 窗口模式）重取，
失败回退 tencent 全史尾部 / tdx。adj_factor 一律不写（NULL）——
由 scripts/af_rebuild.py apply 事件驱动统一重建。
preclose/is_st/pct_chg 从备份表 bak_20261125_kline_daily 恢复
（交易所原始元数据，非 tushare 私有口径）。

幂等：load_t2 按 (ts_code,date) REPLACE。进度落 logs/refetch_kline_window.jsonl。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time

sys.path.insert(0, ".")

import duckdb  # noqa: E402

DB = "data/lake/lake.duckdb"
START = "2026-10-26"
END = "2026-11-25"
PROG = "logs/refetch_kline_window.jsonl"
log = logging.getLogger("refetch_window")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    from lake.ingest import tencent_ingest as ti
    from lake.ingest.sina_adapter import SinaKlineAdapter
    from screener.data.tencent import TencentClient

    con = duckdb.connect(DB)
    # 代码全集从备份表取（删除 tushare 行后，窗口内仅有 tushare 行的股会从
    # kline_daily 消失，必须用删前全量状态）
    codes = [r[0] for r in con.execute(
        "SELECT DISTINCT ts_code FROM bak_20261125_kline_daily "
        f"WHERE date>=? AND date<=?", [START, END]).fetchall()]
    log.info("window %s..%s codes=%d", START, END, len(codes))
    limit = int(os.environ.get("REFETCH_LIMIT", "0"))
    if limit:
        codes = codes[:limit]
        log.info("REFETCH_LIMIT=%d → codes=%d (smoke)", limit, len(codes))

    sina = SinaKlineAdapter(min_interval_s=0.7)
    tclient = TencentClient()
    stats = {"sina": 0, "tencent": 0, "tdx": 0, "failed": []}
    t0 = time.monotonic()
    with open(PROG, "w") as pf:
        for i, code in enumerate(codes, 1):
            rows = None
            src = None
            vol_shares = True
            try:
                sym = SinaKlineAdapter._symbol6(code)
                df = sina._ak_daily(sym, adjust="", start=START, end=END)
                if df is not None and len(df) > 0:
                    from lake.ingest.common import to_float, clean_date
                    rows = []
                    for _, r in df.iterrows():
                        d = clean_date(str(r.get("date")))
                        if not d:
                            continue
                        rows.append({
                            "date": d,
                            "open": to_float(r.get("open")),
                            "high": to_float(r.get("high")),
                            "low": to_float(r.get("low")),
                            "close": to_float(r.get("close")),
                            "volume": to_float(r.get("volume")),
                            "amount": to_float(r.get("amount")),
                        })
                    if rows:
                        src = "sina"
            except Exception as exc:  # noqa: BLE001 - 回退下一源
                log.warning("%s sina fail: %s", code, exc)
            if src is None:
                try:
                    fh = ti.fetch_kline_full_history(tclient, code, page_size=2000)
                    rows = [r for r in fh if START <= str(r.get("date"))[:10] <= END]
                    if rows:
                        src = "tencent"
                        vol_shares = False  # 腾讯手数口径，load_t2 ×100
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s tencent fail: %s", code, exc)
            if src is None:
                try:
                    from lake.ingest.easy_tdx_adapter import EasyTdxAdapter
                    tdx = EasyTdxAdapter()
                    r2 = tdx.fetch_kline(code, start=START, end=END)
                    kl = (r2 or {}).get("ohlcv") or []
                    kl = [r for r in kl if START <= str(r.get("date"))[:10] <= END]
                    if kl:
                        rows = kl
                        src = "tdx"
                        vol_shares = True
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s tdx fail: %s", code, exc)
            if src is None or not rows:
                stats["failed"].append(code)
                continue
            stats[src] += 1
            ti.load_t2(con, code, rows, adj_map=None, is_st=0, source=src,
                       volume_is_shares=vol_shares)
            if i % 200 == 0:
                log.info("progress %d/%d (%.0fs) %s", i, len(codes),
                         time.monotonic() - t0, json.dumps(
                             {k: v for k, v in stats.items() if k != "failed"},
                             ensure_ascii=False))
                pf.write(json.dumps({"i": i, "t": round(time.monotonic() - t0, 1),
                                     **{k: v for k, v in stats.items()
                                        if k != "failed"}}) + "\n")
                pf.flush()

    # 恢复交易所原始元数据（非 tushare 口径）
    con.execute(f"""
        UPDATE kline_daily k SET preclose=b.preclose, is_st=b.is_st,
               pct_chg=b.pct_chg
        FROM bak_20261125_kline_daily b
        WHERE k.ts_code=b.ts_code AND k.date=b.date
          AND b.date BETWEEN ? AND ?""", [START, END])
    con.execute("CHECKPOINT")
    n = con.execute(
        "SELECT count(*), count(*) FILTER (WHERE preclose IS NOT NULL), "
        "count(*) FILTER (WHERE is_st=1) FROM kline_daily "
        f"WHERE date BETWEEN ? AND ?", [START, END]).fetchone()
    print(json.dumps({"stats": stats, "window_rows": list(n),
                      "elapsed_s": round(time.monotonic() - t0, 1)},
                     ensure_ascii=False))
    con.close()
    return 0 if not stats["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
