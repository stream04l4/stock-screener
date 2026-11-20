# -*- coding: utf-8 -*-
"""test_af_no_anchor —— af 无锚点填充（铁律 2026-11-20：同一参数同一源）单元测试。

背景：kline_daily.adj_factor 只允许一个规范锚点源（tushare 官方 af）。
批量 af 失败/缺失时的兜底=**无锚点填充**：沿用湖内现有 level + 已知除权
事件的官方除权参考价步长 c0·(1+B)/(c0−C)；绝不引入他源私有归一化
（sina/tdx hfq÷raw，2026-10-26 sh.600018 与 2026-11-20 山煤国际事故同根因），
绝不写 NULL 洞（NULL 行曾隔断 af_rebase 的 LAG 检测对 → 护栏失明）。

纪律：零网络（内存 DuckDB + 合成序列）；判定/填充用与生产同一函数。
"""
from __future__ import annotations

import pytest

duckdb = pytest.importorskip("duckdb")


@pytest.fixture()
def con():
    c = duckdb.connect(":memory:")
    from lake.ddl import init_schema
    init_schema(c)
    yield c
    c.close()


def add_kline(con, ts_code, rows):
    """rows: [(date, close, af)]（升序）。"""
    for d, close, af in rows:
        con.execute(
            "INSERT INTO kline_daily (ts_code, date, close, adj_factor, source) "
            "VALUES (?, ?, ?, ?, 'test')", [ts_code, d, close, af])


def add_event(con, ts_code, ex_date, cash_dps=None, stk_div=None):
    con.execute(
        "INSERT INTO dividend_events (ts_code, ex_date, cash_dps, stk_div, source) "
        "VALUES (?, ?, ?, ?, 'test')", [ts_code, ex_date, cash_dps, stk_div])


def no_anchor(con, ts_code, start, end, dates=None):
    from lake.ingest.common import no_anchor_af_range
    return no_anchor_af_range(con, ts_code, start, end, dates=dates)


def _approx(actual, expected):
    assert actual is not None, f"期望 {expected}，实际 None（fail-closed？）"
    assert abs(actual - expected) < 1e-9, (actual, expected)


# ---------------------------------------------------------------------------
# 1. 无事件缺口：逐日沿用前一日 level（与 tushare 停牌日 af 不变一致）
# ---------------------------------------------------------------------------
def test_no_event_gap_forward_fills_level(con):
    add_kline(con, "sh.600001", [
        ("2026-11-12", 10.9, 7.5),
        ("2026-11-13", 10.9, None),
        ("2026-11-16", 11.05, None),
        ("2026-11-17", 10.86, None),
    ])
    out = no_anchor(con, "sh.600001", "2026-11-13", "2026-11-17")
    assert out == {"2026-11-13": 7.5, "2026-11-16": 7.5, "2026-11-17": 7.5}, out


# ---------------------------------------------------------------------------
# 2. 现金分红事件日：官方除权参考价步长 c0/(c0−C)
# ---------------------------------------------------------------------------
def test_cash_dividend_event_step(con):
    # c0=12.62，C=0.355 → 步长 = 12.62/12.265
    add_kline(con, "sh.600002", [
        ("2026-07-15", 12.62, 7.3182),
        ("2026-07-16", 12.03, None),
        ("2026-07-17", 11.90, None),
    ])
    add_event(con, "sh.600002", "2026-07-16", cash_dps=0.355)
    step = 12.62 / (12.62 - 0.355)
    out = no_anchor(con, "sh.600002", "2026-07-16", "2026-07-17")
    _approx(out["2026-07-16"], 7.3182 * step)
    _approx(out["2026-07-17"], 7.3182 * step)   # 事件后沿用


# ---------------------------------------------------------------------------
# 3. 送股事件：步长 c0·(1+B)/(c0−C)
# ---------------------------------------------------------------------------
def test_bonus_event_step(con):
    # c0=10.0，C=0.2，B=0.5（10 送 5 转 + 每股派 0.2）→ 10×1.5/9.8
    add_kline(con, "sh.600003", [
        ("2026-06-30", 10.0, 2.0),
        ("2026-07-01", 6.4, None),
    ])
    add_event(con, "sh.600003", "2026-07-01", cash_dps=0.2, stk_div=0.5)
    step = 10.0 * 1.5 / (10.0 - 0.2)
    out = no_anchor(con, "sh.600003", "2026-07-01", "2026-07-01")
    _approx(out["2026-07-01"], 2.0 * step)


# ---------------------------------------------------------------------------
# 4. 事件但金额/送股均缺 → fail-closed（None 传播，绝不沿用旧 level）
# ---------------------------------------------------------------------------
def test_ambiguous_event_fail_closed(con):
    add_kline(con, "sh.600004", [
        ("2026-05-20", 10.0, 3.0),
        ("2026-05-21", 5.0, None),
        ("2026-05-22", 4.9, None),
    ])
    add_event(con, "sh.600004", "2026-05-21", cash_dps=None, stk_div=None)
    out = no_anchor(con, "sh.600004", "2026-05-21", "2026-05-22")
    assert out == {"2026-05-21": None, "2026-05-22": None}, out


# ---------------------------------------------------------------------------
# 5. 无历史 level（新股首日）→ fail-closed None（等 tushare 官方批量）
# ---------------------------------------------------------------------------
def test_no_history_returns_empty_or_none(con):
    add_kline(con, "sh.600005", [("2026-01-02", 10.0, None)])
    # 有 K 线行但无历史 level → fail-closed None（等 tushare 官方批量）
    assert no_anchor(con, "sh.600005", "2026-01-02", "2026-01-02") == \
        {"2026-01-02": None}
    con.execute("DELETE FROM kline_daily")
    # 完全无行 → 空
    assert no_anchor(con, "sh.600005", "2026-01-02", "2026-01-02") == {}


# ---------------------------------------------------------------------------
# 6. 窗口内已有 af 的行：湖内现值权威（锚点续接，不被重算）
# ---------------------------------------------------------------------------
def test_existing_af_row_anchors_con(con):
    add_kline(con, "sh.600006", [
        ("2026-03-02", 9.0, 4.0),
        ("2026-03-03", 9.1, 5.0),     # 已有 af（权威 level）
        ("2026-03-04", 9.2, None),
    ])
    out = no_anchor(con, "sh.600006", "2026-03-03", "2026-03-04")
    assert out == {"2026-03-03": 5.0, "2026-03-04": 5.0}, out


# ---------------------------------------------------------------------------
# 7. 多日缺口 + 中间事件日：level 逐段推进
# ---------------------------------------------------------------------------
def test_multi_day_gap_with_middle_event(con):
    add_kline(con, "sh.600007", [
        ("2026-09-30", 8.0, 1.0),
        ("2026-10-08", 8.1, None),
        ("2026-10-09", 7.9, None),     # 除权日（C=0.4，c0=8.1）
        ("2026-10-12", 7.8, None),
    ])
    add_event(con, "sh.600007", "2026-10-09", cash_dps=0.4)
    step = 8.1 / (8.1 - 0.4)
    out = no_anchor(con, "sh.600007", "2026-10-08", "2026-10-12")
    _approx(out["2026-10-08"], 1.0)          # 事件前：沿用
    _approx(out["2026-10-09"], step)         # 事件日：步长
    _approx(out["2026-10-12"], step)         # 事件后：沿用


# ---------------------------------------------------------------------------
# 8. forward_fill_af：{d: None} = fail-closed 重置（2026-11-20 铁律语义）
# ---------------------------------------------------------------------------
def test_forward_fill_af_none_resets_level(con):
    from lake.ingest.common import forward_fill_af
    dates = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08",
             "2026-01-09"]
    # 旧语义不变：{事件日: af>0} 前向填充
    out = forward_fill_af(dates, {"2026-01-06": 5.0})
    assert out == {"2026-01-05": None, "2026-01-06": 5.0, "2026-01-07": 5.0,
                   "2026-01-08": 5.0, "2026-01-09": 5.0}, out
    # 新语义：{d: None} 自 d 起 level 显式重置（绝不沿用旧 level）
    out2 = forward_fill_af(dates, {"2026-01-06": 5.0, "2026-01-08": None,
                                   "2026-01-09": 6.0})
    assert out2 == {"2026-01-05": None, "2026-01-06": 5.0, "2026-01-07": 5.0,
                    "2026-01-08": None, "2026-01-09": 6.0}, out2


# ---------------------------------------------------------------------------
# 9. 端到端：no_anchor_af_range 填充 + load_t2 落库 → hfq/qfq view 连续
# ---------------------------------------------------------------------------
def test_no_anchor_fill_end_to_end_view_continuity(con):
    add_kline(con, "sh.600008", [
        ("2026-11-12", 10.9, 7.5),
        ("2026-11-13", 10.9, None),
        ("2026-11-16", 11.05, None),
        ("2026-11-18", 10.96, None),
    ])
    from lake.ingest.tencent_ingest import load_t2
    fill = no_anchor(con, "sh.600008", "2026-11-13", "2026-11-18")
    assert all(v == 7.5 for v in fill.values())
    # 按填充值 upsert（模拟日增写入路径）
    for d, af in fill.items():
        con.execute(
            "UPDATE kline_daily SET adj_factor=? WHERE ts_code='sh.600008' "
            "AND date=?", [af, d])
    # qfq view：11-13/11-16/11-18 与 11-12 同 scale → 无假跳变
    qfq = {str(d)[:10]: c for d, c in con.execute(
        "SELECT date, close FROM kline_daily_qfq WHERE ts_code='sh.600008'"
        " ORDER BY date").fetchall()}
    assert abs(qfq["2026-11-13"] - 10.9) < 1e-9
    assert abs(qfq["2026-11-16"] - 11.05) < 1e-9
    # 11-16/11-12 qfq 比值 = raw 比值（无假断崖）
    assert abs(qfq["2026-11-16"] / qfq["2026-11-12"] - 11.05 / 10.9) < 1e-9
    # load_t2 走同一契约（None 重置语义不破坏已有行）
    n = load_t2(con, "sh.600008",
                [{"date": "2026-11-18", "close": 10.96, "volume": 100}],
                adj_map={"2026-11-18": 7.5})
    assert n == 1


# ---------------------------------------------------------------------------
# 10. 目标日不在湖中（日增场景：缺口日正在写入）→ 显式 dates 填充
# ---------------------------------------------------------------------------
def test_target_dates_not_in_lake(con):
    add_kline(con, "sh.600009", [
        ("2026-09-17", 10.0, 1.0),
    ])
    out = no_anchor(con, "sh.600009", "2026-09-18", "2026-09-18",
                    dates=["2026-09-18"])
    assert out == {"2026-09-18": 1.0}, out
    # 显式 dates 缺省行为兼容：湖内已有行日期
    out2 = no_anchor(con, "sh.600009", "2026-09-17", "2026-09-17")
    assert out2 == {"2026-09-17": 1.0}, out2
