# -*- coding: utf-8 -*-
"""lake.ingest.em_dividend_ingest —— T4 分红事件（读 cache/em_dividend_all.csv，零网络）。

数据流（调研报告 §3 T4）：``cache/em_dividend_all.csv``（东财封禁前落盘静态快照，
56,974 行 1991→今，dps_pretax 已 /10 每股口径）。复用 ``sina.load_local_dividends``
（零网络读取函数）——**不新写抓取逻辑**。

字段口径：
- ts_code = 6位裸码补前缀（to_ts_code）；
- ex_date = 除权日（DATE）；ann_date = plan_notice_date（公告日）；
- period = report_date 年份（YYYY，分红归属年度）；
- cash_dps = dps_pretax（元/股，em 已 /10）；stk_div = NULL（暂无源）。

幂等：dividend_events 无 PK（同 ex_date 可多行）→ "先删该 ts_code 后插"。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .common import (DATA_VERSION, clean_date, delete_where, insert_many, now_ts,
                     to_float, to_ts_code)

log = logging.getLogger("lake.ingest.em_dividend")


def load_dividends(con, cache_dir: str, ts_codes: Optional[List[str]] = None,
                   source: str = "em_local_static") -> int:
    """从本地 em_dividend_all.csv 灌 T4。

    :param cache_dir: 项目 cache/ 目录（含 em_dividend_all.csv）。
    :param ts_codes: 可选过滤（只灌指定股；None=全表）。ts_code 格式 sh.601398。
    :return: 写入行数。文件缺失/损坏 → SinaDataError（数据源级失败，显式抛错）。

    幂等：对每个涉及的 ts_code 先 DELETE 再 INSERT（事件表无 PK，upsert 不适用）。
    """
    # 延迟 import：sina 模块较重且依赖 requests；本函数零网络但复用其读取函数。
    from screener.data.sina import load_local_dividends

    rows, meta = load_local_dividends(cache_dir)
    want = set(to_ts_code(c) for c in ts_codes) if ts_codes else None
    # 按 ts_code 分组（先删后插幂等）
    by_code: Dict[str, List[List[Any]]] = {}
    n = 0
    for r in rows:
        code6 = str(r["code"])
        ts = to_ts_code(code6)
        if want is not None and ts not in want:
            continue
        ex_date = clean_date(r.get("ex_date"))
        ann_date = clean_date(r.get("plan_notice_date"))
        # period = report_date 年份（分红归属年度；report_date 形如 2026-06-30）
        rd = clean_date(r.get("report_date"))
        period = rd[:4] if rd else None
        by_code.setdefault(ts, []).append([
            ts, ex_date, ann_date, period,
            to_float(r.get("dps_pretax")),  # cash_dps 元/股（em 已 /10）
            None,                           # stk_div：暂无源 → NULL
            source, now_ts(), DATA_VERSION,
        ])
        n += 1
    cols = ["ts_code", "ex_date", "ann_date", "period", "cash_dps",
            "stk_div", "source", "fetched_at", "data_version"]
    written = 0
    for ts, code_rows in by_code.items():
        # 无 PK 事件表：先删后插（INSERT OR REPLACE 要求 PK，此处不可用）
        delete_where(con, "dividend_events", "ts_code", ts)
        written += insert_many(con, "dividend_events", cols, code_rows)
    log.info("T4 分红灌入 %d 行（%d 股；源 %s，as_of=%s）",
             written, len(by_code), source, meta.get("as_of"))
    return written
