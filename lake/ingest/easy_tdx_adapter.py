# -*- coding: utf-8 -*-
"""lake.ingest.easy_tdx_adapter —— easy-tdx（通达信 MAC 协议）K线适配器（v6.1）。

**进池理由（报告 §2/§3）**：补 T2 ``amount``（腾讯无）+ T2 ``adj_factor`` 推导
（hfq÷raw，替代 hang 的 BaoStock）+ T7 ``amount``。EU 可达、全史含 amount、
close vs 新浪差 0.0000%（researcher probe p5/p8 + TL 独立复现）。

**vendor（报告 §6 风险缓解）**：easy_tdx 33★ 个人项目 → src 已 vendor 进仓库
``lake/vendor/easy_tdx/``（researcher 验证可独立运行），本 adapter 从 vendor 路径
import——pip 依赖漂移/作者停更不影响数据湖。

数据口径：
- volume=股（TDX 口径，与新浪一致；**不** ×100）。amount=元。
- adj_factor = hfq÷raw close 推导（同 sina_adapter 算法；事件日 af）。
- 市场映射：sh.*→market 1、sz.*→market 0；bj.* 不支持（北交所 TDX 无 → 该源对该股失败）。
- 指数：sh000001/sh000300/sh000922→market 1、sz399001→market 0（probe p8 实测）。

限速：≥0.5s/股（RateLimiter 锚点法）。available()：懒缓存 + EU 自检
（from_best_host + 轻量 K线，失败→False 跳过该源——不 crash、不阻塞）。
"""
from __future__ import annotations

import logging
import os
import sys
import threading
from typing import Any, Dict, List, Optional

from .common import clean_date, to_float
from .source_pool import AUTHORITY, RateLimiter

log = logging.getLogger("lake.ingest.easy_tdx_adapter")

# vendor 路径（lake/vendor/easy_tdx）——sys.path 注入后按原包名 import
_VENDOR_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "vendor")


def _ensure_vendor_path() -> None:
    """把 lake/vendor 加入 sys.path（幂等；等价 site-packages 安装的 import 语义）。"""
    if _VENDOR_DIR not in sys.path:
        sys.path.insert(0, _VENDOR_DIR)


# TDX 单页上限 700（easy_tdx 内部自动分页）；全史请求条数给足余量（A股最长~8500 交易日）
_FULL_HISTORY_COUNT = 20000


class EasyTdxAdapter:
    """easy-tdx K线+amount+adj 推导适配器（T2 fallback / T7 amount 补充）。"""

    name = "tdx"
    authority = AUTHORITY["tdx"]  # =3（Q1：其它级）

    def __init__(self, min_interval_s: Optional[float] = None) -> None:
        from ..config import lake_cfg

        self._min_interval = (float(min_interval_s) if min_interval_s is not None
                              else float(lake_cfg().get("tdx_min_interval_s", 0.5)))
        self._limiter = RateLimiter(self._min_interval)
        self._client = None
        self._avail: Optional[bool] = None
        self._lock = threading.Lock()

    # ---------- client 管理（MacClient 持久连接 + 断线重连） ----------
    def _get_client(self):
        """懒建 MacClient（from_best_host 选最优 EU 可达 host）。失败抛异常。

        **DEFECT-HANG-1（R2）**：from_best_host 内部 ping_all 并发探测多 host
        （每 host socket timeout 有界，但整段墙钟无上限）→ 套 fetch_with_timeout。
        """
        if self._client is None:
            _ensure_vendor_path()
            from easy_tdx.mac.client import MacClient

            from .common import fetch_with_timeout

            self._client = fetch_with_timeout(MacClient.from_best_host, ping_timeout=4)
        return self._client

    def _reset_client(self) -> None:
        """连接异常后重置（下次调用重连）。"""
        with self._lock:
            if self._client is not None:
                try:
                    self._client.close()
                except Exception:  # noqa: BLE001 - close 失败不阻断
                    pass
                self._client = None

    def close(self) -> None:
        """driver 收尾调用（释放 TCP + heartbeat 线程）。"""
        self._reset_client()

    # ---------- available：懒缓存 + EU 自检 ----------
    def available(self) -> bool:
        with self._lock:
            if self._avail is not None:
                return self._avail
        try:
            _ensure_vendor_path()
            from easy_tdx.mac.enums import Period

            from .common import fetch_with_timeout

            client = self._get_client()
            # DEFECT-HANG-1（R2）：自检 K线也走 tdx socket——套墙钟硬上限（全 fetch 路径 ≤30s）。
            df = fetch_with_timeout(
                client.get_stock_kline, 1, "601398", Period.DAILY, start=0, count=3)
            ok = df is not None and len(df) > 0
        except Exception as exc:  # noqa: BLE001 - EU 不可达/未装 → False（跳过该源）
            log.warning("tdx available() 自检失败 → 跳过 tdx 源: %s", exc)
            self._reset_client()
            ok = False
        with self._lock:
            self._avail = bool(ok)
        return self._avail

    def reset_available_for_test(self) -> None:
        with self._lock:
            self._avail = None

    # ---------- 市场/代码映射 ----------
    @staticmethod
    def _market_code(ts_code: str) -> Optional[tuple]:
        """sh.601398 → (1, "601398")；sz.000001 → (0, "000001")；bj.* → None（不支持）。"""
        c = ts_code.replace(".", "")
        if c.startswith("sh"):
            return 1, c[2:]
        if c.startswith("sz"):
            return 0, c[2:]
        return None

    # ---------- DataFrame → ohlcv 行（volume=股，amount=元） ----------
    @staticmethod
    def _df_to_rows(df) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        if df is None or len(df) == 0:
            return rows
        cols = set(df.columns)
        for _, r in df.iterrows():
            d = clean_date(str(r.get("datetime") if "datetime" in cols else r.get("date")))
            if not d:
                continue
            vol = r.get("vol") if "vol" in cols else r.get("volume")
            rows.append({
                "date": d,
                "open": to_float(r.get("open")),
                "high": to_float(r.get("high")),
                "low": to_float(r.get("low")),
                "close": to_float(r.get("close")),
                "volume": to_float(vol),   # 股（TDX 口径，不 ×100）
                "amount": to_float(r.get("amount")) if "amount" in cols else None,
            })
        rows.sort(key=lambda x: x["date"])  # 升序（ISO 字典序=时间序）
        return rows

    # ---------- fetch_kline：raw OHLCV+amount + hfq÷raw adj 推导 ----------
    def fetch_kline(self, ts_code: str, start: Optional[str] = None,
                    end: Optional[str] = None) -> Dict[str, Any]:
        """TDX raw K线（全史含 amount）+ hfq÷raw 推导 adj_factor。

        :return: ``{"ohlcv": [...], "adj_factor": {除权日: af}|None}``。
        :raises RuntimeError: 连接失败/取空（worker 回退下一源，不 mark_done）。
        """
        mc = self._market_code(ts_code)
        if mc is None:
            raise RuntimeError(f"tdx 不支持北交所 {ts_code}（回退下一源）")
        market, code6 = mc

        _ensure_vendor_path()
        from easy_tdx.mac.enums import Adjust, Period

        from .common import fetch_with_timeout

        self._limiter.wait()
        try:
            client = self._get_client()
            if start is None and end is None:
                # DEFECT-HANG-1（R2）：tdx socket read 有 timeout，但**整段全史分页**
                # （~30 页 × 重连退避）墙钟无上限 + MacClient._execute 断线重试可累计
                # 分钟级。套 fetch_with_timeout：超时抛 FetchTimeoutError → 下方 except
                # 重置 client 后转 RuntimeError（回退下一源）。
                raw_df = fetch_with_timeout(
                    client.get_stock_kline, market, code6, Period.DAILY,
                    start=0, count=_FULL_HISTORY_COUNT)
            else:
                # 窗口：按自然日估算交易日数 + 缓冲，取最近 N 根后过滤
                import datetime as _dt

                d0 = _dt.date.fromisoformat(start or "1990-01-01")
                d1 = _dt.date.fromisoformat(end or _dt.date.today().isoformat())
                n = max(30, int((d1 - d0).days * 0.72) + 60)
                raw_df = fetch_with_timeout(
                    client.get_stock_kline, market, code6, Period.DAILY,
                    start=0, count=n)
        except Exception as exc:  # noqa: BLE001 - 连接/协议失败/超时 → 重置后抛（回退下一源）
            self._reset_client()
            raise RuntimeError(f"tdx K线取数失败 {ts_code}: {exc}") from exc

        ohlcv = self._df_to_rows(raw_df)
        if start:
            ohlcv = [r for r in ohlcv if r["date"] >= start]
        if end:
            ohlcv = [r for r in ohlcv if r["date"] <= end]
        if not ohlcv:
            raise RuntimeError(f"tdx K线取空 {ts_code}（回退下一源）")

        # hfq（adj 推导；失败不阻断 raw——adj=None，view 该段 NULL）
        adj_map = None
        self._limiter.wait()
        try:
            client = self._get_client()
            hfq_df = fetch_with_timeout(
                client.get_stock_kline, market, code6, Period.DAILY,
                start=0, count=_FULL_HISTORY_COUNT, adjust=Adjust.HFQ)
            adj_map = self._derive_adj_factor(ohlcv, self._df_to_rows(hfq_df))
        except Exception as exc:  # noqa: BLE001 - hfq 失败/超时不阻断 raw
            log.warning("tdx hfq K线失败 %s → adj_factor=None: %s", ts_code, exc)
            self._reset_client()
        return {"ohlcv": ohlcv, "adj_factor": adj_map}

    @staticmethod
    def _derive_adj_factor(ohlcv: List[Dict[str, Any]],
                           hfq_rows: List[Dict[str, Any]]) -> Optional[Dict[str, float]]:
        """hfq÷raw close 逐日比值 → 除权事件日 af（同 sina_adapter 算法）。"""
        if not hfq_rows:
            return None
        hfq_close = {r["date"]: r.get("close") for r in hfq_rows}
        events: Dict[str, float] = {}
        prev_ratio: Optional[float] = None
        for row in ohlcv:  # 升序
            raw_c = row.get("close")
            h_c = hfq_close.get(row["date"])
            if not raw_c or raw_c == 0 or h_c is None:
                continue
            ratio = h_c / raw_c
            if prev_ratio is not None and abs(ratio - prev_ratio) > 1e-4 * max(1.0, prev_ratio):
                events[row["date"]] = round(ratio, 6)
            elif prev_ratio is None:
                events[row["date"]] = round(ratio, 6)
            prev_ratio = ratio
        return events or None

    # ---------- fetch_adj_factor：独立 adj（同推导口径） ----------
    def fetch_adj_factor(self, ts_code: str, start: str, end: str) -> Dict[str, float]:
        """tdx hfq÷raw 推导 adj（[start,end] 窗口）。取空 → {}（回退下一源）。"""
        try:
            res = self.fetch_kline(ts_code, start=start, end=end)
            return res.get("adj_factor") or {}
        except Exception as exc:  # noqa: BLE001 - adj 失败不抛（调用方回退）
            log.warning("tdx fetch_adj_factor 失败 %s: %s", ts_code, exc)
            return {}

    # ---------- fetch_index_kline：T7 指数日K（含 amount——补缺口） ----------
    def fetch_index_kline(self, index_code: str, n: int = 290) -> List[Dict[str, Any]]:
        """TDX 指数日K（sh000001→market 1 / sz399001→market 0；probe p8 实测）。

        :return: ohlcv 行（volume=股、amount=元）；失败/取空 → []（调用方降级 NULL）。
        """
        mc = self._market_code(index_code)
        if mc is None:
            return []
        market, code6 = mc
        _ensure_vendor_path()
        from easy_tdx.mac.enums import Period

        self._limiter.wait()
        try:
            client = self._get_client()
            # DEFECT-HANG-1（R2）：指数 K线同走 tdx socket——套墙钟硬上限（全 fetch 路径 ≤30s）。
            from .common import fetch_with_timeout

            df = fetch_with_timeout(
                client.get_stock_kline, market, code6, Period.DAILY, start=0, count=n)
            return self._df_to_rows(df)
        except Exception as exc:  # noqa: BLE001 - 指数取数失败 → []（amount 留 NULL，不阻断）
            log.warning("tdx 指数 K线失败 %s: %s", index_code, exc)
            self._reset_client()
            return []

    def fetch_f10(self, ts_code: str) -> List[Dict[str, Any]]:
        """tdx 不提供 T5 F10（走 adata_f10）——返回空。"""
        return []
