# -*- coding: utf-8 -*-
"""lake.ingest.source_pool —— v6.1 多源资源池抽象（报告 §5.1，Joel Q1-Q6 拍板）。

**核心概念**：数据湖为中心 + 数据源资源池。每字段组可来自不同源，worker 按
``resolve_source(table, field_group)`` 返回的优先级序取**第一个 available() 且成功**
的源；跨源数值分歧由 :func:`cross_check` 判阈 → 写 ``conflict_src`` 审计列（不阻断）。

**权威性排序（Q1，Joel 拍板）**：新浪(0) > 腾讯(1) > BaoStock(2) > tdx/adata(3)
> 本地推导(4)。数值越小越权威——冲突裁决取 authority 更小者，**不用时间戳新者优先**
（历史回灌场景用 fetched_at 新者覆盖会让重跑时低权威源覆盖高权威源，方向反了）。

**零回归红线**：adapter ``available()`` 懒缓存 + EU 自检——首次调用探测，失败→False
自动跳过该源（不 crash、不阻塞）；新源未装/不可达 → worker 回退既有主源路径，
主路径行为逐字节不变。

数据契约（所有 adapter 统一输出）：
- ``fetch_kline(ts_code, start=None, end=None)`` →
  ``{"ohlcv": [{date, open, high, low, close, volume(股), amount|None}],
     "adj_factor": {除权日: af}|None}``。volume **统一=股**（腾讯内部手→股换算）。
- ``fetch_adj_factor(ts_code, start, end)`` → ``{除权日: af}`` 事件值（load_t2 前向填充）。
- ``fetch_f10(ts_code)`` → ``[{period(YYYYQn), pub_date, roe_weighted, gross_margin,
  liability_pct, yoy_pni, npi, ocf|None}]``（T5；adata 仅暴露本方法——Q4 硬编码边界）。
- ``fetch_index_kline(index_code)`` → ``[{date, open, high, low, close, volume(股), amount|None}]``。
- ``available()`` → bool（懒缓存；EU 自检失败=False）。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence, runtime_checkable

log = logging.getLogger("lake.ingest.source_pool")


# ---------------------------------------------------------------------------
# F3（v6.1.5）：_with_timeout —— 任意代码路径的墙钟硬超时工具（单一事实来源）
# ---------------------------------------------------------------------------
# **为什么抽到 source_pool**：F3 要求"任何代码路径在 baostock 上阻塞不得超过
# 20s"，且 O4 手动探测 / Q6 启动探测 / adapter.available() 三处**共用同一超时
# 工具函数**（brief 逐字），不各写一套。本函数 = daemon 线程 + join(硬预算)：
#   - 正常完成 → 返回 fn 的返回值；
#   - 超预算   → 抛 :class:`TimeoutError`（⊂ OSError ⊂ Exception，调用方 except
#     接住按"失败/超时"处理）。挂死的 daemon 线程**不取消、不强杀**（C 层 socket
#     recv 不可安全中断），随进程退出由 OS 终结（fd 同回收）——与 common.py 的
#     fetch_with_timeout 同语义，但本函数是**通用版**（无取数池/停止轮询依赖），
#     供 baostock 探测类路径复用。
# 为什么独立于 common.fetch_with_timeout：fetch_with_timeout 绑死取数线程池 +
# SIGTERM 停止轮询（R2/R4 语义）；探测路径只需要"纯墙钟上限"，抽薄版避免把
# 停止信号耦合进 O4/Q6 探测。两者共享 daemon-不 join 的回收纪律。
class _TimeoutBox:
    """_with_timeout 的结果载体（区分"已完成"与"超时放弃"）。"""

    __slots__ = ("done", "value", "error")

    def __init__(self) -> None:
        self.done = False
        self.value: Any = None
        self.error: Optional[BaseException] = None


def _with_timeout(fn: Callable[[], Any], secs: float,
                  *, name: str = "timeout-task") -> Any:
    """在墙钟硬超时 ``secs`` 内执行无参 ``fn()``；超预算抛 :class:`TimeoutError`。

    :param fn: 无参 callable（调用方用 lambda/closure 绑定参数）。
    :param secs: 墙钟上限（秒）；<=0 → 视为 0（立即判超时，防误传负值）。
    :return: ``fn()`` 的返回值（正常完成时）。
    :raises TimeoutError: 超预算未完成（挂死线程 daemon 随进程退出回收，不阻塞）。

    **F3 纪律**：这是 baostock 三条探测路径（available / Q6 / O4）共用的超时工具。
    任何调用方传入的 secs 必须 ≤20s（brief 红线"阻塞不得超过 20s"）。
    """
    budget = max(0.0, float(secs))
    box = _TimeoutBox()

    def _runner() -> None:
        try:
            box.value = fn()
            box.done = True
        except BaseException as exc:  # noqa: BLE001 - 异常经 box 传回主线程
            box.error = exc
            box.done = True

    th = threading.Thread(target=_runner, name=name, daemon=True)
    th.start()
    th.join(budget)
    if not box.done:
        # 硬预算耗尽：socket hang 未被协议层释放 → 判超时（daemon 线程随进程退出，无泄漏）
        raise TimeoutError(f"{name} 墙钟超时 {budget:.0f}s（挂死线程 daemon 随进程退出回收）")
    if box.error is not None:
        raise box.error
    return box.value


# ---------------------------------------------------------------------------
# 权威性（Q1 拍板序）：数值越小越权威
# ---------------------------------------------------------------------------
AUTHORITY: Dict[str, int] = {
    "sina": 0,        # 新浪 akshare stock_zh_a_daily（T2 OHLCV/amount 主源）
    "tencent": 1,     # 腾讯（fallback；T7 主源/T3 单源）
    "baostock": 2,    # BaoStock（探测存活时参与 fallback/交叉校验）
    "tdx": 3,         # easy-tdx（协议直连非官方）
    "adata_f10": 3,   # adata F10（社区聚合）
    "local": 4,       # 本地推导（静态 csv / factors 派生）
}


# ---------------------------------------------------------------------------
# v6.1.3：SOURCE_CAPABILITIES —— 各源可提供的数据类型（静态能力表，非用户可配）
#
# 与 adapter 注册表同层的**展示元数据**（/status.source_pool.sources[].provides
# 的数据源；前端"数据源状态"卡渲染 chips）。为什么放这里而非 web_api：能力
# 是数据源池的固有属性，与 AUTHORITY 同源同生命周期——web_api 只读透出。
# table_cn 口径 = web_api._TABLE_META name_cn 的简写（T2 日K线 / T3 估值日线…），
# 与前端 SourcePoolPanel.TABLE_LABELS 对齐；role 含"主源/fallback/交叉校验"及
# 推导方式备注（hfq÷raw）。**T4 分红=本地静态缓存（零网络）不属任何在线源，
# 本表不列 T4**（brief 红线）。
# ---------------------------------------------------------------------------
SOURCE_CAPABILITIES: Dict[str, List[Dict[str, str]]] = {
    "sina": [
        {"table": "kline_daily", "table_cn": "T2 日K线",
         "field_group": "ohlcv_amount", "role": "主源"},
        {"table": "kline_daily", "table_cn": "T2 日K线",
         "field_group": "adj_factor", "role": "主源(推导)"},   # hfq÷raw 推导
    ],
    "tencent": [
        {"table": "stock_master", "table_cn": "T1 股票主档",
         "field_group": "master", "role": "主源"},
        {"table": "kline_daily", "table_cn": "T2 日K线",
         "field_group": "ohlcv_amount", "role": "fallback"},
        {"table": "valuation_daily", "table_cn": "T3 估值日线",
         "field_group": "valuation", "role": "主源"},
        {"table": "index_daily", "table_cn": "T7 指数日线",
         "field_group": "ohlcv", "role": "主源"},
    ],
    "baostock": [
        {"table": "kline_daily", "table_cn": "T2 日K线",
         "field_group": "adj_factor", "role": "fallback(探测存活时)"},
        {"table": "fundamentals_quarterly", "table_cn": "T5 季度基本面",
         "field_group": "f10", "role": "交叉校验"},
    ],
    "tdx": [
        {"table": "kline_daily", "table_cn": "T2 日K线",
         "field_group": "ohlcv_amount", "role": "fallback"},
        {"table": "kline_daily", "table_cn": "T2 日K线",
         "field_group": "adj_factor", "role": "fallback(推导)"},   # hfq÷raw 推导
        {"table": "index_daily", "table_cn": "T7 指数日线",
         "field_group": "ohlcv", "role": "fallback"},
        {"table": "index_daily", "table_cn": "T7 指数日线",
         "field_group": "amount", "role": "主源"},
    ],
    "adata_f10": [
        {"table": "fundamentals_quarterly", "table_cn": "T5 季度基本面",
         "field_group": "f10", "role": "主源"},
    ],
}


@runtime_checkable
class SourceAdapter(Protocol):
    """源适配器统一接口（粒度=按(表,字段组)取数，避免 N×M 函数爆炸——报告 §5.1）。"""

    name: str          # 源名（与 AUTHORITY key 一致）
    authority: int     # 权威性数值（Q1 排序；越小越权威）

    def available(self) -> bool:
        """EU 可达性自检（懒缓存：首次探测，失败→False 自动跳过该源）。"""
        ...

    def fetch_kline(self, ts_code: str, start: Optional[str] = None,
                    end: Optional[str] = None) -> Dict[str, Any]:
        """K线+amount（若源提供）+adj_factor 事件值（若可推导）。见模块 docstring 契约。"""
        ...

    def fetch_adj_factor(self, ts_code: str, start: str, end: str) -> Dict[str, float]:
        """复权因子事件值 {除权日: af}（仅除权日有行，load_t2 前向填充）。"""
        ...

    def fetch_f10(self, ts_code: str) -> List[Dict[str, Any]]:
        """T5 季度基本面（PIT：pub_date 必存）。"""
        ...

    def fetch_index_kline(self, index_code: str, n: int = 290) -> List[Dict[str, Any]]:
        """T7 指数日K（index_code=sh000001 等腾讯格式，无点；n=最近 N 根）。"""
        ...


# ---------------------------------------------------------------------------
# resolve_source：读 config 优先级 + available() 过滤
# ---------------------------------------------------------------------------
def _adapter_registry() -> Dict[str, SourceAdapter]:
    """懒加载 adapter 注册表（import 隔离：未装的库 import 失败→该源不进池）。

    为什么懒加载：lake.ingest.source_pool 被 lake_backfill 顶层 import，而 adata/
    easy_tdx 是重依赖（pandas 等）——模块 import 时不碰它们，只有真正 resolve 到
    对应源才 import。某库未装 → 该源 available()=False，其余源不受影响（零回归）。
    """
    global _REGISTRY
    if _REGISTRY is None:
        reg: Dict[str, SourceAdapter] = {}
        try:
            from .sina_adapter import SinaKlineAdapter

            reg["sina"] = SinaKlineAdapter()
        except Exception as exc:  # noqa: BLE001 - 新浪 adapter 不可用→跳过（不 crash）
            log.warning("sina adapter 不可用（不进池）: %s", exc)
        try:
            from .tencent_adapter import TencentKlineAdapter

            reg["tencent"] = TencentKlineAdapter()
        except Exception as exc:  # noqa: BLE001
            log.warning("tencent adapter 不可用（不进池）: %s", exc)
        try:
            from .baostock_adapter import BaoStockAdapter

            reg["baostock"] = BaoStockAdapter()
        except Exception as exc:  # noqa: BLE001
            log.warning("baostock adapter 不可用（不进池）: %s", exc)
        try:
            from .easy_tdx_adapter import EasyTdxAdapter

            reg["tdx"] = EasyTdxAdapter()
        except Exception as exc:  # noqa: BLE001 - easy_tdx 未装/vendor 缺失→跳过
            log.warning("tdx adapter 不可用（不进池）: %s", exc)
        try:
            from .adata_f10_adapter import AdataF10Adapter

            reg["adata_f10"] = AdataF10Adapter()
        except Exception as exc:  # noqa: BLE001 - adata 未装→跳过
            log.warning("adata_f10 adapter 不可用（不进池）: %s", exc)
        _REGISTRY = reg
    return _REGISTRY


_REGISTRY: Optional[Dict[str, SourceAdapter]] = None


def reset_registry_for_test() -> None:
    """测试隔离：清注册表缓存（下次 resolve 重建）。"""
    global _REGISTRY
    _REGISTRY = None


def get_adapter(name: str) -> Optional[SourceAdapter]:
    """按名取 adapter（未注册/不可用 → None，调用方跳过该源）。"""
    return _adapter_registry().get(name)


def resolve_source(table: str, field_group: str) -> List[SourceAdapter]:
    """返回 (表, 字段组) 的可用 adapter 优先级序列（config 序 + available() 过滤）。

    - **测试隔离门**：env ``LAKE_MULTISOURCE=0`` → 恒返回 []（worker 走 legacy 单源
      路径，零网络——离线单测契约；conftest autouse 默认置位，新源用例显式置 1）。
    - config ``source_priority[table][field_group]`` = [src,...]（Q1 拍板序）；
    - 源开关（sina_enabled 等）关闭 → 该源跳过；
    - adapter 未注册（库未装）或 available()=False（EU 不可达）→ 跳过；
    - **全部被过滤 → 空列表**（worker 回退既有单源路径，零回归兜底）。

    :return: 按优先级排序的 adapter 列表（首元素=主源）。
    """
    if os.environ.get("LAKE_MULTISOURCE") == "0":
        return []  # 测试隔离：强制 legacy 单源路径（零网络）
    from ..config import lake_cfg, source_priority

    cfg = lake_cfg()
    out: List[SourceAdapter] = []
    for name in source_priority(table, field_group):
        # 源开关（v6.1 config：sina_enabled/tdx_enabled/adata_f10_enabled）
        switch_key = f"{name}_enabled"
        if switch_key in cfg and not cfg[switch_key]:
            continue
        adapter = get_adapter(name)
        if adapter is None:
            continue
        try:
            if not adapter.available():
                continue
        except Exception as exc:  # noqa: BLE001 - available() 异常视为不可用（不 crash）
            log.warning("source %s available() 异常 → 跳过: %s", name, exc)
            continue
        out.append(adapter)
    return out


# ---------------------------------------------------------------------------
# cross_check：跨源交叉校验（报告 §4.3；阈值集中 config，超阈写 conflict_src）
# ---------------------------------------------------------------------------
def _fmt_num(v: Any) -> str:
    """conflict_src 摘要数值格式化（≤256B 约束下精简）。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if f == int(f) and abs(f) < 1e15:
        return str(int(f))
    return f"{f:.6g}"


def _rel_diff_pct(a: float, b: float) -> float:
    """相对差 %（以 a 为基准；a=0 → 绝对差）。"""
    if a == 0:
        return abs(b - a) * 100.0
    return abs(b - a) / abs(a) * 100.0


def cross_check_kline(rows_a: Sequence[Dict[str, Any]], src_a: str,
                      rows_b: Sequence[Dict[str, Any]], src_b: str,
                      close_pct: float = 0.5, amount_pct: float = 2.0) -> Optional[str]:
    """T2/T7 K线跨源校验：close（相对差）+ amount（若双方都有）。

    :param rows_a/rows_b: 两源 K线行（升序；date 对齐取交集）。
    :return: 分歧摘要 ``"close:sina:3891.60|tdx:3890.12;amount:..."``（≤256B 截断）；
        无分歧/可比行不足 → None。

    语义（报告 §4.3）：阈值超 → **只记审计列+告警，不阻断**（取主源值入库）。
    close 用相对差（指数点位精度要求高时调用方传更严阈值，如 T7=0.3%）。
    """
    map_b = {r["date"]: r for r in rows_b}
    close_conf: List[str] = []
    amount_conf: List[str] = []
    compared_close = 0
    for ra in rows_a:
        rb = map_b.get(ra["date"])
        if rb is None:
            continue
        ca, cb = ra.get("close"), rb.get("close")
        if ca is not None and cb is not None and ca != 0:
            compared_close += 1
            if _rel_diff_pct(ca, cb) > close_pct:
                close_conf.append(f"{src_a}:{_fmt_num(ca)}|{src_b}:{_fmt_num(cb)}")
        aa, ab = ra.get("amount"), rb.get("amount")
        if aa is not None and ab is not None and aa != 0:
            if _rel_diff_pct(aa, ab) > amount_pct:
                amount_conf.append(f"{src_a}:{_fmt_num(aa)}|{src_b}:{_fmt_num(ab)}")
    if compared_close == 0:
        return None  # 无可比行（日期无交集）→ 不判分歧
    parts: List[str] = []
    if close_conf:
        log.warning("cross_check close 分歧 %s vs %s (%d 行超阈 %.2f%%): %s",
                    src_a, src_b, len(close_conf), close_pct, close_conf[:3])
        parts.append(f"close:{';'.join(close_conf[:4])}")
    if amount_conf:
        log.warning("cross_check amount 分歧 %s vs %s (%d 行超阈 %.2f%%): %s",
                    src_a, src_b, len(amount_conf), amount_pct, amount_conf[:3])
        parts.append(f"amount:{';'.join(amount_conf[:4])}")
    if not parts:
        return None
    summary = ";".join(parts)
    return summary[:256]  # ≤256B 约束（报告 §4.2）


def cross_check_adj_factor(adj_a: Dict[str, float], src_a: str,
                           adj_b: Dict[str, float], src_b: str,
                           pct: float = 0.5) -> Optional[str]:
    """T2 adj_factor 跨源校验（末因子从严——af 误差会累积进 hfq/qfq view）。

    :param adj_a/adj_b: {除权日: af} 事件值（前向填充后的**逐日** af 亦可——调用方
        统一传"可比口径"：本实现按日期交集逐点比，末因子=交集内最大日期的 af）。
    :return: 分歧摘要或 None。

    为什么重点看末因子：hfq view = raw × af——最新 af 错 0.5% 则**所有** hfq 价格
    同比例偏移（累积放大），阈值从严（报告 §4.3）。
    """
    common = sorted(set(adj_a) & set(adj_b))
    if not common:
        return None
    conf: List[str] = []
    for d in common:
        va, vb = adj_a[d], adj_b[d]
        if va is None or vb is None or va == 0:
            continue
        if _rel_diff_pct(va, vb) > pct:
            conf.append(f"{d}:{src_a}:{_fmt_num(va)}|{src_b}:{_fmt_num(vb)}")
    if not conf:
        return None
    # 末因子优先展示（误差累积点）+ 总分歧数
    tail = common[-1]
    log.warning("cross_check adj_factor 分歧 %s vs %s (%d 事件日超阈 %.2f%%) 末因子 %s",
                src_a, src_b, len(conf), pct, tail)
    summary = f"adj:{';'.join(conf[:4])}"
    if len(conf) > 4:
        summary += f";+{len(conf) - 4}more"
    return summary[:256]


def cross_check_f10(recs_a: Sequence[Dict[str, Any]], src_a: str,
                    recs_b: Sequence[Dict[str, Any]], src_b: str,
                    pp: float = 1.0) -> Optional[str]:
    """T5 基本面跨源校验（adata vs BaoStock）：roe_weighted/gross_margin/liability_pct。

    :param recs_a/recs_b: [{period, roe_weighted, gross_margin, liability_pct,...}]。
    :param pp: 绝对百分点阈值（财报四舍五入口径差容忍宽，报告 §4.3 =1pp）。
    :return: 分歧摘要或 None。

    为什么用绝对百分点而非相对差：财务比率是百分数口径（roe=4.3% vs 5.4%），
    相对差在低值区失真（0.5%→1.5% 相对差 200% 但绝对差才 1pp）——按报告用 pp。
    """
    map_b = {r["period"]: r for r in recs_b if r.get("period")}
    conf: List[str] = []
    for ra in recs_a:
        period = ra.get("period")
        rb = map_b.get(period)
        if not period or rb is None:
            continue
        for field in ("roe_weighted", "gross_margin", "liability_pct"):
            va, vb = ra.get(field), rb.get(field)
            if va is None or vb is None:
                continue
            if abs(float(va) - float(vb)) > pp:
                conf.append(f"{period}.{field}:{src_a}:{_fmt_num(va)}|{src_b}:{_fmt_num(vb)}")
    if not conf:
        return None
    log.warning("cross_check T5 分歧 %s vs %s (%d 项超阈 %.1fpp): %s",
                src_a, src_b, len(conf), pp, conf[:3])
    summary = f"f10:{';'.join(conf[:4])}"
    if len(conf) > 4:
        summary += f";+{len(conf) - 4}more"
    return summary[:256]


# ---------------------------------------------------------------------------
# 限速（brief 红线：新浪 ≥1s/股、tdx ≥0.5s、adata F10 ≥1s）
# ---------------------------------------------------------------------------
class RateLimiter:
    """进程内最小间隔限速器（线程安全）。

    为什么用"锚点时间"而非每次 sleep(固定值)：连续调用时保证**任意两次**真实请求
    间隔 ≥ min_interval（sleep 固定值在请求本身耗时短时会累计漂移，锚点法不漂移）。
    """

    def __init__(self, min_interval_s: float) -> None:
        self.min_interval = max(0.0, float(min_interval_s))
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            sleep_s = self._last + self.min_interval - now
            if sleep_s > 0:
                time.sleep(sleep_s)
            self._last = time.monotonic()


# ---------------------------------------------------------------------------
# BaoStock 存活状态（Q6：灌数启动探一次，进程内共享）
# ---------------------------------------------------------------------------
_BS_ALIVE: Optional[bool] = None
_BS_ALIVE_LOCK = threading.Lock()


def set_baostock_alive(alive: bool, detail: str = "") -> None:
    """记录 Q6 探测结果（灌数启动时由 driver 调用；写日志+progress）。"""
    global _BS_ALIVE
    with _BS_ALIVE_LOCK:
        _BS_ALIVE = bool(alive)
    log.info("BaoStock 恢复探测(Q6): alive=%s %s", alive, detail or "")


def baostock_alive() -> bool:
    """当前进程内 BaoStock 是否存活（未探测 → False，按"死"处理——保守）。"""
    with _BS_ALIVE_LOCK:
        return bool(_BS_ALIVE)


def baostock_probed() -> bool:
    """本进程是否**已做过一次真实探测**（Q6 启动探测 / adapter.available() 真探）。

    F3（v6.1.5）：区分"未探测（None，保守按死）"与"已探测得 False（真死）"。
    adapter.available() 用它判断能否直接复用共享结果——**已探过就不再重复探网**
    （避免 Q6 启动探测 + available() 各探一次 = 双连接，团队纪律：BaoStock >~4 并发
    连接触发服务端黑名单）。未探 → False（调用方应做自己的硬超时真探）。
    """
    with _BS_ALIVE_LOCK:
        return _BS_ALIVE is not None


def reset_baostock_state_for_test() -> None:
    """测试隔离。"""
    global _BS_ALIVE
    with _BS_ALIVE_LOCK:
        _BS_ALIVE = None
