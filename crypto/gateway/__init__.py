# -*- coding: utf-8 -*-
"""crypto.gateway —— 实时 K 线数据网关（独立进程，systemd: crypto-gateway.service）。

**纯内存态，不入湖**（Joel 2026-11-11 定案）：
- 数据湖 = 离线数据，由 cron 离线同步刷新（最多落后约一天）
- 网关 = 实时底座，服务后续"是否到达买入点"的实时判断
- 两者解耦：网关不落湖（免 DuckDB 写锁竞争），只维护内存 K 线状态
  + 状态文件 data/crypto/gateway_state.json（web /api/gateway 展示、
  后续信号模块直接读实时状态）

架构参考 quant_iTransformer market_gateway.py（ohlcv_loop / handle_ws_exception
/ 重连回填），按其模式裁剪：
- WebSocket 组合流（wss://stream.binance.com:9443，1 连接 3 标的 kline_1h）
- 同 ts 去重：Binance WS 每秒推送同一根未收盘 K 线的最新状态 → 替换而非追加
- 看门狗：120s 无任何消息 → 视为假死，断开重连（指数退避 2s→60s）
- 状态文件每 60s 原子刷新（web 端随时可读）
"""
