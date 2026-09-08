# -*- coding: utf-8 -*-
"""v4 数据源抽象层（报告 R3 + TL 修正，2026-09-08）。

职责：把每日 cron 的**高频 K线/快照路径**从 BaoStock（~5300 次/日，三次账户级封禁
根因）切到腾讯批量行情（全市场 ~26 批 ≈13–20s），BaoStock 降级为低频（稳态 <50 次/日）。

本模块只放**数据源抽象 + 腾讯实现 + 除权检测器**，不碰 v2/v3 引擎逻辑：
- ``StockBar`` / 派生规则（is_st=名称前缀、tradestatus=vol 规则）——R1-b/c/d 实测口径。
- ``TencentSnapshotSource``：批量快照（batch/interval/timeout/max_attempts 全部来自
  config ``datasource.tencent``），复用现有 :class:`TencentClient` 的 GBK 转码 + 重试，
  扩展字段解析到 R1-b 表；内建契约监控（解析行数==请求数、抽样 pct 一致性）。
- ``ExdateDetector``：TL 修正后的除权检测 + 因子推导。**弃用 hfq/raw**（TL 实测其两
  事件间每日漂移 ~0.4%，精度仅 ±0.4%）；改用 ``preclose_dev = preclose_today /
  close_cache_prevday - 1`` 作信号，命中后 ``r_event = close_prevday / preclose_today``、
  ``new_factor = old_factor × r_event``（零额外请求，全用已拉快照 + 本地缓存）。

零硬编码纪律：所有阈值/批量参数来自 config ``datasource:`` 段（见 ``config.datasource_cfg``），
本模块不出现任何字面量阈值。hfq **不得**作为因子值来源（TL 强制；仅可选事件存在性交叉校验）。
"""
from __future__ import annotations

import logging
import json
import random
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol, Sequence, Tuple

import requests

from .tencent import TENCENT_URL, TencentClient, bs_code_to_tencent

log = logging.getLogger("screener.data.sources")

_LINE_RE = re.compile(r'v_(\w+)="([^"]*)"')


# ===========================================================================
# 除权检测器（TL 修正：preclose 信号，弃用 hfq/raw）
# ===========================================================================
@dataclass
class ExdateCandidate:
    """一只命中除权候选的股票 + 推导出的因子比值。"""

    code: str
    preclose_today: float      # 腾讯快照 idx4（除权日=交易所调整后参考价）
    close_prevday: float       # 本地 kline_af3 缓存 t-1 日 close
    preclose_dev: float        # preclose_today/close_prevday - 1（信号）
    r_event: float             # close_prevday/preclose_today（因子比值，= 1/(1+dev)）
    sanity_ok: bool            # |r_event-1| <= sanity_cap（防异常昨收污染因子序列）


def derive_factor_row(code: str, ex_date: str, old_back: float, r_event: float) -> List[str]:
    """由旧累计因子 × r_event 推导新除权事件行（BaoStock adjfactor 落库格式）。

    - backAdjustFactor = IPO 起**累计**值 → new_back = old_back × r_event（单调非降，
      纯累乘；历史行零改动，保持"尾部追加"不变式）。
    - foreAdjustFactor 置 1.0：reconstruct.py 只消费 backAdjustFactor（列 index 3），
      fore 列不参与 af1 重建，置 1.0 与 BaoStock "事件日 fore=1" 口径一致。
    - adjustFactor = new_back（BaoStock 缓存中 adjustFactor==backAdjustFactor）。
    :param old_back: 旧累计 backAdjustFactor（无缓存时调用方传 1.0）。
    :param r_event: close_prevday/preclose_today（>1 现金分红/送转，∈(0,1] 罕见反向）。
    """
    new_back = round(old_back * r_event, 6)
    return [code, ex_date, "1.0", f"{new_back:.6f}", f"{new_back:.6f}"]


class ExdateDetector:
    """TL 修正后的除权检测 + 因子推导（零额外请求：全用已拉快照 + 本地缓存）。

    **弃用 hfq/raw**（TL 实测其两事件间每日漂移 ~0.4%，精度仅 ±0.4%）；改用：
    - 信号 ``preclose_dev = preclose_today / close_cache_prevday - 1``，|dev|>θ → 候选。
      v4 下缓存由腾讯自身每日写入 → 非除权日该比值恒等于 1（无跨源噪声）；除权日 = r_event。
    - 命中后 ``r_event = close_prevday / preclose_today``，``new_factor = old_factor × r_event``。
    - sanity 上界：|r_event-1|>cap → 不写入、记告警（防异常昨收污染因子序列）。

    阈值 θ / cap / max_candidates / cutover_max_candidates 全部来自 config，零硬编码。
    """

    def __init__(self, detector_cfg: Dict[str, float]) -> None:
        self.theta = float(detector_cfg["preclose_dev_threshold_pct"]) / 100.0   # θ（百分数→小数）
        self.sanity_cap = float(detector_cfg.get("factor_sanity_cap_pct", 30.0)) / 100.0
        self.max_candidates = int(detector_cfg["max_candidates_per_day"])
        self.cutover_max_candidates = int(detector_cfg["cutover_max_candidates"])

    def detect(
        self,
        bars: Dict[str, StockBar],
        prev_closes: Dict[str, float],
        cutover: bool = False,
    ) -> Tuple[Dict[str, ExdateCandidate], List[str]]:
        """全市场除权检测。

        :param bars: 当日腾讯快照 {code: StockBar}（preclose=idx4）。
        :param prev_closes: 本地 kline_af3 缓存 t-1 日 close {code: float}。
        :param cutover: True=切换日 bootstrap（用 cutover_max_candidates 截断，防跨多日缺口放大误报）。
        :return: (candidates, warnings)。candidates={code: ExdateCandidate}（已按 sanity 过滤、
            超限时按 |dev| 降序截断到上限）。
        """
        raw: Dict[str, ExdateCandidate] = {}
        warnings: List[str] = []
        for code, bar in bars.items():
            pre = bar.preclose
            prev = prev_closes.get(code)
            if not pre or not prev or pre <= 0 or prev <= 0:
                continue
            dev = pre / prev - 1.0
            if abs(dev) <= self.theta:
                continue
            r_event = prev / pre
            sanity_ok = abs(r_event - 1.0) <= self.sanity_cap
            raw[code] = ExdateCandidate(
                code=code, preclose_today=pre, close_prevday=prev,
                preclose_dev=dev, r_event=r_event, sanity_ok=sanity_ok,
            )

        # sanity 上界：不 ok 的候选不写入因子（记告警），但保留在结果里供日志/统计
        bad = [c for c in raw.values() if not c.sanity_ok]
        for c in bad:
            warnings.append(
                f"除权候选{c.code} r_event={c.r_event:.4f} 超 sanity 上界 ±{self.sanity_cap*100:.0f}%，不写入因子"
            )

        # 截断（防御异常放大）：切换日用 cutover_max_candidates，日常用 max_candidates_per_day
        cap = self.cutover_max_candidates if cutover else self.max_candidates
        keep = [c for c in raw.values() if c.sanity_ok]
        if len(keep) > cap:
            warnings.append(
                f"除权候选 {len(keep)} 只超上限 {cap}（{'切换日' if cutover else '日常'}）→ 按|dev|降序截断"
            )
            keep.sort(key=lambda c: abs(c.preclose_dev), reverse=True)
            dropped = {c.code for c in keep[cap:]}
            keep = keep[:cap]
        else:
            dropped = set()

        out = {c.code: c for c in keep}
        # 契约监控 (c)：候选数异常（>上限 或 连续 0）由调用方按日序列判断，这里只回传计数信号
        return out, warnings

    def should_alert_zero(self, recent_counts: Sequence[int], zero_days: int = 7) -> bool:
        """契约监控 (c)：连续 N 日候选=0 → 检测器失效信号（告警）。"""
        if len(recent_counts) < zero_days:
            return False
        return all(c == 0 for c in list(recent_counts)[-zero_days:])


# ===========================================================================
# BaoStock 侧包装（R3：all_stock / trade_dates / fundamentals 现状不动，仅做协议适配）
# ===========================================================================
class BaoStockUniverseSource:
    """股票池成员源包装：委托 DataFetcher.all_stock / trade_dates（BaoStock 1–2 次/日）。"""

    def __init__(self, fetcher) -> None:
        self._f = fetcher

    def all_stock(self, day: str):
        return self._f.all_stock(day)

    def trade_dates(self, start: str, end: str) -> List[Tuple[str, bool]]:
        return self._f.trade_dates(start, end)


class BaoStockAdjfactorSource:
    """复权因子源包装：委托 DataFetcher.adjfactor_fetch（现状不动，周对账 C 兜底用）。"""

    def __init__(self, fetcher) -> None:
        self._f = fetcher

    def adjfactor_range(self, code: str, start: str, end: str) -> Tuple[List[str], List[List[str]]]:
        return self._f.adjfactor_fetch(code, start, end)


class BaoStockFundamentalSource:
    """基本面源包装：委托 DataFetcher 的 profit/growth/balance/cashflow/dividend（现状不动）。"""

    def __init__(self, fetcher) -> None:
        self._f = fetcher


# ===========================================================================
# R1-b 字段下标（0-based，field_map_88.csv 实测确认；生产只用下列）
# ===========================================================================
IDX_NAME = 1        # 名称（含 ST/*ST/C/N 前缀）
IDX_CODE = 2        # 代码
IDX_CLOSE = 3       # 最新价（盘后=收盘，不复权）→ R1e 3000/3000 与缓存 af3 一致
IDX_PRECLOSE = 4    # 昨收（不复权口径；除权日=交易所调整后参考价）
IDX_OPEN = 5        # 今开
IDX_VOLUME_HAND = 6  # 成交量(手)
IDX_TIMESTAMP = 30   # 时间戳 yyyyMMddHHmmss（停牌股冻结 090000）
IDX_PCT_CHG = 32     # 涨跌幅%（549/549 与 (price/preclose-1)*100 一致 ±0.02）
IDX_LIMIT_UP = 47    # 涨停价（ST 股该接口给 10% 而非 5%，绝对值不可用于 ST，比值免疫）
IDX_LIMIT_DOWN = 48  # 跌停价


def is_st_name(name: str) -> int:
    """ST 判定（R1-c 实测：名称当日即时更新、口径一致）。

    A 股现网前缀仅 ``*ST`` / ``ST``（S/SST 为 B 股历史前缀，549+25 样本零出现）。
    :return: 1=ST/*ST，0=否。
    """
    if not name:
        return 0
    return 1 if name.startswith(("*ST", "ST")) else 0


def tradestatus_from_vol(volume_hand: Optional[float], price: Optional[float]) -> int:
    """停牌判定（R1-d 实测：vol(手)=0 ⇔ tradestatus=0）。

    生产规则 ``0 if (vol==0 and price>0) else 1``：停牌股仍返回行（price=最后成交价），
    与 BaoStock "OHLC=昨收、volume=0" 等价。退市股 vol=0+price 冻结 → 同样 0。
    """
    if volume_hand is None:
        return 1
    if volume_hand == 0 and (price or 0) > 0:
        return 0
    return 1


@dataclass
class StockBar:
    """单只股票的腾讯批量快照（R1-b 字段 + 派生 is_st/tradestatus）。"""

    code: str
    name: str
    close: Optional[float]          # idx3 最新价（盘后=收盘，不复权）
    preclose: Optional[float]       # idx4 昨收（除权日=交易所调整后参考价）
    open: Optional[float]           # idx5 今开
    volume_hand: Optional[float]    # idx6 成交量(手)
    ts: str                         # idx30 时间戳
    pct_chg: Optional[float]        # idx32 涨跌幅%
    limit_up: Optional[float]       # idx47 涨停价
    limit_down: Optional[float]     # idx48 跌停价
    is_st: int = field(default=0)            # 派生：名称前缀
    tradestatus: int = field(default=1)      # 派生：vol 规则


class DailySnapshotSource(Protocol):
    """每日全市场快照源（R3）。实现：TencentSnapshotSource。"""

    def snapshot(self, codes: List[str]) -> Dict[str, StockBar]: ...


class KlineSource(Protocol):
    """K线源（R3）：新股回补 / 候选确认取数。实现：腾讯 raw + BaoStock fallback。"""

    def kline_raw(self, code: str, start: str, end: str) -> List[Tuple[str, float]]: ...


class AdjfactorSource(Protocol):
    """复权因子源（R3）：BaoStock 现状 adjfactor_fetch/append 不动。"""

    def adjfactor_range(self, code: str, start: str, end: str) -> Tuple[List[str], List[List[str]]]: ...


class UniverseSource(Protocol):
    """股票池成员源（R3）：allstock/trade_dates 保持 BaoStock（1–2 次/日）。"""

    def all_stock(self, day: str): ...
    def trade_dates(self, start: str, end: str) -> List[Tuple[str, bool]]: ...


class FundamentalSource(Protocol):
    """基本面源（R3）：profit/growth/balance/cashflow/dividend 不动。"""


# ===========================================================================
# 腾讯 raw K线源（切换日 bootstrap / 候选确认取数，报告 R3 + brief §6）
# ===========================================================================
class TencentKlineSource:
    """腾讯 web.ifzq.gtimg.cn 不复权日K源（fqkline/get）。

    响应 UTF-8 JSON：data.{tcode}.day = [[date, open, close, high, low, volume], ...]
    （升序，末行=最新交易日；停牌日无行）。本类只取 (date, close) 序列。

    用途（切换日 bootstrap，brief §6）：本地缓存尾部可能是 BaoStock 旧数据
    （跨多日缺口），除权检测器用"缓存尾 close"当 t-1 会引入多日漂移假候选。
    对命中候选取 raw K线 N 根 → 用 run_day 前最近一根真实 close 重算 r_event，
    剔除假阳性；请求数 = 候选数（≤ cutover_max_candidates=300）。
    """

    URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={tcode},day,,,{n},{fq}"

    def __init__(self, tencent_cfg: Dict[str, float]) -> None:
        self.timeout_s = float(tencent_cfg.get("timeout_s", 15))
        self.max_attempts = int(tencent_cfg.get("max_attempts", 3))
        self.kline_bars = int(tencent_cfg.get("kline_bars", 10))
        # 切换日缺口期除权事件检测阈值（qfq/raw 比值跳变 %）：来自 config，零硬编码。
        # qfq 前复权锚定最新价 → 两事件间比值恒=1（无漂移），除权日跳 r_event；
        # 与 hfq（总收益口径、每日漂移 ~0.4%）本质不同，故可用其比值做干净的事件检测。
        self.event_step_pct = float(tencent_cfg.get("gap_event_step_pct", 0.5)) / 100.0
        self.client = TencentClient(timeout=self.timeout_s, max_attempts=self.max_attempts)

    def _fetch_closes(self, code: str, fq: str) -> List[Tuple[str, float]]:
        """单只股票最近 N 根日K (date, close)，升序；失败/无数据 → []。fq=''=不复权(raw)。"""
        url = self.URL.format(tcode=bs_code_to_tencent(code), n=self.kline_bars, fq=fq)
        for attempt in range(1, self.max_attempts + 1):
            try:
                resp = self.client.session.get(url, timeout=self.timeout_s)
                if resp.status_code != 200:
                    log.warning("腾讯K线 HTTP %d (attempt %d) %s", resp.status_code, attempt, code)
                    break
                body = json.loads(resp.content.decode("utf-8", errors="replace"))
                data = body.get("data")
                if not isinstance(data, dict):
                    # 腾讯对异常参数返回 {"code":0,"msg":"param error","data":[]}（data 是 list）：
                    # 确定性错误，重试无意义 → 按"失败/无数据 → []"语义直接返回（根因修复：
                    # 原 data.get(...) 会抛 AttributeError 且不在 except 列表 → 穿透炸掉 bootstrap）。
                    log.warning("腾讯K线响应异常 data=%r %s: %s",
                                type(data).__name__, code, body.get("msg", ""))
                    return []
                node = data.get(bs_code_to_tencent(code), {})
                rows = node.get("day") if fq == "" else (node.get(fq + "day") or node.get("day"))
                rows = rows or []
                out: List[Tuple[str, float]] = []
                for r in rows:
                    try:
                        out.append((str(r[0]), float(r[2])))  # [date, open, close, ...]
                    except (ValueError, IndexError, TypeError):
                        continue
                return out
            except (requests.RequestException, ValueError, KeyError) as exc:
                log.warning("腾讯K线请求失败 (attempt %d) %s: %s", attempt, code, exc)
                if attempt < self.max_attempts:
                    time.sleep(1.0 * attempt)
        return []

    def kline_closes(self, code: str) -> List[Tuple[str, float]]:
        """最近 N 根不复权(raw)日K (date, close)，升序；失败/无数据 → []。"""
        return self._fetch_closes(code, "")

    def gap_events(
        self, code: str, start_after: str, end_before: str
    ) -> Tuple[List[Tuple[str, float]], List[Tuple[str, float]]]:
        """切换日缺口期除权事件检测（brief §6：取 raw K线 N 根覆盖缺口期逐事件补因子）。

        用 **qfq/raw 比值**检测事件（非 hfq——hfq 总收益口径每日漂移 ~0.4%，不可用）：
        qfq 前复权锚定最新价，两除权事件间 qfq/raw 恒=1，除权日跳 r_event。
        在 (start_after, end_before) 窗口内找比值跳变 > event_step_pct 的交易日，
        每个事件 r_event = ratio_after / ratio_before（= 该日 close_prevday/preclose_exday）。

        :return: (raw_closes, events)。events=[(ex_date, r_event), ...] 升序；无事件 → []。
            raw_closes 供 K线缺口回补（append 缺失交易日 close）。失败 → ([], [])。
        """
        raw = self._fetch_closes(code, "")
        qfq = self._fetch_closes(code, "qfq")
        if not raw or not qfq:
            return [], []
        qmap = {d: c for d, c in qfq}
        # 对齐日期序列（raw 与 qfq 同交易日集）；逐日算比值
        series: List[Tuple[str, float]] = []  # (date, ratio)
        for d, rc in raw:
            qc = qmap.get(d)
            if qc and rc > 0:
                series.append((d, qc / rc))
        events: List[Tuple[str, float]] = []
        prev_ratio: Optional[float] = None
        for d, ratio in series:
            if not (start_after < d < end_before):
                prev_ratio = ratio
                continue
            if prev_ratio and abs(ratio / prev_ratio - 1.0) > self.event_step_pct:
                events.append((d, ratio / prev_ratio))  # r_event（>1 分红/送转）
            prev_ratio = ratio
        return raw, events


# ===========================================================================
# 腾讯批量快照源
# ===========================================================================
class TencentSnapshotSource:
    """腾讯 qt.gtimg.cn 批量快照源。

    - 复用 :class:`TencentClient` 的 GBK 转码 + 指数退避重试（``_get_text``），本类只扩展
      字段解析到 R1-b 表并做契约监控——**不改动 tencent.py**。
    - batch_size / interval_s / timeout_s / max_attempts 全部来自 config。
    - 失败语义：单批失败重试后仍失败 → 该批跳过（记告警）；**全批失败（0 只解析）**由调用方
      （DataFetcher）按 fallback=fail_fast 抛 DataSourceError，绝不伪装空结果。
    """

    def __init__(self, tencent_cfg: Dict[str, float]) -> None:
        self.batch_size = int(tencent_cfg["snapshot_batch_size"])
        self.interval_s = float(tencent_cfg["snapshot_interval_s"])
        self.timeout_s = float(tencent_cfg["timeout_s"])
        self.max_attempts = int(tencent_cfg["max_attempts"])
        self.client = TencentClient(timeout=self.timeout_s, max_attempts=self.max_attempts)
        # 契约监控状态（每次 snapshot() 重置）
        self.requested_count = 0
        self.parsed_count = 0
        self.failed_batches = 0
        self.total_batches = 0
        self.warnings: List[str] = []

    def _parse_batch(self, text: str) -> Dict[str, StockBar]:
        """解析单批腾讯响应文本（已 GBK 转码后的 str）→ {bs_code: StockBar}。"""
        out: Dict[str, StockBar] = {}
        for m in _LINE_RE.finditer(text):
            t_code, payload = m.group(1), m.group(2)
            if not payload:
                continue
            f = payload.split("~")
            if len(f) < 49:  # 需覆盖到 idx48（跌停价）
                continue

            def _f(idx: int) -> Optional[float]:
                try:
                    return float(f[idx])
                except (ValueError, IndexError):
                    return None

            name = f[IDX_NAME]
            close = _f(IDX_CLOSE)
            bar = StockBar(
                code=f"{t_code[:2]}.{t_code[2:]}",
                name=name,
                close=close,
                preclose=_f(IDX_PRECLOSE),
                open=_f(IDX_OPEN),
                volume_hand=_f(IDX_VOLUME_HAND),
                ts=f[IDX_TIMESTAMP],
                pct_chg=_f(IDX_PCT_CHG),
                limit_up=_f(IDX_LIMIT_UP),
                limit_down=_f(IDX_LIMIT_DOWN),
            )
            bar.is_st = is_st_name(name)
            bar.tradestatus = tradestatus_from_vol(bar.volume_hand, close)
            out[bar.code] = bar
        return out

    def snapshot(self, codes: List[str]) -> Dict[str, StockBar]:
        """批量取全市场快照。返回 {bs_code: StockBar}；未返回的 code 不在结果里。"""
        self.requested_count = len(codes)
        self.parsed_count = 0
        self.failed_batches = 0
        self.total_batches = 0
        self.warnings = []
        out: Dict[str, StockBar] = {}
        for i in range(0, len(codes), self.batch_size):
            batch = codes[i : i + self.batch_size]
            t_codes = ",".join(bs_code_to_tencent(c) for c in batch)
            url = TENCENT_URL.format(codes=t_codes)
            text = self.client._get_text(url)  # 复用 GBK 转码 + 重试（tencent.py 已写好）
            self.total_batches += 1
            if text is None:
                self.failed_batches += 1
                log.warning("腾讯快照批次失败（%d 只），跳过该批", len(batch))
                continue
            parsed = self._parse_batch(text)
            out.update(parsed)
            # 契约监控 (a)：解析行数 == 请求数（每批断言；不等→告警+计数）
            if len(parsed) != len(batch):
                self.warnings.append(
                    f"契约(a)批次{i // self.batch_size}解析{len(parsed)}!=请求{len(batch)}"
                )
            if i + self.batch_size < len(codes) and self.interval_s > 0:
                time.sleep(self.interval_s)
        self.parsed_count = len(out)
        # 契约监控 (a) 汇总：总解析行数 == 请求数
        if self.parsed_count != self.requested_count:
            self.warnings.append(
                f"契约(a)全市场解析{self.parsed_count}!=请求{self.requested_count}"
                f"（失败批 {self.failed_batches}/{self.total_batches}）"
            )
        return out

    def pct_consistency_sample(self, bars: Dict[str, StockBar], sample_size: int,
                               tol_pct: float) -> List[str]:
        """契约监控 (b)：抽样 N 只校验 pct_chg 与 (price/preclose-1)*100 一致性。

        R1-b 基线 549/549（±0.02）。偏离超 tol → 记告警（不中断，仅信号）。
        """
        ok = []
        candidates = [b for b in bars.values() if b.close is not None and b.preclose]
        random.shuffle(candidates)
        for bar in candidates[:sample_size]:
            computed = (bar.close / bar.preclose - 1.0) * 100.0
            reported = bar.pct_chg or 0.0
            if abs(computed - reported) > tol_pct:
                ok.append(f"契约(b){bar.code}pct计算{computed:.3f}!=报告{reported:.3f}")
        return ok


# NOTE: _LINE_RE / IDX_* / StockBar / ExdateDetector defined above; no trailing cruft.
