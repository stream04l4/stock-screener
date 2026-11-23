# -*- coding: utf-8 -*-
"""test_strategy_signals —— v7.0.19：我的持仓页「策略信号中心」。

契约（全离线，零真实网络/零生产数据）：
- martingale.signals.classify_signal 纯函数四态口径
  （active / approaching / idle / no_data；边界；approach_pct 可调）；
- **v8.2.3 轮次口径**（Joel 2026-11-23 定案，v8.2.0 累计差值口径退役）：
  轮次窗口 round_windows + 用户相对轮次 user_round 纯函数（单一定义点）；
  持仓行附 round_no + 多轮总理论值（首轮窗口起点起策略累计，仅展示）；
  建议量恒 = 本档手数，无「持仓已够不提醒」抑制；k=0 时机门保留；
  build_signals 对 round_no/theory/actual/gap 纯透传。
- martingale.engine.run_stock buys 元组 6 元素标注（k 档下标 / first_of_round / lots
  手数，v7.0.28），前 3 元语义与历史一致（f6 红线由 test_f6_regression 守护）；
- web /api/strategy_signals 端点：股票池 ∪ 账本、触发价（v7.0.23 定案：
  持仓股按 in_pos 分支 in_pos=next 档/未 in_pos=首仓线；**非持仓恒用首仓线**
  ——首仓 = 价跌破首仓线 且 时机✓，双条件）、**时机门**（非持仓股买入信号须
  timing.push_eligible = ① 5年分位≤40% 单条件（v7.0.24；持仓股不受限）、**十年回测标注**（bt_pnl_pct/bt_n_buys）、
  history 仅近 90 天 + **微信推送标记**（代码|动作|档 前缀匹配去重态）、
  湖锁定降级、strategy 库名 400、空 universe；
- **v7.0.20 盘中触碰**：最新行情日最低价已触线、现价回出 → status=touched
  （保留当日，次日行情日滚动后移出）；停牌股（行情日≠全池最新）不算今日触碰；
  触碰事件并入 history（同 代码|日期|档 去重 buy 优先、90 天窗口、文件缺失
  fail-safe）；martingale.touches 纯函数（quote_date_of / compute_touches /
  merge_events / dedupe_buys）。
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
from martingale import touches as touches_mod  # noqa: E402
from martingale.signals import (  # noqa: E402
    DEFAULT_APPROACH_PCT, build_signals, classify_signal, round_windows,
    user_round,
)
from martingale.data import LakeLocked  # noqa: E402

SP = SimpleNamespace(name="drawdown_martingale", lot_size=100, first_lots=1,
                     add_lots_step=1)  # v7.0.28 手数口径


# ---------------------------------------------------------------------------
# classify_signal 纯函数
# ---------------------------------------------------------------------------

def test_classify_active_held_add():
    c = classify_signal(9.9, 10.0, True, 1, SP)
    assert c["status"] == "active"
    assert c["action"] == "add"
    assert c["suggested_lots"] == 2  # 第 k=1 档 = 1 + 1×1 手
    assert c["distance_pct"] == pytest.approx(-1.0)


def test_classify_active_unheld_buy():
    c = classify_signal(20.0, 20.0, False, 0, SP)  # 触线当日（≤）也算 active
    assert c["status"] == "active"
    assert c["action"] == "buy"
    assert c["suggested_lots"] == 1  # 首仓 1 手
    assert c["distance_pct"] == 0.0


def test_classify_approaching_band_and_boundary():
    # 触发价上方 2%（默认带 5% 内）→ approaching（k=0 统一 action="buy"）
    c = classify_signal(30.6, 30.0, True, 0, SP)
    assert c["status"] == "approaching"
    assert c["action"] == "buy"  # k=0 首仓/二次建仓统一动作键 "buy"
    assert c["distance_pct"] == pytest.approx(2.0)
    # +4%（v7.0.30 放宽后入带；旧 3% 带为 idle——带宽生效断言）
    assert classify_signal(31.2, 30.0, True, 0, SP)["status"] == "approaching"
    # 恰好带边（+5%）→ 仍 approaching
    assert classify_signal(31.5, 30.0, True, 0, SP)["status"] == "approaching"
    # 刚出带（+5.01%）→ idle
    c = classify_signal(31.503, 30.0, True, 0, SP)
    assert c["status"] == "idle"
    assert c["action"] is None
    assert c["suggested_lots"] is None


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
    assert c["suggested_lots"] is None


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
                             "k_level", "next_level", "suggested_lots", "action",
                             "new_round", "in_pos", "round_no"}  # v8.2.3：轮次号
    assert a["new_round"] is False and a["in_pos"] is True
    assert a["action"] == "add" and a["next_level"] == 2
    assert a["suggested_lots"] == 2  # 第 k=1 档
    b = out[1]
    assert b["action"] == "buy" and b["next_level"] == 1 and b["suggested_lots"] == 1
    assert b["new_round"] is True and b["in_pos"] is False


def test_build_signals_k0_timing_gate():
    """v8.2.0 补（Joel 2026-11-13 二次定案「二次建仓也要符合时机门」）：
    首档（k=0）触线 = 建仓（再武装后新一轮 = 二次建仓）→ 须当前时机门通过
    才提示（fail-closed：缺条目 = 未过）；轮内加仓档（k≥1）不受约束
    （「不受约束」定案只豁免加仓档）。"""
    pos_k0 = {"ts_code": "sh.600036", "name": "中粮糖业", "spot_price": 13.0,
              "last_price": 13.2, "next_add_price_raw": 13.5, "in_pos": True,
              "k_level": 0, "shares": 400.0, "strat_shares_since": 0.0}
    pos_k1 = {"ts_code": "sh.600546", "name": "山煤国际", "spot_price": 5.0,
              "last_price": 5.2, "next_add_price_raw": 5.5, "in_pos": True,
              "k_level": 1, "shares": 100.0, "strat_shares_since": 100.0}
    # k=0 时机未过（含 fail-closed 缺条目）→ 不产信号
    assert build_signals([pos_k0], SP, timing_map={"sh.600036": False}) == []
    assert build_signals([pos_k0], SP, timing_map={}) == []
    # k=0 时机过 → 二次建仓信号（shares_since=0 不判差值 → 本档 first_lots=1 手）
    out = build_signals([pos_k0], SP, timing_map={"sh.600036": True})
    assert len(out) == 1
    assert out[0]["new_round"] is True and out[0]["in_pos"] is True
    assert out[0]["action"] == "buy" and out[0]["suggested_lots"] == 1
    # k≥1 轮内加仓不受约束：timing_map=False 也照常产信号
    out1 = build_signals([pos_k1], SP, timing_map={"sh.600546": False})
    assert len(out1) == 1 and out1[0]["new_round"] is False
    # timing_map=None → 旧行为（无时机门）
    assert len(build_signals([pos_k0], SP)) == 1


def test_held_first_timing_pool_outside_live(monkeypatch):
    """v8.2.0 补：池外持仓股（无 tmap 行，如中粮糖业）k=0 时机门 → 现算实时
    分位（与微信 position_alert 的 live_pctile_map 同口径），避免「微信能推
    而页面恒 fail-closed」的两处不一致；湖不可用 → fail-closed。"""
    # tmap 空 → 走现算分支；实时分位 35% ≤ 40 → 过（离线 mock connect_readonly，不依赖磁盘真湖）
    monkeypatch.setattr("martingale.data.connect_readonly",
                        lambda cfg=None: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr("martingale.timing.live_pctile_map",
                        lambda con, codes, prices, tp, **kw:
                        {c: 35.0 for c in codes})
    wapp._HELD_PCT_CACHE.clear()
    ok, pct, reasons = wapp._held_first_timing("sh.600737", {}, 13.0)
    assert ok is True and pct == 35.0 and "池外现算" in reasons
    # 分位 55% > 40 → 未过
    monkeypatch.setattr("martingale.timing.live_pctile_map",
                        lambda con, codes, prices, tp, **kw:
                        {c: 55.0 for c in codes})
    wapp._HELD_PCT_CACHE.clear()
    ok2, pct2, _ = wapp._held_first_timing("sh.600737", {}, 13.0)
    assert ok2 is False and pct2 == 55.0
    # 湖占用（connect_readonly 异常）→ fail-closed（不缓存失败）
    def _boom(cfg=None):
        raise RuntimeError("测试注入：湖占用")
    monkeypatch.setattr("martingale.data.connect_readonly", _boom)
    wapp._HELD_PCT_CACHE.clear()
    ok3, pct3, reasons3 = wapp._held_first_timing("sh.600737", {}, 13.0)
    assert ok3 is False and pct3 is None and "fail-closed" in reasons3


# ---------------------------------------------------------------------------
# v8.2.3 轮次口径（Joel 2026-11-23 定案；v8.2.0 累计差值口径退役）：
# - 轮次窗口 = 首轮首笔 → 轮内再武装（k≥1）切分；再武装后等待期归属下一轮
#   首仓生命周期；user_round = 用户 since_date 落入的窗口 = 首轮，其后每再
#   武装 +1；首轮窗口起点 = 多轮总理论值累计起点（仅展示）；
# - 建议量恒 = 本档手数（到线即执行），无「持仓已够不提醒」抑制——持仓信号
#   触线必提醒；k=0 首仓时机门保留（fail-closed）；
# - build_signals 纯透传：round_no/theory/actual/gap 由 web/脚本侧计算随行
#   传入，信号行原样带回（本区块含纯函数 + 透传守护）。
# 生产 sizing：first_lots=2 / add_lots_step=2 / lot_size=100（2/4/6/8 手）。
# ---------------------------------------------------------------------------

SP2 = SimpleNamespace(name="drawdown_martingale", lot_size=100, first_lots=2,
                      add_lots_step=2)


def test_round_windows_split():
    """窗口切分：首笔开窗口；轮内再武装终结前窗口并开启新窗口（等待期归下轮）；
    末窗口开放；首笔 rearm（防御性数据）= 单开放窗口。"""
    marks = [("2020-01-01", "first_buy"), ("2020-03-01", "rearm"),
             ("2020-04-01", "first_buy"), ("2020-06-01", "rearm")]
    assert round_windows(marks) == [
        ("2020-01-01", "2020-03-01"),   # 第 1 轮：被 03-01 再武装终结
        ("2020-03-01", "2020-06-01"),   # 第 2 轮：04-01 首笔落在窗口内不切分
        ("2020-06-01", None),           # 第 3 轮：开放
    ]
    assert round_windows([]) == []
    assert round_windows([("2020-06-01", "rearm")]) == [("2020-06-01", None)]


def test_user_round_window_attribution():
    """since 落入的窗口 = 用户首轮；其后每再武装 +1（Joel：再武装 → 第二轮，
    再再武装 → 第三轮）。当前轮号 = 窗口数 − 首轮窗口下标；理论起点 = 首轮
    窗口起点（只算轮号不算理论，理论由调用方按起点累计）。"""
    marks = [("2020-01-01", "first_buy"), ("2020-03-01", "rearm"),
             ("2020-04-01", "first_buy"), ("2020-06-01", "rearm")]
    # 3 个窗口；since 落 W0 → 用户已在第 3 轮（其后两次再武装）
    assert user_round(marks, "2020-02-01") == (3, "2020-01-01")
    # since 落 W1 → 第 2 轮（其后 1 次再武装）
    assert user_round(marks, "2020-05-01") == (2, "2020-03-01")
    # since 落 W2（当前开放轮）→ 第 1 轮（其后无再武装 = 首轮）
    assert user_round(marks, "2020-07-01") == (1, "2020-06-01")
    # 早于首个窗口 → 首轮 = 第 1 个窗口（当前第 3 轮）
    assert user_round(marks, "2019-12-31") == (3, "2020-01-01")
    # 边界 = 窗口起点
    assert user_round(marks, "2020-01-01") == (3, "2020-01-01")


def test_user_round_unanchored_defaults_first_round():
    """无锚点（since_date 缺失，调用方缺省=今天）→ 今天必落末开放窗口 →
    首轮 = 当前轮（起点 = 末窗口起点）；无 marks → (1, None) = 无数据。"""
    marks = [("2020-01-01", "first_buy"), ("2020-03-01", "rearm")]
    assert user_round(marks, "") == (1, "2020-03-01")
    assert user_round(marks, None) == (1, "2020-03-01")
    assert user_round([], "2020-01-01") == (1, None)


def test_user_round_daqin_style():
    """大秦铁路实案形态：marks 尾 = 当前轮首笔（轮起点 2025-11-27），用户
    since 2026-11-01 落该轮窗口 → 首轮；理论起点 = 前次再武装日 2022-01-20
    （本轮 4 笔 2+4+6+8 = 20 手由调用方按起点累计）。"""
    marks = [("2022-01-20", "rearm"), ("2025-11-27", "first_buy")]
    # 单开放窗口：since 落当前轮 → 首轮（理论起点 = 窗口起点 2022-01-20）
    assert user_round(marks, "2026-11-01") == (1, "2022-01-20")


def _dq_pos(**kw):
    # 现货 4.22 = 触线当日（≤ 线 4.22）；本档 = 2+4×2 = 10 手（SP2）
    base = {"ts_code": "sh.601006", "name": "大秦铁路", "spot_price": 4.22,
            "last_price": 4.95, "next_add_price_raw": 4.22,
            "in_pos": True, "k_level": 4}
    base.update(kw)
    return base


def test_build_signals_round_fields_passthrough():
    """v8.2.3：round_no/theory_lots/actual_lots/gap_lots 由调用方随行传入，
    build_signals 原样透传（不做差值判定）。"""
    pos = _dq_pos(round_no=1, theory_lots=20.0, actual_lots=1.0, gap_lots=19.0)
    sigs = build_signals([pos], SP2)
    assert len(sigs) == 1
    s = sigs[0]
    assert s["action"] == "add" and s["suggested_lots"] == 10  # 本档手数
    assert s["round_no"] == 1
    assert (s["theory_lots"], s["actual_lots"], s["gap_lots"]) == (20.0, 1.0, 19.0)


def test_build_signals_held_no_suppression():
    """v8.2.3：「实际≥理论 → 不提醒」抑制已退役——持仓触线必提醒（建议量 =
    本档手数，不随差值改）。"""
    pos = _dq_pos(round_no=1, theory_lots=20.0, actual_lots=100.0, gap_lots=0.0)
    sigs = build_signals([pos], SP2)
    assert len(sigs) == 1 and sigs[0]["action"] == "add"
    assert sigs[0]["suggested_lots"] == 10
    assert sigs[0]["gap_lots"] == 0.0  # 差值仅为透传展示


def test_build_signals_no_round_fields():
    """无轮次信息（湖中无数据/旧调用方）→ round_no None、无 theory 字段
    （形状守护；建议量仍是本档手数）。"""
    sigs = build_signals([_dq_pos()], SP2)
    assert len(sigs) == 1
    assert sigs[0]["round_no"] is None
    assert "theory_lots" not in sigs[0] and "gap_lots" not in sigs[0]
    assert sigs[0]["suggested_lots"] == 10


# ---------------------------------------------------------------------------
# engine buys 标注（合成数据，同 test_martingale_engine 风格）
# ---------------------------------------------------------------------------

def _dates(*iso):
    return np.array([np.datetime64(d, "D").astype("datetime64[ns]") for d in iso])


def _params(**kw):
    base = dict(name="drawdown_martingale", lot_size=100, first_lots=1,
                add_lots_step=1, first_drawdown=0.15, add_step=0.05, rearm_mode="bounce",
                rearm_pct=0.15, accounting="cash")
    base.update(kw)
    return StrategyParams(**base)


def test_buys_annotation_ladder_and_first_of_round():
    """v8.1.0 P0 锚点：H=100；bar1 80 破首仓线(85) 买 1 手（P0=80）；
    bar2 74 ≤ P0×0.95=76 买 2 手；bar3 70 ≤ P0×0.90=72 买 3 手。
    首仓当根不再同日连穿多档（档线全在 P0 下方）。"""
    p = _params()
    _stat, _state, _v, _c, _f, buys = run_stock(
        "t", _dates("2020-01-01", "2020-01-02", "2020-01-03", "2020-01-04"),
        np.array([100.0, 80.0, 74.0, 70.0]), np.array([100.0, 80.0, 74.0, 70.0]),
        None, p, verbose=False)
    assert len(buys) == 3
    assert [b[3] for b in buys] == [0, 1, 2]            # 档下标
    assert [b[4] for b in buys] == [True, False, False]  # 仅首笔 first_of_round
    assert [b[5] for b in buys] == [1, 2, 3]            # v7.0.28 手数阶梯
    # 现金支出 = 股数×价×(1+comm)
    comm = p.commission
    assert buys[0][2] == pytest.approx(100.0 * 80.0 * (1 + comm))
    assert all(b[2] == pytest.approx(b[5] * 100.0 * b[1] * (1 + comm))
               for b in buys[1:])
    # 前 3 元与历史 3 元组同语义（date, price, 现金支出）
    assert buys[0][0] == str(_dates("2020-01-02")[0])
    assert buys[0][1] == 80.0
    assert buys[0][2] == pytest.approx(100.0 * 80.0 * (1 + comm))


def test_buys_annotation_rearm_new_round_first_of_round_true():
    """v8.1.0：再武装后 k 归零 + P0 清零 → 第二轮首仓是新一轮的 k=0 档，
    first_of_round=True（S4 第二轮首仓与 S1 同一双条件、同一标注口径）。

    bar0 H=100；bar1 80：首仓 1 手（P0=80，k=1 线 76 未到）；
    bar2 100：反弹≥80×1.15=92 → rearm（k=0, P0=0）；
    bar3 80：再破首仓线 85 → 第二轮首仓 1 手（first_of_round=True）。
    """
    p = _params(rearm_pct=0.15)
    stat, _state, _v, _c, _f, buys = run_stock(
        "t", _dates("2020-01-01", "2020-01-02", "2020-01-03", "2020-01-04"),
        np.array([100.0, 80.0, 100.0, 80.0]), np.array([100.0, 80.0, 100.0, 80.0]),
        None, p, verbose=False)
    assert [b[3] for b in buys] == [0, 0]
    assert [b[4] for b in buys] == [True, True]  # v8.1.0：每轮首仓都标注
    # v7.0.28：再武装后 k 归零 → 新轮重新 1 手起算
    assert [b[5] for b in buys] == [1, 1]
    assert stat["n_rearms"] == 1  # 发生了一次再武装
    assert stat["n_rounds"] == 2  # v8.1.0：轮次 = 首仓次数


# ---------------------------------------------------------------------------
# /api/strategy_signals 端点（全 monkeypatch，零湖零网络）
# ---------------------------------------------------------------------------
USER = {
    "sh.600001": {"shares": 100.0, "cost_price": 9.0, "updated_at": "2026-11-07T00:00:00Z"},
    "sh.600003": {"shares": 50.0, "cost_price": 31.0, "updated_at": "2026-11-07T00:00:00Z"},
}
# 触发价口径（v7.0.23）：持仓股按 in_pos 分支（in_pos→next 档；未 in_pos→首仓线）；
# 非持仓股恒用 first_level_raw（首仓线）——与 held 联动，不再单纯看 in_pos
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
    # 非持仓 + 模拟已建仓（in_pos k=2）：v7.0.23 非持仓恒走首仓线 30.0（不再
    # 取 next 档 20.0）——价 20.0 ≤ 30.0 且时机✓ → active 买入·首仓（10 万）；
    # next 20.0 / first 30.0 故意不同值，口径断言可区分
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
    # 口径必须走首仓线 40.0（若误走 next 35.0：37.0 > 35×1.03 → idle，断言可区分）。
    # last_price 43.0 = 首仓线 +7.5%（v7.0.30 预筛带 ≤10% 内 → 取实时价 37.0）
    "sh.600004": {"name": "丁股", "last_price": 43.0, "in_pos": False, "k_level": 0,
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
          tmap=TIMING, bmap=BMAP, pushed=PUSHED_STATE, touch=None, lake_exc=None):
    # v8.2.0 补：替换数据源 → 失效 web 投影/行情缓存（否则上一调用按真实数据
    # 文件键缓存的 stub 值会被继续伺服，用例间互相污染）
    wapp._STATE_CACHE["key"] = None
    wapp._STATE_CACHE["value"] = None
    wapp._SPOT_CACHE["ts"] = 0.0
    wapp._SPOT_CACHE["data"] = {}
    # 审查修复（2026-11-21, e088705 跟进）：k=0 时机门改实时分位优先后，
    # fixture 用真实上市代码（sh.600004=三一重工等）会读真湖算实时分位 →
    # 环境耦合（无真湖静默全绿 / 真湖下与 fixture 价脱钩误 fail）。统一短路
    # 实时路径为空 → 回退 tmap 快照语义（本文件断言的原口径）；实时路径本身
    # 由 test_held_first_timing_pool_outside_live 离线 mock 单独覆盖。
    wapp._HELD_PCT_CACHE.clear()
    monkeypatch.setattr(wapp, "_live_pctile_cached",
                        lambda codes, price_map, tp: {})
    monkeypatch.setattr(wapp, "_up_read", lambda: dict(user))
    monkeypatch.setattr(timing_mod, "load_timing_map", lambda con: dict(tmap))
    monkeypatch.setattr(sel_mod, "load_backtest_map", lambda con: dict(bmap))
    # 微信推送去重态 → 临时文件（pushed=None 表示无推送历史）
    p = tmp_path / "push_state.json"
    p.write_text("{}" if pushed is None else json.dumps(pushed, ensure_ascii=False),
                 encoding="utf-8")
    monkeypatch.setattr(wapp, "PUSH_ALERT_STATE_PATH", p)
    # 盘中触碰事件态（v7.0.20）→ 临时文件（touch=None 表示无事件，隔离真实
    # data/alerts/signal_touch_state.json，测试 hermetic）
    tp = tmp_path / "touch_state.json"
    tp.write_text("{}" if touch is None else json.dumps(touch, ensure_ascii=False),
                  encoding="utf-8")
    monkeypatch.setattr(wapp, "TOUCH_STATE_PATH", tp)

    # _load_pool_ctx 五元组 (sp, up, tp, con, pool)：测试只用到 sp/con/pool
    def _pool_ctx(cfg=None):
        return (SP, None, None,
                SimpleNamespace(close=lambda: None),
                SimpleNamespace(codes=list(pool)))

    if lake_exc is not None:
        def _boom(cfg=None):
            raise lake_exc("测试注入：湖不可用")
        monkeypatch.setattr(wapp, "_load_pool_ctx", _boom)
    else:
        monkeypatch.setattr(wapp, "_load_pool_ctx", _pool_ctx)
    monkeypatch.setattr(
        wapp, "_strategy_state_rows",
        lambda sp, con, codes, end=None, include_buys=False, now=None, **kw:
        (lambda r: {k: dict(v, **({"buys": list(v["buys"])} if include_buys else {}))
                    for k, v in r.items()})(dict(rows)))
    monkeypatch.setattr(tencent_mod, "TencentClient",
                        lambda **kw: SimpleNamespace(
                            fetch=lambda codes: {c: spot[c] for c in codes if c in spot}))
    # Gate 3（11-10）：主源失败/空会整批切东财备源 → 屏蔽 _em_fetch（测试零网络）
    from screener.data import realtime as rt
    monkeypatch.setattr(rt, "_em_fetch", lambda codes, timeout=rt._EM_TIMEOUT: {})


def test_strategy_signals_main_contract(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    out = wapp.get_strategy_signals()
    assert out["strategy"] == "current"
    assert out["lake_locked"] is False
    assert out["approach_pct"] == DEFAULT_APPROACH_PCT == 5.0
    assert out["history_window_days"] == 90
    assert out["price_source"] == "spot"
    assert out["as_of"] == "2026-11-06"
    assert out["n_stocks"] == 5  # 池 5（含湖中无数据的 600005）∪ 账本 2 ⊂ 池

    by = {s["ts_code"]: s for s in out["stocks"]}
    # 持仓股 + in_pos：next 档口径 → 9.9 ≤ 10.0 → active 加仓（持仓不受时机限制）
    s1 = by["sh.600001"]
    assert s1["held"] is True and s1["status"] == "active"
    assert s1["action"] == "add" and s1["next_add_price_raw"] == 10.0
    assert s1["suggested_lots"] == 2  # 第 k=1 档 = 2 手
    assert s1["timing_ok"] is None  # 持仓股不评估时机
    assert s1["bt_pnl_pct"] == 88.5 and s1["bt_n_buys"] == 6
    # 非持仓 + 模拟已建仓 + 时机过（v7.0.23）：触发线 = 首仓线 30.0（非 next
    # 档 20.0），动作统一「买入」、金额 = 首仓 10 万
    s2 = by["sh.600002"]
    assert s2["held"] is False and s2["status"] == "active"
    assert s2["action"] == "buy" and s2["next_add_price_raw"] == 30.0
    assert s2["suggested_lots"] == 1  # 首仓 1 手
    assert s2["timing_ok"] is True
    assert s2["bt_pnl_pct"] == -5.0 and s2["bt_n_buys"] == 3
    # 持仓股 +2%（3% 带内）→ approaching
    s3 = by["sh.600003"]
    assert s3["status"] == "approaching" and s3["distance_pct"] == pytest.approx(2.0)
    # 非持仓 + 未 in_pos + 时机未过：首仓线价格状态照展示，但买入信号被抑制
    s4 = by["sh.600004"]
    assert s4["held"] is False and s4["status"] == "active"
    assert s4["next_add_price_raw"] == 40.0  # 触发价 = 首仓线（若误走 next 35.0 可区分）
    assert s4["action"] is None and s4["suggested_lots"] is None
    assert s4["timing_ok"] is False
    # 时机明细（v7.0.24：① 为门条件带 ✓/✗；v7.0.26：② 参考值删除；
    # strategy.yaml：5 年窗口 / 分位≤40）
    assert s4["timing_reasons"] == \
        "✗ 5年分位 55.0%（需≤40%）"
    # ✓ 行：① 过 = 门过
    assert s2["timing_reasons"] == \
        "✓ 5年分位 21.0%（需≤40%）"
    # 湖中无数据 → no_data（池内非持仓）
    s5 = by["sh.600005"]
    assert s5["status"] == "no_data" and s5["held"] is False and s5["price"] is None
    assert s5["timing_ok"] is False and s5["timing_reasons"] == "无时机数据"
    # v8.2.0 补（Joel 2026-11-21）：next_lots = 下一档手数单点原值
    #（classify_signal suggested_lots），不受时机门/持仓已够判定门控
    #（那两处只作用于 action/suggested_lots）——「即将进入」段展示列
    assert s1["next_lots"] == 2  # in_pos k=1 → first_lots + 1×add_lots_step
    assert s2["next_lots"] == 1  # 非持仓 → 首仓 first_lots
    assert s3["next_lots"] == 1  # 持仓 k=0（再武装位）→ 首仓 first_lots
    assert s4["next_lots"] == 1  # 时机门未过：suggested_lots=None 但手数不门控
    assert s5["next_lots"] is None  # no_data

    t = out["totals"]
    assert t == {"n_stocks": 5, "n_active": 3, "n_touched": 0, "n_approaching": 1,
                 "n_idle": 0, "n_no_data": 1, "n_active_held": 1,
                 "n_active_unheld": 2, "n_history": 3}
    # history：仅 90 天窗口内；日期降序；微信推送标记（甲股 add 第1档 已推）
    h = out["history"]
    assert [(x["date"], x["ts_code"]) for x in h] == [
        ("2026-10-20", "sh.600001"), ("2026-10-15", "sh.600001"),
        ("2026-10-01", "sh.600003"),
    ]
    assert [x["pushed"] for x in h] == [True, False, False]
    assert all(set(x.keys()) == {"ts_code", "name", "held", "date", "price",
                                 "amount", "lots", "level", "first_of_round", "pushed"}
               for x in h)
    assert h[0]["held"] is True and h[2]["held"] is True


def test_strategy_signals_unheld_first_level_rule(monkeypatch, tmp_path):
    """v7.0.23 非持仓首仓双条件（Joel 定案：时机成熟 且 价已跌破首仓线）：
    非持仓 + 模拟已建仓（in_pos k=3）→ 触发线恒为**首仓线**（不再 next 档）：
    - 己股 45.0 ≤ 首仓线 50.0 且时机✓ → active 买入·首仓（10 万；旧口径按
      next 档 40.0 会判 idle——可区分）；
    - 庚股 41.0 ≤ 50.0 → active（旧口径按 next 档会判 approaching——可区分）；
    - 辛股 51.5 = 首仓线 +3% 带边 → approaching（临区预告）；
    - 壬股 45.0 ≤ 50.0 但时机✗ → active 价格态照展示、无买入动作（双条件
      缺一不成信号）。
    建仓后由用户记入持仓账本 → 转持仓加仓档（本测不覆盖，持仓口径由主契约覆盖）。
    """
    def _row(code, name, price):
        return {"name": name, "last_price": price, "in_pos": True, "k_level": 3,
                "next_add_price_raw": 40.0, "first_level_raw": 50.0,
                "end_date": "2026-11-06", "buys": []}
    rows = {
        "sh.600010": _row("sh.600010", "己股", 45.0),
        "sh.600011": _row("sh.600011", "庚股", 41.0),
        "sh.600012": _row("sh.600012", "辛股", 51.5),
        "sh.600013": _row("sh.600013", "壬股", 45.0),
        # 癸股 52.0 = 首仓线 +4%：v7.0.30 的 5% 带内 → approaching（旧 3% 带
        # 为 idle——证明 yaml timing.approach_pct=5 已接入端点分类）
        "sh.600014": _row("sh.600014", "癸股", 52.0),
    }
    pool = list(rows)
    spot = {c: {"price": r["last_price"], "timestamp": "20261107093000"}
            for c, r in rows.items()}
    def _trow(code, ok):
        return {"ts_code": code, "in_watchlist": True,
                "push_eligible": ok, "reasons": "x",
                "cond_price_low": ok, "cond_yield": ok,
                "pctile_window": 30.0 if ok else 60.0,
                "ttm_yield_pct": 5.0 if ok else 3.0}
    tmap = {"sh.600010": _trow("sh.600010", True),
            "sh.600011": _trow("sh.600011", True),
            "sh.600012": _trow("sh.600012", True),
            "sh.600013": _trow("sh.600013", False),
            "sh.600014": _trow("sh.600014", True)}
    _wire(monkeypatch, tmp_path, rows=rows, pool=pool, user={}, spot=spot,
          tmap=tmap, bmap={}, pushed=None)
    out = wapp.get_strategy_signals()
    by = {s["ts_code"]: s for s in out["stocks"]}
    # 己股：价跌破首仓线 10% + 时机✓ → 首仓信号（金额 = initial 10 万，非按档）
    j = by["sh.600010"]
    assert j["status"] == "active" and j["held"] is False
    assert j["next_add_price_raw"] == 50.0  # 触发线 = 首仓线（非 next 档 40.0）
    assert j["action"] == "buy" and j["suggested_lots"] == 1
    assert j["timing_ok"] is True
    # 庚股：更深跌破 → 同样 active（跌破深度不影响信号，只影响后续加仓档）
    g = by["sh.600011"]
    assert g["status"] == "active" and g["action"] == "buy"
    # 辛股：首仓线上方 +3%（5% 带内；旧 3% 带边）→ approaching 临区预告
    x = by["sh.600012"]
    assert x["status"] == "approaching" and x["distance_pct"] == pytest.approx(3.0)
    # 癸股：+4%（5% 带内）→ approaching（旧 3% 带为 idle）
    g2 = by["sh.600014"]
    assert g2["status"] == "approaching" and g2["distance_pct"] == pytest.approx(4.0)
    assert g2["action"] == "buy" and g2["suggested_lots"] == 1
    # 壬股：价条件过、时机✗ → 无买入动作（双条件缺一不成信号）
    r_ = by["sh.600013"]
    assert r_["status"] == "active" and r_["action"] is None
    assert r_["suggested_lots"] is None and r_["timing_ok"] is False
    # 汇总：active 3（600010/11/13，其中 600013 时机✗ 仅价格态）+ approaching 2
    t = out["totals"]
    assert t["n_active"] == 3 and t["n_approaching"] == 2
    assert t["n_active_unheld"] == 3 and t["n_active_held"] == 0
    assert t["n_stocks"] == 5


def test_strategy_signals_spot_preselect_10pct(monkeypatch, tmp_path):
    """v7.0.30 实时取价预筛带（Joel 定案 10%）：取价集合 = 持仓 ∪ 湖最后收盘
    距触发线 ≤10%（含边界；线下方全部含）。>10% 的不取实时——价回退湖最后
    收盘、状态恒 idle（主板跌停制度下当天不可能触线；漏取窗口由微信推送
    通道兜底，position_alert 仍取全量）。

    - 甲 10.0 vs 首仓线 10.0（+0%）→ 取
    - 乙 11.0 vs 10.0（+10%，边界含）→ 取
    - 丙 11.1 vs 10.0（+11%）→ 不取 → 价回退 11.1（湖）→ idle
    - 丁 9.0 vs 10.0（−10%，线下方）→ 取（实时 9.9 → active）
    - 戊 20.0 vs 10.0（+100%）但**持仓** → 无条件取
    """
    def _row(code, name, last, first):
        return {"name": name, "last_price": last, "in_pos": False, "k_level": 0,
                "next_add_price_raw": first, "first_level_raw": first,
                "end_date": "2026-11-06", "buys": []}
    rows = {
        "sh.700001": _row("sh.700001", "甲股", 10.0, 10.0),
        "sh.700002": _row("sh.700002", "乙股", 11.0, 10.0),
        "sh.700003": _row("sh.700003", "丙股", 11.1, 10.0),
        "sh.700004": _row("sh.700004", "丁股", 9.0, 10.0),
        "sh.700005": _row("sh.700005", "戊股", 20.0, 10.0),
    }
    pool = list(rows)
    user = {"sh.700005": {"shares": 100.0, "cost_price": 10.0,
                          "updated_at": "2026-11-07T00:00:00Z"}}
    # 实时价故意与湖价不同值（丙股若被误取会显示 9.0 而非回退 11.1，可区分）
    spot = {
        "sh.700001": {"price": 10.2, "timestamp": "20261107093000"},
        "sh.700002": {"price": 10.9, "timestamp": "20261107093000"},
        "sh.700003": {"price": 9.0, "timestamp": "20261107093000"},
        "sh.700004": {"price": 9.9, "timestamp": "20261107093000"},
        "sh.700005": {"price": 19.0, "timestamp": "20261107093000"},
    }
    tmap = {c: {"ts_code": c, "in_watchlist": True, "push_eligible": True,
                "reasons": "x", "cond_price_low": True, "cond_yield": True,
                "pctile_window": 30.0, "ttm_yield_pct": 5.0} for c in rows}
    _wire(monkeypatch, tmp_path, rows=rows, pool=pool, user=user, spot=spot,
          tmap=tmap, bmap={}, pushed=None)
    # 捕获 fetch 实参（_wire 已装 TencentClient mock → 覆盖为记录版）
    fetched = []
    def _fake_tencent(**kw):
        return SimpleNamespace(
            fetch=lambda codes: (fetched.extend(codes),
                                 {c: spot[c] for c in codes if c in spot})[1])
    monkeypatch.setattr(tencent_mod, "TencentClient", _fake_tencent)

    out = wapp.get_strategy_signals()
    # 取价集合 = 甲/乙/丁/戊（丙 +11% 被预筛掉；戊持仓无条件取）
    assert sorted(fetched) == ["sh.700001", "sh.700002", "sh.700004", "sh.700005"]
    assert out["spot_fetched"] == 4 and out["n_stocks"] == 5
    by = {s["ts_code"]: s for s in out["stocks"]}
    # 丙股：未取实时 → 价回退湖最后收盘 11.1，+11% 出 5% 展示带 → idle
    c = by["sh.700003"]
    assert c["price"] == 11.1 and c["status"] == "idle"
    # 丁股：线下方取实时 9.9 ≤ 10.0 → active
    d = by["sh.700004"]
    assert d["price"] == 9.9 and d["status"] == "active"
    # 戊股：持仓无条件取实时 19.0（vs 首仓线 10.0，+90% → idle 但价是实时的）
    e = by["sh.700005"]
    assert e["price"] == 19.0 and e["held"] is True
    # 乙股：+10% 边界取到实时 10.9（+9.1% > 5% 带 → idle；仅证取价边界含）
    b = by["sh.700002"]
    assert b["price"] == 10.9 and b["status"] == "idle"


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
    """账本里有池外股 → universe = 池 ∪ 账本，池外股照常出状态。

    v8.2.0 补：池外持仓股首档（k=0）触线也须过时机门 → 给该码时机行（分位
    20% ✓）；池外且无时机行 → fail-closed 不提醒（设计行为）。"""
    user = dict(USER, **{"sz.000999": {"shares": 10.0, "cost_price": 1.0,
                                        "updated_at": "2026-11-07T00:00:00Z"}})
    rows = dict(ROWS, **{"sz.000999": {"name": "戊股", "last_price": 5.0,
                                       "in_pos": False, "k_level": 0,
                                       "next_add_price_raw": 5.0,
                                       "first_level_raw": 6.0,
                                       "end_date": "2026-11-06", "buys": []}})
    tmap = {**TIMING, "sz.000999": {"ts_code": "sz.000999", "in_watchlist": False,
                                    "push_eligible": True, "reasons": "5年分位 20%",
                                    "cond_price_low": True, "cond_yield": True,
                                    "pctile_window": 20.0, "ttm_yield_pct": 4.0}}
    _wire(monkeypatch, tmp_path, user=user, rows=rows, tmap=tmap,
          spot={**SPOT, "sz.000999": {"price": 5.9, "timestamp": "20261107093000"}})
    out = wapp.get_strategy_signals()
    assert out["n_stocks"] == 6
    s = next(x for x in out["stocks"] if x["ts_code"] == "sz.000999")
    # 池外账本股 + 未 in_pos → 首仓线口径：5.9 ≤ 6.0 → active（若误走 next 5.0 → idle）
    assert s["held"] is True and s["status"] == "active"
    assert s["action"] == "buy" and s["next_add_price_raw"] == 6.0


def test_strategy_signals_current_strategy_only(monkeypatch, tmp_path):
    """v7.0.29 策略库退役：无 strategy 参数；_load_pool_ctx 恒缺省；响应 strategy='current'。"""
    _wire(monkeypatch, tmp_path)
    seen = {}

    def _ctx(cfg_path=None):
        seen["cfg"] = cfg_path
        return (SP, None, None,
                SimpleNamespace(close=lambda: None),
                SimpleNamespace(codes=list(POOL)))

    monkeypatch.setattr(wapp, "_load_pool_ctx", _ctx)
    out = wapp.get_strategy_signals()
    assert out["strategy"] == "current"
    assert seen["cfg"] is None


def test_strategy_signals_spot_fallback_lake_price(monkeypatch, tmp_path):
    """实时行情失败（spot 空）→ 回退湖最后价，price_source=lake。"""
    _wire(monkeypatch, tmp_path, spot={})
    out = wapp.get_strategy_signals()
    assert out["price_source"] == "lake"
    by = {s["ts_code"]: s for s in out["stocks"]}
    # 湖价 10.1 > 触发 10.0 但 +1%（3% 带内）→ approaching（非 active）
    assert by["sh.600001"]["price"] == 10.1
    assert by["sh.600001"]["status"] == "approaching"


# ---------------------------------------------------------------------------
# v8.2.3 持仓股轮次显示（Joel 2026-11-23：三处信号一致；v8.2.0 锚定判定退役）
# ---------------------------------------------------------------------------

def test_strategy_signals_held_round_display(monkeypatch, tmp_path):
    """持仓股行附用户相对轮次 + 多轮总理论值（仅展示）+ 首档时机门：
    - round_no = user_round（since_date 落点窗口 = 首轮，其后再武装 +1）；
    - 理论手数 = 首轮窗口起点起策略 buys 累计（湖中无数据 → None）；
    - 无「持仓已够 → 不提醒」抑制：触线必提醒（建议 = 本档手数）；
    - k=0 首仓时机门保留（fail-closed：无时机行 = 未过）；
    - 非持仓股完全不受影响（正常逻辑 + 时机门）。"""
    rows = {
        # A k=1 轮内加仓档 + 无轮次信息（湖中无 round_marks）→ 正常加仓信号，
        #    round_no None、无理论字段
        "sh.600101": {"name": "甲", "last_price": 9.9, "in_pos": True, "k_level": 1,
                      "next_add_price_raw": 10.0, "first_level_raw": 12.0,
                      "end_date": "2026-11-06", "buys": []},
        # B k=0 时机✓ + since 2026-10-15 落 10-01 首笔窗口 → 首轮：建仓信号
        #    不抑制（v8.2.3 退役「恰好够不提醒」），round_no=1，理论 = 10-05
        #    买入 100 股 = 1 手，实际 1 手，差 0
        "sh.600102": {"name": "乙", "last_price": 29.0, "in_pos": True, "k_level": 0,
                      "next_add_price_raw": 30.0, "first_level_raw": 30.0,
                      "end_date": "2026-11-06",
                      "round_marks": [("2026-10-01", "first_buy")],
                      "buys": [{"date": "2026-10-05", "price": 28.0, "amount": 100.0,
                                "level": 0, "first_of_round": True, "lots": 1}]},
        # C k=2 + since 09-05 落第 1 窗口 → 用户已历 3 轮（10-10/10-25 两次
        #    再武装）→ round_no=3；多轮总理论 = 首轮起点 09-01 起累计两笔
        #    = 2 手（「策略理论手数按照多轮的总理论值」核心断言）
        "sh.600103": {"name": "丙", "last_price": 19.0, "in_pos": True, "k_level": 2,
                      "next_add_price_raw": 20.0, "first_level_raw": 22.0,
                      "end_date": "2026-11-06",
                      "round_marks": [("2026-09-01", "first_buy"),
                                      ("2026-10-10", "rearm"),
                                      ("2026-10-12", "first_buy"),
                                      ("2026-10-25", "rearm")],
                      "buys": [
                          {"date": "2026-09-02", "price": 21.0, "amount": 100.0,
                           "level": 0, "first_of_round": True, "lots": 1},
                          {"date": "2026-10-26", "price": 19.5, "amount": 100.0,
                           "level": 0, "first_of_round": True, "lots": 1}]},
        # D 非持仓 + 时机✓：正常逻辑买入·首仓 1 手（round_no None、无理论）
        "sh.600104": {"name": "丁", "last_price": 24.5, "in_pos": True, "k_level": 2,
                      "next_add_price_raw": 24.0, "first_level_raw": 25.0,
                      "end_date": "2026-11-06", "buys": []},
        # E k=0 再武装后新一轮 + 时机未过（无时机行 → fail-closed）→ 不提醒；
        #    轮次字段照附（round_no=1；buys 空 → 理论 0 手）
        "sh.600105": {"name": "戊", "last_price": 15.0, "in_pos": True, "k_level": 0,
                      "next_add_price_raw": 16.0, "first_level_raw": 16.0,
                      "end_date": "2026-11-06",
                      "round_marks": [("2026-10-01", "first_buy")], "buys": []},
    }
    user = {
        "sh.600101": {"shares": 300.0, "cost_price": 10.0,
                      "since_date": "2026-11-01",
                      "updated_at": "2026-11-07T00:00:00Z"},
        "sh.600102": {"shares": 100.0, "cost_price": 29.0,
                      "since_date": "2026-10-15",
                      "updated_at": "2026-11-07T00:00:00Z"},
        "sh.600103": {"shares": 100.0, "cost_price": 19.0,
                      "since_date": "2026-09-05",
                      "updated_at": "2026-11-07T00:00:00Z"},
        "sh.600105": {"shares": 100.0, "cost_price": 15.0,
                      "since_date": "2026-10-05",
                      "updated_at": "2026-11-07T00:00:00Z"},
    }
    spot = {c: {"price": rows[c]["last_price"], "timestamp": "20261107093000"}
            for c in rows}
    tmap = {"sh.600102": {"ts_code": "sh.600102", "push_eligible": True,
                          "cond_price_low": True, "pctile_window": 25.0},
            "sh.600104": {"ts_code": "sh.600104", "push_eligible": True,
                          "cond_price_low": True, "pctile_window": 20.0}}
    _wire(monkeypatch, tmp_path, rows=rows, pool=list(rows), user=user,
          spot=spot, tmap=tmap)
    out = wapp.get_strategy_signals()
    by = {s["ts_code"]: s for s in out["stocks"]}
    # A：轮内加仓档 + 无轮次信息 → 正常加仓本档 2 手，round_no/theory 皆 None
    assert by["sh.600101"]["status"] == "active"
    assert by["sh.600101"]["action"] == "add"
    assert by["sh.600101"]["suggested_lots"] == 2
    assert by["sh.600101"]["round_no"] is None
    assert by["sh.600101"]["theory_lots"] is None
    # B：k=0 时机✓ → 建仓信号不抑制（v8.2.3 核心：触线必提醒），首轮，
    #    理论 1 / 实际 1 / 差 0 仅展示
    assert by["sh.600102"]["action"] == "buy" and by["sh.600102"]["suggested_lots"] == 1
    assert by["sh.600102"]["new_round"] is True and by["sh.600102"]["in_pos"] is True
    assert by["sh.600102"]["timing_ok"] is True
    assert by["sh.600102"]["round_no"] == 1
    assert (by["sh.600102"]["theory_lots"], by["sh.600102"]["actual_lots"],
            by["sh.600102"]["gap_lots"]) == (1.0, 1.0, 0.0)
    # C：用户第 3 轮；本档 1+2×1=3 手；多轮总理论 = 首轮起点 09-01 起累计
    #    两笔 = 2 手（实际 1 手 → 差 1）
    assert by["sh.600103"]["action"] == "add" and by["sh.600103"]["suggested_lots"] == 3
    assert by["sh.600103"]["round_no"] == 3
    assert by["sh.600103"]["theory_lots"] == 2.0
    assert by["sh.600103"]["actual_lots"] == 1.0
    assert by["sh.600103"]["gap_lots"] == 1.0
    # D：非持仓正常逻辑（时机✓ → 买入·首仓 1 手）
    assert by["sh.600104"]["action"] == "buy" and by["sh.600104"]["suggested_lots"] == 1
    assert by["sh.600104"]["round_no"] is None
    assert by["sh.600104"]["theory_lots"] is None
    # E：k=0 时机未过（fail-closed）→ 不提醒；轮次字段照附
    assert by["sh.600105"]["action"] is None and by["sh.600105"]["new_round"] is False
    assert by["sh.600105"]["timing_ok"] is False
    assert by["sh.600105"]["round_no"] == 1
    assert (by["sh.600105"]["theory_lots"], by["sh.600105"]["actual_lots"],
            by["sh.600105"]["gap_lots"]) == (0.0, 1.0, 0.0)


# ---------------------------------------------------------------------------
# v7.0.20 盘中触碰（touched）
# ---------------------------------------------------------------------------

def test_strategy_signals_touch_current_retained(monkeypatch, tmp_path):
    """盘中触线又反弹：600003 现价 30.6（+2% approaching）但当日最低 29.9 已摸过
    触发线 30.0（行情日=全池最新）→ status=touched 保留在当前段，动作/金额按
    active 口径补齐（持仓 in_pos k=0 二次建仓 → 须时机✓，动作 1 手）。"""
    spot = {**SPOT, "sh.600003": {"price": 30.6, "low": 29.9,
                                  "timestamp": "20261107093000"}}
    # v8.2.0 补：首档（k=0 = 二次建仓）须当前时机门过 → 600003 改判时机✓
    tmap = {**TIMING, "sh.600003": {"ts_code": "sh.600003", "in_watchlist": True,
                                    "push_eligible": True,
                                    "reasons": "5年分位 28%",
                                    "cond_price_low": True, "cond_yield": True,
                                    "pctile_window": 28.0, "ttm_yield_pct": 4.6}}
    _wire(monkeypatch, tmp_path, spot=spot, tmap=tmap)
    out = wapp.get_strategy_signals()
    s3 = next(x for x in out["stocks"] if x["ts_code"] == "sh.600003")
    assert s3["status"] == "touched" and s3["touched"] is True
    assert s3["day_low"] == 29.9
    assert s3["action"] == "buy" and s3["suggested_lots"] == 1  # 第 k=0 档（二次建仓统一 buy）
    assert s3["distance_pct"] == pytest.approx(2.0)  # 价格状态仍如实展示
    t = out["totals"]
    assert t["n_touched"] == 1 and t["n_approaching"] == 0 and t["n_active"] == 3


def test_strategy_signals_touch_stale_quote_date_ignored(monkeypatch, tmp_path):
    """停牌股行情停在旧日：day_low 是旧日数据，不得算「今日触碰」。
    600003 行情日 11-06（全池最新 11-07）+ 旧日低 29.9 触线 → 不标 touched，
    保持 approaching。"""
    spot = {**SPOT, "sh.600003": {"price": 30.6, "low": 29.9,
                                  "timestamp": "20261106150000"}}
    _wire(monkeypatch, tmp_path, spot=spot)
    out = wapp.get_strategy_signals()
    s3 = next(x for x in out["stocks"] if x["ts_code"] == "sh.600003")
    assert s3["status"] == "approaching" and s3["touched"] is False
    assert out["totals"]["n_touched"] == 0


def test_strategy_signals_touch_unheld_timing_gate(monkeypatch, tmp_path):
    """非持仓股触碰后仍受时机门约束：600004 未 in_pos，首仓线 40.0，现价 41.0
    （+2.5% approaching）当日低 39.9 触线 → touched；时机未过 → 无动作；
    时机过 → 动作补「买入」+ 首仓金额。"""
    spot = {**SPOT, "sh.600004": {"price": 41.0, "low": 39.9,
                                  "timestamp": "20261107093000"}}
    _wire(monkeypatch, tmp_path, spot=spot)
    out = wapp.get_strategy_signals()
    s4 = next(x for x in out["stocks"] if x["ts_code"] == "sh.600004")
    assert s4["status"] == "touched" and s4["touched"] is True
    assert s4["timing_ok"] is False
    assert s4["action"] is None and s4["suggested_lots"] is None
    # 时机过 → 放行（买入 + 首仓 10 万）
    tmap_ok = dict(TIMING, **{"sh.600004": dict(TIMING["sh.600004"],
                                                push_eligible=True)})
    _wire(monkeypatch, tmp_path, spot=spot, tmap=tmap_ok)
    out = wapp.get_strategy_signals()
    s4 = next(x for x in out["stocks"] if x["ts_code"] == "sh.600004")
    assert s4["action"] == "buy" and s4["suggested_lots"] == 1


def test_strategy_signals_touch_history_merge_dedupe_window(monkeypatch, tmp_path):
    """触碰事件并入历史：窗口内保留（source=touch）；同 (代码,日期,档) 与回放
    buy 去重（buy 优先——600001 2026-10-20 第1档回放已记）；窗口外清掉。"""
    touch = {"alerts": {
        "sh.600002": {"ts_code": "sh.600002", "name": "乙股", "date": "2026-11-04",
                      "level": 2, "first_of_round": False, "level_price": 19.5,
                      "amount": 12100.0, "low_price": 19.4, "price": 20.0},
        "sh.600001": {"ts_code": "sh.600001", "name": "甲股", "date": "2026-10-20",
                      "level": 1, "first_of_round": False, "level_price": 10.0,
                      "amount": 11000.0, "low_price": 9.98, "price": 10.1},
        "sh.600003": {"ts_code": "sh.600003", "name": "丙股", "date": "2026-01-01",
                      "level": 0, "first_of_round": True, "level_price": 31.0,
                      "amount": 100000.0, "low_price": 30.9, "price": 31.5},
    }}
    _wire(monkeypatch, tmp_path, touch=touch)
    out = wapp.get_strategy_signals()
    h = out["history"]
    assert [(x["ts_code"], x["date"]) for x in h] == [
        ("sh.600002", "2026-11-04"),   # touch 事件（窗口内）
        ("sh.600001", "2026-10-20"),   # 回放 buy（touch 同键被去重）
        ("sh.600001", "2026-10-15"),   # 回放 buy
        ("sh.600003", "2026-10-01"),   # 回放 buy（1-01 touch 窗口外被滤）
    ]
    assert h[0]["source"] == "touch"
    assert h[0]["price"] == 19.5 and h[0]["level"] == 2 and h[0]["first_of_round"] is False
    assert "source" not in h[1]  # buy 行无 source 字段
    assert out["totals"]["n_history"] == 4


def test_strategy_signals_touch_corrupt_file_ok(monkeypatch, tmp_path):
    """触碰事件文件损坏 → 按无事件处理，不 500。"""
    _wire(monkeypatch, tmp_path)
    (tmp_path / "touch_state.json").write_text("{bad json", encoding="utf-8")
    out = wapp.get_strategy_signals()
    assert [(x["ts_code"], x["date"]) for x in out["history"]] == [
        ("sh.600001", "2026-10-20"), ("sh.600001", "2026-10-15"),
        ("sh.600003", "2026-10-01"),
    ]  # 仅回放 buys，无 touch 行
    assert out["totals"]["n_history"] == 3
    assert all(x["touched"] is False for x in out["stocks"])


# ---------------------------------------------------------------------------
# martingale.touches 纯函数
# ---------------------------------------------------------------------------

def test_touches_quote_date_of():
    assert touches_mod.quote_date_of("20261107093000") == "2026-11-07"
    assert touches_mod.quote_date_of("20261107") == "2026-11-07"
    assert touches_mod.quote_date_of(None) is None
    assert touches_mod.quote_date_of("bad") is None
    assert touches_mod.quote_date_of("202611071") is None


def test_touches_compute_fail_closed_and_event_shape():
    """无低/无触发线 → 不产事件（fail-closed）；事件档位/金额/首笔口径
    （v7.0.23，held 维度）：
    - 持仓股 in_pos → 加仓口径（next 档线，base×growth^k，第 k 档）；
    - 非持仓（held_codes 外）→ 首仓口径（首仓线，initial，level=0 首笔）。
    """
    rows = {
        "a": {"name": "A", "in_pos": True, "k_level": 1,
              "next_add_price_raw": 10.0, "first_level_raw": 12.0},
        "b": {"name": "B", "in_pos": False, "k_level": 0,
              "next_add_price_raw": 35.0, "first_level_raw": 40.0},
        "c": {"name": "C", "in_pos": True, "k_level": 0,
              "next_add_price_raw": 30.0, "first_level_raw": 30.0},
        "d": {"name": "D", "in_pos": None, "k_level": None,
              "next_add_price_raw": None, "first_level_raw": None},
    }
    spot = {
        "a": {"price": 10.5, "low": 9.9, "timestamp": "20261106150000"},
        "b": {"price": 41.0, "low": 40.0, "timestamp": "20261106150000"},
        "c": {"price": 31.0, "timestamp": "20261106150000"},  # 无 low
        "d": {"price": 5.0, "low": 4.9, "timestamp": "20261106150000"},
    }
    # 默认 held_codes 空（全非持仓）：a 走首仓线 12.0（9.9 触线）→ 首仓事件
    evs = touches_mod.compute_touches(rows, spot, SP)
    by = {e["ts_code"]: e for e in evs}
    assert set(by) == {"a", "b"}
    assert by["a"]["date"] == "2026-11-06" and by["a"]["level"] == 0
    assert by["a"]["first_of_round"] is True
    assert by["a"]["level_price"] == 12.0  # 非持仓 = 首仓线（非 next 档 10.0）
    assert by["a"]["lots"] == 1            # 首仓 1 手（v7.0.28 手数口径）
    assert by["b"]["level"] == 0 and by["b"]["first_of_round"] is True
    assert by["b"]["lots"] == 1 and by["b"]["low_price"] == 40.0
    # a 属持仓 → 加仓口径：next 档 10.0（9.9 触线）、第 1 档、base×growth^1
    evs_held = touches_mod.compute_touches(rows, spot, SP, held_codes={"a"})
    by_held = {e["ts_code"]: e for e in evs_held}
    assert set(by_held) == {"a", "b"}
    assert by_held["a"]["level"] == 1 and by_held["a"]["first_of_round"] is False
    assert by_held["a"]["level_price"] == 10.0
    assert by_held["a"]["lots"] == 2  # 第 k=1 档 = 1 + 1×1 手
    # b 仍非持仓 → 口径不变
    assert by_held["b"] == by["b"]


def test_touches_merge_idempotent_and_window():
    """同日重跑覆盖（幂等）；窗口外的老事件按全量最大日期清掉。"""
    existing = {
        "a": {"ts_code": "a", "date": "2026-01-01", "level": 0, "name": "A"},
        "b": {"ts_code": "b", "date": "2026-10-01", "level": 0, "name": "B"},
    }
    new = [
        {"ts_code": "a", "date": "2026-11-06", "level": 1, "name": "A"},
        {"ts_code": "c", "date": "2026-11-06", "level": 0, "name": "C"},
    ]
    merged = touches_mod.merge_events(existing, new)
    assert set(merged) == {"a|2026-11-06", "b|2026-10-01", "c|2026-11-06"}
    assert merged["a|2026-11-06"]["date"] == "2026-11-06"
    # 同股不同日共存（窗口内保留历史触碰）
    merged2 = touches_mod.merge_events(
        merged, [{"ts_code": "a", "date": "2026-11-07", "level": 1, "name": "A"}])
    assert {"a|2026-11-06", "a|2026-11-07"}.issubset(set(merged2))
    # 最大日期 11-06 − 90 天 = 2026-08-08 → b(10-01) 在窗内保留，a(01-01) 窗口外淘汰
    assert all(e["date"] >= "2026-08-08" for e in merged.values())
    # 幂等：同一批再合并不变
    assert touches_mod.merge_events(merged, new) == merged


def test_touches_dedupe_buys_buy_wins():
    h = [
        {"ts_code": "a", "date": "2026-11-04", "level": 2, "source": "touch"},
        {"ts_code": "a", "date": "2026-11-04", "level": 2, "source": "buy"},
        {"ts_code": "a", "date": "2026-11-05", "level": 3, "source": "touch"},
        {"ts_code": "b", "date": "2026-11-04", "level": 0},  # buy 行无 source
    ]
    out = touches_mod.dedupe_buys(h)
    assert len(out) == 3
    kept = {(x["ts_code"], x["date"], x["level"]): x.get("source") for x in out}
    assert kept[("a", "2026-11-04", 2)] == "buy"  # 同键 buy 优先
    assert kept[("a", "2026-11-05", 3)] == "touch"
    assert kept[("b", "2026-11-04", 0)] is None


def test_touches_load_state_fail_safe(tmp_path):
    p = tmp_path / "nope.json"
    assert touches_mod.load_state(str(p)) == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{bad", encoding="utf-8")
    assert touches_mod.load_state(str(bad)) == {}
    ok = tmp_path / "ok.json"
    ok.write_text(json.dumps({"alerts": {"a": {"date": "2026-11-06"},
                                         "b": "corrupt-value"}}),
                  encoding="utf-8")
    state = touches_mod.load_state(str(ok))
    assert set(state) == {"a"}  # 非 dict 值被过滤
