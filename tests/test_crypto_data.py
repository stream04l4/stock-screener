# -*- coding: utf-8 -*-
"""test_crypto_data —— 加密货币数据底座单测（v7.0.45，零真实网络/零真实湖）。

覆盖：
- models：临时湖建表 / upsert 幂等 / deactivate_others / stats
- binance_vision：ym_range / zip_url / last_published_month / CSV 解析（带表头+无表头）
- gateway.state：同 ts K 线去重（替换）/ 收盘标记 / 状态文件原子写
- gateway.stream：组合流 URL / 看门狗假死抛 GatewayStall（假 WS）
- web /api/gateway：状态文件存在/缺失/过期三分支（端点函数直调，环境无 httpx）
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from crypto import models  # noqa: E402
from crypto.sources import binance_vision  # noqa: E402


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------
@pytest.fixture()
def lake(tmp_path):
    p = str(tmp_path / "lake" / "crypto.duckdb")
    con = models.open_db(p)
    yield con, p
    try:
        con.close()
    except Exception:  # noqa: BLE001 - 测试内已 close 过
        pass


def test_upsert_klines_idempotent(lake):
    con, p = lake
    bar = {"ts": 1700000000, "open": 1, "high": 2, "low": 0.5, "close": 1.5,
           "volume": 10, "quote_volume": 15}
    assert models.upsert_klines(con, "binance", "BTCUSDT", [bar]) == 1
    # 同 PK 重写 → 仍 1 行（幂等）
    bar["close"] = 9.9
    models.upsert_klines(con, "binance", "BTCUSDT", [bar])
    n = con.execute("SELECT COUNT(*) FROM kline_1h").fetchone()[0]
    assert n == 1
    c = con.execute("SELECT close FROM kline_1h").fetchone()[0]
    assert c == 9.9  # 替换而非追加


def test_coin_master_active_flip(lake):
    con, _ = lake
    models.upsert_coins(con, ["BTCUSDT", "ETHUSDT"])
    models.upsert_coins(con, ["FOCUS"])  # 模拟清单缩为 FOCUS
    assert models.deactivate_others(con, ["FOCUS"]) >= 2
    rows = con.execute(
        "SELECT symbol, is_active FROM coin_master ORDER BY symbol").fetchall()
    active = {s: a for s, a in rows}
    assert active["FOCUS"] is True
    assert active["BTCUSDT"] is False  # 数据保留仅停用


def test_stats_shape(lake):
    con, p = lake
    models.upsert_coins(con, ["BTCUSDT"])
    models.upsert_klines(con, "binance", "BTCUSDT", [
        {"ts": 1700000000, "open": 1, "high": 2, "low": 0.5, "close": 1.5,
         "volume": 10, "quote_volume": 15}])
    con.close()  # stats 用 read_only 连接，须先释放写锁（与生产锁语义一致）
    st = models.stats(p)
    assert st["installed"] is True
    names = {t["name"]: t["rows"] for t in st["tables"]}
    assert names["kline_1h"] == 1
    assert st["per_symbol"][0]["symbol"] == "BTCUSDT"


# ---------------------------------------------------------------------------
# binance_vision（纯函数 + CSV 解析，零网络）
# ---------------------------------------------------------------------------
def test_ym_range_crosses_year():
    r = list(binance_vision.ym_range((2017, 11), (2018, 2)))
    assert r == [(2017, 11), (2017, 12), (2018, 1), (2018, 2)]


def test_zip_url_shape():
    u = binance_vision.zip_url("BTCUSDT", 2017, 8)
    assert u == ("https://data.binance.vision/data/spot/monthly/klines/"
                 "BTCUSDT/1h/BTCUSDT-1h-2017-08.zip")


def test_last_published_month_is_previous():
    ym = binance_vision.last_published_month(time.struct_time(
        (2026, 11, 11, 10, 0, 0, 0, 0, 0)))
    assert ym == (2026, 10)
    ym2 = binance_vision.last_published_month(time.struct_time(
        (2026, 1, 5, 10, 0, 0, 0, 0, 0)))
    assert ym2 == (2025, 12)  # 1 月 → 上一年 12 月


def test_norm_ts_units():
    """open_time 单位随文件年代变化：秒/毫秒/微秒 → 统一 epoch 秒。"""
    s = 1735689600  # 2025-01-01 00:00 UTC
    assert binance_vision._norm_ts(s) == s
    assert binance_vision._norm_ts(s * 1000) == s        # 毫秒（老文件）
    assert binance_vision._norm_ts(s * 10 ** 6) == s     # 微秒（2025-01 起）


def test_month_bars_csv_both_variants(tmp_path):
    """新文件带表头 / 老文件无表头——两种都解析（真实月文件实测过两种形态）。"""
    cache = str(tmp_path / "cache")
    d = os.path.join(cache, "klines", "TESTUSDT", "2020-08")
    os.makedirs(d)

    # 无表头（2020 年以前的老文件形态）
    with open(os.path.join(d, "TESTUSDT-1h-2020-08.csv"), "w") as f:
        f.write("1596278400000,300.0,310.0,295.0,305.0,1000,1596281999999,"
                "305000,50,60,70,\n")
    bars = binance_vision.month_bars("TESTUSDT", 2020, 8, cache)
    assert len(bars) == 1
    assert bars[0][0] == 1596278400          # ms→s
    assert bars[0][1:] == (300.0, 310.0, 295.0, 305.0, 1000.0, 305000.0)

    # 带表头
    d2 = os.path.join(cache, "klines", "TESTUSDT", "2025-06")
    os.makedirs(d2)
    with open(os.path.join(d2, "TESTUSDT-1h-2025-06.csv"), "w") as f:
        f.write("open_time,open,high,low,close,volume,close_time,"
                "quote_volume,count,taker_buy_volume,taker_buy_quote_volume,"
                "ignore\n")
        f.write("1748736000000,10,11,9,10.5,5,1748739599999,52.5,3,2,21,\n")
    bars2 = binance_vision.month_bars("TESTUSDT", 2025, 6, cache)
    assert len(bars2) == 1
    assert bars2[0] == (1748736000, 10.0, 11.0, 9.0, 10.5, 5.0, 52.5)

    # 微秒 open_time（2025-01 起的真实新格式，真实文件实测过）
    d3 = os.path.join(cache, "klines", "TESTUSDT", "2025-01")
    os.makedirs(d3)
    with open(os.path.join(d3, "TESTUSDT-1h-2025-01.csv"), "w") as f:
        f.write("1735689600000000,93576.0,94509.42,93489.03,94401.14,755,\n")
    bars3 = binance_vision.month_bars("TESTUSDT", 2025, 1, cache)
    assert bars3[0][0] == 1735689600  # µs → s


def test_full_history_cache_reuse_no_network(tmp_path, monkeypatch):
    """缓存命中 → 完全不碰网络（monkeypatch 网络函数抛错验证零调用）。"""
    cache = str(tmp_path / "cache")
    d = os.path.join(cache, "klines", "TESTUSDT", "2020-08")
    os.makedirs(d)
    with open(os.path.join(d, "TESTUSDT-1h-2020-08.csv"), "w") as f:
        f.write("1596278400000,300.0,310.0,295.0,305.0,1000,1596281999999,"
                "305000,50,60,70,\n")

    def _boom(*a, **k):
        raise AssertionError("缓存命中路径不应触网")

    monkeypatch.setattr(binance_vision, "has_month",
                        lambda *a, **k: True)
    monkeypatch.setattr(binance_vision, "_http_status", _boom)
    first, bars, missing = binance_vision.full_history(
        "TESTUSDT", (2020, 8), (2020, 8), cache)
    assert first == (2020, 8)
    assert missing == []
    assert len(bars) == 1


# ---------------------------------------------------------------------------
# gateway.state
# ---------------------------------------------------------------------------
def test_state_candle_dedup_and_close(tmp_path):
    from crypto.gateway.state import GatewayState

    st = GatewayState(str(tmp_path / "state.json"))
    # 同一根 K 线：未收盘推送 ×2（Binance 每秒推最新状态）→ 替换而非追加
    st.on_candle("BTCUSDT", 1700000000, 100, 101, 99, 100.5, 10, 1005, closed=False)
    st.on_candle("BTCUSDT", 1700000000, 100, 102, 99, 101.2, 12, 1012, closed=False)
    assert st.coins["BTCUSDT"]["candle"]["close"] == 101.2
    assert st.coins["BTCUSDT"]["price"] == 101.2
    assert st.coins["BTCUSDT"]["closed_ts"] is None
    # 收盘帧
    st.on_candle("BTCUSDT", 1700000000, 100, 102, 99, 101.2, 12, 1012, closed=True)
    assert st.coins["BTCUSDT"]["closed_ts"] == 1700000000
    snap = st.snapshot()
    assert snap["ws"] == "starting"
    assert snap["coins"]["BTCUSDT"]["price"] == 101.2


def test_state_flush_atomic_json(tmp_path):
    from crypto.gateway.state import GatewayState

    p = str(tmp_path / "sub" / "state.json")
    st = GatewayState(p)
    st.set_ws("connected")
    st.on_candle("ETHUSDT", 1700000000, 2000, 2010, 1990, 2005, 5, 10025, True)
    st.flush()
    with open(p, "r", encoding="utf-8") as f:
        d = json.load(f)
    assert d["ws"] == "connected"
    assert d["coins"]["ETHUSDT"]["candle"]["close"] == 2005
    assert d["updated"] > 0


# ---------------------------------------------------------------------------
# gateway.stream
# ---------------------------------------------------------------------------
def test_stream_url_combined():
    from crypto.gateway.stream import stream_url

    u = stream_url(["BTCUSDT", "ETHUSDT", "SOLUSDT"], "1h")
    assert u == ("wss://stream.binance.com:9443/stream?streams="
                 "btcusdt@kline_1h/ethusdt@kline_1h/solusdt@kline_1h")


def test_watchdog_stalls_on_silent_stream(monkeypatch):
    from crypto.gateway import stream as gs

    class FakeWS:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def recv(self):
            await asyncio.sleep(5)  # 永远不返回 → 看门狗先触发

    monkeypatch.setattr(gs.websockets, "connect",
                        lambda *a, **k: FakeWS())

    async def run():
        gen = gs.kline_iter(["BTCUSDT"], "1h", watchdog_s=0.2)
        with pytest.raises(gs.GatewayStall):
            await gen.__anext__()

    asyncio.run(run())


def test_stream_yields_kline_payload(monkeypatch):
    from crypto.gateway import stream as gs

    class FakeWS:
        def __init__(self):
            self.msgs = [
                json.dumps({"stream": "btcusdt@kline_1h",
                            "data": {"e": "kline", "s": "BTCUSDT",
                                     "k": {"t": 1700000000000, "x": False}}}),
                json.dumps({"stream": "btcusdt@kline_1h",
                            "data": {"e": "kline", "s": "BTCUSDT",
                                     "k": {"t": 1700000000000, "x": True}}}),
            ]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def recv(self):
            if self.msgs:
                return self.msgs.pop(0)
            await asyncio.sleep(10)

    monkeypatch.setattr(gs.websockets, "connect", lambda *a, **k: FakeWS())

    async def run():
        out = []
        gen = gs.kline_iter(["BTCUSDT"], "1h", watchdog_s=1.0)
        for _ in range(2):
            out.append(await gen.__anext__())
        await gen.aclose()
        return out

    out = asyncio.run(run())
    assert out[0][0]["k"]["x"] is False
    assert out[1][0]["k"]["x"] is True
    assert out[0][1] == "btcusdt@kline_1h"


# ---------------------------------------------------------------------------
# web /api/gateway 端点（函数直调，环境无 httpx）
# ---------------------------------------------------------------------------
def test_web_gateway_endpoint_branches(tmp_path, monkeypatch):
    import crypto.web.app as capp

    p = str(tmp_path / "gateway_state.json")
    monkeypatch.setattr(capp, "GATEWAY_STATE_PATH", type(capp.GATEWAY_STATE_PATH)(p))

    # 缺失 → down
    d = capp.gateway_status()
    assert d["ok"] is False and d["ws"] == "down"

    # 正常
    now = int(time.time())
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"ok": True, "ws": "connected", "updated": now,
                   "reconnects": 0, "last_msg": now,
                   "coins": {"BTCUSDT": {"price": 70000.0}}}, f)
    d = capp.gateway_status()
    assert d["ok"] is True and d["stale"] is False

    # 过期（updated 3 分钟前）
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"ok": True, "ws": "connected", "updated": now - 300,
                   "reconnects": 1, "last_msg": now - 300, "coins": {}}, f)
    d = capp.gateway_status()
    assert d["ok"] is True and d["stale"] is True


def test_web_gateway_mounted_on_core_app():
    """/api/gateway 必须挂在 core_app 路由表上（v7.0.44 auth 门覆盖 /crypto/api/*）。"""
    import crypto.web.app as capp

    paths = {r.path for r in capp.core_app.routes}
    assert "/api/gateway" in paths
