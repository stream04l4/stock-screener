# -*- coding: utf-8 -*-
"""纪律检查：代码里不得硬编码筛选阈值（brief §5 禁止事项）。

扫描 screener/ 下所有 .py，找出"字面量 + 阈值关键词"的可疑组合。
允许出现的数字上下文：日期、行数、下标、HTTP 状态码、日志步长等——
用白名单正则排除这些常见非阈值数字，剩下的可疑命中需人工确认（测试失败时列出）。
"""
from __future__ import annotations

import os
import re

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(PROJECT, "screener")

# 阈值关键词：出现这些词附近的字面量数字需要审查
THRESHOLD_KEYWORDS = [
    "min_return", "max_return", "max_vol", "ma_period", "min_yield",
    "roe_min", "liability_max", "gross_margin_min", "top_pct",
    "min_group_size", "listing_min", "window_days", "kline_calendar",
    # v2：打分权重 / 榜单规模 / badge 阈值（R6：纳入单一事实来源约束）
    "weights", "sub_weights", "top_n", "fscore_min",
]

# 允许的非阈值数字模式（行内注释/字符串里的说明、下标、日期等）
ALLOWED_LINE = re.compile(
    r"(\d{4}-\d{2}-\d{2}|YYYY|MMDD|idx|IDX_|status_code|HTTP|port|timeout|"
    r"sqrt\(250\)|TRADING_DAYS_PER_YEAR|= 250|1e-9|0x|percentile.*100|\* 100|/ 100)",
    re.IGNORECASE,
)


def _iter_py_files():
    for root, _, files in os.walk(SRC_DIR):
        for fn in sorted(files):
            if fn.endswith(".py"):
                yield os.path.join(root, fn)


def test_no_hardcoded_thresholds():
    """所有筛选阈值必须来自 config（strategy.yaml），不得硬编码。

    检查策略：对每个源文件，找"函数调用/比较表达式里直接写死阈值数字"的模式。
    具体地：metrics/screener/universe/report/config 中，凡是以关键字命名的参数
    被**字面量数字**赋值（而非来自 cfg）即判失败。
    """
    violations = []
    # 模式1: 形如 compute_xxx(..., ma_period=200, ...) 的字面量实参
    pat_call = re.compile(
        r"(ma_period|min_return|max_return|max_vol|min_yield|roe_min|"
        r"yoy_field|liability_max|gross_margin_min|top_pct|min_group_size|"
        r"window_days|probe_back|listing_min_trading_days)\s*=\s*\d+(\.\d+)?\b"
    )
    # 模式2: 阈值变量被字面量赋值（x = 0.45 且 x 名含阈值词）
    pat_assign = re.compile(
        r"^\s*(\w*(?:min_return|max_return|max_vol|min_yield|roe_min|"
        r"liability_max|gross_margin_min|top_pct|min_group_size)\w*)\s*=\s*\d+(\.\d+)?\s*$",
        re.MULTILINE,
    )
    # 模式3（v2）: 权重/子权重字面量——维度名后直接跟数字（weights={"technical": 0.25}
    # 或 weights["technical"] = 0.25）。权重必须来自 strategy.yaml scoring 段，
    # 代码里出现 "technical"/"dividend"/"industry"/"fundamental": <数字> 即判失败。
    pat_weight = re.compile(
        r"['\"](?:technical|dividend|industry|fundamental)['\"]\s*:\s*\d+(\.\d+)?\b"
    )
    for path in _iter_py_files():
        with open(path, encoding="utf-8") as f:
            src = f.read()
        # 去掉注释与 docstring 行（粗略：以 # 开头的行）
        code_lines = [ln for ln in src.splitlines() if not ln.strip().startswith("#")]
        code = "\n".join(code_lines)
        for m in pat_call.finditer(code):
            violations.append(f"{os.path.relpath(path, PROJECT)}: {m.group(0)}")
        for m in pat_assign.finditer(code):
            violations.append(f"{os.path.relpath(path, PROJECT)}: {m.group(0).strip()}")
        for m in pat_weight.finditer(code):
            violations.append(f"{os.path.relpath(path, PROJECT)}: 权重字面量 {m.group(0)}")

    assert not violations, "疑似硬编码阈值:\n" + "\n".join(violations)


def test_strategy_yaml_is_single_source_of_truth():
    """strategy.yaml 必须包含 brief 要求的全部可配置阈值项。"""
    import yaml
    path = os.path.join(PROJECT, "config", "strategy.yaml")
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    required = {
        ("technical", "ma_period"), ("technical", "return_window_days"),
        ("technical", "min_return_pct"), ("technical", "max_return_pct"),
        ("technical", "max_annual_volatility_pct"),
        ("dividend", "window_days"), ("dividend", "min_yield_pct"),
        ("industry", "top_pct"), ("industry", "min_group_size"),
        ("fundamental", "roe_min_pct"), ("fundamental", "net_profit_yoy_field"),
        ("fundamental", "liability_max_pct"), ("fundamental", "gross_margin_min_pct"),
        ("universe", "listing_min_trading_days"),
        # v2 打分模型（R6：scoring.weights/sub_weights 纳入单一事实来源）
        ("scoring", "mode"), ("scoring", "top_n"), ("scoring", "missing_policy"),
        ("scoring", "weights"), ("scoring", "sub_weights"),
        # v2 badge 阈值 / 硬剔除开关（高股息(绿)复用 dividend.min_yield_pct，不另设键）
        ("badges", "industry_top_pct"), ("badges", "fscore_min"),
        ("hard_filter", "st_enabled"), ("hard_filter", "listing_min_trading_days"),
    }
    missing = [k for k in required if not (isinstance(cfg.get(k[0]), dict) and k[1] in cfg[k[0]])]
    assert not missing, f"strategy.yaml 缺少可配置项: {missing}"

    # v2：四维权重键齐全（代码不得出现字面量权重，必须从这里取）
    weights = cfg["scoring"]["weights"]
    for dim in ("technical", "dividend", "industry", "fundamental"):
        assert dim in weights, f"scoring.weights 缺少维度 {dim}"
    sub = cfg["scoring"]["sub_weights"]
    for dim in ("technical", "dividend", "industry", "fundamental"):
        assert isinstance(sub.get(dim), dict) and sub[dim], f"scoring.sub_weights.{dim} 缺失"
