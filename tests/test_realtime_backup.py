# -*- coding: utf-8 -*-
"""Gate 3 实时一主一备（screener/data/realtime.py）离线测试。

规格（重设计 §5，Gate 0 已批）：
- 主=腾讯 qt.gtimg.cn；备=东财 push2delay ulist；
- **整批切换**：主整批失败（异常/0 只返回）→ 整批走备；主部分成功保留主结果；
- 备源缺字段 → None 显式降级（原则 5）；
- 返回 (spot, source)，source ∈ {"tencent","eastmoney_push2delay",""}。

纪律：全离线——monkeypatch TencentClient.fetch 与 requests.get（requests 层），
零真实网络。
"""
import datetime as dt
import sys
import os
import types
from typing import Dict, Optional

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from screener.data import realtime as rt  # noqa: E402


# ---------------------------------------------------------------------------
# _bs_to_secid 市场号映射
# ---------------------------------------------------------------------------
def test_secid_mapping():
    assert rt._bs_to_secid("sh.600546") == "1.600546"
    assert rt._bs_to_secid("sh.688981") == "1.688981"     # 科创板=沪
    assert rt._bs_to_secid("sz.000001") == "0.000001"
    assert rt._bs_to_secid("sz.300750") == "0.300750"     # 创业板=深
    # 北交所/未知 → fail-closed 跳过（原则 11 显式排除）
    assert rt._bs_to_secid("bj.430047") is None
    assert rt._bs_to_secid("sz.830001") is None
    # 前缀与代码段矛盾 → 不猜
    assert rt._bs_to_secid("sh.000001") is None
    assert rt._bs_to_secid("sz.600546") is None
    # 畸形输入
    assert rt._bs_to_secid("") is None
    assert rt._bs_to_secid("600546") is None
    assert rt._bs_to_secid("sh.60054") is None


# ---------------------------------------------------------------------------
# _em_parse：f124 Unix 秒 → 北京时间 14 位；缺字段 None 降级
# ---------------------------------------------------------------------------
def test_em_parse_timestamp_and_degradation():
    # 2026-11-10 15:00:00 +08:00 = 1794298800（Unix 秒）
    ts = int(dt.datetime(2026, 11, 10, 15, 0, 0,
                        tzinfo=dt.timezone(dt.timedelta(hours=8))).timestamp())
    diff = [{"f2": 11.01, "f3": -2.57, "f12": "600546", "f14": "山煤国际",
             "f15": 11.25, "f16": 10.96, "f124": ts}]
    out = rt._em_parse(diff)
    assert "sh.600546" in out
    r = out["sh.600546"]
    assert r["price"] == 11.01 and r["high"] == 11.25 and r["low"] == 10.96
    assert r["timestamp"] == "20261110150000"   # 14 位（quote_date_of 兼容）
    # 备源无对应字段 → None 显式降级（原则 5）
    for k in ("turnover", "pe_ttm", "float_mv_yi", "total_mv_yi", "pb",
              "ttm_yield_pct"):
        assert r[k] is None, f"{k} 应 None 降级"
    # f12 非 6/0/3 开头（北交所）→ 跳过
    assert rt._em_parse([{"f12": "430047", "f2": 1.0}]) == {}
    # 缺 f124 → timestamp None（消费方 fail-closed 不判触碰）
    assert rt._em_parse([{"f12": "600546", "f2": 1.0}])["sh.600546"]["timestamp"] is None


# ---------------------------------------------------------------------------
# fetch_spot：整批切换语义
# ---------------------------------------------------------------------------
def _fake_tx(spot: Dict, exc: Optional[Exception] = None):
    def fake_fetch(self, codes, batch_size=50):
        if exc:
            raise exc
        return spot
    return fake_fetch


def _patch_tx(monkeypatch, spot: Dict, exc: Optional[Exception] = None):
    from screener.data import tencent
    monkeypatch.setattr(tencent.TencentClient, "fetch",
                        _fake_tx(spot, exc))


def _patch_em(monkeypatch, spot: Dict):
    monkeypatch.setattr(rt, "_em_fetch", lambda codes, timeout=rt._EM_TIMEOUT: spot)


def test_primary_success_no_em(monkeypatch):
    tx_spot = {"sh.600546": {"price": 11.0, "timestamp": "20261110150000"}}
    _patch_tx(monkeypatch, tx_spot)
    em_called = []
    monkeypatch.setattr(rt, "_em_fetch",
                        lambda codes, timeout=rt._EM_TIMEOUT: em_called.append(1))
    spot, source = rt.fetch_spot(["sh.600546"])
    assert source == "tencent" and spot is tx_spot
    assert em_called == []   # 主成功 → 不碰备源


def test_primary_exception_whole_batch_to_em(monkeypatch):
    _patch_tx(monkeypatch, {}, exc=OSError("network down"))
    em_spot = {"sh.600546": {"price": 11.01, "timestamp": "20261110150000"}}
    _patch_em(monkeypatch, em_spot)
    spot, source = rt.fetch_spot(["sh.600546", "sz.000001"])
    assert source == "eastmoney_push2delay" and spot == em_spot


def test_primary_empty_whole_batch_to_em(monkeypatch):
    """主源 0 只返回（整批失败）→ 也走备源（§5 整批制，不逐只混用）。"""
    _patch_tx(monkeypatch, {})
    em_spot = {"sz.000001": {"price": 11.86, "timestamp": "20261110150000"}}
    _patch_em(monkeypatch, em_spot)
    spot, source = rt.fetch_spot(["sz.000001"])
    assert source == "eastmoney_push2delay" and spot == em_spot


def test_both_fail_empty(monkeypatch):
    _patch_tx(monkeypatch, {}, exc=OSError("down"))
    _patch_em(monkeypatch, {})
    spot, source = rt.fetch_spot(["sh.600546"])
    assert spot == {} and source == ""   # 消费方走湖价回退（只漏报不误报）


def test_empty_codes(monkeypatch):
    _patch_tx(monkeypatch, {})
    spot, source = rt.fetch_spot([])
    assert spot == {} and source == ""
