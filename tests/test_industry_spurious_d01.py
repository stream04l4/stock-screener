# -*- coding: utf-8 -*-
"""D-01 回归单测：industry 维度在 fundamental 全缺失时不得用缺失数据冒充。

缺陷（tester DIM-ADAPT-INDUSTRY-SpuriousActivation）：``rank_percentile`` 对组内
**全部 value 均为 None** 的 ROE/YOY 按 code 升序派生非 None 分位 → industry 因子
被误判"有数据"而激活，向截面打分注入 code 序噪声（违反 TL 硬性要求6）。

修复后约定：
- 全组无信号（所有 value 均 None）→ ``rank_percentile`` 返回 (None, None)
  → industry 因子=None → ``_dim_has_data(industry)=False`` → 该维不激活。
- **部分缺失**（组内有真值、个别股 None）→ 仍按"缺失排组末"约定正常排名
  （合理 quant 惯例，保留）。

覆盖两层：
1. ``screener.metrics.rank_percentile`` 纯函数层（v2 live 共用路径）；
2. ``backtest.engine.compute_period_factors`` + ``_dim_has_data`` 引擎层
   （合成缓存 fixture，无 dividend/profit/adjfactor 文件 = fundamental 真缺失）。

全部离线，零 live。
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date, timedelta

REPO = os.path.expanduser("~/stock-screener")
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import pytest  # noqa: E402

from backtest.data_pit import PitData  # noqa: E402
from backtest.engine import _dim_has_data, compute_period_factors  # noqa: E402
from screener.data.cache import DiskCache  # noqa: E402
from screener.metrics import rank_percentile  # noqa: E402

CODES = ["sh.600901", "sh.600902", "sh.600903"]


# ---------------------------------------------------------------------------
# 1) rank_percentile 纯函数层（v2 live 共用路径）
# ---------------------------------------------------------------------------
def test_rank_percentile_all_none_group_returns_none():
    """全组 ROE 均 None → (None, None)：无信号不排名，不按 code 序派生噪声分位。"""
    m: dict[str, float | None] = {c: None for c in CODES}
    assert rank_percentile(CODES[0], CODES, m) == (None, None)
    assert rank_percentile(CODES[-1], CODES, m) == (None, None)


def test_rank_percentile_partial_missing_ranks_last():
    """部分缺失（组内有真值）→ 正常排名，缺失股排组末（约定保留）。"""
    m = {CODES[0]: 12.5, CODES[1]: None, CODES[2]: 8.0}
    # 降序：600901(12.5) > 600903(8.0) > 600902(None)
    assert rank_percentile(CODES[0], CODES, m) == (1, pytest.approx(33.33))
    assert rank_percentile(CODES[2], CODES, m) == (2, pytest.approx(66.67))
    assert rank_percentile(CODES[1], CODES, m) == (3, pytest.approx(100.0))


def test_rank_percentile_mixed_one_factor_none_other_real():
    """ROE 全 None 而 YOY 有真值 → ROE 分位 None、YOY 正常排名（逐因子独立）。"""
    roe: dict[str, float | None] = {c: None for c in CODES}
    yoy = {CODES[0]: -5.0, CODES[1]: 20.0, CODES[2]: None}
    assert rank_percentile(CODES[0], CODES, roe) == (None, None)
    assert rank_percentile(CODES[1], CODES, yoy) == (1, pytest.approx(33.33))


# ---------------------------------------------------------------------------
# 2) 引擎层：compute_period_factors + _dim_has_data（合成缓存，fundamental 真缺失）
# ---------------------------------------------------------------------------
def _gen_kline(code: str, n: int = 80, start: str = "2021-01-04", base: float = 10.0):
    """n 根升序 K线（5字段，无 open 列），close 温和递增 → technical 因子可算。"""
    d0 = date.fromisoformat(start)
    return [[(d0 + timedelta(days=i)).isoformat(), code,
             f"{base + i * 0.01:.4f}", "0", "1"] for i in range(n)]


@pytest.fixture()
def pit_no_fundamentals(tmp_path):
    """2 只同行业股、各 80 根 K线；**无 dividend/profit/adjfactor 文件**。"""
    c = DiskCache(str(tmp_path))
    for code in CODES[:2]:
        c.put(f"kline_af3_{code}", ["date", "code", "close", "isST", "tradestatus"],
              _gen_kline(code))
    c.put("industry", ["updateDate", "code", "code_name", "industry"],
          [["2026-08-31", CODES[0], "测试A", "银行"],
           ["2026-08-31", CODES[1], "测试B", "银行"]])
    return PitData(str(tmp_path), ref_code=CODES[0])


def test_engine_industry_neutral_when_fundamentals_absent(pit_no_fundamentals):
    """fundamental 全缺失 → industry 因子=None、_dim_has_data(industry)=False。"""
    pit = pit_no_fundamentals
    T = date(2021, 4, 30)
    # 前置：确认 fundamental/dividend 真无数据（fixture 前提成立）
    assert pit._fundamental_row("profit", CODES[0], 2020, 4, T) is None
    assert len(pit.dividend_records(CODES[0], T)) == 0

    stocks, _ = compute_period_factors(
        pit, CODES[:2], T, annual_year=2020,
        industry_map={CODES[0]: "银行", CODES[1]: "银行"},
        max_bars=300, window_days=365, min_group_size=2)

    # technical 有 K线 → 激活；dividend/fundamental 无文件 → 不激活
    assert _dim_has_data(stocks, "technical") is True
    assert _dim_has_data(stocks, "dividend") is False
    assert _dim_has_data(stocks, "fundamental") is False
    # D-01 核心断言：industry 因子全 None → 不激活（不再按 code 序派生噪声分位）
    for s in stocks:
        assert s["factors"]["industry"] == {
            "roe_rank_pct": None, "yoy_pni_rank_pct": None}
    assert _dim_has_data(stocks, "industry") is False


def test_engine_industry_partial_missing_still_ranks(tmp_path):
    """部分缺失：同行业 3 股，仅 1 只有 profit/growth 真值 → 正常排名（缺失排组末）。

    - 有数据股 roe_rank_pct=100-33.33≈66.67（rank1/3）；
    - 无数据股按"缺失排组末"仍得非 None 分位（quant 惯例，保留）；
    - _dim_has_data(industry)=True（组内存在真值 → 维度激活）。
    """
    c = DiskCache(str(tmp_path))
    for code in CODES:
        c.put(f"kline_af3_{code}", ["date", "code", "close", "isST", "tradestatus"],
              _gen_kline(code))
    c.put("industry", ["updateDate", "code", "code_name", "industry"],
          [["2026-08-31", CODES[0], "测试A", "银行"],
           ["2026-08-31", CODES[1], "测试B", "银行"],
           ["2026-08-31", CODES[2], "测试C", "银行"]])
    # 仅 sh.600901 有已披露财报（pubDate<=T）：ROE=12.5、YOYPNI=20.0
    c.put(f"profit_{CODES[0]}_2020_4",
          ["code", "pubDate", "statDate", "roeAvg", "npMargin", "gpMargin",
           "netProfit", "epsTTM", "MBRevenue", "totalShare", "liqaShare"],
          [[CODES[0], "2021-04-20", "2020-12-31", "12.5", "0.30", "0.40",
            "1000000000", "2.5", "4000000000", "1000000000", "800000000"]])
    c.put(f"growth_{CODES[0]}_2020_4",
          ["code", "pubDate", "statDate", "YOYEquity", "YOYAsset", "YOYNI",
           "YOYEPSBasic", "YOYPNI"],
          [[CODES[0], "2021-04-20", "2020-12-31", "5.0", "4.0", "6.0", "7.0", "20.0"]])

    pit = PitData(str(tmp_path), ref_code=CODES[0])
    T = date(2021, 4, 30)
    stocks, _ = compute_period_factors(
        pit, CODES, T, annual_year=2020,
        industry_map={cd: "银行" for cd in CODES},
        max_bars=300, window_days=365, min_group_size=2)

    by_code = {s["code"]: s for s in stocks}
    # 有数据股：rank1/3 → pct=33.33 → 因子 100-33.33=66.67
    assert by_code[CODES[0]]["factors"]["industry"]["roe_rank_pct"] == pytest.approx(66.67)
    assert by_code[CODES[0]]["factors"]["industry"]["yoy_pni_rank_pct"] == pytest.approx(66.67)
    # 无数据股：缺失排组末（同序按 code 升序兜底）→ 600902 rank2/3 pct66.67 → 因子 33.33；
    # 600903 rank3/3 pct100 → 因子 0.0（非 None，惯例保留）
    assert by_code[CODES[1]]["factors"]["industry"]["roe_rank_pct"] == pytest.approx(33.33)
    assert by_code[CODES[1]]["factors"]["industry"]["yoy_pni_rank_pct"] == pytest.approx(33.33)
    assert by_code[CODES[2]]["factors"]["industry"]["roe_rank_pct"] == pytest.approx(0.0)
    assert by_code[CODES[2]]["factors"]["industry"]["yoy_pni_rank_pct"] == pytest.approx(0.0)
    # 组内有真值 → industry 维度激活
    assert _dim_has_data(stocks, "industry") is True
