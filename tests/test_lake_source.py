# -*- coding: utf-8 -*-
"""LakeDataFetcher 单测（lake-source brief Part A.2）。

全部注入**临时 duckdb**（同生产 DDL：lake.ddl.DDL_STATEMENTS + INDEX_STATEMENTS，
tests/ 允许 import lake——零 import 约束只扫 screener/web/backtest），造数后断言：
- PIT 过滤（date/pub_date <= run_day；未来行不泄漏）
- **D5' is_st 取 T1**（T2.is_st 全 0 时 KlineData.is_st 仍=1——ST 剔除不得静默失效）
- dividend dedup（同 ex_date 多行 → ann_date 最新者 first-wins）+ PIT + cash_dps>0
- missing → None（未披露季度 / run_day 无行）
- 新鲜度守卫阈值（>3 交易日 warning；>10 raise DataSourceError；T5 pub_date 超披露周期 warning）
- no-op 方法（kline_af3_append/full、adjfactor_*、maybe_refresh_adjfactor→False）
- D1' T7 日历缺口（覆盖期前 → latest_trade_date=None / trade_dates=空，不报错）
- **存储 T2 adj_factor 因子源**（02_code 修正轮 brief：af1=raw×stored_af 精确验证、
  af NULL 段→None、事件序列提取=变化点、PIT 钳制；r_event 推导路径已废弃删除）
- 连接失败归一（被锁 → DataSourceError"独占"；缺文件/0 字节 → DataSourceError）

零网络：本文件不 import baostock、不发任何请求。
"""
from __future__ import annotations

import os
import tempfile as _tempfile
from datetime import date, timedelta

import duckdb
import pandas as pd
import pytest

from lake.ddl import DDL_STATEMENTS, INDEX_STATEMENTS  # tests/ 允许（零 import 约束只扫主路径）
from screener.data.baostock_client import DataSourceError
from screener.data.cache import DiskCache
from screener.data.fetchers import DataFetcher
from screener.data.lake_source import LakeDataFetcher

RUN_DAY = date(2026, 9, 15)   # 周二（T7/造数均按此锚定）


# --------------------------------------------------------------------------- 工具
def _weekdays(start: date, end: date):
    """[start, end] 内全部工作日（造数用"交易日"；T7 与 T2 共用保证对齐）。"""
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def _make_lake(tmp_path, name="lake.duckdb") -> str:
    """建空库（生产同 DDL + 索引），返回路径。"""
    db = tmp_path / name
    con = duckdb.connect(str(db))
    for stmt in DDL_STATEMENTS + INDEX_STATEMENTS:
        con.execute(stmt)
    con.close()
    return str(db)


def _insert_master(con, ts_code, name, industry="C39计算机、通信和其他电子设备制造业",
                   is_st=0, list_date="2015-01-01", delist_date=None):
    con.execute(
        "INSERT INTO stock_master (ts_code, name, industry_name, list_date, delist_date, "
        "is_st, source) VALUES (?,?,?,?,?,?,?)",
        [ts_code, name, industry, list_date, delist_date, is_st, "test"],
    )


def _insert_kline(con, ts_code, closes_by_date, volume=1000, is_st_t2=0, af_by_date=None):
    """T2 造数：closes_by_date = {date: close|None}（volume 可传 callable(date)->int）。

    af_by_date = {date: adj_factor|None}（存储 T2 adj_factor，缺省全 None=未灌入）。
    """
    rows = []
    for d in sorted(closes_by_date):
        c = closes_by_date[d]
        vol = volume(d) if callable(volume) else volume
        af = (af_by_date or {}).get(d)
        rows.append([ts_code, d.isoformat(), c, c, c, c, vol, None, None, is_st_t2,
                     None, af, "test", None, None])
    con.executemany(
        "INSERT INTO kline_daily (ts_code, date, open, high, low, close, volume, amount, "
        "pct_chg, is_st, preclose, adj_factor, source, fetched_at, data_version) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows,
    )


def _insert_dividend(con, ts_code, ex_date, cash_dps, ann_date=None):
    con.execute(
        "INSERT INTO dividend_events (ts_code, ex_date, ann_date, period, cash_dps, stk_div, "
        "source) VALUES (?,?,?,?,?,?,?)",
        [ts_code, ex_date.isoformat(), (ann_date or (ex_date - timedelta(days=20))).isoformat(),
         "test", cash_dps, None, "test"],
    )


def _insert_t5(con, ts_code, period, pub_date, roe_weighted=12.5, gross_margin=30.0,
               liability_pct=40.0, yoy_pni=8.0, npi=1e9):
    con.execute(
        "INSERT INTO fundamentals_quarterly (ts_code, period, pub_date, roe_avg, "
        "roe_weighted, yoy_pni, npi, ocf, gross_margin, liability_pct, source) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [ts_code, period, pub_date.isoformat(), None, roe_weighted, yoy_pni, npi, None,
         gross_margin, liability_pct, "test"],
    )


def _insert_t7(con, start: date, end: date):
    rows = [[ "sh000001", d.isoformat(), 1.0, 1.0, 1.0, 1.0, 1, None, "test"]
            for d in _weekdays(start, end)]
    con.executemany(
        "INSERT INTO index_daily (index_code, date, open, high, low, close, volume, amount, source) "
        "VALUES (?,?,?,?,?,?,?,?,?)", rows,
    )


def _base_lake(tmp_path) -> str:
    """标准 fixture 库：
    - T1: sh.600001(正常) / sh.600002(ST, T1.is_st=1) / sz.000003(短上市) / sz.000004(无K线)
    - T2: 600001/600002 = RUN_DAY-70d..RUN_DAY 全部工作日（close=10，含一天 volume=0 停牌行）；
      000003 = 仅最近 10 个工作日；T2.is_st 全 0（D5' 前提复现）。
    - T4: 600001 三个事件——2026-07-10 cash 0.5（ann 06-20）+ 同 ex_date 重复行 cash 0.3
      （ann 06-01，dedup 应取 ann 最新=0.5）+ 未来事件 2026-10-23 cash 0.9（PIT 应滤掉）。
    - T5: 600001 period 2025Q4 pub 2026-08-20（fresh，≥最近披露截止日 8/31? 否→见下）
      + period 2026Q3 pub 2026-10-01（未来 → PIT 不可见）。
    - T7: sh000001 2026-07-01..RUN_DAY 全部工作日。
    """
    db = _make_lake(tmp_path)
    con = duckdb.connect(db, read_only=False)
    _insert_master(con, "sh.600001", "测试银行")
    _insert_master(con, "sh.600002", "ST测试", is_st=1)
    _insert_master(con, "sz.000003", "短上市", list_date="2026-08-01")
    _insert_master(con, "sz.000004", "无K线股")

    days = list(_weekdays(RUN_DAY - timedelta(days=70), RUN_DAY))
    closes = {d: 10.0 for d in days}
    susp_day = days[-5]   # 停牌日：volume=0（G2"有行"分支造数）
    _insert_kline(con, "sh.600001", closes, volume=lambda d: 0 if d == susp_day else 1000)
    _insert_kline(con, "sh.600002", closes)   # T2.is_st=0（默认）——D5' 验证关键
    short_days = days[-10:]
    _insert_kline(con, "sz.000003", {d: 5.0 for d in short_days})

    _insert_dividend(con, "sh.600001", date(2026, 7, 10), 0.5, ann_date=date(2026, 6, 20))
    _insert_dividend(con, "sh.600001", date(2026, 7, 10), 0.3, ann_date=date(2026, 6, 1))
    _insert_dividend(con, "sh.600001", date(2026, 10, 23), 0.9)   # 未来 → PIT 滤掉

    _insert_t5(con, "sh.600001", "2025Q4", date(2026, 8, 20))
    _insert_t5(con, "sh.600001", "2026Q3", date(2026, 10, 1))     # pub>run_day → 不可见

    _insert_t7(con, date(2026, 7, 1), RUN_DAY)
    con.close()
    return db


def _fetcher(db_path: str, run_day: date = RUN_DAY) -> LakeDataFetcher:
    # client=None：lake 路径永不触发 BaoStock（惰性 login）；cache 占位不读写。
    f = LakeDataFetcher(None, DiskCache(_tempfile.mkdtemp(prefix="lake_test_cache_")),
                        datasource_cfg={"primary": "lake", "fallback": "fail_fast",
                                        "exdate_detector": {"factor_sanity_cap_pct": 30}},
                        db_path=db_path)
    f.set_run_day(run_day)
    return f


# --------------------------------------------------------------------------- 连接/守卫
def test_db_missing_raises(tmp_path):
    f = _fetcher(str(tmp_path / "nope.duckdb"))
    with pytest.raises(DataSourceError, match="不存在"):
        f.all_stock(RUN_DAY.isoformat())


def test_db_zero_byte_raises(tmp_path):
    p = tmp_path / "zero.duckdb"
    p.write_bytes(b"")
    f = _fetcher(str(p))
    with pytest.raises(DataSourceError, match="0 字节"):
        f.all_stock(RUN_DAY.isoformat())


def test_locked_db_normalized(tmp_path):
    """同进程 rw 连接占库 → read_only 连接被拒（实测文案 marker B）→ DataSourceError"独占"。

    与跨进程 backfill flock 的归一路径相同（_is_locked_db_error 双 markers）。
    """
    db = _make_lake(tmp_path)
    holder = duckdb.connect(db)          # rw 持有
    try:
        f = _fetcher(db)
        with pytest.raises(DataSourceError, match="独占"):
            f.all_stock(RUN_DAY.isoformat())
    finally:
        holder.close()


def test_freshness_stale_kline_raises(tmp_path):
    """kline 最新落后 run_day >10 交易日 → DataSourceError（拒绝用旧数据）。

    kline 最新=07-24(周四)，T7 覆盖 (07-24, 09-15] 全部工作日（~40 个交易日）→ raise。
    """
    db = _make_lake(tmp_path)
    con = duckdb.connect(db)
    old_days = list(_weekdays(RUN_DAY - timedelta(days=70), RUN_DAY - timedelta(days=53)))
    assert old_days[-1] == date(2026, 7, 24)   # 造数锚点自检（周四）
    _insert_master(con, "sh.600001", "测试银行")
    _insert_kline(con, "sh.600001", {d: 10.0 for d in old_days})
    _insert_t7(con, RUN_DAY - timedelta(days=75), RUN_DAY)   # T7 覆盖 (a,b] → 精确交易日差(~40)
    con.close()
    f = _fetcher(db)
    with pytest.raises(DataSourceError, match="陈旧"):
        f.all_stock(RUN_DAY.isoformat())


def test_freshness_stale_kline_warns(tmp_path):
    """3 < 落后 <=10 交易日 → warning（universe_notes），不 raise。

    run_day=2026-09-15(周二)；kline 最新=09-09(周三) → (09-09, 09-15] 内 T7 交易日
    = 09-10/09-11/09-14/09-15 共 4 个（>3 且 <=10）→ warning。
    """
    db = _make_lake(tmp_path)
    con = duckdb.connect(db)
    old_days = list(_weekdays(RUN_DAY - timedelta(days=20), RUN_DAY - timedelta(days=6)))
    assert old_days[-1] == date(2026, 9, 9)   # 造数锚点自检（防日历漂移破坏阈值边界）
    _insert_master(con, "sh.600001", "测试银行")
    _insert_kline(con, "sh.600001", {d: 10.0 for d in old_days})
    _insert_t7(con, RUN_DAY - timedelta(days=30), RUN_DAY)
    con.close()
    f = _fetcher(db)
    df = f.all_stock(RUN_DAY.isoformat())
    assert len(df) == 1
    assert any("新鲜度" in n for n in f.universe_notes)


def test_freshness_t5_stale_warns(tmp_path):
    """T5 最新 pub_date < 最近披露截止日（run_day=2026-09-15 → 2026-08-31）→ warning。"""
    db = _base_lake(tmp_path)
    con = duckdb.connect(db)
    # 把 T5 全部 pub_date 压到 4/30（早于 8/31 截止日）
    con.execute("UPDATE fundamentals_quarterly SET pub_date='2026-04-30'")
    con.close()
    f = _fetcher(db)
    f.all_stock(RUN_DAY.isoformat())
    assert any("fundamentals_quarterly" in n for n in f.universe_notes)


def test_freshness_ok_no_notes(tmp_path):
    """新鲜库（kline 到 run_day、T5 pub 2026-10-01 ≥ 截止日）→ 无新鲜度注记。"""
    f = _fetcher(_base_lake(tmp_path))
    f.all_stock(RUN_DAY.isoformat())
    assert not any("新鲜度" in n for n in f.universe_notes)


# --------------------------------------------------------------------------- 股票池/行业
def test_all_stock_shape_and_tradestatus(tmp_path):
    f = _fetcher(_base_lake(tmp_path))
    df = f.all_stock(RUN_DAY.isoformat())
    assert list(df.columns) == ["code", "tradeStatus", "code_name"]
    assert set(df["code"]) == {"sh.600001", "sh.600002", "sz.000003", "sz.000004"}
    m = dict(zip(df["code"], df["tradeStatus"]))
    assert m["sh.600001"] == 1     # run_day 有成交行
    assert m["sz.000003"] == 1     # 短上市但 run_day 有行
    assert m["sz.000004"] == 0     # 无K线 → LEFT JOIN NULL → 0
    assert df["code_name"][df["code"] == "sh.600002"].iloc[0] == "ST测试"


def test_all_stock_delist_excluded(tmp_path):
    db = _base_lake(tmp_path)
    con = duckdb.connect(db)
    con.execute("UPDATE stock_master SET delist_date='2026-09-01' WHERE ts_code='sz.000003'")
    con.close()
    f = _fetcher(db)
    df = f.all_stock(RUN_DAY.isoformat())
    assert "sz.000003" not in set(df["code"])


def test_industry_returns_csric_raw(tmp_path):
    """D6'：industry() 返回 T1.industry_name 原文（含 CSRC 前缀）。"""
    f = _fetcher(_base_lake(tmp_path))
    df = f.industry()
    assert list(df.columns) == ["code", "code_name", "industry"]
    row = df[df["code"] == "sh.600001"].iloc[0]
    assert row["industry"] == "C39计算机、通信和其他电子设备制造业"


# --------------------------------------------------------------------------- K线 / D5'
def test_kline_pit_run_day_clamp(tmp_path):
    """PIT：kline_af3_history 末行日期 <= run_day；run_day 前移 → 窗口收缩。"""
    db = _base_lake(tmp_path)
    f = _fetcher(db)
    hit = f.kline_af3_history("sh.600001")
    assert hit and hit["columns"] == list(DataFetcher.KLINE_AF3_FIELDS)
    assert str(hit["rows"][-1][0]) == RUN_DAY.isoformat()

    # 同一库、run_day 前移（两个 read_only 连接可共存；不再重建库）
    f2 = _fetcher(db, run_day=date(2026, 9, 1))   # 周三
    hit2 = f2.kline_af3_history("sh.600001")
    assert str(hit2["rows"][-1][0]) == "2026-09-01"


def test_is_st_from_t1_not_t2_D5(tmp_path):
    """D5'（关键）：T2.is_st 全 0，KlineData.is_st 必须=1（取 T1 stock_master.is_st）。"""
    f = _fetcher(_base_lake(tmp_path))
    kl = f.kline_af3_incremental("sh.600002", RUN_DAY.isoformat())
    assert kl is not None
    assert kl.is_st == 1, "D5' 违反：is_st 未取 T1（ST 剔除将静默失效）"

    kl1 = f.kline_af3_incremental("sh.600001", RUN_DAY.isoformat())
    assert kl1.is_st == 0
    # _af3_rows 行内 isST 列同样取 T1
    rows = f._af3_rows("sh.600002")
    assert all(r[3] == "1" for r in rows)


def test_kline_run_day_bar_and_suspension(tmp_path):
    f = _fetcher(_base_lake(tmp_path))
    kl = f.kline_af3_incremental("sh.600001", RUN_DAY.isoformat())
    assert kl.current_price == 10.0
    assert kl.n_rows == len(list(_weekdays(RUN_DAY - timedelta(days=70), RUN_DAY)))
    # 停牌日（volume=0 行）：tradestatus=0、current_price 仍=该行 close
    susp = sorted(d for d in _weekdays(RUN_DAY - timedelta(days=70), RUN_DAY))[-5]
    kl_s = f.kline_run_day("sh.600001", susp.isoformat())
    assert kl_s.run_day_tradestatus == 0
    assert kl_s.current_price == 10.0


def test_kline_missing_row_returns_none(tmp_path):
    """run_day 无行（未上市/停牌缺行）→ kline_af3_incremental None。"""
    f = _fetcher(_base_lake(tmp_path))
    assert f.kline_af3_incremental("sz.000004", RUN_DAY.isoformat()) is None
    # 早于上市日
    assert f.kline_af3_incremental("sz.000003", "2026-08-10") is None


def test_kline_first_date_hook(tmp_path):
    f = _fetcher(_base_lake(tmp_path))
    assert f.kline_af3_first_date("sh.600001") == \
        sorted(d for d in _weekdays(RUN_DAY - timedelta(days=70), RUN_DAY))[0].isoformat()
    assert f.kline_af3_first_date("sz.000004") is None


# --------------------------------------------------------------------------- 分红（T4）
def test_dividend_dedup_and_pit(tmp_path):
    """dedup：同 ex_date 两行 → ann_date 最新者（cash 0.5）；未来事件被 PIT 滤掉。"""
    f = _fetcher(_base_lake(tmp_path))
    recs = f.dividend("sh.600001", 2026)
    assert len(recs) == 1, f"dedup/PIT 失败: {recs}"
    r = recs[0]
    assert r["dividOperateDate"] == "2026-07-10"
    assert r["dividCashPsBeforeTax"] == 0.5   # ann 06-20 行（最新）胜出，非 0.3
    assert r["code"] == "sh.600001"


def test_dividend_year_boundary(tmp_path):
    f = _fetcher(_base_lake(tmp_path))
    assert f.dividend("sh.600001", 2025) == []      # 无 2025 事件
    assert len(f.dividend("sh.600001", 2026)) == 1


# --------------------------------------------------------------------------- 基本面（T5）
def test_fundamentals_pit_and_mapping(tmp_path):
    f = _fetcher(_base_lake(tmp_path))
    p = f.profit_data("sh.600001", 2025, 4)
    assert p is not None
    # D2'：roeAvg = roe_weighted（12.5）；gpMargin 百分数→小数
    assert p["roeAvg"] == 12.5
    assert p["gpMargin"] == pytest.approx(0.30)
    assert p["netProfit"] == 1e9
    assert p["pubDate"] == "2026-08-20"
    assert p["statDate"] == "20251231"
    # T5 schema 无对应列 → None（neutral_renorm 兜底）
    assert p["npMargin"] is None and p["totalShare"] is None

    g = f.growth_data("sh.600001", 2025, 4)
    assert g["YOYPNI"] == pytest.approx(0.08)       # yoy_pni 百分数→小数
    assert g["YOYNI"] is None

    b = f.balance_data("sh.600001", 2025, 4)
    assert b["liabilityToAsset"] == pytest.approx(0.40)

    cf = f.cashflow_data("sh.600001", 2025, 4)
    # D3'：T5.ocf=NULL → 因子字段全 None（但"已披露"语义保留：返回 dict 非 None）
    assert cf is not None and cf["CFOToNP"] is None


def test_fundamentals_missing_returns_none(tmp_path):
    """PIT 不可见（pub_date>run_day）/ 无行 → None。"""
    f = _fetcher(_base_lake(tmp_path))
    assert f.profit_data("sh.600001", 2026, 3) is None   # pub 2026-10-01 > run_day
    assert f.growth_data("sz.000004", 2025, 4) is None   # 无 T5 行
    assert f.cashflow_data("sz.000004", 2025, 4) is None


# --------------------------------------------------------------------------- no-op
def test_noop_methods(tmp_path):
    f = _fetcher(_base_lake(tmp_path))
    before = f.kline_af3_history("sh.600001")["rows"]
    f.kline_af3_append("sh.600001", [["2099-01-01", "sh.600001", "99", "0", "1"]])
    assert f.kline_af3_history("sh.600001")["rows"] == before   # 追加无效（lake 只读语义）
    f.kline_af3_full("sh.600001", "2000-01-01", RUN_DAY.isoformat())
    f.adjfactor_append("sh.600001", [["x"]])
    f.adjfactor_full("sh.600001", "2000-01-01", RUN_DAY.isoformat())
    assert f.maybe_refresh_adjfactor("sh.600001", []) is False
    # adjfactor_fetch 返回存储 T2 af 事件（该库未灌 adj_factor → 空，非 no-op 路径）
    cols, rows = f.adjfactor_fetch("sh.600001", "2026-01-01", RUN_DAY.isoformat())
    assert cols == ["code", "dividOperateDate", "foreAdjustFactor",
                    "backAdjustFactor", "adjustFactor"]
    assert rows == []


# --------------------------------------------------------------------------- T7 日历（D1'）
def test_trade_dates_and_latest(tmp_path):
    f = _fetcher(_base_lake(tmp_path))
    lts = f.latest_trade_date(RUN_DAY)
    assert lts == RUN_DAY                       # 2026-09-15 周二（T7 有行）
    # 周六请求 → 回退到最近交易日（周五 9/18? 不——T7 只造到 run_day=9/15 → 回退 9/15）
    assert f.latest_trade_date(date(2026, 9, 18)) == RUN_DAY
    td = f.trade_dates("2026-09-14", "2026-09-16")
    # T7 只造到 9/15 → 区间内只有 9/14、9/15（9/16 无行）
    assert [d for d, _ in td] == ["2026-09-14", "2026-09-15"]


def test_trade_dates_gap_before_t7_coverage_D1(tmp_path):
    """D1'：T7 覆盖期（2026-07-01）之前 → latest=None / trade_dates=空，不报错。"""
    f = _fetcher(_base_lake(tmp_path))
    assert f.latest_trade_date(date(2025, 1, 6)) is None
    assert f.trade_dates("2025-01-01", "2025-01-31") == []


# --------------------------------------------------------------------------- 存储 T2 adj_factor（修正轮）
def _af_lake(tmp_path) -> str:
    """因子源 fixture：sh.600001 平盘 close=10，存储 af 在 ex1/ex2 跳变（前向填充值）。

    - ex1=2026-07-10(周五)：af 1.0 → 1.25
    - ex2=2026-08-10(周一)：af 1.25 → 1.428571
    早期段（07-01..07-09）af=NULL（复现 sh.601398 式历史 NULL 段）。
    T4 同时有除权事件行——验证因子**不再**从 T4 推导（r_event 路径已废弃）。
    """
    db = _make_lake(tmp_path)
    con = duckdb.connect(db)
    _insert_master(con, "sh.600001", "测试银行")
    days = list(_weekdays(date(2026, 7, 1), RUN_DAY))
    closes = {d: 10.0 for d in days}
    af = {}
    for d in days:
        if d < date(2026, 7, 1):
            continue
        if d < date(2026, 7, 10):
            af[d] = None                       # 早期 NULL 段
        elif d < date(2026, 8, 10):
            af[d] = 1.25
        else:
            af[d] = 1.428571
    _insert_kline(con, "sh.600001", closes, af_by_date=af)
    # T4 事件行存在但不得影响因子（废弃路径回归守卫）
    _insert_dividend(con, "sh.600001", date(2026, 7, 10), 0.5)
    _insert_dividend(con, "sh.600001", date(2026, 8, 10), 0.4)
    _insert_t7(con, date(2026, 7, 1), RUN_DAY)
    con.close()
    return db


def test_stored_af_rebuilt_exact(tmp_path):
    """af1 = raw_close × stored_af（逐日前向填充值）——精确验证 + NULL 段 → None。"""
    f = _fetcher(_af_lake(tmp_path))
    rebuilt = f.kline_af3_rebuilt("sh.600001")
    assert rebuilt is not None
    m = {d: (a3, a1) for d, a3, a1 in zip(rebuilt["dates"], rebuilt["af3_close"],
                                          rebuilt["af1_close"])}
    # 早期 NULL 段：close 有值但 af=NULL → af1=None（不得用前值/1.0 填充）
    assert m["2026-07-09"][0] == 10.0 and m["2026-07-09"][1] is None
    # ex1 当日及之后：af=1.25 → af1=12.5
    assert m["2026-07-10"][1] == pytest.approx(12.5)
    assert m["2026-08-07"][1] == pytest.approx(12.5)
    # ex2 当日及之后：af=1.428571 → af1=14.28571
    assert m["2026-08-10"][1] == pytest.approx(14.28571)
    assert m[RUN_DAY.isoformat()][1] == pytest.approx(14.28571)


def test_stored_af_events_extraction(tmp_path):
    """adjfactor_history = 存储 af 变化点事件序列（含首个非 NULL 基准日）。

    事件：07-10 af=1.25（首个非 NULL 日=基准）、08-10 af=1.428571。
    T4 除权事件行存在但**不产生**因子事件（r_event 路径废弃）。
    """
    db = _af_lake(tmp_path)
    f = _fetcher(db)
    hit = f.adjfactor_history("sh.600001")
    assert hit is not None
    assert hit["columns"] == ["code", "dividOperateDate", "foreAdjustFactor",
                              "backAdjustFactor", "adjustFactor"]
    assert [(r[1], r[3]) for r in hit["rows"]] == [
        ("2026-07-10", "1.250000"), ("2026-08-10", "1.428571")]
    # adjfactor_fetch 窗口过滤 + PIT
    _, rows = f.adjfactor_fetch("sh.600001", "2026-08-01", RUN_DAY.isoformat())
    assert [r[1] for r in rows] == ["2026-08-10"]
    # PIT：run_day 前移到 ex2 之前 → ex2 事件不可见（同库第二个 read_only 连接，
    # 与 test_kline_pit_run_day_clamp 同法——不重建库）
    f2 = _fetcher(db, run_day=date(2026, 8, 5))
    hit2 = f2.adjfactor_history("sh.600001")
    assert [r[1] for r in hit2["rows"]] == ["2026-07-10"]


def test_stored_af_rebuilt_pit_clamp(tmp_path):
    """PIT：run_day 前移 → af1 窗口收缩且不含未来段（af 钳制与 K线同口径）。"""
    db = _af_lake(tmp_path)
    f2 = _fetcher(db, run_day=date(2026, 8, 5))
    rebuilt = f2.kline_af3_rebuilt("sh.600001")
    assert rebuilt is not None
    assert rebuilt["dates"][-1] == "2026-08-05"
    # 末行仍在 ex1..ex2 段：af=1.25 → af1=12.5
    assert rebuilt["af1_close"][-1] == pytest.approx(12.5)


def test_no_stored_af_rebuilt_identity(tmp_path):
    """无存储 af（全 NULL）→ af1 全 None（因子未知，不得退化为不复权）。"""
    f = _fetcher(_base_lake(tmp_path))   # _base_lake 未灌 adj_factor
    rebuilt = f.kline_af3_rebuilt("sh.600001")
    assert rebuilt is not None and len(rebuilt["dates"]) > 0
    assert all(a1 is None for a1 in rebuilt["af1_close"])
    # af3 不受影响（raw close 照常返回）
    assert rebuilt["af3_close"][-1] == 10.0
    # adjfactor_history 无 af → None（与 DataFetcher"无缓存"语义一致）
    assert f.adjfactor_history("sh.600001") is None
    assert f.adjfactor_last_date("sh.600001") is None


def test_stored_af_null_segment_mid_history(tmp_path):
    """af NULL 段夹在历史中间（如 sh.601398 2007-2012）→ 该段 af1=None、前后正常。"""
    db = _make_lake(tmp_path)
    con = duckdb.connect(db)
    _insert_master(con, "sh.600001", "测试银行")
    days = list(_weekdays(date(2026, 7, 1), RUN_DAY))
    closes = {d: 10.0 for d in days}
    af = {}
    for d in days:
        if d < date(2026, 7, 10):
            af[d] = 1.0
        elif d < date(2026, 8, 10):
            af[d] = None                    # 中段 NULL（复现 sh.601398 式缺口）
        else:
            af[d] = 1.5
    _insert_kline(con, "sh.600001", closes, af_by_date=af)
    _insert_t7(con, date(2026, 7, 1), RUN_DAY)
    con.close()
    f = _fetcher(db)
    rebuilt = f.kline_af3_rebuilt("sh.600001")
    m = {d: a1 for d, a1 in zip(rebuilt["dates"], rebuilt["af1_close"])}
    assert m["2026-07-09"] == pytest.approx(10.0)     # 前段正常（af=1.0）
    assert m["2026-07-13"] is None                    # NULL 段 → None（不填充）
    assert m["2026-08-10"] == pytest.approx(15.0)     # 后段正常（af=1.5）
