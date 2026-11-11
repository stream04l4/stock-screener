# -*- coding: utf-8 -*-
"""gateway.stream —— Binance WebSocket K 线组合流客户端。

1 条连接订阅全部标的 kline_1h（wss://stream.binance.com:9443/stream）。
消息格式（组合流）：
  {"stream": "btcusdt@kline_1h",
   "data": {"e": "kline", "E": <事件ms>, "s": "BTCUSDT",
            "k": {"t": <开盘ms>, "T": <收盘ms>, "o": "...", "h": "...",
                  "l": "...", "c": "...", "v": <基币量>, "q": <计价量>,
                  "n": <成交笔数>, "x": <是否已收盘>, ...}}}
"""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator, List, Optional, Tuple

import websockets

WS_BASE = "wss://stream.binance.com:9443/stream?streams={streams}"


class GatewayStall(Exception):
    """看门狗判定流假死（超时无消息）或连接异常终止。"""


def stream_url(symbols: List[str], interval: str = "1h") -> str:
    return WS_BASE.format(
        streams="/".join(f"{s.lower()}@kline_{interval}" for s in symbols))


async def kline_iter(symbols: List[str], interval: str = "1h",
                     watchdog_s: float = 120.0) -> AsyncIterator[Tuple[dict, str]]:
    """连接组合流，逐条产出 (kline_data, stream_name)。

    看门狗（参考 market_gateway.py ohlcv_loop 的 90s 假死判定）：
    连续 watchdog_s 秒收不到任何消息 → 抛 GatewayStall，由调用方重连。
    ping_interval/ping_timeout 兜底 TCP 层半死连接。
    """
    url = stream_url(symbols, interval)
    async with websockets.connect(url, ping_interval=20, ping_timeout=20,
                                  max_size=2 ** 20) as ws:
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=watchdog_s)
            except asyncio.TimeoutError:
                raise GatewayStall(f"{watchdog_s}s 无消息，判定假死")
            msg = json.loads(raw)
            data = msg.get("data")
            if not isinstance(data, dict):
                continue
            yield data, msg.get("stream", "")
