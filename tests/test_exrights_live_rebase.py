# -*- coding: utf-8 -*-
"""v7.0.32 除权日实时口径修正（Joel 2026-11-10 定案）。

修复对象：除权日盘中，湖末 bar 还是事件前 K 线（因子未更新），实时原始价
（已除权）对比触发价（= 后复权档位价 ÷ 事件前因子）→ 触发价比真实除权后口径
高一个分红幅度，与除权跌幅同幅 → 线近时分红一跌即假触线（假买入/加仓推送）。

修正（engine.pending_exrights_factor 单一因子，两处消费同口径）：
- state_rows(now=北京今日)：触发价 ÷ r_cum（r_cum = af_new/af_old）；
- live_pctile_map(now=北京今日)：现价 × r_cum（对齐事件前 qfq 窗口口径）。
回测/回放路径 now=None → 零修正（f6 基线不受影响——f6 走 run_stock 直连，
本模块改动不触达）。

纪律：全离线——tmp duckdb 四表（kline_daily/_hfq/_qfq + dividend_events +
stock_master），零真实网络、零触碰生产湖。
"""
import os
import sys
from types import SimpleNamespace

import duckdb
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from martingale import engine, timing  # noqa: E402
from martingale.config import StrategyParams  # noqa: E402
from martingale.signals import build_signals  # noqa: E402

CODE = "t.000001"
TODAY = "2026-11-10"          # 除权日（= 参照日 now）
LAST_BAR = "2026-11-09"       # 湖末 bar（事件前一日）

# 场景：无历史除权（af≡1，hfq=raw）；bounce 再武装到 110（80 低点反弹 ≥15%）
# → entry_high=110、未持仓（首仓线 99 从未触及：最低 100）
DATES = ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05",
         "2026-11-06", "2026-11-07", "2026-11-08", "2026-11-09"]
CLOSES = [80.0, 90.0, 110.0, 108.0, 106.0, 105.0, 102.0, 100.0]

CASH = 5.0                     # 除息 0.18 的 5% 分红量级：d=5（价 100 → 除权后 95）
R = 100.0 / (100.0 - CASH)     # = 20/19 ≈ 1.052632（精确值，断言用分数）


@pytest.fixture
def lake(tmp_path):
    db = tmp_path / "lake.duckdb"
    con = duckdb.connect(str(db))
    con.execute("""
        CREATE TABLE kline_daily (ts_code VARCHAR, date DATE, close DOUBLE,
                                  adj_factor DOUBLE, source VARCHAR);
        CREATE TABLE kline_daily_hfq (ts_code VARCHAR, date DATE, close DOUBLE);
        CREATE TABLE kline_daily_qfq (ts_code VARCHAR, date DATE, close DOUBLE);
        CREATE TABLE dividend_events (ts_code VARCHAR, ex_date DATE,
                                      ann_date DATE, period VARCHAR,
                                      cash_dps DOUBLE, stk_div DOUBLE);
        CREATE TABLE stock_master (ts_code VARCHAR, name VARCHAR);
    """)
    rows_k = ", ".join(f"('{CODE}', '{d}', {c}, 1.0, 'test')"
                       for d, c in zip(DATES, CLOSES))
    con.execute(f"INSERT INTO kline_daily VALUES {rows_k}")
    rows_h = ", ".join(f"('{CODE}', '{d}', {c})" for d, c in zip(DATES, CLOSES))
    con.execute(f"INSERT INTO kline_daily_hfq VALUES {rows_h}")
    con.execute(f"INSERT INTO kline_daily_qfq VALUES {rows_h}")
    con.execute(f"INSERT INTO stock_master VALUES ('{CODE}', '测试股')")
    con.close()
    return str(db)


def _conn(db):
    return duckdb.connect(db, read_only=True)


def _add_event(con_db: str, ex_date: str, cash: float, stk: float):
    con = duckdb.connect(con_db)
    con.execute("INSERT INTO dividend_events VALUES (?, ?, NULL, '2026', ?, ?)",
                [CODE, ex_date, cash, stk])
    con.close()


def _sp() -> StrategyParams:
    return StrategyParams(
        name="drawdown_martingale", lot_size=100, first_lots=100,
        first_drawdown=0.10, add_step=0.05, add_lots_step=10,
        rearm_mode="bounce", rearm_pct=0.15, accounting="cash")


# ---------------------------------------------------------------------------
# pending_exrights_factor：因子计算
# ---------------------------------------------------------------------------
def test_single_cash_exact(lake):
    _add_event(lake, TODAY, CASH, 0.0)
    con = _conn(lake)
    out = engine.pending_exrights_factor(con, [CODE], TODAY)
    assert out[CODE] == pytest.approx(R)
    con.close()


def test_no_pending_event_when_in_lake(lake):
    """ex_date == 末 bar 日（当日 K 线已入湖，af 已更新）→ 1.0（不重复修正）。"""
    _add_event(lake, LAST_BAR, CASH, 0.0)
    con = _conn(lake)
    assert engine.pending_exrights_factor(con, [CODE], TODAY)[CODE] == 1.0
    con.close()


def test_no_pending_event_future(lake):
    """ex_date > now（尚未生效）→ 1.0。"""
    _add_event(lake, "2026-11-11", CASH, 0.0)
    con = _conn(lake)
    assert engine.pending_exrights_factor(con, [CODE], TODAY)[CODE] == 1.0
    con.close()


def test_no_pending_event_past(lake):
    """ex_date < 末 bar 日（早已入湖）→ 1.0。"""
    _add_event(lake, "2026-11-05", CASH, 0.0)
    con = _conn(lake)
    assert engine.pending_exrights_factor(con, [CODE], TODAY)[CODE] == 1.0
    con.close()


def test_cash_and_stk_div(lake):
    """现金+送转：p_ex = (100−5)/(1+10/10) = 47.5 → r = 100/47.5。"""
    _add_event(lake, TODAY, CASH, 10.0)
    con = _conn(lake)
    out = engine.pending_exrights_factor(con, [CODE], TODAY)
    assert out[CODE] == pytest.approx(100.0 / 47.5)
    con.close()


def test_multi_events_sequential(lake):
    """多事件顺序模拟（罕见兜底）：11-10 分红 5 → 95；11-11 10 送 10 → 47.5。
    r_cum = (100/95) × (95/47.5) = 100/47.5。"""
    _add_event(lake, "2026-11-10", CASH, 0.0)
    _add_event(lake, "2026-11-11", 0.0, 10.0)
    con = _conn(lake)
    out = engine.pending_exrights_factor(con, [CODE], "2026-11-11")
    assert out[CODE] == pytest.approx(100.0 / 47.5)
    con.close()


def test_anomalous_cash_skipped(lake):
    """cash_dps ≥ 价（数据异常）→ 跳过该事件 → 1.0（不产生非法因子）。"""
    _add_event(lake, TODAY, 150.0, 0.0)
    con = _conn(lake)
    assert engine.pending_exrights_factor(con, [CODE], TODAY)[CODE] == 1.0
    con.close()


def test_now_none_and_empty(lake):
    _add_event(lake, TODAY, CASH, 0.0)
    con = _conn(lake)
    assert engine.pending_exrights_factor(con, [CODE], None)[CODE] == 1.0
    assert engine.pending_exrights_factor(con, [], TODAY) == {}
    con.close()


def test_code_without_kline(lake):
    con = _conn(lake)
    out = engine.pending_exrights_factor(con, ["t.999999"], TODAY)
    assert out["t.999999"] == 1.0
    con.close()


# ---------------------------------------------------------------------------
# state_rows：触发价口径（first_level_raw 路径；非持仓股，entry_high=110）
# ---------------------------------------------------------------------------
def test_state_rows_exrights_fix(lake):
    """核心场景（Joel 问的『除权后股价下降会不会触发买入信号』）：
    首仓线 hfq=99（110×0.90），线距现价 1%（< 分红 5%）→
    修正前 95≤99 假触线；修正后 95>94.05 不触线。当晚湖同步后自愈
    （ex_date 入湖 → 因子 1.0，本测试的 ex_date 场景不再 pending）。
    """
    _add_event(lake, TODAY, CASH, 0.0)
    con = _conn(lake)
    sp = _sp()
    # 不传 now（回测/旧行为）：99.0 / 1.0 = 99.0
    rows_old = engine.state_rows(sp, con, [CODE], include_buys=True)
    assert rows_old[CODE]["in_pos"] is False
    assert rows_old[CODE]["first_level_raw"] == pytest.approx(99.0)
    assert "exrights_rebase" not in rows_old[CODE]
    # 传 now（实时路径）：99.0 / (1.0 × 20/19) = 94.05
    rows_new = engine.state_rows(sp, con, [CODE], include_buys=True, now=TODAY)
    assert rows_new[CODE]["first_level_raw"] == pytest.approx(99.0 / R)
    assert rows_new[CODE]["exrights_rebase"] == pytest.approx(R, abs=1e-6)
    con.close()


def test_state_rows_no_event_zero_change(lake):
    """无除权事件：now 传与不传结果逐位一致（防过度修正）。"""
    con = _conn(lake)
    sp = _sp()
    rows_old = engine.state_rows(sp, con, [CODE], include_buys=True)
    rows_new = engine.state_rows(sp, con, [CODE], include_buys=True, now=TODAY)
    assert rows_old[CODE] == rows_new[CODE]
    con.close()


def test_state_rows_next_add_raw_also_fixed(lake):
    """持仓股路径（next_add_price_raw 同一 f 修正）：先让状态机建首仓。
    用更低的首仓线（first_drawdown=0.30 → 线 77 < 序列最低 100？否——需真触线）：
    构造跌穿：加一根 hfq 76 的末 bar 前段？——简化：断言 in_pos=True 时
    next_add_raw 也带 exrights_rebase（同 f 一处修正，结构性覆盖）。
    """
    # 追加一根深跌 bar 使首仓触发（hfq 76 ≤ 99 → 建仓 k=1）
    con = duckdb.connect(lake)
    con.execute("INSERT INTO kline_daily VALUES ('t.000002', '2026-11-02', 110.0, 1.0, 'test')")
    con.execute("INSERT INTO kline_daily_hfq VALUES ('t.000002', '2026-11-02', 110.0)")
    con.execute("INSERT INTO kline_daily_qfq VALUES ('t.000002', '2026-11-02', 110.0)")
    con.execute("INSERT INTO kline_daily VALUES ('t.000002', '2026-11-09', 76.0, 1.0, 'test')")
    con.execute("INSERT INTO kline_daily_hfq VALUES ('t.000002', '2026-11-09', 76.0)")
    con.execute("INSERT INTO kline_daily_qfq VALUES ('t.000002', '2026-11-09', 76.0)")
    con.execute("INSERT INTO stock_master VALUES ('t.000002', '测试股2')")
    con.execute("INSERT INTO dividend_events VALUES (?, ?, NULL, '2026', ?, ?)",
                ["t.000002", TODAY, 7.6, 0.0])  # 10% 分红（7.6/76）
    con.close()
    con2 = _conn(lake)
    sp = _sp()
    rows_old = engine.state_rows(sp, con2, ["t.000002"])
    rows_new = engine.state_rows(sp, con2, ["t.000002"], now=TODAY)
    assert rows_old["t.000002"]["in_pos"] is True
    assert rows_new["t.000002"]["in_pos"] is True
    r2 = 76.0 / (76.0 - 7.6)  # 10/9
    assert rows_old["t.000002"]["next_add_price_raw"] == pytest.approx(
        rows_new["t.000002"]["next_add_price_raw"] * r2)
    assert rows_new["t.000002"]["exrights_rebase"] == pytest.approx(r2, abs=1e-6)
    con2.close()


# ---------------------------------------------------------------------------
# build_signals：修正后的行为（Joel 场景端到端判定）
# ---------------------------------------------------------------------------
def test_build_signals_no_false_trigger_on_exday(lake):
    """端到端判定（Joel 问的『除权后股价下降会不会触发买入信号』）：
    首仓线 99，线距现价 1%（< 分红 5%）→ 除权日平盘开盘 95：
    修正前 95≤99 → 产出 active 首仓信号（假推送）；
    修正后 95>94.05 → 不产出信号（真线还差 0.95%）。"""
    _add_event(lake, TODAY, CASH, 0.0)
    con = _conn(lake)
    sp = _sp()
    row_old = engine.state_rows(sp, con, [CODE], include_buys=True)[CODE]
    row_new = engine.state_rows(sp, con, [CODE], include_buys=True, now=TODAY)[CODE]
    con.close()
    pos_old = {**row_old, "spot_price": 95.0}
    pos_new = {**row_new, "spot_price": 95.0}
    # 修正前：95 ≤ 99 → active → 信号列表含首仓买入（假信号）
    sig_old = build_signals([pos_old], sp)
    assert len(sig_old) == 1
    assert sig_old[0]["action"] == "buy"
    assert sig_old[0]["next_add_price"] == pytest.approx(99.0)
    # 修正后：95 > 94.05 → 不触线 → 无信号（当晚湖同步前不再误推）
    sig_new = build_signals([pos_new], sp)
    assert sig_new == []


# ---------------------------------------------------------------------------
# live_pctile_map：分位口径同因子修正
# ---------------------------------------------------------------------------
def test_live_pctile_exrights_fix(lake):
    """除权日现价 95（事件后）vs 事件前 qfq 窗口：
    修正前 cur=95 → 分位 25%（≤95 的 2 个：80/90）；
    修正后 cur=95×20/19=100 → 分位 37.5%（≤100 的 3 个：80/90/100）
    ——除权缺口不再系统性压低分位。"""
    _add_event(lake, TODAY, CASH, 0.0)
    tp = SimpleNamespace(price_window_years=5)
    prices = {CODE: 95.0}
    con = _conn(lake)
    old = timing.live_pctile_map(con, [CODE], prices, tp)
    new = timing.live_pctile_map(con, [CODE], prices, tp, now=TODAY)
    con.close()
    assert old[CODE] == pytest.approx(25.0)
    assert new[CODE] == pytest.approx(37.5)


def test_live_pctile_no_event_unchanged(lake):
    tp = SimpleNamespace(price_window_years=5)
    prices = {CODE: 103.0}
    con = _conn(lake)
    old = timing.live_pctile_map(con, [CODE], prices, tp)
    new = timing.live_pctile_map(con, [CODE], prices, tp, now=TODAY)
    con.close()
    assert old == new  # 无事件 → 零修正
