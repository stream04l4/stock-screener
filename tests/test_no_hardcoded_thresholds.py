# -*- coding: utf-8 -*-
"""纪律检查（v7）：策略/池阈值不得硬编码，必须来自 config/strategy.yaml。

v6 扫描 screener/ 旧多因子阈值；v7 对象换成新包：
1. 扫描 ``martingale/`` 全部 .py，找"v7 阈值关键词 = 字面量数字"的可疑组合
   （策略 8 字段 + universe 5 键）。佣金/印花税是 TL 拍板的**引擎常量**
   （comm=3bps / stamp=5bps），不在检查范围（它们本就不进 schema）。
2. ``config/strategy.yaml`` 必须含 v7 schema 全部可配置项（strategy 9 键含
   name / universe 5 键 / lake / datasource.{sina,rf,baostock}）。
"""
from __future__ import annotations

import os
import re

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(PROJECT, "martingale")

# v7 阈值关键词（strategy 7 字段 + universe 5 键，均不得在代码里写字面量；
# first_drawdown_pct v7.0.26 起居 timing 段，关键词守卫不变；
# v7.0.28 手数口径：lot_size/first_lots/add_lots_step 替代金额 3 键）
THRESHOLD_KEYWORDS = [
    "lot_size", "first_lots", "add_lots_step", "first_drawdown_pct", "add_step_pct",
    "rearm_pct", "accounting",
    "min_total_mv_yi", "min_consecutive_div_years", "listing_min_trading_days",
]


def _iter_py_files():
    for root, _, files in os.walk(SRC_DIR):
        for fn in sorted(files):
            if fn.endswith(".py"):
                yield os.path.join(root, fn)


def test_no_hardcoded_thresholds():
    """martingale/ 中阈值关键词不得与字面量数字直接绑定（赋值/实参）。"""
    violations = []
    # 模式1: kw = <数字>（变量赋值）
    pat_assign = re.compile(
        r"^\s*(\w*(" + "|".join(THRESHOLD_KEYWORDS) + r")\w*)\s*=\s*\d+(\.\d+)?\s*$",
        re.MULTILINE,
    )
    # 模式2: kw=<数字> 实参（调用/构造）
    pat_call = re.compile(
        r"(" + "|".join(THRESHOLD_KEYWORDS) + r")\s*=\s*\d+(\.\d+)?\b"
    )
    for path in _iter_py_files():
        with open(path, encoding="utf-8") as f:
            src = f.read()
        code = "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))
        for m in pat_assign.finditer(code):
            violations.append(f"{os.path.relpath(path, PROJECT)}: {m.group(0).strip()}")
        for m in pat_call.finditer(code):
            violations.append(f"{os.path.relpath(path, PROJECT)}: {m.group(0)}")
    assert not violations, "疑似硬编码策略/池阈值（应来自 strategy.yaml）:\n" + "\n".join(violations)


def test_strategy_yaml_is_single_source_of_truth():
    """strategy.yaml 必须含 v7 schema 全部可配置项（定案，勿增删）。"""
    import yaml
    path = os.path.join(PROJECT, "config", "strategy.yaml")
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    strategy_keys = {
        "name", "lot_size", "first_lots",
        "add_lots_step", "add_step_pct", "rearm_mode", "rearm_pct", "accounting",
    }
    universe_keys = {
        "industry_whitelist_csric2", "soe_keywords", "min_total_mv_yi",
        "min_consecutive_div_years", "listing_min_trading_days",
    }
    timing_keys = {  # v7.0.26：②③ 删除、首仓建仓回撤移入
        "price_window_years", "price_pctile_max", "first_drawdown_pct",
    }
    strat = cfg.get("strategy", {})
    univ = cfg.get("universe", {})
    tm_ = cfg.get("timing", {})
    missing_s = strategy_keys - set(strat)
    missing_u = universe_keys - set(univ)
    missing_t = timing_keys - set(tm_)
    assert not missing_s, f"strategy.yaml strategy 段缺键: {sorted(missing_s)}"
    assert not missing_u, f"strategy.yaml universe 段缺键: {sorted(missing_u)}"
    assert not missing_t, f"strategy.yaml timing 段缺键: {sorted(missing_t)}"

    # 枚举/固定值语义
    assert strat.get("name") == "drawdown_martingale", "strategy.name 必须为 drawdown_martingale"
    assert strat.get("accounting") == "cash", "accounting 固定 cash（cash 口径）"
    assert strat.get("rearm_mode") in ("bounce", "newhigh"), "rearm_mode 枚举越界"

    # lake: 段必须保留（lake/config.py 读取；lake-sync 依赖）
    assert isinstance(cfg.get("lake"), dict) and cfg["lake"], "缺 lake: 段（lake-sync 依赖）"

    # datasource.{sina,rf} 子段必须保留（lake_backfill 唯一消费；v7.0.18 baostock 段移除）
    ds = cfg.get("datasource", {})
    for sub in ("sina", "rf"):
        assert isinstance(ds.get(sub), dict), f"datasource.{sub} 子段缺失（lake-sync 依赖）"

    # pct 字段必须为整数百分数（config.py 负责 /100 转小数）
    # v7.0.26：first_drawdown_pct 居 timing 段
    for k in ("add_step_pct", "rearm_pct"):
        v = strat[k]
        assert isinstance(v, (int, float)) and 0 < v < 100, f"{k} 应为 (0,100) 内百分数: {v!r}"
    v = tm_["first_drawdown_pct"]
    assert isinstance(v, (int, float)) and 0 < v < 100, f"first_drawdown_pct 应为 (0,100) 内百分数: {v!r}"
