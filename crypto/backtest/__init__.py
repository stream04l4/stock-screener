# -*- coding: utf-8 -*-
"""crypto.backtest —— P3：回测引擎（预留代码落点）。

规划：基于 data/crypto/crypto.duckdb 湖内 1h K 线的回测引擎 + 报告
（产物落 data/crypto/backtest/，参照股票侧 output/backtest_martingale/
的 runs/<task_id> 隔离模式；Web 端点 /api/backtest 实体化 + BacktestTab）。
前置：7 年全量回填（python -m crypto.sync --full）。
边界纪律（设计文档 §7）：回测 = 策略验证工具，不接自动交易。
"""
