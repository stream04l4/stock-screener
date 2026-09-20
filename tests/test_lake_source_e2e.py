# -*- coding: utf-8 -*-
"""LakeDataFetcher 离线端到端（lake-source brief Part A.4：pytest 全量的一部分）。

完整 ``run_screener``（mode=zscore，primary=lake）跑在**临时 duckdb** 上：
- 7 只造数股（4 正常候选 + ST + 次新 + 停牌）+ 基准股 sh.601398；
- **socket 守卫**：任何真实网络连接尝试 → AssertionError（零网络纪律的硬证据）；
- 断言漏斗计数 / ST 剔除（D5' e2e）/ 上市天数剔除 / 入选集合 / BaoStock 请求=0。

与单测（test_lake_source.py，方法级）互补：本文件验证 **引擎集成**——
run_screener 的 lake 分支实例化、_resolve_run_day(T7)、build_universe(T1⋈T2)、
stage2 快照(D5' is_st)、_probe_latest_period/_resolve_annual_year(T5 PIT)、
分红/基本面拉取、zscore 打分全链路。

config = 生产 strategy.yaml + 定向修改（v5 硬过滤关闭=跳过新浪 SOE/Tencent 市值——
D4' Phase 2 本期不动；canonical 关闭=零 raw 落盘）。这些修改只影响"网络源开关"，
不改变 DataFetcher 面行为。
"""
from __future__ import annotations

import random
import socket
from datetime import date, timedelta

import duckdb
import pytest
import yaml

from lake.ddl import DDL_STATEMENTS, INDEX_STATEMENTS  # tests/ 允许
from screener.config import load_config
from screener.screener import run_screener

RUN_DAY = date(2026, 9, 15)   # 周二


def _weekdays(start: date, end: date):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


CANDIDATES = [
    ("sh.601398", "工商银行", "J66货币金融服务"),
    ("sh.600028", "测试银行A", "J66货币金融服务"),
    ("sh.600036", "测试银行B", "J66货币金融服务"),
    ("sz.000001", "测试股份C", "G56航空运输业"),
]
ST_CODE = ("sh.600057", "ST测试", "C39计算机")
NEW_CODE = ("sz.300999", "次新股", "C39计算机")
SUSP_CODE = ("sh.600088", "停牌股", "G56航空运输业")


def _build_e2e_lake(tmp_path) -> str:
    db = tmp_path / "e2e.duckdb"
    con = duckdb.connect(str(db))
    for stmt in DDL_STATEMENTS + INDEX_STATEMENTS:
        con.execute(stmt)

    days = list(_weekdays(RUN_DAY - timedelta(days=420), RUN_DAY))   # ~285 交易日 > 250
    short_days = days[-100:]                                          # 次新：仅 100 行 < 250

    def _kline(ts_code, seed, day_list):
        rng = random.Random(seed)
        price = 8.0 + (seed % 7)
        rows = []
        for d in day_list:
            price = max(1.0, price * (1 + rng.uniform(-0.01, 0.011)))
            rows.append([ts_code, d.isoformat(), price, price, price, price, 5000,
                         None, None, 0, None, None, "test", None, None])
        con.executemany(
            "INSERT INTO kline_daily (ts_code, date, open, high, low, close, volume, amount, "
            "pct_chg, is_st, preclose, adj_factor, source, fetched_at, data_version) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)

    all_codes = CANDIDATES + [ST_CODE, NEW_CODE, SUSP_CODE]
    for i, (ts, name, ind) in enumerate(all_codes):
        con.execute(
            "INSERT INTO stock_master (ts_code, name, industry_name, list_date, is_st, source) "
            "VALUES (?,?,?,?,?,?)",
            [ts, name, ind, "2015-01-01", 1 if ts == ST_CODE[0] else 0, "test"])
        _kline(ts, seed=100 + i, day_list=short_days if ts == NEW_CODE[0] else days)

    # 停牌股：run_day 当天无行（删除最后一根）
    con.execute(f"DELETE FROM kline_daily WHERE ts_code='{SUSP_CODE[0]}' AND date='{RUN_DAY.isoformat()}'")

    # T4 分红：候选股 2026-07-15 除权 0.4 元/股（TTM 窗口内）
    for ts, _, _ in CANDIDATES:
        con.execute(
            "INSERT INTO dividend_events (ts_code, ex_date, ann_date, period, cash_dps, source) "
            "VALUES (?,?,?,?,?,?)", [ts, "2026-07-15", "2026-06-20", "test", 0.4, "test"])

    # T5：候选 + 基准股（sh.601398 本身也是候选）——2025Q4(年报) + 2026Q2(最近已披露季)
    for i, (ts, _, _) in enumerate(CANDIDATES):
        con.execute(
            "INSERT INTO fundamentals_quarterly (ts_code, period, pub_date, roe_weighted, "
            "yoy_pni, npi, gross_margin, liability_pct, source) VALUES (?,?,?,?,?,?,?,?,?)",
            [ts, "2025Q4", "2026-03-20", 9.0 + i * 1.5, 5.0 + i * 2, (1 + i) * 1e9,
             18.0 + i * 5, 35.0 + i * 4, "test"])
        con.execute(
            "INSERT INTO fundamentals_quarterly (ts_code, period, pub_date, roe_weighted, "
            "yoy_pni, npi, gross_margin, liability_pct, source) VALUES (?,?,?,?,?,?,?,?,?)",
            [ts, "2026Q2", "2026-08-25", 10.0 + i * 1.2, 3.0 + i, (1 + i) * 5e8,
             19.0 + i * 4, 36.0 + i * 3, "test"])

    # T7：sh000001 全部工作日（覆盖 run_day）
    rows = [["sh000001", d.isoformat(), 1, 1, 1, 1, 1, None, "test"] for d in days]
    con.executemany(
        "INSERT INTO index_daily (index_code, date, open, high, low, close, volume, amount, source) "
        "VALUES (?,?,?,?,?,?,?,?,?)", rows)
    con.close()
    return str(db)


def _lake_cfg(tmp_path) -> dict:
    """生产 strategy.yaml + 定向修改（关 v5 硬过滤网络源 + canonical；primary=lake）。"""
    cfg = load_config("config/strategy.yaml")
    cfg["datasource"]["primary"] = "lake"
    u = cfg["universe"]
    u["soe_required"] = False                    # D4'：Phase 2 网络源本期不动（测试离线）
    u["industry_whitelist_csric2"] = []
    u["min_total_mv_yi"] = None
    cfg["hard_filter"]["min_consecutive_div_years"] = None
    cfg.setdefault("canonical", {})["enabled"] = False
    cfg.setdefault("health", {})["enabled"] = False
    cfg["data"]["cache_dir"] = str(tmp_path / "cache")
    return cfg


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """零网络守卫：任何 socket 连接尝试 → 测试失败（lake-source 硬纪律）。"""

    def _boom(*a, **k):
        raise AssertionError(f"检测到真实网络连接尝试: {a} {k}")

    monkeypatch.setattr(socket, "create_connection", _boom)
    monkeypatch.setattr(socket.socket, "connect", _boom)
    yield


def test_run_screener_lake_e2e_offline(tmp_path, monkeypatch):
    db = _build_e2e_lake(tmp_path)
    cfg = _lake_cfg(tmp_path)
    # run_screener 内部实例化 LakeDataFetcher（不传 db_path）→ env 注入临时库
    monkeypatch.setenv("SCREENER_LAKE_DB", db)

    result = run_screener(cfg, RUN_DAY, output_dir=str(tmp_path / "out"), do_crosscheck=False)

    # ---- 运行日定位（T7）----
    assert result.run_day == RUN_DAY.isoformat()
    assert result.date_fallback is False

    # ---- 漏斗：股票池 6（停牌股 tradeStatus=0 剔除）→ ST 1 + 次新 1 → 候选 4 ----
    assert result.funnel["L1_股票池"] == 6, result.funnel
    assert result.st_excluded_count == 1          # D5' e2e：ST 股被 T1.is_st 剔除
    assert result.insufficient_kline_count == 1   # 次新 100 行 < 250
    assert result.funnel["L2_硬剔除后"] == 4

    # ---- 报告期探测（T5 PIT）：最近已披露=2026Q2；基准年度=2025 ----
    assert result.fundamental_period == "2026Q2"
    assert result.annual_year == 2025

    # ---- 打分 + TopN（top_n=50 > 候选数 → 4 只全入选）----
    assert result.funnel["L4_TopN入选"] == 4
    scored_codes = {s.code for s in result.scored}
    assert scored_codes == {c[0] for c in CANDIDATES}

    # ---- 零网络硬证据：BaoStock 占位客户端 0 请求；K线 0 请求 ----
    assert result.baostock_requests == 0
    assert result.kline_requests == 0

    # ---- 因子非空（分红/基本面链路真实产出）----
    for s in result.scored:
        assert s.raw["dividend"]["ttm_yield"] is not None, f"{s.code} ttm_yield 缺失"
        assert s.raw["fundamental"]["roe_level"] is not None, f"{s.code} roe_level 缺失（D2'）"


def test_datasource_cfg_accepts_lake():
    """config 层：primary=lake 通过校验；非法值仍拒绝。"""
    from screener.config import ConfigError, datasource_cfg

    cfg = load_config("config/strategy.yaml")
    cfg["datasource"]["primary"] = "lake"
    assert datasource_cfg(cfg)["primary"] == "lake"
    cfg["datasource"]["primary"] = "bogus"
    with pytest.raises(ConfigError):
        datasource_cfg(cfg)
