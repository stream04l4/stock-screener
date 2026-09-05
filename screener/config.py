# -*- coding: utf-8 -*-
"""策略配置加载与校验。

所有筛选阈值都来自 config/strategy.yaml —— 代码里不得出现任何硬编码阈值。
本模块只做"读取 + 结构校验"，不做业务判断。
"""
from __future__ import annotations

import os
from typing import Any, Dict

import yaml


class ConfigError(ValueError):
    """配置缺失或非法。"""


def _require(cfg: Dict[str, Any], dotted: str) -> Any:
    cur: Any = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise ConfigError(f"strategy.yaml 缺少配置项: {dotted}")
        cur = cur[part]
    return cur


def load_config(path: str) -> Dict[str, Any]:
    """读取 strategy.yaml，返回原始 dict（已做结构校验）。"""
    if not os.path.exists(path):
        raise ConfigError(f"配置文件不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if not isinstance(cfg, dict):
        raise ConfigError("strategy.yaml 顶层必须是映射(mapping)")

    # --- 结构校验：缺项直接报错，避免运行到一半才发现 ---
    for key in (
        "technical.ma_period",
        "technical.return_window_days",
        "technical.min_return_pct",
        "technical.max_return_pct",
        "technical.max_annual_volatility_pct",
        "dividend.window_days",
        "dividend.min_yield_pct",
        "fundamental.roe_min_pct",
        "fundamental.net_profit_yoy_field",
        "fundamental.liability_max_pct",
        "fundamental.gross_margin_min_pct",
        "fundamental.probe_quarters_back",
        "industry.rank_by",
        "industry.top_pct",
        "industry.min_group_size",
        "universe.a_share_prefixes",
        "universe.listing_min_trading_days",
        "data.kline_calendar_days_back",
    ):
        _require(cfg, key)

    # --- 语义校验 ---
    tech = cfg["technical"]
    if not (0 <= float(tech["min_return_pct"]) <= float(tech["max_return_pct"])):
        raise ConfigError("technical.min_return_pct 必须 <= max_return_pct")
    if int(tech["ma_period"]) < 1 or int(tech["return_window_days"]) < 2:
        raise ConfigError("technical.ma_period / return_window_days 非法")

    fund = cfg["fundamental"]
    if fund["net_profit_yoy_field"] not in ("YOYPNI", "YOYNI"):
        raise ConfigError("fundamental.net_profit_yoy_field 只能是 YOYPNI 或 YOYNI")

    ind = cfg["industry"]
    if ind["rank_by"] != "roeAvg":
        raise ConfigError("industry.rank_by 目前只支持 roeAvg")
    if not (0 < float(ind["top_pct"]) <= 100):
        raise ConfigError("industry.top_pct 必须在 (0, 100]")

    uni = cfg["universe"]
    for p in uni["a_share_prefixes"]:
        if not isinstance(p, str) or "." not in p:
            raise ConfigError(f"universe.a_share_prefixes 项非法: {p!r}（应形如 sh.60）")

    return cfg


def tech(cfg: Dict[str, Any]) -> Dict[str, float]:
    """技术面阈值（小数/百分数口径与原配置一致，见各字段名）。"""
    t = cfg["technical"]
    return {
        "ma_period": int(t["ma_period"]),
        "return_window_days": int(t["return_window_days"]),
        # 配置里是百分数(如 100 表示 100%)，统一转成小数供比较
        "min_return": float(t["min_return_pct"]) / 100.0,
        "max_return": float(t["max_return_pct"]) / 100.0,
        "max_vol": float(t["max_annual_volatility_pct"]) / 100.0,
    }


def dividend_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    d = cfg["dividend"]
    return {
        "window_days": int(d["window_days"]),
        # 百分数 → 小数
        "min_yield": float(d["min_yield_pct"]) / 100.0,
    }


def fundamental_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    f = cfg["fundamental"]
    return {
        # 基本面字段在 BaoStock 里是小数（0.10 = 10%），阈值统一转小数比较
        "roe_min": float(f["roe_min_pct"]) / 100.0,
        "yoy_field": str(f["net_profit_yoy_field"]),
        "liability_max": float(f["liability_max_pct"]) / 100.0,
        "gross_margin_min": float(f["gross_margin_min_pct"]) / 100.0,
        "probe_quarters_back": int(f["probe_quarters_back"]),
    }


def industry_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    i = cfg["industry"]
    return {
        "rank_by": str(i["rank_by"]),
        "top_pct": float(i["top_pct"]) / 100.0,
        "min_group_size": int(i["min_group_size"]),
    }


def universe_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    u = cfg["universe"]
    return {
        "prefixes": [str(p) for p in u["a_share_prefixes"]],
        "listing_min_trading_days": int(u["listing_min_trading_days"]),
        "st_name_keyword": str(u.get("st_name_keyword", "ST")),
    }


def data_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    d = cfg["data"]
    return {
        # 后复权窗口K线往前回溯的日历天数（需覆盖 ma_period/return_window_days 个交易日，
        # 250 个交易日 ≈ 375 个日历天；取 420 留足节假日余量）
        "kline_calendar_days_back": int(d["kline_calendar_days_back"]),
        "retry_max_attempts": int(d.get("retry_max_attempts", 5)),
        "cache_dir": str(d.get("cache_dir", "cache")),
    }


def crosscheck_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    c = cfg.get("crosscheck", {}) or {}
    return {
        "enabled": bool(c.get("enabled", True)),
        "sample_size": int(c.get("sample_size", 20)),
        "price_tolerance_pct": float(c.get("price_tolerance_pct", 0.5)) / 100.0,
        "batch_size": int(c.get("batch_size", 50)),
    }
