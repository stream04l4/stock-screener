# -*- coding: utf-8 -*-
"""回测引擎测试的**合成缓存**构造器（离线，零 live）。

为 PIT 回归测试构造"含 T 之后未来数据"的 fixture：K线/复权因子/财报/分红都
在历史日期 T 之后还有数据行，用于断言 data_pit 在 T 日**读不到**那些未来数据
（R1 每个泄漏点的回归）。

所有文件经 DiskCache.put 写入（保证哨兵+表头格式与真实缓存一致）。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import List

from screener.data.cache import DiskCache


def _dates(start: date, end: date) -> List[str]:
    out, d = [], start
    while d <= end:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


# 测试基准日（PIT 截断点）：2025-03-31
T = date(2025, 3, 31)
# "未来"基准日（验证 T 之后数据在更晚日期可见）
T_LATER = date(2026, 4, 1)


def build_pit_cache(cache_dir: str, ref_code: str = "sh.601398") -> dict:
    """构造合成缓存。返回关键事实字典（供断言引用）。

    股票布局：
    - ``sh.601398``（参考股/日历）：2025-01-01~2025-06-30，close=10 恒定。
    - ``sh.600001``（主测试股）：K线 2025-01-01~2025-06-30；close[i]=10+i（逐日递增，
      便于验证"取的是哪一根"）。复权因子：2025-02-15→1.5、2025-05-10→2.0（后者在 T 之后）。
      财报 profit 2024Q4 pubDate=2025-03-28（T 之前，可见）。分红 2025：ex 2025-01-07
      （T 前）+ ex 2025-04-10（T 后）。
    - ``sh.600002``（未来财报股）：profit 2024Q4 pubDate=2025-04-15（**T 之后**，不可见）。
    - ``sh.600003``（晚上市股）：K线 2025-04-01~2025-06-30（IPO 在 T 之后 → T 日不在池）。
    """
    cache = DiskCache(cache_dir)

    # ---- 参考股（交易日历源）----
    ref_dates = _dates(date(2025, 1, 1), date(2025, 6, 30))
    cache.put(f"kline_af3_{ref_code}",
              ["date", "code", "close", "isST", "tradestatus"],
              [[d, ref_code, "10.0", "0", "1"] for d in ref_dates])

    # ---- sh.600001 主测试股：K线（close 逐日递增）----
    c1 = "sh.600001"
    d1 = _dates(date(2025, 1, 1), date(2025, 6, 30))
    cache.put(f"kline_af3_{c1}",
              ["date", "code", "close", "isST", "tradestatus"],
              [[d, c1, f"{10 + i:.4f}", "0", "1"] for i, d in enumerate(d1)])
    # 复权因子：2025-02-15→1.5（T 前）、2025-05-10→2.0（T 后）
    cache.put(f"adjfactor_{c1}",
              ["code", "dividOperateDate", "foreAdjustFactor",
               "backAdjustFactor", "adjustFactor"],
              [[c1, "2025-02-15", "0.6", "1.5", "1.5"],
               [c1, "2025-05-10", "0.4", "2.0", "2.0"]])
    # 财报 profit 2024Q4：pubDate=2025-03-28（T 之前 → 可见）
    cache.put(f"profit_{c1}_2024_4",
              ["code", "pubDate", "statDate", "roeAvg", "npMargin", "gpMargin",
               "netProfit", "epsTTM", "MBRevenue", "totalShare", "liqaShare"],
              [[c1, "2025-03-28", "2024-12-31", "0.10", "0.30", "0.40",
                "1000000000", "2.5", "4000000000", "1000000000", "800000000"]])
    # 分红 2025：ex 2025-01-07（T 前）+ ex 2025-04-10（T 后）
    cache.put(f"dividend_{c1}_2025",
              ["code", "dividPreNoticeDate", "dividAgmPumDate", "dividPlanAnnounceDate",
               "dividPlanDate", "dividRegistDate", "dividOperateDate", "dividPayDate",
               "dividStockMarketDate", "dividCashPsBeforeTax", "dividCashPsAfterTax",
               "dividStocksPs", "dividCashStock", "dividReserveToStockPs"],
              [[c1, "", "", "2024-12-01", "", "2025-01-06", "2025-01-07", "2025-01-08",
                "", "0.50", "0.45", "0.0", "10派5元", ""],
               [c1, "", "", "2025-03-20", "", "2025-04-09", "2025-04-10", "2025-04-11",
                "", "0.60", "0.54", "0.0", "10派6元", ""]])

    # ---- sh.600002：未来财报（pubDate 在 T 之后）----
    c2 = "sh.600002"
    d2 = _dates(date(2025, 1, 1), date(2025, 6, 30))
    cache.put(f"kline_af3_{c2}",
              ["date", "code", "close", "isST", "tradestatus"],
              [[d, c2, "20.0", "0", "1"] for d in d2])
    cache.put(f"profit_{c2}_2024_4",
              ["code", "pubDate", "statDate", "roeAvg", "npMargin", "gpMargin",
               "netProfit", "epsTTM", "MBRevenue", "totalShare", "liqaShare"],
              [[c2, "2025-04-15", "2024-12-31", "0.12", "0.32", "0.42",
                "2000000000", "3.0", "5000000000", "2000000000", "1600000000"]])

    # ---- sh.600003：晚上市（IPO 在 T 之后）----
    c3 = "sh.600003"
    d3 = _dates(date(2025, 4, 1), date(2025, 6, 30))
    cache.put(f"kline_af3_{c3}",
              ["date", "code", "close", "isST", "tradestatus"],
              [[d, c3, "30.0", "0", "1"] for d in d3])

    # ---- sh.600005：停牌股（2025-03-10~03-20 空 close + tradestatus=0）----
    c5 = "sh.600005"
    rows5 = []
    for i, d in enumerate(_dates(date(2025, 3, 1), date(2025, 4, 15))):
        if date(2025, 3, 10) <= date.fromisoformat(d) <= date(2025, 3, 20):
            rows5.append([d, c5, "", "0", "0"])   # 停牌：空 close
        else:
            rows5.append([d, c5, f"{50 + i:.4f}", "0", "1"])
    cache.put(f"kline_af3_{c5}",
              ["date", "code", "close", "isST", "tradestatus"], rows5)

    # ---- 行业快照（当前口径；含 updateDate）----
    cache.put("industry",
              ["updateDate", "code", "code_name", "industry", "industryClassification"],
              [["2026-08-31", c1, "测试一", "银行", "证监会二级"],
               ["2026-08-31", c2, "测试二", "银行", "证监会二级"],
               ["2026-08-31", c3, "测试三", "", ""]])

    return {
        "ref_code": ref_code, "c1": c1, "c2": c2, "c3": c3, "c5": c5,
        "d1": d1, "T": T, "T_LATER": T_LATER,
        # sh.600001 在 T=2025-03-31 的最后一根 bar：index = 天数(1/1..3/31)=90 → close=10+89
        "c1_last_idx_at_T": (T - date(2025, 1, 1)).days,
    }
