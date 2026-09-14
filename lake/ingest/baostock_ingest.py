# -*- coding: utf-8 -*-
"""lake.ingest.baostock_ingest —— T1 快照 / T2 adj_factor / T5 基本面（复用 BaoStockClient）。

**纪律（v6 brief 最高优先级）**：一律走 ``screener.data.baostock_client.BaoStockClient``
（构造即挂 QuotaGuard 49900/日，每次 query 前 acquire）——**不得绕过守卫裸调 baostock**。
本模块只做"客户端 (fields, rows) → lake 行"的薄转换。

字段口径（调研报告 §3）：
- T1: query_stock_basic(list/delist/board) + query_stock_industry(证监会二级)；
  name/is_st/soe_flag 由腾讯快照/v5 规则在 backfill 层补齐（本模块只落 BaoStock 侧列）。
- T2 adj_factor: query_adjust_factor —— **仅除权日有行**（事件值），前向填充在 load_t2。
- T5: query_profit_data(roeAvg/gpMargin/netProfit/pubDate) + query_growth_data(YOYPNI)
  + query_balance_data(liabilityToAsset)。ocf/roe_weighted 由新浪侧补（sina_ingest）。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from .common import DATA_VERSION, clean_date, now_ts, to_float, upsert

log = logging.getLogger("lake.ingest.baostock")


# ---------------------------------------------------------------------------
# fetch（走 BaoStockClient，自动 QuotaGuard.acquire）
# ---------------------------------------------------------------------------
def fetch_stock_basic(client) -> Tuple[List[str], List[List[str]]]:
    """query_stock_basic 全量（上市/退市/板块）。1 次配额。"""
    import baostock as bs

    return client.call_with_fields(bs.query_stock_basic, label="lake_stock_basic")


def fetch_industry(client) -> Tuple[List[str], List[List[str]]]:
    """query_stock_industry 全量（证监会行业，5546 行）。1 次配额。"""
    import baostock as bs

    return client.call_with_fields(bs.query_stock_industry, label="lake_industry")


def fetch_adjust_factor(client, ts_code: str, start: str, end: str) -> Tuple[List[str], List[List[str]]]:
    """query_adjust_factor [start,end]（仅除权日有行）。1 次配额/股。"""
    import baostock as bs

    return client.call_with_fields(
        bs.query_adjust_factor, label=f"lake_adjfactor_{ts_code}",
        code=ts_code, start_date=start, end_date=end)


def fetch_profit(client, ts_code: str, year: int, quarter: int) -> Optional[Dict[str, Any]]:
    """query_profit_data 单股单季 → {pubDate, roeAvg, gpMargin, netProfit}。1 次配额。"""
    import baostock as bs

    fields, rows = client.call_with_fields(
        bs.query_profit_data, label=f"lake_profit_{ts_code}_{year}Q{quarter}",
        code=ts_code, year=year, quarter=quarter)
    if not rows:
        return None
    d = dict(zip(fields, rows[-1]))
    return {
        "pubDate": clean_date(d.get("pubDate")),
        "roeAvg": to_float(d.get("roeAvg")),
        "gpMargin": to_float(d.get("gpMargin")),
        "netProfit": to_float(d.get("netProfit")),
    }


def fetch_growth(client, ts_code: str, year: int, quarter: int) -> Optional[Dict[str, Any]]:
    """query_growth_data 单股单季 → {YOYPNI}。1 次配额。"""
    import baostock as bs

    fields, rows = client.call_with_fields(
        bs.query_growth_data, label=f"lake_growth_{ts_code}_{year}Q{quarter}",
        code=ts_code, year=year, quarter=quarter)
    if not rows:
        return None
    d = dict(zip(fields, rows[-1]))
    return {"YOYPNI": to_float(d.get("YOYPNI"))}


def fetch_balance(client, ts_code: str, year: int, quarter: int) -> Optional[Dict[str, Any]]:
    """query_balance_data 单股单季 → {liabilityToAsset}。1 次配额。"""
    import baostock as bs

    fields, rows = client.call_with_fields(
        bs.query_balance_data, label=f"lake_balance_{ts_code}_{year}Q{quarter}",
        code=ts_code, year=year, quarter=quarter)
    if not rows:
        return None
    d = dict(zip(fields, rows[-1]))
    return {"liabilityToAsset": to_float(d.get("liabilityToAsset"))}


# ---------------------------------------------------------------------------
# load（→ DuckDB，零网络）
# ---------------------------------------------------------------------------
def load_t1(con, basic_rows: List[List[str]], industry_map: Dict[str, str],
            source: str = "baostock") -> int:
    """T1 upsert：股票主表（BaoStock 侧列；name/is_st/soe_* 留 NULL 由快照层补）。

    :param basic_rows: query_stock_basic 行（fields=[code,code_name,ipoDate,
        outDate,type,status]，见 baostock 文档；type 1=股票）。
    :param industry_map: {ts_code: (industry_csric2, industry_name)}——由
        query_stock_industry 构建（updateDate/code/code_name/industry/industryClassification）。
    """
    rows = []
    for r in basic_rows:
        d = dict(zip(["code", "code_name", "ipoDate", "outDate", "type", "status"], r))
        if str(d.get("type")) != "1":  # 只收股票（排除指数/基金）
            continue
        ts = d["code"]
        ind = industry_map.get(ts, (None, None))
        rows.append([
            ts,
            None,                     # name：腾讯快照补（v5 口径，BaoStock code_name 备用）
            ind[0],                   # industry_csric2
            ind[1],                   # industry_name
            clean_date(d.get("ipoDate")),   # list_date
            clean_date(d.get("outDate")),   # delist_date（在上市 → None/空）
            _board(ts),               # board：代码前缀推断（主板/科创/创业/北交）
            None,                     # is_st：腾讯快照补
            None,                     # st_since
            None,                     # soe_flag：v5 规则在 backfill 层算
            None,                     # soe_basis
            source, now_ts(), DATA_VERSION,
        ])
    return upsert(con, "stock_master", [
        "ts_code", "name", "industry_csric2", "industry_name", "list_date",
        "delist_date", "board", "is_st", "st_since", "soe_flag", "soe_basis",
        "source", "fetched_at", "data_version"], rows)


def _board(ts_code: str) -> str:
    """代码前缀 → 板块（沪主板/科创板/深主板/创业板/北交所）。"""
    c = ts_code.split(".")[-1]
    if c.startswith("68"):
        return "科创"
    if c.startswith(("30",)):
        return "创业"
    if c.startswith(("4", "8")) or c.startswith("92"):
        return "北交"
    return "主板"


def build_industry_map(ind_fields: List[str], ind_rows: List[List[str]]) -> Dict[str, Tuple[Optional[str], Optional[str]]]:
    """query_stock_industry → {ts_code: (industry代码, industry名称)}。

    字段（baostock）：updateDate, code, code_name, industry, industryClassification。
    industry=证监会行业名；分类码需另映射——此处存名称 + 空码占位（csric2 码由
    backfill 层用 cache/industry.csv 对照补齐，避免硬编码映射表）。
    """
    out: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
    for r in ind_rows:
        d = dict(zip(ind_fields, r))
        code = d.get("code")
        if not code:
            continue
        out[code] = (None, d.get("industry") or None)
    return out


def load_t2_adj_factor(con, ts_code: str, adj_rows: List[List[str]],
                       source: str = "baostock") -> Dict[str, float]:
    """T2 adj_factor 事件行 → {除权日: af}（**不直接写表**——load_t2 前向填充后统一写）。

    query_adjust_factor 字段：code, dividOperateDate, foreAdjustFactor,
    backAdjustFactor, adjustFactor。取 adjustFactor（后复权因子，= 本地缓存口径 2.554536）。
    """
    out: Dict[str, float] = {}
    for r in adj_rows:
        d = dict(zip(["code", "dividOperateDate", "foreAdjustFactor",
                      "backAdjustFactor", "adjustFactor"], r))
        ddate = clean_date(d.get("dividOperateDate"))
        af = to_float(d.get("adjustFactor"))
        if ddate and af is not None:
            out[ddate] = af
    return out


def load_t5(con, ts_code: str, period: str, pub_date: Optional[str],
            profit: Optional[Dict[str, Any]], growth: Optional[Dict[str, Any]],
            balance: Optional[Dict[str, Any]],
            ocf: Optional[float] = None, roe_weighted: Optional[float] = None,
            source: str = "baostock") -> int:
    """T5 upsert：单股单季基本面（PIT：pub_date 必存）。

    :param period: YYYYQn。ocf/roe_weighted 由新浪侧传入（None → 列 NULL，待补）。
    """
    p = profit or {}
    row = [
        ts_code, period, pub_date,
        p.get("roeAvg"), roe_weighted,
        (growth or {}).get("YOYPNI"), p.get("netProfit"), ocf,
        p.get("gpMargin"), (balance or {}).get("liabilityToAsset"),
        source, now_ts(), DATA_VERSION,
    ]
    return upsert(con, "fundamentals_quarterly", [
        "ts_code", "period", "pub_date", "roe_avg", "roe_weighted", "yoy_pni",
        "npi", "ocf", "gross_margin", "liability_pct",
        "source", "fetched_at", "data_version"], [row])
