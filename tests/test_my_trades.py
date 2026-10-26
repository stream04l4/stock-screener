# -*- coding: utf-8 -*-
"""交易登记 / 交易历史（v7.0.17）端点直调测试。

契约：
- POST /api/my_trades：shares 正=买入（整手 100 股倍数）、负=卖出（可零股、
  不可超持仓、需先有持仓）；原子更新当前持仓（买：加权成本 3 位小数；卖：
  减股数、均价不变、清仓移除）+ 追加 data/my_trades.json。
- GET /api/my_trades：最新在前，ts_code 可选过滤。
- DELETE /api/my_trades/{id}：仅移除日志，不回改当前持仓。

环境无 httpx → 直接调端点函数（与 test_martingale_web.py 同模式）；
写盘路径一律 monkeypatch 到 tmp（绝不碰生产 user_positions.json / my_trades.json）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from fastapi import HTTPException  # noqa: E402

import web.app as wapp  # noqa: E402


@pytest.fixture()
def tmp_trades(tmp_path, monkeypatch):
    """持仓账本 + 交易历史双账本隔离到 tmp。"""
    pos = tmp_path / "user_positions.json"
    tr = tmp_path / "my_trades.json"
    monkeypatch.setattr(wapp, "USER_POSITIONS_PATH", pos)
    monkeypatch.setattr(wapp, "USER_TRADES_PATH", tr)
    return pos, tr


def _seed_position(code, shares, cost):
    wapp._up_write({code: {"shares": shares, "cost_price": cost,
                           "updated_at": "2026-01-01T00:00:00Z"}})


def _read_pos():
    return wapp._up_read()


def _read_trades():
    return wapp._trades_read()


# ---------------------------------------------------------------------------
# 买入
# ---------------------------------------------------------------------------
def test_trade_buy_updates_position_and_history(tmp_trades):
    _seed_position("sh.600000", 1000, 10.0)
    res = wapp.record_trade({"ts_code": "sh.600000", "shares": 500, "price": 12.0})
    assert res["ok"]
    pos = _read_pos()["sh.600000"]
    # 加权成本：(1000*10 + 500*12) / 1500 = 10.6666… → 10.667（3 位小数，券商口径）
    assert pos["shares"] == 1500
    assert pos["cost_price"] == 10.667
    trades = _read_trades()
    assert len(trades) == 1
    t = trades[0]
    assert t["direction"] == "buy"
    assert t["shares"] == 500
    assert t["price"] == 12.0
    assert t["amount"] == 6000.0
    assert t["realized_pnl"] is None
    assert len(t["date"]) == 10 and t["date"][4] == "-"  # 默认日期 = 今天（YYYY-MM-DD）
    assert t["id"].startswith("t")
    # 落盘文件结构
    disk = json.loads(wapp.USER_TRADES_PATH.read_text(encoding="utf-8"))
    assert disk["trades"] == trades
    assert disk["updated_at"]


def test_trade_buy_first_lot_creates_position(tmp_trades):
    res = wapp.record_trade({"ts_code": "sz.000001", "shares": 100, "price": 8.88,
                             "name": "平安银行", "date": "2026-10-20"})
    assert res["ok"] and not res["position"]["removed"]
    pos = _read_pos()["sz.000001"]
    assert pos["shares"] == 100 and pos["cost_price"] == 8.88
    t = _read_trades()[0]
    assert t["name"] == "平安银行" and t["date"] == "2026-10-20"


def test_trade_buy_requires_whole_lot(tmp_trades):
    with pytest.raises(HTTPException) as e:
        wapp.record_trade({"ts_code": "sh.600000", "shares": 150, "price": 10})
    assert e.value.status_code == 400 and "整手" in e.value.detail


# ---------------------------------------------------------------------------
# 卖出（负数）
# ---------------------------------------------------------------------------
def test_trade_sell_reduces_shares_keeps_cost(tmp_trades):
    _seed_position("sh.600000", 1000, 10.0)
    res = wapp.record_trade({"ts_code": "sh.600000", "shares": -300, "price": 12.0})
    assert res["ok"] and not res["position"]["removed"]
    pos = _read_pos()["sh.600000"]
    assert pos["shares"] == 700
    assert pos["cost_price"] == 10.0  # 剩余持仓均价不变（券商口径）
    t = _read_trades()[0]
    assert t["direction"] == "sell"
    assert t["shares"] == 300          # 历史存正股数，方向由 direction 区分
    assert t["amount"] == 3600.0
    assert t["realized_pnl"] == 600.0  # (12−10)×300


def test_trade_sell_odd_lot_allowed(tmp_trades):
    """A 股卖出可零股一次了结 → 卖 333 股合法。"""
    _seed_position("sh.600000", 500, 10.0)
    res = wapp.record_trade({"ts_code": "sh.600000", "shares": -333, "price": 11.0})
    assert res["ok"]
    assert _read_pos()["sh.600000"]["shares"] == 167


def test_trade_sell_zero_removes_position(tmp_trades):
    _seed_position("sh.600000", 500, 10.0)
    res = wapp.record_trade({"ts_code": "sh.600000", "shares": -500, "price": 9.0})
    assert res["ok"] and res["position"]["removed"]
    assert res["position"]["cost_price"] is None
    assert "sh.600000" not in _read_pos()  # 清仓 → 持仓账本移除
    t = _read_trades()[0]
    assert t["realized_pnl"] == -500.0     # (9−10)×500


def test_trade_sell_cannot_exceed_holding(tmp_trades):
    _seed_position("sh.600000", 400, 10.0)
    with pytest.raises(HTTPException) as e:
        wapp.record_trade({"ts_code": "sh.600000", "shares": -500, "price": 11})
    assert e.value.status_code == 400 and "不能超过持仓" in e.value.detail


def test_trade_sell_without_position(tmp_trades):
    with pytest.raises(HTTPException) as e:
        wapp.record_trade({"ts_code": "sh.600000", "shares": -100, "price": 11})
    assert e.value.status_code == 400 and "未持有" in e.value.detail


# ---------------------------------------------------------------------------
# 列表 / 删除
# ---------------------------------------------------------------------------
def test_trade_list_all_and_filter(tmp_trades, monkeypatch):
    _seed_position("sh.600000", 2000, 10.0)
    _seed_position("sz.000001", 1000, 5.0)
    # 固定时间戳 → created_at 相同，靠 id 排序（追加序）
    class _Fixed(wapp.datetime):
        @classmethod
        def now(cls, tz=None):
            return wapp.datetime(2026, 10, 26, 12, 0, 0, 0, tzinfo=tz)
    monkeypatch.setattr(wapp, "datetime", _Fixed)

    wapp.record_trade({"ts_code": "sh.600000", "shares": 100, "price": 11})
    wapp.record_trade({"ts_code": "sz.000001", "shares": 100, "price": 5.5})
    wapp.record_trade({"ts_code": "sh.600000", "shares": -100, "price": 12})

    all_d = wapp.list_trades()
    assert all_d["n"] == 3
    assert all_d["trades"][0]["id"] == _read_trades()[-1]["id"]  # 最新在前（追加序）

    one = wapp.list_trades(ts_code="sh.600000")
    assert one["n"] == 2
    assert all(t["ts_code"] == "sh.600000" for t in one["trades"])
    assert wapp.list_trades(ts_code="sz.000001")["n"] == 1


def test_trade_delete_removes_record_only(tmp_trades):
    _seed_position("sh.600000", 1000, 10.0)
    wapp.record_trade({"ts_code": "sh.600000", "shares": 100, "price": 11})
    tid = _read_trades()[0]["id"]
    res = wapp.delete_trade(tid)
    assert res["ok"] and res["n"] == 0 and res["ts_code"] == "sh.600000"
    assert _read_trades() == []
    assert _read_pos()["sh.600000"]["shares"] == 1100  # 持仓不回改


def test_trade_delete_not_found(tmp_trades):
    with pytest.raises(HTTPException) as e:
        wapp.delete_trade("t000000")
    assert e.value.status_code == 404


# ---------------------------------------------------------------------------
# 参数校验
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("payload,frag", [
    ({"ts_code": "600000", "shares": 100, "price": 10}, None),           # 无交易所前缀
    ({"ts_code": "sh.600000", "shares": 0, "price": 10}, "不能为 0"),
    ({"ts_code": "sh.600000", "shares": 1.5, "price": 10}, "整数"),
    ({"ts_code": "sh.600000", "shares": "abc", "price": 10}, "数字"),
    ({"ts_code": "sh.600000", "shares": 100, "price": 0}, "正数"),
    ({"ts_code": "sh.600000", "shares": 100, "price": -1}, "正数"),
    ({"ts_code": "sh.600000", "shares": 100, "price": 10, "date": "2026/10/26"}, "YYYY-MM-DD"),
])
def test_trade_rejects_bad_payload(tmp_trades, payload, frag):
    with pytest.raises(HTTPException) as e:
        wapp.record_trade(payload)
    assert e.value.status_code == 400
    if frag:
        assert frag in e.value.detail


def test_trade_same_millisecond_ids_unique(tmp_trades, monkeypatch):
    """同毫秒连笔 → 第二笔 id 加后缀，不撞键。"""
    class _Fixed(wapp.datetime):
        @classmethod
        def now(cls, tz=None):
            return wapp.datetime(2026, 10, 26, 12, 0, 0, 0, tzinfo=tz)
    monkeypatch.setattr(wapp, "datetime", _Fixed)
    wapp.record_trade({"ts_code": "sh.600000", "shares": 100, "price": 10})
    wapp.record_trade({"ts_code": "sh.600000", "shares": -100, "price": 11})
    ids = [t["id"] for t in _read_trades()]
    assert len(set(ids)) == 2
    assert ids[1] == ids[0] + "x"


def test_new_routes_registered_in_openapi():
    """三个新端点已物化到 openapi（登录门禁按 /api/* 路径统一生效）。"""
    paths = set(wapp.app.openapi()["paths"].keys())
    assert "/api/my_trades" in paths
    assert "/api/my_trades/{trade_id}" in paths
    assert wapp.app.openapi()["paths"]["/api/my_trades"]["post"]["responses"]
