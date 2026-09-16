# -*- coding: utf-8 -*-
"""lake.ingest.sina_adapter —— 新浪 akshare ``stock_zh_a_daily`` 适配器（v6.1 T2 主源）。

**Q1 拍板**：T2 OHLCV/amount 主源=新浪（全史一次拉全、含 amount、volume=股）；
adj_factor 主源=**新浪 hfq÷raw close 推导**（adjust="hfq" 再拉一遍，事件日前向填充
复用 ``forward_fill_af``）。

数据流：
- ``fetch_kline`` 调 akshare ``stock_zh_a_daily(symbol, adjust="")`` 取 raw OHLCV+amount
  （一次全史），**同时**调 ``adjust="hfq"`` 取后复权 close，逐日 hfq_close/raw_close
  推导 adj_factor 事件值（相邻交易日比值突变=除权日；恒定段复用）。
- volume 单位=股（新浪口径，与 tdx 一致；腾讯才是手——见 tencent_adapter）。

限速：≥1s/股（raw+hfq 共 2 次调用，实测连续 10 只无限流但留余量）——``RateLimiter``。
available()：懒缓存 + EU 自检（首次探测一只轻量 K线，失败→False 跳过该源）。

⚠️ akshare 是重依赖（顶层 import 慢）——本模块在函数内 lazy import，import 失败
（未装/版本漂移）→ available()=False，worker 回退腾讯主路径（零回归兜底）。
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional

from .common import clean_date, to_float
from .source_pool import AUTHORITY, RateLimiter

log = logging.getLogger("lake.ingest.sina_adapter")


class SinaKlineAdapter:
    """新浪 K线+amount+adj_factor 推导适配器（T2 主源）。"""

    name = "sina"
    authority = AUTHORITY["sina"]  # =0（最权威，Q1）

    def __init__(self, min_interval_s: Optional[float] = None) -> None:
        from ..config import lake_cfg

        self._min_interval = (float(min_interval_s) if min_interval_s is not None
                              else float(lake_cfg().get("sina_min_interval_s", 1.0)))
        self._limiter = RateLimiter(self._min_interval)
        self._avail: Optional[bool] = None   # 懒缓存（None=未探测）
        self._lock = threading.Lock()

    # ---------- available：懒缓存 + EU 自检 ----------
    def available(self) -> bool:
        with self._lock:
            if self._avail is not None:
                return self._avail
        # 首次探测：轻量取一只（601398）最近 5 根 raw K线，成功=EU 可达
        try:
            df = self._ak_daily("sh601398", adjust="", count_recent=5)
            ok = df is not None and len(df) > 0
        except Exception as exc:  # noqa: BLE001 - EU 不可达/未装 → False（跳过该源）
            log.warning("sina available() 自检失败 → 跳过新浪源: %s", exc)
            ok = False
        with self._lock:
            self._avail = bool(ok)
        return self._avail

    def reset_available_for_test(self) -> None:
        with self._lock:
            self._avail = None

    # ---------- 底层：akshare stock_zh_a_daily ----------
    def _ak_daily(self, symbol6: str, adjust: str,
                  count_recent: Optional[int] = None) -> Optional["Any"]:
        """调 akshare ``stock_zh_a_daily``。symbol6=sh601398/sz000001 格式（无点）。

        :param count_recent: 仅取最近 N 根（available() 自检用，省流量）；None=全史。
        :return: pandas DataFrame（列 date/open/high/low/close/volume/amount...）；
            失败/空 → None。
        """
        import akshare as ak

        df = ak.stock_zh_a_daily(symbol=symbol6, adjust=adjust)
        if df is None or len(df) == 0:
            return None
        if count_recent is not None and len(df) > count_recent:
            df = df.tail(count_recent).reset_index(drop=True)
        return df

    @staticmethod
    def _symbol6(ts_code: str) -> str:
        """sh.601398 → sh601398（akshare 无点格式）。"""
        return ts_code.replace(".", "")

    # ---------- fetch_kline：raw OHLCV+amount + hfq/raw adj 推导 ----------
    def fetch_kline(self, ts_code: str, start: Optional[str] = None,
                    end: Optional[str] = None) -> Dict[str, Any]:
        """全史 raw K线（含 amount）+ hfq÷raw 推导 adj_factor 事件值。

        :return: ``{"ohlcv": [...], "adj_factor": {除权日: af}|None}``。
            ohlcv 行 volume=股、amount=元；adj_factor=**事件日→af**（load_t2 前向填充）。
        :raises RuntimeError: raw K线取空（主源失败语义——worker 应回退下一源，不 mark_done）。

        adj 推导算法（brief 已验证口径）：
          af(d) = hfq_close(d) / raw_close(d)。两次除权之间该比值恒定；除权日跳到新值。
          故扫描升序日期，比值相对前一日变化 > 1e-4 → 记为除权事件日（af=新比值）。
          首个事件日之前 af 无定义（load_t2 前向填充给 None，hfq view 该段 NULL，预期）。
        """
        sym = self._symbol6(ts_code)
        # raw（OHLCV+amount）——限速锚点（raw+hfq 共 2 次调用，间隔 ≥1s）
        self._limiter.wait()
        raw_df = self._ak_daily(sym, adjust="")
        if raw_df is None or len(raw_df) == 0:
            raise RuntimeError(f"新浪 raw K线取空 {ts_code}（主源失败，回退下一源）")
        # hfq（后复权 close → adj 推导）
        self._limiter.wait()
        try:
            hfq_df = self._ak_daily(sym, adjust="hfq")
        except Exception as exc:  # noqa: BLE001 - hfq 失败不阻断 raw（adj=None，view 该段 NULL）
            log.warning("新浪 hfq K线取空 %s → adj_factor=None: %s", ts_code, exc)
            hfq_df = None

        ohlcv: List[Dict[str, Any]] = []
        for _, r in raw_df.iterrows():
            d = clean_date(str(r.get("date")))
            if not d:
                continue
            ohlcv.append({
                "date": d,
                "open": to_float(r.get("open")),
                "high": to_float(r.get("high")),
                "low": to_float(r.get("low")),
                "close": to_float(r.get("close")),
                "volume": to_float(r.get("volume")),   # 股（新浪口径）
                "amount": to_float(r.get("amount")),   # 元
            })
        if not ohlcv:
            raise RuntimeError(f"新浪 raw K线解析空 {ts_code}（主源失败，回退下一源）")

        adj_map = self._derive_adj_factor(ohlcv, hfq_df)
        return {"ohlcv": ohlcv, "adj_factor": adj_map}

    def _derive_adj_factor(self, ohlcv: List[Dict[str, Any]],
                           hfq_df: Optional["Any"]) -> Optional[Dict[str, float]]:
        """hfq÷raw close 逐日比值 → 除权事件日 af（相邻比值突变检测）。"""
        if hfq_df is None or len(hfq_df) == 0:
            return None
        hfq_close = {}
        for _, r in hfq_df.iterrows():
            d = clean_date(str(r.get("date")))
            c = to_float(r.get("close"))
            if d and c is not None:
                hfq_close[d] = c
        events: Dict[str, float] = {}
        prev_ratio: Optional[float] = None
        for row in ohlcv:  # 升序
            raw_c = row.get("close")
            h_c = hfq_close.get(row["date"])
            if not raw_c or raw_c == 0 or h_c is None:
                continue
            ratio = h_c / raw_c
            if prev_ratio is not None and abs(ratio - prev_ratio) > 1e-4 * max(1.0, prev_ratio):
                events[row["date"]] = round(ratio, 6)  # 除权事件日（af=新比值）
            elif prev_ratio is None:
                events[row["date"]] = round(ratio, 6)  # 首个可比日=基准事件
            prev_ratio = ratio
        return events or None

    # ---------- fetch_adj_factor：独立 adj（fallback 路径用，同推导口径） ----------
    def fetch_adj_factor(self, ts_code: str, start: str, end: str) -> Dict[str, float]:
        """新浪 hfq÷raw 推导 adj（[start,end] 窗口内事件值）。

        与 fetch_kline 的 adj 同口径；独立方法供"OHLCV 走腾讯、adj 走新浪"的组合。
        取空 → {}（调用方视为该源无 adj，回退下一源）。
        """
        sym = self._symbol6(ts_code)
        self._limiter.wait()
        raw_df = self._ak_daily(sym, adjust="")
        self._limiter.wait()
        try:
            hfq_df = self._ak_daily(sym, adjust="hfq")
        except Exception:  # noqa: BLE001
            return {}
        if raw_df is None or len(raw_df) == 0:
            return {}
        ohlcv: List[Dict[str, Any]] = []
        for _, r in raw_df.iterrows():
            d = clean_date(str(r.get("date")))
            if not d or (start and d < start) or (end and d > end):
                continue
            ohlcv.append({"date": d, "close": to_float(r.get("close"))})
        return self._derive_adj_factor(ohlcv, hfq_df) or {}

    # ---------- fetch_f10 / fetch_index_kline：新浪本 adapter 不提供 ----------
    def fetch_f10(self, ts_code: str) -> List[Dict[str, Any]]:
        """新浪 K线 adapter 不提供 F10（T5 走 adata_f10）——返回空。"""
        return []

    def fetch_index_kline(self, index_code: str, n: int = 290) -> List[Dict[str, Any]]:
        """新浪本 adapter 不提供指数 K线（T7 走腾讯/tdx）——返回空。"""
        return []
