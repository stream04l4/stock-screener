# -*- coding: utf-8 -*-
"""lake-source Part B 前置门 G1/G2/G3（生产库**只读**实测，证据落盘）。

纪律：
- 仅 read_only 连接；backfill flock 期间连不上 → 退出码 2（GATES_LOCKED，等锁释放重跑）；
- **不碰 backfill 进程、不调 /api/lake/sync/*、不 kill 任何进程**；
- G1 不一致 → 退出码 3 + GATES_BLOCKED 证据（brief：不得自行选口径，回报 TL）。

用法：.venv/bin/python scripts/lake_source_gates.py [--db PATH] [--out FILE]
默认 --db data/lake/lake.duckdb --out stages/02_code/gates_evidence.txt

G1（最高优先）：sh.601398 + 2 只高除权次数股，对比
  ① kline_daily_hfq.close（raw×af，lake view）
  ② cache/kline_af3 × cache/adjfactor backAdj 重建值（BaoStock 口径，reconstruct.rebuild_kline_series）
  全窗口相对差 <0.5%；并验证 r_event（T4 ex_date + close 比值）推导因子序列与 ①一致。
G2：3 只近期停牌股 → T2 停牌日行表示（volume=0 行 vs 缺行）。
G3：T4 同 ex_date 多行检查 → dedup SQL 写法验证。
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from screener.reconstruct import rebuild_kline_series  # noqa: E402

G1_STOCKS = ["sh.601398"]   # + 运行时按 T4 事件数自动补 2 只高除权次数股
TOL = 0.005                 # 全窗口相对差阈值（brief：<0.5%）


def _lock_probe(db: str):
    """read_only 连接探测；被锁 → 返回 (None, 错误文案)；成功 → (con, None)。"""
    import duckdb
    try:
        return duckdb.connect(db, read_only=True), None
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


def _cache_rebuilt(code: str):
    """② BaoStock 口径重建：cache/kline_af3_{code} × cache/adjfactor_{code}。"""
    from screener.data.cache import DiskCache
    from screener.data.fetchers import make_cache_name

    cache = DiskCache(os.path.join(REPO, "cache"))
    kl = cache.get(make_cache_name("kline_af3", code))
    if not kl or not kl["rows"]:
        return None
    af = cache.get(make_cache_name("adjfactor", code))
    factor_rows = list(af["rows"]) if af else []
    return rebuild_kline_series(kl["rows"], factor_rows, kl["columns"])


def _rel_diff(a: float, b: float) -> float:
    base = max(abs(a), abs(b), 1e-9)
    return abs(a - b) / base


def g1(con, out) -> bool:
    """G1：hfq view vs cache 重建 + r_event 推导一致性。"""
    out.append("=" * 70)
    out.append("G1 复权口径（最高优先）")
    # 高除权次数股：T4 cash_dps>0 事件数 Top2（排除基准股）
    rows = con.execute(
        "SELECT ts_code, COUNT(*) n FROM dividend_events WHERE cash_dps > 0 "
        "AND ts_code != 'sh.601398' GROUP BY ts_code ORDER BY n DESC LIMIT 2"
    ).fetchall()
    stocks = G1_STOCKS + [r[0] for r in rows]
    out.append(f"标的: {stocks}（后 2 只=T4 除权事件数 Top2）")

    ok_all = True
    for code in stocks:
        out.append("-" * 60)
        out.append(f"[G1] {code}")
        # ① lake hfq view（raw×af）
        lake_rows = con.execute(
            'SELECT date, "close" FROM kline_daily_hfq WHERE ts_code=? ORDER BY date', [code]
        ).fetchall()
        if not lake_rows:
            out.append(f"  ✗ kline_daily_hfq 无 {code} 数据")
            ok_all = False
            continue
        # ② cache 重建（BaoStock 口径）
        rebuilt = _cache_rebuilt(code)
        if rebuilt is None or not rebuilt["dates"]:
            out.append(f"  ✗ cache kline_af3/adjfactor 无 {code}（无法对比 BaoStock 口径）")
            ok_all = False
            continue
        # 对齐日期交集（PIT：只比 lake 有 & cache 有的日期）。
        # lake hfq close=NULL = 该股早期 adj_factor 缺失（历史源未提供，如 sh.601398 23 行/
        # sh.600018 2000-2006）——hfq view 是 Web 图表用派生视图，生产筛选路径不读它
        # （LakeDataFetcher 读 raw + r_event 推导因子），NULL 行跳过对比并计数报告。
        lake_map = {d.isoformat() if hasattr(d, "isoformat") else str(d): c
                    for d, c in lake_rows if c is not None}
        n_lake_null = sum(1 for _, c in lake_rows if c is None)
        cache_map = {d: c for d, c in zip(rebuilt["dates"], rebuilt["af1_close"]) if c is not None}
        common = sorted(set(lake_map) & set(cache_map))
        if len(common) < 30:
            out.append(f"  ✗ 可对齐日期仅 {len(common)}（<30，对比无效）")
            ok_all = False
            continue
        worst, worst_d = 0.0, ""
        for d in common:
            diff = _rel_diff(lake_map[d], cache_map[d])
            if diff > worst:
                worst, worst_d = diff, d
        verdict = "PASS" if worst < TOL else "FAIL"
        if worst >= TOL:
            ok_all = False
        null_note = f"，hfq NULL {n_lake_null} 行已跳过" if n_lake_null else ""
        out.append(f"  ①hfq vs ②cache重建: 对齐 {len(common)} 日，最大相对差 {worst*100:.4f}% @ {worst_d}{null_note} → {verdict}")

        # r_event 推导因子序列 vs ①（G1 后半：验证 LakeDataFetcher._derived_factor_rows 同构）
        from screener.data.lake_source import LakeDataFetcher
        f = LakeDataFetcher.__new__(LakeDataFetcher)   # 不触发 __init__（client/cache 无关）
        f._conn = con
        f.run_day = date.today()
        f._resolved_ds_cfg = {"exdate_detector": {"factor_sanity_cap_pct": 30}}
        ev_rows = f._derived_factor_rows(code)
        # 用推导因子重建 af1，与 ① hfq 比
        raw_rows = con.execute(
            "SELECT date, close FROM kline_daily WHERE ts_code=? ORDER BY date", [code]
        ).fetchall()
        raw_map = {d.isoformat(): c for d, c in raw_rows}
        rebuilt2 = rebuild_kline_series(
            [[d, code, "" if c is None else f"{c:.4f}", "0", "1"] for d, c in sorted(raw_map.items())],
            ev_rows, ["date", "code", "close", "isST", "tradestatus"])
        worst2, worst2_d = 0.0, ""
        n_cmp = 0
        for d, a1 in zip(rebuilt2["dates"], rebuilt2["af1_close"]):
            if a1 is None or d not in lake_map:
                continue
            n_cmp += 1
            diff = _rel_diff(a1, lake_map[d])
            if diff > worst2:
                worst2, worst2_d = diff, d
        verdict2 = "PASS" if (n_cmp >= 30 and worst2 < TOL) else "FAIL"
        if n_cmp < 30 or worst2 >= TOL:
            ok_all = False
        out.append(f"  r_event推导 vs ①hfq: 对齐 {n_cmp} 日，最大相对差 {worst2*100:.4f}% @ {worst2_d} → {verdict2}")
        out.append(f"  推导因子事件数: {len(ev_rows)}")
    return ok_all


def g2(con, out) -> bool:
    """G2：停牌日行表示（volume=0 行 vs 缺行）。抽 3 只近期停牌股。"""
    out.append("=" * 70)
    out.append("G2 停牌日行表示")
    # 近期停牌股 = T2 中最近 60 日内存在 volume=0 行的股票（若"缺行"口径则此查询为空→改查缺口）
    rows = con.execute("""
        SELECT k.ts_code, k.date FROM kline_daily k
        WHERE k.volume = 0 AND k.date >= (SELECT MAX(date) FROM kline_daily) - INTERVAL 60 DAY
        ORDER BY k.date DESC LIMIT 20
    """).fetchall()
    codes = []
    for ts, _d in rows:
        if ts not in codes:
            codes.append(ts)
        if len(codes) >= 3:
            break
    ok = True
    if not codes:
        # 无 volume=0 行 → 可能"缺行"口径：找日期缺口验证
        out.append("  最近 60 日无 volume=0 行 → 检查'缺行'口径（T2 日期缺口 vs T7 交易日）")
        gap = con.execute("""
            SELECT k.ts_code, t.date FROM index_daily t
            LEFT JOIN kline_daily k ON k.ts_code='sh.601398' AND k.date=t.date
            WHERE t.index_code='sh000001' AND t.date >= (SELECT MAX(date) FROM kline_daily) - INTERVAL 60 DAY
              AND k.date IS NULL LIMIT 5
        """).fetchall()
        out.append(f"  sh.601398 在 T7 有而 T2 无的日期: {[str(g[1]) for g in gap] or '无（连续）'}")
    for code in codes:
        vol0 = con.execute(
            "SELECT COUNT(*), MIN(date), MAX(date) FROM kline_daily WHERE ts_code=? AND volume=0", [code]
        ).fetchone()
        out.append(f"  {code}: volume=0 行数={vol0[0]}（{vol0[1]}..{vol0[2]}）→ **有行**口径")
    if codes:
        out.append("  结论：T2 停牌日 = volume=0 行（现有 SQL tradestatus/volume>0 派生正确）")
    else:
        ok = False   # 无法确认 → 需人工判断（但缺行口径下现有 SQL 也兼容，见模块注释）
    return ok


def g3(con, out) -> bool:
    """G3：T4 同 ex_date 多行检查 → dedup SQL 写法验证。"""
    out.append("=" * 70)
    out.append("G3 T4 同 ex_date 多行")
    dup = con.execute("""
        SELECT ts_code, ex_date, COUNT(*) n FROM dividend_events
        WHERE ex_date IS NOT NULL
        GROUP BY ts_code, ex_date HAVING COUNT(*) > 1 ORDER BY n DESC LIMIT 10
    """).fetchall()
    out.append(f"同 (ts_code, ex_date) 多行组数（Top10）: {[(d[0], str(d[1]), d[2]) for d in dup] or '无'}")
    n_groups = con.execute(
        "SELECT COUNT(*) FROM (SELECT ts_code, ex_date FROM dividend_events WHERE ex_date IS NOT NULL GROUP BY ts_code, ex_date HAVING COUNT(*)>1)"
    ).fetchone()[0]
    out.append(f"多行组总数: {n_groups}")
    n_null_ex = con.execute("SELECT COUNT(*) FROM dividend_events WHERE ex_date IS NULL").fetchone()[0]
    out.append(f"ex_date=NULL 行数: {n_null_ex}（数据质量项：东财 CSV 未实施/除权日缺失；生产路径 ex_date<=? 天然排除，不影响筛选）")
    if dup:
        # 验证 dedup SQL（ann_date DESC first-wins）能取到 cash_dps>0 行
        ts, ex = dup[0][0], dup[0][1]
        rows = con.execute(
            "SELECT ann_date, cash_dps FROM dividend_events WHERE ts_code=? AND ex_date=? "
            "ORDER BY ann_date DESC NULLS LAST", [ts, ex]
        ).fetchall()
        out.append(f"  样例 {ts} {ex}: dedup 首行={rows[0]}（应 cash_dps>0）")
        if rows and (rows[0][1] is None or rows[0][1] <= 0):
            out.append("  ⚠️ 首行无现金 → 需调整 dedup 排序（cash_dps>0 优先）")
            return False
    out.append("  结论：dedup SQL（ex_date, ann_date DESC first-wins + cash_dps>0 过滤）验证通过" if dup else "  结论：无多行组，dedup 逻辑平凡成立")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(REPO, "data/lake/lake.duckdb"))
    ap.add_argument("--out", default=os.path.join(REPO, "stages/02_code/gates_evidence.txt"))
    args = ap.parse_args()

    con, err = _lock_probe(args.db)
    if con is None:
        print(f"GATES_LOCKED: 生产库 read_only 连接失败（backfill flock？）\n{err}")
        return 2

    out = [f"lake-source G 门证据 · {date.today().isoformat()} · db={args.db}"]
    try:
        r1 = g1(con, out)
        r2 = g2(con, out)
        r3 = g3(con, out)
    finally:
        con.close()

    out.append("=" * 70)
    out.append(f"汇总: G1={'PASS' if r1 else 'FAIL'} G2={'PASS' if r2 else 'FAIL'} G3={'PASS' if r3 else 'FAIL'}")
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    print("\n".join(out[-6:]))
    if not r1:
        print("GATES_BLOCKED: G1 口径不一致——不得自行选口径，回报 TL（证据见 " + args.out + ")")
        return 3
    if not (r2 and r3):
        print(f"GATES_INCOMPLETE: G2={r2} G3={r3}（G1 通过；G2/G3 需人工复核，证据见 {args.out}）")
        return 4
    print("GATES_PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
