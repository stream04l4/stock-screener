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
    """T6 灌入：前十大股东快照（controller_* 恒 NULL——本期无源，待补源）。

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
                None,  # controller_name：待补源（TL Q1）
                None,  # controller_type：待补源
                None,  # controller_ratio：待补源
                source, now_ts(), DATA_VERSION,
            ])
    if not rows:
        return 0
    cols = [
        "ts_code", "as_of_date", "holder_rank", "holder_name", "hold_ratio",
        "share_nature", "controller_name", "controller_type", "controller_ratio",
        "source", "fetched_at", "data_version"]
    if as_of_date:
        con.execute(
            'DELETE FROM holders_snapshot WHERE ts_code=? AND as_of_date=?',
            [ts_code, as_of_date])
    else:
        delete_where(con, "holders_snapshot", "ts_code", ts_code)
    return insert_many(con, "holders_snapshot", cols, rows)


# ---------------------------------------------------------------------------
# T5 OCF / roe_weighted（fetch：复用 SinaClient._get + parse_cf_report）
# ---------------------------------------------------------------------------
def fetch_quarterly_ocf(client, code6: str, max_pages: int = 3) -> List[Dict[str, Any]]:
    """抓单只财务 JSON（翻页至最近已披露期）→ 各报告期 OCF/ROEWEIGHTED。

    :return: [{period(YYYYQn), report_date, publish_date, ocf(元), roe_weighted_pct}]，
        按 report_date 降序；接口失败 → SinaDataError（由 client 熔断语义抛出）。

    复用：``SinaClient._get``（限速/WAF退避/熔断）+ ``parse_cf_report``（解析器）。
    """
    from screener.data.sina import (SINA_CF_REFERER, SINA_CF_URL, parse_cf_report)

    headers = {"Referer": SINA_CF_REFERER, "User-Agent": "Mozilla/5.0"}
    reports: List[Dict[str, Any]] = []
    num = int(getattr(client, "cf_reports_num", 20))
    for page in range(1, max_pages + 1):
        url = SINA_CF_URL.format(code6=code6, page=page, num=num)
        resp = client._get(url, "cf", headers=headers)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise RuntimeError(f"新浪财务JSON {code6} 非 JSON 响应: {exc}") from exc
        page_reports = parse_cf_report(payload)
        if not page_reports:
            break
        reports.extend(page_reports)
        if len(page_reports) < num:
            break  # 已到最后一页
    out: List[Dict[str, Any]] = []
    for r in reports:
        rd = str(r.get("report_date") or "")
        if not rd:
            continue
        period = f"{rd[:4]}Q{(int(rd[5:7]) - 1) // 3 + 1}"
        out.append({
            "period": period,
            "report_date": rd,
            "publish_date": r.get("publish_date"),
            "ocf": to_float(r.get("ocf")),
            "roe_weighted_pct": to_float(r.get("roe_weighted_pct")),
        })
    return out


def load_t5_ocf(con, ts_code: str, reports: Sequence[Dict[str, Any]],
                source: str = "sina_cf") -> int:
    """T5 补 ocf/roe_weighted（**不覆盖 BaoStock 侧已有列**——新浪只填这两列）。

    行已存在 → UPDATE 两列；不存在（BaoStock 未灌）→ INSERT OR REPLACE 最小行
    （其余列 NULL，待 BaoStock 侧后续补——但整行替换会丢 ocf，故改为"先查后写"：
    缺行时 INSERT 只含新浪两列 + 溯源，不碰其他列）。幂等：重复跑值相同。
    """
    n = 0
    for r in reports:
        exists = con.execute(
            "SELECT 1 FROM fundamentals_quarterly WHERE ts_code=? AND period=?",
            [ts_code, r["period"]]).fetchone()
        if exists:
            con.execute(
                "UPDATE fundamentals_quarterly SET ocf=?, roe_weighted=?, fetched_at=? "
                "WHERE ts_code=? AND period=?",
                [r.get("ocf"), r.get("roe_weighted_pct"), now_ts(), ts_code, r["period"]])
        else:
            # 全参数化（13 列全 ?，NULL 显式传 None）——避免字面 NULL 与占位符错位
            con.execute(
                "INSERT INTO fundamentals_quarterly "
                "(ts_code, period, pub_date, roe_avg, roe_weighted, yoy_pni, npi, ocf, "
                " gross_margin, liability_pct, source, fetched_at, data_version) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [ts_code, r["period"], r.get("publish_date"), None,
                 r.get("roe_weighted_pct"), None, None, r.get("ocf"),
                 None, None, source, now_ts(), DATA_VERSION])
        n += 1
    return n
