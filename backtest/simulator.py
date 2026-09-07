# -*- coding: utf-8 -*-
"""月度调仓组合模拟器（调研报告 R2/R5 + TL 拍板 Q2）。

状态机（每个交易日调用一次 ``step``）：
1. **决策**：调仓日 T 收盘后用"截至 T"的 PIT 信息打分选 Top N → 目标权重
   （等权 1/N，由引擎层算好，经 ``submit_order`` 传入；本层只负责成交与净值）。
2. **成交（T+1）**：决策次日开盘执行。价格经 ``PriceSource.exec_price`` 读取
   （open 缺失时 close 兜底——缓存 99.6% 的股票无 open 列，报告披露）。
3. **成本**（全部来自 config，零硬编码）：
   - 佣金：双边 commission_bp，单笔下限 min_commission_cny（元）；
   - 印花税：**卖出单边**，按成交日日期分段 stamp_tax_sell[{from,to,bp}]
     （2023-08-28 起 0.05%，之前 0.1%——财政部 2023 年第 39 号公告）；
   - 过户费：双边 transfer_fee_bp；
   - 滑点：slippage_bp（买入抬价、卖出压价，作用于成交金额）。
4. **收益口径（方案 B）**：af1 后复权日收益已含分红。显式现金/股数记账是
   ``V_t = V_{t-1}·(1+Σwᵢrᵢ,ₜ)·(1−costs_t)`` 的精确形式（成本在成交时从现金
   扣除，无重复计息）。
5. **停牌**（R2）：目标股执行日 tradestatus=0 → 买入顺延至其下一可交易日
   （逐日重试，跨调仓期有效）；连续停牌 > max_defer_days → 本期放弃该股、
   权重转现金（on_timeout=drop_to_cash）。卖出/减仓同理顺延；超期按 ≤当日
   最后可成交价强制退出（长期停牌/退市整理期口径）。
6. **退市**：K线末根 < 决策日 → 按最后成交价退出，乘 (1−delisting_haircut_pct/100)；
   报告单列"含 N 只退市、合计拖累 X%"。

**PIT 安全**：估值只用"当日或更早"的价格（停牌股用 ≤当日 的最后一根收盘价
冻结估值，绝不用未来价）；换手率用决策日收盘权重计算（T 及之前的信息）。

设计为**纯状态机 + 依赖注入**：价格/停牌/退市全部经 ``PriceSource`` 协议读取
（生产用 backtest.data_pit.PitData；单测用合成数据源），便于已知答案测试。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional, Protocol, Tuple


class PriceSource(Protocol):
    """模拟器所需的最小数据接口（PitData 天然满足；单测可注入合成实现）。"""

    def exec_price(self, code: str, day: str, use_open: bool) -> Tuple[Optional[float], bool]: ...
    def last_bar_date(self, code: str) -> Optional[str]: ...
    def last_close_on_or_before(self, code: str, day: str) -> Optional[float]: ...
    def trade_calendar(self, start: date, end: date) -> List[str]: ...


@dataclass
class CostsConfig:
    """成本参数（全部来自 strategy.yaml backtest.costs，零硬编码）。

    单位约定：模拟器净值归一化（初始 V=1.0），费率类(bp)为无量纲比率直接作用；
    **min_commission_cny 是元** → 按 initial_capital_cny 折算成归一化下限
    （5 元/100 万 = 5e-6，与真实账户"单笔佣金最低 5 元"语义一致）。
    """

    commission_bp: float = 2.5            # 佣金 万2.5 双边
    min_commission_cny: float = 5.0       # 单笔佣金下限（元）
    initial_capital_cny: float = 1_000_000.0   # 初始资金（元；佣金下限折算用）
    stamp_tax_sell: List[Dict[str, object]] = field(default_factory=list)
    # [{from: "1900-01-01", to: "2023-08-27", bp: 10}, {from: "2023-08-28", ...}]
    transfer_fee_bp: float = 0.1          # 过户费 双边 0.001%
    slippage_bp: float = 5.0              # 滑点（买入抬价/卖出压价）
    delisting_haircut_pct: float = 0.0    # 退市退出价打折（默认 0，保留开关）

    @property
    def min_commission_norm(self) -> float:
        """佣金下限（归一化净值单位）= 元 / 初始资金。"""
        if self.initial_capital_cny <= 0:
            return 0.0
        return self.min_commission_cny / self.initial_capital_cny

    def stamp_tax_bp(self, day: str) -> float:
        """按成交日取印花税分段（bp）。无命中 → 0（配置缺失不静默扣费）。"""
        for seg in self.stamp_tax_sell:
            if str(seg.get("from", "")) <= day <= str(seg.get("to", "9999-12-31")):
                return float(seg.get("bp", 0.0))
        return 0.0


@dataclass
class SuspensionConfig:
    max_defer_days: int = 5               # 顺延上限（交易日）
    on_timeout: str = "drop_to_cash"      # 买入超时处置：drop_to_cash | hold


@dataclass
class _Deferred:
    """停牌顺延待办：value=None 卖全仓 / float 买或减仓金额；attempts=已尝试交易日数。"""

    value: Optional[float]
    attempts: int = 0


@dataclass
class RebalanceOrder:
    """一次调仓决策（T 日收盘后产生，次日执行）。"""

    decision_date: str                    # T（ISO）
    targets: Dict[str, float]             # code → 目标权重（和 <= 1，余数=现金）


@dataclass
class SimResult:
    """模拟输出。"""

    dates: List[str] = field(default_factory=list)           # 日频净值日期
    nav: List[float] = field(default_factory=list)           # 策略净值（初始=1.0）
    turnover: Dict[str, float] = field(default_factory=dict)  # 执行日 → 单边换手率
    cost_bps_cum: float = 0.0                                  # 累计成本（bp，展示用）
    n_rebalances: int = 0
    n_delisted_exits: int = 0
    delisting_drag_pct: float = 0.0       # 退市打折合计拖累（占当时净值 %，累加）
    n_drop_to_cash: int = 0               # 买入停牌超时/无法成交转现金次数
    open_fallback_days: Dict[str, int] = field(default_factory=dict)  # 执行日→open缺失兜底笔数
    rebalances: List[Dict[str, object]] = field(default_factory=list)
    # 每次调仓的成交价 {exec_day: {code: fill_px(af1)}}（entry_price 展示用）
    fills: Dict[str, Dict[str, float]] = field(default_factory=dict)


class PortfolioSimulator:
    """月度调仓状态机（现金 + 股数显式记账）。

    :param source: 价格源（PitData / 合成）。
    :param costs: 成本配置。
    :param suspension: 停牌规则。
    :param use_open: True=T+1 开盘成交（open 缺失自动 close 兜底）；
        False=T+1 收盘成交。
    """

    def __init__(self, source: PriceSource, costs: CostsConfig,
                 suspension: SuspensionConfig, use_open: bool = True) -> None:
        self.source = source
        self.costs = costs
        self.susp = suspension
        self.use_open = use_open

        # 状态（初始净值 1.0 全部现金）
        self._cash: float = 1.0
        self._shares: Dict[str, float] = {}
        self._mark_px: Dict[str, float] = {}      # 每持仓最后估值价（≤当日，停牌冻结用）
        self._pending: Optional[RebalanceOrder] = None
        self._exec_day: Optional[str] = None
        # 跨日待办（停牌顺延；逐日重试，超 max_defer_days 处置）
        self._defer_buy: Dict[str, _Deferred] = {}
        self._defer_sell: Dict[str, _Deferred] = {}
        self._last_nav: float = 1.0
        self._result = SimResult()

    @property
    def result(self) -> SimResult:
        return self._result

    # ------------------------------------------------------------------
    def submit_order(self, order: RebalanceOrder) -> None:
        """T 日收盘后提交调仓决策；执行日 = T 的下一交易日。"""
        d0 = date.fromisoformat(order.decision_date)
        cal = self.source.trade_calendar(d0 + timedelta(days=1), d0 + timedelta(days=60))
        if not cal:
            raise RuntimeError(f"调仓决策 {order.decision_date} 之后无交易日，无法执行")
        self._pending = order
        self._exec_day = cal[0]

    # ------------------------------------------------------------------
    def step(self, day: str) -> None:
        """推进一个交易日：先执行挂起调仓/顺延单（开盘价），再按收盘记净值。

        顺序说明：成交发生在 T+1 开盘，NAV(T+1) 用**成交后**持仓按 T+1 收盘
        估值 → 决策只用 T 及之前的信息（PIT 安全），无重叠、无前视。
        """
        if self._pending is not None and self._exec_day == day:
            self._execute(self._pending, day)
            self._pending = None
            self._exec_day = None
        # 顺延中的买入/卖出：逐日重试（跨调仓期有效）
        self._flush_deferred(day)

        nav = self._mark_to_market(day)
        self._result.dates.append(day)
        self._result.nav.append(nav)
        self._last_nav = nav

    # ------------------------------------------------------------------
    def _mark_px_of(self, code: str, day: str) -> Optional[float]:
        """估值价：当日可交易 → 收盘价；停牌/退市 → ≤当日 最后可得收盘价（冻结）。

        PIT 红线：绝不用 >day 的价格（停牌股若日后复牌，其末根K线在 day 之后，
        直接取"全历史末根"会引入未来价）。
        """
        px, _ = self.source.exec_price(code, day, False)
        if px is not None:
            return px
        return self.source.last_close_on_or_before(code, day)

    def _mark_to_market(self, day: str) -> float:
        total = self._cash
        for code, sh in self._shares.items():
            px = self._mark_px_of(code, day)
            if px is not None:
                self._mark_px[code] = px
            total += sh * (px or 0.0)
        return total

    # ------------------------------------------------------------------
    def _sell_cost(self, amount: float, day: str) -> Tuple[float, float]:
        """卖出成本：佣金(双边费率+下限) + 印花税(卖出单边,按日分段) + 过户费 + 滑点。

        :return: (净回款, 成本合计)
        """
        commission = max(amount * self.costs.commission_bp / 1e4,
                         self.costs.min_commission_norm)
        stamp = amount * self.costs.stamp_tax_bp(day) / 1e4
        transfer = amount * self.costs.transfer_fee_bp / 1e4
        slip = amount * self.costs.slippage_bp / 1e4
        total = commission + stamp + transfer + slip
        return amount - total, total

    def _buy_cost(self, amount: float) -> float:
        """买入成本：佣金(双边费率+下限) + 过户费 + 滑点（印花税仅卖出单边）。"""
        commission = max(amount * self.costs.commission_bp / 1e4,
                         self.costs.min_commission_norm)
        transfer = amount * self.costs.transfer_fee_bp / 1e4
        slip = amount * self.costs.slippage_bp / 1e4
        return commission + transfer + slip

    # ------------------------------------------------------------------
    def _do_buy(self, code: str, need: float, day: str, base: float) -> bool:
        """按当日价买入 need 元（含滑点/成本）；现金不足按比例缩减。"""
        px, used_open = self.source.exec_price(code, day, self.use_open)
        if px is None:
            return False
        if not used_open:
            self._result.open_fallback_days[day] = (
                self._result.open_fallback_days.get(day, 0) + 1)
        px_eff = px * (1.0 + self.costs.slippage_bp / 1e4)   # 滑点：买入抬价
        sh_buy = need / px_eff
        cost_amt = sh_buy * px_eff
        buy_cost = self._buy_cost(cost_amt)
        if self._cash < cost_amt + buy_cost:
            scale = (self._cash - buy_cost) / (cost_amt + buy_cost) if cost_amt > 0 else 0.0
            if scale <= 0.01:
                return False   # 现金不足 → 调用方处置（转现金）
            sh_buy *= scale
            cost_amt = sh_buy * px_eff
            buy_cost = self._buy_cost(cost_amt)
        self._cash -= (cost_amt + buy_cost)
        self._shares[code] = self._shares.get(code, 0.0) + sh_buy
        self._mark_px[code] = px
        self._result.fills.setdefault(day, {})[code] = px
        if base > 0:
            self._result.cost_bps_cum += buy_cost / base * 1e4
        return True

    def _do_sell_amount(self, code: str, amount: Optional[float], day: str,
                        force_px: Optional[float] = None) -> bool:
        """卖出：amount=None → 全部持仓；否则按金额减仓（等值股数）。停牌 → False。"""
        sh = self._shares.get(code, 0.0)
        if sh <= 0:
            return True
        px = force_px
        if px is None:
            px, _ = self.source.exec_price(code, day, self.use_open)
        if px is None:
            return False
        if amount is not None and amount < sh * px:
            # 减仓：卖等值股数（滑点压价作用于卖出成交）
            sh_sell = amount / (px * (1.0 - self.costs.slippage_bp / 1e4))
            sh_sell = min(sh_sell, sh)
        else:
            sh_sell = sh
        sale_amt = sh_sell * px
        proceeds, cost = self._sell_cost(sale_amt, day)
        self._cash += proceeds
        self._shares[code] = sh - sh_sell
        if self._shares[code] <= 1e-12:
            del self._shares[code]
            self._mark_px.pop(code, None)
        if base := (self._last_nav or 0.0):
            self._result.cost_bps_cum += cost / base * 1e4
        return True

    # ------------------------------------------------------------------
    def _execute(self, order: RebalanceOrder, day: str) -> None:
        """执行调仓：先卖后买；停牌顺延、退市退出。目标权重基于决策日收盘净值。"""
        base = self._last_nav                      # NAV(T)=决策日收盘（PIT 安全）
        targets = dict(order.targets)
        held = set(self._shares)

        # ---- 执行前权重（换手率用；**决策日 T** 的估值价/净值 → PIT 安全）----
        w_old: Dict[str, float] = {}
        if base > 0:
            for c in held:
                px = self._mark_px.get(c)
                if px:
                    w_old[c] = self._shares[c] * px / base

        def _delisted(code: str) -> bool:
            last_d = self.source.last_bar_date(code)
            return last_d is not None and last_d < order.decision_date

        # ---- 1) 卖出：不在目标中的持仓（含决策日已退市者，无论是否在目标中）----
        for code in sorted(held):
            if _delisted(code):
                # 退市：最后成交价退出（可配打折 haircut → 实际回款按折后价计）
                px_last = self.source.last_close_on_or_before(code, order.decision_date)
                sh = self._shares.get(code, 0.0)
                if px_last is None or sh <= 0:
                    continue
                exit_px = px_last * (1.0 - self.costs.delisting_haircut_pct / 100.0)
                amount = sh * exit_px
                proceeds, _cost = self._sell_cost(amount, day)
                haircut_loss = sh * px_last * self.costs.delisting_haircut_pct / 100.0
                self._cash += proceeds
                del self._shares[code]
                self._mark_px.pop(code, None)
                self._result.n_delisted_exits += 1
                if base > 0:
                    self._result.delisting_drag_pct += haircut_loss / base * 100.0
                continue
            if code not in targets:
                if not self._do_sell_amount(code, None, day):
                    # 停牌卖不掉 → 顺延（超期强制退出在 _flush_deferred）
                    self._defer_sell[code] = _Deferred(value=None, attempts=1)

        # ---- 2) 减仓：仍在目标中但权重下降的持仓 ----
        for code in sorted(held & set(targets)):
            if _delisted(code):
                continue   # 上面已处理
            px_cur, _ = self.source.exec_price(code, day, False)
            cur_px = px_cur if px_cur is not None else self._mark_px.get(code, 0.0)
            cur_value = self._shares.get(code, 0.0) * (cur_px or 0.0)
            target_value = targets[code] * base
            trim = cur_value - target_value
            if trim > 1e-9:
                if not self._do_sell_amount(code, trim, day):
                    self._defer_sell[code] = _Deferred(value=trim, attempts=1)

        # ---- 3) 买入：目标中权重上升/新增的股票 ----
        for code in sorted(targets):
            target_value = targets[code] * base
            px_cur, _ = self.source.exec_price(code, day, False)
            cur_px = px_cur if px_cur is not None else self._mark_px.get(code, 0.0)
            cur_value = self._shares.get(code, 0.0) * (cur_px or 0.0)
            need = target_value - cur_value
            if need <= 1e-9:
                continue
            if _delisted(code):
                # 决策日已退市 → 无法买入，权重转现金
                self._result.n_drop_to_cash += 1
                continue
            if not self._do_buy(code, need, day, base):
                # 停牌/现金不足 → 顺延（逐日重试；超 max_defer_days 处置）
                self._defer_buy[code] = _Deferred(value=need, attempts=1)

        # ---- 4) 换手率：Σ|w_new − w_old|/2（单边；w_old 用决策日收盘权重）----
        turnover = 0.0
        for c in set(targets) | set(w_old):
            turnover += abs(targets.get(c, 0.0) - w_old.get(c, 0.0))
        self._result.turnover[day] = turnover / 2.0

        self._result.n_rebalances += 1
        self._result.rebalances.append({
            "decision_date": order.decision_date, "exec_day": day,
            "targets": dict(targets),
        })

    # ------------------------------------------------------------------
    def _flush_deferred(self, day: str) -> None:
        """逐日重试顺延中的买入/卖出（停牌恢复 → 尽快成交；超期按规则处置）。"""
        base = self._last_nav if self._last_nav > 0 else 1.0

        # ---- 待买 ----
        for code in list(self._defer_buy):
            info = self._defer_buy[code]
            last_d = self.source.last_bar_date(code)
            if last_d is not None and last_d < day:
                # 顺延期间退市 → 放弃，权重转现金
                del self._defer_buy[code]
                self._result.n_drop_to_cash += 1
                continue
            if info.value is None:
                del self._defer_buy[code]   # 防御：待买必有金额
                continue
            if self._do_buy(code, info.value, day, base):
                del self._defer_buy[code]
                continue
            info.attempts += 1
            if info.attempts > self.susp.max_defer_days \
                    and self.susp.on_timeout == "drop_to_cash":
                del self._defer_buy[code]
                self._result.n_drop_to_cash += 1

        # ---- 待卖（全仓 or 减仓）----
        for code in list(self._defer_sell):
            info = self._defer_sell[code]
            if self._do_sell_amount(code, info.value, day):
                del self._defer_sell[code]
                continue
            info.attempts += 1
            if info.attempts > self.susp.max_defer_days:
                # 长期停牌 → 按 ≤当日 最后可成交价强制退出（R2：退市整理期口径）
                px_last = self.source.last_close_on_or_before(code, day)
                if px_last is None:
                    continue
                if self._do_sell_amount(code, info.value, day, force_px=px_last):
                    del self._defer_sell[code]
