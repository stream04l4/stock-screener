# -*- coding: utf-8 -*-
"""v5 Phase 1 新因子/新数据层单测（TL brief 验收②）。

fixture 来源：阶段 1 调研证据留样（evidence/，601398/000001 真实响应）：
- eastmoney_dividend_601398_sample.json：东财 RPT_SHAREBONUS_DET 原始行
  （PRETAX_BONUS_RMB 单位=每10股元；EX_DIVIDEND_DATE 含 null 未实施预案行的结构）。
- probe20_summary.json：601398 前十大股东真实留样（**IS_SJKZR 全为 0**——
  TL D3 实测结论：银行类必须靠关键词判 soe，纯标记规则会漏掉全部国有大行）。

必含用例（brief 验收②）：
1. 每10股单位换算（EM PRETAX_BONUS_RMB /10 → BaoStock 每股口径）；
2. 同除权日预案+正式去重（沿用 v4"一个除权日=一次事件"）；
3. EX_DIVIDEND_DATE=null 过滤（未实施预案不参与计算）;
4. 银行 IS_SJKZR=0 靠关键词判 soe（601398 真实股东留样）；
5. 新股 D1 规则边界（IPO 当年不要求分红）。

全部离线：纯函数 + 本地 fixture，零网络、零 BaoStock。
"""
from __future__ import annotations

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

EV = "/home/ubuntu/.hermes/joint_project/stock-screener/v5_strategy/stages/01_research/evidence"

from screener.metrics import (  # noqa: E402
    annual_dps_from_em,
    consecutive_div_years,
    dedup_dividends,
    dividend_yield_percentile,
    div_stability_cv,
    em_dividend_records,
    fcf_coverage,
    fcf_coverage_proxy,
    is_sjkzr_only,
    new_stock_div_ok,
    soe_flag,
    ttm_dividend_yield,
    yield_spread,
)
from screener.data import em as emmod  # noqa: E402

RUN_DAY = "2026-09-09"  # 与 v4 基线运行日一致


# ---------------------------------------------------------------------------
# fixture：evidence 真实留样 → em_dividend_all.csv 行口径（入库已 /10）
# ---------------------------------------------------------------------------

def _em_rows_from_evidence() -> list[dict]:
    """601398 东财分红原始留样 → 本程序缓存行口径（dps_pretax=PRETAX_BONUS_RMB/10）。"""
    d = json.load(open(f"{EV}/eastmoney_dividend_601398_sample.json", encoding="utf-8"))
    rows = []
    for r in d["first_rows"]:
        dps10 = r.get("PRETAX_BONUS_RMB")
        ex = (r.get("EX_DIVIDEND_DATE") or "").split(" ")[0] if r.get("EX_DIVIDEND_DATE") else ""
        rows.append({
            "code": str(r["SECURITY_CODE"]),
            "report_date": (r.get("REPORT_DATE") or "").split(" ")[0],
            "plan_notice_date": (r.get("PLAN_NOTICE_DATE") or "").split(" ")[0],
            "ex_date": ex,
            # ⚠️单位换算：EM 每10股元 → 每股（与 em.fetch_dividend_all 入库逻辑一致）
            "dps_pretax": None if dps10 is None else round(dps10 / 10.0, 6),
            "progress": str(r.get("ASSIGN_PROGRESS") or ""),
        })
    return rows


def _holder_601398() -> list[dict]:
    """601398 前十大股东真实留样（probe20；IS_SJKZR 全 '0'）。"""
    p = json.load(open(f"{EV}/probe20_summary.json", encoding="utf-8"))
    return [
        {
            "code": "601398",
            "holder_name": str(r["HOLDER_NAME"]),
            "is_sjkzr": str(r.get("IS_SJKZR") or "0"),
            "notice_date": "2026-08-29",
        }
        for r in p["icbc_sjkzr"]["rows"]
    ]


# ---------------------------------------------------------------------------
# 1. 每10股单位换算
# ---------------------------------------------------------------------------

def test_unit_conversion_per10_shares():
    """EM '10派1.689元'（PRETAX_BONUS_RMB=1.689）→ 每股 dps_pretax=0.1689（BaoStock 口径）。"""
    rows = _em_rows_from_evidence()
    assert rows, "fixture 为空"
    # 留样首行：2026-05-13 除权、PRETAX_BONUS_RMB=1.689（每10股）
    first = [r for r in rows if r["ex_date"] == "2026-05-13"]
    assert first, "留样应含 2026-05-13 除权行"
    # 入库口径已是每股：1.689/10 = 0.1689
    assert abs(first[0]["dps_pretax"] - 0.1689) < 1e-9

    # 逐年聚合（PIT ex_date<=run_day）：留样 5 行覆盖 2024-07..2026-05
    annual = annual_dps_from_em(rows, "601398", RUN_DAY)
    # 2025 年三次除权：2025-01-07(1.434) + 2025-07-14(1.646) + 2025-12-15(1.414) → /10
    assert abs(annual[2025] - (1.434 + 1.646 + 1.414) / 10.0) < 1e-9
    # 2026 年一次：1.689/10
    assert abs(annual[2026] - 1.689 / 10.0) < 1e-9


# ---------------------------------------------------------------------------
# 2. 同除权日预案+正式去重（v4 逻辑：一个除权日=一次事件）
# ---------------------------------------------------------------------------

def test_same_ex_date_dedup():
    """同一 ex_date 的'预案行 + 实施行'只计一次现金（annual_dps_from_em 优先取实施行）。"""
    rows = [
        # 2025-07-14：预案（3月公告，dps 未填）+ 正式（5月公告，dps=1.646/10股）
        {"code": "601398", "report_date": "2025-03-31", "plan_notice_date": "2025-03-29",
         "ex_date": "2025-07-14", "dps_pretax": None, "progress": "预案"},
        {"code": "601398", "report_date": "2025-03-31", "plan_notice_date": "2025-05-10",
         "ex_date": "2025-07-14", "dps_pretax": 0.1646, "progress": "实施分配"},
        # 2026-01-07：仅一行
        {"code": "601398", "report_date": "2025-12-31", "plan_notice_date": "2025-11-20",
         "ex_date": "2026-01-07", "dps_pretax": 0.1434, "progress": "实施分配"},
    ]
    annual = annual_dps_from_em(rows, "601398", RUN_DAY)
    # 去重后：2025 只计一次 0.1646（不是 None+0.1646 的重复，也不是预案行污染）
    assert abs(annual[2025] - 0.1646) < 1e-9
    assert abs(annual[2026] - 0.1434) < 1e-9

    # v4 dedup_dividends（ttm 口径）同除权日同样只计一次：
    recs = em_dividend_records(rows, {"601398": "sh.601398"})
    from datetime import date
    cash, _ = dedup_dividends(recs, date(2025, 7, 1), date(2025, 12, 31))
    assert abs(cash - 0.1646) < 1e-9


# ---------------------------------------------------------------------------
# 3. EX_DIVIDEND_DATE=null 过滤（未实施预案）
# ---------------------------------------------------------------------------

def test_null_ex_date_filtered():
    """EX_DIVIDEND_DATE=null 的未实施预案行：取数保留、计算时过滤。"""
    rows = [
        # 未实施预案（ex_date=''）——不应计入任何年度
        {"code": "601398", "report_date": "2026-06-30", "plan_notice_date": "2026-07-01",
         "ex_date": "", "dps_pretax": 0.20, "progress": "预案"},
        # 未来除权（PIT：run_day 之后）——同样不可见
        {"code": "601398", "report_date": "2026-06-30", "plan_notice_date": "2026-08-01",
         "ex_date": "2026-10-15", "dps_pretax": 0.15, "progress": "实施分配"},
        # 已实施（run_day 前）——计入
        {"code": "601398", "report_date": "2026-03-31", "plan_notice_date": "2026-03-28",
         "ex_date": "2026-05-13", "dps_pretax": 0.1689, "progress": "实施分配"},
    ]
    annual = annual_dps_from_em(rows, "601398", RUN_DAY)
    assert set(annual.keys()) == {2026}, f"应只有 2026（已实施行）: {annual}"
    assert abs(annual[2026] - 0.1689) < 1e-9

    # ttm 口径：未实施预案（无除权日）同样被 v4 dedup 过滤
    recs = em_dividend_records(rows, {"601398": "sh.601398"})
    from datetime import date
    cash, _ = dedup_dividends(recs, date(2026, 5, 1), date(2026, 9, 9))
    assert abs(cash - 0.1689) < 1e-9


# ---------------------------------------------------------------------------
# 4. 银行 IS_SJKZR=0 靠关键词判 soe（TL D3 实测结论）
# ---------------------------------------------------------------------------

def test_bank_soe_by_keyword_not_flag():
    """601398 真实股东留样：IS_SJKZR 全 0，但名称含'汇金'/'财政部' → soe（非 None）。"""
    holders = _holder_601398()
    assert all(h["is_sjkzr"] == "0" for h in holders), "留样 IS_SJKZR 应全为 0（TL D3 实测）"
    kws = ["国务院", "国资委", "汇金", "财政部", "国资"]
    assert soe_flag(holders, kws) == "soe"          # 关键词命中 → soe
    assert is_sjkzr_only(holders, kws) is False     # 已判 soe → 不进复核清单

    # 纯标记规则（无关键词）会漏掉 → None（证明必须靠关键词）
    assert soe_flag(holders, []) is None


def test_soe_confirmed_and_review_list():
    """IS_SJKZR=1 + 关键词命中 → soe_confirmed；仅 IS_SJKZR=1 未命中 → None + 复核清单。"""
    kws = ["国务院", "国资委", "汇金", "财政部", "国资"]
    # confirmed：实控人标记 + 名称命中
    h1 = [{"holder_name": "国务院国有资产监督管理委员会", "is_sjkzr": "1"}]
    assert soe_flag(h1, kws) == "soe_confirmed"
    assert is_sjkzr_only(h1, kws) is False
    # 仅标记未命中关键词 → None + 复核清单
    h2 = [{"holder_name": "某某控股集团有限公司", "is_sjkzr": "1"}]
    assert soe_flag(h2, kws) is None
    assert is_sjkzr_only(h2, kws) is True
    # 无标记无关键词 → None，不进复核清单（普通民企）
    h3 = [{"holder_name": "自然人甲", "is_sjkzr": "0"}]
    assert soe_flag(h3, kws) is None
    assert is_sjkzr_only(h3, kws) is False


# ---------------------------------------------------------------------------
# 5. 新股 D1 规则边界（IPO 当年不要求分红）
# ---------------------------------------------------------------------------

def test_d1_mature_stock_consecutive():
    """IPO 满 7 年 → 连续 >=5 个自然年有分红（从 run_year-1 向前数）。"""
    annual = {y: 0.1 for y in range(2018, 2026)}  # 2018..2025 连续 8 年
    n = consecutive_div_years(annual, run_year=2026)
    assert n == 8
    # 断档：2024 无分红 → 从 2025 起只数 1 年
    annual2 = {y: 0.1 for y in (2018, 2019, 2020, 2021, 2023, 2025)}
    assert consecutive_div_years(annual2, run_year=2026) == 1
    # 从未分红 → None（区别于断档=0）
    assert consecutive_div_years({}, run_year=2026) is None


def test_d1_new_stock_ipo_year_excluded():
    """IPO 不满 7 年：IPO 当年不要求分红，之后每个完整年度都要有。"""
    # IPO 2023（run_year=2026）→ 要求区间 [2024, 2025]；2023 当年无分红也 OK
    annual = {2024: 0.1, 2025: 0.1}
    assert new_stock_div_ok(annual, ipo_year=2023, run_year=2026) is True
    # 边界：IPO 当年（2023）有/无分红都不影响判定（上市不足整年，非"完整年度"）
    annual_with_ipo_yr = {2023: 0.1, 2024: 0.1, 2025: 0.1}
    assert new_stock_div_ok(annual_with_ipo_yr, ipo_year=2023, run_year=2026) is True
    # 完整年度断档（2024 无）→ 剔除
    annual_gap = {2025: 0.1}
    assert new_stock_div_ok(annual_gap, ipo_year=2023, run_year=2026) is False
    # 区间为空（IPO 次年即运行年：ipo=2025, run=2026 → range(2026,2026)=空）→ 满足
    assert new_stock_div_ok({}, ipo_year=2025, run_year=2026) is True


# ---------------------------------------------------------------------------
# 其余新因子（纯函数数值正确性）
# ---------------------------------------------------------------------------

def test_div_stability_cv():
    """近5年 DPS CV：完全稳定→0；样本<3→None。"""
    flat = {y: 0.2 for y in range(2021, 2026)}
    assert div_stability_cv(flat, n=5, end_year=2025) == 0.0
    # 样本不足（窗口内只有 2 年有分红）→ None
    sparse = {2024: 0.1, 2025: 0.2}
    assert div_stability_cv(sparse, n=5, end_year=2025) is None
    # 波动越大 CV 越大
    vol = {2021: 0.05, 2022: 0.30, 2023: 0.05, 2024: 0.30, 2025: 0.05}
    assert div_stability_cv(vol, n=5, end_year=2025) > 0.5


def test_fcf_coverage_and_proxy():
    """FCF 覆盖 = (OCF-capex)/(DPS×总股本)；代理 = CFOToNP/payout。"""
    # OCF=100亿 capex=20亿 DPS=0.4 股本=100亿股 → (100-20)/(0.4*100)=2.0
    v = fcf_coverage(1e10, 2e9, 0.4, 1e10)
    assert v is not None and abs(v - 2.0) < 1e-9
    # 缺输入 → None
    assert fcf_coverage(None, 2e9, 0.4, 1e10) is None
    assert fcf_coverage(1e10, 2e9, 0.0, 1e10) is None  # DPS=0 分母<=0
    # 代理：CFOToNP=1.5 payout=0.6 → 2.5
    p = fcf_coverage_proxy(1.5, 0.6)
    assert p is not None and abs(p - 2.5) < 1e-9
    assert fcf_coverage_proxy(None, 0.6) is None
    assert fcf_coverage_proxy(1.5, 0.0) is None


def test_yield_spread():
    """股息率 − 10Y（小数差值）；任一缺失 → None。"""
    assert abs(yield_spread(0.045, 0.0168) - (0.045 - 0.0168)) < 1e-12
    assert yield_spread(None, 0.0168) is None
    assert yield_spread(0.045, None) is None


def test_dividend_yield_percentile():
    """当前 TTM 股息率在自身历史年度序列中的分位 + 样本年数。"""
    dps = {2023: 0.20, 2024: 0.20, 2025: 0.20}
    closes = {"2023-12-29": 10.0, "2024-12-31": 8.0, "2025-12-31": 6.0}
    # 历史年度 yield：2023=0.02 2024=0.025 2025=0.0333
    pct, n = dividend_yield_percentile(dps, closes, "2026-09-09", lookback_years=10,
                                       current_ttm_yield=0.03)
    assert n == 3
    # 当前 0.03 > 0.02, 0.025，< 0.0333 → 2/3 = 66.67%
    assert abs(pct - 66.67) < 0.01
    # 当前 TTM 缺失 → 分位 None、样本数仍返回
    pct2, n2 = dividend_yield_percentile(dps, closes, "2026-09-09", 10, None)
    assert pct2 is None and n2 == 3


def test_ttm_dividend_yield_em_records():
    """EM 行 → BaoStock 口径记录 → v4 ttm_dividend_yield（零口径漂移）。"""
    rows = _em_rows_from_evidence()
    recs = em_dividend_records(rows, {"601398": "sh.601398"})
    from datetime import date
    # 窗口 [2025-09-09, 2026-09-09]：已除权 2025-12-15(0.1414) + 2026-05-13(0.1689)
    y = ttm_dividend_yield(recs, date(2025, 9, 9), date(2026, 9, 9), current_price=8.0)
    assert y is not None
    assert abs(y - (0.1414 + 0.1689) / 8.0) < 1e-9


# ---------------------------------------------------------------------------
# em.py 数据层（离线部分：缓存读写/年度行筛选/sanity）
# ---------------------------------------------------------------------------

def test_em_atomic_csv_roundtrip(tmp_path):
    """原子写 + 读回一致；无哨兵文件 → None（防半截缓存被误用）。"""
    p = str(tmp_path / "t.csv")
    emmod.em_atomic_write_csv(p, ["a", "b"], [["1", "2"], [None, "4"]])
    hit = emmod.em_read_csv(p)
    assert hit is not None
    cols, rows = hit
    assert cols == ["a", "b"] and rows == [["1", "2"], ["", "4"]]
    # 无哨兵 → None
    p2 = str(tmp_path / "bad.csv")
    with open(p2, "w") as f:
        f.write("a,b\n1,2\n")
    assert emmod.em_read_csv(p2) is None


def test_annual_cashflow_rows_pit():
    """年度行筛选（-12-31 且 DATE_TYPE_CODE=001）+ PIT（NOTICE_DATE<=run_day）。"""
    rows = [
        {"report_date": "2025-12-31", "date_type_code": "001", "notice_date": "2026-04-29",
         "netcash_operate": 1e10, "construct_long_asset": 2e9},
        {"report_date": "2025-12-31", "date_type_code": "002", "notice_date": "2026-04-29",
         "netcash_operate": 9e9, "construct_long_asset": None},   # 累计口径行 → 排除
        {"report_date": "2026-06-30", "date_type_code": "001", "notice_date": "2026-08-29",
         "netcash_operate": 5e9, "construct_long_asset": None},   # 半年报 → 排除
        {"report_date": "2026-12-31", "date_type_code": "001", "notice_date": "2027-04-28",
         "netcash_operate": 1e10, "construct_long_asset": None},  # run_day 未披露 → PIT 排除
    ]
    out = emmod.annual_cashflow_rows(rows, RUN_DAY)
    assert len(out) == 1
    assert out[0]["report_date"] == "2025-12-31" and out[0]["ocf"] == 1e10


def test_load_cgb10y_asof_unit(tmp_path):
    """cgb_10y_daily.csv 存百分数（1.6797=1.6797%）→ as-of 读取返回小数 + PIT。"""
    p = str(tmp_path / "cgb_10y_daily.csv")
    emmod.em_atomic_write_csv(p, ["date", "yield_pct"],
                              [["2026-09-08", "1.6815"], ["2026-09-09", "1.6816"],
                               ["2026-09-10", "1.6797"]])
    # as-of 2026-09-09 → 取 09-09 行（1.6816% → 0.016816 小数）；09-10 未来行不可见
    v = emmod.load_cgb10y_asof(str(tmp_path), "2026-09-09")
    assert v is not None and abs(v - 0.016816) < 1e-12
    # as-of 2026-09-10 → 0.016797
    v2 = emmod.load_cgb10y_asof(str(tmp_path), "2026-09-10")
    assert abs(v2 - 0.016797) < 1e-12


def test_detect_holders_end_date_candidates():
    """END_DATE 探测：候选季末序列正确（today=2026-09-09 → 从 2026-06-30 起，不含未披露完的 Q3）。"""
    # 不实际发请求——只验证候选生成逻辑（monkeypatch get_page）
    calls = []

    class FakeClient:
        request_count = 0

        def get_page(self, report_name, columns, page_number, filter_expr=None,
                     sort_columns="", sort_types=""):
            calls.append(filter_expr)
            # 2026-06-30 count 足够 → 命中
            if "2026-06-30" in (filter_expr or ""):
                return [], 58000
            return [], 10

    got = emmod.detect_holders_end_date(FakeClient(), "2026-09-09", min_rows=50000)
    assert got == "2026-06-30"
    # 首个探测的季末必须是 2026-06-30（Q3 未披露完，不得优先）
    assert "2026-06-30" in calls[0]
    assert "2026-09-30" not in calls[0]


# ---------------------------------------------------------------------------
# EMClient.fetch_all_pages 逐页持久检查点（限流/中断后从断点续取，不整表重拉）
# ---------------------------------------------------------------------------

class _FakeEMClient(emmod.EMClient):
    """无网络假客户端：继承真实 fetch_all_pages，仅 stub get_page。"""

    def __init__(self, rows_by_page, busy_from_page=None):
        # 绕过 EMClient.__init__（避免读 config）；直接设 fetch_all_pages/get_page 所需属性
        self.page_size = 500
        self.interval_s = 0.0          # 测试不 sleep
        self.timeout_s = 1.0
        self.max_attempts = 1
        self.cooldown_every_pages = 0  # 关闭周期冷却
        self.cooldown_seconds = 0.0
        self.busy_budget_s = 1.0
        self.request_count = 0
        self._last_request_ts = 0.0
        self.rows_by_page = rows_by_page          # {page: [row,...]}
        self.busy_from_page = busy_from_page      # >=该页抛 EMDataError(服务器繁忙)
        self.calls = []

    def get_page(self, report_name, columns, page, filter_expr=None, sort_columns="", sort_types="1", busy_budget_s=1800.0):
        self.calls.append(page)
        if self.busy_from_page and page >= self.busy_from_page:
            raise emmod.EMDataError("东财 %s page=%d 重试 3 次均失败: 服务器繁忙" % (report_name, page))
        return list(self.rows_by_page.get(page, [])), sum(len(v) for v in self.rows_by_page.values())


def test_checkpoint_resume_after_throttle(tmp_path):
    """全表 3 页；第 2 页起限流 -> 第一次停在第 1 页（检查点已持久）->
    重跑从第 2 页续取，不重拉第 1 页，最终行数 == count。"""
    rows = {1: [{"SECURITY_CODE": "600000"}] * 500,
            2: [{"SECURITY_CODE": "600001"}] * 500,
            3: [{"SECURITY_CODE": "600002"}] * 500}
    ck = str(tmp_path / ".em_ck_test.jsonl")

    # 第一次：第 1 页成功（检查点持久），第 2 页限流 -> EMDataError
    c1 = _FakeEMClient(rows, busy_from_page=2)
    with pytest.raises(emmod.EMDataError):
        c1.fetch_all_pages("T", "ALL", checkpoint_path=ck)
    assert os.path.exists(ck), "第 1 页成功后检查点应已持久"

    # 第二次（限流解除）：从第 2 页续取——get_page 不应再被调用 page=1
    c2 = _FakeEMClient(rows, busy_from_page=None)
    all_rows, count = c2.fetch_all_pages("T", "ALL", checkpoint_path=ck)
    assert 1 not in c2.calls, "续取不应重拉已持久的第 1 页"
    assert set(c2.calls) == {2, 3}
    assert len(all_rows) == 1500 and count == 1500
    assert not os.path.exists(ck), "成功后检查点应被删除"


def test_checkpoint_truncated_last_line_self_heals(tmp_path):
    """检查点末行被截断（不完整页）-> 忽略该行，该页重取；不崩溃。"""
    ck = str(tmp_path / ".em_ck_test.jsonl")
    rows = {1: [{"SECURITY_CODE": "600000"}] * 500,
            2: [{"SECURITY_CODE": "600001"}] * 500}
    # 手工构造：第 1 页完整 + 第 2 页截断（半个 JSON）
    with open(ck, "w", encoding="utf-8") as f:
        f.write(json.dumps({"page": 1, "count": 1000, "rows": rows[1]}, ensure_ascii=False) + "\n")
        f.write('{"page": 2, "count": 1000, "rows": [{"SECURITY_CODE": "60')  # 截断

    c = _FakeEMClient(rows, busy_from_page=None)
    all_rows, count = c.fetch_all_pages("T", "ALL", checkpoint_path=ck)
    assert set(c.calls) == {2}, "截断的第 2 页应重取"
    assert len(all_rows) == 1000 and count == 1000
