# -*- coding: utf-8 -*-
"""v5.2 Phase 1：akshare 校验源客户端（pin ==1.18.88，仅交叉校验用途）。

定位（TL 拍板 Q1 + brief 硬约束）：
- **只做校验源**，绝不进主计算路径；任何失败 → 降级 None/[] + warning，不阻塞主流程。
- 复用 sina.py breaker 模式（D9' 纪律同构）：串行、间隔 >=1s（全局限速锚点）、
  重试 <=2 次、某类接口连续失败 >=breaker → 该类当日整体降级（不得死磕）。
- 每次成功调用落 raw（source=akshare_em / akshare_sina，零转换原始行），供 canonical
  重建 + EmptyPayloadGuard 历史非空判断。

实测口径（报告 §2.1 / evidence/boundary_probe_round5.json）：
- ``stock_zh_a_daily``（新浪 hq 系）：close 与本地 af3 逐位一致（52/52）；全史深度。
- ``stock_fhps_detail_em``（datacenter-web 东财）：**"现金分红比例"=每10股口径，须 /10**；
  与 em_dividend_all 同源（都是东财）→ 一致性检查而非独立校验。

akshare 为非官方接口、版本迭代快 → pin 版本 + 契约监控（列名/行数），漂移时降级为
"仅告警"不阻塞主路径（报告 §7 风险 1）。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

log = logging.getLogger("screener.data.akshare")

# akshare pin 版本（brief 硬约束；导入时校验，漂移 → 告警不阻塞）
PINNED_VERSION = "1.18.88"

# fhps_detail 列名契约（akshare 1.18.88 实测；漂移 → 降级 None + warning）
FHPS_COL_BONUS = "现金分红-现金分红比例"      # 每10股口径（须 /10）
FHPS_COL_EXDATE = "除权除息日"
FHPS_COL_PROGRESS = "方案进度"
FHPS_COL_REPORT = "报告期"


def _import_akshare():
    """惰性导入 + pin 版本校验。失败 → None（调用方按降级处理）。"""
    try:
        import akshare as ak
    except Exception as exc:  # noqa: BLE001 — akshare 缺失/损坏不得炸主路径
        log.warning("akshare 导入失败（校验源降级停用）: %s", exc)
        return None
    ver = getattr(ak, "__version__", "")
    if ver != PINNED_VERSION:
        log.warning("akshare 版本 %s != pin %s（接口可能漂移，校验结果仅供参考）", ver, PINNED_VERSION)
    return ak


def _nan_none(v: Any) -> Any:
    """pandas NaN/NaT → None；其余原样。"""
    if v is None:
        return None
    try:
        import math
        if isinstance(v, float) and math.isnan(v):
            return None
    except (TypeError, ValueError):
        pass
    try:
        import pandas as pd
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return v


class AkshareValidationClient:
    """akshare 校验源受控客户端（breaker 模式同构 SinaClient，D9'）。

    :param cfg: config.health_cfg(cfg) 的 akshare 子段（interval_s/max_attempts/breaker）。
    """

    def __init__(self, cfg: Dict[str, Any]) -> None:
        self.interval_s = float(cfg.get("akshare_interval_s", 1.0))
        if self.interval_s < 1.0:
            self.interval_s = 1.0  # D9' 同构：串行限速下限 1s
        self.max_attempts = int(cfg.get("akshare_max_attempts", 2))
        self.breaker = int(cfg.get("akshare_breaker", 5))
        # 重试退避基数（秒）；生产=1.0（D9'），测试可置 0（离线零等待）
        self.retry_sleep_s = float(cfg.get("akshare_retry_sleep_s", 1.0))
        # 串行限速开关：生产=true（间隔 >=1s，D9' 同构）；测试=false（离线零等待）
        self.throttle_enabled = bool(cfg.get("akshare_throttle_enabled", True))
        self.request_count = 0
        self._last_ts = 0.0
        self._consec_fail: Dict[str, int] = {}
        self._tripped: set = set()

    # ---------- breaker（同构 SinaClient；熔断后**降级 None**，绝不抛进主路径） ----------
    def _breaker_ok(self, category: str) -> bool:
        try:
            self._check_breaker(category)
            return True
        except AkshareDataError:
            log.warning("[AKSHARE] %s 已熔断 → 本次调用降级 None（D9' 同构，不阻塞主路径）", category)
            return False

    def _check_breaker(self, category: str) -> None:
        if category in self._tripped:
            raise AkshareDataError(
                f"akshare {category} 已连续失败 >= {self.breaker} 次并熔断——该类校验当日降级")

    def _note_failure(self, category: str) -> None:
        n = self._consec_fail.get(category, 0) + 1
        self._consec_fail[category] = n
        if n >= self.breaker and category not in self._tripped:
            self._tripped.add(category)
            log.warning("akshare %s 连续失败 %d 次 → 熔断：该类校验当日降级（D9' 同构）", category, n)

    def _note_success(self, category: str) -> None:
        self._consec_fail[category] = 0

    def _throttle(self) -> None:
        if self.throttle_enabled and self.interval_s > 0:
            elapsed = time.time() - self._last_ts
            if elapsed < self.interval_s:
                time.sleep(self.interval_s - elapsed)

    # ---------- 日K（新浪 hq 系；close 校验源） ----------
    def daily_closes(self, code6: str, start_date: str, end_date: str) -> Optional[List[Dict[str, Any]]]:
        """单只 [start,end] 日K → [{date, close}]（升序）。失败/熔断 → None。

        :param code6: 6 位代码（如 "601398"）；内部转 akshare symbol（sh/sz 前缀）。
        """
        if not self._breaker_ok("daily"):
            return None
        ak = _import_akshare()
        if ak is None:
            return None
        symbol = ("sh" if code6.startswith(("6", "9")) else "sz") + code6
        last_err = "unknown"
        # 首次 + max_attempts 次重试（D9' 同构 SinaClient：max_attempts=重试次数 <=2）
        for attempt in range(1, self.max_attempts + 2):
            try:
                self._throttle()
                t0 = time.time()
                df = ak.stock_zh_a_daily(symbol=symbol, start_date=start_date, end_date=end_date)
                self.request_count += 1
                self._last_ts = time.time()
                if df is None or len(df) == 0:
                    # 空 payload：可能是停牌/新股，也可能是静默缺数据——
                    # EmptyPayloadGuard 由调用方结合 rawstore 历史判定（本层不判）。
                    self._note_success("daily")
                    return []
                out = []
                for _, r in df.iterrows():
                    d = _nan_none(r.get("date"))
                    c = _nan_none(r.get("close"))
                    if d is None or c is None:
                        continue
                    out.append({"date": str(d)[:10], "close": float(c)})
                self._note_success("daily")
                log.info("[AKSHARE] daily %s %d 行 %.2fs", code6, len(out), time.time() - t0)
                return out
            except Exception as exc:  # noqa: BLE001 — akshare 异常形态多（ConnectionError 等）
                last_err = f"{type(exc).__name__}: {exc}"
                if attempt <= self.max_attempts:
                    time.sleep(self.retry_sleep_s * attempt)
        self._note_failure("daily")
        log.warning("[AKSHARE] daily %s 重试 %d 次均失败: %s（校验降级）", code6, self.max_attempts, last_err)
        return None

    # ---------- 分红明细（datacenter-web 东财；DPS 一致性检查源） ----------
    def fhps_detail(self, code6: str) -> Optional[List[Dict[str, Any]]]:
        """单只分红全史 → [{ex_date, dps_per_share, progress, report_date}]。失败/熔断 → None。

        **口径换算**：akshare "现金分红比例"=每10股 → /10 得每股 DPS（boundary_probe_round5）。
        未实施（除权除息日 NaT）的行保留 progress 标记、ex_date=None（调用方按 PIT 过滤）。
        """
        if not self._breaker_ok("fhps"):
            return None
        ak = _import_akshare()
        if ak is None:
            return None
        last_err = "unknown"
        for attempt in range(1, self.max_attempts + 2):
            try:
                self._throttle()
                t0 = time.time()
                df = ak.stock_fhps_detail_em(symbol=code6)
                self.request_count += 1
                self._last_ts = time.time()
                if df is None or len(df) == 0:
                    self._note_success("fhps")
                    return []
                # 契约监控：列名漂移检测（akshare 非官方接口，报告 §7 风险 1）
                missing = [c for c in (FHPS_COL_BONUS, FHPS_COL_EXDATE) if c not in df.columns]
                if missing:
                    raise AkshareDataError(f"fhps_detail 列契约漂移（缺 {missing}）→ 降级")
                out = []
                for _, r in df.iterrows():
                    exd = _nan_none(r.get(FHPS_COL_EXDATE))
                    bonus = _nan_none(r.get(FHPS_COL_BONUS))
                    prog = _nan_none(r.get(FHPS_COL_PROGRESS))
                    rep = _nan_none(r.get(FHPS_COL_REPORT))
                    dps = None if bonus is None else round(float(bonus) / 10.0, 6)  # 每10股→每股
                    out.append({
                        "ex_date": str(exd)[:10] if exd is not None else None,
                        "dps_per_share": dps,
                        "progress": str(prog) if prog is not None else "",
                        "report_date": str(rep)[:10] if rep is not None else None,
                    })
                self._note_success("fhps")
                log.info("[AKSHARE] fhps %s %d 行 %.2fs", code6, len(out), time.time() - t0)
                return out
            except AkshareDataError as exc:
                last_err = str(exc)
                break  # 契约漂移=确定性错误，重试无意义
            except Exception as exc:  # noqa: BLE001
                last_err = f"{type(exc).__name__}: {exc}"
                if attempt <= self.max_attempts:
                    time.sleep(self.retry_sleep_s * attempt)
        self._note_failure("fhps")
        log.warning("[AKSHARE] fhps %s 重试 %d 次均失败: %s（校验降级）", code6, self.max_attempts, last_err)
        return None


class AkshareDataError(RuntimeError):
    """akshare 校验源级错误（仅内部用；对外一律降级 None，不抛进主路径）。"""
