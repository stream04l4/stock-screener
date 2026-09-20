# -*- coding: utf-8 -*-
"""lake-source Part B 灰度 diff：lake vs tencent 同 run_day（brief：top50 重合 + funnel 计数）。

纪律：
- 两次运行都写**独立临时 output_dir**，不碰生产 output/；
- do_crosscheck=False（交叉验证是可选校验路径，非主计算；灰度比的是主链路）；
- tencent 侧走现有生产管线（快照+缓存），lake 侧零网络——请求面差异正是本次交付内容；
- **D1' 约束**：run_day 必须 ≥2025-07（T7 日历覆盖起点）；更早的日期 lake 侧无日历 → 拒绝。

用法：.venv/bin/python scripts/lake_source_grayscale.py --date YYYY-MM-DD [--lake-db PATH] [--out FILE]
退出码：0=diff 报告已生成（是否"通过"由 TL/人工按阈值判定）；2=lake 库被锁；1=运行失败。
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from datetime import date

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def _run(primary: str, lake_db: str | None, run_date: date, out_dir: str):
    from screener.config import load_config
    from screener.screener import run_screener

    cfg = load_config(os.path.join(REPO, "config", "strategy.yaml"))
    cfg["datasource"]["primary"] = primary
    cfg.setdefault("canonical", {})["enabled"] = False   # 灰度不落 raw（隔离）
    cache_dir = os.path.join(out_dir, "cache")
    cfg["data"]["cache_dir"] = cache_dir
    if primary == "lake":
        if lake_db:
            os.environ["SCREENER_LAKE_DB"] = lake_db
    return run_screener(cfg, run_date, output_dir=os.path.join(out_dir, "out"),
                        do_crosscheck=False)


def _top_codes(result, n: int = 50):
    if result.mode == "zscore":
        scored = sorted(result.scored, key=lambda s: s.total_score, reverse=True)
        return [s.code for s in scored[:n] if s.top_n_selected]
    # legacy：candidates pass_all
    try:
        import pandas as pd
        df = result.candidates[result.candidates["pass_all"]]
        return df["code"].head(n).tolist()
    except Exception:  # noqa: BLE001
        return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="运行日 YYYY-MM-DD（必须 >=2025-07，D1'）")
    ap.add_argument("--lake-db", default=os.path.join(REPO, "data/lake", "lake.duckdb"))
    ap.add_argument("--out", default=os.path.join(REPO, "stages/02_code/grayscale_diff.txt"))
    args = ap.parse_args()

    run_date = date.fromisoformat(args.date)
    if run_date < date(2025, 7, 1):
        print(f"拒绝：run_day {args.date} < 2025-07（D1'：T7 日历覆盖起点；backfill 回填前不得灰度）")
        return 1

    work = tempfile.mkdtemp(prefix="lake_grayscale_")
    out = [f"lake-source 灰度 diff · run_day={args.date} · lake_db={args.lake_db}"]
    try:
        out.append("运行 lake 侧（零网络）...")
        res_lake = _run("lake", args.lake_db, run_date, os.path.join(work, "lake"))
        out.append(f"  lake: run_day={res_lake.run_day} funnel={res_lake.funnel}")
        out.append("运行 tencent 侧（生产管线）...")
        res_tc = _run("tencent", None, run_date, os.path.join(work, "tencent"))
        out.append(f"  tencent: run_day={res_tc.run_day} funnel={res_tc.funnel}")

        top_lake = _top_codes(res_lake)
        top_tc = _top_codes(res_tc)
        inter = set(top_lake) & set(top_tc)
        out.append("=" * 70)
        out.append(f"Top50: lake={len(top_lake)} tencent={len(top_tc)} 交集={len(inter)} "
                   f"重合率(按tencent)= {len(inter)/max(len(top_tc),1)*100:.1f}%")
        out.append(f"仅 lake: {sorted(set(top_lake) - set(top_tc))}")
        out.append(f"仅 tencent: {sorted(set(top_tc) - set(top_lake))}")

        out.append("-" * 70)
        out.append("funnel 对比:")
        keys = sorted(set(res_lake.funnel) | set(res_tc.funnel))
        for k in keys:
            a, b = res_lake.funnel.get(k), res_tc.funnel.get(k)
            flag = "" if a == b else f"  ← Δ={a if a is not None else '?'} vs {b if b is not None else '?'}"
            out.append(f"  {k}: lake={a} tencent={b}{flag}")
        out.append("-" * 70)
        out.append(f"ST剔除: lake={res_lake.st_excluded_count} tencent={res_tc.st_excluded_count}")
        out.append(f"上市不足: lake={res_lake.insufficient_kline_count} tencent={res_tc.insufficient_kline_count}")
        out.append(f"BaoStock请求: lake={res_lake.baostock_requests} tencent={res_tc.baostock_requests}")
    except Exception as exc:  # noqa: BLE001
        import traceback
        out.append("灰度运行失败:\n" + traceback.format_exc())
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write("\n".join(out) + "\n")
        print(f"GRAYSCALE_FAILED（证据已落盘 {args.out}）")
        return 1
    finally:
        os.environ.pop("SCREENER_LAKE_DB", None)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    print(f"灰度 diff 已生成: {args.out}")
    print(f"Top50 交集={len(inter)}（判定阈值由 TL/人工定：建议 >=90% 且 funnel Δ 可解释）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
