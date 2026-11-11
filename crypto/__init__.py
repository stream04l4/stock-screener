# -*- coding: utf-8 -*-
"""crypto —— 加密货币系统（独立部署单元，:9091）。

与股票系统（web/，:9090）同仓库、同登录（ssc_session cookie 按 hostname
跨端口共享，同 WEB_SESSION_SECRET），但独立进程 / 独立前端 SPA / 独立数据
目录（data/crypto/，湖 = data/crypto/crypto.duckdb）。
架构见 docs/architecture.md（双系统项目地图）+ docs/crypto_design.md。

目录约定（后续扩展落点）：
  crypto/sources/    数据源适配（binance 主源 / okx 备源）
  crypto/sync.py     数据同步（cron 每小时）
  crypto/models.py   湖表 DDL + upsert
  crypto/web/        独立 Web 应用 + SPA（:9091）
  crypto/positions/  P2：我的持仓（代码落点，预留）
  crypto/strategy/   P3：策略定义（代码落点，预留）
  crypto/backtest/   P3：回测引擎（代码落点，预留）

v7.0.43 框架阶段：四 tab 骨架 + 数据底座；内容后续慢慢完善。
"""

__version__ = "0.1.0"
