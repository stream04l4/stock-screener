# -*- coding: utf-8 -*-
"""test_strategy_signals —— v7.0.19：我的持仓页「策略信号中心」。

契约（全离线，零真实网络/零生产数据）：
- martingale.signals.classify_signal 纯函数四态口径
  （active / approaching / idle / no_data；边界；approach_pct 可调）；
- martingale.engine.run_stock buys 元组 5 元素标注（k 档下标 / first_of_round），
  前 3 元语义与历史一致（f6 红线由 test_f6_regression 守护）；
- web /api/strategy_signals 端点：股票池 ∪ 账本、持仓/非持仓触发价口径分叉
  （持仓=模拟 next 档；非持仓=首仓线）、history 日期降序（同日同股档位先后）、
  湖锁定降级、strategy 库名 400、空 universe。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from fastapi import HTTPException  # noqa: E402

import web.app as wapp  # noqa: E402
import screener.data.tencent as tencent_mod  # noqa: E402
from martingale.config import StrategyParams  # noqa: E402
from martingale.engine import run_stock  # noqa: E402
from martingale.signals import (  # noqa: E402
    DEFAULT_APPROACH_PCT, build_signals, classify_signal,
)
from martingale.data import LakeLocked  # noqa: E402

SP = SimpleNamespace(name="drawdown_martingale", base_add_amount=10000.0,
                     growth_ratio=1.1, initial_amount=100000.0)


# ---------------------------------------------------------------------------
# classify_signal 纯函数
# ---------------------------------------------------------------------------

def test_classify_active_held_add():
    c = classify_signal(9.9, 10.0, True, 1, SP)
    assert c["status"] == "active"
    assert c["action"] == "add"
    assert c["suggested_amount"] == round(10000.0 * 1.1 ** 1)
    assert c["distance_pct"] == pytest.approx(-1.0)


def test_classify_active_unheld_buy():
    c = classify_signal(20.0, 20.0, False, 0, SP)  # 触线当日（≤）也算 active
    assert c["status"] == "active"
    assert c["action"] == "buy"
    assert c["suggested_amount"] == 100000
    assert c["distance_pct"] == 0.0


def test_classify_approaching_band_and_boundary():
    # 触发价上方 2%（默认带 3% 内）→ approaching
    c = classify_signal(30.6, 30.0, True, 0, SP)
    assert c["status"] == "approaching"
    assert c["action"] == "add"  # approaching 也预告动作
    assert c["distance_pct"] == pytest.approx(2.0)
    # 恰好带边（+3%）→ 仍 approaching
    assert classify_signal(30.9, 30.0, True, 0, SP)["status"] == "approaching"
    # 刚出带（+3.01%）→ idle
    c = classify_signal(30.903, 30.0, True, 0, SP)
    assert c["status"] == "idle"
    assert c["action"] is None
    assert c["suggested_amount"] is None


def test_classify_idle_far():
    c = classify_signal(45.0, 40.0, False, 0, SP)
    assert c["status"] == "idle"
    assert c["distance_pct"] == pytest.approx(12.5)


def test_classify_no_data_variants():
    for price, trig in ((None, 10.0), (9.0, None), (None, None), (9.0, 0.0)):
        c = classify_signal(price, trig, True, 0, SP)
        assert c["status"] == "no_data"
        assert c["action"] is None and c["distance_pct"] is None


def test_classify_custom_approach_pct():
    # 带宽 1%：+2% 已出带
    assert classify_signal(30.6, 30.0, True, 0, SP, approach_pct=1.0)["status"] == "idle"
    assert classify_signal(30.05, 30.0, True, 0, SP, approach_pct=1.0)["status"] == "approaching"


def test_classify_sp_none_amounts_null():
    c = classify_signal(9.9, 10.0, True, 1, None)
    assert c["status"] == "active" and c["action"] == "add"
    assert c["suggested_amount"] is None


def test_build_signals_unchanged_shape_and_sort():
    """build_signals 重构到 classify_signal 后：输出形状/排序/口径不变。"""
    positions = [
        {"ts_code": "sz.000002", "name": "B", "spot_price": 5.0,
         "last_price": 5.2, "next_add_price_raw": 5.1, "in_pos": False, "k_level": 0},
        {"ts_code": "sh.600001", "name": "A", "spot_price": 9.9,
         "last_price": 10.1, "next_add_price_raw": 10.0, "in_pos": True, "k_level": 1},
        {"ts_code": "sh.600003", "name": "C", "spot_price": 30.6,
         "last_price": 30.5, "next_add_price_raw": 30.0, "in_pos": True, "k_level": 0},
        {"ts_code": "sh.600004", "name": "D", "spot_price": None,
         "last_price": 8.0, "next_add_price_raw": None, "in_pos": None, "k_level": None},
    ]
    out = build_signals(positions, SP)
    assert [s["ts_code"] for s in out] == ["sh.600001", "sz.000002"]  # 升序
    a = out[0]
    assert set(a.keys()) == {"ts_code", "name", "price", "next_add_price",
                             "k_level", "next_level", "suggested_amount", "action"}
    assert a["action"] == "add" and a["next_level"] == 2
    assert a["suggested_amount"] == round(10000.0 * 1.1)
    b = out[1]
    assert b["action"] == "buy" and b["next_level"] == 1 and b["suggested_amount"] == 100000


# ---------------------------------------------------------------------------
# engine buys 标注（合成数据，同 test_martingale_engine 风格）
# ---------------------------------------------------------------------------

def _dates(*iso):
    return np.array([np.datetime64(d, "D").astype("datetime64[ns]") for d in iso])


def _params(**kw):
    base = dict(name="drawdown_martingale", initial_amount=500.0, base_add_amount=1000.0,
                first_drawdown=0.15, add_step=0.05, growth_ratio=1.0, rearm_mode="bounce",
                rearm_pct=0.15, accounting="cash")
    base.update(kw)
    return StrategyParams(**base)


def test_buys_annotation_ladder_and_first_of_round():
    """H=100；bar1 80 穿 k=0(85)+k=1(80)；bar2 74 穿 k=2(75)；bar3 70 穿 k=3(70)。"""
    p = _params()
    _stat, _state, _v, _c, _f, buys = run_stock(
        "t", _dates("2020-01-01", "2020-01-02", "2020-01-03", "2020-01-04"),
        np.array([100.0, 80.0, 74.0, 70.0]), np.array([100.0, 80.0, 74.0, 70.0]),
        None, p, verbose=False)
    assert len(buys) == 4
    assert [b[3] for b in buys] == [0, 1, 2, 3]            # 档下标
    assert [b[4] for b in buys] == [True, False, False, False]  # 仅首笔 first_of_round
    assert buys[0][2] == pytest.approx(500.0)              # 首仓 initial
    assert all(b[2] == pytest.approx(1000.0) for b in buys[1:])
    # 前 3 元与历史 3 元组同语义
    assert buys[0][:3] == (str(_dates("2020-01-02")[0]), 80.0, 500.0)


def test_buys_annotation_rearm_k_resets_but_first_of_round_stays_false():
    """再武装后 k 重置回 0，但 in_pos 恒 True（策略不卖）→ first_of_round=False，
    该笔是「第1档」而非「首仓」（金额 = base×growth^0，非 initial）。

    bar0 H=100；bar1 80：买 k=0（首仓 500）+k=1(80)；bar2 100：反弹≥80×1.15→rearm(k=0)；
    bar3 80：再穿 k=0(85)+k=1(80)——两笔 first_of_round 均 False、金额 1000。
    """
    p = _params(rearm_pct=0.15)
    _stat, _state, _v, _c, _f, buys = run_stock(
        "t", _dates("2020-01-01", "2020-01-02", "2020-01-03", "2020-01-04"),
        np.array([100.0, 80.0, 100.0, 80.0]), np.array([100.0, 80.0, 100.0, 80.0]),
        None, p, verbose=False)
    assert [b[3] for b in buys] == [0, 1, 0, 1]
    assert [b[4] for b in buys] == [True, False, False, False]
    assert buys[0][2] == pytest.approx(500.0)
    assert all(b[2] == pytest.approx(1000.0) for b in buys[1:])
    assert _stat["n_rearms"] == 1  # 发生了一次再武装


# ---------------------------------------------------------------------------
# /api/strategy_signals 端点（全 monkeypatch，零湖零网络）
# ---------------------------------------------------------------------------
USER = {
    "sh.600001": {"shares": 100.0, "cost_price": 9.0, "updated_at": "2026-11-07T00:00:00Z"},
    "sh.600003": {"shares": 50.0, "cost_price": 31.0, "updated_at": "2026-11-07T00:00:00Z"},
}
# 触发价口径（v7.0.19）：按 in_pos 分支，与 held 无关——
# in_pos → next_add_price_raw（加仓第 N 档）；未 in_pos → first_level_raw（首仓线）
ROWS = {
    "sh.600001": {"name": "甲股", "last_price": 10.1, "in_pos": True, "k_level": 1,
                  "next_add_price_raw": 10.0, "first_level_raw": 12.0,
                  "end_date": "2026-11-06",
                  "buys": [
                      {"date": "2026-01-02", "price": 10.5, "amount": 10000.0,
                       "level": 0, "first_of_round": True},
                      {"date": "2026-01-03", "price": 10.0, "amount": 11000.0,
                       "level": 1, "first_of_round": False},
                  ]},
    "sh.600002": {"name": "乙股", "last_price": 20.0, "in_pos": True, "k_level": 2,
                  "next_add_price_raw": 20.0, "first_level_raw": 30.0,
                  "end_date": "2026-11-06", "buys": []},
    "sh.600003": {"name": "丙股", "last_price": 30.5, "in_pos": True, "k_level": 0,
                  "next_add_price_raw": 30.0, "first_level_raw": 30.0,
                  "end_date": "2026-11-05",
                  "buys": [
                      {"date": "2025-12-31", "price": 31.0, "amount": 100000.0,
                       "level": 0, "first_of_round": True},
                  ]},
    # 非持仓 + 未 in_pos：next(35.0) 与 first(40.0) 故意不同值——
    # 口径必须走首仓线 40.0（若误走 next 35.0：37.0 > 35×1.03 → idle，断言可区分）
    "sh.600004": {"name": "丁股", "last_price": 45.0, "in_pos": False, "k_level": 0,
                  "next_add_price_raw": 35.0, "first_level_raw": 40.0,
                  "end_date": "2026-11-06", "buys": []},
    # sh.600005 在池但湖中无数据（ROWS 无此行）→ no_data
}
POOL = ["sh.600001", "sh.600002", "sh.600003", "sh.600004", "sh.600005"]
SPOT = {
    "sh.600001": {"price": 9.9, "timestamp": "20261107093000"},
    "sh.600002": {"price": 20.0, "timestamp": "20261107093000"},
    "sh.600003": {"price": 30.6, "timestamp": "20261107093000"},
    "sh.600004": {"price": 37.0, "timestamp": "20261107093000"},
}


def _wire(monkeypatch, rows=ROWS, pool=POOL, user=USER, spot=SPOT,
          lake_exc=None):
    monkeypatch.setattr(wapp, "_up_read", lambda: dict(user))

    def _pool_ctx(cfg=None):
        return SP, None, SimpleNamespace(close=lambda: None), SimpleNamespace(codes=list(pool))

    if lake_exc is not None:
        def _boom(cfg=None):
            raise lake_exc("测试注入：湖不可用")
        monkeypatch.setattr(wapp, "_load_pool_ctx", _boom)
    else:
        monkeypatch.setattr(wapp, "_load_pool_ctx", _pool_ctx)
    monkeypatch.setattr(
        wapp, "_strategy_state_rows",
        lambda sp, con, codes, end=None, include_buys=False:
        (lambda r: {k: dict(v, **({"buys": list(v["buys"])} if include_buys else {}))
                    for k, v in r.items()})(dict(rows)))
    monkeypatch.setattr(tencent_mod, "TencentClient",
                        lambda **kw: SimpleNamespace(
                            fetch=lambda codes: {c: spot[c] for c in codes if c in spot}))


def test_strategy_signals_main_contract(monkeypatch):
    _wire(monkeypatch)
    out = wapp.get_strategy_signals()
    assert out["strategy"] == "current"
    assert out["lake_locked"] is False
    assert out["approach_pct"] == DEFAULT_APPROACH_PCT == 3.0
    assert out["price_source"] == "spot"
    assert out["as_of"] == "2026-11-06"
    assert out["n_stocks"] == 5  # 池 5（含湖中无数据的 600005）∪ 账本 2 ⊂ 池

    by = {s["ts_code"]: s for s in out["stocks"]}
    # 持仓股 + in_pos：next 档口径 → 9.9 ≤ 10.0 → active 加仓
    s1 = by["sh.600001"]
    assert s1["held"] is True and s1["status"] == "active"
    assert s1["action"] == "add" and s1["next_add_price_raw"] == 10.0
    assert s1["suggested_amount"] == round(10000.0 * 1.1)
    # 非持仓 + in_pos：同样走 next 档（从当前状态加入策略）→ 20.0 ≤ 20.0 → active 加仓
    s2 = by["sh.600002"]
    assert s2["held"] is False and s2["status"] == "active"
    assert s2["action"] == "add" and s2["next_add_price_raw"] == 20.0
    assert s2["suggested_amount"] == round(10000.0 * 1.1 ** 2)
    # 持仓股 +2%（3% 带内）→ approaching
    s3 = by["sh.600003"]
    assert s3["status"] == "approaching" and s3["distance_pct"] == pytest.approx(2.0)
    # 非持仓 + 未 in_pos：首仓线口径（37.0 ≤ 40.0；若误走 next 35.0 会判 idle）
    s4 = by["sh.600004"]
    assert s4["held"] is False and s4["status"] == "active"
    assert s4["action"] == "buy" and s4["next_add_price_raw"] == 40.0
    assert s4["suggested_amount"] == 100000
    # 湖中无数据 → no_data（池内非持仓）
    s5 = by["sh.600005"]
    assert s5["status"] == "no_data" and s5["held"] is False and s5["price"] is None

    t = out["totals"]
    assert t == {"n_stocks": 5, "n_active": 3, "n_approaching": 1, "n_idle": 0,
                 "n_no_data": 1, "n_active_held": 1, "n_active_unheld": 2,
                 "n_history": 3}
    # history：日期降序；3 笔
    h = out["history"]
    assert [(x["date"], x["ts_code"]) for x in h] == [
        ("2026-01-03", "sh.600001"), ("2026-01-02", "sh.600001"),
        ("2025-12-31", "sh.600003"),
    ]
    assert all(set(x.keys()) == {"ts_code", "name", "held", "date", "price",
                                 "amount", "level", "first_of_round"} for x in h)
    assert h[0]["held"] is True and h[2]["held"] is True


def test_strategy_signals_history_same_day_level_order(monkeypatch):
    """同日同股多档：稳定排序保留档位先后（先首仓后加仓）。"""
    rows = dict(ROWS)
    rows["sh.600001"] = dict(rows["sh.600001"], buys=[
        {"date": "2026-01-02", "price": 10.5, "amount": 10000.0, "level": 0,
         "first_of_round": True},
        {"date": "2026-01-02", "price": 10.0, "amount": 11000.0, "level": 1,
         "first_of_round": False},
    ])
    _wire(monkeypatch, rows=rows)
    h = wapp.get_strategy_signals()["history"]
    assert [(x["ts_code"], x["level"]) for x in h if x["ts_code"] == "sh.600001"] \
        == [("sh.600001", 0), ("sh.600001", 1)]


def test_strategy_signals_lake_locked_degrade(monkeypatch):
    """湖锁定 → 只列账本代码，全部 no_data，不 500。"""
    _wire(monkeypatch, lake_exc=LakeLocked)
    out = wapp.get_strategy_signals()
    assert out["lake_locked"] is True
    assert [s["ts_code"] for s in out["stocks"]] == ["sh.600001", "sh.600003"]
    assert all(s["status"] == "no_data" for s in out["stocks"])
    assert out["history"] == []
    assert out["price_source"] == "lake"
    assert out["as_of"] is None


def test_strategy_signals_empty_universe(monkeypatch):
    _wire(monkeypatch, pool=[], user={})
    out = wapp.get_strategy_signals()
    assert out["n_stocks"] == 0 and out["stocks"] == [] and out["history"] == []
    assert out["totals"]["n_stocks"] == 0


def test_strategy_signals_user_only_ledger_codes(monkeypatch):
    """账本里有池外股 → universe = 池 ∪ 账本，池外股照常出状态。"""
    user = dict(USER, **{"sz.000999": {"shares": 10.0, "cost_price": 1.0,
                                        "updated_at": "2026-11-07T00:00:00Z"}})
    rows = dict(ROWS, **{"sz.000999": {"name": "戊股", "last_price": 5.0,
                                        "in_pos": False, "k_level": 0,
                                        "next_add_price_raw": 5.0,
                                        "first_level_raw": 6.0,
                                        "end_date": "2026-11-06", "buys": []}})
    _wire(monkeypatch, user=user, rows=rows,
          spot={**SPOT, "sz.000999": {"price": 5.9, "timestamp": "20261107093000"}})
    out = wapp.get_strategy_signals()
    assert out["n_stocks"] == 6
    s = next(x for x in out["stocks"] if x["ts_code"] == "sz.000999")
    # 池外账本股 + 未 in_pos → 首仓线口径：5.9 ≤ 6.0 → active（若误走 next 5.0 → idle）
    assert s["held"] is True and s["status"] == "active"
    assert s["action"] == "buy" and s["next_add_price_raw"] == 6.0


def test_strategy_signals_strategy_lib_400(monkeypatch, tmp_path):
    _wire(monkeypatch)
    monkeypatch.setattr(wapp, "_strategy_path",
                        lambda name: tmp_path / (name + ".yaml"))
    with pytest.raises(HTTPException) as ei:
        wapp.get_strategy_signals(strategy="不存在")
    assert ei.value.status_code == 400
    # 存在 → cfg_path 透传到 _load_pool_ctx
    lib = tmp_path / "保守_低档.yaml"
    lib.write_text("strategy: {}\n", encoding="utf-8")
    seen = {}

    def _ctx(cfg=None):
        seen["cfg"] = cfg
        return SP, None, SimpleNamespace(close=lambda: None), SimpleNamespace(codes=list(POOL))

    monkeypatch.setattr(wapp, "_load_pool_ctx", _ctx)
    out = wapp.get_strategy_signals(strategy="保守_低档")
    assert out["strategy"] == "保守_低档"
    assert seen["cfg"] == str(lib)


def test_strategy_signals_spot_fallback_lake_price(monkeypatch):
    """实时行情失败（spot 空）→ 回退湖最后价，price_source=lake。"""
    _wire(monkeypatch, spot={})
    out = wapp.get_strategy_signals()
    assert out["price_source"] == "lake"
    by = {s["ts_code"]: s for s in out["stocks"]}
    # 湖价 10.1 > 触发 10.0 但 +1%（3% 带内）→ approaching（非 active）
    assert by["sh.600001"]["price"] == 10.1
    assert by["sh.600001"]["status"] == "approaching"
