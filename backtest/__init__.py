# -*- coding: utf-8 -*-
"""回测引擎包（v3 多因子策略历史回测）。

模块划分（调研报告 R5）：
- ``data_pit``    — PIT 快照访问层（全离线读缓存，R1 泄漏修复收敛点）。
- ``simulator``   — 月度调仓组合模拟器（T+1 成交、等权 Top N、成本可配）。
- ``metrics_bt``  — 绩效指标（年化/波动/夏普/回撤/Calmar/alpha/beta/IR/胜率/换手）。
- ``report_bt``   — CSV / Markdown 输出。
- ``engine``      — 编排：月末 PIT 快照跑打分 → 目标组合 → 驱动模拟器 → 写产物。
- ``migrate_history`` — Phase B 串行迁移脚本（断点续跑；本轮只交付 + dry-run）。

红线：本包**零 live BaoStock 拉取**（封禁中）——一切基于现有缓存离线开发；
live 依赖只进 ``migrate_history`` 且默认不执行。
"""
__all__ = ["data_pit", "simulator", "metrics_bt", "report_bt", "engine", "migrate_history"]
