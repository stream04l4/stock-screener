# -*- coding: utf-8 -*-
"""f6 七股回归（验收红线）：引擎跑生产湖 vs tests/fixtures/f6_baseline/ fixture，
per_stock 14 字段 + 逐笔买入时间表全部对齐。fixture = 验收跑产物快照
（/tmp/f6_* → 任务书 §1.4；tmpfs 易失，此 fixture 是 tester 的唯一基准）。

v8.1.0（Joel 2026-11-12 定案）：首仓双条件（① 高点回撤 ≥15% 且 ② 5 年收盘
分位 ≤40%，PIT）+ 加仓锚点 = 首仓价 P0。基线已按新口径重新生成（2026-11-12；
买点时间表变化属策略口径变更，逐字段漂移见 commit 记录）。测试跑生产口径：
load 起点前移 5 年喂分位窗口 → first_ok_series（与页面同口径）→ 切回窗口。

f6 参数（config/strategy.yaml 生产值，逐位一致的前提）：
lot_size=100, first_lots=2, add_lots_step=2（v7.0.28 手数 C 方案：第 k 档
0 起 = first_lots+k×add_lots_step 手，再武装后 k 归零重新从 2 手起算；
**v8.1.2（2026-11-12）步长 1→2；v8.1.3 首仓 1→2（首仓2/加仓4/再加6/8…），
基线已按新 sizing 重新生成，触发时机（n_buys/日期/价格）与 v8.1.0 基线
逐笔零 diff，仅金额变**），
dd=0.15, step=0.05, rearm=bounce(0.15), accounting=cash, comm=3bps, stamp=5bps,
timing=5年分位≤40%, 窗口 2016-01-04 → 2026-10-21。
"""
from __future__ import annotations

import csv
import math
import sys
from pathlib import Path

import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from martingale.config import load_config  # noqa: E402
from martingale.data import connect_readonly, default_db_path  # noqa: E402
from martingale.engine import first_ok_series, load, run_stock  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "f6_baseline"
START, END = "2016-01-04", "2026-10-21"
F6 = ["sh.601088", "sh.600036", "sz.000651", "sh.600900",
      "sh.601668", "sh.600938", "sz.000858"]

NUM_FIELDS = ["n_bars", "n_rounds", "n_rearms", "n_buys", "total_invested",
              "final_assets", "dividends", "total_pnl", "pnl_pct", "min_unreal_pct"]
DATE_FIELDS = ["first_buy_date", "last_buy_date"]


@pytest.fixture(scope="module")
def cfg():
    return load_config(str(PROJECT_ROOT / "config" / "strategy.yaml"))


@pytest.fixture(scope="module")
def loaded(cfg):
    """一次加载七股 K线/分红（模块级共享，避免 14 次重复 IO）。

    v8.1.0：load 起点前移 5 年（首仓双条件②分位窗口），切回窗口在 _run 内做。
    """
    _sp, _up, tp = cfg
    con = connect_readonly()
    try:
        from martingale.engine import extend_start
        _names, data, divs = load(con, codes=F6,
                                  start=extend_start(START, tp.price_window_years),
                                  end=END)
    finally:
        con.close()
    return data, divs


def _fixture_row(code):
    with open(FIX / code / "per_stock.csv", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    return rows[0]


def _fixture_buys(code):
    """v8.1.0：买入时间表改读 buys.csv（write_outputs 现版格式：
    ts_code,seq,date,price,amount,lots；旧版 summary.md 时间表段已退役）。"""
    with open(FIX / code / "buys.csv", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return [(r["date"], r["price"], r["amount"]) for r in rows]


def _run(cfg, loaded, code):
    sp, _up, tp = cfg
    dates, sig, exec_c = loaded[0][code]
    # v8.1.0：生产口径 = 首仓双条件（① 回撤线 + ② 5 年分位 PIT）
    ok = first_ok_series(dates, sig, tp.price_window_years, tp.price_pctile_max)
    i0 = int(np.searchsorted(dates, np.datetime64(START)))
    dates, sig, exec_c, ok = dates[i0:], sig[i0:], exec_c[i0:], ok[i0:]
    stat, *_ , buys = run_stock(code, dates, sig, exec_c, loaded[1].get(code), sp,
                                verbose=False, first_ok=ok)
    return stat, buys


@pytest.mark.skipif(not Path(default_db_path()).exists(), reason="生产湖不存在")
@pytest.mark.parametrize("code", F6)
def test_f6_per_stock_bitwise(code, cfg, loaded):
    stat, *_ = _run(cfg, loaded, code)
    fx = _fixture_row(code)
    for k in NUM_FIELDS:
        fv = fx[k]
        if fv == "":  # v8.1.0：never 股（双条件未触发，如长江电力）→ nan/空
            assert math.isnan(float(stat[k])), (code, k, stat[k])
        else:
            assert stat[k] == float(fv), (code, k, stat[k], fv)
    for k in DATE_FIELDS:
        assert stat[k] == fx[k], (code, k, stat[k], fx[k])
    assert stat["last_status"] == fx["last_status"]


@pytest.mark.skipif(not Path(default_db_path()).exists(), reason="生产湖不存在")
@pytest.mark.parametrize("code", F6)
def test_f6_buy_timeline(code, cfg, loaded):
    stat, buys = _run(cfg, loaded, code)
    fx_buys = _fixture_buys(code)
    assert stat["n_buys"] == len(fx_buys), (code, stat["n_buys"], len(fx_buys))
    for (dt, price, amt, _k, _for, _lots), (f_dt, f_price, f_amt) in zip(buys, fx_buys):
        assert dt == f_dt, (code, dt, f_dt)
        assert price == float(f_price), (code, dt, price, f_price)
        # buys.csv amount = repr(float) 全精度 → 按四舍五入整数对齐（ulp 尾差）
        assert round(amt) == round(float(f_amt)), (code, dt, amt, f_amt)
