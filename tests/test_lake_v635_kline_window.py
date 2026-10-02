# -*- coding: utf-8 -*-
"""v6.3.5 增量 K线窗口化回归（#2 防停更：14h→~6h）。

根因：sina ``fetch_kline`` 收到 start 却**故意无视**（拉全史 4823 行再客户端丢尾，
注释"全史取尾部窗口"）——增量却只要 1-3 天。单只 raw 3.2s→窗口 0.8s 实测，
×5112 股 ≈ 省 3.3h。

覆盖（零网络——adapter 层 monkeypatch _ak_daily；worker 层 mock adapter）：
1. SinaKlineAdapter.fetch_kline 窗口化：传 start → _ak_daily 收到 start/end 透传；
   不传 start → 全史（start_date='19900101'）——history 路径零回归。
2. EmptyWindowError 语义：窗口 raw 空 → EmptyWindowError（⊂ RuntimeError）；
   全史 raw 空 → 普通 RuntimeError（上市股全史必非空）。
3. run_t2 停牌股（窗口合法空）：全源 EmptyWindowError → worker **完成不抛**
   （标 done，不每日空转重试）；sina 空 + tencent 真实失败 → worker **抛错**
   （不标 done，防 done 键毒化）；sina 空 + tencent 有数 → 用 tencent 写库。
4. tdx hfq 窗口化：窗口模式 hfq count=n（非全史 20000）；全史模式 count=20000 不变。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, ".")
# scripts/ 进 sys.path（lake_backfill 直接驱动，同 test_lake_v61_def1_p0_t2 模式）
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS_DIR = os.path.join(_REPO_ROOT, "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)


# ---------------------------------------------------------------------------
# SinaKlineAdapter 窗口化（monkeypatch _ak_daily，零网络）
# ---------------------------------------------------------------------------
class _FakeDF:
    def __init__(self, rows):
        self._rows = rows
        self.columns = ["date", "open", "high", "low", "close", "volume", "amount"]

    def __len__(self):
        return len(self._rows)

    def iterrows(self):
        for i, r in enumerate(self._rows):
            yield i, dict(r)

    def tail(self, n):
        return _FakeDF(self._rows[-n:])

    def reset_index(self, drop=True):
        return self


def _mk_row(d, close=10.0):
    return {"date": d, "open": close, "high": close + 0.5, "low": close - 0.5,
            "close": close, "volume": 1_000_000.0, "amount": 10_000_000.0}


@pytest.fixture
def _no_network(monkeypatch):
    import socket
    monkeypatch.setattr(socket.socket, "connect",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no network")))


def _sina(monkeypatch, raw_rows, hfq_rows=None):
    """构造 SinaKlineAdapter，_ak_daily 返回预设 DF 并记录调用参数。"""
    from lake.ingest.sina_adapter import SinaKlineAdapter
    from lake.config import lake_cfg

    ad = SinaKlineAdapter(min_interval_s=0.0)
    calls = []
    rows_by_adjust = {"": raw_rows, "hfq": hfq_rows if hfq_rows is not None else raw_rows}

    def fake_ak_daily(symbol6, adjust, count_recent=None, start=None, end=None):
        calls.append({"adjust": adjust, "start": start, "end": end})
        r = rows_by_adjust.get(adjust) or []
        return _FakeDF(r) if r else _FakeDF([])

    monkeypatch.setattr(ad, "_ak_daily", fake_ak_daily)
    return ad, calls


def test_sina_window_passes_start_to_akshare(_no_network, monkeypatch):
    ad, calls = _sina(monkeypatch, [_mk_row("2026-09-28"), _mk_row("2026-09-30")])
    res = ad.fetch_kline("sh.601398", start="2026-09-28")
    assert len(res["ohlcv"]) == 2
    # raw 与 hfq 都收到窗口参数（服务端窗口，不再拉全史）
    assert calls[0]["start"] == "2026-09-28" and calls[0]["adjust"] == ""
    assert calls[1]["start"] == "2026-09-28" and calls[1]["adjust"] == "hfq"


def test_sina_full_history_keeps_default(_no_network, monkeypatch):
    ad, calls = _sina(monkeypatch, [_mk_row("1991-04-03"), _mk_row("2026-09-30")])
    ad.fetch_kline("sh.601398")  # history 路径：不传 start
    assert calls[0]["start"] is None and calls[0]["end"] is None, \
        "history 全史路径不得传窗口（_ak_daily 缺省 19900101）"


def test_sina_empty_window_raises_emptywindow(_no_network, monkeypatch):
    from lake.ingest.common import EmptyWindowError
    ad, _ = _sina(monkeypatch, raw_rows=[], hfq_rows=[])
    with pytest.raises(EmptyWindowError):
        ad.fetch_kline("sh.601398", start="2026-10-03")  # 停牌/节假日


def test_sina_empty_full_history_raises_plain_runtime(_no_network, monkeypatch):
    from lake.ingest.common import EmptyWindowError
    ad, _ = _sina(monkeypatch, raw_rows=[], hfq_rows=[])
    with pytest.raises(RuntimeError) as ei:
        ad.fetch_kline("sh.601398")  # 全史空 = 真实失败（非 EmptyWindowError）
    assert not isinstance(ei.value, EmptyWindowError)


def test_sina_window_adj_derivation_first_day_event(_no_network, monkeypatch):
    """窗口首日比值即基准事件（与全史推导在窗口内 af 一致——除权边界）。"""
    ad, _ = _sina(monkeypatch,
                  raw_rows=[_mk_row("2026-09-28", 10.0), _mk_row("2026-09-30", 10.0)],
                  hfq_rows=[_mk_row("2026-09-28", 50.0), _mk_row("2026-09-30", 50.0)])
    res = ad.fetch_kline("sh.601398", start="2026-09-28")
    # 窗口首日 09-28 比值 5.0 = 基准事件（全史里它之前的 af 也=5.0 时无除权）
    assert res["adj_factor"] == {"2026-09-28": 5.0}


# ---------------------------------------------------------------------------
# tdx hfq 窗口化（monkeypatch _get_client）
# ---------------------------------------------------------------------------
class _TdxFakeDF:
    """easy_tdx 的 DataFrame 替身：_df_to_rows 用 .columns + .iterrows()。"""

    def __init__(self, rows):
        self._rows = rows
        self.columns = ["datetime", "open", "high", "low", "close", "vol", "amount"]

    def __len__(self):
        return len(self._rows)

    def iterrows(self):
        for i, r in enumerate(self._rows):
            yield i, r


class _FakeTdxClient:
    def __init__(self, rows):
        self._rows = rows
        self.calls = []

    def get_stock_kline(self, market, code, period, start, count, adjust=None):
        self.calls.append({"count": count, "adjust": str(adjust)})
        tail = self._rows[-count:] if count else self._rows
        return _TdxFakeDF(tail)

    def close(self):
        pass


def _tdx_rows(n, end_date="2026-09-30"):
    """近 n 天升序日期（截至 end_date）——让窗口过滤（>=start）有行可留。"""
    from datetime import date, timedelta
    d = date.fromisoformat(end_date)
    out = []
    while len(out) < n:
        out.append({"datetime": d.isoformat(), "open": 10.0, "high": 10.5,
                    "low": 9.5, "close": 10.0, "vol": 1_000_000.0,
                    "amount": 10_000_000.0})
        d -= timedelta(days=1)
    out.reverse()   # 升序
    return out


def test_tdx_window_hfq_uses_window_count(_no_network, monkeypatch):
    from lake.ingest.easy_tdx_adapter import EasyTdxAdapter, _FULL_HISTORY_COUNT

    ad = EasyTdxAdapter(min_interval_s=0.0)
    fake_client = _FakeTdxClient(_tdx_rows(200))
    monkeypatch.setattr(ad, "_client", fake_client)
    monkeypatch.setattr(ad, "_get_client", lambda: fake_client)
    ad.fetch_kline("sh.601398", start="2026-09-28")
    counts = [c["count"] for c in fake_client.calls]
    # raw 窗口 n（max(30, ...)）；hfq 同窗口 n（v6.3.5：不再全史 20000）
    assert len(counts) == 2
    assert counts[0] < _FULL_HISTORY_COUNT, f"raw 应窗口化: {counts}"
    assert counts[1] == counts[0], f"hfq 应与 raw 同窗口 count: {counts}"


def test_tdx_full_history_hfq_keeps_full_count(_no_network, monkeypatch):
    from lake.ingest.easy_tdx_adapter import EasyTdxAdapter, _FULL_HISTORY_COUNT

    ad = EasyTdxAdapter(min_interval_s=0.0)
    fake_client = _FakeTdxClient(_tdx_rows(50))
    monkeypatch.setattr(ad, "_client", fake_client)
    monkeypatch.setattr(ad, "_get_client", lambda: fake_client)
    ad.fetch_kline("sh.601398")  # 全史（history 路径）
    counts = [c["count"] for c in fake_client.calls]
    assert counts[0] == _FULL_HISTORY_COUNT and counts[1] == _FULL_HISTORY_COUNT, \
        f"全史模式 raw/hfq 都应 20000: {counts}"


# ---------------------------------------------------------------------------
# run_t2 停牌股语义（直接驱动 run_t2，mock adapter + 真实 tmp 库，零网络）
# ---------------------------------------------------------------------------
class _MockKlineAdapter:
    """run_t2 用 mock：fetch_kline 按 preset 返回/抛错。"""

    def __init__(self, name, authority, behavior):
        self.name = name
        self.authority = authority
        self._behavior = behavior   # "ok" / "empty"(EmptyWindowError) / "fail"
        self.calls = 0

    def available(self):
        return True

    def fetch_kline(self, ts_code, start=None, end=None):
        self.calls += 1
        from lake.ingest.common import EmptyWindowError

        if self._behavior == "empty":
            raise EmptyWindowError(f"{self.name} K线窗口空 {ts_code}（停牌）")
        if self._behavior == "fail":
            raise RuntimeError(f"{self.name} K线取数失败 {ts_code}")
        return {"ohlcv": [
            {"date": "2026-09-28", "open": 10.0, "high": 10.5, "low": 9.5,
             "close": 10.0, "volume": 1_000_000, "amount": 10_000_000.0},
            {"date": "2026-09-30", "open": 10.1, "high": 10.6, "low": 9.6,
             "close": 10.2, "volume": 1_100_000, "amount": 11_000_000.0},
        ], "adj_factor": {"2026-09-28": 2.5}}


def _run_t2_direct(monkeypatch, tmp_path, db, days, codes, registry):
    """直接驱动 run_t2（跳过 run_p0 的 T4/T7/probe）。返回 (stats, con)。"""
    import lake_backfill as drv
    from lake import backfill as lb
    from lake import conn as lconn

    prog = str(tmp_path / "prog_t2.json")
    monkeypatch.setattr(lb.BackfillRunner, "_quota_state", lambda self: (False, 0))
    monkeypatch.setattr(lb, "_progress_path", lambda: prog)
    from lake.ingest import source_pool as sp

    monkeypatch.setattr(sp, "_REGISTRY", dict(registry))
    monkeypatch.delenv("LAKE_MULTISOURCE", raising=False)

    con = lconn.open(db)
    try:
        runner = lb.BackfillRunner(db_path=db)
        stats = drv.run_t2(con, db, days, codes, runner)
        return stats, con
    except Exception:
        con.close()
        raise


def test_run_t2_suspended_stock_all_sources_empty_completes(monkeypatch, tmp_path):
    """全源窗口合法空（停牌）→ worker 完成不抛（标 done，不每日空转重试）。"""
    db = str(tmp_path / "susp.duckdb")
    from lake import conn as lconn

    con = lconn.open(db)
    con.execute("INSERT INTO stock_master (ts_code, name) VALUES ('sh.601398','测试')")
    con.close()

    registry = {
        "sina": _MockKlineAdapter("sina", 0, "empty"),
        "tencent": _MockKlineAdapter("tencent", 1, "empty"),
        "tdx": _MockKlineAdapter("tdx", 3, "empty"),
    }
    stats, con = _run_t2_direct(monkeypatch, tmp_path, db, 40, ["sh.601398"], registry)
    # worker 未抛错（否则 runner.run 记 errors）→ errors 空 = 停牌股"完成"
    assert stats["errors"] == [], f"全源停牌空应视为完成（无错误）: {stats['errors']}"
    # 未写库（窗口内无数据）
    n = con.execute("SELECT COUNT(*) FROM kline_daily").fetchone()[0]
    assert n == 0, "停牌股不应写行"
    con.close()


def test_run_t2_sina_empty_tencent_fail_raises(monkeypatch, tmp_path):
    """sina 空 + tencent **真实失败** → 不视为完成（不标 done，下轮重试）。"""
    db = str(tmp_path / "susp2.duckdb")
    from lake import conn as lconn

    con = lconn.open(db)
    con.execute("INSERT INTO stock_master (ts_code, name) VALUES ('sh.601398','测试')")
    con.close()

    registry = {
        "sina": _MockKlineAdapter("sina", 0, "empty"),
        "tencent": _MockKlineAdapter("tencent", 1, "fail"),
    }
    stats, con = _run_t2_direct(monkeypatch, tmp_path, db, 40, ["sh.601398"], registry)
    # 有真实失败 → worker 抛"所有源均失败" → errors 非空（不标 done）
    assert len(stats["errors"]) == 1, f"真实失败不得视为完成: {stats['errors']}"
    con.close()


def test_run_t2_sina_empty_falls_back_to_tencent_data(monkeypatch, tmp_path):
    """sina 空（停牌误报/口径差）+ tencent 有数 → 用 tencent 写库（fallback 链）。"""
    db = str(tmp_path / "susp3.duckdb")
    from lake import conn as lconn

    con = lconn.open(db)
    con.execute("INSERT INTO stock_master (ts_code, name) VALUES ('sh.601398','测试')")
    con.close()

    registry = {
        "sina": _MockKlineAdapter("sina", 0, "empty"),
        "tencent": _MockKlineAdapter("tencent", 1, "ok"),
    }
    stats, con = _run_t2_direct(monkeypatch, tmp_path, db, 40, ["sh.601398"], registry)
    assert stats["errors"] == [], f"tencent 有数应成功: {stats['errors']}"
    n = con.execute("SELECT COUNT(*) FROM kline_daily WHERE ts_code='sh.601398'").fetchone()[0]
    assert n == 2, f"应写入 tencent 2 行: {n}"
    con.close()
