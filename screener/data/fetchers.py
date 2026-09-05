# -*- coding: utf-8 -*-
"""BaoStock 数据抓取（带本地缓存）。

接口行为均为 2026-09-05 实测确认（见 stages/01_research/research_report.md）：
- ``query_history_k_data_plus``：adjustflag 必须传**字符串**；1=后复权, 2=前复权,
  3=不复权（原任务书 §2 标注有误，以调研报告 §6.1 为准）。
- ``query_dividend_data(code, year, yearType)``：code/year 必填；不传 year 会静默
  只返回最近 1 条 → 必须逐年循环。同一除权日可能有重复行（预案+正式两条记录），
  求和前按 (code, dividOperateDate) 去重。金额字段已是元/股，不要再除 10。
- ``query_profit_data / query_growth_data / query_balance_data``：必须传 code，
  按 (year, quarter) 取单季；未披露返回空行（error_code=0）。
- ``query_all_stock(day=...)``：不带 day 在非交易日返回空 → 始终显式传 day。
- ``query_stock_industry()``：全量含退市股，industry 可能为空。

缓存策略：个股历史数据（日K/分红/季报）不可变 → 永不过期；行业分类每周一更新
→ TTL 24h；交易日历按运行日增量刷新 → TTL 1h。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

import baostock as bs
import pandas as pd

from .baostock_client import BaoStockClient
from .cache import DiskCache, make_cache_name

log = logging.getLogger("screener.data.fetch")


def to_float(value: str) -> Optional[float]:
    """BaoStock 字符串 → float；空串/非法值 → None（缺失用 None，不用 NaN）。"""
    if value is None:
        return None
    s = str(value).strip()
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def to_int(value: str) -> Optional[int]:
    f = to_float(value)
    return None if f is None else int(f)


@dataclass
class KlineData:
    """单只股票的日K数据（后复权窗口 + 运行日不复权快照）。"""

    code: str
    dates: List[str]          # 后复权窗口的交易日序列（升序）
    closes: List[float]       # 后复权收盘价，与 dates 对齐
    tradestatus: List[int]    # 1正常/0停牌，与 dates 对齐
    last_date: str            # 窗口内最后一个有数据的日期
    n_rows: int               # 窗口内总行数（含停牌行）→ 上市时长代理
    current_price: Optional[float]  # 运行日不复权收盘价 af=3（股息率分母）
    is_st: Optional[int]      # 运行日 isST（1/0），无数据为 None
    run_day_tradestatus: Optional[int]  # 运行日 tradestatus


class DataFetcher:
    """所有 BaoStock 查询的统一入口：缓存优先，未命中才发请求。

    缓存 TTL 策略（与数据可变性挂钩）：
    - 日K（历史窗口/运行日快照）：不可变 → 永不过期。
    - 分红 year=Y：在 Y 年内除权事件仍可能新增 → run_day 未过 Y 年底时 TTL 到 Y 年底，
      否则不可变。空结果同样按此策略（避免"今年暂无分红"被永久缓存）。
    - 季报 (year, quarter)：披露截止日前为空属正常（未披露）→ run_day 早于披露截止日时
      TTL 到截止日，否则不可变。这样 10 月三季报披露后能自动探测到新报告期。
    """

    def __init__(self, client: BaoStockClient, cache: DiskCache) -> None:
        self.client = client
        self.cache = cache
        self.run_day: Optional[date] = None  # 由引擎在定位交易日后设置
        self.calls = {"cache_hit": 0, "fetched": 0}

    def set_run_day(self, run_day: date) -> None:
        self.run_day = run_day

    @staticmethod
    def _disclosure_deadline(year: int, quarter: int) -> date:
        """季报披露截止日（监管要求的最晚披露时点，之后为空=确实缺失）。"""
        return {
            1: date(year, 4, 30),
            2: date(year, 8, 31),
            3: date(year, 10, 31),
            4: date(year + 1, 4, 30),
        }[quarter]

    def _ttl_hours_until(self, target: date) -> Optional[float]:
        """run_day 早于 target → TTL 到 target；否则 None（不可变，永不过期）。"""
        if self.run_day is None or self.run_day >= target:
            return None
        hours = (target - self.run_day).total_seconds() / 3600.0
        return max(1.0, hours)

    def _dividend_ttl(self, year: int) -> Optional[float]:
        """分红数据在自然年内可变（新除权事件），年底后不可变。"""
        return self._ttl_hours_until(date(year + 1, 1, 1))

    def _fundamental_ttl(self, year: int, quarter: int) -> Optional[float]:
        return self._ttl_hours_until(self._disclosure_deadline(year, quarter))

    # ---------- 缓存包装 ----------
    def _cached(
        self, name: str, fetch_fn, ttl_hours: Optional[float] = None
    ) -> Tuple[List[str], List[List[str]]]:
        hit = self.cache.get(name, ttl_hours)
        if hit is not None:
            self.calls["cache_hit"] += 1
            return hit["columns"], hit["rows"]
        columns, rows = fetch_fn()
        self.cache.put(name, columns, rows)
        self.calls["fetched"] += 1
        return columns, rows

    # ---------- 交易日历 ----------
    def trade_dates(self, start: str, end: str) -> List[Tuple[str, bool]]:
        """[start, end] 的 (calendar_date, is_trading_day) 列表。"""
        name = make_cache_name("tradedates", start, end)

        def fetch():
            return self.client.call_with_fields(
                bs.query_trade_dates, label="trade_dates", start_date=start, end_date=end
            )

        _, rows = self._cached(name, fetch, ttl_hours=1.0)
        out: List[Tuple[str, bool]] = []
        for r in rows:
            out.append((r[0], to_int(r[1]) == 1))
        return out

    def latest_trade_date(self, on_or_before: date) -> Optional[date]:
        """≤ on_or_before 的最近交易日（查前 30 天日历即可覆盖节假日）。"""
        start = (on_or_before - timedelta(days=30)).isoformat()
        end = on_or_before.isoformat()
        for d, is_trading in reversed(self.trade_dates(start, end)):
            if is_trading:
                return date.fromisoformat(d)
        return None

    # ---------- 全市场股票列表 ----------
    def all_stock(self, day: str) -> pd.DataFrame:
        """某交易日全部证券。列: code, tradeStatus, code_name。始终显式传 day。

        缓存策略（D-01 纵深防御，不复用 _cached 的原因见下）：
        - 非空结果 = 不可变历史数据 → 永不过期（与其他个股历史接口一致）。
        - **空结果不写入缓存**：空列表可能只是"数据尚未产生"（未来日期、
          数据源异常），若按永久缓存落盘，该日真实运行会静默拿到空股票池。
        - 遗留的空缓存文件（如 D-01 污染产生的）读取时一律视为 miss 重新拉取；
          重拉到非空结果会覆盖它，仍为空则旧文件保持惰性（读侧永远不命中）。
        """
        name = make_cache_name("allstock", day)

        # 非空缓存 → 直接命中；空文件（遗留污染）视为 miss
        hit = self.cache.get(name)
        if hit is not None and len(hit["rows"]) > 0:
            self.calls["cache_hit"] += 1
            columns, rows = hit["columns"], hit["rows"]
        else:
            columns, rows = self.client.call_with_fields(
                bs.query_all_stock, label="all_stock", day=day
            )
            self.calls["fetched"] += 1
            if rows:
                # 非空：不可变历史数据 → 永久缓存
                self.cache.put(name, columns, rows)
            # 空结果：不落盘（见 docstring）

        df = pd.DataFrame(rows, columns=["code", "tradeStatus", "code_name"])
        df["tradeStatus"] = df["tradeStatus"].map(to_int).fillna(0).astype(int)
        return df

    # ---------- 行业分类（全量，含退市股） ----------
    def industry(self) -> pd.DataFrame:
        """列: code, code_name, industry。industry 可能为空串。"""
        name = make_cache_name("industry")

        def fetch():
            return self.client.call_with_fields(
                bs.query_stock_industry, label="industry"
            )

        columns, rows = self._cached(name, fetch, ttl_hours=24.0)
        df = pd.DataFrame(rows, columns=columns)
        keep = [c for c in ("updateDate", "code", "code_name", "industry") if c in df.columns]
        return df[keep].reset_index(drop=True)

    # ---------- 日K ----------
    def kline_window(
        self, code: str, start: str, end: str, adjustflag: str = "1"
    ) -> Tuple[List[str], List[float], List[int]]:
        """后复权(af=1)日K窗口：返回 (dates, closes, tradestatus)。

        停牌日 BaoStock 也返回行（OHLC=昨收、volume=0），tradestatus=0。
        """
        name = make_cache_name("kline", code, start, end, f"af{adjustflag}")

        def fetch():
            return self.client.call_with_fields(
                bs.query_history_k_data_plus,
                label=f"kline_{code}",
                code=code,
                fields="date,code,close,tradestatus",
                start_date=start,
                end_date=end,
                frequency="d",
                adjustflag=adjustflag,  # 必须传字符串（int 会 TypeError）
            )

        _, rows = self._cached(name, fetch)
        dates: List[str] = []
        closes: List[float] = []
        status: List[int] = []
        for r in rows:
            c = to_float(r[2])
            if c is None:  # 防御：close 缺失的行不参与计算
                continue
            dates.append(r[0])
            closes.append(c)
            status.append(to_int(r[3]) or 0)
        return dates, closes, status

    def kline_run_day(self, code: str, day: str) -> KlineData:
        """运行日单根K线（af=3 不复权）：当前价 + isST + tradestatus。"""
        name = make_cache_name("kline_day", code, day)

        def fetch():
            return self.client.call_with_fields(
                bs.query_history_k_data_plus,
                label=f"kline_day_{code}",
                code=code,
                fields="date,code,close,isST,tradestatus",
                start_date=day,
                end_date=day,
                frequency="d",
                adjustflag="3",  # 3=不复权（真实成交价，股息率分母）
            )

        _, rows = self._cached(name, fetch)
        if not rows:
            return KlineData(
                code=code, dates=[], closes=[], tradestatus=[], last_date="",
                n_rows=0, current_price=None, is_st=None, run_day_tradestatus=None,
            )
        r = rows[-1]
        return KlineData(
            code=code,
            dates=[r[0]],
            closes=[to_float(r[2]) or 0.0],
            tradestatus=[to_int(r[4]) or 0],
            last_date=r[0],
            n_rows=1,
            current_price=to_float(r[2]),
            is_st=to_int(r[3]),
            run_day_tradestatus=to_int(r[4]),
        )

    # ---------- 分红（逐年循环！） ----------
    DIVIDEND_FIELDS = [
        "code", "dividPreNoticeDate", "dividAgmPumDate", "dividPlanAnnounceDate",
        "dividPlanDate", "dividRegistDate", "dividOperateDate", "dividPayDate",
        "dividStockMarketDate", "dividCashPsBeforeTax", "dividCashPsAfterTax",
        "dividStocksPs", "dividCashStock", "dividReserveToStockPs",
    ]

    def dividend(self, code: str, year: int) -> List[Dict[str, Any]]:
        """单只股票单个自然年的分红记录（yearType=operate，按除权年份）。"""
        name = make_cache_name("dividend", code, year)

        def fetch():
            return self.client.call_with_fields(
                bs.query_dividend_data,
                label=f"dividend_{code}_{year}",
                code=code,
                year=year,
                yearType="operate",
            )

        _, rows = self._cached(name, fetch, ttl_hours=self._dividend_ttl(year))
        out: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(zip(self.DIVIDEND_FIELDS, r))
            d["dividCashPsBeforeTax"] = to_float(d.get("dividCashPsBeforeTax"))
            out.append(d)
        return out

    # ---------- 季度基本面（单股单季） ----------
    def profit_data(self, code: str, year: int, quarter: int) -> Optional[Dict[str, Any]]:
        """query_profit_data：roeAvg / gpMargin 等。未披露 → None。"""
        name = make_cache_name("profit", code, year, quarter)

        def fetch():
            return self.client.call_with_fields(
                bs.query_profit_data, label=f"profit_{code}_{year}Q{quarter}",
                code=code, year=year, quarter=quarter,
            )

        _, rows = self._cached(name, fetch, ttl_hours=self._fundamental_ttl(year, quarter))
        if not rows:
            return None
        d = dict(zip(["code", "pubDate", "statDate", "roeAvg", "npMargin", "gpMargin",
                      "netProfit", "epsTTM", "MBRevenue", "totalShare", "liqaShare"], rows[-1]))
        for k in ("roeAvg", "npMargin", "gpMargin"):
            d[k] = to_float(d.get(k))
        return d

    def growth_data(self, code: str, year: int, quarter: int) -> Optional[Dict[str, Any]]:
        """query_growth_data：YOYNI / YOYPNI 等。未披露 → None。"""
        name = make_cache_name("growth", code, year, quarter)

        def fetch():
            return self.client.call_with_fields(
                bs.query_growth_data, label=f"growth_{code}_{year}Q{quarter}",
                code=code, year=year, quarter=quarter,
            )

        _, rows = self._cached(name, fetch, ttl_hours=self._fundamental_ttl(year, quarter))
        if not rows:
            return None
        d = dict(zip(["code", "pubDate", "statDate", "YOYEquity", "YOYAsset",
                      "YOYNI", "YOYEPSBasic", "YOYPNI"], rows[-1]))
        for k in ("YOYNI", "YOYPNI"):
            d[k] = to_float(d.get(k))
        return d

    def balance_data(self, code: str, year: int, quarter: int) -> Optional[Dict[str, Any]]:
        """query_balance_data：liabilityToAsset 等。未披露 → None。"""
        name = make_cache_name("balance", code, year, quarter)

        def fetch():
            return self.client.call_with_fields(
                bs.query_balance_data, label=f"balance_{code}_{year}Q{quarter}",
                code=code, year=year, quarter=quarter,
            )

        _, rows = self._cached(name, fetch, ttl_hours=self._fundamental_ttl(year, quarter))
        if not rows:
            return None
        d = dict(zip(["code", "pubDate", "statDate", "currentRatio", "quickRatio",
                      "cashRatio", "YOYLiability", "liabilityToAsset", "assetToEquity"],
                     rows[-1]))
        d["liabilityToAsset"] = to_float(d.get("liabilityToAsset"))
        return d
