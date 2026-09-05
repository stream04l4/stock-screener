# -*- coding: utf-8 -*-
"""股票池构建（漏斗第 1 层）。

规则（brief §3.1 + TL 拍板）：
- 沪深 A 股：``query_all_stock(day=...)`` 全量证券按代码前缀过滤
  （sh.60/sh.68/sz.00/sz.30，可配置）。指数/ETF/B股 全部被前缀排除。
- 当日正常交易：tradeStatus == 1（停牌剔除）。
- ST 剔除以日K isST=1 为准 → 需要 K 线数据，放在技术面层执行（universe 层
  只输出候选集；名称含 "ST" 仅作辅助标记，供报告展示）。
- 上市满 N 个交易日：用后复权窗口 K 线行数判断（BaoStock 对已上市股票返回
  每个交易日的行，含停牌日）→ 也在技术面层执行。

注意：query_all_stock 不带 day 参数在非交易日返回空 —— 始终显式传 day
（day 由 CLI 用 query_trade_dates 定位/校验）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict

import pandas as pd

from .data.fetchers import DataFetcher

log = logging.getLogger("screener.universe")


@dataclass
class UniverseStats:
    total_securities: int = 0        # query_all_stock 返回的全部证券数
    a_share_count: int = 0           # 前缀过滤后的 A 股数
    trading_count: int = 0           # tradeStatus=1 的候选数（最终股票池）
    suspended_count: int = 0         # 停牌剔除数
    st_name_count: int = 0           # 名称含 ST 的辅助标记数（最终以日K isST 为准）
    extra: Dict[str, Any] = field(default_factory=dict)


def build_universe(
    fetcher: DataFetcher, run_day: str, prefixes: list[str], st_keyword: str
) -> tuple[pd.DataFrame, UniverseStats]:
    """构建股票池。

    :return: (候选 DataFrame[code, name, tradeStatus, is_st_name], 统计)
    """
    stats = UniverseStats()
    all_df = fetcher.all_stock(run_day)
    stats.total_securities = len(all_df)

    mask_prefix = all_df["code"].str.startswith(tuple(prefixes))
    a_share = all_df[mask_prefix].copy()
    stats.a_share_count = len(a_share)

    suspended = a_share[a_share["tradeStatus"] != 1]
    stats.suspended_count = len(suspended)
    pool = a_share[a_share["tradeStatus"] == 1].copy()
    stats.trading_count = len(pool)

    # 辅助标记（非剔除依据）：名称含 ST
    pool = pool.rename(columns={"code_name": "name"})
    pool["is_st_name"] = pool["name"].str.contains(st_keyword, na=False) if st_keyword else False
    stats.st_name_count = int(pool["is_st_name"].sum())

    log.info(
        "股票池: 全部证券 %d → A股前缀 %d → 正常交易 %d（停牌剔除 %d，名称含ST辅助标记 %d）",
        stats.total_securities, stats.a_share_count, stats.trading_count,
        stats.suspended_count, stats.st_name_count,
    )
    return pool.reset_index(drop=True), stats
