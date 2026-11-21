# -*- coding: utf-8 -*-
"""全面回归测试：覆盖 A 股数据湖、选股/回测策略、信号/持仓与 Web 安全的全部审查修复项。"""
from __future__ import annotations

import datetime as dt
import json
import math
import sys
from pathlib import Path
from types import SimpleNamespace

import duckdb
import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# 1. af_rebase: 除权日保留真实市场涨跌 + 停牌顺延除权 (d0, d1]
# ---------------------------------------------------------------------------

def test_af_rebase_preserves_exrights_day_market_move():
    import af_rebase

    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE kline_daily (
            ts_code VARCHAR, date DATE, close DOUBLE, adj_factor DOUBLE
        );
        CREATE TABLE dividend_events (
            ts_code VARCHAR, ex_date DATE, cash_dps DOUBLE, stk_div DOUBLE
        );
    """)
    # D0: close=10.0, af=1.0; D1 除息 0.5 元 → 理论除息价 9.5，理论 af_move = 10.0/9.5 ≈ 1.052632
    # 当日真实市场涨 +5% → close = 9.5 * 1.05 = 9.975；正确 af1 = 10.0/9.5
    af1 = 10.0 / 9.5
    con.executemany("INSERT INTO kline_daily VALUES (?,?,?,?)", [
        ("sh.600001", "2026-06-01", 10.0, 1.0),
        ("sh.600001", "2026-06-02", 9.975, af1),
    ])
    con.execute("INSERT INTO dividend_events VALUES ('sh.600001', '2026-06-02', 0.5, 0.0)")

    res = af_rebase.rebase_stock_af(con, "sh.600001")
    assert res["jumps"] == [], f"除权日正常 +5% 涨跌不应被误判为假跳变: {res}"
    assert res["rewritten"] == 0
    got_af1 = con.execute(
        "SELECT adj_factor FROM kline_daily WHERE date='2026-06-02'"
    ).fetchone()[0]
    assert got_af1 == pytest.approx(af1)
    con.close()


def test_af_rebase_recognizes_suspended_exrights_interval():
    import af_rebase

    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE kline_daily (
            ts_code VARCHAR, date DATE, close DOUBLE, adj_factor DOUBLE
        );
        CREATE TABLE dividend_events (
            ts_code VARCHAR, ex_date DATE, cash_dps DOUBLE, stk_div DOUBLE
        );
    """)
    # 06-01 后停牌至 06-05，除权日在停牌期 06-03（10 送 10 → stk_div=1.0，理论 af_move=2.0）
    con.executemany("INSERT INTO kline_daily VALUES (?,?,?,?)", [
        ("sh.600002", "2026-06-01", 20.0, 1.0),
        ("sh.600002", "2026-06-05", 10.4, 2.0),  # 复牌涨 4%
    ])
    con.execute("INSERT INTO dividend_events VALUES ('sh.600002', '2026-06-03', 0.0, 1.0)")

    jumps = af_rebase.find_spurious_jumps(con)
    assert jumps == [], f"停牌期除权 (d0, d1] 应被识别，不应误判假跳变: {jumps}"
    con.close()


# ---------------------------------------------------------------------------
# 2. em_batch_ingest: 除权日幂等性 + 同日多事件聚合 + load_ex_events 落库
# ---------------------------------------------------------------------------

def test_derive_af_batch_idempotent_and_multi_events():
    from lake.ingest import em_batch_ingest as emb

    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE kline_daily (
            ts_code VARCHAR, date DATE, close DOUBLE, adj_factor DOUBLE
        );
        CREATE TABLE dividend_events (
            ts_code VARCHAR, ex_date DATE, ann_date DATE, period VARCHAR,
            cash_dps DOUBLE, stk_div DOUBLE, source VARCHAR,
            fetched_at TIMESTAMP, data_version VARCHAR
        );
    """)
    # 假设当日 D='2026-07-01' 已写过一次（重跑场景），必须仍以 D-1 为基，不可二次连乘
    c0 = 20.0
    step = c0 * (1.0 + 0.5) / (c0 - 1.0)  # 同日两笔：现金 0.6+0.4=1.0，送转 0.2+0.3=0.5
    con.executemany("INSERT INTO kline_daily VALUES (?,?,?,?)", [
        ("sh.600010", "2026-06-30", c0, 2.0),
        ("sh.600010", "2026-07-01", 12.7, 2.0 * step),
    ])
    events = [
        {"ts_code": "sh.600010", "ex_date": "2026-07-01", "cash": 0.6, "bonus": 0.2,
         "has_allot": False, "plan": "10派6送2"},
        {"ts_code": "sh.600010", "ex_date": "2026-07-01", "cash": 0.4, "bonus": 0.3,
         "has_allot": False, "plan": "10派4转3"},
    ]
    out1 = emb.derive_af_batch(con, "2026-07-01", events)
    out2 = emb.derive_af_batch(con, "2026-07-01", events)
    assert out1["sh.600010"]["2026-07-01"] == pytest.approx(2.0 * step)
    assert out2["sh.600010"]["2026-07-01"] == pytest.approx(2.0 * step)

    # load_ex_events 幂等落库（每股口径；同日两笔事件各落一行，重跑仍为 2 行）
    n1 = emb.load_ex_events(con, "2026-07-01", events)
    n2 = emb.load_ex_events(con, "2026-07-01", events)
    assert n1 == 2 and n2 == 2
    row = con.execute(
        "SELECT SUM(cash_dps), SUM(stk_div) FROM dividend_events WHERE ts_code='sh.600010'"
    ).fetchone()
    assert row == pytest.approx((1.0, 0.5))
    con.close()


# ---------------------------------------------------------------------------
# 3. volume_is_shares / to_ts_code / no_anchor_af_range 停牌 / factors
# ---------------------------------------------------------------------------

def test_common_to_ts_code_bj92_and_volume_is_shares():
    from lake.ingest.common import to_ts_code
    from lake.ingest.tencent_ingest import load_t2, load_t7
    from martingale.selection import normalize_ts_code

    assert to_ts_code("920001") == "bj.920001"
    assert normalize_ts_code("920001") == "bj.920001"
    assert normalize_ts_code("600000.SH") == "sh.600000"
    assert normalize_ts_code("000001.sz") == "sz.000001"

    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE kline_daily (
            ts_code VARCHAR, date DATE, open DOUBLE, high DOUBLE, low DOUBLE,
            close DOUBLE, volume DOUBLE, amount DOUBLE, pct_chg DOUBLE,
            is_st INTEGER, preclose DOUBLE, adj_factor DOUBLE, source VARCHAR,
            fetched_at TIMESTAMP, data_version VARCHAR, conflict_src VARCHAR,
            PRIMARY KEY (ts_code, date)
        );
        CREATE TABLE index_daily (
            index_code VARCHAR, date DATE, open DOUBLE, high DOUBLE, low DOUBLE,
            close DOUBLE, volume DOUBLE, amount DOUBLE, pct_chg DOUBLE,
            source VARCHAR, fetched_at TIMESTAMP, data_version VARCHAR,
            PRIMARY KEY (index_code, date)
        );
    """)
    row = {"date": "2026-10-10", "open": 10.0, "high": 10.5, "low": 9.8,
           "close": 10.2, "volume": 123400, "amount": 1258680.0}
    load_t2(con, "sh.600001", [row], source="tencent", volume_is_shares=True)
    load_t7(con, "sh000001", [row], source="sina", volume_is_shares=True)
    v2 = con.execute("SELECT volume FROM kline_daily").fetchone()[0]
    v7 = con.execute("SELECT volume FROM index_daily").fetchone()[0]
    assert v2 == 123400.0
    assert v7 == 123400.0
    con.close()


def test_factors_valuation_pctiles_and_aligned_beta():
    from lake.factors import _beta, _valuation_pctiles

    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE valuation_daily (
            ts_code VARCHAR, date DATE, pe_ttm DOUBLE, pb DOUBLE, ttm_yield_pct DOUBLE
        );
    """)
    # 10 个估值日：pe_ttm 从 10 升到 19，但最后一日跌回 12 → 真实分位应是 30%（非旧 bug 的 100%）
    vals = [10, 11, 13, 14, 15, 16, 17, 18, 19, 12]
    d0 = dt.date(2026, 1, 1)
    for i, pe in enumerate(vals):
        con.execute("INSERT INTO valuation_daily VALUES (?,?,?,?,?)",
                    ["sh.600001", str(d0 + dt.timedelta(days=i)), float(pe), float(pe) / 10.0, 4.0])
    pcts = _valuation_pctiles(con, "sh.600001", as_of_date=str(d0 + dt.timedelta(days=9)))
    assert pcts["pe_pctile"] == pytest.approx(30.0)
    assert pcts["pb_pctile"] == pytest.approx(30.0)
    con.close()

    # _beta 按日期对齐（≥60 共同交易日）：中间缺失日（停牌）不造成错位平移
    dates = [str(dt.date(2026, 1, 1) + dt.timedelta(days=i)) for i in range(90)]
    rng = np.random.default_rng(42)
    idx_ret = {d: float(x) for d, x in zip(dates, rng.normal(0.0, 0.02, len(dates)))}
    # 股票精准跟随 2.0 * 指数，但故意缺失第 20~35 天（模拟停牌）
    stk_ret = {d: 2.0 * r for i, (d, r) in enumerate(idx_ret.items()) if not (20 <= i < 35)}
    b = _beta(stk_ret, idx_ret)
    assert b == pytest.approx(2.0, abs=1e-6)


# ---------------------------------------------------------------------------
# 4. universe & central_soe: 单边阈值不短路 + 最大国有股东为央企 + 剔地方国企 token
# ---------------------------------------------------------------------------

def test_central_soe_largest_state_holder_and_local_tokens_removed():
    from martingale.central_soe import CENTRAL_TOKENS, central_soe_codes_latest
    from martingale.config import UniverseParams
    from martingale.universe import build_pool

    for local_tok in ("首钢集团", "首钢总公司", "柳州钢铁", "海钢集团", "中国重汽"):
        assert local_tok not in CENTRAL_TOKENS

    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE stock_master(ts_code VARCHAR, name VARCHAR, industry_csric2 VARCHAR,
                                   list_date DATE, delist_date DATE, is_st TINYINT);
        CREATE TABLE kline_daily(ts_code VARCHAR, date DATE, close DOUBLE, adj_factor DOUBLE);
        CREATE TABLE dividend_events(ts_code VARCHAR, ex_date DATE, cash_dps DOUBLE, stk_div DOUBLE);
        CREATE TABLE valuation_daily(ts_code VARCHAR, date DATE, total_mv DOUBLE, ttm_yield_pct DOUBLE);
        CREATE TABLE holders_snapshot(ts_code VARCHAR, as_of_date DATE, holder_rank TINYINT,
                                      holder_name VARCHAR, share_nature VARCHAR, hold_ratio DOUBLE);
    """)
    # sh.600201: 地方国资 45% + 央企 10% → 最大国有股东是地方国资 → 应被 central_only 剔除
    # sh.600202: 央企 45% + 地方国资 10% → 最大国有股东是央企 → 应通过
    # sh.600203: 国有 5%（低于单边阈值 8%）→ 当 sum=0 时仍应被单边 8% 剔除
    # sh.600204: 2026 新上市 0 分红 → min_consecutive_div_years=3 应拒绝（不得自动放过）
    con.executemany("INSERT INTO stock_master VALUES (?,?,?,?,?,?)", [
        ("sh.600201", "地方控股央企参股", "B06", "2015-01-01", None, 0),
        ("sh.600202", "央企控股", "B06", "2015-01-01", None, 0),
        ("sh.600203", "弱国资", "B06", "2015-01-01", None, 0),
        ("sh.600204", "零分红新股", "B06", "2026-01-01", None, 0),
    ])
    d0 = dt.date(2025, 1, 1)
    kb = []
    for code in ("sh.600201", "sh.600202", "sh.600203", "sh.600204"):
        for i in range(300):
            kb.append((code, str(d0 + dt.timedelta(days=i)), 10.0, 1.0))
    con.executemany("INSERT INTO kline_daily VALUES (?,?,?,?)", kb)
    for code in ("sh.600201", "sh.600202", "sh.600203"):
        for y in (2023, 2024, 2025):
            con.execute("INSERT INTO dividend_events VALUES (?,?,0.5,0.0)", [code, f"{y}-07-01"])
    for code in ("sh.600201", "sh.600202", "sh.600203", "sh.600204"):
        con.execute("INSERT INTO valuation_daily VALUES (?,'2025-10-27',300.0,4.0)", [code])
    con.executemany("INSERT INTO holders_snapshot VALUES (?,?,?,?,?,?)", [
        ("sh.600201", "2025-09-30", 1, "某省国资委", "国有股", 45.0),
        ("sh.600201", "2025-09-30", 2, "中国宝武钢铁集团有限公司", "国有股", 10.0),
        ("sh.600202", "2025-09-30", 1, "中国宝武钢铁集团有限公司", "国有股", 45.0),
        ("sh.600202", "2025-09-30", 2, "某省国资委", "国有股", 10.0),
        ("sh.600203", "2025-09-30", 1, "国家电网公司", "国有股", 5.0),
        ("sh.600204", "2025-09-30", 1, "中国宝武钢铁集团有限公司", "国有股", 45.0),
    ])

    central = central_soe_codes_latest(con, 8.0)
    assert central == {"sh.600202", "sh.600204"}  # 600201 被拒，600203 < 8% 被拒

    up = UniverseParams(
        industry_whitelist_csric2=("B06",), soe_keywords=("国资委",),
        min_total_mv_yi=200.0, min_consecutive_div_years=3,
        listing_min_trading_days=250,
        min_soe_ratio_pct=8.0, min_soe_sum_ratio_pct=0.0, central_only=True,
    )
    res = build_pool(con, up, run_year=2026)
    assert res.codes == ["sh.600202"]
    con.close()


# ---------------------------------------------------------------------------
# 5. engine: aggregate_into 停牌不归零 + state_rows 送转扩股/since 校验
# ---------------------------------------------------------------------------

def test_engine_aggregate_mid_suspension_and_state_rows_since_split():
    from martingale.config import StrategyParams
    from martingale.engine import aggregate_into, state_rows

    # 5 天全局日历，该股缺第 3 天（中途停牌）和第 5 天（尾停牌）
    gdates = np.array([np.datetime64(f"2026-01-0{d}", "D").astype("datetime64[ns]")
                       for d in (1, 2, 3, 4, 5)])
    dates = np.array([np.datetime64(f"2026-01-0{d}", "D").astype("datetime64[ns]")
                      for d in (1, 2, 4)])
    g_v, g_c, g_o = np.zeros(5), np.zeros(5), np.zeros(5, dtype=np.int64)
    aggregate_into(gdates, g_v, g_c, g_o, dates,
                   np.array([100.0, 120.0, 130.0]),
                   np.array([100.0, 100.0, 100.0]),
                   np.array([1, 1, 1], dtype=np.int8))
    # 第 3 天停牌沿用第 2 天 120；第 4 天=130（不双倍）；第 5 天停牌沿用第 4 天 130
    assert list(g_v) == [100.0, 120.0, 120.0, 130.0, 130.0]
    assert list(g_o) == [1, 1, 1, 1, 1]

    # state_rows: 建仓日后送转扩股 + 空/非法 since_date 拒绝
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE stock_master(ts_code VARCHAR, name VARCHAR);
        CREATE TABLE kline_daily(ts_code VARCHAR, date DATE, close DOUBLE, adj_factor DOUBLE);
        CREATE TABLE dividend_events(ts_code VARCHAR, ex_date DATE, cash_dps DOUBLE, stk_div DOUBLE);
        CREATE VIEW kline_daily_hfq AS
            SELECT ts_code, date, close * adj_factor AS close FROM kline_daily;
    """)
    con.execute("INSERT INTO stock_master VALUES ('sh.600301', '扩股测试')")
    # 01-01 高点 100 → 01-02 跌到 80 买 100 股 → 01-03 10 送 10（stk_div=1.0），shares 应扩至 200
    con.executemany("INSERT INTO kline_daily VALUES (?,?,?,?)", [
        ("sh.600301", "2026-01-01", 100.0, 1.0),
        ("sh.600301", "2026-01-02", 80.0, 1.0),
        ("sh.600301", "2026-01-03", 40.0, 2.0),
    ])
    con.execute("INSERT INTO dividend_events VALUES ('sh.600301', '2026-01-03', 0.0, 1.0)")
    sp = StrategyParams(
        name="t", first_drawdown=0.15, add_step=0.10, lot_size=100,
        first_lots=1, add_lots_step=1, rearm_mode="bounce", rearm_pct=0.20,
        accounting="cash",
    )
    r = state_rows(sp, con, ["sh.600301"], since_map={"sh.600301": "2026-01-02"})["sh.600301"]
    assert r["shares_since"] == pytest.approx(200.0)
    # 空/非法 since_date 必须不产出 shares_since（绝不把全史买入算进去）
    r_bad = state_rows(sp, con, ["sh.600301"], since_map={"sh.600301": ""})["sh.600301"]
    assert "shares_since" not in r_bad
    con.close()


# ---------------------------------------------------------------------------
# 6. timing.first_timing_price_map: 严格满足 #{x <= P*} / W <= k（含 ties）
# ---------------------------------------------------------------------------

def test_first_timing_price_map_tie_and_non_divisible_exact():
    from martingale.timing import first_timing_price_map, live_pctile_map

    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE kline_daily (ts_code VARCHAR, date DATE, close DOUBLE)")
    con.execute("CREATE TABLE kline_daily_qfq (ts_code VARCHAR, date DATE, close DOUBLE)")
    # W=63（非整除），且在 40% 附近存在连续相同价格 25.0（ties）
    closes = [float(i) for i in range(1, 20)] + [25.0] * 15 + [float(i) for i in range(35, 64)]
    assert len(closes) == 63
    d0 = dt.date(2025, 6, 1)
    for i, c in enumerate(closes):
        ds = str(d0 + dt.timedelta(days=i))
        con.execute("INSERT INTO kline_daily VALUES ('sh.600401', ?, ?)", [ds, c])
        con.execute("INSERT INTO kline_daily_qfq VALUES ('sh.600401', ?, ?)", [ds, c])
    tp = SimpleNamespace(price_window_years=5, price_pctile_max=40.0)
    pstar = first_timing_price_map(con, ["sh.600401"], tp)["sh.600401"]
    assert pstar is not None
    # 验证翻线价本身在 live_pctile_map 下严格 <= 40.0%
    pct = live_pctile_map(con, ["sh.600401"], {"sh.600401": pstar}, tp)["sh.600401"]
    assert pct is not None and pct <= 40.0 + 1e-9
    con.close()


# ---------------------------------------------------------------------------
# 7. signals / touches / realtime / signal_touch
# ---------------------------------------------------------------------------

def test_signals_zero_price_and_realtime_dash_and_signal_touch_unwrap(monkeypatch, tmp_path):
    from martingale.config import StrategyParams
    from martingale.signals import classify_signal
    from martingale.touches import compute_touches
    from screener.data.realtime import _em_parse
    import scripts.signal_touch as st_mod

    sp = StrategyParams(
        name="t", first_drawdown=0.15, add_step=0.05, lot_size=100,
        first_lots=1, add_lots_step=1, rearm_mode="bounce", rearm_pct=0.20,
        accounting="cash",
    )
    # price <= 0 必须判 no_data，不得误触发 active
    c0 = classify_signal(0.0, 10.0, False, 0, sp)
    assert c0["status"] == "no_data" and c0["action"] is None

    # day_low <= 0 不得误判触碰
    rows = {"sh.600001": {"name": "测试", "in_pos": False, "k_level": 0,
                          "first_level_raw": 10.0, "next_add_price_raw": 10.0}}
    spot_zero = {"sh.600001": {"price": 10.5, "low": 0.0, "timestamp": "20261110150000"}}
    assert compute_touches(rows, spot_zero, sp) == []

    # 东财停牌 "-" 返回 None（而非 0.0）
    parsed = _em_parse([{"f12": "600001", "f13": 1, "f2": "-", "f16": "-", "f18": 10.0}])
    q = parsed.get("sh.600001")
    assert q is not None and q["price"] is None and q["low"] is None

    # signal_touch._up_read 必须解包 positions 字段
    up_file = tmp_path / "user_positions.json"
    up_file.write_text(json.dumps({
        "updated_at": "2026-11-10T00:00:00Z",
        "positions": {"sh.600001": {"shares": 200, "cost_price": 10.0}},
    }), encoding="utf-8")
    monkeypatch.setattr(st_mod, "USER_POSITIONS_PATH", str(up_file))
    assert list(st_mod._up_read().keys()) == ["sh.600001"]


# ---------------------------------------------------------------------------
# 8. Web 安全、账本校验、配置保留与 DuckDB 并发 RO 读取
# ---------------------------------------------------------------------------

def test_web_auth_security_and_ledger_validation(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import web.auth as wauth
    import web.app as wapp

    # 缺失 WEB_SESSION_SECRET 时同进程签名保持一致（不再每次随机失效），且支持含 "." 用户名
    monkeypatch.delenv("WEB_SESSION_SECRET", raising=False)
    monkeypatch.setattr(wauth, "_env_path", lambda: str(tmp_path / ".env_missing"))
    wauth._ENV_CACHE.clear()
    _, _, sec1 = wauth.load_auth_config()
    _, _, sec2 = wauth.load_auth_config()
    assert sec1 == sec2 and len(sec1) >= 32
    t1 = wauth.make_token("alice.bob", sec1)
    assert wauth.parse_token(t1, sec2) == "alice.bob"

    # 非 ASCII 密码返回 False（不抛 TypeError/500）
    assert wauth.check_credentials("admin", "密码错误", "admin", "secret") is False

    # /openapi.json 未登录返回 401
    client = TestClient(wapp.app)
    assert client.get("/openapi.json").status_code == 401
    assert client.get("/docs").status_code == 401

    # 账本校验：拒绝 NaN / 无效日历日期 2026-02-31
    monkeypatch.setattr(wapp, "USER_POSITIONS_PATH", tmp_path / "up.json")
    with pytest.raises(wapp.HTTPException) as e_nan:
        wapp.put_my_positions({"ts_code": "sh.600001", "shares": float("nan"), "cost_price": 10.0})
    assert e_nan.value.status_code == 400
    with pytest.raises(wapp.HTTPException) as e_date:
        wapp.put_my_positions({"ts_code": "sh.600001", "shares": 100, "cost_price": 10.0,
                               "since_date": "2026-02-31"})
    assert e_date.value.status_code == 400


def test_web_duckdb_concurrent_ro_no_config_conflict(tmp_path):
    """同进程同时通过 lake.conn.connect_existing(..., read_only=True) 与
    martingale.data.connect_readonly 打开同一 DuckDB 文件，不报 different configuration。"""
    from lake import conn as lconn
    from martingale import data as mdata

    db = str(tmp_path / "shared.duckdb")
    c0 = duckdb.connect(db)
    c0.execute("CREATE TABLE stock_master(ts_code VARCHAR)")
    c0.execute("INSERT INTO stock_master VALUES ('sh.600001')")
    c0.close()

    c1 = lconn.connect_existing(db, read_only=True)
    c2 = mdata.connect_readonly(db)
    try:
        assert c1.execute("SELECT COUNT(*) FROM stock_master").fetchone()[0] == 1
        assert c2.execute("SELECT COUNT(*) FROM stock_master").fetchone()[0] == 1
    finally:
        c1.close()
        c2.close()
