# -*- coding: utf-8 -*-
"""后复权(af=1)收盘价本地重建（调研报告 R1 核心逻辑，纯函数、离线可测）。

公式（R1-b 实测 0 误差）：
    F(t) = max{ backAdjustFactor(e) : e.dividOperateDate <= t }；若无事件则 F(t)=1.0
    af1_close(t) = af3_close(t) × F(t)

关键性质（R1-a 实测确认）：
- ``backAdjustFactor`` 是 IPO 起**累计**的因子序列，单调非降（纯累乘）。
- 某日 t 的因子只取决于 ex_date <= t 的事件 → **新除权事件不改变事件日之前
  的任何值**。因此缓存可以做到"历史不可变 + 尾部追加"：af=3 K线尾部追加、
  adjfactor 尾部追加一行，本地重建即可得到正确的全历史后复权价，无需回溯重算。

边界（R1-c）：
- 新股：因子从 IPO/首个除权日起 F=1.0，此前 af1==af3（自然成立，无需特判）。
- 停牌日：BaoStock 返回 OHLC=昨收、volume=0；af3_close=昨收、F 不变 → 重建仍精确。
- 退市：无新行/新事件，缓存保持原样。
"""
from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional, Sequence, Tuple


def _to_date(s: str) -> date:
    return date.fromisoformat(str(s).strip())


def factor_at(t: date, factors: Sequence[Tuple[date, float]]) -> float:
    """截至 t 的最新累计因子（step 函数）。

    :param t: 目标日期。
    :param factors: [(dividOperateDate, backAdjustFactor), ...]，**按除权日升序**。
        序列为空 → 1.0（IPO 起无除权事件）。
    """
    f = 1.0
    for d, v in factors:
        if d <= t:
            f = v
        else:
            break
    return f


def reconstruct_af1(
    dates: Sequence[str],
    af3_closes: Sequence[Optional[float]],
    factors: Sequence[Tuple[date, float]],
) -> List[Optional[float]]:
    """由不复权收盘价序列 + 全历史复权因子序列重建后复权收盘价。

    :param dates: K线日期（ISO，升序）。
    :param af3_closes: 与 dates 对齐的不复权收盘价（None=缺失，不参与重建）。
    :param factors: [(除权日, backAdjustFactor)] 升序；可为空（F 恒 1.0）。
    :return: 与 dates 对齐的 af1 收盘价列表（af3 为 None 处保持 None）。

    实现：双指针归并（dates 与 factors 均升序）→ O(n+m)，全市场重建秒级。
    """
    fac_dates = [d for d, _ in factors]
    fac_vals = [v for _, v in factors]
    out: List[Optional[float]] = []
    p = -1  # 最后一个 fac_dates[p] <= t 的因子下标
    for i, ds in enumerate(dates):
        t = _to_date(ds)
        while p + 1 < len(fac_dates) and fac_dates[p + 1] <= t:
            p += 1
        c = af3_closes[i] if i < len(af3_closes) else None
        out.append(None if c is None else c * (fac_vals[p] if p >= 0 else 1.0))
    return out


def rebuild_kline_series(
    kline_rows: Sequence[Sequence[str]],
    factor_rows: Sequence[Sequence[str]],
    columns: Optional[Sequence[str]] = None,
) -> Dict[str, List]:
    """从缓存原始行（DiskCache.get 的 rows）重建 (dates, af3_close, af1_close)。

    :param kline_rows: ``kline_af3_{code}`` 的行，列含 date/close（升序）。
        **按列名定位 close**（``columns`` 与缓存表头一致；缺省回退位置 2，
        即 v2 五字段布局 date,code,close,isST,tradestatus——兼容早期误存的
        15 字段文件需显式传 columns）。
    :param factor_rows: ``adjfactor_{code}`` 的行，列为 [code, dividOperateDate,
        foreAdjustFactor, backAdjustFactor, adjustFactor]（BaoStock 原序）。
    :return: {"dates": [...], "af3_close": [...], "af1_close": [...]}

    停牌日 close 为空串的行：保留日期、close=None（重建为 None，MA 计算时跳过），
    与 BaoStock 行为一致（OHLC=昨收但空 close 防御）。
    """
    if columns:
        try:
            close_i = list(columns).index("close")
        except ValueError:
            close_i = 2
    else:
        close_i = 2

    dates: List[str] = []
    af3: List[Optional[float]] = []
    for r in kline_rows:
        if not r or not str(r[0]).strip():
            continue
        d = str(r[0]).strip()
        raw_close = str(r[close_i]).strip() if close_i < len(r) else ""
        try:
            c: Optional[float] = float(raw_close) if raw_close else None
        except ValueError:
            c = None
        dates.append(d)
        af3.append(c)

    factors: List[Tuple[date, float]] = []
    for r in factor_rows:
        if not r or len(r) < 4:
            continue
        try:
            d = _to_date(str(r[1]))
            v = float(str(r[3]))
        except (ValueError, IndexError):
            continue
        factors.append((d, v))
    factors.sort(key=lambda x: x[0])

    af1 = reconstruct_af1(dates, af3, factors)
    return {"dates": dates, "af3_close": af3, "af1_close": af1}


def moving_average(values: Sequence[Optional[float]], period: int) -> List[Optional[float]]:
    """简单移动平均（前 period-1 个位置为 None；窗口内含 None → 该点 None）。

    用于 Web 个股弹框的 MA20/MA60（基于重建的后复权收盘价）。
    """
    out: List[Optional[float]] = [None] * len(values)
    for i in range(period - 1, len(values)):
        total = 0.0
        ok = True
        for v in values[i - period + 1: i + 1]:
            if v is None:
                ok = False
                break
            total += v
        if ok:
            out[i] = total / period
    return out
