# -*- coding: utf-8 -*-
"""crypto.backtest —— P3：回测引擎（v7.0.47 实体化）。

湖 1h K 线 → crypto.strategy.engine.run_coin → 逐币轮次/净值报告。
CLI：python -m crypto.backtest（报告落 data/crypto/backtest_report.md）。
后续：Web 端点 /api/backtest 实体化 + BacktestTab（等回测数字过目后做）。
边界纪律（设计文档 §7）：回测 = 策略验证工具，不接自动交易。
"""
from crypto.backtest.runner import load_bars, render_report, run_backtest, save_report

__all__ = ["load_bars", "render_report", "run_backtest", "save_report"]
