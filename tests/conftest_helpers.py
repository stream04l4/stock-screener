# -*- coding: utf-8 -*-
"""测试公共工具：从 fixture 加载数据、构造合成K线。"""
from __future__ import annotations

import csv
import os
from datetime import date, timedelta
from typing import Any, Dict, List

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def load_fixture_csv(name: str) -> List[Dict[str, str]]:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return list(csv.DictReader(f))


def dividend_records_601398() -> List[Dict[str, Any]]:
    """真实样例：601398 2019-2026 全部分红记录（含同一除权日重复行）。"""
    out = []
    for row in load_fixture_csv("dividend_601398_full_history_2019-2026.csv"):
        d = dict(row)
        try:
            d["dividCashPsBeforeTax"] = float(d["dividCashPsBeforeTax"])
        except (ValueError, KeyError):
            d["dividCashPsBeforeTax"] = None
        out.append(d)
    return out


def synthetic_closes(n: int, start_price: float = 10.0, daily_drift: float = 0.0) -> List[float]:
    """构造 n 根K线的收盘价序列（几何漂移，无噪声 → 波动率=0）。"""
    p = start_price
    out = []
    for _ in range(n):
        out.append(round(p, 6))
        p *= (1 + daily_drift)
    return out


def trading_dates(n: int, end: date) -> List[str]:
    """构造 n 个"交易日"（用日历日倒推，仅用于长度对齐测试）。"""
    d = end
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
            d -= timedelta(days=1)
        else:
            d -= timedelta(days=1)
    return list(reversed(out))


# ===========================================================================
# v6 FE-D3+：Vue 前端源码树 blob（契约断言用；旧 web/static vanilla 栈已移除）
# ===========================================================================
import glob as _glob  # noqa: E402

_VUE_BLOB_CACHE = None


def vue_src_blob() -> str:
    """web/frontend/src 全量源码拼接（缓存）。防前后端字段脱节的契约断言统一用它。"""
    global _VUE_BLOB_CACHE
    if _VUE_BLOB_CACHE is None:
        root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "web", "frontend", "src")
        parts = []
        for p in _glob.glob(os.path.join(root, "**"), recursive=True):
            if os.path.isfile(p):
                with open(p, encoding="utf-8") as fh:
                    parts.append(fh.read())
        _VUE_BLOB_CACHE = "\n".join(parts)
    return _VUE_BLOB_CACHE


def vue_src(path: str) -> str:
    """读单个 Vue 源文件（相对 web/frontend/src）。"""
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "web", "frontend", "src")
    with open(os.path.join(root, path), encoding="utf-8") as fh:
        return fh.read()
