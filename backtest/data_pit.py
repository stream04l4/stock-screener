# -*- coding: utf-8 -*-
"""PIT 快照访问层（回测引擎数据层核心，调研报告 R1 修复收敛点）。

**全部离线读本地缓存，零 BaoStock 请求。** 所有读取以 ``date <= T`` /
``pubDate <= T`` / ``ex_date <= T`` 截断——T 日之后的任何数据都读不到。

R1 泄漏点 → 本层对应修复（每个都有 tests/test_data_pit.py 的回归单测）：
- K线快照价：取 ``date <= T`` 的最后一根（v2 的 rows[-1] 与 run_day 无关 = 最大泄漏点）。
- af1 技术因子窗口：重建后先按 ``dates <= T`` 截断再取最近 max_bars 根。
- af1 复权因子：``factor_at(T)`` 只依赖 ex_date <= T 的除权事件（新除权不改历史）。
- 上市天数：``count(date <= T)``（v2 用全缓存行数 → 未上市新股被误判为老股）。
- ST 状态：取 T 日那根的 isST（历史列），非"今天"的 isST。
- 基本面报告期：每行校验 ``pubDate <= T``，不满足 → 记缺失（不用未来财报）。
- 分红 TTM：窗口 [T-window, T]，按 ex_date <= T 过滤（v2 已安全，本层保持口径）。
- 股票池：K线跨度近似 ``first_date <= T <= last_date``（当前缓存无退市股 →
  幸存者偏差，报告显式标注；严格 PIT 池 = allstock(day=T)，Phase B/C 补）。

防御 23 只混合格式 K线文件（15 字段头 + 5 字段追加尾）：读取时按
"列名定位 + 字段数校验"，坏行跳过并告警（迁移重拉留 TODO，不阻塞回测）。
"""
from __future__ import annotations

import array
import bisect
import csv
import logging
import math
import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from screener.data.cache import DiskCache, make_cache_name
from screener.reconstruct import factor_at, reconstruct_af1

log = logging.getLogger("backtest.data_pit")

# 复权因子缓存的列布局（BaoStock 原序；与 fetchers.adjfactor_full 一致）
_ADJFACTOR_COLUMNS = ["code", "dividOperateDate", "foreAdjustFactor",
                      "backAdjustFactor", "adjustFactor"]

# 基本面各表的数值字段（解析为 float；缺失/空串 → None）
_FUNDAMENTAL_FLOAT_FIELDS: Dict[str, List[str]] = {
    "profit": ["roeAvg", "npMargin", "gpMargin", "netProfit", "epsTTM",
               "MBRevenue", "totalShare", "liqaShare"],
    "growth": ["YOYEquity", "YOYAsset", "YOYNI", "YOYEPSBasic", "YOYPNI"],
    "balance": ["currentRatio", "quickRatio", "cashRatio", "YOYLiability",
                "liabilityToAsset", "assetToEquity"],
    "cashflow": ["CAToAsset", "NCAToAsset", "tangibleAssetToAsset",
                 "ebitToInterest", "CFOToOR", "CFOToNP", "CFOToGr"],
}

# 基本面访问模式（与 v2 zscore 引擎一致，报告 R4 预算表口径）：
# annual_year=Y 时读 profit(Y,Y-1,Y-2 Q4)+growth(Y Q4)+balance(Y,Y-1 Q4)+cashflow(Y Q4)
_FUNDAMENTAL_KEYS: List[Tuple[str, str, int]] = [
    ("profit", "profit_cur", 0),
    ("profit", "profit_prior", -1),
    ("profit", "profit_oldest", -2),
    ("growth", "growth_cur", 0),
    ("balance", "balance_cur", 0),
    ("balance", "balance_prior", -1),
    ("cashflow", "cashflow_cur", 0),
]


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


@dataclass
class KlineSnapshot:
    """T 日（含）之前的最后一根K线快照 + 上市天数。"""

    code: str
    date: str                     # 快照K线日期（<= T 的最后一个交易日）
    close_af3: Optional[float]    # 不复权收盘价（股息率分母；空 close → None）
    is_st: Optional[int]          # T 日那根的 isST（1/0；缺失 → None）
    tradestatus: Optional[int]    # T 日那根的 tradestatus（1正常/0停牌）
    n_bars: int                   # count(date <= T)：上市天数（PIT 修正点）


@dataclass
class _KlineParsed:
    """单只股票全历史K线的紧凑解析结果（内存驻留，供多期复用）。"""

    code: str
    dates: List[str]              # 升序 ISO 日期
    af3: "array.array"            # 不复权收盘价（NaN=缺失）
    af1: "array.array"            # 后复权收盘价（NaN=缺失）
    is_st: bytes                  # 0/1，255=缺失
    tradestatus: bytes            # 0/1，255=缺失
    has_open: bool = False
    open_: Optional["array.array"] = None   # 开盘价（仅少数文件有 open 列）


class PitData:
    """PIT 快照访问层。构造即离线；所有方法只读缓存目录。

    :param cache_dir: 缓存目录（strategy.yaml data.cache_dir）。
    :param ref_code: 交易日历参考股（上市早、无长期停牌缺行；BaoStock 对
        停牌日也返回行，故其日期序列 = 全市场交易日历）。
    """

    def __init__(self, cache_dir: str, ref_code: str = "sh.601398") -> None:
        self.cache = DiskCache(cache_dir)
        self.ref_code = ref_code
        self._kline_cache: Dict[str, Optional[_KlineParsed]] = {}
        self._span_cache: Dict[str, Optional[Tuple[str, str]]] = {}
        self._all_codes: Optional[List[str]] = None
        self._cal_dates: Optional[List[str]] = None
        self._cal_range: Optional[Tuple[str, str]] = None
        self._div_year_cache: Dict[Tuple[str, int], List[Dict[str, Any]]] = {}
        self._industry_map: Optional[Dict[str, str]] = None
        self._name_map: Optional[Dict[str, str]] = None
        self._bad_row_warnings: set = set()

    # ------------------------------------------------------------------
    # 基础：缓存文件枚举 / 行业与名称快照
    # ------------------------------------------------------------------
    def all_kline_codes(self) -> List[str]:
        """缓存中全部有 K线文件的代码（稳定键 kline_af3_{code}.csv）。"""
        if self._all_codes is None:
            prefix = "kline_af3_"
            codes = []
            for fn in os.listdir(self.cache.cache_dir):
                if fn.startswith(prefix) and fn.endswith(".csv"):
                    codes.append(fn[len(prefix):-len(".csv")])
            codes.sort()
            self._all_codes = codes
        return list(self._all_codes)

    def industry_map(self) -> Dict[str, str]:
        """证监会行业分类（**当前快照**，无历史版本 → 前视偏差，报告标注）。"""
        if self._industry_map is None:
            m: Dict[str, str] = {}
            hit = self.cache.get(make_cache_name("industry"))
            if hit:
                idx = {name: i for i, name in enumerate(hit["columns"])}
                ic = idx.get("code")
                ii = idx.get("industry")
                for r in hit["rows"]:
                    if ic is None or ii is None or len(r) <= max(ic, ii):
                        continue
                    m[str(r[ic]).strip()] = str(r[ii]).strip()
            self._industry_map = m
        return dict(self._industry_map)

    def name_map(self) -> Dict[str, str]:
        """代码 → 名称（行业快照的 code_name 列；缺失 → 空串）。"""
        if self._name_map is None:
            m: Dict[str, str] = {}
            hit = self.cache.get(make_cache_name("industry"))
            if hit:
                idx = {name: i for i, name in enumerate(hit["columns"])}
                ic = idx.get("code")
                in_ = idx.get("code_name")
                for r in hit["rows"]:
                    if ic is None or in_ is None or len(r) <= max(ic, in_):
                        continue
                    m[str(r[ic]).strip()] = str(r[in_]).strip()
            self._name_map = m
        return dict(self._name_map)

    def industry_update_date(self) -> str:
        hit = self.cache.get(make_cache_name("industry"))
        if not hit:
            return ""
        idx = {name: i for i, name in enumerate(hit["columns"])}
        iu = idx.get("updateDate")
        if iu is None or not hit["rows"] or len(hit["rows"][0]) <= iu:
            return ""
        return str(hit["rows"][0][iu]).strip()

    # ------------------------------------------------------------------
    # K线解析（列名定位 + 字段数校验；坏行跳过并告警）
    # ------------------------------------------------------------------
    def _kline_span(self, code: str) -> Optional[Tuple[str, str]]:
        """(首根日期, 末根日期)。只读文件首尾两行（universe 构造用，快）。"""
        if code in self._span_cache:
            return self._span_cache[code]
        hit = self.cache.get(make_cache_name("kline_af3", code))
        span: Optional[Tuple[str, str]] = None
        if hit and hit["rows"]:
            first = str(hit["rows"][0][0]).strip()
            last = str(hit["rows"][-1][0]).strip()
            if first and last:
                span = (first, last)
        self._span_cache[code] = span
        return span

    def _parse_kline(self, code: str) -> Optional[_KlineParsed]:
        """全历史解析 + af1 重建。列名定位 close/isST/tradestatus/open；
        字段数与表头不一致的行视为坏行跳过（23 只混合格式文件的防御）。"""
        hit = self.cache.get(make_cache_name("kline_af3", code))
        if not hit or not hit["rows"]:
            return None
        columns = list(hit["columns"])
        n = len(columns)
        idx = {name: i for i, name in enumerate(columns)}
        if "date" not in idx or "close" not in idx:
            log.warning("K线缓存 %s 表头缺 date/close，跳过", code)
            return None

        dates: List[str] = []
        af3: List[Optional[float]] = []
        is_st: List[int] = []
        tr: List[int] = []
        opens: List[Optional[float]] = []
        has_open = "open" in idx
        bad = 0
        for r in hit["rows"]:
            if not r or len(r) != n:
                # 字段数校验：混合格式文件（15 字段头 + 5 字段追加尾）的坏行
                bad += 1
                continue
            d = str(r[idx["date"]]).strip()
            if not d:
                bad += 1
                continue
            dates.append(d)
            af3.append(_to_float(r[idx["close"]]))
            if "isST" in idx:
                v = _to_float(r[idx["isST"]])
                is_st.append(int(v) if v is not None else 255)
            else:
                is_st.append(255)
            if "tradestatus" in idx:
                v = _to_float(r[idx["tradestatus"]])
                tr.append(int(v) if v is not None else 255)
            else:
                tr.append(255)
            opens.append(_to_float(r[idx["open"]]) if has_open else None)
        if bad:
            # 只告警一次（同一文件多期回测会反复解析）
            if code not in self._bad_row_warnings:
                log.warning("K线缓存 %s: %d 行字段数与表头(%d)不符，已跳过"
                            "（混合格式文件，迁移时重拉 — TODO Phase B）",
                            code, bad, n)
                self._bad_row_warnings.add(code)
        if not dates:
            return None

        # 复权因子（全历史；factor_at 内部按 ex_date <= t 截断 → PIT 安全）
        factors: List[Tuple[date, float]] = []
        af_hit = self.cache.get(make_cache_name("adjfactor", code))
        if af_hit and af_hit["rows"]:
            fidx = {name: i for i, name in enumerate(af_hit["columns"])}
            idate = fidx.get("dividOperateDate", 1)
            iback = fidx.get("backAdjustFactor", 3)
            for r in af_hit["rows"]:
                if len(r) <= max(idate, iback):
                    continue
                try:
                    d = date.fromisoformat(str(r[idate]).strip())
                    v = float(str(r[iback]).strip())
                except (ValueError, IndexError):
                    continue
                factors.append((d, v))
            factors.sort(key=lambda x: x[0])

        af1 = reconstruct_af1(dates, af3, factors)
        parsed = _KlineParsed(
            code=code, dates=dates,
            af3=array.array("d", (math.nan if c is None else c for c in af3)),
            af1=array.array("d", (math.nan if c is None else c for c in af1)),
            is_st=bytes(is_st), tradestatus=bytes(tr),
            has_open=has_open,
        )
        if has_open:
            # 缓存 OHLC 均为**不复权**(af3) → open 必须用同一 F(t) 重建为 af1，
            # 否则 T+1 开盘成交会把"未复权 open × 已复权权重"混算（实测 sh.600007
            # 单日虚增 +77%）。reconstruct_af1 对 None 安全。
            opens_af1 = reconstruct_af1(dates, opens, factors)
            parsed.open_ = array.array(
                "d", (math.nan if c is None else c for c in opens_af1))
        return parsed

    def kline(self, code: str) -> Optional[_KlineParsed]:
        """全历史解析结果（内存缓存，多期复用）。无缓存 → None。"""
        if code not in self._kline_cache:
            self._kline_cache[code] = self._parse_kline(code)
        return self._kline_cache[code]

    # ------------------------------------------------------------------
    # PIT 接口（R5 规格）
    # ------------------------------------------------------------------
    def kline_snapshot(self, code: str, T: date) -> Optional[KlineSnapshot]:
        """T 日（含）之前的最后一根K线快照 + n_bars=count(date<=T)。

        R1 修复：v2 取 rows[-1]（=今天）→ 这里取 date<=T 的最后一根。
        T 早于首根K线（未上市）→ None。
        """
        kl = self.kline(code)
        if not kl:
            return None
        t_iso = T.isoformat()
        i = bisect.bisect_right(kl.dates, t_iso) - 1
        if i < 0:
            return None
        c3 = kl.af3[i]
        return KlineSnapshot(
            code=code, date=kl.dates[i],
            close_af3=None if math.isnan(c3) else float(c3),
            is_st=None if kl.is_st[i] == 255 else int(kl.is_st[i]),
            tradestatus=None if kl.tradestatus[i] == 255 else int(kl.tradestatus[i]),
            n_bars=i + 1,
        )

    def af1_window(self, code: str, T: date, max_bars: int) -> Tuple[List[str], List[float]]:
        """截至 T（含）的最近 max_bars 根后复权收盘价（close 非空才保留）。

        R1 修复：v2 重建全历史后取尾部（含 T 之后）→ 这里先按 dates<=T 截断。
        """
        kl = self.kline(code)
        if not kl:
            return [], []
        t_iso = T.isoformat()
        i = bisect.bisect_right(kl.dates, t_iso) - 1
        if i < 0:
            return [], []
        lo = max(0, i - max_bars + 1)
        dates: List[str] = []
        closes: List[float] = []
        for j in range(lo, i + 1):
            v = kl.af1[j]
            if not math.isnan(v):
                dates.append(kl.dates[j])
                closes.append(float(v))
        return dates, closes

    def factor_at(self, code: str, T: date) -> float:
        """截至 T 的累计复权因子 F(T)（只依赖 ex_date<=T 的事件）。"""
        af_hit = self.cache.get(make_cache_name("adjfactor", code))
        if not af_hit or not af_hit["rows"]:
            return 1.0
        fidx = {name: i for i, name in enumerate(af_hit["columns"])}
        idate = fidx.get("dividOperateDate", 1)
        iback = fidx.get("backAdjustFactor", 3)
        factors: List[Tuple[date, float]] = []
        for r in af_hit["rows"]:
            if len(r) <= max(idate, iback):
                continue
            try:
                d = date.fromisoformat(str(r[idate]).strip())
                v = float(str(r[iback]).strip())
            except (ValueError, IndexError):
                continue
            factors.append((d, v))
        factors.sort(key=lambda x: x[0])
        return factor_at(T, factors)

    # ------------------------------------------------------------------
    # 基本面（pubDate <= T 校验；不满足 → None，绝不用未来财报）
    # ------------------------------------------------------------------
    def _fundamental_row(self, kind: str, code: str, year: int, quarter: int,
                         T: date) -> Optional[Dict[str, Any]]:
        """读单股单季基本面缓存并做 PIT 校验。

        - 文件不存在 / 无数据行 → None（未披露或历史缺口）。
        - **pubDate > T → None**（R1 条件泄漏修复：命中即停但不校验 pubDate
          会让引擎读到未来财报；这里一律视为"截至 T 尚未披露"）。
        """
        hit = self.cache.get(make_cache_name(kind, code, year, quarter))
        if not hit or not hit["rows"]:
            return None
        idx = {name: i for i, name in enumerate(hit["columns"])}
        row = hit["rows"][-1]  # BaoStock 单季查询只返回一行（与 v2 一致）
        ipub = idx.get("pubDate")
        if ipub is not None and ipub < len(row):
            pub_s = str(row[ipub]).strip()
            if pub_s:
                try:
                    if date.fromisoformat(pub_s) > T:
                        return None  # PIT 红线：未来披露的财报读不到
                except ValueError:
                    pass
        out: Dict[str, Any] = {}
        for i, name in enumerate(hit["columns"]):
            if i >= len(row):
                break
            out[name] = str(row[i]).strip()
        for f in _FUNDAMENTAL_FLOAT_FIELDS.get(kind, []):
            if f in out:
                out[f] = _to_float(out[f])
        return out

    def fundamentals(self, code: str, T: date, annual_year: int) -> Dict[str, Optional[Dict[str, Any]]]:
        """v2 zscore 引擎的 7 个基本面键（报告 R4 访问模式），全部 pubDate<=T。"""
        out: Dict[str, Optional[Dict[str, Any]]] = {}
        for kind, key, dy in _FUNDAMENTAL_KEYS:
            out[key] = self._fundamental_row(kind, code, annual_year + dy, 4, T)
        return out

    @staticmethod
    def resolve_annual_year(base_row_fn, run_day: date) -> int:
        """v2 基准年度逻辑（PIT 版）：5 月及以后 → y-1，否则 y-2；
        基准股该年 Q4 无**已披露**(pubDate<=T)数据 → 回退一年。

        :param base_row_fn: (year, quarter) -> row|None（调用方用本类
            _fundamental_row 对基准股构造，保证 pubDate 校验）。
        """
        y = run_day.year - 1 if run_day.month > 4 else run_day.year - 2
        row = base_row_fn(y, 4)
        if row is None or not (row.get("roeAvg") is not None or row.get("pubDate")):
            return y - 1
        return y

    # ------------------------------------------------------------------
    # 分红（ex_date <= T；窗口由调用方给）
    # ------------------------------------------------------------------
    def _dividend_year(self, code: str, year: int) -> List[Dict[str, Any]]:
        """单股单自然年分红记录（按 (code,year) 缓存；空结果也缓存——历史年底后不可变）。"""
        key = (code, year)
        if key in self._div_year_cache:
            return self._div_year_cache[key]
        recs: List[Dict[str, Any]] = []
        hit = self.cache.get(make_cache_name("dividend", code, year))
        if hit and hit["rows"]:
            idx = {name: i for i, name in enumerate(hit["columns"])}

            def g(r: Sequence[str], name: str) -> str:
                i = idx.get(name)
                return str(r[i]).strip() if (i is not None and i < len(r)) else ""

            for r in hit["rows"]:
                recs.append({
                    "code": g(r, "code") or code,
                    "dividOperateDate": g(r, "dividOperateDate"),
                    "dividCashPsBeforeTax": _to_float(g(r, "dividCashPsBeforeTax")),
                })
        self._div_year_cache[key] = recs
        return recs

    def dividend_records(self, code: str, T: date) -> List[Dict[str, Any]]:
        """截至 T 已除权(ex_date<=T)的全部分红记录（读缓存年份 T-2..T）。

        同一除权日可能有"预案+正式"两行 → 调用方用 metrics.dedup_dividends
        去重求和（与 v2 同口径）。按 (code,year) 分年缓存，避免跨期 T 的脏读。
        """
        recs: List[Dict[str, Any]] = []
        for y in (T.year - 2, T.year - 1, T.year):
            recs.extend(self._dividend_year(code, y))
        t_iso = T.isoformat()
        return [r for r in recs if r["dividOperateDate"] and r["dividOperateDate"] <= t_iso]

    # ------------------------------------------------------------------
    # 股票池（PIT）
    # ------------------------------------------------------------------
    def universe(self, T: date, prefixes: Sequence[str]) -> List[str]:
        """T 日的 PIT 股票池：K线跨度近似 first_date <= T <= last_date + 前缀过滤。

        当前缓存只含现存股（无退市股）→ 幸存者偏差，报告标注；严格 PIT 池
        （query_all_stock(day=T)，含当时已上市后退市者）由 Phase B/C 补拉后启用。
        """
        t_iso = T.isoformat()
        out: List[str] = []
        for code in self.all_kline_codes():
            if not code.startswith(tuple(prefixes)):
                continue
            span = self._kline_span(code)
            if span and span[0] <= t_iso <= span[1]:
                out.append(code)
        return out

    # ------------------------------------------------------------------
    # 交易日历（参考股的日期序列；BaoStock 对停牌日也返回行 → 全市场日历）
    # ------------------------------------------------------------------
    def trade_calendar(self, start: date, end: date) -> List[str]:
        """[start, end] 内的全部交易日（ISO 升序）。"""
        if self._cal_dates is not None and self._cal_range is not None:
            s0, e0 = self._cal_range
            if s0 <= start.isoformat() and e0 >= end.isoformat():
                lo = bisect.bisect_left(self._cal_dates, start.isoformat())
                hi = bisect.bisect_right(self._cal_dates, end.isoformat())
                return self._cal_dates[lo:hi]
        ref = self.kline(self.ref_code)
        if not ref or not ref.dates:
            # 兜底：参考股缺失 → 用首只可用K线（仍为全市场日历，停牌有行）
            for code in self.all_kline_codes():
                ref = self.kline(code)
                if ref and len(ref.dates) > 100:
                    log.warning("交易日历参考股 %s 缺失，改用 %s", self.ref_code, code)
                    break
        if not ref or not ref.dates:
            raise RuntimeError("无法构建交易日历：缓存中无任何K线数据")
        self._cal_dates = list(ref.dates)
        self._cal_range = (self._cal_dates[0], self._cal_dates[-1])
        s, e = start.isoformat(), end.isoformat()
        lo = bisect.bisect_left(self._cal_dates, s)
        hi = bisect.bisect_right(self._cal_dates, e)
        return self._cal_dates[lo:hi]

    def next_trade_date(self, T: date, after_days: int = 1) -> Optional[str]:
        """T 之后第 after_days 个交易日（执行日定位；严格晚于 T）。"""
        cal = self.trade_calendar(T + timedelta(days=1), T + timedelta(days=60))
        if len(cal) >= after_days:
            return cal[after_days - 1]
        return None

    # ------------------------------------------------------------------
    # 执行价（T+1 成交用；open 缺失 → close 兜底，报告披露）
    # ------------------------------------------------------------------
    def exec_price(self, code: str, day: str, use_open: bool) -> Tuple[Optional[float], bool]:
        """指定交易日的成交价：(价格(af1), 是否用了 open)。

        - 停牌日（tradestatus=0）→ (None, False)：顺延逻辑由模拟器处理。
        - 退市（无该日K线）→ (None, False)。
        - use_open 但该股无 open 列 / open 缺失 → 用 close 兜底（返回 used_open=False）。
        """
        kl = self.kline(code)
        if not kl:
            return None, False
        i = bisect.bisect_left(kl.dates, day)
        if i >= len(kl.dates) or kl.dates[i] != day:
            return None, False
        if kl.tradestatus[i] == 0:
            return None, False
        if use_open and kl.has_open and kl.open_ is not None:
            v = kl.open_[i]
            if not math.isnan(v):
                return float(v), True
        v = kl.af1[i]
        if math.isnan(v):
            return None, False
        return float(v), False

    def last_bar_date(self, code: str) -> Optional[str]:
        """K线末根日期（退市判定：末根 < 执行日 → 已退市/无法买入）。"""
        kl = self.kline(code)
        if not kl or not kl.dates:
            return None
        return kl.dates[-1]

    def last_close_on_or_before(self, code: str, day: str) -> Optional[float]:
        """≤day 的最后一根**非空** af1 收盘价（停牌冻结估值/退市退出价用）。

        PIT 红线：只回看 ≤day，绝不取未来 bar。全部 close 缺失 → None。
        """
        kl = self.kline(code)
        if not kl:
            return None
        i = bisect.bisect_right(kl.dates, day) - 1
        while i >= 0:
            v = kl.af1[i]
            if not math.isnan(v):
                return float(v)
            i -= 1
        return None
