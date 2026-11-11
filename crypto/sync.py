# -*- coding: utf-8 -*-
"""crypto.sync —— 数据同步入口（cron 每小时 :05 / 手动回填）。

用法：
  python -m crypto.sync                 # 日常增量：回补最近 backfill_window_h 小时
  python -m crypto.sync --days 3        # 回填最近 3 天
  python -m crypto.sync --full          # 全量回填（backfill_start=2019-01-01 起，
                                        # 20 币 ≈ 1.06M 行，限速内分批，预计 <30min）

幂等：kline_1h PK(exchange,symbol,ts) INSERT OR REPLACE，重跑不产生重复。
交叉校验：开启时逐币比对 Binance vs OKX 最近 24h 收盘价，偏差 >0.1% 记入
state 文件（框架阶段日志告警；P2 起接 PushPlus）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time

import yaml

from crypto import models
from crypto.sources import binance, okx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG_PATH = os.path.join(ROOT, "crypto", "config", "crypto.yaml")


def load_cfg() -> dict:
    with open(CFG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _hour_floor(ts: int) -> int:
    return ts - ts % 3600


def cross_check(cfg: dict, coins: list) -> list:
    """Binance vs OKX 最近 24h 收盘价比对。返回 [{symbol, diff_pct, ok}, ...]。"""
    warn_pct = float(cfg["source"].get("cross_check_warn_pct", 0.1))
    out = []
    end_ms = int(time.time() * 1000)
    for sym in coins:
        try:
            a = binance.klines(sym, "1h", end_ms - 24 * 3600 * 1000, end_ms, limit=24)
            b = okx.candles(sym, "1h", start_ms=end_ms - 24 * 3600 * 1000, limit=24)
            a_map = {k["ts"]: k["close"] for k in a}
            diffs = []
            for k in b:
                if k["ts"] in a_map and a_map[k["ts"]]:
                    diffs.append(abs(k["close"] - a_map[k["ts"]]) / a_map[k["ts"]] * 100)
            if not diffs:
                out.append({"symbol": sym, "diff_pct": None, "ok": False,
                            "note": "无重叠 K 线"})
                continue
            worst = max(diffs)
            out.append({"symbol": sym, "diff_pct": round(worst, 4),
                        "ok": worst <= warn_pct})
        except Exception as e:  # noqa: BLE001 - 单币校验失败不阻断同步
            out.append({"symbol": sym, "diff_pct": None, "ok": False,
                        "note": f"{type(e).__name__}: {e}"})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="crypto 数据同步")
    ap.add_argument("--days", type=float, default=None, help="回填最近 N 天")
    ap.add_argument("--full", action="store_true", help="全量回填（backfill_start 起）")
    args = ap.parse_args(argv)

    t0 = time.time()
    cfg = load_cfg()
    coins = list(cfg["coins"])
    exchange = cfg["source"]["primary"]
    timeout_s = float(cfg["source"].get("timeout_s", 10))
    pause_s = float(cfg["sync"].get("request_pause_s", 0.15))

    now = int(time.time())
    if args.full:
        start = int(dt.datetime.strptime(cfg["sync"]["backfill_start"], "%Y-%m-%d")
                     .replace(tzinfo=dt.timezone.utc).timestamp())
    elif args.days:
        start = now - int(args.days * 86400)
    else:
        start = now - int(cfg["sync"].get("backfill_window_h", 3)) * 3600
    start = _hour_floor(start)
    end = _hour_floor(now)

    con = models.open_db(ROOT)
    models.upsert_coins(con, coins)

    per_coin = {}
    rows_total = 0
    for sym in coins:
        try:
            if exchange == "binance":
                bars = binance.klines_paged(sym, start * 1000, end * 1000 + 999_999,
                                            pause_s=pause_s, timeout_s=timeout_s)
            else:  # okx（备源同构；全量回填需分页，框架阶段仅增量用）
                bars = okx.candles(sym, "1h", start_ms=start * 1000,
                                   end_ms=end * 1000, limit=300)
            n = models.upsert_klines(con, exchange, sym, bars)
            per_coin[sym] = {"rows": n, "ok": True}
            rows_total += n
        except Exception as e:  # noqa: BLE001 - 单币失败不阻断（记录后继续）
            per_coin[sym] = {"rows": 0, "ok": False,
                             "error": f"{type(e).__name__}: {e}"}
            print(f"[sync] {sym} 失败：{e}", file=sys.stderr)
    con.close()

    cc = cross_check(cfg, coins[:5]) if cfg["source"].get("cross_check") else []
    cc_bad = [c for c in cc if not c["ok"]]
    state = {
        "ok": all(v["ok"] for v in per_coin.values()),
        "ts": now,
        "time": dt.datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S"),
        "exchange": exchange,
        "window": {"from": start, "to": end},
        "coins": len(coins),
        "rows_total": rows_total,
        "per_coin": per_coin,
        "cross_check": cc,
        "duration_s": round(time.time() - t0, 1),
    }
    sp = os.path.join(ROOT, cfg["lake"]["state_file"])
    os.makedirs(os.path.dirname(sp), exist_ok=True)
    with open(sp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)

    print(f"[sync] {exchange} {len(coins)} 币 {rows_total} 行 "
          f"窗口 {dt.datetime.fromtimestamp(start):%m-%d %H:%M}~"
          f"{dt.datetime.fromtimestamp(end):%m-%d %H:%M} "
          f"{state['duration_s']}s"
          + (f"；交叉校验未过 {len(cc_bad)}/{len(cc)}" if cc_bad else
             (f"；交叉校验 {len(cc)}/{len(cc)} 通过" if cc else "")))
    return 0 if state["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
