# -*- coding: utf-8 -*-
"""v7.0.35 summary.md 个股明细 = 年度汇总（Joel 2026-11-10：回测结果区已有
买入时间表，明细不重复；改逐年外部投入/分红(盈余)/盈亏）。

纯文本生成（report._summary_lines），零网络零湖。
"""
import sys
from pathlib import Path

import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from martingale.config import StrategyParams  # noqa: E402
from martingale import report  # noqa: E402


def _sp():
    return StrategyParams(name="drawdown_martingale", lot_size=100, first_lots=1,
                          add_lots_step=1, first_drawdown=0.15, add_step=0.05,
                          rearm_mode="bounce", rearm_pct=0.15, accounting="cash")


def _row(annual):
    return {
        "ts_code": "sh.600001", "name": "测试股", "n_bars": 100, "n_rounds": 1,
        "n_rearms": 0, "n_buys": 2, "total_invested": 24007.2,
        "realized_pnl": 0.0, "final_assets": 27300.0, "dividends": 300.0,
        "price_pnl": 2992.8, "total_pnl": 3292.8, "pnl_pct": 0.137,
        "min_unreal_pct": -0.0003, "first_buy_date": "2020-01-02",
        "last_buy_date": "2020-01-02", "last_status": "open",
        "end_date": "2021-01-03", "annual": annual,
    }


def _lines(rows):
    gdates = np.array([np.datetime64("2021-01-03", "D")])
    return report._summary_lines(
        _sp(), "/fake/lake.duckdb", gdates, 1, rows, {},
        {r["ts_code"]: r["name"] for r in rows}, [], None, None, 0.0)


def test_detail_uses_annual_table_not_buy_timeline():
    annual = [
        {"year": 2020, "invested_ext": 24007.2, "invested_div": 0.0,
         "div_surplus": 0.0, "price_pnl": 25200.0, "end_assets": 25200.0},
        {"year": 2021, "invested_ext": 0.0, "invested_div": 0.0,
         "div_surplus": 300.0, "price_pnl": 1800.0, "end_assets": 27300.0},
    ]
    text = "\n".join(_lines([_row(annual)]))
    # 年度汇总替代买入时间表
    assert "买入时间表" not in text
    assert "年度汇总" in text
    assert "| 年份 | 外部投入 | 分红投入 | 分红盈余 | 股价盈亏 | 年底总资产 |" in text
    assert "| 2020 | 24,007 | 0 | 0 | +25,200 | 25,200 |" in text
    assert "| 2021 | 0 | 0 | 300 | +1,800 | 27,300 |" in text
    # 口径注释交代恒等式（投入不另加）与盈余（不投入）
    assert "恒等式" in text
    assert "盈余" in text
    # 原有明细头行保留
    assert "### 测试股 (sh.600001)" in text
    assert "总投入" in text


def test_detail_legacy_row_without_annual_key():
    """无 annual 键（旧产物/非 run_stock 来源）→ 不产年度表、不崩。"""
    r = _row([])
    del r["annual"]
    text = "\n".join(_lines([r]))
    assert "年度汇总" not in text
    assert "买入时间表" not in text
    assert "### 测试股 (sh.600001)" in text


def test_detail_empty_annual_skips_section():
    """annual=[]（从未建仓的 triggered 行理论上不存在，但防御）→ 无表。"""
    text = "\n".join(_lines([_row([])]))
    assert "年度汇总" not in text
