# -*- coding: utf-8 -*-
"""crypto.backtest CLI —— 全史回测 + 报告（v7.0.47）。

用法：
    python -m crypto.backtest                 # crypto.yaml coins 段全部
    python -m crypto.backtest --coins BTCUSDT,ETHUSDT
    python -m crypto.backtest --out /tmp/r.md
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CFG_PATH = os.path.join(ROOT, "crypto", "config", "crypto.yaml")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m crypto.backtest")
    ap.add_argument("--coins", default="", help="逗号分隔；缺省 = crypto.yaml coins 段")
    ap.add_argument("--out", default="", help="报告输出路径（缺省 data/crypto/backtest_report.md）")
    args = ap.parse_args(argv)

    with open(CFG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    from crypto.strategy.engine import StrategyParams
    from crypto.backtest.runner import run_backtest, save_report
    p = StrategyParams.from_cfg(cfg)
    symbols = [s.strip() for s in args.coins.split(",") if s.strip()] or cfg["coins"]

    t0 = time.time()
    print(f"[backtest] 策略参数: 建仓=一年高点×{p.trigger_frac():.0%}, "
          f"网格={p.add_grid_pct:g}%/级, 首仓={p.base_amount_usdt:g} USDT, "
          f"增长={p.growth:g}, 止盈={p.take_profit_mult:g}x, 费={p.fee_bps:g}bps")
    results = run_backtest(symbols, p)
    for r in results:
        if r["n_bars"] == 0:
            print(f"[backtest] {r['symbol']}: 无数据")
            continue
        o = r["open_round"]
        tail = (f"未了结: {o['entry_date']} 建仓 {o['entry']}, k={o['k_level']}, "
                f"投入 {o['spent_usdt']} USDT, 浮盈 {o['unrealized_pct']}%"
                if o else "当前空仓")
        print(f"[backtest] {r['symbol']}: {r['start']}~{r['end']} "
              f"了结 {len(r['rounds'])} 轮 | {tail} | "
              f"净值 {r['final_equity']} USDT | MDD {r['mdd_pct']}%")
    out = save_report(results, p, args.out or "")
    print(f"[backtest] 报告 → {out}（{time.time() - t0:.1f}s）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
