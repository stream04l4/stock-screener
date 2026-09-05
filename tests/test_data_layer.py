# -*- coding: utf-8 -*-
"""数据层单测（离线）：GBK 转码封装、腾讯解析、to_float。

腾讯响应格式（调研报告 §5）：``v_sh601398="f0~f1~...~f87";``，共 88 个字段。
本测试用 helper 构造**完整 88 字段**的 payload（只填关键字段下标），
保证与真实响应的解析路径一致。
"""
from __future__ import annotations

import pytest

from screener.data.fetchers import to_float, to_int
from screener.data.tencent import (
    IDX_FLOAT_MV,
    IDX_NAME,
    IDX_PB,
    IDX_PE_TTM,
    IDX_PCT_CHG,
    IDX_PRICE,
    IDX_TIMESTAMP,
    IDX_TOTAL_MV,
    IDX_TURNOVER,
    TencentClient,
)

N_FIELDS = 88


def make_payload(code: str, name: str, price: float, **kw) -> str:
    """构造单只股票的完整 88 字段 payload。"""
    f = [""] * N_FIELDS
    f[0] = "1"
    f[IDX_NAME] = name
    f[2] = code.replace(".", "")
    f[IDX_PRICE] = str(price)
    f[4] = str(price - 0.03)   # 昨收
    f[5] = str(price - 0.06)   # 今开
    f[IDX_TIMESTAMP] = kw.get("timestamp", "20260904161432")
    f[IDX_PCT_CHG] = kw.get("pct_chg", "0.37")
    f[IDX_TURNOVER] = kw.get("turnover", "0.09")
    f[IDX_PE_TTM] = kw.get("pe_ttm", "7.74")
    f[IDX_FLOAT_MV] = kw.get("float_mv", "21919.47")
    f[IDX_TOTAL_MV] = kw.get("total_mv", "28975.83")
    f[IDX_PB] = kw.get("pb", "0.73")
    return "~".join(f)


def test_tencent_parse_real_sample_values():
    """真实样例数值（601398 收盘后快照，调研报告 §5）：字段下标全部对齐。"""
    text = f'v_sh601398="{make_payload("sh.601398", "工商银行", 8.13)}";\n'
    out = TencentClient.parse(text)
    assert "sh.601398" in out
    q = out["sh.601398"]
    assert q["name"] == "工商银行"
    assert q["price"] == pytest.approx(8.13)
    assert q["timestamp"] == "20260904161432"
    assert q["pct_chg"] == pytest.approx(0.37)
    assert q["turnover"] == pytest.approx(0.09)
    assert q["pe_ttm"] == pytest.approx(7.74)
    assert q["float_mv_yi"] == pytest.approx(21919.47)
    assert q["total_mv_yi"] == pytest.approx(28975.83)
    assert q["pb"] == pytest.approx(0.73)


def test_tencent_parse_multiple_codes():
    """批量响应：多只股票各占一行。"""
    text = (
        f'v_sh601398="{make_payload("sh.601398", "工商银行", 8.13)}";\n'
        f'v_sz000001="{make_payload("sz.000001", "平安银行", 11.20, pct_chg="0.90")}";\n'
    )
    out = TencentClient.parse(text)
    assert set(out.keys()) == {"sh.601398", "sz.000001"}
    assert out["sz.000001"]["price"] == pytest.approx(11.20)


def test_tencent_parse_empty_payload():
    """无效代码 → 空 payload，不崩溃、不出现在结果里。"""
    text = (
        'v_sh999999="";\n'
        f'v_sz000001="{make_payload("sz.000001", "平安银行", 11.20)}";\n'
    )
    out = TencentClient.parse(text)
    assert "sh.999999" not in out
    assert "sz.000001" in out


def test_gbk_decode_wrapper():
    """GBK 转码封装：接口返回 GBK 字节流 → decode('gbk') 得到中文。"""
    raw = f'v_sh601398="{make_payload("sh.601398", "工商银行", 8.13)}";'.encode("gbk")
    text = raw.decode("gbk", errors="replace")  # 与 TencentClient._get_text 同款转码
    out = TencentClient.parse(text)
    assert out["sh.601398"]["name"] == "工商银行"


def test_bs_code_to_tencent():
    from screener.data.tencent import bs_code_to_tencent
    assert bs_code_to_tencent("sh.601398") == "sh601398"
    assert bs_code_to_tencent("sz.000001") == "sz000001"


# ---------- 字符串 → 数值（缺失 = 空串，不是 NaN） ----------

@pytest.mark.parametrize("raw,expected", [
    ("", None),
    (None, None),
    ("0.1689", 0.1689),
    ("8.1300", 8.13),
    ("abc", None),
    ("  ", None),
])
def test_to_float(raw, expected):
    got = to_float(raw)
    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected)


@pytest.mark.parametrize("raw,expected", [
    ("1", 1),
    ("0", 0),
    ("", None),
    ("2.7", 2),  # int() 截断
])
def test_to_int(raw, expected):
    assert to_int(raw) == expected
