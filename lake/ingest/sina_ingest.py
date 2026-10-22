# -*- coding: utf-8 -*-
"""lake.ingest.sina_ingest —— T6 股东 / T5.ocf+roe_weighted（复用 SinaClient）。

数据流（调研报告 §3）：
- T6 = ``SinaClient.fetch_holders``（F10 流通股股东页，前十大 rank/name/hold_shares/
  circ_ratio_pct/share_nature）；as_of_date=end_date；hold_ratio=circ_ratio_pct
  （**流通股口径**，只做定性识别）。**controller_* 三列本期无源 → NULL**（TL Q1 拍板：
  不引入新源，文档标注"待补源"；soe 识别继续 v5 规则）。
- T5.ocf = 新浪财务 JSON ``MANANETR``（绝对额，元；累计口径）+ roe_weighted=ROEWEIGHTED
  （百分数,加权）。复用 ``SinaClient._get``（限速+WAF456退避+熔断）+ ``parse_cf_report``。

纪律：串行 ≥1s + WAF(456) 长退避——全部由 SinaClient 内部保证，本模块不另设节奏。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from .common import DATA_VERSION, clean_date, delete_where, insert_many, now_ts, to_float

log = logging.getLogger("lake.ingest.sina")


# ---------------------------------------------------------------------------
# T6 股东（load：纯转换，输入=parse_holders_page 结构）
# ---------------------------------------------------------------------------
def load_t6(con, ts_code: str, periods: Sequence[Dict[str, Any]],
            as_of_date: Optional[str] = None,
            source: str = "sina_f10") -> int:
    """T6 灌入：前十大股东快照（v7.0.7：controller_* 死列已砍，只灌股东五要素）。

    :param periods: ``parse_holders_page`` 输出（[{end_date, notice_date, holders:[...]}]）。
    :param as_of_date: 只灌该期（PIT）；None=全部报告期。

    幂等（holders_snapshot **无 PK**，INSERT OR REPLACE 不可用 → 先删后插）：
    - as_of_date 指定 → 只删/换该 (ts_code, as_of_date) 行（PIT，不碰其他期）；
    - as_of_date=None → 删该股全部快照后整体重灌。
    - 转换后零行 → no-op（**不删旧数据**：无法区分"真无数据"与"解析漂移"，
      最安全解释是保留既有行）。
    """
    rows = []
    for p in periods:
        end_date = clean_date(p.get("end_date"))
        if not end_date:
            continue
        if as_of_date and end_date != as_of_date:
            continue
        for h in p.get("holders", []):
            rows.append([
                ts_code, end_date,
                int(h["holder_rank"]) if h.get("holder_rank") is not None else None,
                h.get("holder_name"),
                to_float(h.get("circ_ratio_pct")),  # hold_ratio=流通股口径%（定性）
                h.get("share_nature"),
                source, now_ts(), DATA_VERSION,
            ])
    if not rows:
        return 0
    cols = [
        "ts_code", "as_of_date", "holder_rank", "holder_name", "hold_ratio",
        "share_nature",
        "source", "fetched_at", "data_version"]
    if as_of_date:
        con.execute(
            'DELETE FROM holders_snapshot WHERE ts_code=? AND as_of_date=?',
            [ts_code, as_of_date])
    else:
        delete_where(con, "holders_snapshot", "ts_code", ts_code)
    return insert_many(con, "holders_snapshot", cols, rows)


