# -*- coding: utf-8 -*-
"""gateway 主循环：python -m crypto.gateway（systemd: crypto-gateway.service）。

纯内存态（Joel 2026-11-11 定案：网关不入湖，湖由离线同步负责）：
  while True: 连 WS → 收消息 → 更新内存 K 线状态
              异常/假死 → 指数退避重连
  并发 flush 任务：每 60s 原子写状态文件（web /api/gateway 读取）

参考 market_gateway.py start_gateways + ohlcv_loop 的骨架。
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys
import time

import yaml

from crypto.gateway.state import GatewayState
from crypto.gateway.stream import GatewayStall, kline_iter

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CFG_PATH = os.path.join(ROOT, "crypto", "config", "crypto.yaml")

WATCHDOG_S = 120.0    # 无消息 120s 判假死（1h K 线流 BTC/ETH/SOL 秒级推送，余量充足）
FLUSH_S = 60          # 状态文件刷新周期
BACKOFF_MAX_S = 60


def load_cfg() -> dict:
    with open(CFG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _log(msg: str) -> None:
    print(f"[gateway {time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


async def main() -> None:
    cfg = load_cfg()
    coins = list(cfg["coins"])
    state_path = os.path.join(ROOT, "data", "crypto", "gateway_state.json")

    state = GatewayState(state_path)

    async def flush_loop() -> None:
        """周期任务：状态文件原子写（web /api/gateway 随时可读）。"""
        while True:
            await asyncio.sleep(FLUSH_S)
            state.flush()

    loop = asyncio.get_running_loop()
    stop = loop.create_future()

    def _signal_handler() -> None:
        if not stop.done():
            stop.set_result(None)

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler)

    flush_task = asyncio.create_task(flush_loop())
    backoff = 2
    _log(f"启动：{len(coins)} 标的 kline_1h 组合流（watchdog {WATCHDOG_S:.0f}s, "
         f"状态文件 {state_path}，纯内存态不入湖）")

    try:
        while not stop.done():
            got_data = False
            try:
                state.set_ws("connected")
                async for data, _stream in kline_iter(coins, "1h", WATCHDOG_S):
                    if stop.done():
                        break
                    state.note_message()
                    if data.get("e") != "kline":
                        continue
                    k = data.get("k") or {}
                    sym = data.get("s")
                    if not sym or sym not in coins:
                        continue
                    state.on_candle(
                        sym,
                        int(k["t"]) // 1000,
                        float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"]),
                        float(k["v"]), float(k["q"]),
                        bool(k.get("x")),
                    )
                    got_data = True
                raise GatewayStall("连接被对端关闭")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - 重连退避（参考 handle_ws_exception）
                if stop.done():
                    break
                state.reconnects += 1
                state.set_ws("reconnecting", error=f"{type(e).__name__}: {e}")
                _log(f"流中断（{type(e).__name__}: {e}），{backoff}s 后重连 "
                     f"（第 {state.reconnects} 次）")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX_S)
                continue
            if got_data:  # 本次连接收到过数据 → 退避复位
                backoff = 2
    finally:
        state.set_ws("stopping")
        flush_task.cancel()
        _log("已停止")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    sys.exit(0)
