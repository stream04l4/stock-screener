# -*- coding: utf-8 -*-
"""v6.1.8 full 模式测试（TL D1-D7 交付物 ②；**v7.0.18 后仅存与 baostock 无关的用例**）。

覆盖 brief F1-F4（全离线，零真实网络/零真实外部渠道）：
  - F1 t8/t9：run_t8 直驱（recompute_all 落库）、run_t9 直驱（em-sentinel csv 哨兵）、
    full 模式轻量阶段前置（t8/t9 在 t5 之前，段标顺序）；
  - F2 量纲：cross_check_f10 量纲错位防御（~100× → log error + 跳过，不误报）
    （cross_check 函数保留供未来多源复用；BaoStockAdapter/_bs_to_pct 用例随渠道移除）；
  - F3 抽样：_t5_crosscheck_sampled 随 T5 交叉校验整体移除（T5=adata 单源，
    v7.0.18 Joel 拍板——跨源对账能力放弃）；
  - F4 配额：QuotaGuard 到顶/边界行为（守卫迁 lake/ingest/common.py，泛化保留）。

T1 主源=v7.0.18 起 tushare stock_basic（run_full 用例中 fake pro 零网络）。
"""
from __future__ import annotations

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
import lake.ingest.source_pool as sp  # noqa: E402

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
# F2 量纲：cross_check_f10 量纲错位防御（~100× → log error + 跳过，不误报）
# （v7.0.18：源名用中性的 "src_a"/"src_b"——原 baostock 对端随渠道移除）
# ===========================================================================
def test_cross_check_f10_dimension_mismatch_skipped_not_false_positive(caplog):
    """旧 bug 复现场景：一源=百分数(91.92) vs 另一源=小数(0.9192) → 恰好 100×。
    防御命中 → log error '疑似量纲未对齐' + 跳过该字段比较 → **不记分歧**（返回 None）。"""
    import logging

    a = [{"period": "2026Q2", "roe_weighted": None, "gross_margin": 18.1436,
          "liability_pct": 91.9207}]
    b = [{"period": "2026Q2", "roe_weighted": None, "gross_margin": 0.181436,
          "liability_pct": 0.919207}]
    with caplog.at_level(logging.ERROR, logger="lake.ingest.source_pool"):
        s = sp.cross_check_f10(a, "adata_f10", b, "src_b", pp=1.0)
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
    s = sp.cross_check_f10(a, "adata_f10", b, "src_b", pp=1.0)
    assert s is not None and "liability_pct" in s, f"真实分歧应检出: {s}"


def test_cross_check_f10_aligned_no_conflict():
    """两源同口径（都百分数，差 <1pp）→ 无分歧、无量纲跳过。"""
    a = [{"period": "2026Q2", "gross_margin": 18.1436, "liability_pct": 91.9207}]
    b = [{"period": "2026Q2", "gross_margin": 18.15, "liability_pct": 91.93}]
    s = sp.cross_check_f10(a, "adata_f10", b, "src_b", pp=1.0)
    assert s is None, f"同口径小差应无分歧: {s}"


# ===========================================================================
# F4 配额：QuotaGuard 到顶（count==budget）后 acquire 必拒 + 文件停在 budget
# （v7.0.18：QuotaGuard 迁 lake/ingest/common.py，异常 QuotaExhaustedError）
# ===========================================================================
def test_quota_guard_at_cap_acquire_rejects(tmp_path):
    """mock 计数到顶（count==budget=5000）→ acquire 必拒，严格 ≤budget。"""
    from lake.ingest.common import QuotaExhaustedError, QuotaGuard

    p = str(tmp_path / "bs_quota.json")
    g = QuotaGuard(daily_quota=5000, path=p)
    # 播种到顶（count==budget）——模拟"已用满当日预算"
    g.set_count(5000)
    assert g.get_state()[1] == 5000
    # 到顶后 acquire 必拒（显式失败，不静默放行 → 杜绝 count=5004>budget 的 off-by-one）
    with pytest.raises(QuotaExhaustedError, match="配额耗尽"):
        g.acquire()
    # 拒绝那次不计数、不落盘 → 文件停在 budget（严格 ≤budget，不越界）
    assert g.get_state()[1] == 5000, "到顶后 acquire 被拒，count 必须停在 budget"


def test_quota_guard_just_below_cap_allows_one(tmp_path):
    """count==budget-1 → 下一次 acquire 放行（count→budget），再下一次才拒（边界精确）。"""
    from lake.ingest.common import QuotaExhaustedError, QuotaGuard

    p = str(tmp_path / "bs_quota.json")
    g = QuotaGuard(daily_quota=5000, path=p)
    g.set_count(4999)
    assert g.acquire() == 5000          # budget-1 → budget：放行（恰好用满）
    with pytest.raises(QuotaExhaustedError):
        g.acquire()                      # budget → 拒绝（严格 ≤budget）
    assert g.get_state()[1] == 5000


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
class _Row:
    """fake DataFrame 行（itertuples 返回对象，属性按列名取）。"""

    def __init__(self, **kw):
        self.__dict__.update(kw)


class _FakeDF:
    """最小 DataFrame 替身：仅 itertuples(index=False)。"""

    def __init__(self, rows):
        self._rows = [_Row(**r) for r in rows]

    def itertuples(self, index=False):
        return iter(self._rows)


class _FakePro:
    """v7.0.18：T1 主源=tushare stock_basic——fake 零网络（L 状态含 1 只北交所验证排除）。"""

    def stock_basic(self, exchange="", list_status="L", fields=""):
        if list_status == "L":
            return _FakeDF([
                {"ts_code": "sh.600001", "name": "股600001", "industry": "金融",
                 "market": "主板", "list_date": "1999-01-01"},
                {"ts_code": "sz.000002", "name": "股000002", "industry": "农林牧渔",
                 "market": "主板", "list_date": "1999-01-01"},
                {"ts_code": "bj.920001", "name": "北交所股", "industry": "其他",
                 "market": "北交所", "list_date": "2021-01-01"},
            ])
        return _FakeDF([{"ts_code": "sh.600001", "delist_date": None}])


def test_full_t8_t9_before_t5_segment_order(tmp_path, monkeypatch, capsys):
    """run_full：六阶段 history→p3→t8→t9→t5→t6，t8/t9 轻量阶段前置在 t5 之前。"""
    db = str(tmp_path / "full6.duckdb")
    codes = ["sh.600001", "sz.000002"]
    con = lconn.open(db)
    _seed_master_kline(con, codes, n_days=30)

    # 复用 v616 的 fake 模式：t8/t9 用真实 run_t8/run_t9（零网络，本地），其余阶段 fake。
    # v7.0.18：T1=tushare（fake pro）+ 腾讯快照补 name/is_st（fake）；T2 池空→腾讯全史。
    import lake.ingest.tushare_ingest as tsi
    import lake.ingest.tencent_ingest as ti
    import screener.data.tencent as tmod

    monkeypatch.setattr(tsi, "tushare_available", lambda: True)
    monkeypatch.setattr(tsi, "_pro", lambda: _FakePro())
    monkeypatch.setattr(ti, "fetch_snapshot",
                        lambda client, batch: {c: {"name": f"股{c[-6:]}"} for c in batch})

    def _kl():
        return [{"date": d, "open": 10.0, "high": 11.0, "low": 9.5,
                 "close": 10.5, "volume": 100.0}
                for d in ("2026-09-16", "2026-09-17", "2026-09-18")]

    monkeypatch.setattr(ti, "fetch_kline_full_history",
                        lambda c, t, page_size=2000: [dict(r) for r in _kl()])
    monkeypatch.setattr(ti, "fetch_kline_ohlcv", lambda c, t, n: [dict(r) for r in _kl()])

    class _TC:
        pass

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
                     "npi": 1e9}]

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


# ===========================================================================
# T1（v7.0.18）：tushare stock_basic 单次 L 调用 + delist_date 沿用 + t1-delist
# ===========================================================================
def test_t1_single_l_call_preserves_delist_and_coarse_industry(tmp_path, monkeypatch):
    """v7.0.18.1：T1 主档只发 1 次 L 调用（D 档拆 t1-delist，免费档实测 1 次/小时）；
    delist_date 沿用 stock_master 旧值；行业=粗分类（csric2 置 NULL）；北交所排除。"""
    import lake.ingest.tushare_ingest as tsi
    import lake.ingest.tencent_ingest as ti
    import screener.data.tencent as tmod

    db = str(tmp_path / "t1x.duckdb")
    con = lconn.open(db)
    # 预置 1 行（baostock 时代旧数据）：带 delist_date + 旧 csric2
    con.execute(
        "INSERT INTO stock_master (ts_code,name,industry_csric2,board,is_st,"
        "source,fetched_at,data_version,delist_date) VALUES "
        "('sz.000002','旧值','J66','主板',0,'t','2026-01-01 09:00:00','v6.1','2026-09-01')")
    calls = []

    class _P:
        def stock_basic(self, exchange="", list_status="L", fields=""):
            calls.append(list_status)
            if list_status == "L":
                return _FakeDF([
                    {"ts_code": "sh.600001", "name": "股600001", "industry": "金融",
                     "market": "主板", "list_date": "1999-01-01"},
                    {"ts_code": "sz.000002", "name": "股000002", "industry": "农林牧渔",
                     "market": "主板", "list_date": "1999-01-01"},
                    {"ts_code": "bj.920001", "name": "北交所股", "industry": "其他",
                     "market": "北交所", "list_date": "2021-01-01"},
                ])
            return _FakeDF([])

    monkeypatch.setattr(tsi, "tushare_available", lambda: True)
    monkeypatch.setattr(tsi, "_pro", lambda: _P())
    monkeypatch.setattr(ti, "fetch_snapshot", lambda c, b: {})   # 快照补 name/is_st no-op

    class _TC:
        pass

    monkeypatch.setattr(tmod, "TencentClient", _TC)
    res = drv.run_t1(con, db)
    assert calls == ["L"], f"T1 应只发 1 次 L 调用（D 档已拆 t1-delist）: {calls}"
    assert res["tushare_calls_this_step"] == 1
    row = con.execute(
        "SELECT industry_name, industry_csric2, CAST(delist_date AS VARCHAR), source "
        "FROM stock_master WHERE ts_code='sz.000002'").fetchone()
    assert row == ("农林牧渔", None, "2026-09-01", "tushare"), \
        f"csric2 应 NULL、delist_date 应沿用旧值: {row}"
    n_bj = con.execute("SELECT COUNT(*) FROM stock_master WHERE ts_code='bj.920001'").fetchone()[0]
    assert n_bj == 0, "北交所不得入 universe"
    con.close()


def test_t1_delist_updates_only_existing_rows(tmp_path, monkeypatch):
    """t1-delist：D 调用只 UPDATE 命中行的 delist_date；universe 外 code 不插入。"""
    import lake.ingest.tushare_ingest as tsi

    db = str(tmp_path / "t1d.duckdb")
    con = lconn.open(db)
    con.execute(
        "INSERT INTO stock_master (ts_code,name,industry_csric2,board,is_st,"
        "source,fetched_at,data_version) VALUES "
        "('sz.000002','旧值','J66','主板',0,'t','2026-01-01 09:00:00','v6.1')")

    class _P:
        def stock_basic(self, exchange="", list_status="D", fields=""):
            assert list_status == "D"
            return _FakeDF([
                {"ts_code": "sz.000002", "delist_date": "2026-10-09"},
                {"ts_code": "bj.920999", "delist_date": "2026-10-09"},   # 不在库 → 跳过
            ])

    monkeypatch.setattr(tsi, "tushare_available", lambda: True)
    monkeypatch.setattr(tsi, "_pro", lambda: _P())
    res = drv.run_t1_delist(con)
    assert res["delist_updated"] == 1, res
    got = con.execute(
        "SELECT delist_date FROM stock_master WHERE ts_code='sz.000002'").fetchone()[0]
    assert str(got) == "2026-10-09", f"delist_date 应更新: {got}"
    n = con.execute("SELECT COUNT(*) FROM stock_master").fetchone()[0]
    assert n == 1, f"universe 外 code 不得插入: {n}"
    con.close()
