# -*- coding: utf-8 -*-
"""lake.ingest.tencent_adapter —— 腾讯 K线适配器（v6.1：现有 tencent_ingest 外包）。

**零影响原则（brief §5.3）**：本 adapter 只是把 ``tencent_ingest`` 既有 fetch 函数
包成 SourceAdapter 接口——签名不变、旧调用方（run_t2/run_t7/smoke）零影响。

数据口径：
- OHLCV = 腾讯 raw K线（fetch_kline_ohlcv 近 N / fetch_kline_full_history 全史分页）。
- volume 单位=手 → **本 adapter 统一 ×100 转股**（source_pool 契约：volume=股）。
- amount 腾讯日K不提供 → None（NULL，不硬造——Q8 纪律）。
- adj_factor 腾讯无复权因子接口 → fetch_adj_factor 返回 {}（回退下一源 tdx/BaoStock）。

available()：懒缓存 + EU 自检（取一只最近 5 根，失败→False 跳过）。
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional

from .common import to_float
from .source_pool import AUTHORITY
# 模块级 import **模块对象**（而非 from-import 函数名）：测试 monkeypatch
# ``tencent_ingest.fetch_kline_ohlcv`` / ``fetch_kline_full_history`` 时，本 adapter
# 经 ``_ti.<fn>(...)`` 的调用点同步生效（v607/v603 离线单测契约——patch 的是
# tencent_ingest 模块属性；from-import 会绑定旧引用绕过 patch 打到真实网络）。
from . import tencent_ingest as _ti

log = logging.getLogger("lake.ingest.tencent_adapter")


class TencentKlineAdapter:
    """腾讯 K线适配器（T2 fallback / T7 主源）。"""

    name = "tencent"
    authority = AUTHORITY["tencent"]  # =1（Q1）

    def __init__(self) -> None:
        self._client = None
        self._avail: Optional[bool] = None
        self._lock = threading.Lock()

    def _get_client(self):
        if self._client is None:
            from screener.data.tencent import TencentClient

            self._client = TencentClient()
        return self._client

    # ---------- available：懒缓存 + EU 自检 ----------
    def available(self) -> bool:
        with self._lock:
            if self._avail is not None:
                return self._avail
        try:
            rows = _ti.fetch_kline_ohlcv(self._get_client(), "sh.601398", n=5)
            ok = len(rows) > 0
        except Exception as exc:  # noqa: BLE001 - EU 不可达 → False（跳过该源）
            log.warning("tencent available() 自检失败 → 跳过腾讯源: %s", exc)
            ok = False
        with self._lock:
            self._avail = bool(ok)
        return self._avail

    def reset_available_for_test(self) -> None:
        with self._lock:
            self._avail = None

    # ---------- fetch_kline：OHLCV（volume 手→股；amount=None） ----------
    def fetch_kline(self, ts_code: str, start: Optional[str] = None,
                    end: Optional[str] = None) -> Dict[str, Any]:
        """腾讯 raw K线。

        - ``start/end`` 均 None → **全史**（fetch_kline_full_history 分页翻到 IPO，
          单页重试耗尽抛 RuntimeError——主源失败语义，worker 回退下一源）。
        - 指定窗口 → fetch_kline_ohlcv(n=窗口交易日数+缓冲)。

        :return: ``{"ohlcv": [...volume=股, amount=None...], "adj_factor": None}``。
        """
        client = self._get_client()
        if start is None and end is None:
            rows = _ti.fetch_kline_full_history(client, ts_code)  # 失败抛 RuntimeError
        else:
            # 近 N 天窗口（end-start 自然日 → 交易日数估算 ×2/3 + 缓冲）
            import datetime as _dt

            d0 = _dt.date.fromisoformat(start or "1990-01-01")
            d1 = _dt.date.fromisoformat(end or _dt.date.today().isoformat())
            n = max(30, int((d1 - d0).days * 0.72) + 60)
            rows = _ti.fetch_kline_ohlcv(client, ts_code, n=n)
            if not rows:
                raise RuntimeError(f"腾讯 K线取空 {ts_code}（回退下一源）")
        ohlcv = [{
            "date": r["date"],
            "open": r.get("open"), "high": r.get("high"),
            "low": r.get("low"), "close": r.get("close"),
            "volume": (int(r["volume"] * 100) if r.get("volume") is not None else None),  # 手→股
            "amount": None,  # 腾讯日K不提供 amount（NULL，不硬造）
        } for r in rows]
        return {"ohlcv": ohlcv, "adj_factor": None}

    # ---------- fetch_adj_factor：腾讯无复权因子 → {} ----------
    def fetch_adj_factor(self, ts_code: str, start: str, end: str) -> Dict[str, float]:
        """腾讯不提供复权因子（Q1 adj 主源=新浪推导，fallback=tdx/BaoStock）。"""
        return {}

    # ---------- fetch_index_kline：T7 指数日K ----------
    def fetch_index_kline(self, index_code: str, n: int = 290) -> List[Dict[str, Any]]:
        """腾讯指数日K（index_code=sh000001 无点格式；amount=None）。

        :param n: 取最近 n 根（默认 290≈1.5 年交易日；T7 worker 传 days+30 对齐旧口径）。
        复用 fetch_kline_ohlcv（指数同端点）；取空 → []（调用方回退 tdx）。
        """
        from .tencent_ingest import INDEX_CODES, fetch_kline_ohlcv

        if index_code not in INDEX_CODES:
            return []
        prefix = "sh" if index_code.startswith("sh") else "sz"
        ts_code = f"{prefix}.{index_code[2:]}"  # sh000001 → sh.000001（fetch 内部转 tcode）
        rows = fetch_kline_ohlcv(self._get_client(), ts_code, n=n)
        return [{
            "date": r["date"],
            "open": r.get("open"), "high": r.get("high"),
            "low": r.get("low"), "close": r.get("close"),
            "volume": (int(r["volume"] * 100) if r.get("volume") is not None else None),
            "amount": None,
        } for r in rows]

    def fetch_f10(self, ts_code: str) -> List[Dict[str, Any]]:
        """腾讯不提供 T5 F10（走 adata_f10）——返回空。"""
        return []
