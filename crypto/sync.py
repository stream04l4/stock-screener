# -*- coding: utf-8 -*-
"""crypto.sync —— 数据同步入口（cron 每小时 :05 / 手动回填）。

用法：
  python -m crypto.sync                 # 日常增量：回补最近 backfill_window_h 小时
  python -m crypto.sync --days 3        # 回填最近 3 天
  python -m crypto.sync --full          # 全量回填：历史段走 Binance 官方月度快照
                                        #（data.binance.vision，每币自动探测首月，
                                        #  BTC/ETH 2017-08 起、SOL 2020-08 起），
                                        # 当月+尾巴走 REST 补齐

幂等：kline_1h PK(exchange,symbol,ts) INSERT OR REPLACE，重跑不产生重复；
vision 月度 CSV 沙盒缓存命中即复用，重跑只补尾巴。
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
from crypto.sources import binance, binance_vision, okx

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


def _bars_to_dicts(bars):
    """vision 元组 (ts,o,h,l,c,v,q) → upsert_klines 的 dict 列表。"""
    keys = ("ts", "open", "high", "low", "close", "volume", "quote_volume")
    return [dict(zip(keys, b)) for b in bars]


def _month1_ts(y: int, m: int) -> int:
    return int(dt.datetime(y, m, 1, tzinfo=dt.timezone.utc).timestamp())


def full_backfill(cfg: dict, coins: list, con, now: int,
                  timeout_s: float, pause_s: float):
    """--full 全量回填（Joel 2026-11-11 定案：先做 BTC/ETH/SOL 三标的）。

    历史段：Binance 官方月度快照（每币自动探测首个可用月 → 最后已发布月，
    4 线程并发，CSV 沙盒缓存命中即复用）；
    尾巴段：REST 拉取最后已发布月之后 → 现在（含当月与未收盘小时，
    与 vision 尾月重叠无害——同 PK 幂等覆盖）。
    返回 (per_coin, rows_total, start_ts)。
    """
    from crypto.sources import binance_vision  # 局部导入：仅 --full 需要

    vcfg = cfg.get("vision") or {}
    cache_dir = os.path.join(ROOT, vcfg.get("cache_dir", "data/crypto/vision_cache"))
    _py = tuple(int(x) for x in str(vcfg.get("probe_start_ym", "2017-08")).split("-"))
    probe_ym = (_py[0], _py[1])  # type: tuple[int, int]
    workers = int(vcfg.get("workers", 4))
    last_pub = binance_vision.last_published_month()
    ny, nm = (last_pub[0] + 1, 1) if last_pub[1] == 12 else (last_pub[0], last_pub[1] + 1)
    tail_start = _hour_floor(_month1_ts(ny, nm))

    per_coin = {}
    rows_total = 0
    ts_min_global = None
    for sym in coins:
        t_sym = time.time()
        try:
            first_ym, bars, missing = binance_vision.full_history(
                sym, probe_ym, last_pub, cache_dir,
                workers=workers, timeout_s=timeout_s)
            n_hist = models.upsert_klines(con, "binance", sym, _bars_to_dicts(bars))
            tail = binance.klines_paged(sym, tail_start * 1000,
                                        _hour_floor(now) * 1000 + 999_999,
                                        pause_s=pause_s, timeout_s=timeout_s)
            n_tail = models.upsert_klines(con, "binance", sym, tail)
            n = n_hist + n_tail
            rows_total += n
            ts_min = bars[0][0] if bars else None
            ts_max = (bars[-1][0] if bars else 0) or (tail[-1]["ts"] if tail else 0)
            if ts_min is not None and (ts_min_global is None or ts_min < ts_min_global):
                ts_min_global = ts_min
            per_coin[sym] = {
                "rows": n,
                "ok": first_ym is not None and not missing,
                "first_month": f"{first_ym[0]}-{first_ym[1]:02d}" if first_ym else None,
                "hist_rows": n_hist, "tail_rows": n_tail,
                "last_published_month": f"{last_pub[0]}-{last_pub[1]:02d}",
                "missing_months": missing,
                "ts_min": ts_min, "ts_max": ts_max,
                "range": f"{dt.datetime.fromtimestamp(ts_min):%Y-%m}~"
                         f"{dt.datetime.fromtimestamp(ts_max):%Y-%m-%d %H:%M}"
                         if ts_min and ts_max else None,
                "duration_s": round(time.time() - t_sym, 1),
            }
            print(f"[sync][full] {sym}: {n_hist} 历史行 + {n_tail} 尾巴行 "
                  f"({per_coin[sym]['range']}) {per_coin[sym]['duration_s']}s"
                  + (f"；缺失月份 {missing}" if missing else ""))
        except Exception as e:  # noqa: BLE001 - 单币失败不阻断（记录后继续）
            per_coin[sym] = {"rows": 0, "ok": False,
                             "error": f"{type(e).__name__}: {e}"}
            print(f"[sync][full] {sym} 失败：{e}", file=sys.stderr)
    return per_coin, rows_total, ts_min_global


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="crypto 数据同步")
    ap.add_argument("--days", type=float, default=None, help="回填最近 N 天")
    ap.add_argument("--full", action="store_true",
                    help="全量回填（vision 月度快照历史段 + REST 尾巴段）")
    args = ap.parse_args(argv)

    t0 = time.time()
    cfg = load_cfg()
    coins = list(cfg["coins"])
    exchange = cfg["source"]["primary"]
    timeout_s = float(cfg["source"].get("timeout_s", 10))
    pause_s = float(cfg["sync"].get("request_pause_s", 0.15))

    now = int(time.time())
    end = _hour_floor(now)
    # 湖写锁独占：整点 cron sync 运行中可能短暂持锁，撞上则重试（12×5s=60s 上限）
    con = None
    for _ in range(12):
        try:
            con = models.open_db(os.path.join(ROOT, cfg["lake"]["path"]))
            break
        except Exception:  # noqa: BLE001 - 锁竞争（cron 离线同步运行中）
            time.sleep(5)
    if con is None:
        raise RuntimeError("湖连接失败：写锁持续被占用（另一同步进程异常？）")
    models.upsert_coins(con, coins)
    # 清单外币种（如 top20 退役的 17 币）置 is_active=FALSE，数据保留
    models.deactivate_others(con, coins)

    if args.full:
        per_coin, rows_total, start = full_backfill(cfg, coins, con, now,
                                                    timeout_s, pause_s)
    else:
        if args.days:
            start = now - int(args.days * 86400)
        else:
            start = now - int(cfg["sync"].get("backfill_window_h", 3)) * 3600
        start = _hour_floor(start)

        per_coin = {}
        rows_total = 0
        for sym in coins:
            try:
                if exchange == "binance":
                    bars = binance.klines_paged(sym, start * 1000,
                                                _hour_floor(now) * 1000 + 999_999,
                                                pause_s=pause_s, timeout_s=timeout_s)
                else:  # okx（备源同构；全量回填需分页，框架阶段仅增量用）
                    bars = okx.candles(sym, "1h", start_ms=start * 1000,
                                       end_ms=_hour_floor(now) * 1000, limit=300)
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

    _win_from = start if start is not None else 0
    print(f"[sync] {exchange} {len(coins)} 币 {rows_total} 行 "
          f"窗口 {dt.datetime.fromtimestamp(_win_from):%m-%d %H:%M}~"
          f"{dt.datetime.fromtimestamp(end):%m-%d %H:%M} "
          f"{state['duration_s']}s"
          + (f"；交叉校验未过 {len(cc_bad)}/{len(cc)}" if cc_bad else
             (f"；交叉校验 {len(cc)}/{len(cc)} 通过" if cc else "")))
    return 0 if state["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
