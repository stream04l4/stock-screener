# -*- coding: utf-8 -*-
"""lake.ingest.local_cache_ingest —— 从现有 cache/*.csv bootstrap T2/T5 历史段（零网络）。

数据流（调研报告 §2 local_cache_ingest）：v5 主路径已落盘的稳定键缓存是**现成的
历史数据源**，直接读进 lake 即可 bootstrap T2(is_st)/T2 adj_factor/T5(profit)/T9(rf)。
- ``cache/kline_af3_{code}.csv``：date,code,close,isST,tradestatus。
  ⚠️ **close = 不复权(raw)价**（BaoStock adjustflag=3；"af3"是历史命名，实为 raw——
  见 reconstruct.py docstring"由不复权收盘价序列重建后复权"）。raw close 可直接入 T2.close。
- ``cache/adjfactor_{code}.csv``：code,dividOperateDate,fore/back/adjustFactor（仅除权日有行）。
- ``cache/profit_{code}_{year}_{q}.csv``：code,pubDate,statDate,roeAvg,npMargin,gpMargin,…
- ``cache/rf_10y_daily.csv``：date,yield_pct（T9 现值序列）。

口径：T2.close 存 raw；adj_factor 存累计后复权因子(adjustFactor)事件值，load_t2 前向填充。
"""
from __future__ import annotations

import csv
import logging
import os
from typing import Any, Dict, List, Optional

from .common import DATA_VERSION, clean_date, now_ts, to_float, upsert

log = logging.getLogger("lake.ingest.local_cache")

# 缓存文件哨兵（与 DiskCache 同语义：区分本程序写入的有效缓存）。
# **v6.1.8 F1**：接受两个哨兵——``stock-screener-cache-v1``（lake bootstrap 缓存，
# kline_af3/adjfactor/profit_* 均用此）与 ``stock-screener-em-cache-v1``（screener/data/em.py
# 的 EM_CACHE_SENTINEL，rf_10y_daily.csv 由 screener.data.rf 写入时用此）。历史缺陷：本模块
# 原只认前者，而 rf csv 是后者 → load_macro_rf 恒读 None → macro_rf 0 行（T9 落库静默失败）。
# 两哨兵都合法（同一项目的两套缓存写入器），放宽为白名单而非改成单一值——避免误伤已用
# 前者写入的 kline_af3/adjfactor/profit 缓存。
_CACHE_SENTINELS = ("stock-screener-cache-v1", "stock-screener-em-cache-v1")


def _read_cache_csv(path: str) -> Optional[Dict[str, Any]]:
    """读 DiskCache 格式 CSV（哨兵 + 表头 + 数据）。缺失/损坏 → None。"""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            reader = csv.reader(f)
            sentinel = next(reader, None)
            if not sentinel or sentinel[0] not in _CACHE_SENTINELS:
                return None
            columns = next(reader, None)
            if columns is None:
                return None
            rows = [row for row in reader]
        return {"columns": columns, "rows": rows}
    except (OSError, csv.Error):
        return None


def load_kline_isst(con, cache_dir: str, ts_code: str, source: str = "local_cache") -> int:
    """从 kline_af3 缓存补 T2 的 is_st（按日期 upsert，只填 is_st 列）。

    kline_af3 每行 (date, code, close_af3, isST, tradestatus)。T2 可能已有腾讯 raw 行
    → 用 UPDATE is_st（行存在时）；不存在则跳过（raw OHLCV 由 tencent_ingest 负责）。
    """
    path = os.path.join(cache_dir, f"kline_af3_{ts_code}.csv")
    hit = _read_cache_csv(path)
    if not hit or not hit["rows"]:
        return 0
    n = 0
    for r in hit["rows"]:
        d = dict(zip(hit["columns"], r))
        date = clean_date(d.get("date"))
        is_st = to_float(d.get("isST"))
        if not date or is_st is None:
            continue
        con.execute(
            "UPDATE kline_daily SET is_st=? WHERE ts_code=? AND date=?",
            [int(is_st), ts_code, date])
        n += 1
    return n


def load_adjfactor_events(con, cache_dir: str, ts_code: str) -> Dict[str, float]:
    """从 adjfactor 缓存读 {除权日: adjustFactor}（事件序列，供 load_t2 前向填充）。"""
    path = os.path.join(cache_dir, f"adjfactor_{ts_code}.csv")
    hit = _read_cache_csv(path)
    out: Dict[str, float] = {}
    if not hit or not hit["rows"]:
        return out
    for r in hit["rows"]:
        d = dict(zip(hit["columns"], r))
        date = clean_date(d.get("dividOperateDate"))
        af = to_float(d.get("adjustFactor"))
        if date and af is not None:
            out[date] = af
    return out


def load_profit_history(con, cache_dir: str, ts_code: str,
                        source: str = "local_cache") -> int:
    """从 profit_{code}_{y}_{q}.csv 缓存 bootstrap T5（零网络）。

    扫描 cache/ 下该 code 的全部 profit 文件，逐季 upsert（roe_avg/gross_margin/npi/
    pub_date；yoy_pni/liability_pct/ocf 留 NULL 待 growth/balance/sina 补）。
    """
    n = 0
    try:
        files = [f for f in os.listdir(cache_dir)
                 if f.startswith(f"profit_{ts_code}_") and f.endswith(".csv")]
    except OSError:
        return 0
    for fn in sorted(files):
        hit = _read_cache_csv(os.path.join(cache_dir, fn))
        if not hit or not hit["rows"]:
            continue
        d = dict(zip(hit["columns"], hit["rows"][-1]))
        stat = clean_date(d.get("statDate"))
        if not stat:
            continue
        period = f"{stat[:4]}Q{(int(stat[5:7]) - 1) // 3 + 1}"
        con.execute(
            "INSERT OR REPLACE INTO fundamentals_quarterly "
            "(ts_code, period, pub_date, roe_avg, roe_weighted, yoy_pni, npi, ocf, "
            " gross_margin, liability_pct, source, fetched_at, data_version) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [ts_code, period, clean_date(d.get("pubDate")), to_float(d.get("roeAvg")),
             None, None, to_float(d.get("netProfit")), None, to_float(d.get("gpMargin")),
             None, source, now_ts(), DATA_VERSION])
        n += 1
    return n


def load_macro_rf(con, cache_dir: str, source: str = "te_local") -> int:
    """T9 macro_rf：读 cache/rf_10y_daily.csv（现值序列，零网络；Q7 历史缺口显式 NULL）。

    文件格式：哨兵 + 表头(date,yield_pct) + 数据行。全部 upsert（date PK 幂等）；
    只灌已落盘行（现值序列起步，历史缺口不回填——Joel 已确认）。
    """
    path = os.path.join(cache_dir, "rf_10y_daily.csv")
    hit = _read_cache_csv(path)
    if not hit or not hit["rows"]:
        return 0
    n = 0
    for r in hit["rows"]:
        d = dict(zip(hit["columns"], r))
        date = clean_date(d.get("date"))
        yld = to_float(d.get("yield_pct"))
        if not date or yld is None:
            continue
        con.execute(
            "INSERT OR REPLACE INTO macro_rf (date,yield_pct,source,fetched_at,"
            "data_version) VALUES (?,?,?,?,?)",
            [date, yld, source, now_ts(), DATA_VERSION])
        n += 1
    return n
