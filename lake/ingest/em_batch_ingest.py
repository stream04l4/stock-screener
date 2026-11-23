# -*- coding: utf-8 -*-
"""lake.ingest.em_batch_ingest —— 东财 push2/datacenter 全市场单日批量（tushare 替代主通道）。

Joel 2026-11-15 拍板：全面取代 tushare（每日增量 T2/T3 主通道 + T1 主档/退市 +
af 本地推导），本模块是其取数层。全部接口 2026-11-15 实测验证（字段映射/单位/
分页/限频见各函数 docstring 的实锤注记）。

通道与数据面：
- **push2 clist 快照**（``fetch_clist_snapshot``）：全市场 5564 股 A 股
  （沪主板/科创/深主板/创业，无 B 股/北交所，fs 口径实测 2026-11-15）单次拉取
  56 页 ~1 分钟。字段（实锤映射，平安银行/中船防务 2026-11-15 样例验证）：
    f12=6位码  f14=名称  f2=close  f3=pct_chg%  f17=open  f15=high  f16=low
    f18=preclose  f5=volume(手)  f6=amount(元)  f8=turnover%
    f20=总市值(元)  f21=流通市值(元)  f26=上市日(YYYYMMDD)  f100=东财行业
    f115=pe_ttm  f167=pb
  单位换算：volume 手→股(×100)；市值 元→亿元(÷1e8)——与 tushare 批量行同口径。
- **datacenter 分红送配**（``fetch_ex_events``）：``RPT_SHAREBONUS_DET``
  按除权日过滤（filter **必须括号包裹**，实测 2026-11-15：不包 → count=None；
  包 → 正常）。全市场单日最多 ~18 条（11 月全月 115 条），1 页取完。
  字段：PRETAX_BONUS_RMB=现金/10股(元) → C=每股现金；BONUS_RATIO/IT_RATIO=
  送/转 每10股(股) → B=(送+转)/10 每股；ALLOT_RATIO=配（近一年 0 样本）。
- **af 推导**（``derive_af_batch``）：官方除权参考价口径
  ``step = c0·(1+B)/(c0−C)``（= c0/ref，ref=(c0−C)/(1+B)），c0=除权前一交易日
  湖内 close（2 位小数=交易所报价分辨率，与 tushare 同精度）。5002 条 2025-11~
  2026-11 除权事件对照湖内 tushare af 实测：绝大多数匹配 0.1% 内（残差=af 存储
  精度/低价股 0.01 元分辨率，与 tushare 自身口径同阶）。**配股 → fail-closed 抛错**
  （近一年 0 样本；出现时宁可该日批量失败走逐只，不猜配股价）。

与 tushare 批量的契约（run_incremental batch 消费面完全一致）：
- ``t2_rows``: [{ts_code, date, open, high, low, close, preclose, pct_chg,
  volume(股), amount(元)}]
- ``t3_rows``: [{ts_code, date, total_mv_yi, float_mv_yi, pe_ttm, pb,
  turnover, ttm_yield_pct(None——东财 clist 无 dv_ttm；主用值本就本地派生)}]
- ``af``: {ts_code: {date: af}} **全股票**（事件股=推导新值；非事件股=前一日
  level 沿用）——load_t2 前向填充契约要求批量日每只都有值（tushare 同口径）。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

from .common import clean_date, to_float, to_ts_code

log = logging.getLogger("lake.ingest.em_batch")

_PUSH2_CLIST = "https://push2.eastmoney.com/api/qt/clist/get"
_PUSH2DELAY_CLIST = "https://push2delay.eastmoney.com/api/qt/clist/get"
_SG_HOST = "oci"   # 新加坡备用出口（Joel 2026-11-15 提醒：EU push2 被 WAF 软封时走 SG）
_DATACENTER = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_UT = "bd1d9ddb04089700cf9c27f6f7426281"
# 沪深 A 股（沪主板 t:6 / 科创 t:80 / 深主板 t:2 / 创业 t:23）——无 B 股、无北交所
# （universe 口径与 tushare market 过滤一致：沪深）。
_FS_A = "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23"
_FIELDS = "f2,f3,f5,f6,f8,f12,f14,f15,f16,f17,f18,f20,f21,f26,f100,f115,f167"
_PAGE_SIZE = 100
_TIMEOUT_S = 30

_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) stock-screener/7.0"}


class EmBatchError(RuntimeError):
    """东财批量通道数据级失败（行数异常/配股事件/接口漂移）。"""


def _get_json(url: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """GET + JSON（带退避重试；502/504/429 常见于 push2 连发分页，实测
    2026-11-15 第 4 页 502——退避 2/5/10s 三连后仍失败才抛）。"""
    last: Optional[Exception] = None
    for i in range(3):
        try:
            r = requests.get(url, params=params, headers=_UA, timeout=_TIMEOUT_S)
            if r.status_code in (429, 500, 502, 503, 504):
                raise EmBatchError(f"HTTP {r.status_code}（限频/网关抖动）")
            r.raise_for_status()
            return r.json()
        except Exception as exc:  # noqa: BLE001 - 重试后抛
            last = exc
            if i < 2:
                time.sleep(2.0 + i * 3.0)
    raise EmBatchError(f"东财批量取数失败 {url}: {last}")


def _ymd8(v: Any) -> Optional[str]:
    """YYYYMMDD → YYYY-MM-DD（东财日期字段统一 8 位数字）。"""
    s = str(v).strip() if v is not None else ""
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return clean_date(v)


def fetch_clist_snapshot(allow_sg: bool = True) -> List[Dict[str, Any]]:
    """全市场 A 股单次快照（56 页 ~1-2 分钟）。

    出口链路（2026-11-15 实锤）：
    1. EU 直连 push2.eastmoney.com（常态，~1 分钟）；
    2. EU 持续 502（WAF 软封，连发分页易触发）→ SG 备用出口（ssh oci，
       push2delay.eastmoney.com——**直连 delay 端**：push2 的 302 重定向会丢
       fs 参数致 total 漂移 5564→59905，实测）；
    3. 都失败 → 抛 EmBatchError（调用方回退新浪/逐只 legacy）。

    :return: [{ts_code, name, close, pct_chg, open, high, low, preclose,
               volume(股), amount(元), turnover, total_mv_yi, float_mv_yi,
               list_date, industry, pe_ttm, pb}]
    （OHLC/preclose = **最近一个交易日** 的会话数据——周末/节假日调用返回
    上周五值；盘中调用返回实时值，调用方须自行按指数最新 bar 判定，见
    run_incremental 集成处的 guard。）
    """
    egress = "eu"
    try:
        rows_raw, total = _fetch_all_pages(_PUSH2_CLIST)
    except EmBatchError as exc:
        if not allow_sg:
            raise
        log.warning("EU push2 失败 → 切 SG 备用出口（ssh %s push2delay）: %s",
                    _SG_HOST, str(exc)[:120])
        rows_raw, total = _fetch_all_pages_sg()
        egress = "sg"
    rows: List[Dict[str, Any]] = []
    for x in rows_raw:
        rows.append(_normalize_row(x))
    if not rows:
        raise EmBatchError("东财 clist 快照为空（接口异常？）")
    log.info("东财 clist 快照: %d 只（egress=%s, total=%s）", len(rows), egress, total)
    return rows


def _normalize_row(x: Dict[str, Any]) -> Dict[str, Any]:
    """clist 原始行 → 湖契约行（仅沪深 A 股；停牌 '-' → None）。"""
    c6 = str(x.get("f12") or "").zfill(6)
    if not c6 or c6[0] not in "036":   # 仅沪深 A 股（口径双保险）
        raise EmBatchError(f"clist 出现非沪深 A 股代码 {c6}（fs 参数失效？）")
    _f5 = to_float(x.get("f5"))
    _mv = to_float(x.get("f20"))
    _fmv = to_float(x.get("f21"))
    return {
        "ts_code": to_ts_code(c6),
        "name": str(x.get("f14") or ""),
        "close": to_float(x.get("f2")),
        "pct_chg": to_float(x.get("f3")),
        "open": to_float(x.get("f17")),
        "high": to_float(x.get("f15")),
        "low": to_float(x.get("f16")),
        "preclose": to_float(x.get("f18")),
        "volume": int(_f5 * 100) if _f5 is not None else None,
        "amount": to_float(x.get("f6")),
        "turnover": to_float(x.get("f8")),
        "total_mv_yi": _mv / 1e8 if _mv is not None else None,
        "float_mv_yi": _fmv / 1e8 if _fmv is not None else None,
        "list_date": _ymd8(x.get("f26")),
        "industry": str(x.get("f100") or "") or None,
        "pe_ttm": to_float(x.get("f115")),
        "pb": to_float(x.get("f167")),
    }


def _fetch_all_pages(url: str) -> Tuple[List[Dict[str, Any]], Optional[int]]:
    """EU 直连分页拉全（56 页；fid=f12 固定排序 + 按代码去重防翻页漏股）。"""
    rows_raw: List[Dict[str, Any]] = []
    seen_codes: Set[str] = set()
    page = 1
    total: Optional[int] = None
    while True:
        d = _get_json(url, {
            "pn": page, "pz": _PAGE_SIZE, "po": 0, "np": 1, "fid": "f12",
            "ut": _UT, "fltt": 2, "invt": 2, "fs": _FS_A, "fields": _FIELDS,
        })
        data = d.get("data") or {}
        total = data.get("total")
        diff = data.get("diff") or []
        if not diff:
            break
        added = 0
        for item in diff:
            c = str(item.get("f12") or "").strip()
            if c and c in seen_codes:
                continue
            if c:
                seen_codes.add(c)
            rows_raw.append(item)
            added += 1
        if total is None or len(rows_raw) >= int(total) or added == 0:
            break
        if page > int(total) // _PAGE_SIZE + 5:   # 接口漂移保护（防死循环）
            break
        page += 1
        time.sleep(0.2)   # 分页小睡（限速纪律；push2 连发易 502）
    return rows_raw, total


# SG 远程取数脚本（stdlib only；在 oci 上执行，push2delay 直连+固定排序+去重分页）
_SG_FETCH_SCRIPT = r'''
import json, time, urllib.parse, urllib.request
URL = "https://push2delay.eastmoney.com/api/qt/clist/get"
Q0 = {"pz": 100, "po": 0, "np": 1, "fid": "f12", "ut": "__UT__", "fltt": 2, "invt": 2,
      "fs": "__FS__", "fields": "__FIELDS__"}
out, seen, page, total = [], set(), 1, None
while True:
    q = dict(Q0); q["pn"] = page
    url = URL + "?" + urllib.parse.urlencode(q)
    last = None
    for i in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            d = json.loads(urllib.request.urlopen(req, timeout=30).read().decode()).get("data") or {}
            last = None
            break
        except Exception as e:   # noqa: BLE001
            last = e
            time.sleep(3 * (i + 1))
    if last is not None:
        raise SystemExit("SG page " + str(page) + " failed: " + str(last))
    diff = d.get("diff") or []
    if not diff:
        break
    added = 0
    for item in diff:
        c = str(item.get("f12") or "").strip()
        if c and c in seen:
            continue
        if c:
            seen.add(c)
        out.append(item)
        added += 1
    total = d.get("total")
    if (total and len(out) >= int(total)) or added == 0 or page > 100:
        break
    page += 1
    time.sleep(0.2)
print(json.dumps({"total": total, "rows": out}))
'''.replace("__UT__", _UT).replace("__FS__", _FS_A).replace("__FIELDS__", _FIELDS)


def _fetch_all_pages_sg() -> Tuple[List[Dict[str, Any]], Optional[int]]:
    """SG 备用出口：单次 ssh（远程 python3 拉全 56 页，JSON 回传）。

    凭据零依赖（EM 接口无鉴权；ssh 走本机 oci 别名密钥，BatchMode 防交互挂起）。
    """
    import subprocess

    r = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
         _SG_HOST, "python3", "-"],
        input=_SG_FETCH_SCRIPT.encode("utf-8"),
        capture_output=True, timeout=1500)
    if r.returncode != 0:
        raise EmBatchError(
            f"SG 出口取数失败 rc={r.returncode}: "
            f"{r.stderr.decode('utf-8', 'ignore')[:200]}")
    payload = json.loads(r.stdout.decode("utf-8").strip().splitlines()[-1])
    rows_raw = payload.get("rows") or []
    if not rows_raw:
        raise EmBatchError("SG 出口 clist 快照为空（接口异常？）")
    return rows_raw, payload.get("total")


def snapshot_to_t2t3(snap: List[Dict[str, Any]],
                     date: str) -> (List[Dict[str, Any]], List[Dict[str, Any]]):
    """快照行 → (T2 K线行, T3 估值行)（tushare 批量同契约，含单位换算）。

    停牌股（f2/f15/f16/f17 为 None/'-'）OHLC 缺失 → 该行不进 T2（T3 保留：
    停牌日估值快照仍有效，tushare 行为一致——其 daily 亦无停牌股但
    daily_basic 有）。
    """
    t2: List[Dict[str, Any]] = []
    t3: List[Dict[str, Any]] = []
    for r in snap:
        if r["close"] is not None and r["open"] is not None:
            t2.append({
                "ts_code": r["ts_code"], "date": date,
                "open": r["open"], "high": r["high"], "low": r["low"],
                "close": r["close"], "preclose": r["preclose"],
                "pct_chg": r["pct_chg"], "volume": r["volume"],
                "amount": r["amount"],
            })
        t3.append({
            "ts_code": r["ts_code"], "date": date,
            "total_mv_yi": r["total_mv_yi"],
            "float_mv_yi": r["float_mv_yi"],
            "pe_ttm": r["pe_ttm"], "pb": r["pb"],
            "turnover": r["turnover"],
            "ttm_yield_pct": None,
            # 东财 clist 无 dv_ttm（交叉校验列，主用本地派生）
        })
    return t2, t3


def _parse_ex_event_row(x: Dict[str, Any],
                        fallback_date: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """解析 datacenter RPT_SHAREBONUS_DET 单行 → 统一每股口径事件 dict。

    仅保留沪深 A 股（0/3/6 开头）；若 ASSIGN_PROGRESS 明确标为未通过/停止/取消/预案则过滤。
    """
    prog = str(x.get("ASSIGN_PROGRESS") or "").strip()
    if prog and any(bad in prog for bad in ("未通过", "停止", "取消", "不分配", "股东大会")) and "实施" not in prog:
        return None
    if prog and ("预案" in prog or "提议" in prog) and "实施" not in prog:
        return None
    c6 = str(x.get("SECURITY_CODE") or "").zfill(6)
    if not c6 or c6[0] not in "036":
        return None
    ex_d = _ymd8(x.get("EX_DIVIDEND_DATE")) or fallback_date
    if not ex_d:
        return None
    ann_d = _ymd8(x.get("IMPL_PLAN_NOTICE_DATE")) or _ymd8(x.get("PLAN_NOTICE_DATE"))
    rep_d = _ymd8(x.get("REPORT_DATE"))
    period = rep_d[:4] if rep_d else str(ex_d)[:4]
    cash = to_float(x.get("PRETAX_BONUS_RMB"))
    bonus = (to_float(x.get("BONUS_RATIO")) or 0.0) / 10.0 + \
            (to_float(x.get("IT_RATIO")) or 0.0) / 10.0
    return {
        "ts_code": to_ts_code(c6),
        "ex_date": ex_d,
        "ann_date": ann_d,
        "period": period,
        "cash": (cash / 10.0 if cash is not None else 0.0),  # /10股→每股
        "bonus": bonus,
        "has_allot": (to_float(x.get("ALLOT_RATIO")) or 0.0) > 0,
        "plan": str(x.get("IMPL_PLAN_PROFILE") or "")[:60],
    }


def fetch_ex_events(trade_date: str) -> List[Dict[str, Any]]:
    """datacenter 全市场除权日=指定日的分红送配事件（通常 ≤20 条/日）。

    :param trade_date: YYYY-MM-DD（除权日）。
    :return: [{ts_code, ex_date, cash(元/股), bonus(股/股), has_allot(bool)}]
    :raises EmBatchError: 接口异常 / 分页异常。

    **配股（has_allot=True）由调用方 fail-closed 处理**（derive_af_batch 抛错）
    ——东财无配股价字段，af 步长不可推导；近一年全市场 0 样本（2025-11~2026-11
    5002 事件实锤），属理论边界。
    """
    filter_s = f"(EX_DIVIDEND_DATE='{trade_date}')"
    rows: List[Dict[str, Any]] = []
    page = 1
    while True:
        d = _get_json(_DATACENTER, {
            "sortColumns": "EX_DIVIDEND_DATE", "sortTypes": "1",
            "pageSize": 500, "pageNumber": page, "reportName": "RPT_SHAREBONUS_DET",
            "columns": "SECURITY_CODE,EX_DIVIDEND_DATE,IMPL_PLAN_NOTICE_DATE,REPORT_DATE,"
                       "PRETAX_BONUS_RMB,BONUS_RATIO,IT_RATIO,ALLOT_RATIO,"
                       "IMPL_PLAN_PROFILE,ASSIGN_PROGRESS",
            "source": "WEB", "client": "WEB", "filter": filter_s,
        })
        result = d.get("result") or {}
        data = result.get("data") or []
        if not data:
            break
        for x in data:
            parsed = _parse_ex_event_row(x, fallback_date=trade_date)
            if parsed is not None:
                rows.append(parsed)
        pages = result.get("pages") or 0
        if page >= int(pages):
            break
        page += 1
    return rows


def fetch_ex_events_range(start_date: str, end_date: str) -> List[Dict[str, Any]]:
    """datacenter 全市场除权日 ∈ [start_date, end_date] 的全部分红送配事件（分页+去重）。

    用于：① 修复 dividend_events 历史停更缺口；② run_t4_sync 每日滚动增量（默认回看 14d）。
    """
    filter_s = f"(EX_DIVIDEND_DATE>='{start_date}')(EX_DIVIDEND_DATE<='{end_date}')"
    rows: List[Dict[str, Any]] = []
    seen_keys: Set[Tuple[str, str, str, float, float]] = set()
    page = 1
    while True:
        d = _get_json(_DATACENTER, {
            "sortColumns": "EX_DIVIDEND_DATE,SECURITY_CODE",
            "sortTypes": "1,1",
            "pageSize": 500,
            "pageNumber": page,
            "reportName": "RPT_SHAREBONUS_DET",
            "columns": "SECURITY_CODE,EX_DIVIDEND_DATE,IMPL_PLAN_NOTICE_DATE,REPORT_DATE,"
                       "PRETAX_BONUS_RMB,BONUS_RATIO,IT_RATIO,ALLOT_RATIO,"
                       "IMPL_PLAN_PROFILE,ASSIGN_PROGRESS",
            "source": "WEB",
            "client": "WEB",
            "filter": filter_s,
        })
        result = d.get("result") or {}
        data = result.get("data") or []
        if not data:
            break
        added = 0
        for x in data:
            parsed = _parse_ex_event_row(x, fallback_date=None)
            if parsed is None:
                continue
            dk = (
                parsed["ts_code"],
                str(parsed["ex_date"]),
                str(parsed.get("period") or ""),
                round(float(parsed.get("cash") or 0.0), 6),
                round(float(parsed.get("bonus") or 0.0), 6),
            )
            if dk in seen_keys:
                continue
            seen_keys.add(dk)
            rows.append(parsed)
            added += 1
        pages = result.get("pages") or 0
        if page >= int(pages) or added == 0 or page >= 200:
            break
        page += 1
        time.sleep(0.15)
    return rows


def derive_af_batch(con, trade_date: str,
                    events: List[Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
    """全市场单日复权因子（af 契约：每只 {date: af}；tushare 同形）。

    口径（2026-11-15 5002 事件实锤 = tushare 口径）：
    - 事件股：af_D = af_{D-1} × c0·(1+B)/(c0−C)（官方除权参考价步长）；
    - 非事件股：af_D = af_{D-1}（前一日 level 沿用）；
    - c0 = 湖内 D 前最近交易日 close（缺失=停牌 → 沿用，与 tushare 停牌日
      af 不变一致）；
    - 无任何历史行（新股首日）→ af=1.0（tushare 新股口径）；
    - 配股事件 → **fail-closed 抛 EmBatchError**（不猜配股价）。

    :param con: 只读/读写 lake 连接（本地 SQL，零网络）。
    :param trade_date: YYYY-MM-DD。
    :param events: fetch_ex_events 输出。
    """
    if any(e.get("has_allot") for e in events):
        _allot = [e for e in events if e.get("has_allot")]
        raise EmBatchError(
            f"东财 af 推导遇配股事件 {len(_allot)} 条（无配股价字段，fail-closed）："
            + ", ".join(f"{e['ts_code']}({e.get('plan','')})" for e in _allot[:3]))

    # 1) 全市场 trade_date 之前最近非 NULL af level（必须加 AND date < ?，
    #    否则除权日当天重跑会把当日已乘过一次 step 的 af 当基数再乘一次 → 双重复权）。
    levels: Dict[str, Optional[float]] = {}
    for ts, af in con.execute(
            "SELECT ts_code, adj_factor FROM kline_daily "
            "WHERE (ts_code, date) IN "
            "(SELECT ts_code, MAX(date) FROM kline_daily "
            " WHERE date < ? AND adj_factor IS NOT NULL AND adj_factor > 0 "
            " GROUP BY ts_code) "
            "AND adj_factor IS NOT NULL", [trade_date]).fetchall():
        levels[ts] = af
    # 2) 事件股的 c0（D 前最近 close；只查事件股 ≤20 只/日）与是否有历史 K线行
    ev_codes = sorted({e["ts_code"] for e in events})
    c0_map: Dict[str, float] = {}
    has_hist_kline: Set[str] = set()
    if ev_codes:
        ph = ",".join("?" * len(ev_codes))
        for ts, c0 in con.execute(
                f"SELECT ts_code, close FROM kline_daily WHERE "
                f"(ts_code, date) IN "
                f"(SELECT ts_code, MAX(date) FROM kline_daily "
                f"WHERE date < ? AND ts_code IN ({ph}) GROUP BY ts_code)",
                [trade_date] + ev_codes).fetchall():
            has_hist_kline.add(ts)
            if c0 is not None:
                c0_map[ts] = float(c0)

    out: Dict[str, Dict[str, Optional[float]]] = {}
    for ts, af_prev in levels.items():
        if af_prev is not None:
            out[ts] = {trade_date: float(af_prev)}
        # af_prev 为 None（历史 af 全 NULL 的股）→ 不写（保持 NULL，同 adj_map=None）

    # 3) 按 ts_code 聚合当日全部事件（防同日多笔分红/送转重复连乘 c0）
    agg_ev: Dict[str, Dict[str, Any]] = {}
    for e in events:
        ts = e["ts_code"]
        rec = agg_ev.setdefault(ts, {"cash": 0.0, "bonus": 0.0, "plans": []})
        rec["cash"] += float(e.get("cash") or 0.0)
        rec["bonus"] += float(e.get("bonus") or 0.0)
        if e.get("plan"):
            rec["plans"].append(str(e["plan"]))

    for ts, rec in agg_ev.items():
        c0 = c0_map.get(ts)
        if c0 is None or c0 <= 0:
            log.warning("af 推导: %s 无 D-1 close（停牌/缺数据）→ 跳过写入", ts)
            out.pop(ts, None)
            continue
        cash = rec["cash"]
        bonus = rec["bonus"]
        denom = c0 - cash
        if denom <= 0:
            raise EmBatchError(
                f"af 推导异常: {ts} c0={c0} cash={cash} 步长分母非正（数据异常，"
                f"fail-closed；plan={','.join(rec['plans'])[:60]}")
        step = c0 * (1.0 + bonus) / denom
        base = out.get(ts, {}).get(trade_date)
        if base is None:
            # SQL 已按 date < trade_date AND adj_factor IS NOT NULL 查过全史，
            # 能走到此处表示历史上无任何非 NULL af，以 1.0 起步不产生断崖
            base = 1.0
            log.warning("af 推导: %s 历史无非 NULL af → 以 1.0 起步", ts)
        out[ts] = {trade_date: base * step}
    return out


def load_ex_events(con, trade_date: str, events: List[Dict[str, Any]],
                   source: str = "em_datacenter") -> int:
    """将抓到的除权事件增量落库到 dividend_events（单事务批量幂等替换）。

    stk_div 统一按每股送转股数（bonus）存储，与 cash_dps（元/股）同口径。
    """
    from .common import DATA_VERSION, now_ts

    if not events:
        return 0
    by_key: Dict[Tuple[str, str], List[List[Any]]] = {}
    ts_now = now_ts()
    for e in events:
        if e.get("has_allot"):
            continue
        ts = e.get("ts_code")
        ex_d = e.get("ex_date") or trade_date
        cash = float(e.get("cash") or 0.0)
        bonus = float(e.get("bonus") or 0.0)
        if not ts or not ex_d or (cash == 0.0 and bonus == 0.0):
            continue
        ann_d = e.get("ann_date")
        period = str(e.get("period") or str(ex_d)[:4])
        by_key.setdefault((ts, ex_d), []).append([
            ts, ex_d, ann_d, period, cash, bonus, source, ts_now, DATA_VERSION,
        ])
    if not by_key:
        return 0
    cols = ["ts_code", "ex_date", "ann_date", "period", "cash_dps",
            "stk_div", "source", "fetched_at", "data_version"]
    all_rows = [r for grp in by_key.values() for r in grp]
    del_keys = [[ts, ex_d] for (ts, ex_d) in by_key]
    placeholders = ",".join("?" * len(cols))
    in_tx = False
    try:
        con.execute("BEGIN TRANSACTION")
        in_tx = True
    except Exception:  # noqa: BLE001 - 调用方已在事务内
        in_tx = False
    try:
        con.executemany("DELETE FROM dividend_events WHERE ts_code=? AND ex_date=?", del_keys)
        con.executemany(
            f"INSERT INTO dividend_events ({','.join(cols)}) VALUES ({placeholders})",
            all_rows)
        if in_tx:
            con.execute("COMMIT")
    except Exception:
        if in_tx:
            try:
                con.execute("ROLLBACK")
            except Exception:  # noqa: BLE001
                pass
        raise
    return len(all_rows)


def cross_check_ex_events_tushare(con, events: List[Dict[str, Any]],
                                  max_sample: int = 2) -> Dict[str, Any]:
    """用 tushare dividend 对 EM 新增事件做低频抽样交叉校验（保护免费档限频）。

    - max_sample 默认 2 只（取现金分红最大的样本）；
    - tushare 不可用 / 限频 / 网络异常仅记 warning 返回，绝不阻断 EM 主通道。
    """
    if not events or max_sample <= 0:
        return {"checked": 0, "skipped": True, "mismatches": []}
    try:
        from . import tushare_ingest as ti
        if not ti.tushare_available():
            return {"checked": 0, "skipped": True, "reason": "tushare_unavailable", "mismatches": []}
    except Exception as exc:  # noqa: BLE001
        return {"checked": 0, "skipped": True, "reason": str(exc)[:80], "mismatches": []}

    # 按每股现金分红/事件强度降序选最多 max_sample 只互不重复的股票抽样核对
    seen_codes: Set[str] = set()
    samples: List[Dict[str, Any]] = []
    for ev in sorted(events, key=lambda x: -(float(x.get("cash") or 0.0) + float(x.get("bonus") or 0.0))):
        c = str(ev.get("ts_code") or "")
        ex_d = str(ev.get("ex_date") or "")[:10]
        if not c or not ex_d or "." not in c or c in seen_codes:
            continue
        seen_codes.add(c)
        samples.append(ev)
        if len(samples) >= max_sample:
            break

    checked = 0
    mismatches: List[Dict[str, Any]] = []
    cols = {str(r[1]).lower() for r in con.execute("PRAGMA table_info('dividend_events')").fetchall()}
    has_conflict = "conflict_src" in cols
    for ev in samples:
        c = ev["ts_code"]
        ex_d = str(ev["ex_date"])[:10]
        mkt, num = c.split(".", 1)
        ts_fmt = f"{num}.{mkt.upper()}"
        try:
            ts_rows = ti.fetch_dividend(ts_fmt)
        except Exception as exc:  # noqa: BLE001 - 限频/缺token不阻断
            log.warning("T4 tushare 交叉校验跳过 %s: %s", c, str(exc)[:100])
            continue
        checked += 1
        # 查找匹配同一 ex_date 的 tushare 分红行
        matched = [
            r for r in (ts_rows or [])
            if str(r.get("ex_date") or "").replace("-", "")[:8] == ex_d.replace("-", "")
        ]
        em_cash = float(ev.get("cash") or 0.0)
        if not matched:
            if em_cash > 0.0:
                msg = f"tushare_missing:{ex_d}"
                mismatches.append({"ts_code": c, "ex_date": ex_d, "em_cash": em_cash, "tushare_cash": None})
                log.warning("T4 交叉校验分歧 %s %s: EM cash=%.4f, tushare 无该日记录", c, ex_d, em_cash)
                if has_conflict:
                    con.execute("UPDATE dividend_events SET conflict_src=? WHERE ts_code=? AND ex_date=?",
                                [msg[:256], c, ex_d])
            continue
        ts_cash = sum(float(r.get("div_cash_before_tax") or 0.0) for r in matched)
        tol = max(0.005, 0.02 * em_cash)
        if abs(em_cash - ts_cash) > tol:
            msg = f"em:{em_cash:.4f}|tushare:{ts_cash:.4f}"
            mismatches.append({"ts_code": c, "ex_date": ex_d, "em_cash": em_cash, "tushare_cash": ts_cash})
            log.warning("T4 交叉校验分歧 %s %s: %s", c, ex_d, msg)
            if has_conflict:
                con.execute("UPDATE dividend_events SET conflict_src=? WHERE ts_code=? AND ex_date=?",
                            [msg[:256], c, ex_d])
    return {"checked": checked, "skipped": False, "mismatches": mismatches}


def active_code_set(snap: List[Dict[str, Any]]) -> Set[str]:
    """快照中活跃（当前上市）股票集合（ts_code 湖格式）。"""
    return {r["ts_code"] for r in snap}
