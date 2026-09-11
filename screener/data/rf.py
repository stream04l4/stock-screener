# -*- coding: utf-8 -*-
"""v5 Round-2 10Y 国债收益率源（TL D4'：TradingEconomics，东财 RPTA_WEB_TREASURYYIELD 停用）。

背景（brief_round2）：中债官网 API 405/404、stooq JS 挑战、FRED 404 ——
**TE 是唯一实测可达的 rf 源**（TL 亲自 curl 验证，evidence_r2/te_cn.html）。

页面结构（te_cn.html 留样实测）：
- JSON-LD ``Dataset`` 节点：``dateModified: "20260910T12:00:00.00Z"``（数据日期，UTC 中午）
  + ``description: "...eased to 1.68% on September 10, 2026..."``；
- 正文 stats 卡同句重复出现。

解析策略（双通道交叉验证，防单点漂移）：
1. 主值 = JSON-LD description / metaDesc 中 ``eased|rose|fell|held to X% on <Month> D, YYYY``；
2. 日期 = JSON-LD dateModified（YYYYMMDD）；若与正文月份/日不一致 → 以正文为准并告警。

落盘 ``cache/rf_10y_daily.csv``（date, yield_pct 百分数口径，供追溯）：每日运行抓取一次
（Phase 1 不要求历史序列——div_yield_percentile 只用本地价格+分红史，与 rf 无关）。
解析失败 → config ``risk_free_10y_fallback_pct``(=2.0) + **告警不静默**（D4'）。
sanity 区间 [0.5%, 4.0%]：越界告警不静默（防页面改版漂移）。
"""
from __future__ import annotations

import csv
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

from .baostock_client import DataSourceError

log = logging.getLogger("screener.data.rf")

RF_CACHE_FILE = "rf_10y_daily.csv"
# 与 em.py 本地缓存同哨兵（独立声明，避免 import em——D-EM 纪律）
_RF_SENTINEL = "stock-screener-em-cache-v1"

_EN_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}

# "eased to 1.68% on September 10, 2026"（TE 固定句式；动词覆盖 eased/rose/fell/held/climbed/dipped）
_YIELD_SENTENCE_RE = re.compile(
    r"(?:eased|rose|fell|held|climbed|dipped|slid)\s+to\s+(\d+(?:\.\d+)?)\s*%\s+"
    r"on\s+([A-Z][a-z]+)\s+(\d{1,2}),\s+(\d{4})", re.I)


class RFDataError(DataSourceError):
    """TE 接口级失败（请求重试耗尽）。解析失败不抛——走 fallback + 告警（D4'）。"""


# ---------------------------------------------------------------------------
# 纯解析函数（离线可测；fixture = evidence_r2/te_cn.html）
# ---------------------------------------------------------------------------

def parse_te_page(html: str) -> Optional[Tuple[str, float]]:
    """TE 中国 10Y 页面（已解码 str）→ (date 'YYYY-MM-DD', yield_pct 百分数)。

    双通道：JSON-LD Dataset（dateModified + description）优先；失败回退 metaDesc。
    解析不出 → None（调用方走 fallback + 告警）。
    """
    # 通道1：JSON-LD Dataset 节点
    for m in re.finditer(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', html, re.S):
        try:
            obj = json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            continue
        nodes = obj.get("@graph") if isinstance(obj, dict) else None
        if not isinstance(nodes, list):
            nodes = [obj] if isinstance(obj, dict) else []
        for node in nodes:
            if not isinstance(node, dict) or "Dataset" not in str(node.get("@type", "")):
                continue
            desc = str(node.get("description") or "")
            ym = _YIELD_SENTENCE_RE.search(desc)
            if not ym:
                continue
            yield_pct = float(ym.group(1))
            month = _EN_MONTHS.get(ym.group(2).lower())
            day, year = int(ym.group(3)), int(ym.group(4))
            if not month or not (1 <= day <= 31):
                continue
            d_iso = f"{year:04d}-{month:02d}-{day:02d}"
            # dateModified（YYYYMMDD）与正文日期交叉验证（不一致 → 以正文为准 + 告警）
            dm = str(node.get("dateModified") or "")[:8]
            if re.fullmatch(r"\d{8}", dm):
                dm_iso = f"{dm[:4]}-{dm[4:6]}-{dm[6:]}"
                if dm_iso != d_iso:
                    log.warning("[RF] TE 日期不一致: JSON-LD dateModified=%s vs 正文=%s（以正文为准）",
                                dm_iso, d_iso)
            return d_iso, yield_pct
    # 通道2：meta description（同句式；无独立日期源 → 用句子内日期，但无法交叉验证）
    m = re.search(r'<meta[^>]*name="description"[^>]*content="([^"]*)"', html)
    if m:
        ym = _YIELD_SENTENCE_RE.search(m.group(1))
        if ym:
            month = _EN_MONTHS.get(ym.group(2).lower())
            day, year = int(ym.group(3)), int(ym.group(4))
            if month and 1 <= day <= 31:
                return f"{year:04d}-{month:02d}-{day:02d}", float(ym.group(1))
    return None


def _read_rf_csv(path: str) -> Optional[List[List[str]]]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            reader = csv.reader(f)
            sentinel = next(reader, None)
            if not sentinel or sentinel[0] != _RF_SENTINEL:
                return None
            next(reader, None)  # 表头
            return [row for row in reader]
    except (OSError, csv.Error) as exc:
        log.warning("[RF] rf_10y_daily.csv 读取失败 %s: %s", path, exc)
        return None


def _atomic_write_rf_csv(path: str, rows: List[List[str]]) -> None:
    tmp = path + f".tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow([_RF_SENTINEL])
        w.writerow(["date", "yield_pct"])
        for r in rows:
            w.writerow(r)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# 取数 + 落盘（每日一次）
# ---------------------------------------------------------------------------

def fetch_rf_10y(
    rf_cfg: Dict[str, Any], cache_dir: str, run_day: str,
    session: Optional[requests.Session] = None,
) -> Tuple[float, Dict[str, Any]]:
    """抓 TE 10Y 现值 → 落盘 ``cache/rf_10y_daily.csv``（date, yield_pct）→ 返回小数。

    流程（TL D4'）：
    1. 请求页面（timeout=15s，重试 <=2 次；串行单请求，无批量压力）；
    2. parse_te_page → (date, yield_pct)；**解析失败 → fallback_pct + 告警不静默**；
    3. sanity 区间 [lo, hi]：越界告警不静默（防漂移）；
    4. 落盘当日行（同 date 覆盖、升序保持）——供追溯。

    :return: (rf_10y **小数** e.g. 0.0168, meta{date, yield_pct, source})
        source ∈ {"tradingeconomics", "fallback"}。
        请求本身失败（重试耗尽）也走 fallback——Phase 1 rf 只影响 yield_spread 一个
        子因子，fail-fast 会炸全市场，违背"解析失败→fallback+告警"的 D4' 语义。
    """
    timeout = 15.0
    max_attempts = 2
    fallback_pct = float(rf_cfg["fallback_pct"])
    lo, hi = rf_cfg["sanity_pct"]
    sess = session or requests.Session()

    html: Optional[str] = None
    last_err = "unknown"
    for attempt in range(1, max_attempts + 2):
        try:
            resp = sess.get(rf_cfg["url"], timeout=timeout,
                            headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code == 200:
                html = resp.content.decode("utf-8", errors="replace")
                break
            last_err = f"HTTP {resp.status_code}"
        except requests.RequestException as exc:
            last_err = f"{type(exc).__name__}: {exc}"
        if attempt <= max_attempts:
            time.sleep(1.0 * attempt)

    meta: Dict[str, Any] = {"date": "", "yield_pct": None, "source": "fallback"}
    parsed: Optional[Tuple[str, float]] = None
    if html is not None:
        try:
            parsed = parse_te_page(html)
        except Exception as exc:  # noqa: BLE001 - 解析异常同样走 fallback（D4'）
            last_err = f"parse error: {exc}"
    if parsed is None:
        log.warning("[RF] TE 10Y 解析失败(%s) → 回退 config fallback=%.2f%%（告警不静默，D4'）",
                    last_err, fallback_pct)
        rf_decimal = fallback_pct / 100.0
    else:
        d_iso, y_pct = parsed
        if not (lo <= y_pct <= hi):
            log.warning("[RF] 契约告警: TE 10Y=%.2f%% 越界 sanity[%.1f%%, %.1f%%]（页面改版?人工核对）",
                        y_pct, lo, hi)
        meta = {"date": d_iso, "yield_pct": y_pct, "source": "tradingeconomics"}
        rf_decimal = y_pct / 100.0

    # 落盘（仅真实解析成功时写——fallback 值不落盘，避免污染追溯序列）
    if meta["source"] == "tradingeconomics":
        path = os.path.join(cache_dir, RF_CACHE_FILE)
        rows = _read_rf_csv(path) or []
        by_date: Dict[str, str] = {}
        for r in rows:
            if len(r) >= 2 and r[0]:
                by_date[r[0]] = r[1]
        by_date[meta["date"]] = f"{meta['yield_pct']:.4f}"
        new_rows = [[d, by_date[d]] for d in sorted(by_date)]
        _atomic_write_rf_csv(path, new_rows)
        log.info("[RF] 10Y国债(TE) %s=%.4f%% 落盘 %s（累计 %d 行）",
                 meta["date"], meta["yield_pct"], path, len(new_rows))

    # PIT 语义：run_day 之前的运行日不得"看见"未来数据——本函数只返回**现值**，
    # 调用方按 run_day 使用（每日运行时 date≈run_day；历史回测场景 Phase 1 不涉及）。
    return rf_decimal, meta


def load_rf_10y_asof(cache_dir: str, run_day: str) -> Optional[float]:
    """读 ``rf_10y_daily.csv`` 中 <= run_day 的最近一行（**小数**，PIT）。

    无缓存/无可见行 → None（yield_spread 因子记缺失，不阻塞主流程）。
    """
    rows = _read_rf_csv(os.path.join(cache_dir, RF_CACHE_FILE))
    if not rows:
        return None
    best_d, best_y = "", None
    for r in rows:
        d = str(r[0]) if len(r) > 0 else ""
        try:
            y = float(r[1]) if len(r) > 1 and r[1] else None
        except ValueError:
            y = None
        if d and y is not None and d <= run_day and d > best_d:
            best_d, best_y = d, y
    return None if best_y is None else best_y / 100.0
