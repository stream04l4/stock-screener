# -*- coding: utf-8 -*-
"""缓存层单测（离线）。核心验收：同一天重复运行不得重复拉取。"""
from __future__ import annotations

import os
import time

import pytest

from screener.data.cache import DiskCache, make_cache_name


def test_put_get_roundtrip(tmp_path):
    c = DiskCache(str(tmp_path))
    c.put("kline_sh.601398_2025-07-19_2026-09-04_af1", ["date", "close"],
          [["2026-09-04", "8.13"], ["2026-09-03", "8.10"]])
    hit = c.get("kline_sh.601398_2025-07-19_2026-09-04_af1")
    assert hit is not None
    assert hit["columns"] == ["date", "close"]
    assert hit["rows"][0] == ["2026-09-04", "8.13"]


def test_miss_returns_none(tmp_path):
    c = DiskCache(str(tmp_path))
    assert c.get("nonexistent") is None
    assert c.has("nonexistent") is False


def test_ttl_expiry(tmp_path):
    c = DiskCache(str(tmp_path))
    c.put("x", ["a"], [["1"]])
    # 把 mtime 改成 2 小时前 → ttl=1h 应过期
    p = c._path("x")
    old = time.time() - 2 * 3600
    os.utime(p, (old, old))
    assert c.has("x", ttl_hours=1.0) is False
    assert c.get("x", ttl_hours=1.0) is None
    # ttl=None 永不过期
    assert c.get("x") is not None


def test_corrupt_file_returns_none(tmp_path):
    """半截/损坏文件不得被当作有效缓存（原子写防的就是这个）。"""
    c = DiskCache(str(tmp_path))
    p = c._path("bad")
    with open(p, "w", encoding="utf-8") as f:
        f.write('broken"quote\n')  # 非法 CSV
    assert c.get("bad") is None


def test_atomic_write_no_tmp_leftover(tmp_path):
    c = DiskCache(str(tmp_path))
    c.put("y", ["a"], [["1"], ["2"]])
    files = list(tmp_path.iterdir())
    assert len(files) == 1 and files[0].name == "y.csv"


def test_cache_name_stable():
    """同一组参数 → 同一个缓存名（重复运行命中同一文件）。"""
    a = make_cache_name("kline", "sh.601398", "2025-07-19", "2026-09-04", "af1")
    b = make_cache_name("kline", "sh.601398", "2025-07-19", "2026-09-04", "af1")
    assert a == b
    c = make_cache_name("kline", "sh.601398", "2025-07-19", "2026-09-05", "af1")
    assert a != c
