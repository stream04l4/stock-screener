# -*- coding: utf-8 -*-
"""test_position_alert —— v7.0.11：持仓策略点位微信提醒。

契约（全离线，零真实网络/零生产数据）：
- martingale.signals.build_signals 纯函数口径（add/buy/不触发/排序）；
- position_alert.run() 管道：账本→湖状态机行→实时价→信号→去重→webhook→状态落盘；
  账本/湖/腾讯/webhook 全部 monkeypatch 替身；
- 去重：同日同点位不重推；隔日可再推；推送失败不落状态（可重试）；
- 退出码：0 正常 / 2 缺 key / 3 湖不可用 / 4 推送失败。
"""
from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import position_alert as pa  # noqa: E402
from martingale.signals import build_signals  # noqa: E402

FAKE_NOW = "2026-10-26T14:30:00+08:00"   # 周一 14:30 北京（交易时段内）
USER = {
    "sh.601728": {"shares": 500.0, "cost_price": 6.0, "updated_at": FAKE_NOW},
    "sh.601398": {"shares": 1000.0, "cost_price": 8.0, "updated_at": FAKE_NOW},
}
# 601728 触发（5.98 ≤ 6.00，in_pos k=1）；601398 未触发（8.50 > 8.00）
ROWS = {
    "sh.601728": {"name": "中国神华", "last_price": 6.02, "in_pos": True,
                  "k_level": 1, "next_add_price_raw": 6.0},
    "sh.601398": {"name": "工商银行", "last_price": 8.55, "in_pos": True,
                  "k_level": 0, "next_add_price_raw": 8.0},
}
SP = SimpleNamespace(name="drawdown_martingale", lot_size=100, first_lots=1,
                     add_lots_step=1)  # v7.0.28 手数口径


def _wire(monkeypatch, tmp_path, spot=None, lake_rc="ok", posts=None,
          user=None, pool_codes=None, rows=None, tmap=None):
    """全链路替身：web.app 三函数 + 时机表 + 腾讯 + requests.post + 时钟 + .env key。"""
    import web.app as wapp

    user = dict(USER) if user is None else user
    pool_codes = list(pool_codes) if pool_codes is not None else ["sh.601728", "sh.601398"]
    rows = dict(ROWS) if rows is None else rows
    tmap = {} if tmap is None else tmap
    monkeypatch.setattr(wapp, "_up_read", lambda: user)
    con = SimpleNamespace(close=lambda: None)
    pool = SimpleNamespace(codes=pool_codes)
    if lake_rc == "ok":
        monkeypatch.setattr(wapp, "_load_pool_ctx",
                            lambda cfg_path=None: (SP, None, con, pool))
        monkeypatch.setattr(wapp, "_strategy_state_rows",
                            lambda sp, con2, codes: dict(rows))
        import martingale.timing as tmod

        monkeypatch.setattr(tmod, "load_timing_map", lambda con2: dict(tmap))
    else:
        def _boom(cfg_path=None):
            raise RuntimeError("数据湖被其他进程锁定（测试注入）")

        monkeypatch.setattr(wapp, "_load_pool_ctx", _boom)

    spot = spot if spot is not None else {
        "sh.601728": {"price": 5.98, "timestamp": "14:30:01"},
        "sh.601398": {"price": 8.50, "timestamp": "14:30:01"},
    }
    import screener.data.tencent as tq

    monkeypatch.setattr(tq, "TencentClient",
                        lambda **k: SimpleNamespace(fetch=lambda cs: dict(spot)))

    posts = posts if posts is not None else []

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"code": 200, "msg": "success", "data": "test-uuid"}

    def fake_post(url, json=None, timeout=None):  # noqa: A002 - 镜像 requests 签名
        posts.append({"url": url, "json": json})
        return _Resp()

    import requests

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(pa, "_beijing_now",
                        lambda: __import__("datetime").datetime.fromisoformat(FAKE_NOW))
    # .env key：monkeypatch 读取器（不碰真实 .env）
    monkeypatch.setattr(pa, "_load_env_key",
                        lambda name: "test-key-123" if name == "PUSHPLUS_TOKEN" else None)
    state_path = str(tmp_path / "state.json")
    return posts, state_path


# ---------------------------------------------------------------------------
# 1) build_signals 纯函数口径
# ---------------------------------------------------------------------------
def test_build_signals_add_buy_and_none():
    sp = SP
    positions = [
        {"ts_code": "a.1", "name": "A", "spot_price": 5.98, "last_price": 6.0,
         "in_pos": True, "k_level": 1, "next_add_price_raw": 6.0},
        {"ts_code": "b.2", "name": "B", "spot_price": None, "last_price": 5.0,
         "in_pos": False, "k_level": 0, "next_add_price_raw": 5.0},   # 实时缺失→湖价触发
        {"ts_code": "c.3", "name": "C", "spot_price": 9.0, "last_price": 9.0,
         "in_pos": True, "k_level": 0, "next_add_price_raw": 8.0},    # 高于线→不触发
        {"ts_code": "d.4", "name": "D", "spot_price": 7.0, "last_price": 7.0,
         "in_pos": False, "k_level": 0, "next_add_price_raw": None},  # 无点位→不触发
    ]
    sig = build_signals(positions, sp)
    assert [s["ts_code"] for s in sig] == ["a.1", "b.2"]
    a = sig[0]
    assert (a["action"], a["next_level"]) == ("add", 2)
    assert a["suggested_lots"] == 2                # 第 k=1 档 = 1+1×1 手
    b = sig[1]
    assert (b["action"], b["next_level"]) == ("buy", 1)
    assert b["suggested_lots"] == 1                # 首仓 1 手
    # sp 缺省 → 手数 None 不报错
    sig2 = build_signals(positions, None)
    assert sig2[0]["suggested_lots"] is None


# ---------------------------------------------------------------------------
# 2) run() 管道
# ---------------------------------------------------------------------------
def test_run_pushes_once_and_dedupes_same_day(monkeypatch, tmp_path):
    posts, state_path = _wire(monkeypatch, tmp_path)
    rc = pa.run(force=True, state_path=state_path)
    assert rc == 0
    assert len(posts) == 1, posts
    body = posts[0]["json"]
    assert posts[0]["url"].endswith("/send")          # pushplus 发送接口
    assert body["token"] == "test-key-123"
    assert body["template"] == "html"
    content = body["content"]
    assert "中国神华" in content and "sh.601728" in content
    assert "5.980" in content and "6.000" in content   # 现价/触发价
    assert "2 手" in content                           # 第 k=1 档 = 2 手（v7.0.28）
    assert "工商银行" not in content                    # 未触发不出现
    assert "中国神华" in body["title"]

    # 状态落盘（键含 点位+k+动作）
    st = json.load(open(state_path, encoding="utf-8"))
    assert "sh.601728|add|1|6.0" in st["alerts"]

    # 同日第二轮 → 去重不重推
    rc2 = pa.run(force=True, state_path=state_path)
    assert rc2 == 0
    assert len(posts) == 1, "同日同点位不得重推"


def test_run_repushes_next_day(monkeypatch, tmp_path):
    posts, state_path = _wire(monkeypatch, tmp_path)
    assert pa.run(force=True, state_path=state_path) == 0
    assert len(posts) == 1
    # 次日（点位未变）→ 可再推一次（每日提醒语义）
    import datetime as dt

    next_day = dt.datetime.fromisoformat(FAKE_NOW) + dt.timedelta(days=1)
    monkeypatch.setattr(pa, "_beijing_now", lambda: next_day)
    assert pa.run(force=True, state_path=state_path) == 0
    assert len(posts) == 2, "隔日同点位应再推（每日一次提醒）"


def test_run_push_failure_no_state_writable(monkeypatch, tmp_path):
    posts, state_path = _wire(monkeypatch, tmp_path)

    import requests

    def _boom(url, json=None, timeout=None):  # noqa: A002
        raise RuntimeError("webhook 503（测试注入）")

    monkeypatch.setattr(requests, "post", _boom)
    rc = pa.run(force=True, state_path=state_path)
    assert rc == 4, "推送失败退出码 4"
    assert not os.path.exists(state_path), "推送失败不得落状态（下轮重试）"


def test_run_lake_unavailable_rc3(monkeypatch, tmp_path):
    posts, state_path = _wire(monkeypatch, tmp_path, lake_rc="locked")
    rc = pa.run(force=True, state_path=state_path)
    assert rc == 3, "湖不可用退出码 3（下轮重试）"
    assert posts == []


def test_run_no_trigger_no_push(monkeypatch, tmp_path):
    posts, state_path = _wire(monkeypatch, tmp_path,
                              spot={"sh.601728": {"price": 6.50, "timestamp": "14:30:01"},
                                    "sh.601398": {"price": 8.50, "timestamp": "14:30:01"}})
    rc = pa.run(force=True, state_path=state_path)
    assert rc == 0
    assert posts == [], "无触发不推送"
    assert not os.path.exists(state_path)


def test_run_spot_failure_falls_back_to_lake_price(monkeypatch, tmp_path):
    """实时行情全失败 → 回退湖最后价（只漏报不误报）：湖价 6.02 > 6.0 不触发。"""
    posts, state_path = _wire(monkeypatch, tmp_path, spot={})
    rc = pa.run(force=True, state_path=state_path)
    assert rc == 0
    assert posts == [], "湖价高于线 → 不触发（不误报）"


def test_run_dry_run_no_push_no_state(monkeypatch, tmp_path):
    posts, state_path = _wire(monkeypatch, tmp_path)
    rc = pa.run(force=True, dry_run=True, state_path=state_path)
    assert rc == 0
    assert posts == []
    assert not os.path.exists(state_path)


def test_run_empty_ledger_rc0(monkeypatch, tmp_path):
    posts, state_path = _wire(monkeypatch, tmp_path)
    import web.app as wapp

    monkeypatch.setattr(wapp, "_up_read", lambda: {})
    rc = pa.run(force=True, state_path=state_path)
    assert rc == 0
    assert posts == []


def test_missing_key_rc2(monkeypatch, tmp_path):
    posts, state_path = _wire(monkeypatch, tmp_path)
    import web.app as wapp

    monkeypatch.setattr(pa, "_load_env_key", lambda name: None)
    rc = pa.run(force=True, state_path=state_path)
    assert rc == 2, "缺 webhook key 退出码 2"
    assert posts == []


def test_trading_window_gate(monkeypatch):
    """时段门：周六跳过；周一 14:30 放行；周一 12:00 午休跳过。"""
    import datetime as dt

    def at(hh, mm, weekday_mon=0):
        base = dt.datetime(2026, 10, 26, hh, mm)   # 周一
        # 调整到指定星期（2026-10-26=周一）
        delta = (weekday_mon - base.weekday()) % 7
        return base + dt.timedelta(days=delta)

    assert pa.in_trading_window(at(14, 30)) is True
    assert pa.in_trading_window(at(10, 0)) is True
    assert pa.in_trading_window(at(9, 16)) is True
    assert pa.in_trading_window(at(12, 0)) is False     # 午休
    assert pa.in_trading_window(at(15, 30)) is False    # 收盘后（15:05 前有效）
    assert pa.in_trading_window(at(14, 0, weekday_mon=5)) is False   # 周六
    # --force 由 run() 入口处理（此处只测纯函数）


# ---------------------------------------------------------------------------
# 3) 首仓通道（v7.0.23：非持仓股，双条件 = 时机✓ 且 现价 ≤ 首仓线）
# ---------------------------------------------------------------------------
# 600001 双条件全过 → 推；600002 价过时机✗ → 不推；600003 时机✓价未过 → 不推
FIRST_POOL = ["sh.600001", "sh.600002", "sh.600003"]
FIRST_ROWS = {
    "sh.600001": {"name": "首仓达标", "last_price": 8.9, "in_pos": True,
                  "k_level": 1, "next_add_price_raw": 8.0,
                  "first_level_raw": 9.0},
    "sh.600002": {"name": "时机未过", "last_price": 4.9, "in_pos": False,
                  "k_level": 0, "next_add_price_raw": None,
                  "first_level_raw": 5.0},
    "sh.600003": {"name": "价格未过", "last_price": 10.5, "in_pos": False,
                  "k_level": 0, "next_add_price_raw": None,
                  "first_level_raw": 9.5},
}
FIRST_TMAP = {
    "sh.600001": {"push_eligible": True, "pctile_window": 35.2,
                  "ttm_yield_pct": 4.8},
    "sh.600002": {"push_eligible": False, "pctile_window": 55.0,
                  "ttm_yield_pct": 3.0},
    "sh.600003": {"push_eligible": True, "pctile_window": 30.0,
                  "ttm_yield_pct": 5.0},
}
FIRST_SPOT = {
    "sh.600001": {"price": 8.90, "timestamp": "14:30:01"},
    "sh.600002": {"price": 4.90, "timestamp": "14:30:01"},
    "sh.600003": {"price": 10.50, "timestamp": "14:30:01"},
}


def _wire_first(monkeypatch, tmp_path, spot=None, tmap=None):
    """空持仓账本 + 3 只池内非持仓股的默认替身（可覆盖 spot/tmap）。"""
    spot = dict(FIRST_SPOT) if spot is None else spot
    tmap = dict(FIRST_TMAP) if tmap is None else tmap
    return _wire(monkeypatch, tmp_path, spot=spot, tmap=tmap, user={},
                 pool_codes=FIRST_POOL, rows=FIRST_ROWS)


def test_run_first_position_dual_condition(monkeypatch, tmp_path):
    """双条件全过 → 推首仓信号（账本空也走——v7.0.23 不再早退）。"""
    posts, state_path = _wire_first(monkeypatch, tmp_path)
    rc = pa.run(force=True, state_path=state_path)
    assert rc == 0
    assert len(posts) == 1, posts
    body = posts[0]["json"]
    content = body["content"]
    assert "首仓达标" in content and "sh.600001" in content
    assert "首仓线" in content and "非持仓·首仓信号" in content
    assert "分位 35.2%（低位）" in content          # v7.0.26：时机明细只剩分位
    assert "1 手" in content                            # 首仓 first_lots=1 手（v7.0.28）
    assert "时机未过" not in content and "价格未过" not in content  # 单条件不推
    assert "策略点位提醒" in body["title"]
    # 去重键：code|buy|0|首仓线
    st = json.load(open(state_path, encoding="utf-8"))
    assert "sh.600001|buy|0|9.0" in st["alerts"]
    # 同日第二轮 → 去重不重推
    rc2 = pa.run(force=True, state_path=state_path)
    assert rc2 == 0 and len(posts) == 1


def test_run_first_position_fail_closed_no_timing_row(monkeypatch, tmp_path):
    """无 timing 行 → fail-closed：价破首仓线也不推。"""
    posts, state_path = _wire_first(monkeypatch, tmp_path, tmap={})
    rc = pa.run(force=True, state_path=state_path)
    assert rc == 0
    assert posts == [], "时机表缺行 fail-closed 不推（不误报）"


def test_run_first_position_price_above_line_no_push(monkeypatch, tmp_path):
    """时机✓ 但现价高于首仓线 → 不推（双条件缺一不成立）。"""
    spot = dict(FIRST_SPOT)
    spot["sh.600001"] = {"price": 9.20, "timestamp": "14:30:01"}
    posts, state_path = _wire_first(
        monkeypatch, tmp_path, spot=spot,
        tmap={"sh.600001": dict(FIRST_TMAP["sh.600001"])})
    rc = pa.run(force=True, state_path=state_path)
    assert rc == 0 and posts == []
