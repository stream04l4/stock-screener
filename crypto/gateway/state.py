# -*- coding: utf-8 -*-
"""gateway.state —— 实时状态内存库 + 状态文件（供 web /api/gateway 读取）。"""
from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Dict, Optional


class GatewayState:
    """每币实时状态。线程安全不保证（单 asyncio 事件循环内使用）。"""

    def __init__(self, state_path: str):
        self.state_path = state_path
        self.ws: str = "starting"          # starting / connected / reconnecting / stopping
        self.started: int = int(time.time())
        self.updated: int = 0
        self.reconnects: int = 0
        self.last_msg: int = 0
        self.last_error: Optional[str] = None
        self.coins: Dict[str, dict] = {}   # sym → {price, candle_ts, closed_ts, last_update}

    def set_ws(self, ws: str, error: Optional[str] = None) -> None:
        self.ws = ws
        self.last_error = error
        self.flush()

    def note_message(self) -> None:
        self.last_msg = int(time.time())

    def on_candle(self, symbol: str, ts: int, o: float, h: float, l: float,
                  c: float, v: float, q: float, closed: bool) -> None:
        """WS K 线消息。同 ts 未收盘 → 整根替换（Binance 每秒推送最新状态，
        最后一帧即最新 OHLCV——参考 market_gateway.py 的 q[-1]=candle 去重）。"""
        st = self.coins.setdefault(symbol, {
            "price": None, "candle_ts": None, "closed_ts": None,
            "candle": None, "last_update": 0,
        })
        st["candle"] = {"ts": ts, "open": o, "high": h, "low": l,
                        "close": c, "volume": v, "quote_volume": q}
        st["candle_ts"] = ts
        st["price"] = c
        if closed:
            st["closed_ts"] = ts
        st["last_update"] = int(time.time())

    def snapshot(self) -> dict:
        return {
            "ok": self.ws == "connected",
            "ws": self.ws,
            "started": self.started,
            "updated": self.updated or self.started,
            "reconnects": self.reconnects,
            "last_msg": self.last_msg,
            "last_error": self.last_error,
            "coins": self.coins,
        }

    def flush(self) -> None:
        """原子写状态文件（web 端随时可读，不会读到半截 JSON）。"""
        self.updated = int(time.time())
        d = os.path.dirname(self.state_path)
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".gateway_state.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.snapshot(), f, ensure_ascii=False)
            os.replace(tmp, self.state_path)
        except Exception:  # noqa: BLE001 - 状态文件写失败不影响主流程
            try:
                os.unlink(tmp)
            except OSError:
                pass
