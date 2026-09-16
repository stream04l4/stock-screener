# -*- coding: utf-8 -*-
"""lake.ingest.tencent_ingest —— T2 OHLCV / T3 估值 / T7 指数（复用腾讯客户端）。

数据流（调研报告 §3）：
- T2 OHLCV = 腾讯 raw K线（web.ifzq.gtimg.cn fqkline/get，与 TencentKlineSource 同端点）；
  volume 手→股 ×100（Q8 BaoStock 口径）；amount 腾讯日K行不提供 → NULL（不硬造）。
- T3 估值 = 腾讯快照 idx45/44/39/46/38（total_mv/float_mv/pe_ttm/pb/turnover）；
  ttm_yield_pct 主用本地派生（Q3），idx64 仅交叉校验 → 本层存 idx64 作参考、派生在 factors。
- T7 四指数 = 同 K线端点，index_code=sh000001/sh000300/sz399001/sh000922；amount 缺→NULL。

复用：``screener.data.tencent.TencentClient``（GBK/重试）+ ``TencentKlineSource.URL``
端点常量。**不新写抓取逻辑**——只把同一端点的响应解析到完整 OHLCV 行。

v6.0.7（history 全史补库运行时缺陷修复）：腾讯 fqkline 端点 **n 上限=2000**
（TL 实测 n>2000 → ``param error``），且支持日期段参数
``param={tcode},day,{start},{end},{n},{fq}``。新增 :func:`fetch_kline_full_history`
从最新往回分页翻到 IPO 边界（旧 ``fetch_kline_ohlcv(n=12000)`` 全市场取空）。
**失败语义与 fetch_kline_ohlcv 不同**：单页重试耗尽 → 抛 RuntimeError（不静默
返回 []——旧行为是 done 键毒化根因，见 v6.0.7 brief 缺陷 3）。

v6.0.10（停止收尾加速）：重试循环每轮检查 :func:`lake.backfill.stop_requested`
全局标志（Web"停止同步"按钮 → SIGTERM handler 置位）→ 收到停止信号**提前中断
当前重试**（抛 StopRequestedError / 按既有失败语义返回 []），不必等满退避——
单任务收尾从"卡几分钟"降到秒级。非 lake 场景标志恒 False，行为零变化。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Sequence

from ..backfill import StopRequestedError, stop_requested as _bk_stop_requested
from .common import DATA_VERSION, clean_date, now_ts, to_float, upsert

log = logging.getLogger("lake.ingest.tencent")


def _stop_requested() -> bool:
    """v6.0.10：全局停止标志（lazy 兜底——backfill 不可用时恒 False，行为不变）。"""
    try:
        return _bk_stop_requested()
    except Exception:  # noqa: BLE001 - 防御：标志不可用 → 不中断（原行为）
        return False

# 四指数（调研报告 §3 T7；腾讯格式，无点）
INDEX_CODES = ["sh000001", "sh000300", "sz399001", "sh000922"]

# K线端点（与 sources.TencentKlineSource.URL 一致；fq=''=raw 不复权）
_KLINE_URL = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
              "?param={tcode},day,,,{n},{fq}")


def _bs_to_tcode(ts_code: str) -> str:
    """sh.601398 → sh601398；已是腾讯格式（无点）→ 原样。"""
    return ts_code.replace(".", "")


# ---------------------------------------------------------------------------
# fetch（网络）
# ---------------------------------------------------------------------------
def fetch_kline_ohlcv(client, ts_code: str, n: int) -> List[Dict[str, Any]]:
    """腾讯 raw 日K完整 OHLCV 行（升序，末行=最新交易日）。

    :param client: TencentClient（复用其 session/超时/重试语义；本函数用 session.get）。
    :return: [{date, open, high, low, close, volume}]；失败/无数据 → []。
        volume 单位=手（调用方 ×100 转股）；amount 不提供（→NULL）。
    """
    import requests

    tcode = _bs_to_tcode(ts_code)
    url = _KLINE_URL.format(tcode=tcode, n=n, fq="")
    timeout = float(getattr(client, "timeout", 15))
    max_attempts = int(getattr(client, "max_attempts", 3))
    for attempt in range(1, max_attempts + 1):
        # v6.0.10：收到停止信号 → 提前中断（抛错不标 done，下轮续传；不返回 []——
        # p0 worker 对空结果仍 mark_done，会把未取数误标完成）
        if _stop_requested():
            raise StopRequestedError("腾讯K线重试中收到停止信号，提前中断")
        try:
            resp = client.session.get(url, timeout=timeout)
            if resp.status_code != 200:
                log.warning("腾讯K线 HTTP %s %s (attempt %d)", resp.status_code, ts_code, attempt)
                break
            body = json.loads(resp.content.decode("utf-8", errors="replace"))
            data = body.get("data")
            if not isinstance(data, dict):
                log.warning("腾讯K线响应异常 data=%r %s: %s", type(data).__name__, ts_code, body.get("msg", ""))
                return []
            node = data.get(tcode, {})
            rows = node.get("day") or []
            out: List[Dict[str, Any]] = []
            for r in rows:
                try:
                    d = clean_date(r[0])
                    if not d:
                        continue
                    out.append({
                        "date": d,
                        "open": to_float(r[1]),
                        "close": to_float(r[2]),
                        "high": to_float(r[3]),
                        "low": to_float(r[4]),
                        "volume": to_float(r[5]),  # 手
                    })
                except (IndexError, TypeError):
                    continue
            return out
        except (requests.RequestException, ValueError) as exc:
            log.warning("腾讯K线请求失败 %s (attempt %d): %s", ts_code, attempt, exc)
    return []


def _kline_page_url(tcode: str, start: Optional[str], end: Optional[str],
                    n: int) -> str:
    """日期段分页 URL：``param={tcode},day,{start},{end},{n},{fq}``（fq=''=raw）。

    首页 start/end 留空 = 最新 N 根；后续页 end=上一页首日-1天、start 留空。
    """
    return (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
            f"?param={tcode},day,{start or ''},{end or ''},{n},")


def _fetch_kline_page(client, tcode: str, start: Optional[str], end: Optional[str],
                      n: int) -> List[Dict[str, Any]]:
    """取一页 raw 日K（升序）。**单页重试耗尽 → 抛 RuntimeError**（不静默返回 []）。

    为什么必须抛错：旧 ``fetch_kline_ohlcv`` 失败返回 []，history worker 拿不到
    失败信号仍 mark_done → done 键毒化（v6.0.7 brief 缺陷 3：204 个 kline_history
    done 键被误标）。全史路径上"取空"与"真没数据"必须可区分——重试耗尽即视为
    网络/服务端故障，抛错让 worker 失败（不 mark_done，下轮重跑幂等）。
    """
    import requests

    url = _kline_page_url(tcode, start, end, n)
    timeout = float(getattr(client, "timeout", 15))
    max_attempts = int(getattr(client, "max_attempts", 3))
    last_err = ""
    for attempt in range(1, max_attempts + 1):
        # v6.0.10：收到停止信号 → 提前中断（StopRequestedError ⊂ RuntimeError，
        # 与"单页重试耗尽"同走失败语义——worker 不 mark_done，下轮续传）
        if _stop_requested():
            raise StopRequestedError("腾讯K线全史重试中收到停止信号，提前中断")
        try:
            resp = client.session.get(url, timeout=timeout)
            if resp.status_code != 200:
                last_err = f"HTTP {resp.status_code}"
                log.warning("腾讯K线全史 HTTP %s tcode=%s (attempt %d)",
                            resp.status_code, tcode, attempt)
                continue
            body = json.loads(resp.content.decode("utf-8", errors="replace"))
            data = body.get("data")
            if not isinstance(data, dict):
                # param error / 网关异常（data=list 等）——重试，与网络失败同语义
                last_err = f"响应异常 data={type(data).__name__} msg={body.get('msg', '')!r}"
                log.warning("腾讯K线全史 %s tcode=%s (attempt %d)", last_err, tcode, attempt)
                continue
            node = data.get(tcode, {})
            rows = node.get("day") or []
            out: List[Dict[str, Any]] = []
            for r in rows:
                try:
                    d = clean_date(r[0])
                    if not d:
                        continue
                    out.append({
                        "date": d,
                        "open": to_float(r[1]),
                        "close": to_float(r[2]),
                        "high": to_float(r[3]),
                        "low": to_float(r[4]),
                        "volume": to_float(r[5]),  # 手（load_t2 ×100 转股）
                    })
                except (IndexError, TypeError):
                    continue
            return out
        except (requests.RequestException, ValueError) as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            log.warning("腾讯K线全史请求失败 tcode=%s (attempt %d): %s", tcode, attempt, exc)
    raise RuntimeError(f"腾讯K线全史单页重试 {max_attempts} 次仍失败 "
                       f"tcode={tcode} start={start or ''} end={end or ''}: {last_err}")


def fetch_kline_full_history(client, ts_code: str, page_size: int = 2000) -> List[Dict[str, Any]]:
    """腾讯 raw 日K **全史**（从最新往回分页翻到 IPO 边界；升序合并、去重 by date）。

    翻页算法（TL 实测 sh601398 三页验证）：
      page1 无日期段 n=page_size → 最新 N 根；
      page2 end=page1首日-1天 → 再取 N 根；
      ……直到某页返回空（超出 IPO 前）或行数 < 请求数（到 IPO 边界）。

    :param client: TencentClient（复用 session/timeout/max_attempts；单页重试语义同
        ``fetch_kline_ohlcv``，但**重试耗尽抛 RuntimeError** 而非返回 []）。
    :param page_size: 每页行数。腾讯端点 **n 上限=2000**（TL 实测 n>2000 → param
        error）→ 超过即 clamp 到 2000，绝不越界发请求。
    :return: [{date, open, high, low, close, volume}] 全量升序；volume 单位=手。
    :raises RuntimeError: 任一页重试耗尽（整轮失败或部分成功）——宁缺毋滥：部分
        数据不返回，调用方重跑幂等（load_t2 upsert）。

    ``fetch_kline_ohlcv`` 签名与行为保持不变（p0/T7/smoke 调用方零影响）。
    """
    import datetime as _dt

    tcode = _bs_to_tcode(ts_code)
    # n 上限=2000：clamp 而非报错——调用方传大值时降级为合法页大小，不越界打接口
    n = max(1, min(int(page_size), 2000))

    merged: Dict[str, Dict[str, Any]] = {}   # date → row（去重 by date）
    end: Optional[str] = None                # None=首页（最新 N 根）
    while True:
        page = _fetch_kline_page(client, tcode, None, end, n)
        if not page:
            break                            # 空页：翻到 IPO 边界之前 / 无更多数据
        for row in page:
            merged.setdefault(row["date"], row)
        oldest = min(r["date"] for r in page)
        if len(page) < n:
            break                            # 行数<请求数 → 已到 IPO 边界（最后页）
        d = _dt.date.fromisoformat(oldest) - _dt.timedelta(days=1)
        end = d.isoformat()                  # 下一页 end=本页首日-1天，继续往回翻

    rows = [merged[d] for d in sorted(merged)]   # 全量升序（ISO 日期字典序=时间序）
    if not rows:
        raise RuntimeError(f"腾讯K线全史取空 tcode={tcode}（重试耗尽后仍无任何数据页）")
    return rows


def fetch_snapshot(client, ts_codes: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """腾讯批量快照（复用 TencentClient.fetch）。返回 {ts_code: {...}}。"""
    return client.fetch(list(ts_codes))


# ---------------------------------------------------------------------------
# load（→ DuckDB，零网络）
# ---------------------------------------------------------------------------
def load_t2(con, ts_code: str, kline_rows: Sequence[Dict[str, Any]],
            adj_map: Optional[Dict[str, float]] = None,
            is_st: int = 0, source: str = "tencent",
            conflict_src: Optional[str] = None,
            volume_is_shares: bool = False) -> int:
    """T2 upsert：OHLCV + amount（若行提供）+ adj_factor（前向填充）。

    :param kline_rows: K线行（升序）。volume 单位由 ``volume_is_shares`` 决定：
        False=手（腾讯口径，×100 转股，Q8 既有行为）；True=股（新浪/tdx/BaoStock
        口径，v6.1 多源——原样入库不换算）。amount=None → NULL（腾讯不提供，不硬造）。
    :param adj_map: {date: af}——**事件日→af**（仅除权日有行）；本函数前向填充到每个交易日。
        None → adj_factor 全 NULL（hfq/qfq view 该段为 NULL，属预期）。
    :param conflict_src: v6.1 跨源分歧摘要（≤256B）。**v6.1 DEF-1：kline_daily 每次
        写入都显式落列**——有分歧=摘要；无分歧（None）→ 显式写 NULL（write_null 哨兵），
        REPLACE 后不残留上一次写入的陈旧值（审计列必须反映本次写入）。
    """
    from .common import forward_fill_af, write_null

    dates = [r["date"] for r in kline_rows]
    af_filled = forward_fill_af(dates, adj_map or {})
    rows = []
    for r in kline_rows:
        vol = r.get("volume")
        if vol is not None:
            vol = vol if volume_is_shares else int(vol * 100)  # 手→股（Q8）/ 股原样
        rows.append([
            ts_code, r["date"],
            r.get("open"), r.get("high"), r.get("low"), r.get("close"),
            vol,
            r.get("amount"),          # amount：新浪/tdx/BaoStock 提供；腾讯 None→NULL
            None,                    # pct_chg：快照口径，K线行不含 → NULL
            int(is_st),
            None,                    # preclose：K线行不含 → NULL
            af_filled.get(r["date"]),
            source, now_ts(), DATA_VERSION,
        ])
    return upsert(con, "kline_daily", [
        "ts_code", "date", "open", "high", "low", "close", "volume",
        "amount", "pct_chg", "is_st", "preclose", "adj_factor",
        # DEF-1：conflict_src 三态——摘要字符串原样；None→显式 NULL（write_null）。
        # kline_daily 每次写入必落列，REPLACE 后不留上一次写入的陈旧残留。
        "source", "fetched_at", "data_version"], rows,
        conflict_src=conflict_src if conflict_src is not None else write_null())


def load_t3(con, ts_code: str, snap: Dict[str, Any], date: str,
            source: str = "tencent") -> int:
    """T3 upsert：估值快照（腾讯 idx45/44/39/46/38）。

    ttm_yield_pct 存 idx64（参考/交叉校验）；主用本地派生值由 factors.py 覆盖写入。
    """
    row = [
        ts_code, date,
        snap.get("total_mv_yi"),   # idx45 总市值(亿)
        snap.get("float_mv_yi"),   # idx44 流通市值(亿)
        snap.get("pe_ttm"),        # idx39 PE(TTM)
        snap.get("pb"),            # idx46 PB
        snap.get("turnover"),      # idx38 换手率%
        snap.get("ttm_yield_pct"), # idx64 TTM股息率%（交叉校验源）
        source, now_ts(), DATA_VERSION,
    ]
    return upsert(con, "valuation_daily", [
        "ts_code", "date", "total_mv", "float_mv", "pe_ttm", "pb",
        "turnover_pct", "ttm_yield_pct", "source", "fetched_at", "data_version"], [row])


def load_t7(con, index_code: str, kline_rows: Sequence[Dict[str, Any]],
            source: str = "tencent", conflict_src: Optional[str] = None) -> int:
    """T7 upsert：指数日K（amount 行提供则写、缺→NULL）。

    :param conflict_src: v6.1 跨源分歧摘要（close 腾讯 vs tdx >0.3%；None=NULL）。
    """
    rows = []
    for r in kline_rows:
        vol_hand = r.get("volume")
        rows.append([
            index_code, r["date"],
            r.get("open"), r.get("high"), r.get("low"), r.get("close"),
            int(vol_hand * 100) if vol_hand is not None else None,
            r.get("amount"),  # amount：行提供则写（tdx 补缺口）；缺→NULL（不硬造）
            source, now_ts(), DATA_VERSION,
        ])
    return upsert(con, "index_daily", [
        "index_code", "date", "open", "high", "low", "close", "volume",
        "amount", "source", "fetched_at", "data_version"], rows, conflict_src=conflict_src)
