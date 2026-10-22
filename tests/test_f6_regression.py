# -*- coding: utf-8 -*-
"""f6 七股回归（验收红线）：引擎跑生产湖 vs tests/fixtures/f6_baseline/ fixture，
per_stock 14 字段 + 逐笔买入时间表全部对齐。fixture = Joel 2026-10-22 验收跑产物快照
（/tmp/f6_* → 任务书 §1.4；tmpfs 易失，此 fixture 是 tester 的唯一基准）。

f6 参数（config/strategy.yaml 生产值，逐位一致的前提）：
initial=10000, first_amount=100000, dd=0.15, step=0.05, growth=1.1,
ladder=high, rearm=bounce(0.15), accounting=cash, comm=3bps, stamp=5bps,
窗口 2016-01-04 → 2026-10-21。
"""
from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from martingale.config import load_config  # noqa: E402
from martingale.data import connect_readonly, default_db_path  # noqa: E402
from martingale.engine import load, run_stock  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "f6_baseline"
START, END = "2016-01-04", "2026-10-21"
F6 = ["sh.601088", "sh.600036", "sz.000651", "sh.600900",
      "sh.601668", "sh.600938", "sz.000858"]

NUM_FIELDS = ["n_bars", "n_rounds", "n_rearms", "n_buys", "total_invested",
              "final_assets", "dividends", "total_pnl", "pnl_pct", "min_unreal_pct"]
DATE_FIELDS = ["first_buy_date", "last_buy_date"]


@pytest.fixture(scope="module")
def cfg():
    return load_config(str(PROJECT_ROOT / "config" / "strategy.yaml"))[0]


@pytest.fixture(scope="module")
def loaded():
    """一次加载七股 K线/分红（模块级共享，避免 14 次重复 IO）。"""
    con = connect_readonly()
    try:
        _names, data, divs = load(con, codes=F6, start=START, end=END)
    finally:
        con.close()
    return data, divs


def _fixture_row(code):
    with open(FIX / code / "per_stock.csv", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    return rows[0]


def _fixture_buys(code):
    text = (FIX / code / "summary.md").read_text(encoding="utf-8")
    return re.findall(r"  - (\S+) @ ([\d.]+) 买 ([\d,]+)", text)


def _run(cfg, loaded, code):
    dates, sig, exec_c = loaded[0][code]
    stat, *_ , buys = run_stock(code, dates, sig, exec_c, loaded[1].get(code), cfg,
                                verbose=False)
    return stat, buys


@pytest.mark.skipif(not Path(default_db_path()).exists(), reason="生产湖不存在")
@pytest.mark.parametrize("code", F6)
def test_f6_per_stock_bitwise(code, cfg, loaded):
    stat, *_ = _run(cfg, loaded, code)
    fx = _fixture_row(code)
    for k in NUM_FIELDS:
        assert stat[k] == float(fx[k]), (code, k, stat[k], fx[k])
    for k in DATE_FIELDS:
        assert stat[k] == fx[k], (code, k, stat[k], fx[k])
    assert stat["last_status"] == fx["last_status"]


@pytest.mark.skipif(not Path(default_db_path()).exists(), reason="生产湖不存在")
@pytest.mark.parametrize("code", F6)
def test_f6_buy_timeline(code, cfg, loaded):
    stat, buys = _run(cfg, loaded, code)
    fx_buys = _fixture_buys(code)
    assert stat["n_buys"] == len(fx_buys), (code, stat["n_buys"], len(fx_buys))
    for (dt, price, amt), (f_dt, f_price, f_amt) in zip(buys, fx_buys):
        assert dt == f_dt, (code, dt, f_dt)
        assert price == float(f_price), (code, dt, price, f_price)
        # summary 按整数千分位渲染（:,.0f）→ 金额按四舍五入对齐（float 幂次有 ulp 尾差）
        assert round(amt) == int(f_amt.replace(",", "")), (code, dt, amt, f_amt)
