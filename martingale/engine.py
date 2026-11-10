# -*- coding: utf-8 -*-
"""martingale.engine —— 每股状态机（深回撤金字塔加仓）。

**权威来源 = scripts/backtest_martingale.py（f6 已验收基线）逐行移植**。
f6 验收配置 = --ladder high --rearm bounce --rearm-pct 0.15 --exit none
--stop-add-pct 0 --stop-out-pct 0 --max-position 0 --max-adds 99 --accounting cash
--commission-bps 3 --stamp-bps 5。本引擎 = 该配置下参考代码的**行为等价删减**：
- 只保留 high-ladder 分支（参考 L217-285 语义）：rearm（bounce 先更新 low 再判
  反弹 / newhigh）→ 逐档 while 买入（先比档再买、跳空一日穿多档）；
- 仓位 sizing（v7.0.28，Joel 2026-11-09 拍板 C 方案，替代原 金额×growth^k）：
  第 k 档买 (first_lots + k×add_lots_step) 手 × lot_size 股，现金支出 =
  股数×成交价×(1+comm)；再武装后 k 归零 → 新轮重新从 first_lots 手起；
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

from typing import Any, Dict, List, Optional, Sequence, Tuple

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


def _annual_summary(dates: np.ndarray, v_arr: np.ndarray, cd_arr: np.ndarray,
                    du_arr: np.ndarray, ext_by_year: Dict[int, float],
                    div_by_year: Dict[int, float]) -> List[Dict[str, float]]:
    """v7.0.36 年度汇总（仅报告字段：进 summary.md 个股明细，不进 f6 字段集 /
    per_stock.csv；买入决策零改动，仅出资归属变化）。列语义（Joel 2026-11-10
    定案 v2：分红盈余优先出资，不够再外部投入）：
    - 外部投入(Y) = 当年买入中外部出资部分（Σamount 含佣金 − 分红出资）
    - 分红投入(Y) = 当年买入中分红余额出资部分（余额不够部分走外部）
    - 分红盈余(Y) = 分红余额净变动 = (收到−投出)；余额跨年结转
    - 股价盈亏(Y) = 持仓市值变化 = (v−余额)末 − (v−余额)初（含新购股份价值；
      投入的钱——无论外部还是分红——已转为市值，都含在这一项里）
    - 年底总资产(Y) = 市值 + 分红余额 = v_arr 年末值
    恒等式：资产(年末) = 资产(年初) + 股价盈亏 + 分红盈余（投入不另加——
    会重复计算）；Σ(股价盈亏+分红盈余) = 期末总资产（telescoping）。
    无持仓且无投入/分红变动的年份跳过（建仓前全零无信息量）。
    """
    n = len(dates)
    if not n:
        return []
    years = np.datetime_as_string(dates, unit="Y").astype(int)
    yearly: Dict[int, tuple] = {}
    for i in range(n):
        yearly[int(years[i])] = (float(v_arr[i]), float(cd_arr[i]), float(du_arr[i]))
    out: List[Dict[str, float]] = []
    prev_v = prev_cd = prev_du = 0.0
    for y in sorted(yearly):
        v_end, cd_end, du_end = yearly[y]
        pool_end = cd_end - du_end
        pool_prev = prev_cd - prev_du
        invested_ext = ext_by_year.get(y, 0.0)
        invested_div = div_by_year.get(y, 0.0)
        div_surplus = pool_end - pool_prev
        price_pnl = (v_end - pool_end) - (prev_v - pool_prev)
        if (prev_v > 0.0 or invested_ext > 0.0 or invested_div > 0.0
                or div_surplus > 0.0):
            out.append({"year": int(y), "invested_ext": invested_ext,
                        "invested_div": invested_div, "div_surplus": div_surplus,
                        "price_pnl": price_pnl, "end_assets": v_end})
        prev_v, prev_cd, prev_du = v_end, cd_end, du_end
    return out


def run_stock(ts_code: str, dates: np.ndarray, sig: np.ndarray, exec_c: np.ndarray,
              exmap: Optional[Dict], p: StrategyParams, verbose: bool = True):
    """单股状态机（high-ladder 唯一分支；不卖、不封顶、无熔断）。

    :param verbose: True 时逐条打印隐含除权推导（CLI 行为，与参考脚本一致）；
        Web /api/positions 全池跑 101 只时传 False 避免日志洪水。

    返回 (stat, state, v_arr, c_arr, flag, buys)：
    - stat: 与参考实现 per_stock.csv 字段**逐位对齐**的统计（pnl_pct/min_unreal_pct
      为比率而非百分数；first/last_buy_date 为 datetime64[ns] 的 str，缺买为 ""）；
      v7.0.36 起 total_invested=外部净投入（分红余额优先出资，缺口外部）、
      price_pnl=市值−外部净投入、pnl_pct=总盈亏/外部净投入；dividends=累计收到
      （口径不变）；另含报告专用键 div_pool/annual（不进 f6 字段集/per_stock.csv
      列）；
    - state: 当前持仓状态投影（status 子命令 / /api/positions 用；cash_div=分红余额）；
    - v_arr/c_arr/flag: 逐 bar 组合价值/外部净成本/持仓标记；
    - buys: [(date_str, price, amount, k, first_of_round, lots), ...]
      （date_str = datetime64[ns] 的 str，price = 原始成交价，amount = 本笔现金
      支出（含佣金，v7.0.28 手数口径），k = 该笔触发的档下标 0 起（买入前值，
      未含本笔 +1），first_of_round = 是否本轮首笔（首仓/再武装后重启），
      lots = 本笔手数（v7.0.28）。消费方 report.py / state_rows 已同步解包。）
    """
    n = len(sig)
    comm = p.commission  # 3bps 买入佣金（引擎常量）

    in_pos = False
    shares = 0.0
    open_cost = 0.0        # 当前持有仓位的成本
    gross_cost = 0.0       # Σ买入成本（毛额；v7.0.36 起仅作参照，stat 报外部净额）
    entry_high = 0.0       # high 模式：滚动高点
    k_level = 0            # 当前阶梯已触发级数
    n_rearms = 0
    low_since_high = 0.0   # bounce 模式：当前高点设定以来的最低价
    realized = 0.0         # 本策略不卖 → 恒 0（字段保留）
    cash_div = 0.0         # v7.0.36：分红余额（收到累计 − 分红优先出资投出）
    div_total = 0.0        # v7.0.36：累计收到分红（口径不变，stat["dividends"]）
    du_cum = 0.0           # v7.0.36：累计分红出资（年汇总 du_arr 用）
    ext_running = 0.0      # v7.0.36：累计外部净投入（余额优先出资的缺口部分）
    ext_by_year: Dict[int, float] = {}
    div_by_year: Dict[int, float] = {}
    n_buys = 0
    n_rounds = 0
    first_buy_i = -1
    last_buy_i = -1
    min_unreal_pct = 0.0
    buys: List[Tuple[str, float, float, int, bool, int]] = []

    v_arr = np.zeros(n)
    c_arr = np.zeros(n)
    flag = np.zeros(n, dtype=np.int8)
    cd_arr = np.zeros(n)   # v7.0.35：逐 bar 累计收到分红（仅年汇总报告用）
    du_arr = np.zeros(n)   # v7.0.36：逐 bar 累计分红出资（仅年汇总报告用）

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
                    div_total += shares * cash
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
            n_lots = p.first_lots + k_level * p.add_lots_step   # C 方案：1/2/3…手
            first_of_round = not in_pos
            buy_shares = float(n_lots * p.lot_size)
            buy_amt = buy_shares * price * (1.0 + comm)  # 现金支出（含 3bps 佣金）
            # v7.0.36：分红余额优先出资，缺口外部（Joel 2026-11-10 定案）——
            # 买入决策不看出资（状态机零改动，buy 时间表与 v7.0.35 逐位一致），
            # 仅出资归属/净值口径变化：外部净投入 = Σcost − 分红出资
            div_part = min(buy_amt, cash_div)
            ext_part = buy_amt - div_part
            cash_div -= div_part
            ext_running += ext_part
            du_cum += div_part
            _y = int(str(dates[i])[:4])
            ext_by_year[_y] = ext_by_year.get(_y, 0.0) + ext_part
            div_by_year[_y] = div_by_year.get(_y, 0.0) + div_part
            shares += buy_shares
            open_cost += buy_amt
            gross_cost += buy_amt
            n_buys += 1
            last_buy_i = i
            if first_of_round:
                in_pos = True
                n_rounds += 1
                if first_buy_i < 0:
                    first_buy_i = i
            # k = 本笔触发的档下标（买入前值）；first_of_round 供 Web 历史信号
            # 区分「首仓/本轮重启首档」与「加仓第 k+1 档」（v7.0.19）；
            # n_lots = 本笔手数（v7.0.28 手数口径，Web 历史信号展示用）
            buys.append((str(dates[i]), round(float(price), 3), float(buy_amt),
                         int(k_level), bool(first_of_round), int(n_lots)))
            k_level += 1
        val = shares * price + cash_div  # cash_div = 分红余额（v7.0.36）
        # v7.0.36：浮亏口径 = 净值(市值+分红余额) − 外部净投入，对外部出资的回报
        if in_pos and ext_running > 0.0:
            u = (val - ext_running) / ext_running
            if u < min_unreal_pct:
                min_unreal_pct = u
        v_arr[i] = val
        cd_arr[i] = div_total
        du_arr[i] = du_cum
        c_arr[i] = ext_running if in_pos else 0.0  # v7.0.36：成本轴=外部净投入
        flag[i] = 1 if in_pos else 0

    final_assets = shares * float(exec_c[-1]) + cash_div  # 市值 + 分红余额
    # v7.0.36：总盈亏 = 净值 − 外部净投入（与旧口径绝对额恒等：
    # M + 余额 − E = M + Σd − Σcost，分红出资在分子分母同时消去）
    total_pnl = realized + shares * float(exec_c[-1]) + cash_div - ext_running
    stat = {
        "ts_code": ts_code,
        "n_bars": n,
        "n_rounds": n_rounds,
        "n_rearms": n_rearms,
        "n_buys": n_buys,
        "total_invested": float(ext_running),  # v7.0.36：外部净投入（分红优先出资）
        "realized_pnl": realized,
        "final_assets": final_assets,
        "dividends": float(div_total),          # 累计收到分红（口径不变）
        "div_pool": float(cash_div),            # v7.0.36 报告专用键：分红余额
        "price_pnl": total_pnl - cash_div,      # 股价增长 = 市值 − 外部净投入
        "total_pnl": total_pnl,
        "pnl_pct": (total_pnl / ext_running) if ext_running > 0 else np.nan,
        "min_unreal_pct": min_unreal_pct,
        "first_buy_date": str(dates[first_buy_i]) if first_buy_i >= 0 else "",
        "last_buy_date": str(dates[last_buy_i]) if last_buy_i >= 0 else "",
        "last_status": "open" if in_pos else ("closed" if n_rounds else "never"),
        "end_date": str(dates[-1]),
        # v7.0.35 年汇总（报告专用；f6 字段集/per_stock.csv 列不受影响）
        "annual": _annual_summary(dates, v_arr, cd_arr, du_arr, ext_by_year,
                                  div_by_year),
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
        "cash_div": float(cash_div),  # v7.0.36：分红余额（已扣分红优先出资）
        "unreal_pct": ((final_assets - ext_running) / ext_running)
        if ext_running > 0 else 0.0,
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


def pending_exrights_factor(con, codes, now) -> Dict[str, float]:
    """除权日实时口径修正因子（v7.0.32，Joel 2026-11-10 定案修复）。

    背景：实时价（原始口径）对比触发价（= 后复权档位价 ÷ 湖末 bar 因子）时，
    **除权日盘中湖末 bar 还是昨日（事件前因子）**——除权缺口未被吸收，触发价
    比真实除权后口径正好高一个分红幅度，与当日除权跌幅同幅 → 线近时分红一跌
    即假触线（假买入/加仓信号；当晚 19:30 湖同步后自愈）。

    对每股返回 r_cum = af_new/af_old：已生效（ex_date ≤ now）但 K 线尚未入湖
    （ex_date > 该股末 bar）的除权事件累计复权比。单事件（现实场景）精确：
    r = P_prev × (1 + stk_div/10) / (P_prev − cash_dps)，P_prev = 事件前末 bar
    原始收盘；多事件（湖多日未同步，罕见）顺序模拟、忽略事件间真实价漂移
    （近似，优于无修正）。无 pending 事件 / now=None / 无数据 → 1.0。

    纯只读两查询（kline_daily 末 bar + dividend_events）；lake 与 martingale
    零 import 约束不受影响（仅 duckdb API）。
    """
    out: Dict[str, float] = {str(c): 1.0 for c in codes}
    if not codes or now is None:
        return out
    now_s = str(now)[:10]
    fmt = ", ".join(["?"] * len(codes))
    # 每股末 bar（date + 原始收盘）
    last_bar: Dict[str, tuple] = {}
    con.execute(f"""
        SELECT k.ts_code, CAST(k.date AS VARCHAR) AS d, k.close FROM kline_daily k
        JOIN (SELECT ts_code, MAX(date) AS md FROM kline_daily
              WHERE ts_code IN ({fmt}) GROUP BY ts_code) m
          ON m.ts_code = k.ts_code AND m.md = k.date
        WHERE k.ts_code IN ({fmt}) AND k.close > 0""", list(codes) + list(codes))
    for c, d, cl in con.fetchall():
        last_bar[str(c)] = (str(d)[:10], float(cl))
    con.execute(f"""
        SELECT ts_code, ex_date, COALESCE(cash_dps, 0), COALESCE(stk_div, 0)
        FROM dividend_events
        WHERE ts_code IN ({fmt}) AND ex_date IS NOT NULL AND ex_date <= ?
        ORDER BY ts_code, ex_date""", list(codes) + [now_s])
    ev: Dict[str, list] = {}
    for c, d, cash, stk in con.fetchall():
        ev.setdefault(str(c), []).append((str(d)[:10], float(cash or 0), float(stk or 0)))
    for c in codes:
        cs = str(c)
        lb = last_bar.get(cs)
        if not lb or lb[1] <= 0:
            continue
        last_d, cur_price = lb
        r_cum = 1.0
        for d, cash, stk in ev.get(cs, ()):
            if d <= last_d:
                continue  # 已入湖（af 已更新）或非 pending
            if cur_price <= 0:
                break
            denom = cur_price - cash
            if denom <= 0:
                continue  # cash_dps ≥ 价：数据异常，跳过该事件
            p_ex = denom / (1.0 + stk / 10.0)
            r_cum *= cur_price / p_ex
            cur_price = p_ex
        out[cs] = r_cum
    return out


def state_rows(sp: StrategyParams, con, codes, end: Optional[str] = None,
               include_buys: bool = False, now=None) -> Dict[str, Dict[str, Any]]:
    """对 codes 逐股跑状态机 → {code: 持仓行}（Web /api/positions、
    /api/my_positions、/api/strategy_signals 与 scripts/signal_touch.py 共用——
    单一口径，防漂移；v7.0.20 由 web/app.py._strategy_state_rows 下沉至此）。

    end=YYYY-MM-DD 截窗口（含）；缺省跑到各股最新 bar。湖中无数据的代码不产出行。
    include_buys=True 时行内附 buys（历史触发逐笔；5 元组 (date,price,amount,
    k,first_of_round) → dict 列表）+ first_level_raw（非持仓股触发价口径）。
    now=参照日（date/datetime/str）：实时路径传当天——除权日盘中湖末 bar 还是
    事件前 K 线，原始口径触发价需按 pending 除权事件除以累计复权比（见
    pending_exrights_factor），否则除权缺口造成假触线；回测/回放路径不传
    （now=None → 零修正，f6 基线不受影响）。
    """
    names, data, divs = load(con, codes=list(codes), start=None, end=end)
    rows: Dict[str, Dict[str, Any]] = {}
    # 除权日实时口径修正（now=参照日；None → 回测/回放路径，零修正）
    rebase = pending_exrights_factor(con, codes, now) if now is not None else None
    for c in codes:
        series = data.get(c)
        if not series or len(series[0]) < 2:
            continue
        dates, sig, exec_c = series
        _stat, state, _v, _c, _f, _buys = run_stock(
            c, dates, sig, exec_c, divs.get(c), sp, verbose=False)
        rows[c] = {
            "ts_code": c,
            "name": names.get(c, ""),
            "in_pos": bool(state["in_pos"]),
            "shares": round(float(state["shares"]), 2),
            "avg_cost": round(float(state["avg_cost"]), 4),
            "last_price": float(state["last_price"]),
            "market_value": round(float(state["market_value"]), 2),
            "cash_div": round(float(state["cash_div"]), 2),
            "unreal_pct": round(float(state["unreal_pct"]) * 100.0, 2),
            "k_level": int(state["k_level"]),
            "entry_high": float(state["entry_high"]),
            "low_since_high": float(state["low_since_high"]),
            # 触发价原始口径（_raw）：引擎的 next_add/next_rearm 基于 hfq 信号价，
            # 而 last_price 是原始收盘——直接比较会因复权因子（分红股 F 可达数十倍）
            # 产生假信号。raw = hfq / F，F = sig[-1]/exec_c[-1]（最新 bar 复权因子）。
            "next_add_price": round(float(state["next_add_price"]), 4)
                if state["next_add_price"] is not None else None,
            "next_rearm_price": round(float(state["next_rearm_price"]), 4)
                if state["next_rearm_price"] is not None else None,
            "end_date": str(dates[-1])[:10],
        }
        f = float(sig[-1]) / float(exec_c[-1]) if float(exec_c[-1]) > 0 else 1.0
        # 除权日实时口径修正：末 bar 因子 × pending 事件累计复权比 → 触发价
        # 换算到除权后口径（realtime 原始价同口径；非除权日 r_cum=1 零影响）
        if rebase is not None:
            r_cum = rebase.get(str(c), 1.0)
            if r_cum > 1.0 + 1e-9:
                f = f * r_cum
                rows[c]["exrights_rebase"] = round(r_cum, 6)
        if rows[c]["next_add_price"] is not None:
            rows[c]["next_add_price_raw"] = round(rows[c]["next_add_price"] / f, 4)
        else:
            rows[c]["next_add_price_raw"] = None
        if rows[c]["next_rearm_price"] is not None:
            rows[c]["next_rearm_price_raw"] = round(rows[c]["next_rearm_price"] / f, 4)
        else:
            rows[c]["next_rearm_price_raw"] = None
        if include_buys:
            # 历史触发逐笔（策略状态机口径；date 截到日；k=档下标 0 起，
            # first_of_round=本轮首笔 → 前端渲染「首仓/第 N 档」；
            # lots=本笔手数 v7.0.28 手数口径）
            rows[c]["buys"] = [
                {"date": str(b[0])[:10], "price": b[1], "amount": b[2],
                 "level": b[3], "first_of_round": b[4], "lots": b[5]} for b in _buys
            ]
            # 首仓线（本周期 H×(1−first_drawdown) 的原始口径）：策略信号中心对
            # **非持仓**股用它作触发价——未持仓用户可执行的信号是首仓，而非
            # 模拟已买入的 next 档（模拟 in_pos/k 仅作参考展示）
            if float(state["entry_high"]) > 0:
                rows[c]["first_level_raw"] = round(
                    sp.level_price(state["entry_high"], 0) / f, 4)
            else:
                rows[c]["first_level_raw"] = None
    return rows
