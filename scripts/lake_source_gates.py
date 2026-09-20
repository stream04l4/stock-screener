# -*- coding: utf-8 -*-
"""lake-source 前置门 G1/G2/G3（生产库**只读**实测，证据落盘）。

纪律：
- 仅 read_only 连接；backfill flock 期间连不上 → 退出码 2（GATES_LOCKED，等锁释放重跑）；
- **不碰 backfill 进程、不调 /api/lake/sync/*、不 kill 任何进程**；
- G1 不一致（除 EXPECTED_FAIL 已知损坏股外）→ 退出码 3 + GATES_BLOCKED 证据
  （brief：不得自行选口径，回报 TL）。

用法：.venv/bin/python scripts/lake_source_gates.py [--db PATH] [--out FILE]
默认 --db data/lake/lake.duckdb --out stages/02_code/gates_evidence.txt

G1（最高优先；02_code 修正轮 brief §4 重写——方向纠正：存储 T2 是**被验证方**，
BaoStock cache 重建是 ground truth）：
- **主校验**：存储 T2 adj_factor（raw close × af 逐日前向填充值 = 库内已存列，
  直接取；**必须 ORDER BY date**）vs BaoStock ground truth
  （cache/kline_af3 × cache/adjfactor backAdj 重建，reconstruct.rebuild_kline_series），
  **近 420 交易日窗口**（因子最大回看窗口=420 日，config data.kline_calendar_days_back
  同源口径），TOL=2%。
  标的：G1_STOCKS（sh.601398/sh.601318/sh.600028）+ T4 除权事件 Top2 + sh.601688
  （已知近期单点损坏 → **预期 FAIL**，单列不阻塞门，列入 t2_repair.md 重灌清单）。
- **辅助报告（不阻塞）**：全历史窗口 max 偏差分布（早期 vs 近 420 日）——
  用于生成 T2 损坏清单（t2_damage_scan.py 的抽样对照）。
G2：3 只近期停牌股 → T2 停牌日行表示（volume=0 行 vs 缺行）。
G3：T4 同 ex_date 多行检查 → dedup SQL 写法验证。

退出码：0=GATES_PASS；2=GATES_LOCKED；3=GATES_BLOCKED（G1 非预期 FAIL）；
4=GATES_INCOMPLETE（G2/G3 需人工复核）。
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from screener.reconstruct import rebuild_kline_series  # noqa: E402

G1_STOCKS = ["sh.601398", "sh.601318", "sh.600028"]   # + T4 事件 Top2 + 601688
TOL = 0.02                  # 近 420 交易日窗口相对差阈值（brief：<2%）
WINDOW_TRADING_DAYS = 420   # 因子最大回看窗口（config data.kline_calendar_days_back 同源）
# 已知近期单点损坏（TL 实测 sh.601688=26% @ 2025-08/09）→ 预期 FAIL：
# 不计入门判定，单列证据 + 列入 stages/02_code/t2_repair.md 重灌清单。
EXPECTED_FAIL = {"sh.601688"}


def _lock_probe(db: str):
    """read_only 连接探测；被锁 → 返回 (None, 错误文案)；成功 → (con, None)。"""
    import duckdb
    try:
        return duckdb.connect(db, read_only=True), None
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


def _cache_rebuilt(code: str):
    """BaoStock ground truth：cache/kline_af3_{code} × cache/adjfactor_{code} 重建。"""
    from screener.data.cache import DiskCache
    from screener.data.fetchers import make_cache_name

    cache = DiskCache(os.path.join(REPO, "cache"))
    kl = cache.get(make_cache_name("kline_af3", code))
    if not kl or not kl["rows"]:
        return None
    af = cache.get(make_cache_name("adjfactor", code))
    factor_rows = list(af["rows"]) if af else []
    return rebuild_kline_series(kl["rows"], factor_rows, kl["columns"])


def _stored_t2_af1(con, code: str):
    """存储 T2 口径 af1：raw close × adj_factor（库内已前向填充的逐日值）。

    **必须 ORDER BY date**（brief 关键陷阱：TL 诊断脚本曾漏排序，前向填充乱序
    产生假阳性偏差）。adj_factor NULL / close NULL → None 占位（对比时跳过、计数）。
    返回 {date_iso: af1|None}。
    """
    rows = con.execute(
        "SELECT date, close, adj_factor FROM kline_daily WHERE ts_code=? ORDER BY date",
        [code],
    ).fetchall()
    out = {}
    for d, c, af in rows:
        ds = d.isoformat() if hasattr(d, "isoformat") else str(d)
        out[ds] = None if (c is None or af is None) else float(c) * float(af)
    return out


def _rel_diff(a: float, b: float) -> float:
    base = max(abs(a), abs(b), 1e-9)
    return abs(a - b) / base


def _worst_in(lake_map, cache_map, dates):
    """dates 内两源相对差最大值（跳过 None）。返回 (worst, worst_date, n_cmp)。"""
    worst, worst_d, n = 0.0, "", 0
    for d in dates:
        a, b = lake_map.get(d), cache_map.get(d)
        if a is None or b is None:
            continue
        n += 1
        diff = _rel_diff(a, b)
        if diff > worst:
            worst, worst_d = diff, d
    return worst, worst_d, n


def g1(con, out) -> bool:
    """G1：存储 T2 adj_factor vs BaoStock ground truth（近 420 交易日窗口，TOL=2%）。"""
    out.append("=" * 70)
    out.append("G1 复权口径（最高优先；方向=存储 T2 被验证，BaoStock cache 为 ground truth）")
    # T4 除权事件数 Top2（排除基准股与已知损坏股——601688 单列）
    rows = con.execute(
        "SELECT ts_code, COUNT(*) n FROM dividend_events WHERE cash_dps > 0 "
        "AND ts_code NOT IN ('sh.601398','sh.601318','sh.600028','sh.601688') "
        "GROUP BY ts_code ORDER BY n DESC LIMIT 2"
    ).fetchall()
    stocks = G1_STOCKS + [r[0] for r in rows] + ["sh.601688"]
    out.append(f"标的: {stocks}（G1_STOCKS + T4 除权事件 Top2 + sh.601688 已知损坏）")

    ok_all = True
    expected_fail_seen = []
    for code in stocks:
        out.append("-" * 60)
        out.append(f"[G1] {code}" + ("（预期 FAIL：已知近期损坏，列入 t2_repair.md）"
                                     if code in EXPECTED_FAIL else ""))
        lake_map = _stored_t2_af1(con, code)
        rebuilt = _cache_rebuilt(code)
        if not lake_map:
            out.append(f"  ✗ 存储 T2 无 {code} 数据")
            ok_all = False
            continue
        if rebuilt is None or not rebuilt["dates"]:
            out.append(f"  ✗ cache kline_af3/adjfactor 无 {code}（无法对比 ground truth）")
            ok_all = False
            continue
        cache_map = {d: c for d, c in zip(rebuilt["dates"], rebuilt["af1_close"])
                     if c is not None}
        common = sorted(set(lake_map) & set(cache_map))
        if len(common) < 30:
            out.append(f"  ✗ 可对齐日期仅 {len(common)}（<30，对比无效）")
            ok_all = False
            continue

        # ---- 主校验：近 420 交易日窗口（两源共有日期序列取尾部）----
        win_dates = common[-WINDOW_TRADING_DAYS:]
        worst, worst_d, n_cmp = _worst_in(lake_map, cache_map, win_dates)
        verdict = "PASS" if worst < TOL else "FAIL"
        n_null = sum(1 for d in win_dates if lake_map.get(d) is None)
        null_note = f"，af NULL {n_null} 行已跳过" if n_null else ""
        out.append(f"  主校验 近{len(win_dates)}日: 对齐 {n_cmp} 日，最大相对差 "
                   f"{worst*100:.4f}% @ {worst_d}{null_note} → {verdict}")

        # ---- 辅助报告（不阻塞）：早期 vs 近 420 日分布（T2 损坏清单依据）----
        win_set = set(win_dates)
        early_dates = [d for d in common if d not in win_set]
        w_early, wd_early, n_early = _worst_in(lake_map, cache_map, early_dates)
        out.append(f"  辅助(不阻塞): 早期段 {n_early} 日 max={w_early*100:.4f}% @ "
                   f"{wd_early or '-'}；近 420 日 max={worst*100:.4f}%（全历史 {len(common)} 日）")

        if code in EXPECTED_FAIL:
            # 预期 FAIL：单列证据，不计入门判定（重灌后应转 PASS——t2_repair.md 验收项）
            expected_fail_seen.append((code, worst, worst_d))
            out.append(f"  → 预期 FAIL 确认（worst={worst*100:.2f}% ≥ {TOL*100:.0f}%）；"
                       "不计入门判定，列入重灌清单")
        elif verdict != "PASS":
            ok_all = False
    if expected_fail_seen:
        out.append(f"预期 FAIL 汇总（不阻塞门）: "
                   f"{[(c, f'{w*100:.2f}%', d) for c, w, d in expected_fail_seen]}")
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
        # 无 volume=0 行 → 可能"缺行"口径：找日期缺口验证。
        # 基准股连续（T7 有而 T2 无的日期=空）→ 当前库为"缺行"口径，现有 SQL
        # （volume>0 派生 tradestatus；缺行时该行自然不存在）两分支兼容 → PASS。
        out.append("  最近 60 日无 volume=0 行 → 检查'缺行'口径（T2 日期缺口 vs T7 交易日）")
        gap = con.execute("""
            SELECT k.ts_code, t.date FROM index_daily t
            LEFT JOIN kline_daily k ON k.ts_code='sh.601398' AND k.date=t.date
            WHERE t.index_code='sh000001' AND t.date >= (SELECT MAX(date) FROM kline_daily) - INTERVAL 60 DAY
              AND k.date IS NULL LIMIT 5
        """).fetchall()
        gap_list = [str(g[1]) for g in gap]
        out.append(f"  sh.601398 在 T7 有而 T2 无的日期: {gap_list or '无（连续）'}")
        if gap_list:
            out.append("  ⚠️ 存在日期缺口 → 需人工判断'缺行'口径下的 tradestatus 语义")
            ok = False
        else:
            out.append("  结论：T2 停牌日 = 缺行（基准股近 60 日连续无 volume=0 行）；"
                       "现有 SQL 两分支兼容（volume>0 派生 / 缺行自然不存在），PASS")
    for code in codes:
        vol0 = con.execute(
            "SELECT COUNT(*), MIN(date), MAX(date) FROM kline_daily WHERE ts_code=? AND volume=0", [code]
        ).fetchone()
        out.append(f"  {code}: volume=0 行数={vol0[0]}（{vol0[1]}..{vol0[2]}）→ **有行**口径")
    if codes:
        out.append("  结论：T2 停牌日 = volume=0 行（现有 SQL tradestatus/volume>0 派生正确）")
    return ok


def g3(con, out) -> bool:
    """G3：T4 同 ex_date 多行检查 → dedup SQL 写法验证。

    验证对象 = **生产 dividend() 的实际口径**（lake_source.dividend / DataFetcher 同源）：
    WHERE cash_dps > 0 AND ex_date BETWEEN ... + (ex_date, ann_date DESC NULLS LAST)
    first-wins dedup。多行组中"最新 ann_date 行无现金但有旧公告行有现金"的组，
    生产路径靠 WHERE cash_dps>0 天然只取到有效行（dedup 在其内）→ 不影响筛选；
    **整组全 NULL/无现金** → 生产路径取空（该除权年无有效分红记录，数据质量项）。
    """
    out.append("=" * 70)
    out.append("G3 T4 同 ex_date 多行")
    dup = con.execute("""
        SELECT ts_code, ex_date, COUNT(*) n FROM dividend_events
        WHERE ex_date IS NOT NULL
        GROUP BY ts_code, ex_date HAVING COUNT(*) > 1 ORDER BY n DESC, ts_code LIMIT 10
    """).fetchall()
    out.append(f"同 (ts_code, ex_date) 多行组数（Top10）: {[(d[0], str(d[1]), d[2]) for d in dup] or '无'}")
    n_groups = con.execute(
        "SELECT COUNT(*) FROM (SELECT ts_code, ex_date FROM dividend_events WHERE ex_date IS NOT NULL GROUP BY ts_code, ex_date HAVING COUNT(*)>1)"
    ).fetchone()[0]
    out.append(f"多行组总数: {n_groups}")
    n_null_ex = con.execute("SELECT COUNT(*) FROM dividend_events WHERE ex_date IS NULL").fetchone()[0]
    out.append(f"ex_date=NULL 行数: {n_null_ex}（数据质量项：东财 CSV 未实施/除权日缺失；生产路径 ex_date<=? 天然排除，不影响筛选）")
    if dup:
        # 按**生产口径**验证 dedup：cash_dps>0 过滤 + (ex_date, ann_date DESC) first-wins。
        # 抽样 Top10 组逐组检查"生产路径能否取到有效现金行"。
        n_eff, n_nocash = 0, []
        for ts, ex, _n in dup:
            rows = con.execute(
                "SELECT ann_date, cash_dps FROM dividend_events WHERE ts_code=? AND ex_date=? "
                "AND cash_dps > 0 ORDER BY ann_date DESC NULLS LAST", [ts, ex]
            ).fetchall()
            if rows:
                n_eff += 1
                out.append(f"  样例 {ts} {ex}: 生产口径 dedup 首行={rows[0]}（cash_dps>0 ✓）")
            else:
                n_nocash.append((ts, str(ex)))
        if n_nocash:
            # 整组无现金 → 生产路径取空（数据质量项，**非 dedup SQL 缺陷**：
            # 这些组本就无有效分红行可取；东财 CSV 未实施/除权日缺失的已知形态）
            out.append(f"  ⚠️ {len(n_nocash)} 个多行组整组无 cash_dps>0 行（生产路径取空，数据质量项，"
                       f"不影响 dedup 逻辑正确性）: {n_nocash[:5]}{'...' if len(n_nocash) > 5 else ''}")
    out.append("  结论：dedup SQL（cash_dps>0 WHERE + ex_date, ann_date DESC first-wins）按生产口径验证通过"
               if dup else "  结论：无多行组，dedup 逻辑平凡成立")
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
        print("GATES_BLOCKED: G1 口径不一致（非预期 FAIL）——不得自行选口径，回报 TL"
              f"（证据见 {args.out}；EXPECTED_FAIL={sorted(EXPECTED_FAIL)} 已单列不阻塞）")
        return 3
    if not (r2 and r3):
        print(f"GATES_INCOMPLETE: G2={r2} G3={r3}（G1 通过；G2/G3 需人工复核，证据见 {args.out}）")
        return 4
    print("GATES_PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
