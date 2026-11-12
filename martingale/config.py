# -*- coding: utf-8 -*-
"""martingale.config —— 策略参数加载与校验（v7 slim schema）。

所有策略阈值来自 config/strategy.yaml 的 ``strategy:`` / ``universe:`` 两段——
代码里不得出现任何硬编码策略阈值。

单位约定（brief §4 定案）：
- 带 ``_pct`` 的字段在 yaml 里存**整数百分数**（15 = 15%）；本模块统一转**小数**
  （0.15）供引擎比较。Web 编辑/展示一律用百分数口径（与 yaml 一致）。
- 金额字段单位 = 元。

schema（定案，勿增删）：
- strategy: name / lot_size / first_lots / add_lots_step /
  add_step_pct / rearm_mode(bounce|newhigh) / rearm_pct /
  accounting(固定 cash，只读)
  （v7.0.28：仓位口径切手数 C 方案（Joel 2026-11-09 拍板）——initial_amount /
  base_add_amount / growth_ratio 删除，第 k 档买 first_lots + k×add_lots_step 手）
- universe: industry_whitelist_csric2 / soe_keywords / min_total_mv_yi /
  min_consecutive_div_years / listing_min_trading_days
  （SOE 恒强制，不设开关——TL 拍板；全链 5 条件保留）
  + v7.0.12 门槛：backtest_positive / backtest_window_years /
  backtest_min_return_pct（**可选键**，缺省 20.0，v7.0.39）/ min_ttm_yield_pct /
  price_min_yuan / price_max_yuan
- timing: price_window_years / price_pctile_max / first_drawdown_pct
  （v7.0.13 选股时机；v7.0.26：② 股息率 / ③ 250 日线删除、首仓建仓回撤
  first_drawdown_pct 自 strategy 段移入；**段可缺省**——策略库旧快照
  无此段，缺省 = 生产默认值且 first_drawdown_pct 回退读 strategy 段；
  load_config 返回 triple）

成本参数（佣金/印花税）不在 schema 内：引擎常量（见 engine.py 头部注释，
TL 拍板：不进配置、不进 Web；3bps 买入佣金精确对齐 f6 已验收基线）。
"""
from __future__ import annotations

import os
import re
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
    """策略参数（pct 字段已转小数）。

    仓位口径 = 手数（v7.0.28 C 方案）：第 k 档买 (first_lots + k×add_lots_step) 手，
    1 手 = lot_size 股。不再使用金额 sizing（initial/base_add/growth_ratio 已删）。
    """
    name: str                      # drawdown_martingale（固定）
    lot_size: int                  # 1 手 = 多少股（A 股 100）
    first_lots: int                # 首仓手数（每轮首档 k=0 买这么多手）
    first_drawdown: float          # 首仓建仓回撤（小数）：hfq ≤ H×(1−dd) 触发首仓（k=0）。
                                   # v7.0.26：yaml 键 first_drawdown_pct 居 timing 段
                                   # （首仓属选股时机）；本字段留在 StrategyParams 供
                                   # 引擎/档线计算使用（数学零改动，f6 基线不变）
    add_step: float                # 加仓步长（小数）：档线 H×(1−dd−k×step)
    add_lots_step: int             # 加仓手数增量：第 k 档 = first_lots + k×add_lots_step 手
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
    backtest_positive: bool = True                 # ⑦ 十年回测门槛开关
    backtest_window_years: int = 10                # ⑦ 回测窗口年数（滚动，截至最新 K线日）
    # ⑦ 十年回测净收益率下限（%，百分数口径；2026-11-10 Joel 定案默认 20——原
    # 「必须为正」= 0% 门槛的放宽版，过滤收益不高的股票）。仅在
    # backtest_positive 开启时生效；老 yaml 缺键 → 默认 20.0（可选键，v7.0.30 先例）。
    backtest_min_return_pct: float = 20.0
    min_ttm_yield_pct: float = 3.0                 # ⑧ TTM 股息率下限（%）
    price_min_yuan: float = 3.0                    # ⑨ 股价区间下限（元；远离退市风险区）
    price_max_yuan: float = 100.0                  # ⑨ 股价区间上限（元；手数粒度约束）
    # —— v8.0.8 国企控股强度门（Joel 2026-11-11：行业全放开后「top10 任一国有股东」
    # 判定过宽——美的/青年服饰 靠证金公司 1.88%/5.58% 这类财务性小持股进池）——
    # 数据口径（holders_snapshot 最新 as_of、top10、share_nature='国有股'）：
    # 最大单一国有占比 ≥ min_soe_ratio_pct **或** top10 国有合计 ≥
    # min_soe_sum_ratio_pct → 通过控股强度门（合计档覆盖多平台持股型地方国企：
    # 江苏银行单一 6.98% 但合计 35.51%）。两键均 0/缺省 = 门关闭（回退 v8.0.0
    # 行为，向后兼容）。8/30 定值依据：2026-11-11 湖数据实测——伪国企（民营
    # 控股）最大国有占比全部 ≤5.58%，真国企全部 ≥8.21%，5.6~8.2 为数据自然
    # 断层（详见 strategy.yaml 注释）。
    min_soe_ratio_pct: float = 0.0
    # top10 国有合计占比下限（%）：合计档覆盖多平台持股型地方国企
    # （江苏银行单一 6.98% 但合计 35.51%）。与 min_soe_ratio_pct 为**或**关系。
    min_soe_sum_ratio_pct: float = 0.0
    # 控股强度门之上的硬剔除名单（ts_code 列表，格式同白名单校验）。现状：
    # sh.603766 隆鑫通用——最大国有渝富 15.52% 过门，但第一大股东宗申新智
    # 24.55% 民营控股，实际控股=民营 → 伪国企。
    exclude_codes: Tuple[str, ...] = ()
    # 控股强度门豁免（code→理由）：湖数据 top10 标注缺口（实际控股股东为国企
    # 但 nature 标「境内法人/境外法人」）导致误伤时按股豁免，值写明实际控股
    # 股东留审计痕迹；holders 数据刷新后复核可摘除项。
    soe_control_overrides: Dict[str, str] = field(default_factory=dict)
    # —— v8.1.1 央企收紧门（Joel 2026-11-12：行业风控复杂机制搁置，央国企
    # 资质作兜底前置筛选；真央国企 vs 地方国企 vs 国资参股的区分）——
    # true = 池在 SOE 门之上再要求「最大国有股东（≥ 控股强度门槛）为央企」
    # （central_soe.CENTRAL_TOKENS 匹配，口径见该模块 docstring）。
    # 默认 false = 与 v8.0.8 行为完全一致（老 yaml 缺键 → false）。
    central_only: bool = False


@dataclass(frozen=True)
class TimingParams:
    """选股时机参数（v7.0.13 引入；v7.0.24 定案：时机门 = ① 单条件）。

    作用域 = 候选池成员（不是全市场、不是基础池）。
    时机门（页面"今日选股"观察名单）= ① 价格分位 ≤ price_pctile_max%。
    v7.0.25 起时机无独立微信推送——微信统一为「进入当前信号才推」
    （价破线，盘中策略点位提醒）。
    v7.0.26：② 股息率 / ③ 250 日线参数**彻底删除**（回测定案无用，不留
    观察项）；首仓建仓回撤（first_drawdown_pct）自 strategy 段移入 timing
    段管理（首仓 = 选股时机；引擎经 StrategyParams.first_drawdown 读取，
    数学零改动）。
    v7.0.30：新增 web 侧两参数（approach_pct 显示带 / spot_preselect_pct
    实时取价预筛带），状态机/回测/微信推送均不消费。
    """
    price_window_years: int = 5        # ① 价格低位窗口（年，滚动至最新 K线日）
    price_pctile_max: float = 40.0     # ① 当前价处于近 N 年收盘分位 ≤ P%（时机门唯一条件）
    # 「即将进入信号区」显示带（百分数，非小数）：纯 web 展示口径，状态机/
    # 回测/微信推送均不消费。v7.0.30 起从 yaml timing.approach_pct 读取
    # （缺键默认 5.0；老 yaml 兼容）。
    approach_pct: float = 5.0
    # 实时取价预筛带（百分数，非小数）：web 信号页只对「持仓 ∪ 湖最后收盘
    # 距触发线 ≤ 本值%」取腾讯实时价（v7.0.30 Joel 定案 10——主板跌停制度
    # 下 >10% 当天不可能触线）。纯 web 取价口径；微信推送脚本仍取全量。
    # 缺键默认 10.0（老 yaml 兼容）。
    spot_preselect_pct: float = 10.0


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
    for key in ("name", "lot_size", "first_lots", "add_lots_step",
                "add_step_pct", "rearm_mode", "rearm_pct", "accounting"):
        if key not in s:
            raise ConfigError(f"strategy.{key} 缺失")
    name = str(s["name"])
    if name != STRATEGY_NAME:
        raise ConfigError(f"strategy.name 目前只支持 {STRATEGY_NAME!r}（当前 {name!r}）")
    lot_size = _int(s["lot_size"], "strategy.lot_size", 1, 10000)
    first_lots = _int(s["first_lots"], "strategy.first_lots", 1, 10000)
    # 首仓建仓回撤：v7.0.26 起居 timing 段（首仓 = 选股时机参数；页面归「选股策略」）。
    # 兼容旧布局（策略库快照/老 yaml 无 timing 段）：回退读 strategy.first_drawdown_pct。
    t = cfg.get("timing")
    if isinstance(t, dict) and "first_drawdown_pct" in t:
        dd = _num(t["first_drawdown_pct"], "timing.first_drawdown_pct", 1, 90) / 100.0
    elif "first_drawdown_pct" in s:
        dd = _num(s["first_drawdown_pct"], "strategy.first_drawdown_pct", 1, 90) / 100.0
    else:
        raise ConfigError(
            "首仓建仓回撤缺失：应配置 timing.first_drawdown_pct"
            "（旧布局 strategy.first_drawdown_pct 仅兼容无 timing 段的快照）")
    step = _num(s["add_step_pct"], "strategy.add_step_pct", 0.1, 50) / 100.0
    add_lots_step = _int(s["add_lots_step"], "strategy.add_lots_step", 0, 10000)
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
        name=name, lot_size=lot_size, first_lots=first_lots,
        first_drawdown=dd, add_step=step, add_lots_step=add_lots_step,
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
    if wl != ("*",):
        # v8.0.8：["*"] = 行业白名单全放开（无行业过滤）；其余项须形如 B06。
        for code in wl:
            if not (len(code) == 3 and code[0].isalpha() and code[0].isupper()
                    and code[1:3].isdigit()):
                raise ConfigError(f"universe.industry_whitelist_csric2 项非法: {code!r}"
                                  "（应形如 B06；全放开用 [\"*\"]）")
    kws = _str_list(u["soe_keywords"], "universe.soe_keywords")
    mv = _num(u["min_total_mv_yi"], "universe.min_total_mv_yi", 1, 1e5)
    divy = _int(u["min_consecutive_div_years"], "universe.min_consecutive_div_years", 1, 20)
    listing = _int(u["listing_min_trading_days"], "universe.listing_min_trading_days", 20, 1200)
    # —— v7.0.12 候选池选股门槛 ——
    bt_pos = u["backtest_positive"]
    if not isinstance(bt_pos, bool):
        raise ConfigError(f"universe.backtest_positive 必须是布尔（当前 {bt_pos!r}）")
    bt_years = _int(u["backtest_window_years"], "universe.backtest_window_years", 1, 30)
    # backtest_min_return_pct：v7.0.39 新增，**可选键**（老 yaml/策略库历史快照
    # 无此键 → 默认 20.0；生产 yaml 已显式写入 20）。百分数口径（与
    # min_ttm_yield_pct 同，不做小数转换）；0 = 恢复原「必须为正」语义。
    bt_min = _num(u.get("backtest_min_return_pct", 20.0),
                  "universe.backtest_min_return_pct", 0, 1e3)
    min_yield = _num(u["min_ttm_yield_pct"], "universe.min_ttm_yield_pct", 0, 100)
    pmin = _num(u["price_min_yuan"], "universe.price_min_yuan", 0, 1e4)
    pmax = _num(u["price_max_yuan"], "universe.price_max_yuan", 0, 1e5)
    if pmin > pmax:
        raise ConfigError(
            f"universe.price_min_yuan({pmin:g}) 不得大于 price_max_yuan({pmax:g})")
    # —— v8.0.8 国企控股强度门（**可选键**：老 yaml 缺键 → 全 0/空 = 门关闭）——
    soe_ratio = _num(u.get("min_soe_ratio_pct", 0.0),
                     "universe.min_soe_ratio_pct", 0, 100)
    soe_sum = _num(u.get("min_soe_sum_ratio_pct", 0.0),
                   "universe.min_soe_sum_ratio_pct", 0, 100)
    excl = _str_list(u.get("exclude_codes", []), "universe.exclude_codes", non_empty=False)
    for code in excl:
        if not re.match(r"^(sh|sz|bj)\.\d{6}$", code):
            raise ConfigError(
                f"universe.exclude_codes 项非法: {code!r}（格式应为 sh.600519）")
    ov_raw = u.get("soe_control_overrides", {})
    if not isinstance(ov_raw, dict):
        raise ConfigError(f"universe.soe_control_overrides 必须是映射（当前 {ov_raw!r}）")
    overrides: Dict[str, str] = {}
    for k, v in ov_raw.items():
        if not re.match(r"^(sh|sz|bj)\.\d{6}$", str(k)):
            raise ConfigError(
                f"universe.soe_control_overrides 键非法: {k!r}（格式应为 sh.600519）")
        overrides[str(k)] = str(v)
    # —— v8.1.1 央企收紧门（**可选键**：老 yaml 缺键 → false = 门关闭）——
    central_only_raw = u.get("central_only", False)
    if not isinstance(central_only_raw, bool):
        raise ConfigError(
            f"universe.central_only 必须是布尔（当前 {central_only_raw!r}）")
    return UniverseParams(
        industry_whitelist_csric2=wl, soe_keywords=kws,
        min_total_mv_yi=mv, min_consecutive_div_years=divy,
        listing_min_trading_days=listing,
        backtest_positive=bt_pos, backtest_window_years=bt_years,
        backtest_min_return_pct=bt_min,
        min_ttm_yield_pct=min_yield, price_min_yuan=pmin, price_max_yuan=pmax,
        min_soe_ratio_pct=soe_ratio, min_soe_sum_ratio_pct=soe_sum,
        exclude_codes=excl, soe_control_overrides=overrides,
        central_only=central_only_raw,
    )


def _timing_section(cfg: Dict[str, Any]) -> TimingParams:
    """timing 段解析。**段可缺省**（策略库旧快照/老 yaml 无此段 → 生产默认值；
    此时首仓回撤回退读 strategy 段）；段存在时 3 键必须齐全且过范围校验。
    v7.0.26：② 股息率 / ③ 250 日线参数删除（回测定案无用）；first_drawdown_pct
    自 strategy 段移入（首仓建仓回撤 = 选股时机参数）。"""
    t = cfg.get("timing")
    if t is None:
        return TimingParams()
    if not isinstance(t, dict):
        raise ConfigError("strategy.yaml timing 段必须是映射(mapping)")
    for key in ("price_window_years", "price_pctile_max", "first_drawdown_pct"):
        if key not in t:
            raise ConfigError(f"timing.{key} 缺失")
    years = _int(t["price_window_years"], "timing.price_window_years", 1, 30)
    pct = _num(t["price_pctile_max"], "timing.price_pctile_max", 1, 100)
    # approach_pct：v7.0.30 新增，**可选键**（老 yaml/策略库历史快照无此键 →
    # 默认 5.0；POST /api/strategy/import 的裸 yaml 因此不被强制升级）
    approach = _num(t.get("approach_pct", 5.0), "timing.approach_pct", 0, 100)
    # spot_preselect_pct：v7.0.30 新增，**可选键**（默认 10.0，同上兼容逻辑）
    preselect = _num(t.get("spot_preselect_pct", 10.0),
                     "timing.spot_preselect_pct", 0, 100)
    return TimingParams(price_window_years=years, price_pctile_max=pct,
                        approach_pct=approach, spot_preselect_pct=preselect)


def load_config(path: str) -> Tuple[StrategyParams, UniverseParams, TimingParams]:
    """读取 strategy.yaml，返回 (StrategyParams, UniverseParams, TimingParams)。

    缺段/缺键/越界 → ConfigError（避免运行到一半才发现）；timing 段例外可缺省。
    ``lake:`` / ``datasource:`` 段不属于本包（lake 与 lake-sync 专用），存在与否
    均不影响本函数（screener.config.load_config 与 lake.config.lake_cfg 各自读取）。
    """
    if not os.path.exists(path):
        raise ConfigError(f"配置文件不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if not isinstance(cfg, dict):
        raise ConfigError("strategy.yaml 顶层必须是映射(mapping)")
    # timing 段校验先行（段存在时 3 键齐全——含 first_drawdown_pct；缺键报
    # timing.* 而非被 strategy 段的 dd 缺失兜底消息抢先）
    t = _timing_section(cfg)
    return _strategy_section(cfg), _universe_section(cfg), t
