#!/usr/bin/env python3
"""F6 fixture 重基线（2026-11-26 tushare 弃用换源后）。

口径 = tests/test_f6_regression.py::_run 完全一致（生产 config/strategy.yaml，
窗口 2016-01-04..2026-10-21 冻结，起点前移 5 年分位窗）。
窗口内 K线/分红未动（重取仅覆盖 10-26..11-25）；变化全部来自 af 基准
（tushare rebase → 事件驱动单源重建，同 2026-10-26 af 对齐先例）。

用法: .venv/bin/python scripts/regen_f6_fixture.py [--check]
  默认覆盖写 tests/fixtures/f6_baseline/<code>/{per_stock,buys}.csv
  --check: 只打印新值 vs 现 fixture 的 diff，不写盘
"""
from __future__ import annotations

import csv
import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from martingale.config import load_config  # noqa: E402
from martingale.data import connect_readonly  # noqa: E402
from martingale.engine import extend_start, load, run_stock  # noqa: E402

START, END = "2016-01-04", "2026-10-21"
F6 = ["sh.601088", "sh.600036", "sz.000651", "sh.600900",
      "sh.601668", "sh.600938", "sz.000858"]
FIX = ROOT / "tests" / "fixtures" / "f6_baseline"

PER_STOCK_COLS = ["ts_code", "n_bars", "n_rounds", "n_rearms", "n_buys",
                  "total_invested", "realized_pnl", "final_assets",
                  "dividends", "div_pool", "price_pnl", "total_pnl",
                  "pnl_pct", "min_unreal_pct", "first_buy_date",
                  "last_buy_date", "last_status", "end_date", "name"]
BUYS_COLS = ["ts_code", "seq", "date", "price", "amount", "lots"]


def main(check: bool) -> int:
    sp, _up, tp = load_config(str(ROOT / "config" / "strategy.yaml"))
    con = connect_readonly()
    try:
        _names, data, divs = load(con, codes=F6,
                                  start=extend_start(START,
                                                     tp.price_window_years),
                                  end=END)
    finally:
        con.close()

    changed = 0
    for code in F6:
        dates, sig, exec_c, sig_low, sig_open = data[code]
        i0 = int(np.searchsorted(dates, np.datetime64(START)))
        ctx = (dates, sig, i0)
        d2, s2, e2, l2, o2 = (dates[i0:], sig[i0:], exec_c[i0:],
                              sig_low[i0:], sig_open[i0:])
        stat, *_, buys = run_stock(
            code, d2, s2, e2, divs.get(code), sp, verbose=False,
            low_sig=l2, open_sig=o2,
            first_gate=(tp.price_window_years, tp.price_pctile_max, 60),
            gate_ctx=ctx)

        row = {k: stat.get(k, code if k == "ts_code" else "") for k in
               PER_STOCK_COLS}
        row["name"] = _names.get(code, "")
        # buys: (dt, price, amt, k, for_, lots)
        new_buys = [[b[0], b[1], b[2], b[5]] for b in buys]

        old_ps = old_buys = None
        fp = FIX / code / "per_stock.csv"
        if fp.exists():
            with open(fp, encoding="utf-8") as f:
                old_ps = list(csv.DictReader(f))[0]
            fb = FIX / code / "buys.csv"
            if fb.exists():
                with open(fb, encoding="utf-8") as f:
                    old_buys = list(csv.DictReader(f))

        diffs = []
        for k in PER_STOCK_COLS[1:]:
            nv, ov = row[k], (old_ps or {}).get(k, "")
            if str(nv) != str(ov):
                diffs.append((k, ov, nv))
        nb = len(new_buys)
        ob = len(old_buys or [])
        if check:
            print(f"== {code}: buys {ob} -> {nb}"
                  + (f"  ps_diff: {diffs[:6]}{'...' if len(diffs) > 6 else ''}"
                     if diffs else ""))
            for i in range(max(nb, ob)):
                a = new_buys[i] if i < nb else None
                b = [old_buys[i][c] for c in ("date", "price", "amount",
                                              "lots")] if i < ob else None
                if a != b:
                    print(f"   buy#{i + 1}: old={b} new={a}")
            if diffs or nb != ob:
                changed += 1
        else:
            out = FIX / code
            out.mkdir(parents=True, exist_ok=True)

            def _cell(v):
                # nan/None → 空串（测试约定：fv=="" ↔ stat 为 nan）
                if v is None or (isinstance(v, float) and math.isnan(v)):
                    return ""
                return v

            with open(out / "per_stock.csv", "w", encoding="utf-8",
                      newline="") as f:
                w = csv.writer(f)
                w.writerow(PER_STOCK_COLS)
                w.writerow([_cell(row[k]) for k in PER_STOCK_COLS])
            with open(out / "buys.csv", "w", encoding="utf-8",
                      newline="") as f:
                w = csv.writer(f)
                w.writerow(BUYS_COLS)
                for i, (dt, price, amt, lots) in enumerate(new_buys, 1):
                    w.writerow([code, i, dt, price, amt, lots])
            print(f"== {code}: 写入 buys={nb}"
                  + (f" (diff: {len(diffs)} ps 字段)" if diffs else " (无变化)"))
            if diffs or nb != ob:
                changed += 1
    return 0


if __name__ == "__main__":
    sys.exit(main(check="--check" in sys.argv))
