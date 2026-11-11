# -*- coding: utf-8 -*-
"""crypto.strategy —— 加密货币马丁格尔策略（v7.0.47 实体化）。

策略参数单一定义点：StrategyParams（engine.py，从 crypto.yaml strategy 段
加载）；状态机 run_coin（1h 收盘价序列 → 轮次/净值，纯函数零 IO）。
规格与决策门见 engine.py docstring（Joel 2026-11-11 定案）。

边界纪律（设计文档 §7）：策略验证用途，不接自动交易、不做预测。
"""
from crypto.strategy.engine import StrategyParams, rolling_high_1y, run_coin

__all__ = ["StrategyParams", "rolling_high_1y", "run_coin"]
