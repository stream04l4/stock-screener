#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生产数据湖一次性修复与验收工具（缺陷 A 幻影反盖章 + 缺陷 B dividend_events 补齐）。

用法：
  # 1) 仅预演（不修改库，检查影响面 + 输出报告）
  .venv/bin/python scripts/repair_af_and_dividends.py

  # 2) 自动备份 + 补齐 2026-01-01~今日分红 + 代数反盖章 + 全史 rebase + 抽样校验 + 重算选股与 Diff
  .venv/bin/python scripts/repair_af_and_dividends.py --apply
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import math
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import af_rebase  # noqa: E402

log = logging.getLogger("repair_af_and_dividends")

# 缺陷 B 中已知 >2% 官方步长曾被抹平的 9 只重点股票
KEY_RESTORE_DIV_CODES = [
    "sh.601229",  # 上海银行 1.0324
    "sz.000807",  # 云铝股份 1.0353
    "sh.603511",  # 爱慕股份 1.0233
    "sh.603279",  # 景津装备 1.0380
    "sh.601128",  # 常熟银行 1.0207
    "sz.002091",  # 江苏国泰 1.0268
    "sh.603866",  # 桃李面包 1.0305
    "sh.603167",  # 渤海轮渡 1.0442
    "sh.601998",  # 中信银行 1.0217
]
# 被价格比覆盖失真的 10 送 4 代表股
KEY_BONUS_DISTORT_CODES = ["sz.300873", "sz.300857"]
# 幻影盖章代表股（含基础池/候选池受影响股）
KEY_PHANTOM_CODES = ["sz.301219", "sh.601857", "sh.600392", "sh.601995"]
DEFAULT_TUSHARE_SAMPLES = ["sz.301219", "sh.601857", "sh.601998"]


def _today_beijing() -> str:
    tz = dt.timezone(dt.timedelta(hours=8))
    return dt.datetime.now(tz).date().isoformat()


def _safe_load_table(con, table: str) -> List[Dict[str, Any]]:
    try:
        cur = con.execute(f"SELECT * FROM {table}")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception:  # noqa: BLE001
        return []


def snapshot_state(con, focus_codes: Optional[List[str]] = None,
                   recent_since: str = "2026-10-01") -> Dict[str, Any]:
    """捕获修复前后可比的状态快照（分红表 / 十年回测 / 时机分位 / 重点股大边界）。"""
    div_row = con.execute(
        "SELECT COUNT(*), CAST(MAX(ex_date) AS VARCHAR) FROM dividend_events"
    ).fetchone()
    sel_rows = _safe_load_table(con, "selection_backtest_10y")
    tim_rows = _safe_load_table(con, "selection_timing")
    sel_by_code = {str(r["ts_code"]): r for r in sel_rows}
    tim_by_code = {str(r["ts_code"]): r for r in tim_rows}
    watchlist = sorted(
        str(r["ts_code"]) for r in tim_rows if bool(r.get("in_watchlist"))
    )

    codes = focus_codes or sorted(set(KEY_RESTORE_DIV_CODES + KEY_BONUS_DISTORT_CODES + KEY_PHANTOM_CODES))
    boundaries: Dict[str, List[Dict[str, Any]]] = {}
    if codes:
        ph = ",".join("?" * len(codes))
        brows = con.execute(f"""
            SELECT t.ts_code, CAST(t.d0 AS VARCHAR), CAST(t.d1 AS VARCHAR),
                   t.c0, t.c1, t.af0, t.af1, (t.af1 / t.af0) AS af_move
            FROM (
                SELECT ts_code, date AS d1, LAG(date) OVER w AS d0,
                       close AS c1, LAG(close) OVER w AS c0,
                       adj_factor AS af1, LAG(adj_factor) OVER w AS af0
                FROM kline_daily
                WHERE ts_code IN ({ph}) AND adj_factor IS NOT NULL AND adj_factor > 0
                  AND close IS NOT NULL AND close > 0
                WINDOW w AS (PARTITION BY ts_code ORDER BY date)
            ) t
            WHERE t.d0 IS NOT NULL AND t.d1 >= ?
              AND ABS(LN(t.af1 / t.af0)) > 0.004
            ORDER BY t.ts_code, t.d1
        """, codes + [recent_since]).fetchall()
        for c, d0, d1, c0, c1, af0, af1, mv in brows:
            boundaries.setdefault(str(c), []).append({
                "d0": str(d0)[:10], "d1": str(d1)[:10],
                "af_move": round(float(mv), 6),
                "price_ratio": round(float(c0) / float(c1), 6),
            })

    return {
        "dividend_rows": int(div_row[0] or 0),
        "dividend_max_ex_date": str(div_row[1])[:10] if div_row[1] else None,
        "selection_n": len(sel_by_code),
        "selection_by_code": sel_by_code,
        "timing_n": len(tim_by_code),
        "timing_by_code": tim_by_code,
        "watchlist": watchlist,
        "focus_boundaries": boundaries,
    }


def verify_tushare_samples(con, sample_codes: List[str],
                           since_date: str = "2026-10-01",
                           tol: float = af_rebase.DEFAULT_TOL) -> Dict[str, Any]:
    """低频抽样对比湖内 adj_factor 相邻日比与 tushare 官方相邻日比（检查平坦性与除权一致性）。"""
    try:
        from lake.ingest import tushare_ingest as ti
        if not ti.tushare_available():
            return {"skipped": True, "reason": "tushare_unavailable", "samples": []}
    except Exception as exc:  # noqa: BLE001
        return {"skipped": True, "reason": str(exc)[:100], "samples": []}

    results: List[Dict[str, Any]] = []
    all_ok = True
    for code in sample_codes:
        if "." not in code:
            continue
        mkt, num = code.split(".", 1)
        ts_fmt = f"{num}.{mkt.upper()}"
        try:
            ts_rows = ti.fetch_adj_factor(ts_fmt)
        except Exception as exc:  # noqa: BLE001
            results.append({"ts_code": code, "ok": None, "error": str(exc)[:100]})
            continue
        ts_map: Dict[str, float] = {}
        for r in ts_rows or []:
            d = str(r.get("trade_date") or "").strip()
            if len(d) == 8 and d.isdigit():
                d = f"{d[:4]}-{d[4:6]}-{d[6:]}"
            af = r.get("adj_factor")
            if d >= since_date and af is not None and float(af) > 0:
                ts_map[d[:10]] = float(af)
        lake_rows = con.execute(
            "SELECT CAST(date AS VARCHAR), adj_factor FROM kline_daily "
            "WHERE ts_code=? AND date >= ? AND adj_factor IS NOT NULL AND adj_factor > 0 "
            "ORDER BY date", [code, since_date]).fetchall()
        lake_map = {str(d)[:10]: float(a) for d, a in lake_rows}
        common = sorted(set(lake_map) & set(ts_map))
        diffs: List[Dict[str, Any]] = []
        for i in range(1, len(common)):
            d0, d1 = common[i - 1], common[i]
            l_mv = lake_map[d1] / lake_map[d0]
            t_mv = ts_map[d1] / ts_map[d0]
            if abs(math.log(l_mv / t_mv)) > tol:
                diffs.append({"d0": d0, "d1": d1, "lake_move": round(l_mv, 6),
                              "tushare_move": round(t_mv, 6)})
        ok = (len(common) >= 2) and (len(diffs) == 0)
        if not ok:
            all_ok = False
        results.append({"ts_code": code, "common_days": len(common),
                        "ok": ok, "diffs": diffs[:5]})
    return {"skipped": False, "all_ok": all_ok, "samples": results}


def _recompute_selection_and_timing(con, config_path: Optional[str] = None) -> Dict[str, Any]:
    from martingale.config import load_config
    from martingale.selection import load_manual_pool, refresh_selection
    from martingale.timing import refresh_timing

    cfg_p = config_path or os.path.join(PROJECT_ROOT, "config", "strategy.yaml")
    sp, up, tp = load_config(cfg_p)
    manual = load_manual_pool()
    sel = refresh_selection(con, sp, up, verbose=True, tp=tp)
    tim = refresh_timing(con, up, tp, manual=manual)
    return {"selection": sel, "timing": tim}


def _format_diff_report(before: Dict[str, Any], after: Dict[str, Any],
                        summary: Dict[str, Any]) -> str:
    lines = [
        f"# 数据湖 AF 幻影反盖章与分红事件补齐报告",
        f"",
        f"- 执行模式: `{'APPLY (已写入)' if summary['apply'] else 'DRY-RUN (仅预演)'}`",
        f"- 数据库路径: `{summary['db_path']}`",
        f"- 备份文件: `{summary.get('backup_path') or '无'}`",
        f"",
        f"## 1. dividend_events 补齐摘要",
        f"- 修复前: {before['dividend_rows']} 行, MAX(ex_date) = `{before['dividend_max_ex_date']}`",
        f"- 补齐区间: `{summary['div_sync'].get('start_date')} ~ {summary['div_sync'].get('end_date')}`"
        f"（抓取 {summary['div_sync'].get('fetched', 0)} 条，写入 {summary['div_sync'].get('written', 0)} 条）",
        f"- 修复后: {after['dividend_rows']} 行, MAX(ex_date) = `{after['dividend_max_ex_date']}`",
        f"",
        f"## 2. 缺陷 A：暴跌日幻影反盖章",
        f"- 扫描起算日: `{summary['phantom_scan'].get('since_date')}`",
        f"- 检出幻影边界: {len(summary['phantom_scan'].get('phantoms', []))} 处（涉及 {summary['phantom_scan'].get('stocks_affected', 0)} 只股）",
        f"- 检出 10送4 等被价格比覆盖真除权: {len(summary['phantom_scan'].get('event_distortions', []))} 处",
        f"- 反盖章结果: {summary['un_stamp'].get('stocks', 0)} 股 / {summary['un_stamp'].get('boundaries', 0)} 边界 / {summary['un_stamp'].get('rows_rewritten', 0)} 行",
        f"",
        f"## 3. 全史 Rebase 与被抹平真除权恢复",
        f"- 全史 repair_all_af: 对齐 {summary['repair_all'].get('stocks_rebased', 0)} 股, {summary['repair_all'].get('jump_pairs', 0)} 跳变, {summary['repair_all'].get('rows_rewritten', 0)} 行, suspected(保留源值) {summary['repair_all'].get('suspected_pairs', 0)} 处",
        f"- 中小步长真除权(0.5%~2%)恢复: {summary['restore_small'].get('stocks', 0)} 股 / {summary['restore_small'].get('boundaries', 0)} 边界 / {summary['restore_small'].get('rows_rewritten', 0)} 行",
        f"- 验收重扫残余跳变: {summary['verify_jumps_after']} 处（非 suspected={summary['verify_non_suspected_after']}）; 残余幻影={summary['verify_phantoms_after']}",
        f"",
        f"## 4. 重点股边界前后对照",
    ]
    focus = sorted(set(KEY_RESTORE_DIV_CODES + KEY_BONUS_DISTORT_CODES + KEY_PHANTOM_CODES))
    for c in focus:
        b_list = before["focus_boundaries"].get(c, [])
        a_list = after["focus_boundaries"].get(c, [])
        if b_list or a_list:
            lines.append(f"- `{c}`: 修复前={b_list} | 修复后={a_list}")

    if summary.get("recompute"):
        b_wl = set(before["watchlist"])
        a_wl = set(after["watchlist"])
        added = sorted(a_wl - b_wl)
        removed = sorted(b_wl - a_wl)
        lines.extend([
            f"",
            f"## 5. 选股与分位前后 Diff",
            f"- 今日选股观察名单: {len(b_wl)} -> {len(a_wl)} 只 | 新增={added} | 移出={removed}",
        ])
        for c in sorted(set(before["timing_by_code"]) | set(after["timing_by_code"])):
            bp = (before["timing_by_code"].get(c) or {}).get("pctile_window")
            ap = (after["timing_by_code"].get(c) or {}).get("pctile_window")
            if bp != ap:
                lines.append(f"  - `{c}` 5年分位: {bp} -> {ap}")
    return "\n".join(lines) + "\n"


def run_repair(con, db_path: str, *,
               apply: bool = False,
               div_start: str = "2026-01-01",
               div_end: Optional[str] = None,
               phantom_since: str = "2026-01-01",
               events_json: Optional[str] = None,
               skip_tushare: bool = False,
               skip_recompute: bool = False,
               tushare_samples: Optional[List[str]] = None,
               config_path: Optional[str] = None) -> Tuple[Dict[str, Any], str]:
    """执行完整修复与验收流（支持 dry-run 与离线 events_json 测试）。"""
    from lake.ingest import em_batch_ingest as emb

    end_s = (div_end or _today_beijing())[:10]
    before = snapshot_state(con)

    # 1. 拉取/读取补齐事件（无论是否 apply 都先抓取事件以统计预演影响）
    if events_json:
        with open(events_json, "r", encoding="utf-8") as f:
            raw_evs = json.load(f)
        events = list(raw_evs.get("events", raw_evs) if isinstance(raw_evs, dict) else raw_evs)
    else:
        events = emb.fetch_ex_events_range(div_start, end_s)

    written_ev = 0
    if apply and events:
        written_ev = emb.load_ex_events(con, end_s, events)

    div_sync = {
        "start_date": div_start,
        "end_date": end_s,
        "fetched": len(events),
        "written": written_ev,
    }

    # 2. 扫描（若为 dry-run 且未落库，也能基于现有库预演；apply 时已基于补齐后的 dividend_events）
    phantom_scan = af_rebase.find_phantom_stamps(con, since_date=phantom_since)
    un_stamp = af_rebase.un_stamp_phantoms(
        con, phantom_scan["phantoms"], dry_run=(not apply))

    # 3. 全史 rebase（恢复 2 只 10送4 与 9 只 >2% 被抹除权；且 suspected 不盖章）+ 恢复 0.5%-2% 小步长
    if apply:
        repair_all = af_rebase.repair_all_af(con)
        restore_small = af_rebase.restore_flattened_events(con, since_date=div_start)
        con.execute("CHECKPOINT")
    else:
        sp_scan = af_rebase.find_spurious_jumps(con, since_date=None)
        repair_all = {
            "dry_run": True,
            "candidate_spurious": len(sp_scan),
            "stocks_rebased": 0, "jump_pairs": 0,
            "rows_rewritten": 0, "suspected_pairs": sum(1 for x in sp_scan if x["kind"] == "suspected"),
        }
        restore_small = {"dry_run": True, "stocks": 0, "boundaries": 0, "rows_rewritten": 0}

    # 4. 验收重扫
    post_jumps = af_rebase.find_spurious_jumps(con, since_date=None)
    post_non_susp = [j for j in post_jumps if j["kind"] not in ("suspected", "unquantified_event")]
    post_phantoms = af_rebase.find_phantom_stamps(con, since_date=phantom_since)

    # 5. 可选 tushare 官方对照抽样
    ts_verify = ({"skipped": True, "samples": []} if skip_tushare
                 else verify_tushare_samples(con, tushare_samples or DEFAULT_TUSHARE_SAMPLES))

    # 6. 可选重算 selection / timing（仅 apply 后）
    recompute = None
    if apply and not skip_recompute:
        recompute = _recompute_selection_and_timing(con, config_path=config_path)
        con.execute("CHECKPOINT")

    after = snapshot_state(con)
    summary: Dict[str, Any] = {
        "apply": apply,
        "db_path": db_path,
        "div_sync": div_sync,
        "phantom_scan": phantom_scan,
        "un_stamp": un_stamp,
        "repair_all": repair_all,
        "restore_small": restore_small,
        "verify_jumps_after": len(post_jumps),
        "verify_non_suspected_after": len(post_non_susp),
        "verify_phantoms_after": len(post_phantoms["phantoms"]),
        "verify_distortions_after": len(post_phantoms["event_distortions"]),
        "tushare_verify": ts_verify,
        "recompute": recompute,
    }
    report_md = _format_diff_report(before, after, summary)
    return summary, report_md


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    from lake.conn import LakeLock, default_db_path

    p = argparse.ArgumentParser(description="数据湖 AF 幻影反盖章与分红事件补齐验收脚本")
    p.add_argument("--db", default=None, help="DuckDB 库路径（缺省 data/lake/lake.duckdb）")
    p.add_argument("--apply", action="store_true", default=False, help="实际写入修改库（缺省仅 dry-run 预演）")
    p.add_argument("--no-backup", action="store_true", default=False, help="写入前跳过文件备份")
    p.add_argument("--div-start", default="2026-01-01", help="分红补齐起始日期")
    p.add_argument("--div-end", default=None, help="分红补齐结束日期（缺省=今日）")
    p.add_argument("--phantom-since", default="2026-01-01", help="幻影扫描起始日期")
    p.add_argument("--events-json", default=None, help="离线事件 JSON 路径（测试/离线环境用）")
    p.add_argument("--skip-tushare", action="store_true", default=False, help="跳过 tushare 抽样校验")
    p.add_argument("--skip-recompute", action="store_true", default=False, help="跳过重算 selection/timing")
    p.add_argument("--report-out", default=None, help="报告输出路径（缺省写入 logs/）")
    args = p.parse_args(argv)

    db_path = args.db or default_db_path()
    if not os.path.exists(db_path):
        print(f"错误: 数据湖不存在: {db_path}", file=sys.stderr)
        return 3

    import duckdb
    backup_path = None
    if args.apply and not args.no_backup:
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = f"{db_path}.bak_{stamp}"
        shutil.copy2(db_path, backup_path)
        log.info("已备份数据湖 → %s", backup_path)

    if args.apply:
        with LakeLock(db_path):
            con = duckdb.connect(db_path)
            try:
                summary, report_md = run_repair(
                    con, db_path,
                    apply=True,
                    div_start=args.div_start,
                    div_end=args.div_end,
                    phantom_since=args.phantom_since,
                    events_json=args.events_json,
                    skip_tushare=args.skip_tushare,
                    skip_recompute=args.skip_recompute,
                )
            finally:
                con.close()
    else:
        con = duckdb.connect(db_path, read_only=True)
        try:
            summary, report_md = run_repair(
                con, db_path,
                apply=False,
                div_start=args.div_start,
                div_end=args.div_end,
                phantom_since=args.phantom_since,
                events_json=args.events_json,
                skip_tushare=args.skip_tushare,
                skip_recompute=True,
            )
        finally:
            con.close()

    if backup_path:
        summary["backup_path"] = backup_path
        report_md = report_md.replace("- 备份文件: `无`", f"- 备份文件: `{backup_path}`")

    out_path = args.report_out or os.path.join(
        PROJECT_ROOT, "logs",
        f"af_repair_report_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.md")
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    Path(out_path).write_text(report_md, encoding="utf-8")
    print(report_md)
    print(f"报告已保存: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
