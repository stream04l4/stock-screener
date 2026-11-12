# -*- coding: utf-8 -*-
"""martingale.central_soe 单测（v8.1.1 央企收紧门）。

- 央企 token 命中/未命中（地方国企/央企小比例持股/关键词 SOE 无国有标注）；
- PIT：as_of 前取更早快照（控制权变更时点语义）；
- 漏斗：central_only=true 在 SOE 门之上剔地方国企，false = 零行为变化；
- config：central_only 缺省 false / 显式 true / 非布尔报错。
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from martingale.central_soe import central_soe_codes_asof, central_soe_codes_latest  # noqa: E402
from martingale.config import ConfigError, UniverseParams, _universe_section  # noqa: E402
from martingale.universe import build_pool  # noqa: E402

RUN_YEAR = 2026


def _mk_db(path: Path) -> None:
    """5 只合成股 + 带 hold_ratio 的股东快照（两期，PIT 用）。"""
    con = duckdb.connect(str(path))
    con.execute("""
        CREATE TABLE stock_master(ts_code VARCHAR, name VARCHAR, industry_csric2 VARCHAR,
                                   list_date DATE, delist_date DATE, is_st TINYINT);
        CREATE TABLE kline_daily(ts_code VARCHAR, date DATE, close DOUBLE, adj_factor DOUBLE);
        CREATE TABLE dividend_events(ts_code VARCHAR, ex_date DATE, cash_dps DOUBLE, stk_div DOUBLE);
        CREATE TABLE valuation_daily(ts_code VARCHAR, date DATE, total_mv DOUBLE, ttm_yield_pct DOUBLE);
        CREATE TABLE holders_snapshot(ts_code VARCHAR, as_of_date DATE, holder_rank TINYINT,
                                      holder_name VARCHAR, share_nature VARCHAR,
                                      hold_ratio DOUBLE);
        CREATE VIEW kline_daily_hfq AS SELECT ts_code, date, (close * adj_factor) AS close
            FROM kline_daily;
    """)

    master = [
        ("sh.600101", "央企A", "B06", "2015-01-01", None, 0),   # 宝武 40% 国有 → 央企
        ("sh.600102", "地方B", "B06", "2015-01-01", None, 0),   # 深铁 27% 国有 → 地方国企
        ("sh.600103", "小持C", "B06", "2015-01-01", None, 0),   # 国家电网 5% → 央企但 <8%
        ("sh.600104", "变控D", "B06", "2015-01-01", None, 0),   # 2020 地方 50% → 2024 保利 40%
        ("sh.600105", "民企E", "B06", "2015-01-01", None, 0),   # 民营 → SOE 门即排
        # 数据标注缺口豁免对：BVI 控股（国有股证据缺失）
        ("sh.600938", "海油O", "B06", "2015-01-01", None, 0),   # override + 央企豁免 → 留
        ("sh.600066", "宇通L", "B06", "2015-01-01", None, 0),   # override 但地方国企 → 剔
    ]
    con.executemany("INSERT INTO stock_master VALUES (?,?,?,?,?,?)", master)

    # K线 300 bars（全过上市时长）
    import datetime as _dt
    kb = []
    for code, _n in [(c, 300) for c in (r[0] for r in master)]:
        cur = _dt.date(2026, 10, 21)
        dates = []
        while len(dates) < 300:
            if cur.weekday() < 5:
                dates.append(cur)
            cur -= _dt.timedelta(days=1)
        for dt in reversed(dates):
            kb.append((code, dt, 10.0, 1.0))
    con.executemany("INSERT INTO kline_daily VALUES (?,?,?,?)", kb)

    # 分红 2023/2024/2025 连续
    divs = []
    for code, *_ in master:
        for y in (2023, 2024, 2025):
            divs.append((code, f"{y}-07-15", 0.5, 0.0))
    con.executemany("INSERT INTO dividend_events VALUES (?,?,?,?)", divs)

    val = [(c, "2026-10-20", 300.0, 4.0) for c, *_ in master]
    con.executemany("INSERT INTO valuation_daily VALUES (?,?,?,?)", val)

    # 股东快照：2020-03-31 期 + 2026-09-30 期（PIT：as_of 前无快照 = 数据缺失
    # fail-closed 排除，所以 A/B 两期都有）
    holders = [
        ("sh.600101", "2020-03-31", 1, "中国宝武钢铁集团有限公司", "国有股", 40.0),
        ("sh.600101", "2026-09-30", 1, "中国宝武钢铁集团有限公司", "国有股", 40.0),
        ("sh.600102", "2020-03-31", 1, "深铁集团", "国有股", 27.0),
        ("sh.600102", "2026-09-30", 1, "深铁集团", "国有股", 27.0),
        ("sh.600103", "2026-09-30", 1, "国家电网公司", "国有股", 5.0),
        # 变控D：2020 = 地方国资 50%；2024 = 央企保利 40%
        ("sh.600104", "2020-03-31", 1, "某市属国有资本运营有限公司", "国有股", 50.0),
        ("sh.600104", "2024-03-31", 1, "中国保利集团有限公司", "国有股", 40.0),
        ("sh.600105", "2026-09-30", 1, "某民营资本公司", None, 45.0),
        # 标注缺口：BVI 控股（仿生产：top10 有小额国有股行过基础 SOE 门，
        # 但控股强度门不过 → 靠 soe_control_overrides 豁免）
        ("sh.600938", "2026-09-30", 1, "中国海洋石油(BVI)公司", "境外法人股", 60.5),
        ("sh.600938", "2026-09-30", 4, "中国石油天然气集团有限公司", "国有股", 0.4),
        # 宇通：郑州国资 12% 国有（过基础门+控股门，但=地方国企 → 央企门剔）
        ("sh.600066", "2026-09-30", 1, "郑州宇通集团有限公司", "境内法人股", 37.7),
        ("sh.600066", "2026-09-30", 2, "郑州市国资委", "国有股", 12.0),
    ]
    con.executemany("INSERT INTO holders_snapshot VALUES (?,?,?,?,?,?)", holders)
    con.close()


# ---------------------------------------------------------------------------
# central_soe 集合函数
# ---------------------------------------------------------------------------

def test_latest_central_set(tmp_path):
    db = tmp_path / "c.duckdb"
    _mk_db(db)
    con = duckdb.connect(str(db), read_only=True)
    s = central_soe_codes_latest(con, 8.0)
    con.close()
    # 101 宝武 40% ✓；104 最新=保利 40% ✓；102 深铁 ✗；103 国网 5%<8 ✗；105 无国有 ✗
    assert s == {"sh.600101", "sh.600104"}


def test_asof_pit_uses_earlier_snapshot(tmp_path):
    db = tmp_path / "c.duckdb"
    _mk_db(db)
    con = duckdb.connect(str(db), read_only=True)
    # 2022：变控D 最近快照 = 2020 地方国资 50% → 非央企
    s = central_soe_codes_asof(con, "2022-06-01", 8.0)
    con.close()
    assert "sh.600104" not in s
    assert "sh.600101" in s
    assert s == {"sh.600101"}


def test_ratio_threshold(tmp_path):
    db = tmp_path / "c.duckdb"
    _mk_db(db)
    con = duckdb.connect(str(db), read_only=True)
    s5 = central_soe_codes_latest(con, 5.0)
    con.close()
    # 阈值 5%：小持C 国网 5% 进集合
    assert "sh.600103" in s5
    assert "sh.600102" not in s5  # 地方国资无论阈值都不算央企


# ---------------------------------------------------------------------------
# 漏斗：central_only 开关
# ---------------------------------------------------------------------------

def _up(**kw):
    base = dict(industry_whitelist_csric2=("B06",), soe_keywords=("国务院", "国资委"),
                min_total_mv_yi=200.0, min_consecutive_div_years=3,
                listing_min_trading_days=250,
                min_soe_ratio_pct=8.0, min_soe_sum_ratio_pct=0.0)
    base.update(kw)
    return UniverseParams(**base)


def test_funnel_central_only_excludes_local(tmp_path):
    db = tmp_path / "f.duckdb"
    _mk_db(db)
    con = duckdb.connect(str(db), read_only=True)
    res_off = build_pool(con, _up(), run_year=RUN_YEAR)
    res_on = build_pool(con, _up(central_only=True), run_year=RUN_YEAR)
    con.close()
    # 关：SOE 门过 A/B/C/D/海油O/宇通L（E 民营排）；sum=0 → 合计档恒过 → 全进
    assert set(res_off.codes) == {
        "sh.600101", "sh.600102", "sh.600103", "sh.600104", "sh.600938", "sh.600066",
    }
    # 开：再剔 B（深铁=地方国资）+ C（央企但 5% < 8% 控股门槛）
    #     + 宇通L（郑州国资=地方国企）；海油O 保留（CENTRAL_OVERRIDE_CODES 央企豁免）
    assert set(res_on.codes) == {"sh.600101", "sh.600104", "sh.600938"}


# ---------------------------------------------------------------------------
# config 解析
# ---------------------------------------------------------------------------

def test_config_central_only_default_false():
    u = _universe_section({
        "universe": {
            "industry_whitelist_csric2": ["B06"], "soe_keywords": ["国资委"],
            "min_total_mv_yi": 200, "min_consecutive_div_years": 3,
            "listing_min_trading_days": 250, "backtest_positive": True,
            "backtest_window_years": 10, "min_ttm_yield_pct": 3,
            "price_min_yuan": 3, "price_max_yuan": 100,
        }
    })
    assert u.central_only is False


def test_config_central_only_true_and_non_bool():
    base = {
        "industry_whitelist_csric2": ["B06"], "soe_keywords": ["国资委"],
        "min_total_mv_yi": 200, "min_consecutive_div_years": 3,
        "listing_min_trading_days": 250, "backtest_positive": True,
        "backtest_window_years": 10, "min_ttm_yield_pct": 3,
        "price_min_yuan": 3, "price_max_yuan": 100,
    }
    u = _universe_section({"universe": {**base, "central_only": True}})
    assert u.central_only is True
    with pytest.raises(ConfigError):
        _universe_section({"universe": {**base, "central_only": "yes"}})
