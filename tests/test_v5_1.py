# -*- coding: utf-8 -*-
"""v5.1（TL V1-4/V1-5/V1-6）单测：payout 软约束 / reinvest 多期平滑 + DPS CAGR / 边界补全。

全部离线：纯函数 + monkeypatch 数据层（零网络、零 BaoStock、零东财、零新浪）。
引擎级用例直接调 ``_run_zscore``（fake fetcher + stub 数据源），验证：
- rf fallback → data_notes 含 "回退 config fallback"（评审① 防退化回归）；
- payout 软约束只改打分值、展示列仍为原始 payout；
- v4 配置回退（无新键）→ 打分值 == 原始 payout / 参考价 = 单年 DPS 原行为。
"""
from __future__ import annotations

import copy
import os
import sys
from datetime import date, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from screener import config as cfgmod  # noqa: E402
from screener.data import rf as rfmod  # noqa: E402
from screener.data import sina as sinamod  # noqa: E402
from screener.data.fetchers import KlineData  # noqa: E402
from screener.metrics import (  # noqa: E402
    annual_dps_from_em,
    consecutive_div_years,
    dps_cagr,
    payout_ratio_scored,
)
from screener.screener import ScreenResult, _run_zscore  # noqa: E402

RUN_DAY = date(2026, 9, 9)      # 与 v4/v5 基线运行日一致（annual_year=2025）
RUN_DAY_S = RUN_DAY.isoformat()


# ---------------------------------------------------------------------------
# V1-6.1 consecutive_div_years 边界（TL 核验③：实现行为正确，补专门用例）
# ---------------------------------------------------------------------------

def test_consecutive_div_years_two_payouts_same_year_counts_one():
    """某年两笔分红（annual_dps 聚合后该年>0）只算 1 年。"""
    # 2025 年两次除权各 0.15 → annual_dps_from_em 聚合为 {2025: 0.30}，连续计数按"年"
    rows = [
        {"code": "601398", "ex_date": "2025-07-14", "dps_pretax": 0.15, "progress": "实施分配"},
        {"code": "601398", "ex_date": "2025-12-15", "dps_pretax": 0.15, "progress": "实施分配"},
    ]
    annual = annual_dps_from_em(rows, "601398", RUN_DAY_S)
    assert set(annual) == {2025} and abs(annual[2025] - 0.30) < 1e-9
    # 2023..2025 连续（2025 聚合值>0）→ run_year=2026 数到 3 年，两笔分红不重复计数
    annual_full = {y: 0.1 for y in (2023, 2024)}
    annual_full[2025] = 0.30
    assert consecutive_div_years(annual_full, run_year=2026) == 3


def test_consecutive_div_years_recount_after_gap():
    """中断后重新计数：2026,2025 有 / 2024 断 / 2023..2019 有 → run_year=2027 时 =2。"""
    annual = {y: 0.1 for y in (2019, 2020, 2021, 2022, 2023, 2025, 2026)}
    assert consecutive_div_years(annual, run_year=2027) == 2
    # run_year-1 当年无分红 → 0（区别于"从未分红"=None）
    annual2 = {y: 0.1 for y in (2019, 2020, 2021)}
    assert consecutive_div_years(annual2, run_year=2027) == 0
    assert consecutive_div_years({}, run_year=2027) is None


# ---------------------------------------------------------------------------
# V1-6.2 annual_dps_from_em 同年多笔实施分配求和（构造行）
# ---------------------------------------------------------------------------

def test_annual_dps_same_year_multiple_payouts_summed():
    """同一自然年 3 笔实施分配 → 该年 DPS = 三笔之和（annual_dps 聚合口径）。"""
    rows = [
        {"code": "601398", "ex_date": "2025-01-07", "dps_pretax": 0.1434, "progress": "实施分配"},
        {"code": "601398", "ex_date": "2025-07-14", "dps_pretax": 0.1646, "progress": "实施分配"},
        {"code": "601398", "ex_date": "2025-12-15", "dps_pretax": 0.1414, "progress": "实施分配"},
        # 其他年份一笔（验证按年分组互不干扰）
        {"code": "601398", "ex_date": "2024-07-15", "dps_pretax": 0.10, "progress": "实施分配"},
    ]
    annual = annual_dps_from_em(rows, "601398", RUN_DAY_S)
    assert abs(annual[2025] - (0.1434 + 0.1646 + 0.1414)) < 1e-9
    assert abs(annual[2024] - 0.10) < 1e-9


# ---------------------------------------------------------------------------
# V1-6.3 payout_ratio_scored（TL V1-4 软约束纯函数）
# ---------------------------------------------------------------------------

def test_payout_ratio_scored_in_band_unchanged():
    """[lo,hi] 内 → 原值（含边界）。"""
    assert payout_ratio_scored(0.5, 0.3, 0.8, 0.5) == 0.5
    assert payout_ratio_scored(0.3, 0.3, 0.8, 0.5) == 0.3   # 下边界
    assert payout_ratio_scored(0.8, 0.3, 0.8, 0.5) == 0.8   # 上边界（80%→不变）


def test_payout_ratio_scored_above_hi_decays():
    """>hi → hi-(p-hi)*decay：100% → 0.8-0.2*0.5 = 0.7；下限 0。"""
    assert abs(payout_ratio_scored(1.0, 0.3, 0.8, 0.5) - 0.7) < 1e-12
    # 超额透支（payout>1）→ 衰减到负 → 截断为 0（不归负分）
    assert payout_ratio_scored(3.0, 0.3, 0.8, 0.5) == 0.0


def test_payout_ratio_scored_below_lo_decays():
    """<lo → p*decay：20% → 0.1（意愿不足降分但不归零）。"""
    assert abs(payout_ratio_scored(0.2, 0.3, 0.8, 0.5) - 0.1) < 1e-12


def test_payout_ratio_scored_none_and_decay_param():
    """None→None；decay 参数生效（config 驱动，禁硬编码）。"""
    assert payout_ratio_scored(None, 0.3, 0.8, 0.5) is None
    # decay=0.25：>hi → 0.8-0.2*0.25=0.75；<lo → 0.2*0.25=0.05
    assert abs(payout_ratio_scored(1.0, 0.3, 0.8, 0.25) - 0.75) < 1e-12
    assert abs(payout_ratio_scored(0.2, 0.3, 0.8, 0.25) - 0.05) < 1e-12


# ---------------------------------------------------------------------------
# V1-6.4 dps_cagr（TL V1-5 展示列纯函数）
# ---------------------------------------------------------------------------

def test_dps_cagr_normal_growth():
    """正常增长：1.0→1.5 两年 → (1.5/1.0)^(1/1)-1 = 50%。"""
    assert abs(dps_cagr({2024: 1.0, 2025: 1.5}, n_years=2, run_year=2026) - 0.5) < 1e-12
    # 多年：首末锚定 (1.9/1.0)^(1/4)-1（中间年缺失不影响）
    v = dps_cagr({2021: 1.0, 2025: 1.9}, n_years=5, run_year=2026)
    assert v is not None and abs(v - (1.9 ** 0.25 - 1.0)) < 1e-12


def test_dps_cagr_zero_or_missing_ends_none():
    """首年 0/缺失 → None；末年 0/缺失 → None。"""
    assert dps_cagr({2025: 1.5}, n_years=2, run_year=2026) is None          # 首年(2024)缺失→0
    assert dps_cagr({2024: 1.0}, n_years=2, run_year=2026) is None          # 末年(2025)缺失→0
    assert dps_cagr({2024: 0.0, 2025: 1.5}, n_years=2, run_year=2026) is None  # 首年显式 0


def test_dps_cagr_insufficient_window_none():
    """窗口不足（n_years<2）→ None。"""
    assert dps_cagr({2024: 1.0, 2025: 1.5}, n_years=1, run_year=2026) is None


# ---------------------------------------------------------------------------
# V1-6.5/6/7 引擎级（_run_zscore + fake fetcher；monkeypatch 数据层，零网络）
# ---------------------------------------------------------------------------

CODES = ["sh.601398", "sz.000001", "sz.000002"]   # A=高payout / B=低payout / C=无分红
NAMES = {"sh.601398": "工商银行", "sz.000001": "平安银行", "sz.000002": "万科A"}

# 分红行（本地静态表口径）：A 有 2021/2023/2025（2024 断档）、B 仅 2025、C 无
DIV_ROWS = [
    {"code": "601398", "report_date": "2020-12-31", "plan_notice_date": "2021-03-01",
     "ex_date": "2021-07-15", "dps_pretax": 1.0, "progress": "实施分配"},
    {"code": "601398", "report_date": "2022-12-31", "plan_notice_date": "2023-03-01",
     "ex_date": "2023-07-15", "dps_pretax": 1.0, "progress": "实施分配"},
    {"code": "601398", "report_date": "2024-12-31", "plan_notice_date": "2025-03-01",
     "ex_date": "2025-07-15", "dps_pretax": 1.9, "progress": "实施分配"},
    {"code": "000001", "report_date": "2024-12-31", "plan_notice_date": "2025-03-01",
     "ex_date": "2025-07-15", "dps_pretax": 0.2, "progress": "实施分配"},
]

# 基本面（BaoStock 口径字符串）：totalShare=1e8、netProfit=2e8 → payout = cash_annual*1e8/2e8
# A: cash_annual=1.9 → payout=0.95（>hi=0.8）；B: 0.2 → 0.10（<lo=0.3）；C: None
_FUND = {
    (2025, 4): {"totalShare": "100000000", "netProfit": "200000000", "roeAvg": "0.12",
                "gpMargin": "0.30", "pubDate": "2026-03-28"},
    (2024, 4): {"roeAvg": "0.11", "pubDate": "2025-03-28"},
    (2023, 4): {"roeAvg": "0.10", "pubDate": "2024-03-28"},
}


def _dates_ending(n: int, end: str) -> list[str]:
    d = date.fromisoformat(end)
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return list(reversed(out))


class _FakeFetcher:
    """duck-typed fetcher：只实现 _run_zscore 触碰的方法，全部本地数据。"""

    def __init__(self):
        self._dates = _dates_ending(300, RUN_DAY_S)

    # -- 基本面（_resolve_annual_year + 逐股 fund_rows）--
    def profit_data(self, code, year, quarter):
        return copy.deepcopy(_FUND.get((year, quarter)))

    def growth_data(self, code, year, quarter):
        return {"YOYPNI": "0.05"} if (year, quarter) == (2025, 4) else None

    def balance_data(self, code, year, quarter):
        if (year, quarter) == (2025, 4):
            return {"liabilityToAsset": "0.40", "currentRatio": "1.5"}
        if (year, quarter) == (2024, 4):
            return {"liabilityToAsset": "0.42", "currentRatio": "1.6"}
        return None

    def cashflow_data(self, code, year, quarter):
        return {"CFOToNP": "1.2"} if (year, quarter) == (2025, 4) else None

    # -- v4 路径分红（v5 路径走本地静态表，不经此）--
    def dividend(self, code, year):
        c6 = code.split(".")[1]
        recs = []
        for r in DIV_ROWS:
            if r["code"] == c6 and int(r["ex_date"][:4]) == year:
                recs.append({"code": code, "dividOperateDate": r["ex_date"],
                             "dividCashPsBeforeTax": r["dps_pretax"]})
        return recs

    # -- K线（本地重建窗口；300 根平盘 → 技术因子可算）--
    def kline_af3_rebuilt(self, code):
        return {"dates": self._dates, "af3_close": [10.0] * 300, "af1_close": [10.0] * 300}

    def maybe_refresh_adjfactor(self, code, recs):
        return False


class _FakeSinaClient:
    """OCF stub：全部返回 None → fcf_coverage 走降级代理（离线，零请求）。"""

    request_count = 0

    def __init__(self, sina_cfg):
        pass

    def fetch_annual_ocf(self, code6, run_day_s):
        return None


def _snapshots() -> dict:
    return {
        c: KlineData(code=c, dates=[RUN_DAY_S], closes=[10.0], tradestatus=[1],
                     last_date=RUN_DAY_S, n_rows=300, current_price=10.0,
                     is_st=0, run_day_tradestatus=1)
        for c in CODES
    }


def _run_engine(monkeypatch, cfg: dict, rf_source: str = "fallback") -> ScreenResult:
    """跑 _run_zscore（3 只候选，全离线）。rf_source 控制 fetch_rf_10y 的返回 source。"""
    monkeypatch.setattr(sinamod, "load_local_dividends",
                        lambda cache_dir: (list(DIV_ROWS), {"source": "local_static"}))

    def _fake_rf(rfc, cache_dir, run_day_s):
        return 0.02, {"source": rf_source, "date": run_day_s, "yield_pct": 2.0}
    monkeypatch.setattr(rfmod, "fetch_rf_10y", _fake_rf)
    monkeypatch.setattr(sinamod, "SinaClient", _FakeSinaClient)

    fetcher = _FakeFetcher()
    result = ScreenResult(run_day=RUN_DAY_S, requested_date=RUN_DAY_S, mode="zscore")
    scfg = cfgmod.scoring_cfg(cfg)
    ic = cfgmod.industry_cfg(cfg)
    window_start = RUN_DAY - timedelta(days=365)
    _run_zscore(
        cfg, fetcher, None, result, CODES, NAMES,
        {c: "J66 货币金融服务" for c in CODES},
        _snapshots(), scfg, ic, window_start, RUN_DAY, do_crosscheck=False,
    )
    return result


def _raw_payout(result: ScreenResult, code: str):
    for s in result.scored:
        if s.code == code:
            return s.raw["dividend"]["payout_ratio"]
    raise AssertionError(f"{code} 不在 scored")


def _cand_row(result: ScreenResult, code: str) -> dict:
    row = result.candidates[result.candidates["code"] == code].iloc[0]
    return row.to_dict()


def test_engine_v51_full_payout_soft_constraint_and_reinvest_smoothing(monkeypatch):
    """V1-6.3/5 + 展示列：v5.1 全配置（band=30~80/decay=0.5，smooth=3，growth=5）。

    A: payout=0.95 → 打分值 0.8-(0.95-0.8)*0.5=0.725；展示列仍 95.0%。
       ref = (1.0+0+1.9)/3 / 4% = 24.167（2024 断档按 0 拉低）；CAGR=(1.9^0.25-1)=17.4%。
    B: payout=0.10 → 打分值 0.05；ref = (0+0+0.2)/3/4% = 1.667；CAGR=None（首年缺失）。
    C: 无分红 → payout None、ref None、CAGR None。
    """
    cfg = cfgmod.load_config(os.path.join(ROOT, "config", "strategy.yaml"))
    result = _run_engine(monkeypatch, cfg, rf_source="fallback")

    # 打分值（软约束变换后）
    assert abs(_raw_payout(result, "sh.601398") - 0.725) < 1e-12
    assert abs(_raw_payout(result, "sz.000001") - 0.05) < 1e-12
    assert _raw_payout(result, "sz.000002") is None

    # CSV 展示列 = 原始 payout（不误导）
    a, b, c = (_cand_row(result, code) for code in CODES)
    assert a["payout_ratio_pct"] == 95.0 and b["payout_ratio_pct"] == 10.0
    assert c["payout_ratio_pct"] is None or (isinstance(c["payout_ratio_pct"], float)
                                             and c["payout_ratio_pct"] != c["payout_ratio_pct"])

    # reinvest 多期平滑参考价（窗口 [2023,2024,2025]，无分红年按 0）
    assert a["reinvest_ref_price_4pct"] == pytest.approx(24.167, abs=1e-3)
    assert b["reinvest_ref_price_4pct"] == pytest.approx(1.667, abs=1e-3)
    assert c["reinvest_ref_price_4pct"] is None or (isinstance(c["reinvest_ref_price_4pct"], float)
                                                    and c["reinvest_ref_price_4pct"] != c["reinvest_ref_price_4pct"])

    # DPS CAGR 列（近5年，首末锚定）
    assert a["dps_cagr_5y_pct"] == pytest.approx(17.4, abs=0.05)
    assert b["dps_cagr_5y_pct"] is None or (isinstance(b["dps_cagr_5y_pct"], float)
                                            and b["dps_cagr_5y_pct"] != b["dps_cagr_5y_pct"])
    assert c["dps_cagr_5y_pct"] is None or (isinstance(c["dps_cagr_5y_pct"], float)
                                            and c["dps_cagr_5y_pct"] != c["dps_cagr_5y_pct"])


def test_engine_rf_fallback_data_notes_regression(monkeypatch):
    """V1-6.6（评审① 防退化）：fetch_rf_10y source=fallback → data_notes 含回退标注。"""
    cfg = cfgmod.load_config(os.path.join(ROOT, "config", "strategy.yaml"))
    result = _run_engine(monkeypatch, cfg, rf_source="fallback")
    assert any("回退 config fallback" in n for n in result.data_notes), \
        f"rf fallback 未标注: {result.data_notes}"

    # 对照：source=tradingeconomics → 无 fallback 标注（正常源说明）
    result2 = _run_engine(monkeypatch, cfg, rf_source="tradingeconomics")
    assert not any("回退 config fallback" in n for n in result2.data_notes)


def test_engine_v4_config_fallback_payout_unchanged(monkeypatch):
    """V1-6.7（零回归）：strategy_v4.yaml（无 payout_band_pct 键）→ 打分值 == 原始 payout。"""
    cfg = cfgmod.load_config(os.path.join(ROOT, "config", "strategy_v4.yaml"))
    assert cfgmod.dividend_cfg(cfg)["payout_band_pct"] is None   # 前置：键确实缺失
    result = _run_engine(monkeypatch, cfg, rf_source="fallback")

    # v4 路径（v5_on=False）：无软约束变换，原始 payout 直接进打分
    assert abs(_raw_payout(result, "sh.601398") - 0.95) < 1e-12
    assert abs(_raw_payout(result, "sz.000001") - 0.10) < 1e-12
    assert _raw_payout(result, "sz.000002") is None
    # v4 路径不产出 v5 展示列（reinvest/CAGR 为空）
    a = _cand_row(result, "sh.601398")
    assert a["reinvest_ref_price_4pct"] is None or (isinstance(a["reinvest_ref_price_4pct"], float)
                                                    and a["reinvest_ref_price_4pct"] != a["reinvest_ref_price_4pct"])


def test_engine_v5_no_smooth_keys_single_year_refprice(monkeypatch):
    """V1-6.5（V1-5 零回归）：v5 配置但 reinvest 无 dps_smooth_years/dps_growth_years
    键 → 参考价 = 单年 DPS 原行为（A=1.9/4%=47.5、B=0.2/4%=5.0），CAGR 列空。"""
    cfg = cfgmod.load_config(os.path.join(ROOT, "config", "strategy.yaml"))
    del cfg["reinvest"]["dps_smooth_years"]
    del cfg["reinvest"]["dps_growth_years"]
    result = _run_engine(monkeypatch, cfg, rf_source="fallback")

    a, b, c = (_cand_row(result, code) for code in CODES)
    # 单年（annual_year=2025）DPS / 4% —— v5 原行为
    assert a["reinvest_ref_price_4pct"] == pytest.approx(47.5, abs=1e-3)
    assert b["reinvest_ref_price_4pct"] == pytest.approx(5.0, abs=1e-3)
    # 有效年份=0（C 无分红）→ ref_price None
    assert c["reinvest_ref_price_4pct"] is None or (isinstance(c["reinvest_ref_price_4pct"], float)
                                                    and c["reinvest_ref_price_4pct"] != c["reinvest_ref_price_4pct"])
    # growth 键缺失 → CAGR 列空
    for row in (a, b, c):
        v = row["dps_cagr_5y_pct"]
        assert v is None or (isinstance(v, float) and v != v), f"CAGR 应为空: {v}"
