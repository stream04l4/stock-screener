# -*- coding: utf-8 -*-
"""股票池单测（离线，真实样例 allstock_2026-09-04）。

用 FakeFetcher 注入 fixture 数据，验证前缀过滤/停牌剔除逻辑。
"""
from __future__ import annotations

import csv
import os

import pandas as pd
import pytest

from conftest_helpers import FIXTURES
from screener.universe import build_universe


class FakeFetcher:
    """只实现 all_stock()，返回 fixture 数据（模拟 DataFetcher）。"""

    def __init__(self, day: str = "2026-09-04"):
        path = os.path.join(FIXTURES, "allstock_2026-09-04_full_7376rows.csv")
        with open(path, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.df = pd.DataFrame(rows)

    def all_stock(self, day: str) -> pd.DataFrame:
        assert day == "2026-09-04"
        df = self.df.copy()
        # 与真实 DataFetcher.all_stock 一致：tradeStatus 转 int
        df["tradeStatus"] = df["tradeStatus"].map(int)
        return df


PREFIXES = ["sh.60", "sh.68", "sz.00", "sz.30"]


def test_a_share_prefix_filter():
    """前缀过滤 → 5215 只（与调研报告/TL 核验数字一致）。"""
    pool, stats = build_universe(FakeFetcher(), "2026-09-04", PREFIXES, "ST")
    assert stats.total_securities == 7376
    assert stats.a_share_count == 5215


def test_trading_status_filter():
    """tradeStatus=1 → 5207 只（停牌 8 只剔除）。"""
    pool, stats = build_universe(FakeFetcher(), "2026-09-04", PREFIXES, "ST")
    assert stats.trading_count == 5207
    assert stats.suspended_count == 8
    assert len(pool) == 5207
    assert (pool["tradeStatus"] == 1).all()


def test_excludes_indices_etfs():
    """指数/ETF 不在结果里。"""
    pool, _ = build_universe(FakeFetcher(), "2026-09-04", PREFIXES, "ST")
    names = pool["name"].tolist()
    assert not any("指数" in n for n in names)
    # sh.51xxxx ETF / sz.15xxxx ETF 前缀不在白名单
    assert not any(c.startswith(("sh.5", "sz.15")) for c in pool["code"])


def test_st_name_flag():
    """名称含 ST 的候选被标记（辅助字段，剔除由日K isST 负责）。"""
    pool, stats = build_universe(FakeFetcher(), "2026-09-04", PREFIXES, "ST")
    assert stats.st_name_count > 0
    flagged = pool[pool["is_st_name"]]
    assert all("ST" in n for n in flagged["name"])
