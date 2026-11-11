# -*- coding: utf-8 -*-
"""Binance 现货公共 REST 适配（主源，无需 key）。

2026-11-11 EU 实测：api.binance.com 直连 200；klines 1h 全量回溯至 2019-01-01。
限速：小时增量 <10 请求 ≈ 权重 0.01%，不做 token bucket，仅留 request_pause。
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from typing import List, Optional

BASE = "https://api.binance.com"


def _get(path: str, params: dict, timeout_s: float = 10) -> list:
    url = BASE + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "stock-screener/7.0"})
    with urllib.request.urlopen(req, timeout=timeout_s) as r:
        return json.loads(r.read().decode("utf-8"))


def klines(symbol: str, interval: str = "1h",
           start_ms: Optional[int] = None, end_ms: Optional[int] = None,
           limit: int = 1000, timeout_s: float = 10) -> list:
    """K 线，返回 [{ts, open, high, low, close, volume, quote_volume}, ...]（升序）。

    ts = 开盘时间 epoch 秒。Binance 单页上限 1000 根（调用方分页）。
    """
    params = {"symbol": symbol, "interval": interval, "limit": min(int(limit), 1000)}
    if start_ms is not None:
        params["startTime"] = int(start_ms)
    if end_ms is not None:
        params["endTime"] = int(end_ms)
    raw = _get("/api/v3/klines", params, timeout_s)
    out = []
    for k in raw:
        out.append({
            "ts": int(k[0]) // 1000,
            "open": float(k[1]), "high": float(k[2]),
            "low": float(k[3]), "close": float(k[4]),
            "volume": float(k[5]), "quote_volume": float(k[7]),
        })
    return out


def klines_paged(symbol: str, start_ms: int, end_ms: int, interval: str = "1h",
                 pause_s: float = 0.15, timeout_s: float = 10) -> list:
    """[start_ms, end_ms] 区间完整 K 线（自动分页，含限速间隔）。"""
    out: List[dict] = []
    cur = int(start_ms)
    while cur < end_ms:
        batch = klines(symbol, interval=interval, start_ms=cur,
                       end_ms=end_ms, limit=1000, timeout_s=timeout_s)
        if not batch:
            break
        out.extend(batch)
        nxt = batch[-1]["ts"] * 1000 + 1
        if nxt <= cur:  # 防御：无进展则终止
            break
        cur = nxt
        if pause_s > 0:
            time.sleep(pause_s)
        if len(batch) < 1000:
            break
    return out


def ticker_24h(timeout_s: float = 10) -> list:
    """全部现货 24h ticker（1 个请求），返回 [{symbol, last, chg_pct, high, low, quote_vol}, ...]。"""
    raw = _get("/api/v3/ticker/24hr", {}, timeout_s)
    out = []
    for t in raw:
        out.append({
            "symbol": t["symbol"], "last": float(t["lastPrice"]),
            "chg_pct": float(t["priceChangePercent"]),
            "high": float(t["highPrice"]), "low": float(t["lowPrice"]),
            "quote_volume": float(t["quoteVolume"]),
        })
    return out


def server_time(timeout_s: float = 10) -> int:
    """交易所服务器时间 epoch 秒（对齐本地时钟偏移用）。"""
    raw = _get("/api/v3/time", {}, timeout_s)
    return int(raw["serverTime"]) // 1000
