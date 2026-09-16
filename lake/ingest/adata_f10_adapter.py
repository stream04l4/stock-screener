# -*- coding: utf-8 -*-
"""lake.ingest.adata_f10_adapter —— adata ``get_core_index`` F10 适配器（v6.1 T5 主源）。

**Q4 拍板（硬编码边界）**：本 adapter **只暴露 fetch_f10**——K线/分红接口一律不封装
（防误用：adata K线走被封的 push2his、分红走 baidu TooManyRedirects，EU 均不可用；
只 F10 ``get_core_index`` 走 datacenter.eastmoney.com EU 实测 200 OK）。

进池理由（报告 §3 T5）：BaoStock 财报 EU hang → T5 当前无可靠源；adata 补
pub_date(notice_date)/roe_weighted(roe_wtd)/gross_margin/liability_pct(asset_liab_ratio)
/yoy_pni(net_profit_yoy_gr)/npi(net_profit_attr_sh)——PIT 可用（notice_date=披露日）。

字段映射（adata → T5 列，口径对齐 baostock_ingest.load_t5）：
- notice_date → pub_date（PIT 必存；缺失的行跳过——PIT 语义要求 pub_date 非空）
- report_date → period YYYYQn（(月-1)//3+1，与 sina_ingest 口径一致）
- roe_wtd → roe_weighted；gross_margin → gross_margin；asset_liab_ratio → liability_pct
- net_profit_yoy_gr → yoy_pni；net_profit_attr_sh → npi（单位=元，与现口径一致）
- ocf：adata oper_cf_ps 是**每股**值非绝对额 → 不映射（T5.ocf 留 NULL，待新浪侧补——
  报告 §3 T5 "ocf 主源仍新浪"）。

限速：≥1s/股（RateLimiter 锚点法）。available()：懒缓存 + EU 自检。
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional

from .common import clean_date, to_float
from .source_pool import AUTHORITY, RateLimiter

log = logging.getLogger("lake.ingest.adata_f10_adapter")


def _period_from_report_date(report_date: str) -> Optional[str]:
    """'2026-06-30' → '2026Q2'（YYYYQn，(月-1)//3+1；与 sina_ingest 口径一致）。"""
    d = clean_date(report_date)
    if not d:
        return None
    month = int(d[5:7])
    return f"{d[:4]}Q{(month - 1) // 3 + 1}"


class AdataF10Adapter:
    """adata F10 适配器（T5 主源；**仅 fetch_f10**——Q4 硬编码边界）。"""

    name = "adata_f10"
    authority = AUTHORITY["adata_f10"]  # =3（Q1：其它级）

    def __init__(self, min_interval_s: Optional[float] = None) -> None:
        from ..config import lake_cfg

        self._min_interval = (float(min_interval_s) if min_interval_s is not None
                              else float(lake_cfg().get("adata_f10_min_interval_s", 1.0)))
        self._limiter = RateLimiter(self._min_interval)
        self._avail: Optional[bool] = None
        self._lock = threading.Lock()

    # ---------- available：懒缓存 + EU 自检 ----------
    def available(self) -> bool:
        with self._lock:
            if self._avail is not None:
                return self._avail
        try:
            df = self._core_index("601398")
            ok = df is not None and len(df) > 0
        except Exception as exc:  # noqa: BLE001 - EU 不可达/未装 → False（跳过该源）
            log.warning("adata_f10 available() 自检失败 → 跳过 adata 源: %s", exc)
            ok = False
        with self._lock:
            self._avail = bool(ok)
        return self._avail

    def reset_available_for_test(self) -> None:
        with self._lock:
            self._avail = None

    # ---------- 底层：adata get_core_index（唯一封装的接口——Q4） ----------
    @staticmethod
    def _core_index(code6: str):
        """调 adata ``stock.finance.get_core_index``。失败抛异常（调用方处理）。"""
        import adata

        return adata.stock.finance.get_core_index(code6)

    @staticmethod
    def _code6(ts_code: str) -> str:
        """sh.601398 → 601398。"""
        return ts_code.split(".")[-1]

    # ---------- fetch_f10：T5 季度基本面（唯一对外取数方法） ----------
    def fetch_f10(self, ts_code: str) -> List[Dict[str, Any]]:
        """adata F10 → T5 行（全历史报告期，降序）。

        :return: [{period(YYYYQn), pub_date, roe_weighted, gross_margin,
                  liability_pct, yoy_pni, npi, ocf(None)}]。
            **pub_date 缺失的行跳过**（PIT 语义：fundamentals_quarterly.pub_date 必存）。
        :raises RuntimeError: adata 调用失败（worker 回退下一源/不 mark_done）。
        """
        code6 = self._code6(ts_code)
        self._limiter.wait()
        try:
            df = self._core_index(code6)
        except Exception as exc:  # noqa: BLE001 - F10 取数失败 → 显式抛（回退/重试）
            raise RuntimeError(f"adata F10 取数失败 {ts_code}: {exc}") from exc
        if df is None or len(df) == 0:
            return []

        out: List[Dict[str, Any]] = []
        for _, r in df.iterrows():
            period = _period_from_report_date(str(r.get("report_date") or ""))
            pub_date = clean_date(str(r.get("notice_date") or ""))
            if not period or not pub_date:
                continue  # PIT：pub_date 必存——缺失行跳过（不硬造）
            out.append({
                "period": period,
                "pub_date": pub_date,
                "roe_weighted": to_float(r.get("roe_wtd")),
                "gross_margin": to_float(r.get("gross_margin")),
                "liability_pct": to_float(r.get("asset_liab_ratio")),
                "yoy_pni": to_float(r.get("net_profit_yoy_gr")),
                "npi": to_float(r.get("net_profit_attr_sh")),
                # ocf：adata 只有 oper_cf_ps（每股值）非绝对额 → 不映射（留 NULL，
                # 报告 §3 T5：ocf 主源仍新浪——sina_ingest.load_t5_ocf 侧补）
                "ocf": None,
            })
        return out

    # ---------- 其余接口：Q4 硬编码不封装（显式抛错防误用） ----------
    def fetch_kline(self, ts_code: str, start: Optional[str] = None,
                    end: Optional[str] = None) -> Dict[str, Any]:
        """Q4：adata K线 EU 不可用（push2his 封禁）——**不封装**，显式抛错防误用。"""
        raise NotImplementedError("adata_f10 adapter 仅暴露 fetch_f10（Q4 拍板；"
                                  "K线走 sina/tencent/tdx）")

    def fetch_adj_factor(self, ts_code: str, start: str, end: str) -> Dict[str, float]:
        """Q4：adata 不提供复权因子——显式抛错防误用。"""
        raise NotImplementedError("adata_f10 adapter 仅暴露 fetch_f10（Q4 拍板）")

    def fetch_index_kline(self, index_code: str, n: int = 290) -> List[Dict[str, Any]]:
        """Q4：adata 不提供指数 K线——显式抛错防误用。"""
        raise NotImplementedError("adata_f10 adapter 仅暴露 fetch_f10（Q4 拍板）")


# ---------------------------------------------------------------------------
# load（→ DuckDB，零网络）
# ---------------------------------------------------------------------------
def load_t5(con, ts_code: str, recs: List[Dict[str, Any]],
            source: str = "adata_f10", conflict_src: Optional[str] = None) -> int:
    """T5 upsert：adata F10 全报告期（PK (ts_code, period) → INSERT OR REPLACE）。

    :param recs: fetch_f10 输出。pub_date 必存（fetch_f10 已过滤缺失行——PIT 语义）。
        roe_avg/ocf=None→NULL（BaoStock 侧 roeAvg / 新浪侧 ocf 后续可补，见下注）。
    :param conflict_src: v6.1 跨源分歧摘要（adata vs BaoStock >1pp；None=NULL）。

    ⚠️ 整行替换语义：upsert 会覆盖该 (ts_code, period) 全行——若此前 sina_ingest
    .load_t5_ocf 已补 ocf/roe_weighted，本函数以 adata 值为准（Q1：T5 主源=adata；
    ocf adata 无绝对额→NULL，需后续 load_t5_ocf 再补——worker 编排保证顺序）。
    """
    from .common import DATA_VERSION, now_ts, upsert

    rows = []
    for r in recs:
        if not r.get("period") or not r.get("pub_date"):
            continue  # PIT：pub_date 必存（双保险——fetch_f10 已过滤）
        rows.append([
            ts_code, r["period"], r["pub_date"],
            None,                     # roe_avg：BaoStock 口径，adata 无 → NULL
            r.get("roe_weighted"),    # adata roe_wtd（加权ROE%）
            r.get("yoy_pni"),         # net_profit_yoy_gr
            r.get("npi"),             # net_profit_attr_sh（元）
            r.get("ocf"),             # None（adata 无绝对额 OCF——新浪侧补）
            r.get("gross_margin"),    # adata gross_margin%
            r.get("liability_pct"),   # adata asset_liab_ratio%
            source, now_ts(), DATA_VERSION,
        ])
    if not rows:
        return 0
    return upsert(con, "fundamentals_quarterly", [
        "ts_code", "period", "pub_date", "roe_avg", "roe_weighted", "yoy_pni",
        "npi", "ocf", "gross_margin", "liability_pct",
        "source", "fetched_at", "data_version"], rows, conflict_src=conflict_src)
