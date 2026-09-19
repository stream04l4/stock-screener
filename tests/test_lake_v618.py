# -*- coding: utf-8 -*-
"""test_lake_v618 —— v6.1.8 离线单测（T8/T9 接入 + T5 量纲修复 + 交叉校验抽样 + 配额 off-by-one）。

覆盖 brief F1-F4（全离线，零真实网络/零真实 baostock）：
- **F2 量纲**：``_bs_to_pct`` 小数→百分数；``fetch_f10`` 输出比率字段 ×100、npi 不缩放；
  ``cross_check_f10`` 量纲错位防御（~100× → log error + 跳过该字段，不误报）+ 真实分歧仍检出。
- **F3 抽样**：``_t5_crosscheck_sampled`` pct=0 全跳/100 全做/种子可复现；config accessor 夹取 [0,100]。
- **F4 配额**：QuotaGuard 到顶（count==budget）后 acquire 必拒 + 文件停在 budget；client 集成
  到顶拒绝且 query_fn 零调用；BaoStockAdapter._get_client 的 QuotaGuard.daily_quota==lake 预算。
- **F1 t8/t9**：run_t8 直驱（真实 recompute_all，as_of=T2 max date、factor_snapshot 落库、
  progress total=universe/tier=P3）；run_t9 直驱（rf.fetch_rf_10y 替身零网络 + load_macro_rf
  读 em-sentinel csv → macro_rf 落库，验证 F1 哨兵缺陷修复）。

纪律：全离线——conftest autouse LAKE_MULTISOURCE=0；fake 打在模块级取数函数/adapter 上；
库一律 tmp_path，不碰 data/lake/、不碰生产 bs_quota.json（QuotaGuard 用 tmp path）。
"""
from __future__ import annotations

import json
import os
import sys

import pytest

duckdb = pytest.importorskip("duckdb")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
for _p in (REPO_ROOT, SCRIPTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from lake import backfill as lb  # noqa: E402
from lake import conn as lconn  # noqa: E402
from lake.config import t5_crosscheck_sample_pct  # noqa: E402
import lake.config as lconfig  # noqa: E402
import lake.ingest.baostock_ingest as bsi  # noqa: E402
import lake.ingest.source_pool as sp  # noqa: E402
from lake.ingest.baostock_adapter import BaoStockAdapter, _bs_to_pct  # noqa: E402

import lake_backfill as drv  # noqa: E402

FIXED_TODAY = "2026-09-18"


@pytest.fixture(autouse=True)
def _v618_isolate(tmp_path, monkeypatch):
    """路径隔离 + 离线门控（同 v616 口径）：progress→tmp、QuotaGuard per-task 门恒放行。"""
    monkeypatch.setattr(drv, "_today_beijing", lambda: FIXED_TODAY)
    monkeypatch.setattr(drv.time, "sleep", lambda s: None)
    monkeypatch.setattr(lb.BackfillRunner, "_quota_state", lambda self: (False, 0))
    prog = str(tmp_path / "prog" / "backfill_progress.json")
    monkeypatch.setattr(lb, "_progress_path", lambda: prog)
    monkeypatch.setattr(lconn, "progress_path", lambda: prog)
    yield


# ===========================================================================
# F2 量纲：_bs_to_pct（小数→百分数）
# ===========================================================================
def test_bs_to_pct_none_passthrough():
    assert _bs_to_pct(None) is None


def test_bs_to_pct_scales_by_100():
    # 浦发 2026Q2 liabilityToAsset 原始=0.923676 → 百分数 92.3676（与 adata ~92 同口径）
    assert _bs_to_pct(0.923676) == pytest.approx(92.3676)
    # 茅台 gpMargin 原始=0.895552 → 89.5552
    assert _bs_to_pct(0.895552) == pytest.approx(89.5552)
    # 负值（亏损季 yoy_pni）也 ×100，符号保留
    assert _bs_to_pct(-0.123) == pytest.approx(-12.3)


# ===========================================================================
# F2 量纲：fetch_f10 输出比率字段 ×100、npi（绝对额=元）不缩放
# ===========================================================================
def test_fetch_f10_ratio_fields_x100_npi_unchanged(monkeypatch):
    """BaoStock 原始=小数 → fetch_f10 输出 gross_margin/liability_pct/yoy_pni ×100；npi 原样。"""
    # fake baostock_ingest 取数（原始小数口径，实测核对见 _bs_to_pct docstring）
    monkeypatch.setattr(bsi, "fetch_profit",
                        lambda client, code, y, q: {
                            "pubDate": f"{y}-0{q}-31", "roeAvg": 0.05,
                            "gpMargin": 0.181436, "netProfit": 1_234_567_890.0})
    monkeypatch.setattr(bsi, "fetch_growth",
                        lambda client, code, y, q: {"YOYPNI": 0.0812})
    monkeypatch.setattr(bsi, "fetch_balance",
                        lambda client, code, y, q: {"liabilityToAsset": 0.923676})

    ad = BaoStockAdapter()
    # 用 fake client（避免真实 BaoStockClient 构造/登录）——_get_client 被替换
    class _FakeClient:
        pass
    monkeypatch.setattr(ad, "_get_client", lambda: _FakeClient())

    recs = ad.fetch_f10("sh.601398")
    assert recs, "fetch_f10 应返回记录（fake 每季都成功）"
    r = recs[0]
    # 比率字段 ×100 → 百分数口径（与 adata 同单位，cross_check 不再全量误报）
    assert r["gross_margin"] == pytest.approx(18.1436)
    assert r["liability_pct"] == pytest.approx(92.3676)
    assert r["yoy_pni"] == pytest.approx(8.12)
    # npi=netProfit 绝对额（元）——**不缩放**（×100 会错 100 倍）
    assert r["npi"] == 1_234_567_890.0
    # BaoStock 无加权 ROE → None（cross_check 跳过该字段，不误报）
    assert r["roe_weighted"] is None


# ===========================================================================
# F2 量纲：cross_check_f10 量纲错位防御（~100× → log error + 跳过，不误报）
# ===========================================================================
def test_cross_check_f10_dimension_mismatch_skipped_not_false_positive(caplog):
    """旧 bug 复现场景：adata=百分数(91.92) vs baostock=小数(0.9192) → 恰好 100×。
    防御命中 → log error '疑似量纲未对齐' + 跳过该字段比较 → **不记分歧**（返回 None）。"""
    import logging

    a = [{"period": "2026Q2", "roe_weighted": None, "gross_margin": 18.1436,
          "liability_pct": 91.9207}]
    b = [{"period": "2026Q2", "roe_weighted": None, "gross_margin": 0.181436,
          "liability_pct": 0.919207}]
    with caplog.at_level(logging.ERROR, logger="lake.ingest.source_pool"):
        s = sp.cross_check_f10(a, "adata_f10", b, "baostock", pp=1.0)
    # 量纲错位被跳过 → 无真实分歧 → None（不写 conflict_src，防静默误报）
    assert s is None, f"量纲错位不应记为分歧: {s}"
    # 且已 log error 留痕（可观测"口径没对齐"，而非静默吞掉）
    errs = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("疑似量纲未对齐" in r.getMessage() for r in errs), \
        f"应 log error 留痕: {[r.getMessage() for r in errs]}"


def test_cross_check_f10_real_divergence_still_detected():
    """非量纲错位的真实分歧（50 vs 91.92，比值 ~1.8 不在 [99,101]）→ 照常记分歧。"""
    a = [{"period": "2026Q2", "gross_margin": None, "liability_pct": 91.92}]
    b = [{"period": "2026Q2", "gross_margin": None, "liability_pct": 50.0}]
    s = sp.cross_check_f10(a, "adata_f10", b, "baostock", pp=1.0)
    assert s is not None and "liability_pct" in s, f"真实分歧应检出: {s}"


def test_cross_check_f10_aligned_no_conflict():
    """F2 修复后两源同口径（都百分数，差 <1pp）→ 无分歧、无量纲跳过。"""
    a = [{"period": "2026Q2", "gross_margin": 18.1436, "liability_pct": 91.9207}]
    b = [{"period": "2026Q2", "gross_margin": 18.15, "liability_pct": 91.93}]
    s = sp.cross_check_f10(a, "adata_f10", b, "baostock", pp=1.0)
    assert s is None, f"同口径小差应无分歧: {s}"


def test_cross_check_f10_dual_source_aligned_after_fix(monkeypatch):
    """离线复现交付物④：adata(百分数) vs 修复后 fetch_f10(×100) → 差异 <1pp、无分歧。"""
    # adata F10 主源（百分数口径，浦发 gross_margin~18 / liability~92）
    adata_recs = [{"period": "2026Q2", "pub_date": "2026-04-30", "roe_weighted": 5.0,
                   "gross_margin": 18.1436, "liability_pct": 91.9207, "yoy_pni": 8.12,
                   "npi": 1e9, "ocf": None}]
    # BaoStock 侧：原始小数 → fetch_f10 ×100（同 test_fetch_f10_ratio_fields_x100）
    monkeypatch.setattr(bsi, "fetch_profit",
                        lambda client, code, y, q: {"pubDate": f"{y}-0{q}-31",
                                                     "roeAvg": 0.05, "gpMargin": 0.181436,
                                                     "netProfit": 1e9})
    monkeypatch.setattr(bsi, "fetch_growth",
                        lambda client, code, y, q: {"YOYPNI": 0.0812})
    monkeypatch.setattr(bsi, "fetch_balance",
                        lambda client, code, y, q: {"liabilityToAsset": 0.919207})
    ad = BaoStockAdapter()

    class _FakeClient:
        pass
    monkeypatch.setattr(ad, "_get_client", lambda: _FakeClient())
    bs_recs = [r for r in ad.fetch_f10("sh.601398") if r["period"] == "2026Q2"]
    assert bs_recs, "应取到 2026Q2"
    # 修复后两源同口径 → cross_check 无分歧（差异 <1pp）
    s = sp.cross_check_f10(adata_recs, "adata_f10", bs_recs, "baostock", pp=1.0)
    assert s is None, f"修复后双源应同口径无分歧: {s}"


# ===========================================================================
# F3 抽样：_t5_crosscheck_sampled（pct=0 全跳 / 100 全做 / 种子可复现）
# ===========================================================================
def test_t5_sample_pct0_all_skip():
    for c in ("sh.600001", "sz.000002", "sh.601398"):
        assert drv._t5_crosscheck_sampled(c, 0) is False, f"pct=0 应全跳: {c}"


def test_t5_sample_pct100_all_do():
    for c in ("sh.600001", "sz.000002", "sh.601398"):
        assert drv._t5_crosscheck_sampled(c, 100) is True, f"pct=100 应全做: {c}"


def test_t5_sample_reproducible_same_day(monkeypatch):
    """种子=日期+code：同一天同一 code 判定恒一致（断点续传重跑不漂移）。"""
    monkeypatch.setattr(drv, "_today_beijing", lambda: "2026-09-18")
    codes = [f"sh.{600000 + i}" for i in range(1000)]
    r1 = {c: drv._t5_crosscheck_sampled(c, 5) for c in codes}
    r2 = {c: drv._t5_crosscheck_sampled(c, 5) for c in codes}
    assert r1 == r2, "同一天同一 code 判定必须可复现（断点续传不漂移）"
    # pct=5 → 约 5% 命中（统计区间 [2.5%,7.5%]，防退化恒真/恒假）
    n_hit = sum(r1.values())
    assert 25 <= n_hit <= 75, f"pct=5 命中率应 ~5%: {n_hit}/1000"


def test_t5_sample_rotates_across_days(monkeypatch):
    """不同天种子不同 → 抽样集合变化（长期看全市场都被覆盖到）。"""
    d1 = {f"sh.{600000 + i}": drv._t5_crosscheck_sampled(f"sh.{600000 + i}", 5)
          for i in range(200)}
    monkeypatch.setattr(drv, "_today_beijing", lambda: "2026-09-19")
    d2 = {f"sh.{600000 + i}": drv._t5_crosscheck_sampled(f"sh.{600000 + i}", 5)
          for i in range(200)}
    assert d1 != d2, "不同天抽样集合应变化（日期轮换）"


def test_t5_sample_pct_accessor_clamps(monkeypatch):
    """config accessor：夹到 [0,100]；非法值 → 缺省 5（fail-open，不阻断 T5）。"""
    cases = {5: 5, 100: 100, 0: 0, 150: 100, -3: 0, "abc": 5, None: 5}
    for raw, expect in cases.items():
        monkeypatch.setattr(lconfig, "lake_cfg", lambda v=raw: {"t5_crosscheck_sample_pct": v})
        assert t5_crosscheck_sample_pct() == expect, f"pct={raw!r} 应夹到 {expect}"


# ===========================================================================
# F4 配额：QuotaGuard 到顶（count==budget）后 acquire 必拒 + 文件停在 budget
# ===========================================================================
def test_quota_guard_at_cap_acquire_rejects(tmp_path):
    """brief 逐字：mock 计数到顶（count==budget=5000）→ acquire 必拒，严格 ≤budget。"""
    from screener.data.baostock_client import BaoStockError, QuotaGuard

    p = str(tmp_path / "bs_quota.json")
    g = QuotaGuard(daily_quota=5000, path=p)
    # 播种到顶（count==budget）——模拟"已用满当日预算"
    g.set_count(5000)
    assert g.get_state()[1] == 5000
    # 到顶后 acquire 必拒（显式失败，不静默放行 → 杜绝 count=5004>budget 的 off-by-one）
    with pytest.raises(BaoStockError, match="配额耗尽"):
        g.acquire()
    # 拒绝那次不计数、不落盘 → 文件停在 budget（严格 ≤budget，不越界）
    assert g.get_state()[1] == 5000, "到顶后 acquire 被拒，count 必须停在 budget"


def test_quota_guard_just_below_cap_allows_one(tmp_path):
    """count==budget-1 → 下一次 acquire 放行（count→budget），再下一次才拒（边界精确）。"""
    from screener.data.baostock_client import BaoStockError, QuotaGuard

    p = str(tmp_path / "bs_quota.json")
    g = QuotaGuard(daily_quota=5000, path=p)
    g.set_count(4999)
    assert g.acquire() == 5000          # budget-1 → budget：放行（恰好用满）
    with pytest.raises(BaoStockError):
        g.acquire()                      # budget → 拒绝（严格 ≤budget）
    assert g.get_state()[1] == 5000


def test_quota_client_at_budget_rejects_without_query(tmp_path, monkeypatch):
    """client 集成：QuotaGuard.daily_quota=budget，到顶后 call 立即拒、query_fn 零调用。"""
    import screener.data.baostock_client as bsm
    from screener.data.baostock_client import BaoStockClient, BaoStockError

    # 离线打桩 login（acquire 在 login 之前 → 到顶时连登录都不发生）
    class _Lg:
        error_code = "0"
        error_msg = "ok"
    monkeypatch.setattr(bsm.bs, "login", lambda *a, **k: _Lg())
    monkeypatch.setattr(bsm.bs, "logout", lambda *a, **k: None)

    p = str(tmp_path / "bs_quota.json")
    from screener.data.baostock_client import QuotaGuard
    QuotaGuard(daily_quota=5000, path=p).set_count(5000)   # 播种到顶
    calls = []

    class _RS:
        error_code = "0"
        error_msg = "ok"
        fields = ["code"]

        def next(self):
            return False

        def get_row_data(self):
            return None

    client = BaoStockClient(max_attempts=1, daily_quota=5000, quota_path=p)
    with pytest.raises(BaoStockError, match="配额耗尽"):
        client.call(lambda code="sh.600000": (calls.append(1), _RS())[1], label="x")
    assert calls == [], "到顶后 query_fn 必须零调用（acquire 先拒）"


def test_baostock_adapter_client_quota_equals_lake_budget(tmp_path, monkeypatch):
    """F4 根因修复：BaoStockAdapter._get_client 的 QuotaGuard.daily_quota==lake 日预算。

    为什么必须断言这条：BaoStockClient 默认 daily_quota=49900（screener 硬上限），若 lake
    不覆盖，硬上限≫lake 预算(5000) → 到顶后仍放行大量调用（v6.1.7 实锤 count=5004>5000）。
    修复=adapter 显式传 daily_quota=lake budget → 硬上限==预算，严格 ≤budget。
    """
    import screener.data.baostock_client as bsm

    # 隔离：default_quota_path → tmp（不碰生产 ~/.stock_screener/bs_quota.json）
    monkeypatch.setattr(bsm, "default_quota_path", lambda: str(tmp_path / "q.json"))
    # lake budget 取 config（默认 5000）——断言 adapter 传的就是这个值
    ad = BaoStockAdapter()
    client = ad._get_client()
    assert client.quota_guard is not None
    assert client.quota_guard.daily_quota == 5000, \
        f"adapter client QuotaGuard.daily_quota 应==lake budget 5000: {client.quota_guard.daily_quota}"


# ===========================================================================
# F1 t8：run_t8 直驱（真实 recompute_all，as_of=T2 max date、factor_snapshot 落库）
# ===========================================================================
def _seed_master_kline(con, codes, n_days=30):
    """播种 stock_master + kline_daily（n_days 天/股，close 递减 → drawdown 可算）。"""
    import datetime as _dt

    base = _dt.date(2026, 8, 1)
    for c in codes:
        con.execute(
            "INSERT INTO stock_master (ts_code,name,industry_csric2,board,is_st,"
            "source,fetched_at,data_version) VALUES (?,?,?,?,0,'t','2026-09-15 09:00:00','v6.1')",
            [c, f"股{c}", "J66", "主板"])
        for i in range(n_days):
            d = (base + _dt.timedelta(days=i)).isoformat()
            close = 100.0 - i * 0.5   # 递减 → 有回撤
            con.execute(
                "INSERT INTO kline_daily (ts_code,date,open,high,low,close,volume,"
                "adj_factor,source,fetched_at,data_version) VALUES (?,?,?,?,?,?,100,1.0,"
                "'t','2026-09-15 09:00:00','v6.1')",
                [c, d, close, close + 1, close - 1, close])
    # valuation_daily（pe_ttm/pb 非空，≥10 行 → pe/pb 分位可算）
    for c in codes:
        for i in range(12):
            d = (base + _dt.timedelta(days=i)).isoformat()
            con.execute(
                "INSERT INTO valuation_daily (ts_code,date,total_mv,pe_ttm,pb,"
                "source,fetched_at,data_version) VALUES (?,?,?,?,?,'t',"
                "'2026-09-15 09:00:00','v6.1')",
                [c, d, 100.0, 5.0 + i * 0.1, 0.8])


def test_run_t8_recompute_lands_factor_snapshot(tmp_path):
    """run_t8：as_of=库内 T2 max date；recompute_all 纯本地重算 → factor_snapshot 落库；
    progress factor_snapshot total=universe/tier=P3。"""
    db = str(tmp_path / "t8.duckdb")
    codes = ["sh.600001", "sz.000002"]
    con = lconn.open(db)
    _seed_master_kline(con, codes, n_days=30)

    runner = lb.BackfillRunner(db_path=db)
    stats = drv.run_t8(con, db, codes, runner)

    # as_of = 库内 T2 max date（brief 逐字）
    t2_max = str(con.execute("SELECT MAX(date) FROM kline_daily").fetchone()[0])[:10]
    assert stats["as_of_date"] == t2_max, f"as_of 应=T2 max date: {stats['as_of_date']} vs {t2_max}"
    # factor_snapshot 落库（drawdown/pe/pb 分位等因子；纯本地、零网络）
    n = con.execute("SELECT COUNT(*) FROM factor_snapshot").fetchone()[0]
    assert n > 0, f"factor_snapshot 应落库: {n}"
    # 全部落在 as_of=T2 max date（幂等覆盖同 as_of，不翻倍）
    n_asof = con.execute(
        "SELECT COUNT(DISTINCT ts_code) FROM factor_snapshot WHERE as_of_date=?",
        [t2_max]).fetchone()[0]
    assert n_asof == len(codes), f"两股都应有因子: {n_asof}"
    assert stats["rows_written"] > 0
    # progress：factor_snapshot total=universe(2)/tier=P3/done==total→done
    prog = lb.load_progress(lb.progress_path_for_db(db) or lb._progress_path())
    e8 = next((t for t in prog.get("tasks", []) if t.get("table") == "factor_snapshot"), None)
    assert e8 is not None, "progress 应有 factor_snapshot 条目"
    assert e8["tier"] == "P3", f"tier 应=P3（本地计算）: {e8}"
    assert e8["total"] == len(codes), f"total 应=universe: {e8}"
    assert e8["done"] == len(codes), f"done 应==total: {e8}"
    con.close()


def test_run_t8_no_kline_raises_clean(tmp_path):
    """无 T2 数据 → run_t8 显式失败（不硬造 as_of）→ run_full 编排层记 error、不阻断后续。"""
    db = str(tmp_path / "t8empty.duckdb")
    con = lconn.open(db)
    con.execute("INSERT INTO stock_master (ts_code,name,source,fetched_at,data_version) "
                "VALUES ('sh.600001','股x','t','2026-09-15 09:00:00','v6.1')")
    runner = lb.BackfillRunner(db_path=db)
    with pytest.raises(RuntimeError, match="无 as_of"):
        drv.run_t8(con, db, ["sh.600001"], runner)
    con.close()


# ===========================================================================
# F1 t9：run_t9 直驱（rf.fetch_rf_10y 替身零网络 + load_macro_rf 读 em-sentinel csv）
# ===========================================================================
def _write_rf_csv(cache_dir, rows):
    """按 rf.py 的落盘格式写 rf_10y_daily.csv（**em 哨兵**——验证 F1 哨兵缺陷修复）。"""
    import csv as _csv

    path = os.path.join(cache_dir, "rf_10y_daily.csv")
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = _csv.writer(f)
        w.writerow(["stock-screener-em-cache-v1"])   # rf.py 的 _RF_SENTINEL（em 哨兵）
        w.writerow(["date", "yield_pct"])
        for r in rows:
            w.writerow(r)
    return path


def test_run_t9_loads_macro_rf_from_em_sentinel_csv(tmp_path, monkeypatch):
    """run_t9：rf.fetch_rf_10y 替身（零网络）+ load_macro_rf 读 **em-sentinel** csv → macro_rf 落库。

    这条同时验证 F1 的哨兵缺陷修复：cache/rf_10y_daily.csv 由 rf.py 用 em 哨兵写入，
    local_cache_ingest 原只认 'stock-screener-cache-v1' → load_macro_rf 恒读 None（macro_rf 0 行）。
    修复后接受两哨兵 → 真实 csv 能落库。
    """
    db = str(tmp_path / "t9.duckdb")
    cache_dir = str(tmp_path / "cache")
    os.makedirs(cache_dir, exist_ok=True)
    # em-sentinel csv（rf.py 实际格式；2 行历史）
    _write_rf_csv(cache_dir, [["2026-09-10", "1.6800"], ["2026-09-17", "1.7100"]])

    con = lconn.open(db)
    # 隔离：_cache_dir → tmp cache；rf.fetch_rf_10y 替身（零网络，返回现值）
    monkeypatch.setattr(drv, "_cache_dir", lambda: cache_dir)
    import screener.data.rf as _rf_mod

    monkeypatch.setattr(_rf_mod, "fetch_rf_10y",
                        lambda cfg, cd, run_day, session=None: (0.0171,
                                                                {"source": "tradingeconomics",
                                                                 "date": "2026-09-17",
                                                                 "yield_pct": 1.71}))
    runner = lb.BackfillRunner(db_path=db)
    stats = drv.run_t9(con, db, None, runner)

    # macro_rf 落库（em-sentinel csv 的 2 行——验证哨兵修复，非 0 行）
    n = con.execute("SELECT COUNT(*) FROM macro_rf").fetchone()[0]
    assert n == 2, f"macro_rf 应落 em-sentinel csv 的 2 行（哨兵修复）: {n}"
    assert stats["rows_loaded"] == 2
    # progress：macro_rf total=1/done==1→done/tier=P3
    prog = lb.load_progress(lb.progress_path_for_db(db) or lb._progress_path())
    e9 = next((t for t in prog.get("tasks", []) if t.get("table") == "macro_rf"), None)
    assert e9 is not None, "progress 应有 macro_rf 条目"
    assert e9["total"] == 1 and e9["done"] == 1, f"macro_rf total=1/done=1: {e9}"
    assert e9["tier"] == "P3"
    con.close()


def test_run_t9_fetch_failure_uses_existing_csv_not_blocking(tmp_path, monkeypatch):
    """brief 逐字：rf.fetch_rf_10y 失败 → log warning + 用现有 csv 继续，不阻断（仍落库）。"""
    db = str(tmp_path / "t9fail.duckdb")
    cache_dir = str(tmp_path / "cache")
    os.makedirs(cache_dir, exist_ok=True)
    _write_rf_csv(cache_dir, [["2026-09-17", "1.7100"]])

    con = lconn.open(db)
    monkeypatch.setattr(drv, "_cache_dir", lambda: cache_dir)
    import screener.data.rf as _rf_mod

    def _boom(cfg, cd, run_day, session=None):
        raise RuntimeError("TE 抓取故障（测试注入）")
    monkeypatch.setattr(_rf_mod, "fetch_rf_10y", _boom)

    runner = lb.BackfillRunner(db_path=db)
    # 不抛（brief：失败不阻断）→ 仍用现有 csv 落库
    stats = drv.run_t9(con, db, None, runner)
    assert stats["rf_source"] == "fetch_failed_use_existing"
    n = con.execute("SELECT COUNT(*) FROM macro_rf").fetchone()[0]
    assert n == 1, f"抓取失败仍应用现有 csv 落库: {n}"
    con.close()


# ===========================================================================
# F1 t8/t9：full 模式轻量阶段前置（t8/t9 在 t5 之前；段标顺序）
# ===========================================================================
def test_full_t8_t9_before_t5_segment_order(tmp_path, monkeypatch, capsys):
    """run_full：六阶段 history→p3→t8→t9→t5→t6，t8/t9 轻量阶段前置在重配额 t5 之前。"""
    from lake.ingest.tencent_ingest import INDEX_CODES

    db = str(tmp_path / "full6.duckdb")
    codes = ["sh.600001", "sz.000002"]
    con = lconn.open(db)
    _seed_master_kline(con, codes, n_days=30)

    # 复用 v616 的 fake 模式：t8/t9 用真实 run_t8/run_t9（零网络，本地），其余阶段 fake。
    import lake.ingest.tencent_ingest as ti
    import lake.ingest.baostock_ingest as bsi_mod
    import screener.data.baostock_client as bsc
    import screener.data.tencent as tmod

    def _kl():
        return [{"date": d, "open": 10.0, "high": 11.0, "low": 9.5,
                 "close": 10.5, "volume": 100.0}
                for d in ("2026-09-16", "2026-09-17", "2026-09-18")]

    monkeypatch.setattr(ti, "fetch_kline_full_history",
                        lambda c, t, page_size=2000: [dict(r) for r in _kl()])
    monkeypatch.setattr(ti, "fetch_kline_ohlcv", lambda c, t, n: [dict(r) for r in _kl()])
    monkeypatch.setattr(ti, "fetch_snapshot",
                        lambda c, ts: {x: {"total_mv_yi": 1.0, "float_mv_yi": 1.0,
                                           "pe_ttm": 5.0, "pb": 0.8, "turnover": 1.0}
                                       for x in ts})
    monkeypatch.setattr(bsi_mod, "fetch_adjust_factor",
                        lambda bs, code, s, e: (["code", "adjustFactor"], []))

    class _BS:
        def __init__(self, *a, **k):
            pass

        def close(self):
            pass

    class _TC:
        pass

    monkeypatch.setattr(bsc, "BaoStockClient", _BS)
    monkeypatch.setattr(tmod, "TencentClient", _TC)

    # t9 零网络：rf.fetch_rf_10y 替身 + cache_dir→tmp（空 csv → load_macro_rf 0 行，不阻断）
    import screener.data.rf as _rf_mod

    monkeypatch.setattr(drv, "_cache_dir", lambda: str(tmp_path / "c9"))
    os.makedirs(str(tmp_path / "c9"), exist_ok=True)
    monkeypatch.setattr(_rf_mod, "fetch_rf_10y",
                        lambda cfg, cd, run_day, session=None: (0.017, {"source": "fake"}))

    # t5 adata mock + t6 sina fake
    class _AD:
        def available(self):
            return True

        def fetch_f10(self, c):
            return [{"period": "2026Q1", "pub_date": "2026-04-30", "roe_weighted": 5.0,
                     "gross_margin": 30.0, "liability_pct": 40.0, "yoy_pni": 8.0,
                     "npi": 1e9, "ocf": None}]

    class _Sina:
        def __init__(self, *a, **k):
            pass

        def fetch_holders(self, code6):
            return [{"end_date": "2026-03-31", "notice_date": "2026-04-20",
                     "holders": [{"holder_rank": 1, "holder_name": "A",
                                  "hold_shares": 1000.0, "circ_ratio_pct": 12.5,
                                  "share_nature": "国有股"}]}]

    monkeypatch.setattr(sp, "_REGISTRY", {"adata_f10": _AD()})
    monkeypatch.setattr(drv, "_t6_client_factory", lambda: _Sina())

    summary = drv.run_full(con, db, codes, "1990-01-01", FIXED_TODAY, days=3)

    # 六阶段顺序（t8/t9 在 t5 之前）
    assert list(summary["phases"]) == ["history", "p3", "t8", "t9", "t5", "t6"], \
        f"阶段序应 history→p3→t8→t9→t5→t6: {list(summary['phases'])}"
    for ph in ("history", "p3", "t8", "t9", "t5", "t6"):
        assert summary["phases"][ph]["ok"] is True, f"{ph} 应 ok: {summary['phases'][ph]}"
    # 段标：12 条（6×开始/结束），t8/t9 在 t5 之前
    out = capsys.readouterr().out
    marks = [ln for ln in out.splitlines() if "===== phase:" in ln]
    assert len(marks) == 12, f"应 12 条段标: {marks}"
    seq = [(ph) for ph, kind in [("history", None), ("history", None),
                                 ("p3", None), ("p3", None),
                                 ("t8", None), ("t8", None),
                                 ("t9", None), ("t9", None),
                                 ("t5", None), ("t5", None),
                                 ("t6", None), ("t6", None)]]
    for ln, ph in zip(marks, seq):
        assert f"===== phase: {ph} =====" in ln, f"段标序错误（期望 {ph}）: {marks}"
    # t8 真实重算落库（非 fake）：factor_snapshot 有行
    n_f = con.execute("SELECT COUNT(*) FROM factor_snapshot").fetchone()[0]
    assert n_f > 0, f"t8 应真实重算落库 factor_snapshot: {n_f}"
    con.close()
