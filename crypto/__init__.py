# -*- coding: utf-8 -*-
"""crypto —— 加密货币系统（独立部署单元，:9091）。

与股票系统（web/，:9090）同仓库、同登录（ssc_session cookie 按 hostname
跨端口共享，同 WEB_SESSION_SECRET），但独立进程 / 独立前端 SPA / 独立数据湖
（data/lake/crypto.duckdb）。架构见 docs/crypto_design.md（v0.2 双系统定案）。

v7.0.43 框架阶段：四 tab（我的持仓/策略/回测/数据湖）骨架 + 数据底座
（Binance 主源 + OKX 交叉校验）；内容后续慢慢完善。
"""

__version__ = "0.1.0"
