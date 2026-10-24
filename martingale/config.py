# -*- coding: utf-8 -*-
"""martingale.config —— 策略参数加载与校验（v7 slim schema）。

所有策略阈值来自 config/strategy.yaml 的 ``strategy:`` / ``universe:`` 两段——
代码里不得出现任何硬编码策略阈值。

单位约定（brief §4 定案）：
- 带 ``_pct`` 的字段在 yaml 里存**整数百分数**（15 = 15%）；本模块统一转**小数**
  （0.15）供引擎比较。Web 编辑/展示一律用百分数口径（与 yaml 一致）。
- 金额字段单位 = 元。

schema（定案，勿增删）：
- strategy: name / initial_amount / base_add_amount / first_drawdown_pct /
  add_step_pct / growth_ratio / rearm_mode(bounce|newhigh) / rearm_pct /
  accounting(固定 cash，只读)
- universe: industry_whitelist_csric2 / soe_keywords / min_total_mv_yi /
  min_consecutive_div_years / listing_min_trading_days
  （SOE 恒强制，不设开关——TL 拍板；全链 5 条件保留）

成本参数（佣金/印花税）不在 schema 内：引擎常量（见 engine.py 头部注释，
TL 拍板：不进配置、不进 Web；3bps 买入佣金精确对齐 f6 已验收基线）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

import yaml

# 策略名（固定值；Web 只读展示）
STRATEGY_NAME = "drawdown_martingale"

# 会计口径（固定 cash；brief §4：不暴露给用户改）
ACCOUNTING_CASH = "cash"

# rearm 模式（brief §3：bounce 默认 / newhigh）
REARM_MODES = ("bounce", "newhigh")

# 佣金/印花税（引擎常量；f6 基线含 3bps 买入成本——勿改，改则 f6 回归必炸）
COMMISSION_BPS = 3.0   # 佣金（买入）3bps
STAMP_BPS = 5.0        # 印花税（卖出）5bps——本策略不卖出 → no-op


class ConfigError(ValueError):
    """配置缺失或非法。"""


@dataclass(frozen=True)
class StrategyParams:
    """策略参数（pct 字段已转小数；金额单位元）。"""
    name: str                      # drawdown_martingale（固定）
    initial_amount: float          # 初始投入金额：一次性首仓（占用 k=0 档，每股仅第一轮首买用）
    base_add_amount: float         # 加仓基准金额：每轮首档（k=0）；第 k 档 = 基准×growth^k
    first_drawdown: float          # 首次回撤比例（小数）：hfq ≤ H×(1−dd) 建仓
    add_step: float                # 加仓步长（小数）：档线 H×(1−dd−k×step)
    growth_ratio: float            # 加仓提升比率：第 k 档 = 基准×比率^k
    rearm_mode: str                # bounce | newhigh
    rearm_pct: float               # 高点刷新比例（小数；bounce 模式生效）
    accounting: str                # 固定 "cash"（只读）
    # 成本常量（引擎常量，非配置项；见模块头注释）
    commission: float = field(default=COMMISSION_BPS / 1e4, compare=False)
    stamp: float = field(default=STAMP_BPS / 1e4, compare=False)

    def level_price(self, entry_high: float, k: int) -> float:
        """第 k 档买入线：H×(1−first_drawdown−k×add_step)（k=0 为首建仓位）。"""
        return entry_high * (1.0 - self.first_drawdown - k * self.add_step)

    def next_add_price(self, entry_high: float, k_level: int) -> float:
        """当前状态下的**下一个**触发价位 = 下一档（k_level 尚未触发的那档）。

        引擎里 k_level = 当前周期已买入档数（每买一档 +1）→ 下一档下标 = k_level。
        等价于 brief 的 H×(1−dd−(k+1)×step)（k = 已买档序号，0 起）。
        """
        return self.level_price(entry_high, k_level)

    def next_rearm_price(self, low_since_high: float) -> float:
        """bounce 模式下再武装触发价：L×(1+rearm_pct)（newhigh 模式无此语义）。"""
        return low_since_high * (1.0 + self.rearm_pct)


@dataclass(frozen=True)
class UniverseParams:
    """股票池参数（全链 5 条件 + v7.0.12 候选池选股门槛）。"""
    industry_whitelist_csric2: Tuple[str, ...]   # 证监会二级行业白名单（码前缀，如 B06）
    soe_keywords: Tuple[str, ...]                # 央国企识别关键词（双规则之二）
    min_total_mv_yi: float                       # 总市值下限（亿元，最新估值日）
    min_consecutive_div_years: int               # 连续分红年数下限（新股规则见 universe.py）
    listing_min_trading_days: int                # 上市满 N 个交易日（K线行数）
    # —— v7.0.12 候选池选股门槛（Joel 2026-10-24 拍板；波动率门槛经讨论明确不加）——
    # 默认值 = 生产 config/strategy.yaml；直接构造（测试等）缺省时行为与生产一致。
    backtest_positive: bool = True                 # ⑦ 十年回测必须为正（硬门槛开关）
    backtest_window_years: int = 10                # ⑦ 回测窗口年数（滚动，截至最新 K线日）
    min_ttm_yield_pct: float = 3.0                 # ⑧ TTM 股息率下限（%）
    price_min_yuan: float = 3.0                    # ⑨ 股价区间下限（元；远离退市风险区）
    price_max_yuan: float = 100.0                  # ⑨ 股价区间上限（元；手数粒度约束）


def _require(cfg: Dict[str, Any], dotted: str) -> Any:
    cur: Any = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise ConfigError(f"strategy.yaml 缺少配置项: {dotted}")
        cur = cur[part]
    return cur


def _num(v: Any, what: str, lo: float, hi: float, allow_int: bool = True) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ConfigError(f"{what} 必须是数值（当前 {v!r}）")
    f = float(v)
    if not (lo <= f <= hi):
        raise ConfigError(f"{what}={v} 超出范围 [{lo},{hi}]")
    return f


def _int(v: Any, what: str, lo: int, hi: int) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise ConfigError(f"{what} 必须是整数（当前 {v!r}）")
    if not (lo <= v <= hi):
        raise ConfigError(f"{what}={v} 超出范围 [{lo},{hi}]")
    return v


def _str_list(v: Any, what: str, non_empty: bool = True) -> Tuple[str, ...]:
    if not isinstance(v, list) or (non_empty and not v):
        raise ConfigError(f"{what} 必须是{'非空' if non_empty else ''}字符串数组（当前 {v!r}）")
    for x in v:
        if not isinstance(x, str) or not x.strip():
            raise ConfigError(f"{what} 项非法: {x!r}（应是非空字符串）")
    return tuple(x.strip() for x in v)


def _strategy_section(cfg: Dict[str, Any]) -> StrategyParams:
    s = cfg.get("strategy")
    if not isinstance(s, dict):
        raise ConfigError("strategy.yaml 缺少 strategy 段")
    for key in ("name", "initial_amount", "base_add_amount", "first_drawdown_pct",
                "add_step_pct", "growth_ratio", "rearm_mode", "rearm_pct", "accounting"):
        if key not in s:
            raise ConfigError(f"strategy.{key} 缺失")
    name = str(s["name"])
    if name != STRATEGY_NAME:
        raise ConfigError(f"strategy.name 目前只支持 {STRATEGY_NAME!r}（当前 {name!r}）")
    initial = _num(s["initial_amount"], "strategy.initial_amount", 1, 1e9)
    base = _num(s["base_add_amount"], "strategy.base_add_amount", 1, 1e9)
    dd = _num(s["first_drawdown_pct"], "strategy.first_drawdown_pct", 1, 90) / 100.0
    step = _num(s["add_step_pct"], "strategy.add_step_pct", 0.1, 50) / 100.0
    growth = _num(s["growth_ratio"], "strategy.growth_ratio", 1.0, 10)
    mode = str(s["rearm_mode"])
    if mode not in REARM_MODES:
        raise ConfigError(f"strategy.rearm_mode 只能是 {'/'.join(REARM_MODES)}（当前 {mode!r}）")
    rearm_pct = _num(s["rearm_pct"], "strategy.rearm_pct", 1, 100) / 100.0
    accounting = str(s["accounting"])
    # accounting 固定 cash（brief §4 只读；Web PUT 传其他值会被 _validate_strategy 400 拦截）
    if accounting != ACCOUNTING_CASH:
        raise ConfigError(f"strategy.accounting 固定为 {ACCOUNTING_CASH!r}（当前 {accounting!r}）")
    if dd + step >= 1.0:
        raise ConfigError("first_drawdown_pct + add_step_pct 必须 < 100")
    return StrategyParams(
        name=name, initial_amount=initial, base_add_amount=base,
        first_drawdown=dd, add_step=step, growth_ratio=growth,
        rearm_mode=mode, rearm_pct=rearm_pct, accounting=accounting,
    )


def _universe_section(cfg: Dict[str, Any]) -> UniverseParams:
    u = cfg.get("universe")
    if not isinstance(u, dict):
        raise ConfigError("strategy.yaml 缺少 universe 段")
    for key in ("industry_whitelist_csric2", "soe_keywords", "min_total_mv_yi",
                "min_consecutive_div_years", "listing_min_trading_days",
                "backtest_positive", "backtest_window_years", "min_ttm_yield_pct",
                "price_min_yuan", "price_max_yuan"):
        if key not in u:
            raise ConfigError(f"universe.{key} 缺失")
    wl = _str_list(u["industry_whitelist_csric2"], "universe.industry_whitelist_csric2")
    for code in wl:
        if not (len(code) == 3 and code[0].isalpha() and code[0].isupper()
                and code[1:3].isdigit()):
            raise ConfigError(f"universe.industry_whitelist_csric2 项非法: {code!r}（应形如 B06）")
    kws = _str_list(u["soe_keywords"], "universe.soe_keywords")
    mv = _num(u["min_total_mv_yi"], "universe.min_total_mv_yi", 1, 1e5)
    divy = _int(u["min_consecutive_div_years"], "universe.min_consecutive_div_years", 1, 20)
    listing = _int(u["listing_min_trading_days"], "universe.listing_min_trading_days", 20, 1200)
    # —— v7.0.12 候选池选股门槛 ——
    bt_pos = u["backtest_positive"]
    if not isinstance(bt_pos, bool):
        raise ConfigError(f"universe.backtest_positive 必须是布尔（当前 {bt_pos!r}）")
    bt_years = _int(u["backtest_window_years"], "universe.backtest_window_years", 1, 30)
    min_yield = _num(u["min_ttm_yield_pct"], "universe.min_ttm_yield_pct", 0, 100)
    pmin = _num(u["price_min_yuan"], "universe.price_min_yuan", 0, 1e4)
    pmax = _num(u["price_max_yuan"], "universe.price_max_yuan", 0, 1e5)
    if pmin > pmax:
        raise ConfigError(
            f"universe.price_min_yuan({pmin:g}) 不得大于 price_max_yuan({pmax:g})")
    return UniverseParams(
        industry_whitelist_csric2=wl, soe_keywords=kws,
        min_total_mv_yi=mv, min_consecutive_div_years=divy,
        listing_min_trading_days=listing,
        backtest_positive=bt_pos, backtest_window_years=bt_years,
        min_ttm_yield_pct=min_yield, price_min_yuan=pmin, price_max_yuan=pmax,
    )


def load_config(path: str) -> Tuple[StrategyParams, UniverseParams]:
    """读取 strategy.yaml，返回 (StrategyParams, UniverseParams)。

    缺段/缺键/越界 → ConfigError（避免运行到一半才发现）。
    ``lake:`` / ``datasource:`` 段不属于本包（lake 与 lake-sync 专用），存在与否
    均不影响本函数（screener.config.load_config 与 lake.config.lake_cfg 各自读取）。
    """
    if not os.path.exists(path):
        raise ConfigError(f"配置文件不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if not isinstance(cfg, dict):
        raise ConfigError("strategy.yaml 顶层必须是映射(mapping)")
    return _strategy_section(cfg), _universe_section(cfg)
