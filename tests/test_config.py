# -*- coding: utf-8 -*-
"""配置层单测：缺项报错、阈值单位换算（百分数→小数）、字段白名单。

v2 扩展（报告 R6）：scoring 段校验（mode∈{zscore,legacy}、权重和≈1、
sub_weights 每维和≈1、missing_policy 白名单、top_n≥1）+ badges/hard_filter 段。
"""
from __future__ import annotations

import copy
import os
import tempfile

import pytest
import yaml

from screener.config import (
    ConfigError,
    data_cfg,
    dividend_cfg,
    fundamental_cfg,
    hard_filter_cfg,
    industry_cfg,
    load_config,
    scoring_cfg,
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
        # v2 打分模型（默认值照报告 R4）
        "scoring": {
            "mode": "zscore",
            "top_n": 50,
            "missing_policy": "neutral_renorm",
            "normalize": "cross_section",
            "weights": {"technical": 0.25, "dividend": 0.30, "industry": 0.15,
                        "fundamental": 0.30},
            "sub_weights": {
                "technical": {"ma_bullish": 0.2, "window_return": 0.2, "low_vol": 0.2,
                              "rsi": 0.2, "macd": 0.2},
                "dividend": {"ttm_yield": 0.6, "payout_ratio": 0.4},
                "industry": {"roe_rank_pct": 0.5, "yoy_pni_rank_pct": 0.5},
                "fundamental": {"roe_level": 0.3, "roe_stability": 0.2,
                                "low_liability": 0.15, "gross_margin": 0.15,
                                "piotroski": 0.2},
            },
        },
        # v2 badge 阈值（web 表格用，百分数口径）。高股息(绿)复用 dividend.min_yield_pct。
        "badges": {"industry_top_pct": 10, "fscore_min": 7},
        # v2 硬性剔除开关
        "hard_filter": {"st_enabled": True, "listing_min_trading_days": 250},
    }


def _dump_and_load(cfg: dict):
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        yaml.safe_dump(cfg, f)
        tmp = f.name
    try:
        return load_config(tmp)
    finally:
        os.unlink(tmp)


def test_load_config_file():
    """真实 strategy.yaml 可加载且结构完整。"""
    path = os.path.join(os.path.dirname(__file__), "..", "config", "strategy.yaml")
    cfg = load_config(path)
    assert cfg["technical"]["ma_period"] == 200
    assert cfg["fundamental"]["net_profit_yoy_field"] in ("YOYPNI", "YOYNI")
    # v2 段存在且默认值符合报告 R4
    assert cfg["scoring"]["mode"] == "zscore"
    assert cfg["scoring"]["top_n"] == 50
    assert cfg["scoring"]["missing_policy"] == "neutral_renorm"


def test_missing_key_raises():
    cfg = _base_cfg()
    del cfg["dividend"]["min_yield_pct"]
    with pytest.raises(ConfigError):
        _dump_and_load(cfg)


def test_invalid_yoy_field_raises():
    cfg = _base_cfg()
    cfg["fundamental"]["net_profit_yoy_field"] = "YOYXXX"
    with pytest.raises(ConfigError):
        _dump_and_load(cfg)


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
    with pytest.raises(ConfigError):
        _dump_and_load(cfg)


def test_universe_prefix_validation():
    cfg = _base_cfg()
    cfg["universe"]["a_share_prefixes"] = ["sh60"]  # 缺 "."
    with pytest.raises(ConfigError):
        _dump_and_load(cfg)


def test_data_cfg_defaults():
    cfg = _base_cfg()
    d = data_cfg(cfg)
    assert d["kline_calendar_days_back"] == 420
    assert d["retry_max_attempts"] == 5
    assert d["cache_dir"] == "cache"


# ---------------------------------------------------------------------------
# v2 scoring 段校验（R6 必改）
# ---------------------------------------------------------------------------

def test_scoring_valid_loads():
    cfg = _base_cfg()
    loaded = _dump_and_load(cfg)
    sc = scoring_cfg(loaded)
    assert sc["mode"] == "zscore"
    assert sc["top_n"] == 50
    assert sc["missing_policy"] == "neutral_renorm"
    assert sum(sc["weights"].values()) == pytest.approx(1.0)
    for dim, subs in sc["sub_weights"].items():
        assert sum(subs.values()) == pytest.approx(1.0), dim


def test_scoring_missing_section_raises():
    cfg = _base_cfg()
    del cfg["scoring"]
    with pytest.raises(ConfigError):
        _dump_and_load(cfg)


def test_scoring_missing_weights_key_raises():
    cfg = _base_cfg()
    del cfg["scoring"]["weights"]["dividend"]
    with pytest.raises(ConfigError):
        _dump_and_load(cfg)


@pytest.mark.parametrize("mode", ["zscore", "legacy"])
def test_scoring_mode_whitelist(mode):
    cfg = _base_cfg()
    cfg["scoring"]["mode"] = mode
    loaded = _dump_and_load(cfg)
    assert loaded["scoring"]["mode"] == mode


def test_scoring_bad_mode_raises():
    cfg = _base_cfg()
    cfg["scoring"]["mode"] = "random_forest"
    with pytest.raises(ConfigError):
        _dump_and_load(cfg)


def test_scoring_weights_sum_not_one_raises():
    """权重和必须 ≈1（归一化基准；TL 拍板：代码零硬编码，全部在此校验）。"""
    cfg = _base_cfg()
    cfg["scoring"]["weights"]["technical"] = 0.50  # 和=1.25
    with pytest.raises(ConfigError, match="之和必须为 1"):
        _dump_and_load(cfg)


def test_scoring_sub_weights_sum_not_one_raises():
    cfg = _base_cfg()
    cfg["scoring"]["sub_weights"]["dividend"]["ttm_yield"] = 0.9  # 和=1.3
    with pytest.raises(ConfigError, match="sub_weights.dividend"):
        _dump_and_load(cfg)


def test_scoring_bad_missing_policy_raises():
    cfg = _base_cfg()
    cfg["scoring"]["missing_policy"] = "impute_median"
    with pytest.raises(ConfigError):
        _dump_and_load(cfg)


def test_scoring_top_n_must_be_positive():
    cfg = _base_cfg()
    cfg["scoring"]["top_n"] = 0
    with pytest.raises(ConfigError):
        _dump_and_load(cfg)


# ---------------------------------------------------------------------------
# v2 badges / hard_filter 段校验
# ---------------------------------------------------------------------------

def test_badges_valid_loads():
    """badges 段可加载；高股息(绿)阈值复用 dividend.min_yield_pct（单一事实来源）。"""
    cfg = _base_cfg()
    loaded = _dump_and_load(cfg)
    assert loaded["badges"]["industry_top_pct"] == 10
    assert loaded["badges"]["fscore_min"] == 7
    # 高股息阈值不另设键，来自 dividend.min_yield_pct（web/app.py run_detail 组装）
    assert "high_dividend_pct" not in loaded["badges"]
    assert loaded["dividend"]["min_yield_pct"] == 3


def test_badges_out_of_range_raises():
    cfg = _base_cfg()
    cfg["badges"]["industry_top_pct"] = 150
    with pytest.raises(ConfigError):
        _dump_and_load(cfg)


def test_hard_filter_valid_loads():
    cfg = _base_cfg()
    h = hard_filter_cfg(_dump_and_load(cfg))
    assert h["st_enabled"] is True
    assert h["listing_min_trading_days"] == 250


def test_hard_filter_bad_type_raises():
    cfg = _base_cfg()
    cfg["hard_filter"]["st_enabled"] = "yes"  # 必须布尔
    with pytest.raises(ConfigError):
        _dump_and_load(cfg)
