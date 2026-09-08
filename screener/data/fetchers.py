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
import os
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

import baostock as bs
import pandas as pd

from .baostock_client import BaoStockClient, DataSourceError
from .cache import DiskCache, make_cache_name
from .sources import (
    ExdateDetector,
    StockBar,
    TencentKlineSource,
    TencentSnapshotSource,
)

log = logging.getLogger("screener.data.fetch")

# ===========================================================================
# v4 数据源默认配置加载（零硬编码：全部来自 config/strategy.yaml）
# ===========================================================================
_STRATEGY_CFG_CACHE: Optional[Dict[str, Any]] = None


def _strategy_cfg() -> Dict[str, Any]:
    """读取项目根 config/strategy.yaml（模块级缓存一次）。

    失败/缺失 → {}（调用方回退默认值）。惰性加载：只有真正走 v4 接缝函数
    （kline_af3_incremental / maybe_refresh_adjfactor）时才触发；migrate/prewarm
    只调 all_stock/kline_af3_full/fundamentals，永不触碰。
    """
    global _STRATEGY_CFG_CACHE
    if _STRATEGY_CFG_CACHE is not None:
        return _STRATEGY_CFG_CACHE
    cfg: Dict[str, Any] = {}
    try:
        from .. import config as _cfgmod  # 惰性导入避免循环
        # __file__ = <root>/screener/data/fetchers.py → 上溯 3 层到项目根
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        path = os.path.join(root, "config", "strategy.yaml")
        if os.path.exists(path):
            cfg = _cfgmod.load_config(path)
    except Exception as exc:  # noqa: BLE001 — 配置层故障不应击穿数据层；回退 baostock 默认
        log.warning("strategy.yaml 加载失败，数据源回退默认(baostock): %s", exc)
        cfg = {}
    _STRATEGY_CFG_CACHE = cfg if isinstance(cfg, dict) else {}
    return _STRATEGY_CFG_CACHE


def _load_default_datasource_cfg() -> Dict[str, Any]:
    """未显式传 datasource_cfg 时的默认解析：读 strategy.yaml datasource 段。

    - yaml 存在且含 datasource 段 → 严格校验后返回（生产 cron 由此拿到 primary=tencent）；
    - yaml 缺失/无该段 → baostock 默认（v4 上线前行为，向后兼容）。
    """
    cfg = _strategy_cfg()
    if not cfg:
        return {"primary": "baostock", "fallback": "fail_fast"}
    from .. import config as _cfgmod  # noqa: PLC0415
    try:
        return _cfgmod.datasource_cfg(cfg)
    except Exception as exc:  # noqa: BLE001 — 非法 datasource 段 → 回退默认并告警
        log.warning("datasource 段校验失败，回退默认(baostock): %s", exc)
        return {"primary": "baostock", "fallback": "fail_fast"}


def _a_share_prefixes() -> List[str]:
    """universe.a_share_prefixes（单一事实来源=strategy.yaml）；缺失 → 空（不过滤）。"""
    cfg = _strategy_cfg()
    uni = cfg.get("universe") or {}
    prefixes = [str(p) for p in (uni.get("a_share_prefixes") or [])]
    return prefixes



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

    def __init__(self, client: BaoStockClient, cache: DiskCache,
                 datasource_cfg: Optional[Dict[str, Any]] = None) -> None:
        self.client = client
        self.cache = cache
        self.run_day: Optional[date] = None  # 由引擎在定位交易日后设置
        self.calls = {"cache_hit": 0, "fetched": 0}
        # v4 数据源抽象层（报告 R3 + TL 修正）：显式传入 datasource_cfg 则用之；
        # 未传（None，生产 cron / migrate / prewarm 的构造方式）→ **惰性**从
        # config/strategy.yaml 的 datasource 段加载（零硬编码纪律：阈值全来自 yaml）。
        # 惰性而非 __init__ 立即加载：migrate/prewarm 只调 all_stock/kline_af3_full/
        # fundamentals（不碰腾讯接缝函数）→ 永不触发加载，避免无谓 I/O 与失败面。
        self._explicit_ds_cfg = datasource_cfg
        self._resolved_ds_cfg: Optional[Dict[str, Any]] = None
        self._snapshot: Optional[Dict[str, StockBar]] = None   # 全市场快照（惰性拉一次）
        self._candidates: Dict[str, Any] = {}                  # 除权候选（detector 输出）
        self._cutover_events: Dict[str, List[Tuple[str, float]]] = {}  # 切换日 qfq/raw 多事件回补
        self._prev_closes: Dict[str, float] = {}               # 本地缓存 t-1 close（检测器输入）
        self._snapshot_source: Optional[TencentSnapshotSource] = None
        self._kline_source: Optional[TencentKlineSource] = None  # 切换日候选确认（可注入 fake）
        self._detector: Optional[ExdateDetector] = None
        self.contract_warnings: List[str] = []                 # 契约监控告警（供日志/报告）

    @property
    def datasource_cfg(self) -> Dict[str, Any]:
        """解析后的 datasource 配置（惰性：首次访问时从 strategy.yaml 加载并缓存）。"""
        if self._resolved_ds_cfg is None:
            self._resolved_ds_cfg = (
                self._explicit_ds_cfg or _load_default_datasource_cfg()
            )
        return self._resolved_ds_cfg

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

    # ---------- v4 数据源抽象层（报告 R3 + TL 修正）：腾讯批量快照接缝 ----------
    def _is_tencent_primary(self) -> bool:
        return str(self.datasource_cfg.get("primary", "baostock")).lower() == "tencent"

    def _fallback_is_fail_fast(self) -> bool:
        return str(self.datasource_cfg.get("fallback", "fail_fast")).lower() == "fail_fast"

    def _kline_af3_tail_rows(self, code: str, n: int = 2) -> List[List[str]]:
        """高效读取 kline_af3 缓存末尾 n 行数据（尾部字节读，不加载全历史）。

        数据行为纯 ASCII（日期/代码/数值），字节级尾读安全；小文件（仅表头+几行）
        退化为整读。哨兵行与表头行统一跳过。生产全市场检测时避免 5215×全历史 I/O。
        """
        path = self.cache._path(self._kline_af3_key(code))
        try:
            size = os.path.getsize(path)
        except OSError:
            return []
        chunk = max(n * 256, 4096)
        try:
            with open(path, "rb") as fh:
                if size > chunk:
                    fh.seek(size - chunk)
                raw = fh.read()
        except OSError:
            return []
        lines = raw.decode("utf-8", errors="replace").splitlines()
        # 部分读取时首行可能不完整 → 丢弃
        if size > chunk and lines:
            lines = lines[1:]
        data: List[List[str]] = []
        for ln in lines:
            s = ln.strip()
            if not s or s.startswith("stock-screener-cache") or s.startswith("date,"):
                continue  # 哨兵 / 表头
            data.append(s.split(","))
        return data[-n:]

    def _build_prev_closes(self, codes: List[str], run_day: str) -> Dict[str, float]:
        """构建检测器输入：本地 kline_af3 缓存 t-1 日 close {code: float}。

        - 缓存尾 == run_day（今天 BaoStock cron 已跑过）→ t-1 = 倒数第 2 行 close；
        - 缓存尾 < run_day（缺口/切换日）→ t-1 = 末行 close（可能跨多日，检测器对此免疫：
          误报无害，且切换日由 cutover_max_candidates 截断）。
        """
        out: Dict[str, float] = {}
        for code in codes:
            tail = self._kline_af3_tail_rows(code, 2)
            if not tail:
                continue
            last_date = str(tail[-1][0]).strip()
            if last_date == run_day and len(tail) >= 2:
                row = tail[-2]
            else:
                row = tail[-1]
            c = to_float(row[2]) if len(row) > 2 else None
            if c is not None and c > 0:
                out[code] = c
        return out

    def _ensure_snapshot(self, run_day: str) -> None:
        """惰性触发一次全市场腾讯快照 + 除权检测（引擎循环首只股票时调用，之后复用）。

        - codes = all_stock(run_day) ∩ A股前缀（universe.a_share_prefixes，单一事实来源
          strategy.yaml）∩ tradeStatus==1 —— 与 build_universe 口径一致（~5215 只），
          不拉指数/ETF/B股（避免浪费批次 + 污染契约(a)行数==请求数）。
          all_stock 走缓存命中 → **0 次 live BaoStock**（primary=tencent 下仍如此）。
        - 契约监控 (a)(b) 在 TencentSnapshotSource.snapshot / pct_consistency_sample 内计算。
        - **失败语义**：全批失败（0 只解析）且 fallback=fail_fast → raise DataSourceError
          （绝不伪装空结果）；部分缺失由 kline_af3_incremental 逐股回退 BaoStock。
        """
        if self._snapshot is not None:
            return
        tcfg = self.datasource_cfg.get("tencent", {}) or {}
        detector_cfg = self.datasource_cfg.get("exdate_detector", {}) or {}
        source = self._snapshot_source  # 测试可注入 fake；生产用真实腾讯源
        if source is None:
            source = TencentSnapshotSource(tcfg)
            self._snapshot_source = source
        self._detector = ExdateDetector(detector_cfg)

        all_df = self.all_stock(run_day)    # allstock 缓存命中 → 0 次 live BaoStock
        prefixes = _a_share_prefixes()
        codes = [
            str(rec["code"]) for rec in all_df.to_dict("records")
            if (not prefixes or str(rec["code"]).startswith(tuple(prefixes)))
            and int(rec["tradeStatus"]) == 1
        ]
        bars = source.snapshot(codes) if codes else {}

        # 失败语义：全批失败（0 只解析）→ fail_fast 显式失败
        if not bars and self._fallback_is_fail_fast():
            raise DataSourceError(
                f"数据源级失败: 腾讯批量快照全批失败（请求 {source.requested_count} 只、"
                f"解析 0 只，失败批 {source.failed_batches}/{source.total_batches}）——"
                "fallback=fail_fast，拒绝产出误导性空结果"
            )
        self._snapshot = bars

        # 除权检测（TL preclose 信号）：输入=快照 preclose + 本地缓存 t-1 close
        prev_closes = self._build_prev_closes(codes, run_day)
        self._prev_closes = prev_closes
        cutover = any(
            str(tail[-1][0]).strip() != run_day
            for tail in (self._kline_af3_tail_rows(c, 1) for c in codes[:200]) if tail
        )  # 抽样判断是否切换日（缓存尾非 run_day → 有缺口）
        self._candidates, det_warnings = self._detector.detect(bars, prev_closes, cutover=cutover)

        # 切换日 bootstrap（brief §6）：缓存尾是 BaoStock 旧数据（跨多日缺口），
        # preclose/缓存尾比值跨多日放大误报 → 对命中候选取腾讯 raw+qfq K线 N 根，
        # (a) 回补缺口期缺失 K线行，(b) 用 qfq/raw 比值检测缺口期除权事件（逐事件补因子）。
        if cutover and self._candidates:
            self._populate_cutover_events(run_day)

        # 契约监控 (b)：抽样 pct 一致性（离线，零额外请求）
        xc = self.datasource_cfg.get("contract", {}) or {}
        sample_n = int(xc.get("pct_sample_size", 20))
        tol_pct = float(xc.get("pct_tolerance_pct", 0.5))
        pct_warnings = source.pct_consistency_sample(bars, sample_n, tol_pct)

        self.contract_warnings = list(source.warnings) + det_warnings + pct_warnings
        for w in self.contract_warnings:
            log.warning("[契约监控] %s", w)
        log.info(
            "腾讯快照完成: 请求 %d / 解析 %d（失败批 %d/%d），除权候选 %d 只（切换日=%s）",
            source.requested_count, source.parsed_count, source.failed_batches,
            source.total_batches, len(self._candidates), cutover,
        )

    def _populate_cutover_events(self, run_day: str) -> None:
        """切换日 bootstrap（brief §6）：对命中候选取腾讯 raw+qfq K线 N 根，覆盖缺口期。

        - (a) 回补缺口期缺失 K线行（cache_tail < d < run_day，raw close；isST/tradestatus
          置 0——历史日无快照可派生，run_day 行的准确值由 kline_af3_incremental 的快照
          bar 提供；停牌/退市股缺口由周扫 BaoStock 精确对账兜底）。
        - (b) 用 **qfq/raw 比值**检测缺口期除权事件（非 hfq——hfq 总收益口径每日漂移
          ~0.4% 不可用；qfq 前复权锚定最新价，两事件间比值恒=1、除权日跳 r_event）。
          每个事件存入 self._cutover_events[code]，供 maybe_refresh_adjfactor 逐事件补因子。

        请求数 = 候选数（≤ cutover_max_candidates=300），远低于腾讯 ≤200 次/日的**生产**
        预算——切换日是一次性 bootstrap，且仅对命中候选触发（稳态 0 次）。
        """
        ksrc = self._kline_source
        if ksrc is None:
            ksrc = TencentKlineSource(self.datasource_cfg.get("tencent", {}) or {})
            self._kline_source = ksrc
        events_map: Dict[str, List[Tuple[str, float]]] = {}
        for code in list(self._candidates.keys()):
            tail_rows = self._kline_af3_tail_rows(code, 1)
            if not tail_rows:
                continue  # 无缓存 → kline_af3_incremental 走 BaoStock 全量回补（不在此处理）
            tail_date = str(tail_rows[-1][0]).strip()
            raw_closes, events = ksrc.gap_events(code, start_after=tail_date, end_before=run_day)
            if not raw_closes:
                continue  # K线取数失败 → 该股跳过（周扫兜底），绝不从陈旧缓存推导因子
            # (a) 回补缺口期缺失 K线行（严格 < run_day；run_day 由快照 bar 提供）
            gap_rows = [
                [d, code, f"{c:.4f}", "0", "0"]
                for d, c in raw_closes if tail_date < d < run_day
            ]
            if gap_rows:
                self.kline_af3_append(code, gap_rows)  # 内部按日期去重，幂等
            # (b) 记录缺口期除权事件（升序）
            if events:
                events_map[code] = events
        self._cutover_events = events_map
        n_ev = sum(len(v) for v in events_map.values())
        log.info(
            "切换日 bootstrap: %d 只候选取 K线，回补缺口行、检出缺口期除权事件 %d 个（%d 只）",
            len(self._candidates), n_ev, len(events_map),
        )

    def _append_run_day_from_bar(self, code: str, run_day: str, bar: StockBar) -> None:
        """把腾讯快照当日行追加到 kline_af3 稳定键缓存（close=idx3、isST=名称前缀、tradestatus=vol规则）。"""
        if bar.close is None:
            return
        row = [run_day, code, f"{bar.close:.4f}", str(bar.is_st), str(bar.tradestatus)]
        self.kline_af3_append(code, [row])

    def _read_kline_data(self, code: str) -> Optional[KlineData]:
        """读已落库缓存末行 → 运行日 KlineData（幂等：不重复追加）。"""
        hit = self.kline_af3_history(code)
        if not hit or not hit["rows"]:
            return None
        r = hit["rows"][-1]
        idx = {name: i for i, name in enumerate(hit["columns"])}

        def col(name: str) -> str:
            i = idx.get(name)
            return str(r[i]).strip() if (i is not None and i < len(r)) else ""

        close_s = col("close")
        return KlineData(
            code=code,
            dates=[col("date")],
            closes=[to_float(close_s) or 0.0],
            tradestatus=[to_int(col("tradestatus")) or 0],
            last_date=col("date"),
            n_rows=len(hit["rows"]),
            current_price=to_float(close_s),
            is_st=to_int(col("isST")),
            run_day_tradestatus=to_int(col("tradestatus")),
        )

    def kline_af3_incremental(self, code: str, run_day: str) -> Optional[KlineData]:
        """v2 核心：稳定键增量更新 + 运行日快照（替代旧 kline_run_day）。

        - 缓存缺失 → 全量拉取（IPO/2000 起 ~ run_day）+ 写缓存；
        - 缓存存在且尾日期 == run_day → 0 次查询（纯命中）；
        - 否则尾部追加 [last_date+1, run_day]（通常仅当日 1 根）。

        返回运行日 KlineData（current_price/is_st/tradestatus），无数据 → None。
        该查询同时提供当前价(af3 close)与窗口扩展，每股 K线查询 2 次→1 次。

        v4（报告 R3 + TL 修正）：primary=tencent 时高频路径走腾讯批量快照——
        run_screener 启动时惰性拉一次全市场快照（_ensure_snapshot），本函数逐股把当日行
        append 到 kline_af3_{code}.csv（close=idx3、isST=名称前缀、tradestatus=vol规则）。
        **幂等**：缓存尾 == run_day（今天 BaoStock cron 已跑过）→ 跳过追加，直接读缓存。
        primary=baostock（显式 cfg / strategy.yaml 配 baostock）→ 走下方原 BaoStock 路径。
        """
        # ---- v4 腾讯主路径（primary=tencent）----
        if self._is_tencent_primary():
            self._ensure_snapshot(run_day)
            last = self.kline_af3_last_date(code)
            if last == run_day:
                # 幂等：今天已落库（BaoStock cron 先跑过 / 本运行已 append）→ 0 追加直接读
                return self._read_kline_data(code)
            bar = (self._snapshot or {}).get(code)
            if bar is not None and bar.close is not None:
                # 快照命中 → append 当日行（close=idx3、isST=名称前缀、tradestatus=vol规则）
                self._append_run_day_from_bar(code, run_day, bar)
                return self._read_kline_data(code)
            # 快照缺该 code（退市/新股未入快照）→ 回退 BaoStock 路径（下方原逻辑）
            log.debug("腾讯快照缺 %s，回退 BaoStock", code)

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
        :return: True = 实际发生了因子查询/推导（供统计/日志）。

        v4（TL 修正）：primary=tencent 时改用 preclose 检测 + 本地推导——命中当日除权
        候选集（detector 输出，_ensure_snapshot 一次计算）→ r_event=close_prevday/preclose_today、
        new_factor=old_back×r_event、append adjfactor 行（**零额外请求**，全用已拉快照+缓存）；
        未命中 → 0 查询。hfq 不作因子来源（TL 强制）。primary=baostock → 走下方原事件驱动路径。
        """
        # ---- v4 腾讯主路径：preclose 检测 + 本地推导（零额外请求）----
        if self._is_tencent_primary():
            if self.run_day is None:
                return False
            rd = self.run_day.isoformat()
            self._ensure_snapshot(rd)
            cand = (self._candidates or {}).get(code)
            # 切换日缺口期多事件回补（brief §6）：该缓存尾 < run_day 的候选，用 qfq/raw
            # 比值检出的缺口期除权事件**逐事件**补因子（每个 r_event 独立、累乘）。
            tail_rows = self._kline_af3_tail_rows(code, 1)
            tail_date = str(tail_rows[-1][0]).strip() if tail_rows else rd
            gap_events = (self._cutover_events or {}).get(code)
            if cand is None and not gap_events:
                return False  # 未命中除权候选且无缺口期事件 → 0 查询（绝大多数股票稳态）
            old_back = 1.0
            af_hit = self.adjfactor_history(code)
            if af_hit and af_hit["rows"]:
                try:
                    old_back = float(str(af_hit["rows"][-1][3]).strip())
                except (ValueError, IndexError):
                    old_back = 1.0
            # 组装待写入事件列表 [(ex_date, r_event), ...]（升序）：
            # - 切换日候选 → 缺口期 qfq/raw 事件 + run_day 当日 preclose 事件（若 cand 存在）；
            # - 稳态候选（缓存尾==run_day）→ 仅 run_day 当日 preclose 事件。
            events: List[Tuple[str, float]] = []
            if gap_events and tail_date < rd:
                last_af = self.adjfactor_last_date(code) or ""
                events.extend(e for e in gap_events if e[0] > last_af)
            if cand is not None:
                events.append((rd, cand.r_event))
            if not events:
                return False
            # sanity 上界：|r_event-1|>cap 的事件不写入（防异常昨收污染因子序列）。
            # backAdjustFactor 是 IPO 起累计值 → new_back 逐事件累乘 old_back×r1×r2...。
            cap = float(self.datasource_cfg.get("exdate_detector", {})
                        .get("factor_sanity_cap_pct", 30.0)) / 100.0
            rows: List[List[str]] = []
            cur_back = old_back
            for ex_date, r_event in events:
                if abs(r_event - 1.0) > cap:
                    log.warning("复权因子 %s 除权日 %s r_event=%.4f 超 sanity ±%.0f%%，不写入",
                                code, ex_date, r_event, cap * 100)
                    continue
                cur_back = round(cur_back * r_event, 6)
                rows.append([code, ex_date, "1.0", f"{cur_back:.6f}", f"{cur_back:.6f}"])
            if not rows:
                return False
            self.adjfactor_append(code, rows)  # 内部按除权日去重+升序（幂等）
            for row in rows:
                log.info("复权因子腾讯推导: %s 除权日 %s new_back=%.6f",
                         code, row[1], float(row[3]))
            return True

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
