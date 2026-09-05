# -*- coding: utf-8 -*-
"""本地磁盘缓存（CSV 原子写）。

设计目标：**同一天重复运行不得重复拉取** BaoStock。
- 键 = (查询类型, 参数)，与"运行日"解耦：同一批数据（如 2026-09-04 的日K、
  2026Q2 季报）在任何一次运行里只需要拉一次，之后全部命中缓存。
- 原子写：先写 .tmp 再 os.replace，避免半截文件被当作有效缓存。
- 市场级快照（行业分类等）支持 TTL；个股历史数据（日K/分红/季报）不可变，永不过期。
"""
from __future__ import annotations

import csv
import logging
import os
import time
from typing import Any, Dict, List, Optional, Sequence

log = logging.getLogger("screener.data.cache")

# 缓存文件首行哨兵：区分"本程序写入的有效缓存"与外部/损坏文件
CACHE_SENTINEL = "stock-screener-cache-v1"


class DiskCache:
    """CSV 文件缓存。目录结构: {cache_dir}/{name}.csv"""

    def __init__(self, cache_dir: str) -> None:
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)

    # ---------- 基础读写 ----------
    def _path(self, name: str) -> str:
        safe = "".join(c if (c.isalnum() or c in "._-") else "_" for c in name)
        return os.path.join(self.cache_dir, f"{safe}.csv")

    def has(self, name: str, ttl_hours: Optional[float] = None) -> bool:
        """缓存是否存在且未过期。ttl_hours=None 表示永不过期。"""
        path = self._path(name)
        if not os.path.exists(path):
            return False
        if ttl_hours is None:
            return True
        age_h = (time.time() - os.path.getmtime(path)) / 3600.0
        return age_h < ttl_hours

    def put(self, name: str, columns: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
        """原子写入缓存。首行为哨兵标记，用于识别非本程序产生/损坏的文件。"""
        path = self._path(name)
        tmp = path + f".tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([CACHE_SENTINEL])
            writer.writerow(list(columns))
            for row in rows:
                writer.writerow([("" if v is None else str(v)) for v in row])
        os.replace(tmp, path)

    def get(self, name: str, ttl_hours: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """读取缓存；不存在/过期/损坏（无哨兵标记）返回 None。"""
        if not self.has(name, ttl_hours):
            return None
        path = self._path(name)
        try:
            with open(path, "r", encoding="utf-8", newline="") as f:
                reader = csv.reader(f)
                sentinel = next(reader, None)
                if not sentinel or sentinel[0] != CACHE_SENTINEL:
                    return None  # 非本程序写入或文件损坏
                columns = next(reader, None)
                if columns is None:
                    return None
                rows = [row for row in reader]
            return {"columns": columns, "rows": rows}
        except (OSError, csv.Error) as exc:
            log.warning("缓存读取失败 %s: %s（将重新拉取）", name, exc)
            return None

    # ---------- 统计 ----------
    def stats(self) -> Dict[str, int]:
        n = 0
        for fn in os.listdir(self.cache_dir):
            if fn.endswith(".csv"):
                n += 1
        return {"files": n}


def make_cache_name(*parts: Any) -> str:
    """把查询参数拼成稳定的缓存文件名（不含扩展名）。"""
    return "_".join(str(p) for p in parts)
