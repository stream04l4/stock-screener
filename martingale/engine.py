# -*- coding: utf-8 -*-
"""martingale.engine —— 每股状态机（深回撤金字塔加仓）。

**权威来源 = scripts/backtest_martingale.py（f6 已验收基线）逐行移植**。
f6 验收配置 = --ladder high --rearm bounce --rearm-pct 0.15 --exit none
--stop-add-pct 0 --stop-out-pct 0 --max-position 0 --max-adds 99 --accounting cash
--commission-bps 3 --stamp-bps 5。本引擎 = 该配置下参考代码的**行为等价删减**：
- 只保留 high-ladder 分支（参考 L217-285 语义）：rearm（bounce 先更新 low 再判
  反弹 / newhigh）→ 逐档 while 买入（先比档再买、跳空一日穿多档、档金额
  base×growth^k、first_amount 一次性）；
- exit=none/stop_add=0/stop_out=0/max_position=0 的分支整体不存在（规格：不卖、
  不封顶、无熔断，exit/stop/max_position 参数一并移除）；max_adds=99 在验收
  数据上从未触顶（单周期最大 k < 5），规格"上不封顶" → while True 严格等价；
- 保留分红（除息日前一日收盘持仓×cash_dps，当日买入前执行；送转按比例加股）；
- 保留停牌除权顺延 + 隐含除权推导（af_ratio 算法，含送转整数判定/原始价缺口
  校验/因子 glitch 忽略——f6 数字含此推导贡献，改任何一处必偏）；
- 组合聚合 = 参考 L404-412 逐行（含尾部 `g_value[gi[-1]:] += v_arr[-1]` 的
  末值结转方式——f6 基线即此行为，勿"修正"）。

成本常量（TL 拍板：不进 schema、不进 Web）：
- comm = 3bps 买入佣金 —— f6 已验收基线即含此成本（改 0 则七股逐位必偏）；
- stamp = 5bps 卖出印花税 —— 本策略永不卖出，no-op（保留常量仅为规格完整性）。

信号/成交口径（与参考一致，勿混）：
- 信号（状态机比较）= kline_daily_hfq.close（后复权，自动吸收除权跳空）；
- 成交（股数/市值）= kline_daily.close（原始价）；
- 分红 = dividend_events（cash_dps 现金 / stk_div 送转，除息日 ex_date）。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .config import StrategyParams

# 档线比较 epsilon（与参考一致：浮点噪声防护，1e-9 远小于任何真实价差）
_EPS = 1e-9

StockSeries = Tuple[np.ndarray, np.ndarray, np.ndarray]  # (dates[ns], sig, exec_c)


def _to_ns(d) -> np.datetime64:
    """DB DATE/TIMESTAMP → datetime64[ns]（与参考 pd.Timestamp(...).to_datetime64()
    对齐——exmap key 与 dates 数组必须同精度，searchsorted/get 才能命中）。"""
    return np.datetime64(str(d)[:10], "D").astype("datetime64[ns]")


def load(con, codes: Optional[Sequence[str]] = None, start: Optional[str] = None,
         end: Optional[str] = None):
    """加载回测所需 K线（hfq 信号 + 原始成交）/ 分红事件 / 名称。

    返回 (names, data, divs)：
    - names: {ts_code: name}
    - data:  {ts_code: (dates: datetime64[ns], sig: 后复权收盘, exec_c: 原始收盘)}
    - divs:  {ts_code: {datetime64[ns]: (cash_dps, stk_div)}}

    WHERE 与参考 load() 逐条一致：k.close IS NOT NULL AND k.close > 0 AND
    h.close IS NOT NULL AND h.close > 0 [+日期窗口 +codes]。
    """
    where_h: List[str] = ["h.close IS NOT NULL", "h.close > 0"]
    params: List[str] = []
    if start:
        where_h.append("h.date >= ?")
        params.append(start)
    if end:
        where_h.append("h.date <= ?")
        params.append(end)
    codes_in = ""
    if codes:
        codes = list(dict.fromkeys(codes))
        fmt = ", ".join(["?"] * len(codes))
        where_h.append(f"h.ts_code IN ({fmt})")
        params.extend(codes)

    sql = f"""
        SELECT h.ts_code, h.date, h.close AS sig_close, k.close AS exec_close
        FROM kline_daily_hfq h
        JOIN kline_daily k USING (ts_code, date)
        WHERE k.close IS NOT NULL AND k.close > 0 AND {' AND '.join(where_h)}
        ORDER BY h.ts_code, h.date
    """
    con.execute(sql, params)
    codes_seen: List[str] = []
    per: Dict[str, List] = {}
    for c, d, s, e in con.fetchall():
        if c not in per:
            codes_seen.append(c)
            per[c] = []
        per[c].append((d, s, e))

    data: Dict[str, StockSeries] = {}
    for c in codes_seen:
        rows = per[c]
        dates = np.array([r[0] for r in rows], dtype="datetime64[D]").astype("datetime64[ns]")
        vals = np.array([[r[1], r[2]] for r in rows], dtype=np.float64)
        data[c] = (dates, vals[:, 0], vals[:, 1])

    div_where = "ex_date IS NOT NULL"
    div_params: List[str] = []
    if codes:
        fmt = ", ".join(["?"] * len(codes))
        div_where += f" AND ts_code IN ({fmt})"
        div_params.extend(codes)
    con.execute(f"""
        SELECT ts_code, ex_date, COALESCE(cash_dps, 0) cash_dps, COALESCE(stk_div, 0) stk_div
        FROM dividend_events WHERE {div_where}
    """, div_params)
    divs: Dict[str, Dict] = {}
    for c, d, cd, sd in con.fetchall():
        divs.setdefault(c, {})[_to_ns(d)] = (float(cd or 0.0), float(sd or 0.0))

    names: Dict[str, str] = {}
    if codes_seen:
        fmt = ", ".join(["?"] * len(codes_seen))
        con.execute(f"SELECT ts_code, name FROM stock_master WHERE ts_code IN ({fmt})",
                    codes_seen)
        for c, nm in con.fetchall():
            names[c] = str(nm) if nm is not None else ""
    return names, data, divs


def run_stock(ts_code: str, dates: np.ndarray, sig: np.ndarray, exec_c: np.ndarray,
              exmap: Optional[Dict], p: StrategyParams, verbose: bool = True):
    """单股状态机（high-ladder 唯一分支；不卖、不封顶、无熔断）。

    :param verbose: True 时逐条打印隐含除权推导（CLI 行为，与参考脚本一致）；
        Web /api/positions 全池跑 101 只时传 False 避免日志洪水。

    返回 (stat, state, v_arr, c_arr, flag, buys)：
    - stat: 与参考实现 per_stock.csv 字段**逐位对齐**的统计（pnl_pct/min_unreal_pct
      为比率而非百分数；first/last_buy_date 为 datetime64[ns] 的 str，缺买为 ""）；
    - state: 当前持仓状态投影（status 子命令 / /api/positions 用）；
    - v_arr/c_arr/flag: 逐 bar 组合价值/成本/持仓标记；
    - buys: [(date_str, price, amount), ...]（date_str = datetime64[ns] 的 str，
      与 summary 买入时间表逐位一致）。
    """
    n = len(sig)
    comm = p.commission  # 3bps 买入佣金（引擎常量）

    in_pos = False
    shares = 0.0
    open_cost = 0.0        # 当前持有仓位的成本
    total_invested = 0.0
    entry_high = 0.0       # high 模式：滚动高点
    k_level = 0            # 当前阶梯已触发级数
    n_rearms = 0
    low_since_high = 0.0   # bounce 模式：当前高点设定以来的最低价
    realized = 0.0         # 本策略不卖 → 恒 0（字段保留）
    cash_div = 0.0
    n_buys = 0
    n_rounds = 0
    first_buy_i = -1
    last_buy_i = -1
    min_unreal_pct = 0.0
    buys: List[Tuple[str, float, float]] = []

    v_arr = np.zeros(n)
    c_arr = np.zeros(n)
    flag = np.zeros(n, dtype=np.int8)

    # —— 隐含除权还原（参考 L154-202 逐行移植）——
    # 湖内 stk_div 多数为空 → 从 hfq 因子跳变推导除权事件：
    # 因子倍率 = (1+送转) × (1+现金分红)；判定规则（勿改，f6 数字含其贡献）：
    #   - 因子步长落在相邻两根 bar 之间（停牌期除权在复牌日归属）
    #   - 当日有现金分红事件：送转比例接近整数(±8%)才记送转，否则视为因子舍入残差
    #   - 当日无事件：送转比例接近整数或 ≥1 → 记送转；<1 的非整数组 → 判为漏记现金分红
    #   - 原始价缺口必须与因子跳变匹配（容差 5pp+30%相对），否则视为因子源 glitch，忽略
    if exmap is not None:
        exmap = dict(exmap)
        # 停牌期除权事件 → 顺延到复牌日（首个存在的 bar）收取
        for k in list(exmap):
            i = int(np.searchsorted(dates, k))
            if i >= n or dates[i] != k:
                if i >= n:
                    del exmap[k]  # 数据终点之后的除权日，不计
                    continue
                c0, s0 = exmap.get(dates[i], (0.0, 0.0))
                c1, s1 = exmap[k]
                exmap[dates[i]] = (c0 + c1, s0 + s1)
                del exmap[k]
        for i in range(1, n):
            if sig[i] > 0 and sig[i - 1] > 0 and exec_c[i] > 0 and exec_c[i - 1] > 0:
                af_ratio = (sig[i] / exec_c[i]) / (sig[i - 1] / exec_c[i - 1])
                if af_ratio <= 1.001:
                    continue
                d = exmap.get(dates[i], (0.0, 0.0))[0]
                pc = exec_c[i - 1]
                cash_mult = pc / (pc - d) if 0 < d < pc else 1.0
                x10 = (af_ratio / cash_mult - 1.0) * 10.0
                if x10 < 0.1:
                    continue
                expected = (pc - d) / (pc * (1.0 + x10 / 10.0))  # 原始价应得比率
                actual = exec_c[i] / pc
                if abs(actual - expected) > 0.05 + 0.3 * abs(expected - 1.0):
                    continue  # 因子跳变与原始价缺口不符 → 因子数据错误，忽略
                near_int = any(abs(x10 - k) <= 0.08 * k for k in range(1, 11))
                cash0, stk0 = exmap.get(dates[i], (0.0, 0.0))
                if d > 0:
                    if x10 >= 1.0 and near_int:
                        exmap[dates[i]] = (cash0, stk0 + x10)
                        if verbose:
                            print(f"[除权推导] {str(dates[i])[:10]} 10送{x10:.2f} (当日分红{d:.3f})")
                    # 其余：与已记事件一致的舍入残差，忽略（防重复计）
                elif x10 >= 1.0 or near_int:
                    exmap[dates[i]] = (cash0, stk0 + x10)
                    if verbose:
                        print(f"[除权推导] {str(dates[i])[:10]} 10送{x10:.2f} (无分红事件)")
                else:
                    exmap[dates[i]] = (cash0 + pc * (af_ratio / cash_mult - 1.0), stk0)
                    if verbose:
                        print(f"[除权推导] {str(dates[i])[:10]} "
                              f"补记现金分红 {pc * (af_ratio / cash_mult - 1.0):.3f} 元/股")

    for i in range(n):
        sprice = float(sig[i])
        price = float(exec_c[i])
        # 1) 除息/除权：前一日收持仓 → 收现金分红（送转同步加股）
        if exmap and shares > 0:
            ev = exmap.get(dates[i])
            if ev is not None:
                cash, stk = ev
                if cash:
                    cash_div += shares * cash
                if stk:
                    shares += shares * stk / 10.0
        # 2) 再武装：newhigh=创新高; bounce=较本周期低点反弹 rearm_pct
        #    （新高可低于旧高点；先更新 low_since_high 再判反弹 → 创新低当日不触发）
        if p.rearm_mode == "bounce":
            if entry_high == 0.0:
                entry_high = sprice
                low_since_high = sprice
            else:
                if sprice < low_since_high:
                    low_since_high = sprice
                if sprice >= low_since_high * (1.0 + p.rearm_pct):
                    n_rearms += 1
                    entry_high = sprice
                    low_since_high = sprice
                    k_level = 0
        elif sprice > entry_high:
            if in_pos:
                n_rearms += 1
            entry_high = sprice
            k_level = 0
        # 3) 今日穿过的每一级都买（跳空可一日多级；v7 无熔断/封顶/次数上限）
        while True:
            level = entry_high * (1.0 - p.first_drawdown - k_level * p.add_step)
            if sprice > level + _EPS:
                break
            buy_amt = p.base_add_amount * (p.growth_ratio ** k_level)
            first_of_round = not in_pos
            if first_of_round and first_buy_i < 0:
                buy_amt = p.initial_amount  # 全策略首仓（一次性），之后按常规阶梯
            shares += buy_amt * (1.0 - comm) / price
            open_cost += buy_amt
            total_invested += buy_amt
            n_buys += 1
            last_buy_i = i
            if first_of_round:
                in_pos = True
                n_rounds += 1
                if first_buy_i < 0:
                    first_buy_i = i
            buys.append((str(dates[i]), round(float(price), 3), float(buy_amt)))
            k_level += 1
        val = shares * price + cash_div
        if in_pos and open_cost > 0.0:
            u = (val - open_cost) / open_cost
            if u < min_unreal_pct:
                min_unreal_pct = u
        v_arr[i] = val
        c_arr[i] = open_cost if in_pos else 0.0
        flag[i] = 1 if in_pos else 0

    final_assets = shares * float(exec_c[-1]) + cash_div
    total_pnl = realized + shares * float(exec_c[-1]) + cash_div - open_cost
    stat = {
        "ts_code": ts_code,
        "n_bars": n,
        "n_rounds": n_rounds,
        "n_rearms": n_rearms,
        "n_buys": n_buys,
        "total_invested": total_invested,
        "realized_pnl": realized,
        "final_assets": final_assets,
        "dividends": cash_div,               # cash 口径（固定）
        "price_pnl": total_pnl - cash_div,
        "total_pnl": total_pnl,
        "pnl_pct": (total_pnl / total_invested) if total_invested > 0 else np.nan,
        "min_unreal_pct": min_unreal_pct,
        "first_buy_date": str(dates[first_buy_i]) if first_buy_i >= 0 else "",
        "last_buy_date": str(dates[last_buy_i]) if last_buy_i >= 0 else "",
        "last_status": "open" if in_pos else ("closed" if n_rounds else "never"),
        "end_date": str(dates[-1]),
    }
    last_price = float(exec_c[-1]) if n else 0.0
    state = {
        "ts_code": ts_code,
        "in_pos": bool(in_pos),
        "shares": float(shares),
        "open_cost": float(open_cost),
        "avg_cost": (float(open_cost) / shares) if shares > 0 else 0.0,
        "last_price": last_price,
        "market_value": float(shares * last_price),
        "cash_div": float(cash_div),
        "unreal_pct": ((final_assets - open_cost) / open_cost) if open_cost > 0 else 0.0,
        "entry_high": float(entry_high),
        "low_since_high": float(low_since_high),
        "k_level": int(k_level),
        # 下一触发价位（状态机直接投影；brief §2 API 语义）
        "next_add_price": (float(entry_high * (1.0 - p.first_drawdown - k_level * p.add_step))
                           if entry_high > 0 else None),
        "next_rearm_price": (float(low_since_high * (1.0 + p.rearm_pct))
                             if (p.rearm_mode == "bounce" and low_since_high > 0) else None),
    }
    return stat, state, v_arr, c_arr, flag, buys


def aggregate_into(gdates: np.ndarray, g_value: np.ndarray, g_cost: np.ndarray,
                   g_open: np.ndarray, dates: np.ndarray, v_arr: np.ndarray,
                   c_arr: np.ndarray, flag: np.ndarray) -> None:
    """组合聚合（参考 L404-412 逐行移植）：scatter 到全局轴 + 末值向前结转
    （停牌/数据截断后持仓不消失）。

    注意：结转切片 `g_value[gi[-1]:]` 从该股末 bar 当日开始（含当日）——
    f6 基线即此行为，"修正"为 [gi[-1]+1:] 会改变 equity_curve 数值，禁止改动。
    """
    gi = np.searchsorted(gdates, dates)
    g_value[gi] += v_arr
    g_cost[gi] += c_arr
    g_open[gi] += flag
    n_dates = len(gdates)
    if len(gi) and gi[-1] < n_dates - 1:
        g_value[gi[-1]:] += v_arr[-1]
        g_cost[gi[-1]:] += c_arr[-1]
        g_open[gi[-1]:] += flag[-1]
