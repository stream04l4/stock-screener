# -*- coding: utf-8 -*-
"""lake.ingest.baostock_adapter —— BaoStock 适配器（v6.1：外包 + Q6 存活门控）。

**Q6 拍板**：灌数启动探一次 ``query_all_stock``（10s socket 超时判活）——存活则按
Q1 排序参与 fallback/交叉校验；死亡则 available()=False，worker 自动跳过 BaoStock
源（不 crash、不阻塞）。本 adapter 的 ``available()`` **直接读 Q6 探测结果**
（:func:`lake.ingest.source_pool.baostock_alive`）——不在 adapter 内重复探测。

数据口径（复用 baostock_ingest 既有 fetch，零重写）：
- adj_factor = query_adjust_factor（仅除权日有行 → load_t2 前向填充）。
- T5 F10 = query_profit_data + query_growth_data + query_balance_data（**最近 4 季**——
  交叉校验用；全史 85 季 ×3 次/股超配额，不可行。BaoStock 死时零调用）。
- OHLCV = query_history_k_data_plus（含 amount）——Q1 优先级里 T2 ohlcv_amount 未列
  baostock（sina→tencent→tdx），本方法保留作 config 可调的备选能力。

纪律：一律走 BaoStockClient（QuotaGuard 日预算内，不得裸调）；stop_checker 注入
lake.backfill.stop_requested（SIGTERM 收尾加速，v6.0.10 既有机制）。
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional, Tuple

from .source_pool import AUTHORITY

log = logging.getLogger("lake.ingest.baostock_adapter")

# T5 交叉校验只比最近 N 季（全史 ×3 次/股超配额；BaoStock 死时零调用）
_F10_CROSSCHECK_QUARTERS = 4


class BaoStockAdapter:
    """BaoStock 适配器（Q6 存活门控；fallback/交叉校验源）。"""

    name = "baostock"
    authority = AUTHORITY["baostock"]  # =2（Q1）

    def __init__(self) -> None:
        self._client = None
        self._lock = threading.Lock()

    # ---------- client 管理（懒建；QuotaGuard 内） ----------
    def _get_client(self):
        """懒建 BaoStockClient（构造即挂 QuotaGuard；stop_checker=SIGTERM 收尾钩子）。"""
        if self._client is None:
            from lake.backfill import stop_requested as _bk_stop
            from screener.data.baostock_client import BaoStockClient

            self._client = BaoStockClient(stop_checker=_bk_stop)
        return self._client

    def close(self) -> None:
        """driver 收尾调用（登出，释放会话）。"""
        with self._lock:
            if self._client is not None:
                try:
                    self._client.close()
                except Exception:  # noqa: BLE001 - close 失败不阻断
                    pass
                self._client = None

    # ---------- available：Q6 探测结果门控（不在本 adapter 重复探测） ----------
    def available(self) -> bool:
        """BaoStock 是否存活 = Q6 灌数启动探测结果（未探测 → False，保守按死处理）。"""
        from .source_pool import baostock_alive

        return baostock_alive()

    # ---------- fetch_adj_factor：query_adjust_factor（事件值） ----------
    def fetch_adj_factor(self, ts_code: str, start: str, end: str) -> Dict[str, float]:
        """BaoStock 复权因子（仅除权日有行）。失败/空 → {}（调用方回退下一源）。

        QuotaGuard 内（1 次配额/股）；配额耗尽抛 BaoStockError → 调用方捕获回退。

        **DEFECT-HANG-1（R2）**：baostock 协议层在**服务端关连接**时 ``send_msg``
        内部 ``while True: recv`` 对空读 IndexError→except→返回 None→上层重试，形成
        **~100% CPU 紧循环**（09-16 shutdown-spin 根因：strace 327k recvfrom/8s on
        CLOSE-WAIT socket）。socket timeout patch 只救"半死连接慢滴答"，救不了这种
        协议层自旋。套 fetch_with_timeout 墙钟硬上限：超时抛 FetchTimeoutError →
        本方法返回 {}（回退下一源），挂死线程 daemon 随进程退出回收（其持有的
        baostock 模块级 socket fd 由 OS 随进程消失）。
        """
        from .baostock_ingest import fetch_adjust_factor, load_t2_adj_factor

        from .common import fetch_with_timeout

        try:
            fields, rows = fetch_with_timeout(
                fetch_adjust_factor, self._get_client(), ts_code, start, end)
        except Exception as exc:  # noqa: BLE001 - BaoStock 失败/超时 → {}（回退，不 crash）
            log.warning("baostock adj_factor 失败 %s: %s", ts_code, exc)
            return {}
        return load_t2_adj_factor(None, ts_code, rows) if rows else {}

    # ---------- fetch_kline：query_history_k_data_plus（含 amount；备选能力） ----------
    def fetch_kline(self, ts_code: str, start: Optional[str] = None,
                    end: Optional[str] = None) -> Dict[str, Any]:
        """BaoStock 日K（OHLCV+amount，volume=股）。Q1 优先级 T2 未列本源——保留作
        config 可调备选。失败/空 → RuntimeError（worker 回退下一源）。

        :raises RuntimeError: 查询失败或取空。
        """
        import baostock as bs

        from .common import clean_date, to_float

        start = start or "1990-01-01"
        end = end or "2099-12-31"
        # DEFECT-HANG-1（R2）：同 fetch_adj_factor——baostock 协议层紧循环/半死连接
        # 墙钟兜底。超时抛 FetchTimeoutError → 本方法 except 转 RuntimeError（回退）。
        from .common import fetch_with_timeout

        try:
            fields, rows = fetch_with_timeout(
                self._get_client().call_with_fields,
                bs.query_history_k_data_plus, label=f"lake_kline_{ts_code}",
                code=ts_code, start_date=start, end_date=end,
                fields="date,open,high,low,close,volume,amount",
                frequency="d", adjustflag="2")  # 2=raw 不复权（adj 单独取）
        except Exception as exc:  # noqa: BLE001 - BaoStock 失败/超时 → 显式抛（回退下一源）
            raise RuntimeError(f"baostock K线失败 {ts_code}: {exc}") from exc

        ohlcv: List[Dict[str, Any]] = []
        for r in rows:
            d = clean_date(r[0])
            if not d:
                continue
            ohlcv.append({
                "date": d,
                "open": to_float(r[1]), "high": to_float(r[2]),
                "low": to_float(r[3]), "close": to_float(r[4]),
                "volume": to_float(r[5]),   # 股（BaoStock 口径）
                "amount": to_float(r[6]),   # 元
            })
        if not ohlcv:
            raise RuntimeError(f"baostock K线取空 {ts_code}（回退下一源）")
        return {"ohlcv": ohlcv, "adj_factor": None}

    # ---------- fetch_f10：T5 最近 N 季（交叉校验用） ----------
    def fetch_f10(self, ts_code: str) -> List[Dict[str, Any]]:
        """BaoStock T5 基本面**最近 4 季**（交叉校验用；全史超配额不可行）。

        每季 = query_profit_data + query_growth_data + query_balance_data（3 次配额）。
        :return: [{period, pub_date, roe_weighted(None→BaoStock 无加权ROE), gross_margin,
                  liability_pct, yoy_pni, npi, ocf(None)}]；失败/空 → []。

        字段口径对齐 adata_f10_adapter（cross_check_f10 按 period 对齐比 roe_weighted/
        gross_margin/liability_pct——BaoStock roe_avg≠加权ROE，该字段两边都 None 时
        cross_check 自动跳过，不误报分歧）。
        """
        import datetime as _dt

        from .baostock_ingest import fetch_balance, fetch_growth, fetch_profit
        from .common import to_float

        today = _dt.date.today()
        # 最近 N 季候选（含当季——未披露时 BaoStock 返回空，自动跳过）
        candidates: List[Tuple[int, int]] = []
        y, q = today.year, (today.month - 1) // 3 + 1
        for i in range(_F10_CROSSCHECK_QUARTERS):
            candidates.append((y, q))
            q -= 1
            if q == 0:
                q, y = 4, y - 1

        client = self._get_client()
        out: List[Dict[str, Any]] = []
        # DEFECT-HANG-1（R2）：整段 F10（≤4 季 ×3 查询）墙钟兜底——baostock 协议层
        # 紧循环/半死连接。超时抛 FetchTimeoutError → 本方法 except 返回已取部分
        # （交叉校验源，缺失不阻断 T5 主源 adata）。
        from .common import fetch_with_timeout

        try:
            out = fetch_with_timeout(self._fetch_f10_inner, client, ts_code, candidates)
        except Exception as exc:  # noqa: BLE001 - F10 失败/超时 → 返回已取部分（不阻断）
            log.warning("baostock T5 F10 失败 %s: %s", ts_code, exc)
        return out

    def _fetch_f10_inner(self, client, ts_code: str,
                         candidates: List[Tuple[int, int]]) -> List[Dict[str, Any]]:
        """fetch_f10 的取数主体（供 fetch_with_timeout 墙钟包裹；见其 docstring）。"""
        from .baostock_ingest import fetch_balance, fetch_growth, fetch_profit

        out: List[Dict[str, Any]] = []
        for year, quarter in candidates:
            try:
                profit = fetch_profit(client, ts_code, year, quarter)
                if not profit:
                    continue
                growth = fetch_growth(client, ts_code, year, quarter)
                balance = fetch_balance(client, ts_code, year, quarter)
            except Exception as exc:  # noqa: BLE001 - 单季失败跳过（不阻断其余季）
                log.warning("baostock T5 %s %dQ%d 失败: %s", ts_code, year, quarter, exc)
                continue
            out.append({
                "period": f"{year}Q{quarter}",
                "pub_date": profit.get("pubDate"),
                # BaoStock 无加权 ROE（只有 roeAvg）→ None（cross_check 跳过该字段）
                "roe_weighted": None,
                "gross_margin": (profit or {}).get("gpMargin"),
                "liability_pct": (balance or {}).get("liabilityToAsset"),
                "yoy_pni": (growth or {}).get("YOYPNI"),
                "npi": (profit or {}).get("netProfit"),
                "ocf": None,
            })
        return out

    # ---------- fetch_index_kline：BaoStock 指数非本 adapter 职责 ----------
    def fetch_index_kline(self, index_code: str, n: int = 290) -> List[Dict[str, Any]]:
        """T7 走腾讯/tdx——BaoStock adapter 不提供指数（返回空）。"""
        return []
