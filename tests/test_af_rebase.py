# -*- coding: utf-8 -*-
"""test_af_rebase —— v7.0.16 adj_factor 基准对齐（af_rebase，v7.0.21 提升至根模块）单元测试。

背景：sh.600018 上港集团 5 年分位=0.5% 假信号（2026-10-26）。多源池逐日换源，
各适配器 af 是源私有归一化 → 换源日假断崖 → hfq/qfq view 断步。af_rebase 把
每股 af 段统一换算到最早段基准（只消除断步，不动真实除权跳变）。

纪律：零网络（内存 DuckDB + 合成序列）；判定/换算用与生产同一函数；
幂等性必须断言（重跑零写入）。
"""
from __future__ import annotations

import math

import pytest

duckdb = pytest.importorskip("duckdb")


@pytest.fixture()
def con():
    c = duckdb.connect(":memory:")
    from lake.ddl import init_schema
    init_schema(c)
    yield c
    c.close()


def add_rows(con, ts_code, rows, source="tencent"):
    """rows: [(date, close, af)]（升序）。"""
    for d, close, af in rows:
        con.execute(
            "INSERT INTO kline_daily (ts_code, date, close, adj_factor, source) "
            "VALUES (?, ?, ?, ?, ?)", [ts_code, d, close, af, source])


def add_event(con, ts_code, ex_date, cash_dps=0.05):
    con.execute(
        "INSERT INTO dividend_events (ts_code, ex_date, cash_dps, source) "
        "VALUES (?, ?, ?, 'test')", [ts_code, ex_date, cash_dps])


def af_series(con, ts_code):
    return [r[0] for r in con.execute(
        "SELECT adj_factor FROM kline_daily WHERE ts_code=? ORDER BY date",
        [ts_code]).fetchall()]


def hfq_series(con, ts_code):
    return [r[0] for r in con.execute(
        "SELECT close FROM kline_daily_hfq WHERE ts_code=? ORDER BY date",
        [ts_code]).fetchall()]


def assert_hfq_continuous(con, ts_code, max_step=0.02):
    """断步消除后：非事件日 hfq 跳变 ≈ raw 跳变；事件日（已知或大缺口）hfq 连续。"""
    from af_rebase import LARGE_DROP_RATIO
    events = {str(d)[:10] for (d,) in con.execute(
        "SELECT DISTINCT ex_date FROM dividend_events WHERE ts_code=?",
        [ts_code]).fetchall()}
    rows = con.execute(
        "SELECT date, close, LAG(close) OVER (ORDER BY date) "
        "FROM kline_daily_hfq WHERE ts_code=? ORDER BY date", [ts_code]).fetchall()
    raw = con.execute(
        "SELECT date, close, LAG(close) OVER (ORDER BY date) "
        "FROM kline_daily WHERE ts_code=? ORDER BY date", [ts_code]).fetchall()
    for (d, h, hp), (_, r, rp) in zip(rows, raw):
        # hfq 行 NULL（af 未填日）无连续性可言 → 跳过（修复后由无锚点填充补齐）
        if h is None or hp is None or rp is None or not hp or not rp:
            continue
        step_h, step_r = h / hp, r / rp
        if str(d)[:10] in events or step_r < 1.0 / LARGE_DROP_RATIO:
            # 事件日：hfq 应连续（真实事件被 af 跳变吸收）
            assert abs(math.log(step_h)) < max_step, \
                f"事件日 hfq 未连续：×{step_h:.4f}"
        else:
            assert abs(math.log(step_h / step_r)) < max_step, \
                f"hfq 断步残留：hfq×{step_h:.4f} vs raw×{step_r:.4f}"


# ---------------------------------------------------------------------------
# 1. 基准切换（无事件）：sh.600018 同构
# ---------------------------------------------------------------------------
def test_base_switch_no_event_rebased_and_idempotent(con):
    from af_rebase import rebase_stock_af
    rows = [("2026-09-10", 5.50, 16.987), ("2026-09-14", 5.48, 16.987),
            ("2026-09-15", 5.47, 16.987),           # tencent 段末
            ("2026-09-16", 5.46, 1.747253),        # sina 段首（基准 ÷9.72）
            ("2026-09-17", 5.44, 1.747253)]
    add_rows(con, "sh.600018", rows)
    res = rebase_stock_af(con, "sh.600018")
    assert len(res["jumps"]) == 1 and res["jumps"][0]["kind"] == "no_event"
    assert res["jumps"][0]["date"] == "2026-09-16"
    # 换算后全段 af 回到最早段基准（≈16.987）
    afs = af_series(con, "sh.600018")
    assert all(abs(a - 16.987) < 1e-6 for a in afs), afs
    # hfq 序列连续（断步消除）
    assert_hfq_continuous(con, "sh.600018")
    # 幂等：重跑零跳变零写入
    res2 = rebase_stock_af(con, "sh.600018")
    assert res2["jumps"] == [] and res2["rewritten"] == 0


# ---------------------------------------------------------------------------
# 2. 已知除权日同源自洽（不触发——除权日 af_move=price_ratio 精确成立）
# ---------------------------------------------------------------------------
def test_known_dividend_same_source_unchanged(con):
    from af_rebase import rebase_stock_af
    # 除息日 D=0.5：close 10.0→9.5；源 hfq 连续 ⇒ af 跳 10/9.5
    rows = [("2026-07-21", 10.0, 2.0), ("2026-07-22", 10.0, 2.0),
            ("2026-07-23", 9.5, 2.0 * 10.0 / 9.5), ("2026-07-24", 9.4, 2.0 * 10.0 / 9.5)]
    add_rows(con, "sh.600900", rows)
    add_event(con, "sh.600900", "2026-07-23", cash_dps=0.5)
    res = rebase_stock_af(con, "sh.600900")
    assert res["jumps"] == [] and res["rewritten"] == 0
    assert af_series(con, "sh.600900") == [2.0, 2.0, 2.0 * 10.0 / 9.5, 2.0 * 10.0 / 9.5]


# ---------------------------------------------------------------------------
# 3. 基准切换 + 除权日同一天（换算须吸收真实事件）
# ---------------------------------------------------------------------------
def test_base_switch_with_dividend(con):
    from af_rebase import rebase_stock_af
    # A 段 C_A=10：af=10×G；B 段 C_B=1.7：af=1.7×G；除息日 10→9.5
    g1 = 10.0 / 9.5
    rows = [("2026-07-22", 10.0, 10.0),
            ("2026-07-23", 9.5, 1.7 * g1),
            ("2026-07-24", 9.4, 1.7 * g1)]
    add_rows(con, "sh.600675", rows)
    add_event(con, "sh.600675", "2026-07-23", cash_dps=0.5)
    res = rebase_stock_af(con, "sh.600675")
    assert len(res["jumps"]) == 1 and res["jumps"][0]["kind"] == "event"
    # 换算后 B 段 af = 10×G(d1) = 10×(10/9.5)（与 A 段同基准）
    afs = af_series(con, "sh.600675")
    assert abs(afs[1] - 10.0 * g1) < 1e-6 and abs(afs[2] - 10.0 * g1) < 1e-6
    assert_hfq_continuous(con, "sh.600675")
    assert rebase_stock_af(con, "sh.600675")["jumps"] == []


# ---------------------------------------------------------------------------
# 4. 同源代码波动不误报（大涨日 af 恒定；大缺口日双假设都不偏离才放行）
# ---------------------------------------------------------------------------
def test_large_price_moves_same_source_unchanged(con):
    from af_rebase import rebase_stock_af
    # +10% 日（无事件、af 恒定）
    add_rows(con, "sh.600015", [("2026-01-05", 10.0, 5.0),
                                ("2026-01-06", 11.0, 5.0)])
    assert rebase_stock_af(con, "sh.600015")["jumps"] == []
    # 同源代码真实 -50% 崩盘（af 恒定）：suspected 双检验放行
    add_rows(con, "sh.600340", [("2026-01-05", 10.0, 10.0),
                                ("2026-01-06", 5.0, 10.0)])
    assert rebase_stock_af(con, "sh.600340")["jumps"] == []


# ---------------------------------------------------------------------------
# 5. 同源窗口重锚定（sh.600340 真实模式：sina→sina af 单日 ×3.448）
# ---------------------------------------------------------------------------
def test_same_source_reanchor_caught(con):
    from af_rebase import rebase_stock_af
    rows = [("2026-10-14", 6.1, 5.0), ("2026-10-15", 6.0, 5.0),
            ("2026-10-16", 6.0, 5.0 * 3.448),      # 重锚定
            ("2026-10-17", 6.0, 5.0 * 3.448 * 0.290)]  # 再次重锚回（≈5.0）
    add_rows(con, "sh.600340b", rows)
    res = rebase_stock_af(con, "sh.600340b")
    assert len(res["jumps"]) == 2
    afs = af_series(con, "sh.600340b")
    assert all(abs(a - 5.0) < 0.05 for a in afs), afs
    assert rebase_stock_af(con, "sh.600340b")["jumps"] == []


# ---------------------------------------------------------------------------
# 6. 基准切换 + 无记录大缺口（suspected 严禁按价格比盖章；核实回调后可精准对齐）
# ---------------------------------------------------------------------------
def test_base_switch_with_unrecorded_event(con):
    from af_rebase import rebase_stock_af
    # 10 送 10（分红表无记录）：close 10→5，真实 G(d1)=2。
    # C_A=10、C_B=2.5：af(d0)=10×1=10.0，af(d1)=2.5×2=5.0
    # price_ratio=2.0（≥1.08→suspected）；af_move=0.5 → 双假设均偏离
    rows = [("2026-05-20", 10.0, 10.0), ("2026-05-21", 5.0, 5.0)]
    add_rows(con, "sh.600708", rows)

    # ① 默认离线路径：严禁以价格比盖章改写源 af，仅记 suspected
    res = rebase_stock_af(con, "sh.600708")
    assert res["jumps"] == [] and res["rewritten"] == 0
    assert len(res["suspected"]) == 1 and res["suspected"][0]["kind"] == "suspected"
    assert af_series(con, "sh.600708") == [10.0, 5.0]

    # ② 在线/外部核实确认无除权（暴跌日换源）：按平坦对齐（10.0），完整保留 -50% 跌幅
    res_no = rebase_stock_af(
        con, "sh.600708",
        verify_event_fn=lambda code, d0, d1: {"confirmed": False})
    assert len(res_no["jumps"]) == 1 and res_no["jumps"][0]["kind"] == "verified_no_event"
    assert af_series(con, "sh.600708") == [10.0, 10.0]

    # ③ 重置后核实确认为 10 送 10（bonus=1.0）：按官方步长对齐至 20.0
    con.execute("DELETE FROM kline_daily WHERE ts_code='sh.600708'")
    add_rows(con, "sh.600708", rows)
    res_yes = rebase_stock_af(
        con, "sh.600708",
        verify_event_fn=lambda code, d0, d1: {"confirmed": True, "cash": 0.0, "bonus": 1.0})
    assert len(res_yes["jumps"]) == 1 and res_yes["jumps"][0]["kind"] == "verified_event"
    afs = af_series(con, "sh.600708")
    assert abs(afs[1] - 20.0) < 1e-6
    assert_hfq_continuous(con, "sh.600708")


# ---------------------------------------------------------------------------
# 7. find_spurious_jumps 全市场扫描（多股合成库）
# ---------------------------------------------------------------------------
def test_find_spurious_jumps(con):
    from af_rebase import find_spurious_jumps
    add_rows(con, "sh.000001", [("2026-10-20", 10.0, 3.0),
                                ("2026-10-21", 10.1, 0.3)])   # 假跳变
    add_rows(con, "sh.000002", [("2026-10-20", 10.0, 3.0),
                                ("2026-10-21", 10.1, 3.0)])   # 正常
    jumps = find_spurious_jumps(con)
    assert [j["ts_code"] for j in jumps] == ["sh.000001"]
    assert jumps[0]["date"] == "2026-10-21"


# ---------------------------------------------------------------------------
# 8. qfq view 端到端：600018 复刻——修复前 -90% 假跌，修复后连续
# ---------------------------------------------------------------------------
def test_qfq_view_continuity_600018_replica(con):
    from af_rebase import rebase_stock_af
    rows = [("2026-09-15", 5.47, 16.987161),
            ("2026-09-16", 5.46, 1.747253),
            ("2026-09-17", 5.44, 1.747253)]
    add_rows(con, "sh.600018", rows)
    qfq = lambda: [r[0] for r in con.execute(  # noqa: E731
        "SELECT close FROM kline_daily_qfq WHERE ts_code='sh.600018' "
        "ORDER BY date").fetchall()]
    before = qfq()
    assert before[1] / before[0] < 0.2, "前置断言：修复前应存在 ~-90% 假跌"
    rebase_stock_af(con, "sh.600018")
    after = qfq()
    assert abs(after[1] / after[0] - 5.46 / 5.47) < 1e-9
    assert all(a > 5.0 for a in after), after


# ---------------------------------------------------------------------------
# 9. NULL 盲点（2026-11-20 山煤国际事故）：af=NULL 行隔断 LAG 配对
# ---------------------------------------------------------------------------
def test_null_day_blindspot_jump_detected_and_rebased(con):
    """批量 af 失败日（整列 NULL）夹在两段之间：旧检测器的 LAG 只看到 NULL
    邻居 → 假跳变不可见，rebase_recent 日增护栏静默失效。修复后：
    11-12(af=7.5) → 11-13(af=NULL) → 11-16(af=11.8564, 无事件) 必须被
    find_spurious_jumps 捕获并 rebase 回最早段基准。"""
    from af_rebase import find_spurious_jumps, rebase_stock_af
    add_rows(con, "sh.600546", [
        ("2026-11-10", 11.01, 7.5),
        ("2026-11-12", 10.90, 7.5),
        ("2026-11-13", 10.90, None),       # 批量 af 失败日（NULL 洞）
        ("2026-11-16", 11.05, 11.8564),    # 新段（无除权事件）
        ("2026-11-18", 10.96, 11.8564),
    ])
    jumps = find_spurious_jumps(con)
    assert [j["ts_code"] for j in jumps] == ["sh.600546"], jumps
    assert jumps[0]["date"] == "2026-11-16" and jumps[0]["kind"] == "no_event"
    res = rebase_stock_af(con, "sh.600546")
    assert len(res["jumps"]) == 1 and res["jumps"][0]["date"] == "2026-11-16"
    # 对齐后 11-16/11-18 回到最早段基准 7.5（NULL 行保持 NULL，由无锚点填充补）
    afs = dict(con.execute(
        "SELECT CAST(date AS VARCHAR), adj_factor FROM kline_daily "
        "WHERE ts_code='sh.600546'").fetchall())
    assert abs(afs["2026-11-16"] - 7.5) < 1e-9 and afs["2026-11-13"] is None
    # hfq 连续（断步消除）
    assert_hfq_continuous(con, "sh.600546")
    # 幂等：重扫零跳变
    assert find_spurious_jumps(con) == []
