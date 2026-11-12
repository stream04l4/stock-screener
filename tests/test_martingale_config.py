# -*- coding: utf-8 -*-
"""martingale.config 单测：schema 校验 + pct→小数 + 派生价位计算 + 错误分支。"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from martingale.config import (  # noqa: E402
    ACCOUNTING_CASH,
    COMMISSION_BPS,
    ConfigError,
    StrategyParams,
    load_config,
)

REAL_YAML = PROJECT_ROOT / "config" / "strategy.yaml"


# ---------------------------------------------------------------------------
# 生产 yaml：加载 + 单位换算
# ---------------------------------------------------------------------------

def test_load_real_config():
    p, u, t = load_config(str(REAL_YAML))
    assert p.name == "drawdown_martingale"
    assert p.accounting == ACCOUNTING_CASH
    assert p.rearm_mode in ("bounce", "newhigh")
    # pct 字段：yaml 整数百分数 → 小数
    assert 0 < p.first_drawdown < 1
    assert 0 < p.add_step < 1
    assert 0 < p.rearm_pct <= 1
    # 手数 = yaml 原值（v7.0.28 手数口径；v8.1.2 步长 1→2；v8.1.3 首仓 1→2）
    assert p.lot_size == int(REAL_YAML.read_text().split("lot_size:")[1].split()[0])
    assert (p.first_lots, p.add_lots_step) == (2, 2)
    # 佣金常量 3bps（引擎常量，不进 yaml）
    assert p.commission == COMMISSION_BPS / 1e4 == 3.0 / 1e4
    assert p.stamp == 5.0 / 1e4
    # universe
    # v8.0.8：行业白名单全放开 ["*"]（或 3 位 csric2 行业码）
    if u.industry_whitelist_csric2 == ("*",):
        pass  # 全放开哨兵
    else:
        assert len(u.industry_whitelist_csric2) >= 10
        assert all(len(c) == 3 and c[0].isalpha() and c[0].isupper() and c[1:3].isdigit()
                   for c in u.industry_whitelist_csric2)
    assert u.min_total_mv_yi >= 1
    assert u.min_consecutive_div_years >= 1
    assert 20 <= u.listing_min_trading_days <= 1200
    # v8.0.8 国企控股强度门（生产定值；可选键，缺省 0=关闭）
    assert u.min_soe_ratio_pct == 8.0
    assert u.min_soe_sum_ratio_pct == 30.0
    assert u.exclude_codes == ("sh.603766",)
    assert len(u.soe_control_overrides) == 4
    # v7.0.26 timing：生产 yaml 段 = 3 键（5 年 / 40%；首仓建仓回撤亦归本段）
    assert (t.price_window_years, t.price_pctile_max) == (5, 40.0)
    assert p.first_drawdown == pytest.approx(0.15)  # 取自 timing 段


# ---------------------------------------------------------------------------
# 派生价位
# ---------------------------------------------------------------------------

def _params(**kw):
    base = dict(name="drawdown_martingale", lot_size=100, first_lots=1,
                add_lots_step=1, first_drawdown=0.15, add_step=0.05, rearm_mode="bounce",
                rearm_pct=0.15, accounting="cash")
    base.update(kw)
    return StrategyParams(**base)


def test_level_price_and_next_add_price():
    p = _params()
    # 第 k 档线 = H×(1−0.15−k×0.05)
    assert p.level_price(100.0, 0) == pytest.approx(85.0)
    assert p.level_price(100.0, 1) == pytest.approx(80.0)
    assert p.level_price(100.0, 2) == pytest.approx(75.0)
    # 下一档 = 已买档数 k_level 对应的那条线（尚未触发）
    assert p.next_add_price(100.0, 2) == pytest.approx(75.0)


def test_next_rearm_price():
    p = _params()
    assert p.next_rearm_price(50.0) == pytest.approx(50.0 * 1.15)


# ---------------------------------------------------------------------------
# 错误分支（ConfigError）
# ---------------------------------------------------------------------------

def _write(tmp_path, cfg):
    import yaml
    f = tmp_path / "s.yaml"
    f.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return str(f)


def _cfg():
    return copy.deepcopy(__import__("yaml").safe_load(REAL_YAML.read_text(encoding="utf-8")))


def test_missing_strategy_section(tmp_path):
    c = _cfg()
    del c["strategy"]
    with pytest.raises(ConfigError, match="strategy 段"):
        load_config(_write(tmp_path, c))


def test_missing_field(tmp_path):
    c = _cfg()
    del c["strategy"]["first_lots"]
    with pytest.raises(ConfigError, match="strategy.first_lots 缺失"):
        load_config(_write(tmp_path, c))


def test_bad_name(tmp_path):
    c = _cfg()
    c["strategy"]["name"] = "other_strategy"
    with pytest.raises(ConfigError, match="name"):
        load_config(_write(tmp_path, c))


def test_accounting_fixed_cash(tmp_path):
    c = _cfg()
    c["strategy"]["accounting"] = "market_value"
    with pytest.raises(ConfigError, match="cash"):
        load_config(_write(tmp_path, c))


def test_rearm_mode_enum(tmp_path):
    c = _cfg()
    c["strategy"]["rearm_mode"] = "whenever"
    with pytest.raises(ConfigError, match="rearm_mode"):
        load_config(_write(tmp_path, c))


def test_dd_plus_step_must_below_100(tmp_path):
    c = _cfg()
    c["timing"]["first_drawdown_pct"] = 60
    c["strategy"]["add_step_pct"] = 45
    with pytest.raises(ConfigError, match="< 100"):
        load_config(_write(tmp_path, c))


def test_whitelist_code_format(tmp_path):
    c = _cfg()
    c["universe"]["industry_whitelist_csric2"] = ["b06"]  # 小写 → 非法
    with pytest.raises(ConfigError, match="industry_whitelist"):
        load_config(_write(tmp_path, c))
    c["universe"]["industry_whitelist_csric2"] = ["B6"]  # 长度非法
    with pytest.raises(ConfigError, match="industry_whitelist"):
        load_config(_write(tmp_path, c))


def test_whitelist_empty(tmp_path):
    c = _cfg()
    c["universe"]["industry_whitelist_csric2"] = []
    with pytest.raises(ConfigError, match="非空"):
        load_config(_write(tmp_path, c))


def test_range_violation(tmp_path):
    c = _cfg()
    c["strategy"]["first_lots"] = 0  # < 1
    with pytest.raises(ConfigError, match="超出范围"):
        load_config(_write(tmp_path, c))


def test_type_violation_bool_is_not_int(tmp_path):
    c = _cfg()
    c["strategy"]["first_lots"] = True
    with pytest.raises(ConfigError, match="必须是整数"):
        load_config(_write(tmp_path, c))
