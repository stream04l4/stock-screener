# -*- coding: utf-8 -*-
"""PIT 审计回归单测（报告 R1 每个泄漏点 → "读不到未来数据"断言）。

fixture = ``bt_cache_fixture.build_pit_cache``：K线/复权因子/财报/分红都含
**T=2025-03-31 之后的未来数据**。每条用例断言 data_pit 在 T 日截断正确，
并在更晚日期（T_LATER）验证"未来数据确实存在、只是 T 日读不到"。

R1 泄漏点覆盖：
1. K线快照价（v2 rows[-1] = 最大泄漏点）→ ``kline_snapshot`` 取 date<=T 最后一根；
2. af1 技术因子窗口（v2 全历史重建后取尾）→ ``af1_window`` 先按 dates<=T 截断；
3. af1 复权因子（v2 用全历史除权事件）→ ``factor_at(T)`` 只依赖 ex_date<=T；
4. 基本面报告期（v2 命中即停不校验 pubDate）→ ``fundamentals`` 逐行 pubDate<=T；
5. 分红 TTM（v2 已安全，保持口径）→ ``dividend_records`` ex_date<=T；
6. ST 状态（v2 读"今天"的 isST）→ 取 T 日那根的 isST；
7. 上市天数（v2 len(全缓存) → 未上市新股误判老股）→ n_bars=count(date<=T)；
8. 股票池（幸存者偏差 + 晚上市剔除）→ ``universe(T)`` K线跨度近似。

另含：混合格式 K线坏行防御（23 只文件，列名+字段数校验，跳过并告警）。
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

import pytest

from backtest.data_pit import PitData
from bt_cache_fixture import T, T_LATER, build_pit_cache


@pytest.fixture()
def pit(tmp_path):
    facts = build_pit_cache(str(tmp_path / "cache"))
    return PitData(str(tmp_path / "cache"), ref_code=facts["ref_code"]), facts


# ---------------------------------------------------------------------------
# 1. K线快照价（R1 最大泄漏点）
# ---------------------------------------------------------------------------
def test_kline_snapshot_truncates_to_T(pit):
    """T 日快照 = date<=T 的最后一根（不是全缓存 rows[-1]）。"""
    p, f = pit
    snap = p.kline_snapshot(f["c1"], T)
    assert snap is not None
    # 2025-03-31 是第 90 天（1/1 起）→ close = 10+89 = 99.0；未来行(4~6月)读不到
    assert snap.date == "2025-03-31"
    assert snap.close_af3 == pytest.approx(99.0)
    # 更晚日期能看到未来 bar（证明 fixture 确实含 T 之后数据）
    snap_later = p.kline_snapshot(f["c1"], T_LATER)
    assert snap_later.date > "2025-03-31"
    assert snap_later.close_af3 > 99.0


def test_kline_snapshot_before_ipo_returns_none(pit):
    """T 早于首根K线（未上市）→ None。"""
    p, f = pit
    assert p.kline_snapshot(f["c3"], T) is None          # sh.600003 IPO=2025-04-01
    assert p.kline_snapshot(f["c3"], T_LATER) is not None


# ---------------------------------------------------------------------------
# 2. af1 技术因子窗口（R1：v2 全历史重建后取尾含未来）
# ---------------------------------------------------------------------------
def test_af1_window_truncates_to_T(pit):
    """af1_window(T) 只含 dates<=T 的 bar；窗口内复权台阶只用 ex_date<=T 的事件。"""
    p, f = pit
    dates, closes = p.af1_window(f["c1"], T, max_bars=5)
    assert all(d <= "2025-03-31" for d in dates), "窗口混入了 T 之后的 bar"
    assert len(dates) == 5 and dates[-1] == "2025-03-31"
    # 2025-02-15 除权后 F=1.5（2025-05-10 的 F=2.0 在 T 之后，不得计入）
    # close(3/31)=99 → af1 = 99×1.5 = 148.5
    assert closes[-1] == pytest.approx(99.0 * 1.5)
    # 更晚日期（数据范围内）：F=2.0 生效（2025-06-30 close=190 → af1=380）
    _, closes_later = p.af1_window(f["c1"], date(2025, 6, 30), max_bars=1)
    assert closes_later[-1] == pytest.approx(190.0 * 2.0)


# ---------------------------------------------------------------------------
# 3. af1 复权因子（R1：v2 用全历史除权事件）
# ---------------------------------------------------------------------------
def test_factor_at_truncates_to_T(pit):
    """factor_at(T) 只依赖 ex_date<=T 的事件（新除权不改历史）。"""
    p, f = pit
    assert p.factor_at(f["c1"], T) == pytest.approx(1.5)     # 2025-05-10 的 2.0 读不到
    assert p.factor_at(f["c1"], date(2025, 6, 30)) == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# 4. 基本面报告期（R1：v2 命中即停不校验 pubDate）
# ---------------------------------------------------------------------------
def test_fundamentals_pubdate_le_T(pit):
    """pubDate<=T 的财报可见；pubDate>T 的未来财报读不到（记缺失）。"""
    p, f = pit
    # sh.600001：profit 2024Q4 pubDate=2025-03-28 <= T → 可见
    fund1 = p.fundamentals(f["c1"], T, annual_year=2024)
    assert fund1["profit_cur"] is not None
    assert fund1["profit_cur"]["pubDate"] == "2025-03-28"
    assert fund1["profit_cur"]["roeAvg"] == pytest.approx(0.10)

    # sh.600002：profit 2024Q4 pubDate=2025-04-15 > T → **不可见**（PIT 红线）
    fund2 = p.fundamentals(f["c2"], T, annual_year=2024)
    assert fund2["profit_cur"] is None

    # 更晚日期：sh.600002 的财报可见（证明数据存在、只是 T 日读不到）
    fund2_later = p.fundamentals(f["c2"], T_LATER, annual_year=2024)
    assert fund2_later["profit_cur"] is not None


def test_fundamentals_missing_file_is_none(pit):
    """缓存中不存在的 (表,年,季) → None（未披露/历史缺口，不报错）。"""
    p, f = pit
    fund = p.fundamentals(f["c1"], T, annual_year=2020)
    assert all(v is None for v in fund.values())


# ---------------------------------------------------------------------------
# 5. 分红（R1：v2 已安全，保持 ex_date<=T 口径）
# ---------------------------------------------------------------------------
def test_dividends_exdate_le_T(pit):
    """dividend_records(T) 只含 ex_date<=T；T 之后的除权读不到。"""
    p, f = pit
    recs = p.dividend_records(f["c1"], T)
    ex_dates = sorted(r["dividOperateDate"] for r in recs)
    assert ex_dates == ["2025-01-07"], "T 之后的除权(2025-04-10)泄漏了"
    # 更晚日期：两笔都可见
    recs_later = p.dividend_records(f["c1"], T_LATER)
    assert sorted(r["dividOperateDate"] for r in recs_later) == ["2025-01-07", "2025-04-10"]


def test_dividend_year_cache_no_stale_empty(pit):
    """回归：先问 T（当年无 ex<=T 之后的记录）再问更晚日期，不得返回脏空缓存。

    （(code,year) 分年缓存修复前的 bug：dividend_records(c1, T) 只读 2023/24/25
    并缓存 → 后续年份组合变化时误读旧结果。）
    """
    p, f = pit
    assert len(p.dividend_records(f["c1"], date(2025, 1, 8))) == 1   # 只有 1/7
    assert len(p.dividend_records(f["c1"], T_LATER)) == 2            # 4/10 也可见


# ---------------------------------------------------------------------------
# 6. ST 状态（R1：v2 读"今天"的 isST）
# ---------------------------------------------------------------------------
def test_open_column_reconstructed_to_af1(tmp_path):
    """回归：缓存 OHLC 均为不复权(af3) → open 必须用同一 F(t) 重建为 af1。

    历史 bug：open 按原值使用、close 已复权 → T+1 开盘成交把"未复权 open ×
    已复权权重"混算（实测 sh.600007 单日 NAV 虚增 +77%）。
    """
    from screener.data.cache import DiskCache
    cache = DiskCache(str(tmp_path))
    code = "sh.600888"
    # 2025-01-01~03-31 close=10（无除权）；2025-04-01 起 F=2.0：close=20、open=20
    def _ds(a, b):
        out = []
        d = a
        while d <= b:
            out.append(d.isoformat())
            d += timedelta(days=1)
        return out

    d1 = _ds(date(2025, 1, 1), date(2025, 3, 31))
    d2 = _ds(date(2025, 4, 1), date(2025, 6, 30))
    rows = ["date", "code", "open", "high", "low", "close", "preclose",
            "volume", "amount", "turn", "tradestatus", "pctChg", "peTTM",
            "pbMRQ", "isST"]

    def mk(d, c):
        return [d, code, str(c), str(c * 1.01), str(c * 0.99), str(c),
                str(c), "1000", "10000", "0.5", "1", "0.0", "10", "1", "0"]

    cache.put(f"kline_af3_{code}", rows, [mk(d, 10.0) for d in d1] +
              [mk(d, 20.0) for d in d2])
    # 除权事件：2025-04-01 F=2.0（close 台阶 10→20 与之匹配）
    cache.put(f"adjfactor_{code}",
              ["code", "dividOperateDate", "foreAdjustFactor",
               "backAdjustFactor", "adjustFactor"],
              [[code, "2025-04-01", "0.5", "2.0", "2.0"]])

    p = PitData(str(tmp_path))
    # 2025-04-01 的 open：原值 20（不复权）× F(2025-04-01)=2.0 → af1=40
    px_open, used_open = p.exec_price(code, "2025-04-01", True)
    assert used_open is True
    assert px_open == pytest.approx(40.0), \
        f"open 未按 F(t) 重建为 af1（得到 {px_open}，期望 40）"
    # close 同口径：20×2=40 → 开盘价==收盘价（合成数据无日内波动）
    px_close, _ = p.exec_price(code, "2025-04-01", False)
    assert px_close == pytest.approx(40.0)


def test_st_status_at_T(tmp_path):
    """isST 取 T 日那根（历史列），非缓存末根的当前值。"""
    from datetime import timedelta
    from screener.data.cache import DiskCache
    cache = DiskCache(str(tmp_path))
    code = "sh.600999"
    # 2025-01-01~03-31 isST=1；2025-04-01 起 isST=0（末根=今天非 ST）
    rows = []
    d = date(2025, 1, 1)
    while d <= date(2025, 6, 30):
        st = "1" if d <= T else "0"
        rows.append([d.isoformat(), code, "10.0", st, "1"])
        d += timedelta(days=1)
    cache.put(f"kline_af3_{code}", ["date", "code", "close", "isST", "tradestatus"], rows)
    p = PitData(str(tmp_path), ref_code=code)
    snap_t = p.kline_snapshot(code, T)
    snap_later = p.kline_snapshot(code, T_LATER)
    assert snap_t is not None and snap_t.is_st == 1          # T 日那根 ST
    assert snap_later is not None and snap_later.is_st == 0  # 之后摘帽


# ---------------------------------------------------------------------------
# 7. 上市天数（R1：v2 len(全缓存) → 未上市新股被误判老股）
# ---------------------------------------------------------------------------
def test_listing_days_count_le_T(pit):
    """n_bars = count(date<=T)；T 日未上市的股票 n_bars=0/None。"""
    p, f = pit
    snap = p.kline_snapshot(f["c1"], T)
    assert snap.n_bars == (T - date(2025, 1, 1)).days + 1   # 90
    # sh.600003：IPO 在 T 之后 → T 日快照 None（上市天数=0，硬剔除）
    assert p.kline_snapshot(f["c3"], T) is None
    snap_later = p.kline_snapshot(f["c3"], date(2025, 6, 30))
    assert snap_later.n_bars == (date(2025, 6, 30) - date(2025, 4, 1)).days + 1


# ---------------------------------------------------------------------------
# 8. 股票池（PIT 近似 + 幸存者偏差标注）
# ---------------------------------------------------------------------------
def test_universe_pit_membership(pit):
    """universe(T)：first_date<=T<=last_date；晚上市股 T 日不在池、之后在池。"""
    p, f = pit
    uni_T = set(p.universe(T, prefixes=("sh.60",)))
    assert f["c1"] in uni_T and f["c2"] in uni_T
    assert f["c3"] not in uni_T, "IPO 在 T 之后的股票不得出现在 T 日池"
    uni_later = set(p.universe(date(2025, 6, 30), prefixes=("sh.60",)))
    assert f["c3"] in uni_later


# ---------------------------------------------------------------------------
# 停牌冻结估值（PIT：绝不用 >day 的价格）
# ---------------------------------------------------------------------------
def test_last_close_on_or_before_frozen(pit):
    """停牌股 ≤day 最后可得收盘价；复牌后的未来价不得用于历史 day。"""
    p, f = pit
    c5 = f["c5"]
    # 2025-03-15（停牌中）：≤该日最后非空 close = 2025-03-09 那根
    v_susp = p.last_close_on_or_before(c5, "2025-03-15")
    # 3/1..3/9 共 9 根（i=0..8）→ close = 50+8 = 58.0
    assert v_susp == pytest.approx(58.0)
    # 复牌后 day：取当日价（非未来）
    v_after = p.last_close_on_or_before(c5, "2025-04-01")
    i_apr1 = (date(2025, 4, 1) - date(2025, 3, 1)).days
    assert v_after == pytest.approx(50 + i_apr1)


def test_exec_price_suspended_returns_none(pit):
    """停牌日 exec_price → None（模拟器顺延逻辑的触发条件）。"""
    p, f = pit
    px, used_open = p.exec_price(f["c5"], "2025-03-15", use_open=True)
    assert px is None and used_open is False


# ---------------------------------------------------------------------------
# 混合格式 K线坏行防御（R6 风险 5：23 只文件 15字段头+5字段追加尾）
# ---------------------------------------------------------------------------
def test_mixed_format_kline_bad_rows_skipped(tmp_path, caplog):
    """列名定位 + 字段数校验：与表头字段数不符的坏行跳过并告警，不崩溃。"""
    from screener.data.cache import DiskCache
    cache = DiskCache(str(tmp_path))
    code = "sh.600888"
    # 5 字段表头；其中一行是 15 字段混合格式（坏行）→ 应被跳过
    rows = [
        ["2025-01-01", code, "10.0", "0", "1"],
        ["2025-01-02", code, "10.5", "0", "1", "9.8", "10.6", "9.7", "10.4",
         "1000", "9000", "0.1", "1", "1.1", "5.0", "0"],   # 坏行（15 字段）
        ["2025-01-03", code, "11.0", "0", "1"],
    ]
    cache.put(f"kline_af3_{code}", ["date", "code", "close", "isST", "tradestatus"], rows)
    p = PitData(str(tmp_path), ref_code=code)
    with caplog.at_level(logging.WARNING, logger="backtest.data_pit"):
        kl = p.kline(code)
    assert kl is not None
    assert kl.dates == ["2025-01-01", "2025-01-03"], "坏行未跳过"
    assert any("字段数与表头" in m for m in caplog.messages), "坏行未告警"


def test_kline_missing_header_skipped(tmp_path, caplog):
    """表头缺 date/close → 该股整体跳过（返回 None），不崩溃。"""
    from screener.data.cache import DiskCache
    cache = DiskCache(str(tmp_path))
    code = "sh.600777"
    cache.put(f"kline_af3_{code}", ["date", "code", "closeX"],
              [["2025-01-01", code, "10.0"]])
    p = PitData(str(tmp_path), ref_code=code)
    with caplog.at_level(logging.WARNING, logger="backtest.data_pit"):
        assert p.kline(code) is None
