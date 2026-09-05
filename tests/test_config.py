# -*- coding: utf-8 -*-
"""配置层单测：缺项报错、阈值单位换算（百分数→小数）、字段白名单。"""
from __future__ import annotations

import copy

import pytest

from screener.config import (
    ConfigError,
    data_cfg,
    dividend_cfg,
    fundamental_cfg,
    industry_cfg,
    load_config,
    tech,
    universe_cfg,
)


def _base_cfg() -> dict:
    return {
        "technical": {"ma_period": 200, "return_window_days": 250,
                      "min_return_pct": 0, "max_return_pct": 100,
                      "max_annual_volatility_pct": 45},
        "dividend": {"window_days": 365, "min_yield_pct": 3},
        "industry": {"rank_by": "roeAvg", "top_pct": 30, "min_group_size": 5},
        "fundamental": {"roe_min_pct": 10, "net_profit_yoy_field": "YOYPNI",
                        "liability_max_pct": 60, "gross_margin_min_pct": 20,
                        "probe_quarters_back": 3},
        "universe": {"a_share_prefixes": ["sh.60", "sh.68", "sz.00", "sz.30"],
                     "listing_min_trading_days": 250, "st_name_keyword": "ST"},
        "data": {"kline_calendar_days_back": 420, "retry_max_attempts": 5,
                 "cache_dir": "cache"},
    }


def test_load_config_file():
    """真实 strategy.yaml 可加载且结构完整。"""
    import os
    path = os.path.join(os.path.dirname(__file__), "..", "config", "strategy.yaml")
    cfg = load_config(path)
    assert cfg["technical"]["ma_period"] == 200
    assert cfg["fundamental"]["net_profit_yoy_field"] in ("YOYPNI", "YOYNI")


def test_missing_key_raises():
    import os, tempfile
    cfg = _base_cfg()
    del cfg["dividend"]["min_yield_pct"]
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        import yaml
        yaml.safe_dump(cfg, f)
        tmp = f.name
    try:
        with pytest.raises(ConfigError):
            load_config(tmp)
    finally:
        os.unlink(tmp)


def test_invalid_yoy_field_raises():
    import os, tempfile, yaml
    cfg = _base_cfg()
    cfg["fundamental"]["net_profit_yoy_field"] = "YOYXXX"
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        yaml.safe_dump(cfg, f)
        tmp = f.name
    try:
        with pytest.raises(ConfigError):
            load_config(tmp)
    finally:
        os.unlink(tmp)


def test_percent_to_decimal_conversion():
    """百分数配置 → 小数阈值（0.10/0.60/0.20/0.03），避免"ROE>=10 永不通过"坑。"""
    cfg = _base_cfg()
    assert tech(cfg)["max_vol"] == pytest.approx(0.45)
    assert tech(cfg)["min_return"] == pytest.approx(0.0)
    assert tech(cfg)["max_return"] == pytest.approx(1.0)
    assert dividend_cfg(cfg)["min_yield"] == pytest.approx(0.03)
    f = fundamental_cfg(cfg)
    assert f["roe_min"] == pytest.approx(0.10)
    assert f["liability_max"] == pytest.approx(0.60)
    assert f["gross_margin_min"] == pytest.approx(0.20)
    assert industry_cfg(cfg)["top_pct"] == pytest.approx(0.30)


def test_return_range_semantics():
    cfg = _base_cfg()
    cfg["technical"]["min_return_pct"] = 50
    cfg["technical"]["max_return_pct"] = 10
    import os, tempfile, yaml
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        yaml.safe_dump(cfg, f)
        tmp = f.name
    try:
        with pytest.raises(ConfigError):
            load_config(tmp)
    finally:
        os.unlink(tmp)


def test_universe_prefix_validation():
    cfg = _base_cfg()
    cfg["universe"]["a_share_prefixes"] = ["sh60"]  # 缺 "."
    import os, tempfile, yaml
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        yaml.safe_dump(cfg, f)
        tmp = f.name
    try:
        with pytest.raises(ConfigError):
            load_config(tmp)
    finally:
        os.unlink(tmp)


def test_data_cfg_defaults():
    cfg = _base_cfg()
    d = data_cfg(cfg)
    assert d["kline_calendar_days_back"] == 420
    assert d["retry_max_attempts"] == 5
    assert d["cache_dir"] == "cache"
