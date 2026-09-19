# -*- coding: utf-8 -*-
"""lake.ingest.baostock_adapter —— BaoStock 适配器（v6.1：外包 + Q6 存活门控）。

**Q6 拍板**：灌数启动探一次 ``query_all_stock``（10s socket 超时判活）——存活则按
Q1 排序参与 fallback/交叉校验；死亡则 available()=False，worker 自动跳过 BaoStock
源（不 crash、不阻塞）。本 adapter 的 ``available()`` **直接读 Q6 探测结果**
（:func:`lake.ingest.source_pool.baostock_alive`）——不在 adapter 内重复探测。

数据口径（复用 baostock_ingest 既有 fetch，零重写）：
- adj_factor = query_adjust_factor（仅除权日有行 → load_t2 前向填充）。
- T5 F10 = query_profit_data + query_growth_data + query_balance_data（**最近 4 季**——
  交叉校验用；全史 85 季 ×3 次/股超配额，不可行。BaoStock 死时零调用）。
- OHLCV = query_history_k_data_plus（含 amount）——Q1 优先级里 T2 ohlcv_amount 未列
  baostock（sina→tencent→tdx），本方法保留作 config 可调的备选能力。

纪律：一律走 BaoStockClient（QuotaGuard 日预算内，不得裸调）；stop_checker 注入
lake.backfill.stop_requested（SIGTERM 收尾加速，v6.0.10 既有机制）。
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional, Tuple

from .source_pool import AUTHORITY

log = logging.getLogger("lake.ingest.baostock_adapter")

# T5 交叉校验只比最近 N 季（全史 ×3 次/股超配额；BaoStock 死时零调用）
_F10_CROSSCHECK_QUARTERS = 4


def _bs_to_pct(v: Optional[float]) -> Optional[float]:
    """BaoStock 比率字段（小数口径）→ 百分数口径（×100），None 透传。

    **v6.1.8 F2 量纲修复**：实测核对（brief 要求"先实测核对"）——BaoStock 官方 API
    ``query_profit_data.gpMargin`` / ``query_balance_data.liabilityToAsset`` /
    ``query_growth_data.YOYPNI`` 原始返回**小数（分数）口径**，非百分数：
      - sh.601398 2026Q2 liabilityToAsset=0.923676（浦发负债率 ~92%）；
      - sh.600519 2026Q2 gpMargin=0.895552（茅台毛利率 ~89.6%）；
      - sz.000333 2026Q2 gpMargin=0.252558（美的毛利率 ~25.3%）。
    T5 主源 adata_f10 是**百分数口径**（浦发 gross_margin~18、liability~92）——两源
    直接比会恰好差 100×，cross_check_f10 全量误报（v6.1.7 实锤 52796 行 conflict_src
    100% 非空）。本 adapter 原为**裸 to_float 透传、无任何 /100**（brief 假设"adapter
    多除了 100"不成立——BaoStock 本就返回小数），故对齐口径须在此 **×100**，使
    fetch_f10 输出与 adata 同为百分数。仅比率字段缩放；npi（netProfit，绝对额=元）
    不得缩放。
    """
    return None if v is None else float(v) * 100.0


class BaoStockAdapter:
    """BaoStock 适配器（Q6 存活门控；fallback/交叉校验源）。"""

    name = "baostock"
    authority = AUTHORITY["baostock"]  # =2（Q1）

    def __init__(self) -> None:
        self._client = None
        self._lock = threading.Lock()
        # F3（v6.1.5）：available() 懒缓存（None=未探测）。首次调用触发真探测，
        # 后续直接返回缓存——与既有"懒缓存"契约一致，避免每轮 resolve_source 重复探网。
        self._avail_cache: Optional[bool] = None

    def reset_available_for_test(self) -> None:
        """测试隔离：清 available() 懒缓存（下次调用重新探测）。与其他 adapter 同接口。"""
        with self._lock:
            self._avail_cache = None

    # ---------- client 管理（懒建；QuotaGuard 内） ----------
    def _get_client(self):
        """懒建 BaoStockClient（构造即挂 QuotaGuard；stop_checker=SIGTERM 收尾钩子）。

        **v6.1.8 F4 配额 off-by-one**：``daily_quota`` 显式取 lake 日预算
        （config ``baostock_daily_budget``，缺省 5000）——与 BackfillRunner.budget_per_day
        同口径。为什么必须传：BaoStockClient 默认 daily_quota=49900（screener 侧硬上限），
        若 lake 不覆盖，QuotaGuard 硬上限(49900)≫lake 预算(5000)，到顶后仍放行大量调用
        （v6.1.7 实锤 bs_quota.json count=5004>budget 5000——守卫在到顶后仍放行）。传 budget
        后 QuotaGuard 硬上限==lake 预算，**严格 ≤budget**（acquire 在 count>budget 时拒），
        与 BackfillRunner._quota_state 的 per-task ``used>=budget`` 门形成双保险：per-task
        门防跨任务越界、QuotaGuard 硬上限防单任务内多次调用（如 T5 fetch_f10=4季×3查询）
        在任务中途越界。screener/ 零改动（本参数为既有可选构造参数，主路径默认值不变）。
        """
        if self._client is None:
            from lake.backfill import stop_requested as _bk_stop
            from lake.config import lake_cfg
            from screener.data.baostock_client import BaoStockClient

            budget = int(lake_cfg().get("baostock_daily_budget", 5000))
            self._client = BaoStockClient(stop_checker=_bk_stop, daily_quota=budget)
        return self._client

    def close(self) -> None:
        """driver 收尾调用（登出，释放会话）。"""
        with self._lock:
            if self._client is not None:
                try:
                    self._client.close()
                except Exception:  # noqa: BLE001 - close 失败不阻断
                    pass
                self._client = None

    # ---------- available：F3 硬超时真探（懒缓存 + 复用 Q6 共享结果） ----------
    @staticmethod
    def _available_timeout_s() -> float:
        """available() 真探墙钟上限（秒）。env ``BS_AVAILABLE_TIMEOUT_S`` 可覆盖（测试提速），
        缺省 **15s**（brief F3"thread+join(15s)"；<20s 红线）。与 common.fetch_timeout_s
        同模式：每次调用现读 env（monkeypatch.setenv 对已 import 模块生效）。"""
        import os as _os

        try:
            return max(1.0, float(_os.environ.get("BS_AVAILABLE_TIMEOUT_S", "15")))
        except ValueError:
            return 15.0

    def available(self) -> bool:
        """BaoStock 是否存活——**F3（v6.1.5）任何路径阻塞 ≤20s**。

        三级短路（避免重复探网 / 双连接触发 BaoStock 服务端黑名单，团队纪律）：
        1. **懒缓存命中**（``_avail_cache`` 非 None）→ 直接返回（首次后零网络）。
        2. **本进程 Q6 已探过**（:func:`baostock_probed`）→ 复用共享结果
           （灌数启动 _run_bs_probe 已探一次；此处不重复探网，只回写缓存）。
        3. **未探 → 真探**：``probe_baostock_alive``（login+query_all_stock）包
           :func:`_with_timeout` 墙钟硬上限（缺省 15s <20s 红线）——EU hang 场景在
           上限内必返回 False（detail="timeout"），绝不长挂。结果回写共享状态（后续
           resolve_source 的 baostock_alive() 直接读）+ 懒缓存。

        为什么不再只读 Q6 结果：v6.1.4 的 available() 恒读 ``baostock_alive()``，
        未探测时保守按死——但**若某路径绕过 Q6 直接调 available()（如 O4 手动探测
        之外的健康检查、或灌数启动前），它会静默 False 而不探网**；更关键的是 F3
        要求"任何代码路径在 baostock 上阻塞不得超过 20s"——available() 本身必须是有界
        的真探，而非依赖外部是否先跑过 Q6。
        """
        from .source_pool import baostock_alive, baostock_probed

        with self._lock:
            if self._avail_cache is not None:
                return self._avail_cache   # 1) 懒缓存命中（首次后零网络）
        if baostock_probed():
            # 2) Q6/其他路径已探过 → 复用共享结果（不重复探网，防双连接黑名单）
            ok = bool(baostock_alive())
            with self._lock:
                self._avail_cache = ok
            return ok
        # 3) 未探 → 真探（login+query 包硬超时 ≤20s；失败/超时→False）
        from .source_pool import _with_timeout, set_baostock_alive

        tmo = self._available_timeout_s()   # 缺省 15s（<20s 红线）；env 可覆盖（测试提速）
        try:
            from screener.data.baostock_client import probe_baostock_alive

            # 内层 wall_budget_s=15（probe 自身硬预算，原缺省会到 40s > 红线，现显式收紧）；
            # 外层 _with_timeout(tmo≤20) 双保险——任一层先到上限即判超时。两层都 <20s。
            res = _with_timeout(
                lambda: probe_baostock_alive(timeout_s=10.0, wall_budget_s=min(15.0, tmo)),
                secs=tmo, name="bs-available")
            ok = bool(res.get("alive", False))
            set_baostock_alive(ok, res.get("detail", ""))
        except TimeoutError:
            # 外层硬超时（内层线程 hang 未释放）→ False + detail="timeout"（F3 逐字）
            log.warning("baostock available() 真探墙钟超时 >%.0fs → False（EU hang 场景）", tmo)
            ok = False
            set_baostock_alive(False, "timeout")
        except Exception as exc:  # noqa: BLE001 - 探测异常→False（保守按死，不 crash）
            log.warning("baostock available() 真探失败 → False: %s", exc)
            ok = False
            set_baostock_alive(False, f"{type(exc).__name__}: {str(exc)[:80]}")
        with self._lock:
            self._avail_cache = bool(ok)
        return self._avail_cache

    # ---------- fetch_adj_factor：query_adjust_factor（事件值） ----------
    def fetch_adj_factor(self, ts_code: str, start: str, end: str) -> Dict[str, float]:
        """BaoStock 复权因子（仅除权日有行）。失败/空 → {}（调用方回退下一源）。

        QuotaGuard 内（1 次配额/股）；配额耗尽抛 BaoStockError → 调用方捕获回退。

        **DEFECT-HANG-1（R2）**：baostock 协议层在**服务端关连接**时 ``send_msg``
        内部 ``while True: recv`` 对空读 IndexError→except→返回 None→上层重试，形成
        **~100% CPU 紧循环**（09-16 shutdown-spin 根因：strace 327k recvfrom/8s on
        CLOSE-WAIT socket）。socket timeout patch 只救"半死连接慢滴答"，救不了这种
        协议层自旋。套 fetch_with_timeout 墙钟硬上限：超时抛 FetchTimeoutError →
        本方法返回 {}（回退下一源），挂死线程 daemon 随进程退出回收（其持有的
        baostock 模块级 socket fd 由 OS 随进程消失）。
        """
        from .baostock_ingest import fetch_adjust_factor, load_t2_adj_factor

        from .common import fetch_with_timeout

        try:
            fields, rows = fetch_with_timeout(
                fetch_adjust_factor, self._get_client(), ts_code, start, end)
        except Exception as exc:  # noqa: BLE001 - BaoStock 失败/超时 → {}（回退，不 crash）
            log.warning("baostock adj_factor 失败 %s: %s", ts_code, exc)
            return {}
        return load_t2_adj_factor(None, ts_code, rows) if rows else {}

    # ---------- fetch_kline：query_history_k_data_plus（含 amount；备选能力） ----------
    def fetch_kline(self, ts_code: str, start: Optional[str] = None,
                    end: Optional[str] = None) -> Dict[str, Any]:
        """BaoStock 日K（OHLCV+amount，volume=股）。Q1 优先级 T2 未列本源——保留作
        config 可调备选。失败/空 → RuntimeError（worker 回退下一源）。

        :raises RuntimeError: 查询失败或取空。
        """
        import baostock as bs

        from .common import clean_date, to_float

        start = start or "1990-01-01"
        end = end or "2099-12-31"
        # DEFECT-HANG-1（R2）：同 fetch_adj_factor——baostock 协议层紧循环/半死连接
        # 墙钟兜底。超时抛 FetchTimeoutError → 本方法 except 转 RuntimeError（回退）。
        from .common import fetch_with_timeout

        try:
            fields, rows = fetch_with_timeout(
                self._get_client().call_with_fields,
                bs.query_history_k_data_plus, label=f"lake_kline_{ts_code}",
                code=ts_code, start_date=start, end_date=end,
                fields="date,open,high,low,close,volume,amount",
                frequency="d", adjustflag="2")  # 2=raw 不复权（adj 单独取）
        except Exception as exc:  # noqa: BLE001 - BaoStock 失败/超时 → 显式抛（回退下一源）
            raise RuntimeError(f"baostock K线失败 {ts_code}: {exc}") from exc

        ohlcv: List[Dict[str, Any]] = []
        for r in rows:
            d = clean_date(r[0])
            if not d:
                continue
            ohlcv.append({
                "date": d,
                "open": to_float(r[1]), "high": to_float(r[2]),
                "low": to_float(r[3]), "close": to_float(r[4]),
                "volume": to_float(r[5]),   # 股（BaoStock 口径）
                "amount": to_float(r[6]),   # 元
            })
        if not ohlcv:
            raise RuntimeError(f"baostock K线取空 {ts_code}（回退下一源）")
        return {"ohlcv": ohlcv, "adj_factor": None}

    # ---------- fetch_f10：T5 最近 N 季（交叉校验用） ----------
    def fetch_f10(self, ts_code: str) -> List[Dict[str, Any]]:
        """BaoStock T5 基本面**最近 4 季**（交叉校验用；全史超配额不可行）。

        每季 = query_profit_data + query_growth_data + query_balance_data（3 次配额）。
        :return: [{period, pub_date, roe_weighted(None→BaoStock 无加权ROE), gross_margin,
                  liability_pct, yoy_pni, npi, ocf(None)}]；失败/空 → []。

        **字段口径（v6.1.8 F2）**：gross_margin/liability_pct/yoy_pni 已 **×100 转百分数**
        （BaoStock 原始=小数，见 :func:`_bs_to_pct`），与 adata_f10_adapter 同口径——
        cross_check_f10 按 period 对齐比 roe_weighted/gross_margin/liability_pct（绝对差
        >1pp）时两源单位一致，不再全量误报。npi=netProfit 绝对额（元），不缩放。
        BaoStock roe_avg≠加权ROE，该字段两边都 None 时 cross_check 自动跳过，不误报分歧。
        """
        import datetime as _dt

        from .baostock_ingest import fetch_balance, fetch_growth, fetch_profit
        from .common import to_float

        today = _dt.date.today()
        # 最近 N 季候选（含当季——未披露时 BaoStock 返回空，自动跳过）
        candidates: List[Tuple[int, int]] = []
        y, q = today.year, (today.month - 1) // 3 + 1
        for i in range(_F10_CROSSCHECK_QUARTERS):
            candidates.append((y, q))
            q -= 1
            if q == 0:
                q, y = 4, y - 1

        client = self._get_client()
        out: List[Dict[str, Any]] = []
        # DEFECT-HANG-1（R2）：整段 F10（≤4 季 ×3 查询）墙钟兜底——baostock 协议层
        # 紧循环/半死连接。超时抛 FetchTimeoutError → 本方法 except 返回已取部分
        # （交叉校验源，缺失不阻断 T5 主源 adata）。
        from .common import fetch_with_timeout

        try:
            out = fetch_with_timeout(self._fetch_f10_inner, client, ts_code, candidates)
        except Exception as exc:  # noqa: BLE001 - F10 失败/超时 → 返回已取部分（不阻断）
            log.warning("baostock T5 F10 失败 %s: %s", ts_code, exc)
        return out

    def _fetch_f10_inner(self, client, ts_code: str,
                         candidates: List[Tuple[int, int]]) -> List[Dict[str, Any]]:
        """fetch_f10 的取数主体（供 fetch_with_timeout 墙钟包裹；见其 docstring）。"""
        from .baostock_ingest import fetch_balance, fetch_growth, fetch_profit

        out: List[Dict[str, Any]] = []
        for year, quarter in candidates:
            try:
                profit = fetch_profit(client, ts_code, year, quarter)
                if not profit:
                    continue
                growth = fetch_growth(client, ts_code, year, quarter)
                balance = fetch_balance(client, ts_code, year, quarter)
            except Exception as exc:  # noqa: BLE001 - 单季失败跳过（不阻断其余季）
                log.warning("baostock T5 %s %dQ%d 失败: %s", ts_code, year, quarter, exc)
                continue
            out.append({
                "period": f"{year}Q{quarter}",
                "pub_date": profit.get("pubDate"),
                # BaoStock 无加权 ROE（只有 roeAvg）→ None（cross_check 跳过该字段）
                "roe_weighted": None,
                # F2 量纲：BaoStock 比率原始=小数 → ×100 对齐 adata 百分数口径
                # （实测核对见 _bs_to_pct docstring；npi 是绝对额=元，不缩放）
                "gross_margin": _bs_to_pct((profit or {}).get("gpMargin")),
                "liability_pct": _bs_to_pct((balance or {}).get("liabilityToAsset")),
                "yoy_pni": _bs_to_pct((growth or {}).get("YOYPNI")),
                "npi": (profit or {}).get("netProfit"),
                "ocf": None,
            })
        return out

    # ---------- fetch_index_kline：BaoStock 指数非本 adapter 职责 ----------
    def fetch_index_kline(self, index_code: str, n: int = 290) -> List[Dict[str, Any]]:
        """T7 走腾讯/tdx——BaoStock adapter 不提供指数（返回空）。"""
        return []
