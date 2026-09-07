# -*- coding: utf-8 -*-
"""回测结果数据模型（engine 组装、report_bt/web 渲染；与 CSV 列一一对应）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class PeriodHolding:
    """某调仓期的一只入选股（monthly_holdings.csv 一行）。"""

    code: str
    name: str = ""
    weight: float = 0.0
    entry_price: Optional[float] = None   # 成交价（af1；展示用）
    total_score: Optional[float] = None


@dataclass
class PeriodInfo:
    """某调仓期的决策信息。"""

    decision_date: str                    # T（月末交易日，ISO）
    exec_day: str = ""                    # 实际成交日（T+1）
    active_dims: List[str] = field(default_factory=list)   # 本期激活维度（PIT 可用）
    n_universe: int = 0                   # PIT 股票池大小
    n_hard_pass: int = 0                  # 硬剔除后候选数
    holdings: List[PeriodHolding] = field(default_factory=list)


@dataclass
class BenchmarkSeries:
    """基准净值序列（与策略日期对齐）。"""

    name: str                             # hs300 / zz500 / ew_allmarket ...
    code: str = ""                        # sh.000300 等（ew_allmarket 为空）
    dates: List[str] = field(default_factory=list)
    navs: List[float] = field(default_factory=list)


@dataclass
class BacktestResult:
    """一次完整回测的产物集合。"""

    start: str = ""
    end: str = ""
    top_n: int = 0
    execution_mode: str = "t1_open"       # t1_open | t1_close | t_close
    weights_ref: str = "scoring.weights"
    risk_free_pct: float = 2.0

    dates: List[str] = field(default_factory=list)        # 日频净值日期
    navs: List[float] = field(default_factory=list)       # 策略净值（初始=1.0）
    benchmarks: List[BenchmarkSeries] = field(default_factory=list)
    periods: List[PeriodInfo] = field(default_factory=list)

    turnover_by_day: Dict[str, float] = field(default_factory=dict)
    cost_bps_cum: float = 0.0
    n_rebalances: int = 0
    n_delisted_exits: int = 0
    delisting_drag_pct: float = 0.0
    n_drop_to_cash: int = 0
    open_fallback_total: int = 0          # open 缺失 close 兜底总笔数

    industry_update_date: str = ""        # 行业快照日期（当前口径标注）
    universe_note: str = ""               # 股票池口径说明（幸存者偏差等）
    data_notes: List[str] = field(default_factory=list)   # 报告"口径与局限"节
