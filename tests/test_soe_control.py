# -*- coding: utf-8 -*-
"""v8.0.8 国企控股强度门 + 行业全放开（["*"]）测试（合成湖，零网络、零生产表写入）。

Joel 2026-11-11 定案：行业白名单全放开 + 门槛提档（3.5%/30%）+ 控股强度门
保证无伪国企。本文件覆盖：
1. ["*"] 全放开语义（无行业过滤）vs 枚举白名单；["*","B06"] 混用 = 非法；
2. 控股强度门：最大单一国有占比 ≥ min_soe_ratio_pct **或** top10 国有合计 ≥
   min_soe_sum_ratio_pct **或** soe_control_overrides 豁免 → 保留；
   两键均 0/缺省 = 门关闭（v8.0.0 行为）；
3. exclude_codes 硬剔除（门之上的排除）；
4. config.py 可选键：缺省 = 默认（门关闭）；格式非法 → ConfigError。
"""
import sys
from pathlib import Path

import duckdb
import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from martingale.config import ConfigError, UniverseParams, load_config  # noqa: E402
from martingale.universe import build_pool  # noqa: E402

RUN_YEAR = 2026

# 8 只合成股（全部过市值/上市时长/连续分红，差异集中在 行业/SOE/占比/排除/豁免）：
#   sh.600101 强国企   B06 单一国有 40%          → 门：过（≥8）
#   sh.600102 多平台   B06 国有 7.5%×4（合计30，单一7.5）→ 门：仅合计档过（≥30）
#   sh.600103 弱国企   B06 国有 5%（合计5）       → 门：不过（伪国企型，证金小持股）
#   sh.600104 排除股   B06 单一国有 50%          → exclude_codes 硬剔除
#   sh.600105 豁免股   B06 国有 3%（数据标注缺口，大股东境外法人 60%）→ 仅豁免保留
#   sh.600106 关键词股 B06 关键词 SOE、无国有性质标注      → 占比 (0,0)，门不过（设计内从严）
#   sh.600107 白名单外 B07→C99 单一国有 40%       → 仅 ["*"] 全放开时进池
#   sz.000108 白名单外 C99 单一国有 40%           → 同上（验证非 B06 行业也放开）
_CODES = ["sh.600101", "sh.600102", "sh.600103", "sh.600104",
          "sh.600105", "sh.600106", "sh.600107", "sz.000108"]
IND = {"sh.600107": "C99", "sz.000108": "C99"}
NAME = {c: f"股{i}" for i, c in enumerate(_CODES)}


def _up(**kw):
    base = dict(industry_whitelist_csric2=("B06",), soe_keywords=("国务院", "国资委"),
                min_total_mv_yi=200.0, min_consecutive_div_years=3,
                listing_min_trading_days=250)
    base.update(kw)
    return UniverseParams(**base)


def _mk_db(path: Path) -> None:
    con = duckdb.connect(str(path))
    con.execute("""
        CREATE TABLE stock_master(ts_code VARCHAR, name VARCHAR, industry_csric2 VARCHAR,
                                   list_date DATE, delist_date DATE, is_st TINYINT);
        CREATE TABLE kline_daily(ts_code VARCHAR, date DATE, close DOUBLE, adj_factor DOUBLE);
        CREATE TABLE dividend_events(ts_code VARCHAR, ex_date DATE, cash_dps DOUBLE, stk_div DOUBLE);
        CREATE TABLE valuation_daily(ts_code VARCHAR, date DATE, total_mv DOUBLE, ttm_yield_pct DOUBLE);
        CREATE TABLE holders_snapshot(ts_code VARCHAR, as_of_date DATE, holder_rank TINYINT,
                                      holder_name VARCHAR, share_nature VARCHAR, hold_ratio DOUBLE);
    """)
    con.executemany(
        "INSERT INTO stock_master VALUES (?,?,?,?,?,?)",
        [(c, NAME[c], IND.get(c, "B06"), "2015-01-01", None, 0) for c in _CODES])
    # K线 300 bars（全部 ≥250）
    import datetime as _dt
    kb = []
    cur = _dt.date(2026, 10, 21)
    dates = []
    while len(dates) < 300:
        if cur.weekday() < 5:
            dates.append(cur)
        cur -= _dt.timedelta(days=1)
    for c in _CODES:
        kb.extend((c, dt, 10.0, 1.0) for dt in reversed(dates))
    con.executemany("INSERT INTO kline_daily VALUES (?,?,?,?)", kb)
    # 连续分红 2023/2024/2025（老股规则全过）
    con.executemany("INSERT INTO dividend_events VALUES (?,?,?,?)",
                    [(c, f"{y}-07-15", 0.5, 0.0) for c in _CODES for y in (2023, 2024, 2025)])
    con.executemany("INSERT INTO valuation_daily VALUES (?,?,?,?)",
                    [(c, "2026-10-20", 300.0, 4.0) for c in _CODES])
    # 股东（as_of 2026-09-30；hold_ratio = 百分数口径）
    holders = [
        # 强国企：单一 40
        ("sh.600101", "2026-09-30", 1, "某国有资本公司", "国有股", 40.0),
        # 多平台：7.5×4（合计 30，单一 7.5 < 8 → 仅合计档可过）
        ("sh.600102", "2026-09-30", 1, "平台A", "国有股", 7.5),
        ("sh.600102", "2026-09-30", 2, "平台B", "国有股", 7.5),
        ("sh.600102", "2026-09-30", 3, "平台C", "国有股", 7.5),
        ("sh.600102", "2026-09-30", 4, "平台D", "国有股", 7.5),
        # 弱国企：仅 5（证金型小持股）
        ("sh.600103", "2026-09-30", 1, "某证金公司", "国有股", 5.0),
        # 排除股：单一 50
        ("sh.600104", "2026-09-30", 1, "某国有资本公司", "国有股", 50.0),
        # 豁免股：国有 3 + 大股东境外法人 60（标注缺口）
        ("sh.600105", "2026-09-30", 1, "某境外控股公司", "境外法人股", 60.0),
        ("sh.600105", "2026-09-30", 2, "某国资小股东", "国有股", 3.0),
        # 关键词股：仅关键词命中，无国有性质标注
        ("sh.600106", "2026-09-30", 1, "国务院国有资产监督管理委员会", None, 30.0),
        # 白名单外两只
        ("sh.600107", "2026-09-30", 1, "某国有资本公司", "国有股", 40.0),
        ("sz.000108", "2026-09-30", 1, "某国有资本公司", "国有股", 40.0),
    ]
    con.executemany("INSERT INTO holders_snapshot VALUES (?,?,?,?,?,?)", holders)
    con.close()


def test_gate_off_is_v8_behavior(tmp_path):
    """两键缺省（=0）→ 门关闭：弱国企/关键词股照旧进池（v8.0.0 语义）。"""
    db = tmp_path / "t.duckdb"
    _mk_db(db)
    con = duckdb.connect(str(db), read_only=True)
    res = build_pool(con, _up(), run_year=RUN_YEAR)
    con.close()
    assert set(res.codes) == {"sh.600101", "sh.600102", "sh.600103",
                              "sh.600104", "sh.600105", "sh.600106"}
    assert res.funnel["soe_ratio"] == res.funnel["soe"]  # 门关闭 = 计数同口径


def test_gate_excludes_weak_and_keyword(tmp_path):
    """门开启（8/30）：弱国企（5<8 且 5<30）、豁免股（3，无豁免配置）、关键词股
    （无国有占比）均被剔；多平台股凭合计 30 过门。"""
    db = tmp_path / "t.duckdb"
    _mk_db(db)
    con = duckdb.connect(str(db), read_only=True)
    res = build_pool(con, _up(min_soe_ratio_pct=8.0, min_soe_sum_ratio_pct=30.0),
                     run_year=RUN_YEAR)
    con.close()
    assert set(res.codes) == {"sh.600101", "sh.600102", "sh.600104"}
    assert res.funnel["soe"] == 6            # 交后口径不变
    assert res.funnel["soe_ratio"] == 3      # 门后：排 弱国企 + 豁免股(未配) + 关键词股


def test_gate_sum_branch_only(tmp_path):
    """多平台股（单一 7.5 < 8）仅凭合计 30 ≥ 30 过门；把合计线提到 50 则出局。"""
    db = tmp_path / "t.duckdb"
    _mk_db(db)
    con = duckdb.connect(str(db), read_only=True)
    res = build_pool(con, _up(min_soe_ratio_pct=8.0, min_soe_sum_ratio_pct=30.0),
                     run_year=RUN_YEAR)
    assert "sh.600102" in set(res.codes)
    con.close()

    con = duckdb.connect(str(db), read_only=True)
    res2 = build_pool(con, _up(min_soe_ratio_pct=8.0, min_soe_sum_ratio_pct=50.0),
                      run_year=RUN_YEAR)
    con.close()
    assert "sh.600102" not in set(res2.codes)


def test_exclude_codes_hard_drop(tmp_path):
    """exclude_codes 在门之上硬剔除：50% 强国企也被排。"""
    db = tmp_path / "t.duckdb"
    _mk_db(db)
    con = duckdb.connect(str(db), read_only=True)
    res = build_pool(con, _up(min_soe_ratio_pct=8.0, min_soe_sum_ratio_pct=30.0,
                              exclude_codes=("sh.600104",)), run_year=RUN_YEAR)
    con.close()
    assert "sh.600104" not in set(res.codes)


def test_override_rescue(tmp_path):
    """soe_control_overrides 豁免标注缺口股（3% < 8 且 3% < 30，仅豁免保留）。"""
    db = tmp_path / "t.duckdb"
    _mk_db(db)
    con = duckdb.connect(str(db), read_only=True)
    res = build_pool(con, _up(min_soe_ratio_pct=8.0, min_soe_sum_ratio_pct=30.0,
                              soe_control_overrides={"sh.600105": "测试豁免"}),
                     run_year=RUN_YEAR)
    con.close()
    assert "sh.600105" in set(res.codes)


def test_star_whitelist_opens_all_industries(tmp_path):
    """["*"] = 全放开：C99 行业的强国企也进池；枚举白名单则不进。"""
    db = tmp_path / "t.duckdb"
    _mk_db(db)
    con = duckdb.connect(str(db), read_only=True)
    res = build_pool(con, _up(min_soe_ratio_pct=8.0, min_soe_sum_ratio_pct=30.0),
                     run_year=RUN_YEAR)
    con.close()
    assert "sh.600107" not in set(res.codes) and "sz.000108" not in set(res.codes)

    con = duckdb.connect(str(db), read_only=True)
    res_star = build_pool(con, _up(industry_whitelist_csric2=("*",),
                                   min_soe_ratio_pct=8.0, min_soe_sum_ratio_pct=30.0),
                          run_year=RUN_YEAR)
    con.close()
    assert "sh.600107" in set(res_star.codes) and "sz.000108" in set(res_star.codes)


def _write_cfg(tmp_path: Path, universe: dict) -> Path:
    cfg = {
        "strategy": {"name": "drawdown_martingale", "lot_size": 100, "first_lots": 1,
                     "add_lots_step": 1, "add_step_pct": 5, "rearm_mode": "bounce",
                     "rearm_pct": 15, "accounting": "cash"},
        "universe": universe,
        "timing": {"price_window_years": 5, "price_pctile_max": 40,
                   "first_drawdown_pct": 15},
    }
    p = tmp_path / "s.yaml"
    p.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return p


def _base_universe(**kw):
    u = {"industry_whitelist_csric2": ["*"], "soe_keywords": ["国务院", "国资委"],
         "min_total_mv_yi": 200, "min_consecutive_div_years": 5,
         "listing_min_trading_days": 250, "backtest_positive": True,
         "backtest_window_years": 10, "backtest_min_return_pct": 30,
         "min_ttm_yield_pct": 3.5, "price_min_yuan": 2, "price_max_yuan": 100}
    u.update(kw)
    return u


def test_config_optional_keys_default(tmp_path):
    """老 yaml 无 v8.0.8 新键 → 默认（门关闭、名单空、豁免空）。"""
    _, up, _ = load_config(str(_write_cfg(tmp_path, _base_universe())))
    assert up.industry_whitelist_csric2 == ("*",)
    assert up.min_soe_ratio_pct == 0.0 and up.min_soe_sum_ratio_pct == 0.0
    assert up.exclude_codes == () and up.soe_control_overrides == {}


def test_config_new_keys_parse(tmp_path):
    _, up, _ = load_config(str(_write_cfg(tmp_path, _base_universe(
        min_soe_ratio_pct=8, min_soe_sum_ratio_pct=30,
        exclude_codes=["sh.603766"],
        soe_control_overrides={"sh.600938": "测试理由"}))))
    assert up.min_soe_ratio_pct == 8.0 and up.min_soe_sum_ratio_pct == 30.0
    assert up.exclude_codes == ("sh.603766",)
    assert up.soe_control_overrides == {"sh.600938": "测试理由"}


def test_config_star_mixed_with_codes_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load_config(str(_write_cfg(
            tmp_path, _base_universe(industry_whitelist_csric2=["*", "B06"]))))


def test_config_bad_exclude_code_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load_config(str(_write_cfg(
            tmp_path, _base_universe(exclude_codes=["603766"]))))


def test_config_bad_override_key_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load_config(str(_write_cfg(
            tmp_path, _base_universe(soe_control_overrides={"海油": "理由"}))))
