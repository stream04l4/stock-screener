# -*- coding: utf-8 -*-
"""Phase B 迁移脚本离线单测（零 live）：窗口→键推导 / 缓存盘点 / 断点续跑。"""
from __future__ import annotations

import json
import os
from datetime import date

import pytest

from backtest.migrate_history import (Checkpoint, QueryItem, annual_years_for_window,
                                      build_plan, current_pool, dividend_years_for_window,
                                      inventory_cache, monthly_rebalance_days,
                                      needed_fund_keys)


# ---------------------------------------------------------------------------
# 窗口 → 键推导（与 R4 _r4_budget.py 同口径）
# ---------------------------------------------------------------------------
def test_annual_years_5y_window():
    ys = annual_years_for_window(date(2021, 9, 30), date(2026, 9, 4))
    # 2021-09(m>4)→2020 ... 2026-08→2025；含 2021-02 前? 无（窗口从 9 月开始）
    assert ys == [2020, 2021, 2022, 2023, 2024, 2025]


def test_annual_years_january_uses_y_minus_2():
    # 1-4 月调仓期 → y-2（年报未披露完）
    ys = annual_years_for_window(date(2025, 1, 31), date(2025, 6, 30))
    assert 2023 in ys   # 2025-01~04 → 2023
    assert 2024 in ys   # 2025-05~06 → 2024


def test_needed_fund_keys_access_pattern():
    """annual_year=Y：profit(Y,Y-1,Y-2 Q4)+growth(Y)+balance(Y,Y-1)+cashflow(Y)。"""
    need = needed_fund_keys([2024])
    assert ("profit", 2024, 4) in need
    assert ("profit", 2023, 4) in need
    assert ("profit", 2022, 4) in need
    assert ("growth", 2024, 4) in need
    assert ("balance", 2024, 4) in need
    assert ("balance", 2023, 4) in need
    assert ("cashflow", 2024, 4) in need
    assert ("growth", 2023, 4) not in need      # growth 仅当期
    assert ("cashflow", 2023, 4) not in need    # cashflow 仅当期
    assert ("balance", 2022, 4) not in need     # balance 仅 Y/Y-1


def test_dividend_years_ttm_union():
    ys = dividend_years_for_window(date(2021, 9, 30), date(2026, 9, 4))
    assert ys == [2020, 2021, 2022, 2023, 2024, 2025, 2026]


def test_monthly_rebalance_days_last_trading_day():
    cal = ["2025-06-27", "2025-06-30", "2025-07-01", "2025-07-31"]
    assert monthly_rebalance_days(cal) == ["2025-06-30", "2025-07-31"]


# ---------------------------------------------------------------------------
# 缓存盘点 + 计划构建（合成缓存，零 live）
# ---------------------------------------------------------------------------
def _mk_cache(tmp_path, codes=("sh.600000",), fund_keys=(), div_years=()):
    """最小合成缓存：K线/复权因子 + 指定财报键/分红年。"""
    d = tmp_path / "cache"
    d.mkdir(parents=True, exist_ok=True)
    for c in codes:
        (d / f"kline_af3_{c}.csv").write_text(
            "stock-screener-cache-v1\ndate,code,close,isST,tradestatus\n"
            "2025-01-01," + c + ",10.0,0,1\n", encoding="utf-8")
        (d / f"adjfactor_{c}.csv").write_text(
            "stock-screener-cache-v1\ncode,dividOperateDate,foreAdjustFactor,"
            "backAdjustFactor,adjustFactor\n" + c + ",2025-01-01,1.0,1.0,1.0\n",
            encoding="utf-8")
    for t, y, q in fund_keys:
        (d / f"{t}_{codes[0]}_{y}_{q}.csv").write_text(   # 稳定键无 Q（make_cache_name(kind,code,year,quarter)）
            "stock-screener-cache-v1\ncode,pubDate,statDate,roeAvg\n"
            + codes[0] + f",2025-04-30,{y}-12-31,10.0\n", encoding="utf-8")
    for y in div_years:
        (d / f"dividend_{codes[0]}_{y}.csv").write_text(
            "stock-screener-cache-v1\ncode,dividOperateDate,dividCashPsBeforeTax\n"
            + codes[0] + f",{y}-07-01,0.5\n", encoding="utf-8")
    return str(d)


def test_inventory_and_plan_missing_only(tmp_path):
    """计划只含**缺失**键：已缓存的财报/分红年不出现。"""
    cache = _mk_cache(
        tmp_path,
        fund_keys=[("profit", 2024, 4), ("growth", 2024, 4)],
        div_years=[2025])
    inv = inventory_cache(cache)
    assert (2024, 4) in inv["fund"]["profit"]
    assert (2024, 4) in inv["fund"]["growth"]
    assert 2025 in inv["div_years"]

    plan = build_plan(cache, date(2025, 1, 31), date(2025, 6, 30),
                      include_delisted=False)
    keys = {(it.kind, it.year, it.quarter) for it in plan.items if it.kind != "allstock"}
    # profit 2024Q4 已缓存 → 不在计划；growth 2024Q4 已缓存 → 不在计划
    assert ("profit", 2024, 4) not in keys
    assert ("growth", 2024, 4) not in keys
    # profit 2023Q4（Y-1）缺失 → 在计划
    assert ("profit", 2023, 4) in keys
    # dividend 2025 已缓存 → 不在；2024 缺失 → 在
    divs = {it.year for it in plan.items if it.kind == "dividend"}
    assert 2025 not in divs and 2024 in divs


def test_checkpoint_resume_skips_done(tmp_path):
    """断点续跑：已完成键重跑时跳过（状态文件 JSON 原子写）。"""
    p = tmp_path / "ckpt.json"
    ck = Checkpoint(str(p))
    assert not os.path.exists(p) or ck.done == set()

    ck.mark_done("profit_sh.600000_2024_4")   # 稳定键 = QueryItem.key（无 Q）
    ck.executed_total = 1
    ck.save()

    # 重新加载 → done 集合恢复
    ck2 = Checkpoint(str(p))
    assert "profit_sh.600000_2024_4" in ck2.done
    assert ck2.executed_total == 1

    # pending 过滤（与 run_migration 同逻辑）
    items = [QueryItem(kind="profit", code="sh.600000", year=2024, quarter=4),
             QueryItem(kind="growth", code="sh.600000", year=2024, quarter=4)]
    pending = [it for it in items if it.key not in ck2.done]
    assert len(pending) == 1 and pending[0].kind == "growth"

    # JSON 结构完整（updated_at/done/executed_total）
    doc = json.loads(p.read_text(encoding="utf-8"))
    assert set(doc) >= {"updated_at", "done", "executed_total"}


def test_checkpoint_corrupt_file_fails_clean(tmp_path):
    """状态文件损坏 → 抛错而非静默清零（防丢进度）。"""
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        Checkpoint(str(p))


# ---------------------------------------------------------------------------
# _run_one 键一致性 + 空结果语义（fake bs，零 live）
# ---------------------------------------------------------------------------
class _FakeRS:
    """模拟 baostock ResultData：error_code/fields/get_row_data/next。"""

    def __init__(self, fields, rows):
        self.error_code = "0"
        self.error_msg = ""
        self.fields = fields
        self._rows = rows
        self._i = -1

    def next(self):
        self._i += 1
        return self._i < len(self._rows)

    def get_row_data(self):
        return self._rows[self._i]


class _FakeBS:
    """按 kind 返回预置行的假 baostock（只验证缓存键/布局，不触网）。"""

    def __init__(self, by_kind=None):
        self.by_kind = by_kind or {}
        self.calls = []

    def _mk(self, fields, rows):
        return _FakeRS(fields, rows)

    def query_profit_data(self, **kw):
        self.calls.append(("profit", kw))
        f = ["code", "pubDate", "statDate", "roeAvg"]
        r = self.by_kind.get("profit")
        return self._mk(f, [r] if r else [])

    def query_dividend_data(self, **kw):
        self.calls.append(("dividend", kw))
        f = ["code", "dividOperateDate", "dividCashPsBeforeTax"]
        r = self.by_kind.get("dividend")
        return self._mk(f, [r] if r else [])

    def query_history_k_data_plus(self, **kw):
        self.calls.append(("kline", kw))
        f = ["date", "code", "close", "isST", "tradestatus"]
        r = self.by_kind.get("kline")
        return self._mk(f, [r] if r else [])

    def query_adjust_factor(self, **kw):
        self.calls.append(("adjfactor", kw))
        f = ["code", "dividOperateDate", "foreAdjustFactor", "backAdjustFactor",
             "adjustFactor"]
        r = self.by_kind.get("adjfactor")
        return self._mk(f, [r] if r else [])

    def query_all_stock(self, **kw):
        self.calls.append(("allstock", kw))
        f = ["code", "tradeStatus", "code_name"]
        r = self.by_kind.get("allstock")
        return self._mk(f, [r] if r else [])


def test_run_one_writes_exact_stable_key(tmp_path):
    """_run_one 写缓存文件名 == it.key + '.csv'（断点/缺哪补哪的判据）。"""
    from backtest.migrate_history import _run_one
    from screener.data.cache import DiskCache

    cache = DiskCache(str(tmp_path))
    bs = _FakeBS({
        "profit": ["sh.600000", "2025-04-30", "2024-12-31", "12.5"],
        "dividend": ["sh.600000", "2024-07-01", "0.5"],
        "kline": ["2025-01-01", "sh.600000", "10.0", "0", "1"],
        "adjfactor": ["sh.600000", "2024-07-01", "0.9", "1.1", "1.1"],
        "allstock": ["sh.600000", "1", "浦发银行"],
    })

    cases = [
        (QueryItem(kind="profit", code="sh.600000", year=2024, quarter=4),
         "profit_sh.600000_2024_4"),
        (QueryItem(kind="dividend", code="sh.600000", year=2024),
         "dividend_sh.600000_2024"),
        (QueryItem(kind="kline_af3", code="sh.600000", start="2021-01-01", end="2026-09-04"),
         "kline_af3_sh.600000"),
        (QueryItem(kind="adjfactor", code="sh.600000", start="2021-01-01", end="2026-09-04"),
         "adjfactor_sh.600000"),
        (QueryItem(kind="allstock", code="2025-06-30"), "allstock_2025-06-30"),
    ]
    for it, key in cases:
        assert _run_one(bs, cache, it) is True
        assert (tmp_path / f"{key}.csv").exists(), f"键不一致：期望 {key}.csv"
        # 内容可读且首行哨兵（DiskCache.get 可解析）
        hit = cache.get(key)
        assert hit and hit["rows"], f"{key} 缓存不可读"


def test_run_one_empty_semantics(tmp_path):
    """空结果语义：K线/因子 → 'empty'（不写文件，标记完成）；分红空年**写空缓存**（返回 True，
    稳定键"已查过"→ 不再重复查询，回测层读空=无分红）；allstock → False（重试）。"""
    from backtest.migrate_history import _run_one
    from screener.data.cache import DiskCache

    cache = DiskCache(str(tmp_path))
    bs_empty = _FakeBS({})   # 所有查询返回空行
    assert _run_one(bs_empty, cache, QueryItem(kind="profit", code="sh.600000",
                                               year=2019, quarter=4)) == "empty"
    assert _run_one(bs_empty, cache, QueryItem(kind="kline_af3", code="sz.000001",
                                               start="2021-01-01", end="2026-09-04")) == "empty"
    # 分红空年：写空缓存文件（防重复查询）→ True，且回测层可读（rows=∅）
    assert _run_one(bs_empty, cache, QueryItem(kind="dividend", code="sh.600000",
                                               year=2019)) is True
    hit = cache.get("dividend_sh.600000_2019")
    assert hit is not None and hit["rows"] == []
    # allstock 空 → False（重试语义，不标记完成）
    assert _run_one(bs_empty, cache, QueryItem(kind="allstock",
                                               code="2025-06-30")) is False


def test_run_one_kline_uses_five_fields(tmp_path):
    """K线只请求 5 字段（15 字段慢 ~4x，v2 实测）；adjfactor 走 query_adjust_factor。"""
    from backtest.migrate_history import _run_one
    from screener.data.cache import DiskCache

    cache = DiskCache(str(tmp_path))
    bs = _FakeBS({"kline": ["2025-01-01", "sh.600000", "10.0", "0", "1"],
                  "adjfactor": ["sh.600000", "2024-07-01", "0.9", "1.1", "1.1"]})
    _run_one(bs, cache, QueryItem(kind="kline_af3", code="sh.600000",
                                  start="2021-01-01", end="2026-09-04"))
    _run_one(bs, cache, QueryItem(kind="adjfactor", code="sh.600000",
                                  start="2021-01-01", end="2026-09-04"))
    kinds = [c[0] for c in bs.calls]
    assert kinds == ["kline", "adjfactor"]
    kline_kw = bs.calls[0][1]
    assert kline_kw["fields"] == "date,code,close,isST,tradestatus"
    assert kline_kw["adjustflag"] == "3"   # 不复权（真实成交价）
