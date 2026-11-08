# -*- coding: utf-8 -*-
"""test_strategy_signals —— v7.0.19：我的持仓页「策略信号中心」。

契约（全离线，零真实网络/零生产数据）：
- martingale.signals.classify_signal 纯函数四态口径
  （active / approaching / idle / no_data；边界；approach_pct 可调）；
- martingale.engine.run_stock buys 元组 5 元素标注（k 档下标 / first_of_round），
  前 3 元语义与历史一致（f6 红线由 test_f6_regression 守护）；
- web /api/strategy_signals 端点：股票池 ∪ 账本、触发价按 in_pos 分支
  （in_pos=模拟 next 档；未 in_pos=首仓线）、**时机门**（非持仓股买入信号须
  timing.push_eligible；持仓股不受限）、**十年回测标注**（bt_pnl_pct/bt_n_buys）、
  history 仅近 90 天 + **微信推送标记**（代码|动作|档 前缀匹配去重态）、
  湖锁定降级、strategy 库名 400、空 universe。
"""
from __future__ import annotations

import json
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
import martingale.selection as sel_mod  # noqa: E402
import martingale.timing as timing_mod  # noqa: E402
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
                      # 日期落在 90 天保留窗口内（as_of 2026-11-06 → 2026-08-08）
                      {"date": "2026-10-15", "price": 10.5, "amount": 10000.0,
                       "level": 0, "first_of_round": True},
                      {"date": "2026-10-20", "price": 10.0, "amount": 11000.0,
                       "level": 1, "first_of_round": False},
                  ]},
    "sh.600002": {"name": "乙股", "last_price": 20.0, "in_pos": True, "k_level": 2,
                  "next_add_price_raw": 20.0, "first_level_raw": 30.0,
                  "end_date": "2026-11-06", "buys": []},
    "sh.600003": {"name": "丙股", "last_price": 30.5, "in_pos": True, "k_level": 0,
                  "next_add_price_raw": 30.0, "first_level_raw": 30.0,
                  "end_date": "2026-11-05",
                  "buys": [
                      {"date": "2026-10-01", "price": 31.0, "amount": 100000.0,
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
# 选股时机快照（selection_timing 口径）：600002 过提醒口径（买入信号放行），
# 600004 未过（分位/股息不达标 → 买入信号抑制，✗ 展示未达条件），600005 无行
# （fail-closed）。cond_*/pctile_window/ttm_yield_pct 为原始字段（拼未达文案用）
TIMING = {
    "sh.600001": {"ts_code": "sh.600001", "in_watchlist": True,
                  "push_eligible": False, "reasons": "分位 35% / 股息 3.0%",
                  "cond_price_low": False, "cond_yield": False,
                  "pctile_window": 35.0, "ttm_yield_pct": 3.0},
    "sh.600002": {"ts_code": "sh.600002", "in_watchlist": True,
                  "push_eligible": True, "reasons": "5年分位 21% / 股息 4.6%",
                  "cond_price_low": True, "cond_yield": True,
                  "pctile_window": 21.0, "ttm_yield_pct": 4.6},
    "sh.600003": {"ts_code": "sh.600003", "in_watchlist": True,
                  "push_eligible": False, "reasons": "分位 48% / 股息 3.8%",
                  "cond_price_low": False, "cond_yield": False,
                  "pctile_window": 48.0, "ttm_yield_pct": 3.8},
    "sh.600004": {"ts_code": "sh.600004", "in_watchlist": False,
                  "push_eligible": False, "reasons": "",
                  "cond_price_low": False, "cond_yield": False,
                  "pctile_window": 55.0, "ttm_yield_pct": 2.0},
}
# 十年回测（selection_backtest_10y 口径）
BMAP = {
    "sh.600001": {"ts_code": "sh.600001", "pnl_pct": 0.885, "n_buys": 6},
    "sh.600002": {"ts_code": "sh.600002", "pnl_pct": -0.05, "n_buys": 3},
    "sh.600004": {"ts_code": "sh.600004", "pnl_pct": 0.12, "n_buys": 2},
}
# 微信点位提醒去重态（scripts/position_alert.py 口径 key = 代码|动作|档|触发价）
PUSHED_STATE = {"alerts": {"sh.600001|add|1|10.0":
                           {"date": "2026-10-20", "at": "2026-10-20T10:00:00",
                            "price": 10.0, "name": "甲股"}}}


def _wire(monkeypatch, tmp_path, rows=ROWS, pool=POOL, user=USER, spot=SPOT,
          tmap=TIMING, bmap=BMAP, pushed=PUSHED_STATE, lake_exc=None):
    monkeypatch.setattr(wapp, "_up_read", lambda: dict(user))
    monkeypatch.setattr(timing_mod, "load_timing_map", lambda con: dict(tmap))
    monkeypatch.setattr(sel_mod, "load_backtest_map", lambda con: dict(bmap))
    # 微信推送去重态 → 临时文件（pushed=None 表示无推送历史）
    p = tmp_path / "push_state.json"
    p.write_text("{}" if pushed is None else json.dumps(pushed, ensure_ascii=False),
                 encoding="utf-8")
    monkeypatch.setattr(wapp, "PUSH_ALERT_STATE_PATH", p)

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


def test_strategy_signals_main_contract(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    out = wapp.get_strategy_signals()
    assert out["strategy"] == "current"
    assert out["lake_locked"] is False
    assert out["approach_pct"] == DEFAULT_APPROACH_PCT == 3.0
    assert out["history_window_days"] == 90
    assert out["price_source"] == "spot"
    assert out["as_of"] == "2026-11-06"
    assert out["n_stocks"] == 5  # 池 5（含湖中无数据的 600005）∪ 账本 2 ⊂ 池

    by = {s["ts_code"]: s for s in out["stocks"]}
    # 持仓股 + in_pos：next 档口径 → 9.9 ≤ 10.0 → active 加仓（持仓不受时机限制）
    s1 = by["sh.600001"]
    assert s1["held"] is True and s1["status"] == "active"
    assert s1["action"] == "add" and s1["next_add_price_raw"] == 10.0
    assert s1["suggested_amount"] == round(10000.0 * 1.1)
    assert s1["timing_ok"] is None  # 持仓股不评估时机
    assert s1["bt_pnl_pct"] == 88.5 and s1["bt_n_buys"] == 6
    # 非持仓 + in_pos + 时机过：next 档、动作统一「买入」、金额按档
    s2 = by["sh.600002"]
    assert s2["held"] is False and s2["status"] == "active"
    assert s2["action"] == "buy" and s2["next_add_price_raw"] == 20.0
    assert s2["suggested_amount"] == round(10000.0 * 1.1 ** 2)
    assert s2["timing_ok"] is True
    assert s2["bt_pnl_pct"] == -5.0 and s2["bt_n_buys"] == 3
    # 持仓股 +2%（3% 带内）→ approaching
    s3 = by["sh.600003"]
    assert s3["status"] == "approaching" and s3["distance_pct"] == pytest.approx(2.0)
    # 非持仓 + 未 in_pos + 时机未过：首仓线价格状态照展示，但买入信号被抑制
    s4 = by["sh.600004"]
    assert s4["held"] is False and s4["status"] == "active"
    assert s4["next_add_price_raw"] == 40.0  # 触发价 = 首仓线（若误走 next 35.0 可区分）
    assert s4["action"] is None and s4["suggested_amount"] is None
    assert s4["timing_ok"] is False
    # 逐条件 ✓/✗ 明细（端点由原始字段 + config 阈值拼；strategy.yaml：
    # 5 年窗口 / 分位≤40 / 股息≥4.5，③ 未启用）
    assert s4["timing_reasons"] == \
        "✗ 5年分位 55.0%（需≤40%） · ✗ 股息率 2.00%（需≥4.5%）"
    # ✓ 行同样逐条件列明（整体过 = 每条 ✓）
    assert s2["timing_reasons"] == \
        "✓ 5年分位 21.0%（需≤40%） · ✓ 股息率 4.60%（需≥4.5%）"
    # 湖中无数据 → no_data（池内非持仓）
    s5 = by["sh.600005"]
    assert s5["status"] == "no_data" and s5["held"] is False and s5["price"] is None
    assert s5["timing_ok"] is False and s5["timing_reasons"] == "无时机数据"

    t = out["totals"]
    assert t == {"n_stocks": 5, "n_active": 3, "n_approaching": 1, "n_idle": 0,
                 "n_no_data": 1, "n_active_held": 1, "n_active_unheld": 2,
                 "n_history": 3}
    # history：仅 90 天窗口内；日期降序；微信推送标记（甲股 add 第1档 已推）
    h = out["history"]
    assert [(x["date"], x["ts_code"]) for x in h] == [
        ("2026-10-20", "sh.600001"), ("2026-10-15", "sh.600001"),
        ("2026-10-01", "sh.600003"),
    ]
    assert [x["pushed"] for x in h] == [True, False, False]
    assert all(set(x.keys()) == {"ts_code", "name", "held", "date", "price",
                                 "amount", "level", "first_of_round", "pushed"}
               for x in h)
    assert h[0]["held"] is True and h[2]["held"] is True


def test_strategy_signals_history_same_day_level_order(monkeypatch, tmp_path):
    """同日同股多档：稳定排序保留档位先后（先首仓后加仓）。"""
    rows = dict(ROWS)
    rows["sh.600001"] = dict(rows["sh.600001"], buys=[
        {"date": "2026-10-15", "price": 10.5, "amount": 10000.0, "level": 0,
         "first_of_round": True},
        {"date": "2026-10-15", "price": 10.0, "amount": 11000.0, "level": 1,
         "first_of_round": False},
    ])
    _wire(monkeypatch, tmp_path, rows=rows)
    h = wapp.get_strategy_signals()["history"]
    assert [(x["ts_code"], x["level"]) for x in h if x["ts_code"] == "sh.600001"] \
        == [("sh.600001", 0), ("sh.600001", 1)]


def test_strategy_signals_history_window_90d(monkeypatch, tmp_path):
    """超出 90 天保留窗口的老触发不进 history（as_of 2026-11-06 → 2026-08-08）。"""
    rows = dict(ROWS)
    rows["sh.600002"] = dict(rows["sh.600002"], buys=[
        {"date": "2026-01-05", "price": 21.0, "amount": 10000.0, "level": 0,
         "first_of_round": True},   # 窗口外 → 过滤
        {"date": "2026-11-01", "price": 20.5, "amount": 11000.0, "level": 1,
         "first_of_round": False},  # 窗口内 → 保留
    ])
    _wire(monkeypatch, tmp_path, rows=rows)
    h = wapp.get_strategy_signals()["history"]
    assert [(x["ts_code"], x["date"]) for x in h if x["ts_code"] == "sh.600002"] \
        == [("sh.600002", "2026-11-01")]
    assert wapp.get_strategy_signals()["totals"]["n_history"] == 4  # 3 + 1


def test_strategy_signals_pushed_missing_file_ok(monkeypatch, tmp_path):
    """推送状态文件损坏/缺失 → 全部未推送，不报错。"""
    rows = dict(ROWS)
    _wire(monkeypatch, tmp_path, pushed=None)
    p = tmp_path / "push_state.json"
    p.write_text("{bad json", encoding="utf-8")
    out = wapp.get_strategy_signals()
    assert all(x["pushed"] is False for x in out["history"])


def test_strategy_signals_lake_locked_degrade(monkeypatch, tmp_path):
    """湖锁定 → 只列账本代码，全部 no_data，不 500。"""
    _wire(monkeypatch, tmp_path, lake_exc=LakeLocked)
    out = wapp.get_strategy_signals()
    assert out["lake_locked"] is True
    assert [s["ts_code"] for s in out["stocks"]] == ["sh.600001", "sh.600003"]
    assert all(s["status"] == "no_data" for s in out["stocks"])
    assert out["history"] == []
    assert out["price_source"] == "lake"
    assert out["as_of"] is None


def test_strategy_signals_empty_universe(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, pool=[], user={})
    out = wapp.get_strategy_signals()
    assert out["n_stocks"] == 0 and out["stocks"] == [] and out["history"] == []
    assert out["totals"]["n_stocks"] == 0


def test_strategy_signals_user_only_ledger_codes(monkeypatch, tmp_path):
    """账本里有池外股 → universe = 池 ∪ 账本，池外股照常出状态。"""
    user = dict(USER, **{"sz.000999": {"shares": 10.0, "cost_price": 1.0,
                                        "updated_at": "2026-11-07T00:00:00Z"}})
    rows = dict(ROWS, **{"sz.000999": {"name": "戊股", "last_price": 5.0,
                                        "in_pos": False, "k_level": 0,
                                        "next_add_price_raw": 5.0,
                                        "first_level_raw": 6.0,
                                        "end_date": "2026-11-06", "buys": []}})
    _wire(monkeypatch, tmp_path, user=user, rows=rows,
          spot={**SPOT, "sz.000999": {"price": 5.9, "timestamp": "20261107093000"}})
    out = wapp.get_strategy_signals()
    assert out["n_stocks"] == 6
    s = next(x for x in out["stocks"] if x["ts_code"] == "sz.000999")
    # 池外账本股 + 未 in_pos → 首仓线口径：5.9 ≤ 6.0 → active（若误走 next 5.0 → idle）
    assert s["held"] is True and s["status"] == "active"
    assert s["action"] == "buy" and s["next_add_price_raw"] == 6.0


def test_strategy_signals_strategy_lib_400(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
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


def test_strategy_signals_spot_fallback_lake_price(monkeypatch, tmp_path):
    """实时行情失败（spot 空）→ 回退湖最后价，price_source=lake。"""
    _wire(monkeypatch, tmp_path, spot={})
    out = wapp.get_strategy_signals()
    assert out["price_source"] == "lake"
    by = {s["ts_code"]: s for s in out["stocks"]}
    # 湖价 10.1 > 触发 10.0 但 +1%（3% 带内）→ approaching（非 active）
    assert by["sh.600001"]["price"] == 10.1
    assert by["sh.600001"]["status"] == "approaching"
