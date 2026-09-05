# -*- coding: utf-8 -*-
"""D-01 回归测试（离线 fixture，不联网）。

缺陷 D-01：未来日期运行会把空股票列表永久缓存，污染该日真实运行。
修复两处：
1. 主修复 —— ``_resolve_run_day`` 拒绝未来日期（requested > today → RuntimeError）。
2. 纵深防御 —— ``DataFetcher.all_stock`` 对空结果（0 行）不写入永久缓存；
   遗留的空缓存文件读取时视为 miss 重新拉取。
"""
from __future__ import annotations

import os
from datetime import date, timedelta

import pytest

from screener.data.cache import CACHE_SENTINEL, DiskCache
from screener.data.fetchers import DataFetcher
from screener.screener import _resolve_run_day


# ---------- 离线 fake（不碰 BaoStock / 网络） ----------

class FakeClient:
    """假 BaoStock client：记录调用次数，all_stock 返回预置行。"""

    def __init__(self, all_stock_rows=None):
        self.all_stock_rows = list(all_stock_rows or [])
        self.request_count = 0
        self.labels = []

    def call_with_fields(self, query_fn, *, label="", **kwargs):
        self.request_count += 1
        self.labels.append(label)
        if label == "all_stock":
            return ["code", "tradeStatus", "code_name"], list(self.all_stock_rows)
        raise AssertionError(f"unexpected call: {label}")

    def close(self):
        pass


def make_fetcher(tmp_path, client) -> DataFetcher:
    return DataFetcher(client, DiskCache(str(tmp_path / "cache")))


class FakeCalendarFetcher:
    """只实现 latest_trade_date（模拟交易日历）。"""

    def __init__(self, resolve_to=None):
        self.resolve_to = resolve_to  # None → 原样返回请求日（视为交易日）
        self.latest_calls = []

    def latest_trade_date(self, on_or_before):
        self.latest_calls.append(on_or_before)
        return self.resolve_to if self.resolve_to is not None else on_or_before


# ---------- D-01 主修复：未来日期被拒绝 ----------

def test_future_date_rejected():
    """未来交易日直接拒绝，错误信息含'请求日期晚于当前日期'。"""
    f = FakeCalendarFetcher()
    with pytest.raises(RuntimeError, match="请求日期晚于当前日期"):
        _resolve_run_day(f, date(2026, 9, 11), today=date(2026, 9, 5))


def test_future_date_rejected_before_any_calendar_query():
    """守卫在任何网络请求（交易日历拉取）之前抛出。"""
    f = FakeCalendarFetcher()
    with pytest.raises(RuntimeError):
        _resolve_run_day(f, date(2026, 9, 11), today=date(2026, 9, 5))
    assert f.latest_calls == []


def test_today_allowed():
    """请求日 == 今天 → 允许（当天运行场景，A股收盘后 T+1 数据已更新）。"""
    f = FakeCalendarFetcher()
    d, fallback = _resolve_run_day(f, date(2026, 9, 4), today=date(2026, 9, 4))
    assert d == date(2026, 9, 4) and fallback is False


def test_past_nontrading_day_falls_back_unchanged():
    """过去非交易日 → 回退到最近交易日（原有行为保持不变）。"""
    f = FakeCalendarFetcher(resolve_to=date(2026, 9, 4))  # 周六 09-05 → 周五 09-04
    d, fallback = _resolve_run_day(f, date(2026, 9, 5), today=date(2026, 9, 5))
    assert d == date(2026, 9, 4) and fallback is True


def test_cli_future_date_exits_nonzero(capsys, monkeypatch):
    """CLI 层：--date 未来日期 → exit code 1 + 明确报错，在任何数据访问之前被拒绝。

    不桩掉 run_screener（守卫就在引擎内部），而是把 BaoStockClient 的网络入口
    call_with_fields 打桩为"一碰就炸"——若守卫失效、流程走到拉数据，会立刻以
    NETWORK_REACHED 失败（而非挂起在真实网络重试上）。
    """
    import screener.__main__ as cli_mod
    from screener.data.baostock_client import BaoStockClient

    calls = []

    def fake_call_with_fields(self, query_fn, *, label="", **kwargs):
        calls.append(label)
        raise RuntimeError("NETWORK_REACHED")

    monkeypatch.setattr(BaoStockClient, "call_with_fields", fake_call_with_fields)
    # 日志落盘打桩（避免测试污染 logs/）
    monkeypatch.setattr(cli_mod, "_setup_logging", lambda *a, **k: None)

    future = (date.today() + timedelta(days=30)).isoformat()  # 动态未来日，测试长期有效
    rc = cli_mod.main(["--date", future, "--config", "config/strategy.yaml"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "请求日期晚于当前日期" in err
    assert calls == []  # 守卫在任何 BaoStock 请求之前生效


# ---------- D-01 纵深防御：all_stock 空结果不永久缓存 ----------

def test_all_stock_empty_not_cached(tmp_path):
    """空结果（0 行）→ 不落盘；第二次调用重新拉取而非命中空缓存。"""
    client = FakeClient(all_stock_rows=[])
    f = make_fetcher(tmp_path, client)

    df1 = f.all_stock("2026-09-11")
    assert len(df1) == 0
    # 原缺陷现场：生成仅表头的 cache/allstock_2026-09-11.csv —— 修复后不得存在
    assert not os.path.exists(str(tmp_path / "cache" / "allstock_2026-09-11.csv"))
    assert client.request_count == 1

    df2 = f.all_stock("2026-09-11")
    assert len(df2) == 0
    assert client.request_count == 2          # 重新拉取（空结果未命中）
    assert f.calls["fetched"] == 2 and f.calls["cache_hit"] == 0


def test_all_stock_nonempty_permanent_cache(tmp_path):
    """非空结果 → 永久缓存（既有行为不变：第二次命中、零请求）。"""
    rows = [["sh.601398", "1", "工商银行"], ["sz.000001", "1", "平安银行"]]
    client = FakeClient(all_stock_rows=rows)
    f = make_fetcher(tmp_path, client)

    df1 = f.all_stock("2026-09-04")
    assert len(df1) == 2
    df2 = f.all_stock("2026-09-04")
    assert len(df2) == 2
    assert client.request_count == 1
    assert f.calls["cache_hit"] == 1 and f.calls["fetched"] == 1


def test_all_stock_legacy_empty_file_treated_as_miss(tmp_path):
    """遗留空缓存文件（D-01 事故污染件）→ 读侧视为 miss 重新拉取；
    重拉到非空结果覆盖污染文件，之后命中缓存。"""
    cdir = tmp_path / "cache"
    cdir.mkdir()
    # 按旧格式手工构造污染文件：哨兵 + 表头，0 行数据
    with open(cdir / "allstock_2026-09-11.csv", "w", encoding="utf-8") as fh:
        fh.write(f"{CACHE_SENTINEL}\ncode,tradeStatus,code_name\n")

    client = FakeClient(all_stock_rows=[["sh.601398", "1", "工商银行"]])
    f = make_fetcher(tmp_path, client)

    df = f.all_stock("2026-09-11")
    assert len(df) == 1                        # 重新拉取，未返回空池
    assert client.request_count == 1           # 污染文件被当作 miss

    df2 = f.all_stock("2026-09-11")
    assert len(df2) == 1
    assert client.request_count == 1           # 非空结果已覆盖，命中缓存


def test_all_stock_repeated_empty_keeps_legacy_file_inert(tmp_path):
    """重拉仍为空 → 旧文件留在盘上但惰性（读侧永不命中），每次调用都重新拉取。"""
    cdir = tmp_path / "cache"
    cdir.mkdir()
    with open(cdir / "allstock_2026-09-11.csv", "w", encoding="utf-8") as fh:
        fh.write(f"{CACHE_SENTINEL}\ncode,tradeStatus,code_name\n")

    client = FakeClient(all_stock_rows=[])
    f = make_fetcher(tmp_path, client)

    assert len(f.all_stock("2026-09-11")) == 0
    assert client.request_count == 1
    # 旧文件仍在（未被删除），但不得被当作有效缓存命中
    assert os.path.exists(str(cdir / "allstock_2026-09-11.csv"))
    assert len(f.all_stock("2026-09-11")) == 0
    assert client.request_count == 2           # 仍重新拉取
