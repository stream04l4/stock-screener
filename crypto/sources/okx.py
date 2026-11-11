# -*- coding: utf-8 -*-
"""OKX 公共 REST 适配（备源 / 交叉校验，无需 key）。

2026-11-11 EU 实测：www.okx.com 直连 200；candles 1H 与 Binance 价差 <0.01%。
OKX 注意：返回**倒序**（最新在前），此处统一转升序。
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import List, Optional

BASE = "https://www.okx.com"

# OKX bar 取值：1m/5m/15m/30m/1H/2H/4H/1D...（小时以上大写）
_BAR = {"1h": "1H", "4h": "4H", "1d": "1D", "15m": "15m"}


def _get(path: str, params: dict, timeout_s: float = 10) -> dict:
    url = BASE + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "stock-screener/7.0"})
    with urllib.request.urlopen(req, timeout=timeout_s) as r:
        data = json.loads(r.read().decode("utf-8"))
    if str(data.get("code", "0")) != "0":
        raise RuntimeError(f"OKX 错误 code={data.get('code')} msg={data.get('msg')}")
    return data


def candles(symbol: str, interval: str = "1h",
            start_ms: Optional[int] = None, end_ms: Optional[int] = None,
            limit: int = 100, timeout_s: float = 10) -> list:
    """K 线（升序）。OKX 单页上限 300（history 端点 100），交叉校验够用。

    symbol 需 OKX 格式（BTC-USDT）；binance 格式 BTCUSDT 自动转换（USDT 后缀）。
    """
    inst = symbol[:-4] + "-" + symbol[-4:] if symbol.endswith("USDT") else symbol
    params = {"instId": inst, "bar": _BAR.get(interval, "1H"), "limit": min(int(limit), 300)}
    # OKX 分页语义（易错，2026-11-11 实测验证）：
    #   before=T → 返回比 T **新**的记录（ts > T）
    #   after=T  → 返回比 T **老**的记录（ts < T）
    # 取 [start_ms, end_ms] 区间 = before=start + after=end
    if start_ms is not None:
        params["before"] = int(start_ms)
    if end_ms is not None:
        params["after"] = int(end_ms)
    data = _get("/api/v5/market/candles", params, timeout_s)
    out = []
    for c in data.get("data", []):
        out.append({
            "ts": int(c[0]) // 1000,
            "open": float(c[1]), "high": float(c[2]),
            "low": float(c[3]), "close": float(c[4]),
            "volume": float(c[5]), "quote_volume": float(c[6]),
        })
    out.sort(key=lambda x: x["ts"])  # OKX 返回倒序 → 统一升序
    return out
