# -*- coding: utf-8 -*-
"""实时行情一主一备统一入口（重设计 §5，Gate 0 已批；Gate 3 实施 2026-11-10）。

主 = 腾讯 qt.gtimg.cn（TencentClient.fetch，批量 GBK，现网在用）；
备 = 东财 push2delay ulist（海外流量 302 转发的延时行情服务器；11-10 实测
    rc=0、f2/f15/f16/f14/f124 字段齐备、f124=最新 tick Unix 秒）。

切换粒度 = **整批**（§5 定案）：主源整批失败（异常或 0 只返回）→ 整批走备源，
**不逐只混用**；主源部分成功则保留主源结果（消费方对缺失 code 本就有湖价回退）。
为什么整批：主备口径不同（备源缺换手率/PE/市值且是延时行情），逐只混用会让
同一批股票的高低价口径不一致（触碰判定按批内最大 quote_date，混写产生歧义）。

备源缺字段 → None 显式降级（原则 5 口径不变）：turnover/pe_ttm/float_mv_yi/
total_mv_yi/pb/ttm_yield_pct 六个字段备源无对应 → None；name/price/high/low/
pct_chg/timestamp 有对应。timestamp 由 f124（Unix 秒）转**北京时间**
'YYYYMMDDHHMMSS'（与腾讯字段同格式 → touches.quote_date_of 直接可用）。

调用方（三处共用，防漂移——§5「主备逻辑单点实现」）：
    scripts/position_alert.py（微信点位提醒）
    scripts/signal_touch.py（盘中触碰信号）
    web/app.py（/api/my_positions 与 /api/strategy_signals 实时价）
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import requests

log = logging.getLogger("screener.data.realtime")

# 东财 push2delay ulist（海外 302 落地；国内直连 push2 亦可，同 schema）
EM_ULIST_URL = (
    "https://push2delay.eastmoney.com/api/qt/ulist.np/get"
    "?fltt=2&fields=f2,f3,f12,f14,f15,f16,f124&secids={secids}"
)
_EM_BATCH = 50          # 每批 secids 上限（池 68 只 → 2 批，远低于任何频控）
_EM_TIMEOUT = 6.0       # 单批超时（备源路径，整批失败→({},"")，不无限重试）
_CST = timezone(timedelta(hours=8))   # A 股行情日 = 北京时间


def _bs_to_secid(code: str) -> Optional[str]:
    """sh.600546 → '1.600546'；sz.000001 → '0.000001'。

    市场号规则：沪(1) = 6 开头（含 688 科创板）；深(0) = 0/3 开头。
    北交所(4/8/9 开头) 不在池内（原则 11 显式排除）→ None（跳过，不猜）。
    """
    if not code or "." not in code:
        return None
    prefix, num = code.split(".", 1)
    if not num.isdigit() or len(num) != 6:
        return None
    if num[0] == "6":
        market = 1
    elif num[0] in ("0", "3"):
        market = 0
    else:
        return None   # BJ/未知 → fail-closed 跳过
    if prefix.lower() != ("sh" if market == 1 else "sz"):
        # 前缀与代码段矛盾（如 sh.000001）→ 不猜，跳过
        return None
    return f"{market}.{num}"


def _em_parse(diff: List[dict]) -> Dict[str, Dict]:
    """ulist diff 列表 → {bs_code: {...}}（与 TencentClient.parse 同形状）。"""
    out: Dict[str, Dict] = {}
    for it in diff or []:
        code = str(it.get("f12", ""))
        if not code.isdigit() or len(code) != 6:
            continue
        if code[0] == "6":
            bs = f"sh.{code}"
        elif code[0] in ("0", "3"):
            bs = f"sz.{code}"
        else:
            continue
        ts_raw = it.get("f124")
        timestamp: Optional[str] = None
        if isinstance(ts_raw, (int, float)) and ts_raw > 0:
            # f124 = 最新 tick Unix 秒 → 北京时间 14 位（quote_date_of 兼容）
            timestamp = datetime.fromtimestamp(int(ts_raw), tz=_CST).strftime(
                "%Y%m%d%H%M%S")
        out[bs] = {
            "name": it.get("f14"),
            "price": it.get("f2"),
            "high": it.get("f15"),
            "low": it.get("f16"),
            "timestamp": timestamp,
            "pct_chg": it.get("f3"),
            # 备源无对应字段 → None 显式降级（原则 5）：
            "turnover": None, "pe_ttm": None, "float_mv_yi": None,
            "total_mv_yi": None, "pb": None, "ttm_yield_pct": None,
        }
    return out


def _em_fetch(codes: List[str], timeout: float = _EM_TIMEOUT) -> Dict[str, Dict]:
    """push2delay ulist 批量取价（整批制；任一批失败只丢该批，与其他批无关）。"""
    secids = [s for s in (_bs_to_secid(c) for c in codes) if s]
    if not secids:
        return {}
    out: Dict[str, Dict] = {}
    for i in range(0, len(secids), _EM_BATCH):
        batch = secids[i:i + _EM_BATCH]
        url = EM_ULIST_URL.format(secids=",".join(batch))
        try:
            resp = requests.get(url, timeout=timeout)
            if resp.status_code != 200:
                log.warning("东财 push2delay 批次 HTTP %d（%d 只）", resp.status_code, len(batch))
                continue
            data = resp.json()
            if data.get("rc") != 0:
                log.warning("东财 push2delay rc=%s（%d 只）", data.get("rc"), len(batch))
                continue
            out.update(_em_parse((data.get("data") or {}).get("diff") or []))
        except (requests.RequestException, ValueError) as exc:
            log.warning("东财 push2delay 批次失败（%d 只）: %s", len(batch), exc)
    return out


def fetch_spot(codes: List[str], timeout: float = 5.0,
               max_attempts: int = 1) -> Tuple[Dict[str, Dict], str]:
    """一主一备统一入口。

    :return: (spot, source)；spot 形状 = TencentClient.fetch（{bs_code: {...}}）；
        source ∈ {"tencent", "eastmoney_push2delay", ""}（""=两源皆败，
        消费方走原有湖价回退，只漏报不误报）。
    """
    from screener.data.tencent import TencentClient   # 延迟导入防环（tencent 不依赖本模块）
    if not codes:
        return {}, ""
    # 1) 主：腾讯（max_attempts 沿用调用方现值——生产三处均为 1，快速失败走备）
    try:
        spot = TencentClient(timeout=timeout, max_attempts=max_attempts).fetch(codes) or {}
        if spot:
            return spot, "tencent"
        log.warning("腾讯实时行情整批为空 → 整批切东财 push2delay（%d 只）", len(codes))
    except Exception as exc:  # noqa: BLE001 - 主源任何异常都触发整批切备
        log.warning("腾讯实时行情异常 → 整批切东财 push2delay: %s", exc)
    # 2) 备：东财 push2delay（不重试——备源是兜底，快速失败走湖价回退）
    try:
        spot = _em_fetch(codes) or {}
        if spot:
            return spot, "eastmoney_push2delay"
    except Exception as exc:  # noqa: BLE001
        log.warning("东财 push2delay 异常（回退湖价）: %s", exc)
    return {}, ""
