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

    # ---------- v2 稳定键增量缓存（报告 R1-c 方案 A） ----------
    # 旧漂移键 kline_{code}_{start}_{end}_af1* / kline_day_* 保留但不再读写：
    # af=1 全历史 = kline_af3 × adjfactor 本地重建，历史不可变、尾部追加。

    # v2 稳定键缓存的 K线字段：只保留全链路实际消费的列（date/close/isST/tradestatus
    # + code）。af1 重建、MA/RSI/MACD/波动率、运行日快照、上市天数全部只需 close；
    # open/high/low/preclose/volume/amount/turn/pctChg/peTTM/pbMRQ 在 v2 无消费者。
    # 字段数直接决定全历史拉取时长（逐行字符串传输，实测 15 字段 28-41s vs 5 字段
    # 5.5-9.3s/股）→ 一次性迁移可行性关键。解析一律按列名（reconstruct/snapshot），
    # 兼容早期误存的 15 字段文件。
    KLINE_AF3_FIELDS = ("date", "code", "close", "isST", "tradestatus")

    def _kline_af3_key(self, code: str) -> str:
        return make_cache_name("kline_af3", code)

    def _adjfactor_key(self, code: str) -> str:
        return make_cache_name("adjfactor", code)

    def kline_af3_history(self, code: str) -> Optional[Dict[str, Any]]:
        """读全历史不复权K线缓存（稳定键）。返回 {"columns","rows"} 或 None（缺失/损坏）。"""
        return self.cache.get(self._kline_af3_key(code))

    def kline_af3_last_date(self, code: str) -> Optional[str]:
        """缓存中最后一根K线的日期（增量起点 = 该日+1）。无缓存 → None。"""
        hit = self.kline_af3_history(code)
        if not hit or not hit["rows"]:
            return None
        last = str(hit["rows"][-1][0]).strip()
        return last or None

    def kline_af3_fetch(self, code: str, start: str, end: str) -> Tuple[List[str], List[List[str]]]:
        """拉取 [start, end] 的不复权K线（af=3），**不写缓存**（由调用方决定追加）。"""
        return self.client.call_with_fields(
            bs.query_history_k_data_plus,
            label=f"kline_af3_{code}",
            code=code,
            fields=",".join(self.KLINE_AF3_FIELDS),
            start_date=start,
            end_date=end,
            frequency="d",
            adjustflag="3",  # 3=不复权（真实成交价，永不变）
        )

    def kline_af3_append(self, code: str, new_rows: List[List[str]]) -> None:
        """把新区间K线行追加到稳定键缓存（原子重写全文件）。"""
        if not new_rows:
            return
        hit = self.kline_af3_history(code)
        cols = list(hit["columns"]) if hit else list(self.KLINE_AF3_FIELDS)
        rows = list(hit["rows"]) if hit else []
        # 防御：丢弃与已有尾部日期重复的行（重跑/断点续传幂等）
        existing_dates = {str(r[0]).strip() for r in rows}
        rows.extend(r for r in new_rows if str(r[0]).strip() not in existing_dates)
        self.cache.put(self._kline_af3_key(code), cols, rows)

    def kline_af3_full(self, code: str, start: str, end: str) -> None:
        """一次性全量拉取（IPO/2000 起 ~ run_day）并写入稳定键缓存。"""
        _, rows = self.kline_af3_fetch(code, start, end)
        if rows:
            self.cache.put(self._kline_af3_key(code), list(self.KLINE_AF3_FIELDS), rows)

    def adjfactor_history(self, code: str) -> Optional[Dict[str, Any]]:
        """读全历史复权因子缓存（稳定键）。返回 {"columns","rows"} 或 None。"""
        return self.cache.get(self._adjfactor_key(code))

    def adjfactor_last_date(self, code: str) -> Optional[str]:
        """已缓存的最后除权日。无缓存/无事件 → None。"""
        hit = self.adjfactor_history(code)
        if not hit or not hit["rows"]:
            return None
        last = str(hit["rows"][-1][1]).strip()
        return last or None

    def adjfactor_fetch(self, code: str, start: str, end: str) -> Tuple[List[str], List[List[str]]]:
        """拉取 [start, end] 的复权因子（query_adjust_factor），不写缓存。"""
        return self.client.call_with_fields(
            bs.query_adjust_factor,
            label=f"adjfactor_{code}",
            code=code, start_date=start, end_date=end,
        )

    def adjfactor_append(self, code: str, new_rows: List[List[str]]) -> None:
        """把新除权事件行追加到稳定键缓存（按 dividOperateDate 去重、升序）。"""
        if not new_rows:
            return
        hit = self.adjfactor_history(code)
        cols = list(hit["columns"]) if hit else ["code", "dividOperateDate", "foreAdjustFactor",
                                                 "backAdjustFactor", "adjustFactor"]
        rows = list(hit["rows"]) if hit else []
        seen = {str(r[1]).strip() for r in rows}
        merged = rows + [r for r in new_rows if str(r[1]).strip() not in seen]
        # 按除权日升序（稳定键约定）
        merged.sort(key=lambda r: str(r[1]))
        self.cache.put(self._adjfactor_key(code), cols, merged)

    def adjfactor_full(self, code: str, start: str, end: str) -> None:
        """一次性全量拉取复权因子（2000 起）并写入稳定键缓存。"""
        _, rows = self.adjfactor_fetch(code, start, end)
        if rows:
            rows.sort(key=lambda r: str(r[1]))
            self.cache.put(self._adjfactor_key(code),
                           ["code", "dividOperateDate", "foreAdjustFactor",
                            "backAdjustFactor", "adjustFactor"], rows)

    def kline_af3_incremental(self, code: str, run_day: str) -> Optional[KlineData]:
        """v2 核心：稳定键增量更新 + 运行日快照（替代旧 kline_run_day）。

        - 缓存缺失 → 全量拉取（IPO/2000 起 ~ run_day）+ 写缓存；
        - 缓存存在且尾日期 == run_day → 0 次查询（纯命中）；
        - 否则尾部追加 [last_date+1, run_day]（通常仅当日 1 根）。

        返回运行日 KlineData（current_price/is_st/tradestatus），无数据 → None。
        该查询同时提供当前价(af3 close)与窗口扩展，每股 K线查询 2 次→1 次。
        """
        last = self.kline_af3_last_date(code)
        if last is None:
            # 首次：全量历史（断点续跑天然支持——写成功后下次走增量）。
            # 1990 起覆盖全部 A 股历史。
            self.kline_af3_full(code, "1990-01-01", run_day)
        elif last < run_day:
            start = (date.fromisoformat(last) + timedelta(days=1)).isoformat()
            _, rows = self.kline_af3_fetch(code, start, run_day)
            self.kline_af3_append(code, rows)

        hit = self.kline_af3_history(code)
        if not hit or not hit["rows"]:
            return None
        # 运行日快照：取最后一根（BaoStock 对停牌日也返回行，OHLC=昨收）
        r = hit["rows"][-1]
        idx = {name: i for i, name in enumerate(hit["columns"])}

        def col(name: str) -> Optional[str]:
            i = idx.get(name)
            return str(r[i]).strip() if (i is not None and i < len(r)) else ""

        close_s = col("close")
        return KlineData(
            code=code,
            dates=[col("date")],
            closes=[to_float(close_s) or 0.0],
            tradestatus=[to_int(col("tradestatus")) or 0],
            last_date=col("date"),
            n_rows=len(hit["rows"]),  # 全历史K线行数（上市时长代理，含停牌行）
            current_price=to_float(close_s),
            is_st=to_int(col("isST")),
            run_day_tradestatus=to_int(col("tradestatus")),
        )

    def kline_af3_rebuilt(self, code: str) -> Optional[Dict[str, List]]:
        """从稳定键缓存重建 (dates, af3_close, af1_close)（本地、离线、秒级）。"""
        kl = self.kline_af3_history(code)
        if not kl or not kl["rows"]:
            return None
        af = self.adjfactor_history(code)
        factor_rows = list(af["rows"]) if af else []
        from ..reconstruct import rebuild_kline_series
        # 按缓存表头定位 close 列（兼容 5 字段 v2 布局与早期 15 字段文件）
        return rebuild_kline_series(kl["rows"], factor_rows, kl["columns"])

    def maybe_refresh_adjfactor(self, code: str, div_records: List[Dict[str, Any]]) -> bool:
        """事件驱动复权因子刷新（R1-c）：仅当分红数据出现 > 已缓存最后除权日的
        新 ex-date 时，拉取 [last_ex_date, run_day] 的因子并 append；否则 0 次查询。

        backAdjustFactor 是 IPO 起累计值 → 新事件行自带完整累计因子，直接去重
        append 即可（历史行零改动）。
        :return: True = 实际发生了因子查询（供统计/日志）。
        """
        if self.run_day is None or not div_records:
            return False
        new_ex = max(
            (str(r.get("dividOperateDate") or "").strip() for r in div_records),
            default="",
        )
        last = self.adjfactor_last_date(code)
        if not new_ex or (last is not None and new_ex <= last):
            return False  # 无新除权事件 → 0 次额外查询（绝大多数股票的稳态）
        # last=None（无缓存）时从 1990 起——backAdjustFactor 是 IPO 起累计值，
        # 漏掉早期除权台阶会让该段 af1 重建退化为不复权（2000 起点曾漏 4 只）。
        start = last if last else "1990-01-01"
        _, rows = self.adjfactor_fetch(code, start, self.run_day.isoformat())
        self.adjfactor_append(code, rows)
        log.info("复权因子事件驱动刷新: %s 新除权日 %s（start=%s）", code, new_ex, start)
        return True

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

    def cashflow_data(self, code: str, year: int, quarter: int) -> Optional[Dict[str, Any]]:
        """query_cash_flow_data：CFOToNP 等（v2 Piotroski S2/S4 用）。未披露 → None。"""
        name = make_cache_name("cashflow", code, year, quarter)

        def fetch():
            return self.client.call_with_fields(
                bs.query_cash_flow_data, label=f"cashflow_{code}_{year}Q{quarter}",
                code=code, year=year, quarter=quarter,
            )

        _, rows = self._cached(name, fetch, ttl_hours=self._fundamental_ttl(year, quarter))
        if not rows:
            return None
        d: Dict[str, Any] = dict(zip(["code", "pubDate", "statDate", "CAToAsset", "NCAToAsset",
                                      "tangibleAssetToAsset", "ebitToInterest", "CFOToOR",
                                      "CFOToNP", "CFOToGr"], rows[-1]))
        for k in ("CAToAsset", "NCAToAsset", "tangibleAssetToAsset",
                  "ebitToInterest", "CFOToOR", "CFOToNP", "CFOToGr"):
            d[k] = to_float(str(d.get(k) or ""))
        return d
