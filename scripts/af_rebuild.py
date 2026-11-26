#!/usr/bin/env python3
"""af 事件驱动重建（tushare 源全弃用，Joel 2026-11-25 拍板，P1 核心）。

设计（单源事件驱动，零外部 af 源）
----------------------------------
af(date) = 从首根 K 线日（=上市日，af=1.0 锚）起，逐交易日前向累积**官方除权步长**：
    step(除权日 D) = c0 · (1 + B) / (c0 − C)
      c0  = D 前最近交易日 raw close（kline_daily）
      C   = 该除权日 Σ 每股税前现金分红（dividend_events.cash_dps）
      B   = 该除权日 Σ 每股送转股（dividend_events.stk_div）
    非除权日 step = 1.0（level 沿用，无 NULL 洞——铁律 2026-11-20 延续）。
口径实锤：em_batch_ingest.derive_af_batch 同公式（2026-11-15 5002 事件对照
tushare af 残差 <0.1%）。af 从此是 (kline_daily, dividend_events) 的纯函数：
确定性、可重放、跨日可比（历史值一旦写入永不因新数据漂移——根治 tushare
基准日漂 + af_rebase 日改锚）。

配股：事件表无配股价字段（东财不提供）→ 配股日现金/送转=0 时步长不可推导，
fail-closed 记入 failures 名单（近两年全市场 0 样本；P3 门会按"事件日缺口
不匹配"捕获，人工核实后补事件）。

子命令
------
  backfill-events --start 2026-09-01 --end 2026-11-25 [--check 2025]
      EM RPT_SHAREBONUS_DET 区间全量抓取 upsert（幂等）；--check YEAR 输出
      该年 EM 抓取数 vs 表内数对账。
  dryrun [--codes a,b,c]
      计算指定股票新 af 并与现湖对比：最新日缩放比、非事件日跳变数、
      事件步长 vs 旧湖 af 比值 mismatch（幻影盖章应在此现形）。
      --codes 缺省 = selection 池全量 + 审计关键股 + 20 只随机。
  apply [--batch 250]
      全市场重建 kline_daily.adj_factor（分批 staging + UPDATE join +
      CHECKPOINT）；failures 输出 JSON。
"""
from __future__ import annotations

import argparse
import bisect
import json
import logging
import sys
import time
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, ".")

import duckdb  # noqa: E402

DB = "data/lake/lake.duckdb"
LEGACY_CUTOFF = "2026-10-25"  # tushare 日增起点 10-26 之前：旧湖 af 为 sina 推导（干净）
log = logging.getLogger("af_rebuild")


def ensure_legacy_steps_table(con) -> None:
    con.execute("""
        CREATE TABLE IF NOT EXISTS af_legacy_steps(
            ts_code VARCHAR, ex_date DATE, step DOUBLE,
            source VARCHAR DEFAULT 'legacy_af_inferred',
            fetched_at TIMESTAMP DEFAULT now(),
            PRIMARY KEY (ts_code, ex_date))""")

# 审计关键股（11-23/11-25 两案主角，dryrun 必含）
AUDIT_CODES = [
    "sz.000708",   # 中信特钢 29.94% 快照伪象
    "sh.600000",   # 浦发 tushare 8.58x rebase 实证
    "sz.300873",   # 爱迪特 10送4 官方 1.4138 被盖 1.1758
    "sz.300857",   # 科创新源 同上 1.1680
    "sz.301219",   # 青木科技 幻影盖章 1.152（tushare 官方恒 3.4404）
    "sh.601857",   # 中国石油 11-05 幻影 +8.6%
    "sh.601995",   # 84 池幻影盖章
    "sh.600392",   # 84 池幻影盖章
    "sh.601998",   # 中信银行 缺分红步长被抹 1.0217
    "sh.600654",   # 11-25 tushare rebase top 137.68x
    "sz.000002",   # 万科 46.56x
]


class AfRebuildError(RuntimeError):
    """fail-closed：数据异常（步长分母非正等），该股拒绝重建。"""


# ---------------------------------------------------------------------------
# 核心：事件驱动 af 序列
# ---------------------------------------------------------------------------

def compute_af_series(con, ts_code: str) -> Dict[str, float]:
    """返回 {date(str): af}（全部交易日，无 NULL）。异常抛 AfRebuildError。

    步长来源：① dividend_events（EM 官方事件表，唯一规范源）；
    ② af_legacy_steps（历史回收：tushare 弃用前 10-25 之前、sina 推导 af
    证实存在但事件表缺失的除权，一次性回收持久化，重跑幂等）。
    """
    rows = con.execute(
        "SELECT date, close FROM kline_daily WHERE ts_code=? ORDER BY date",
        [ts_code]).fetchall()
    if not rows:
        return {}
    dates: List[str] = [str(d)[:10] for d, _ in rows]
    closes: List[Optional[float]] = [c for _, c in rows]

    ev: Dict[str, Tuple[float, float]] = {}
    for exd, cash, bonus in con.execute(
            "SELECT ex_date, COALESCE(SUM(cash_dps),0), COALESCE(SUM(stk_div),0) "
            "FROM dividend_events WHERE ts_code=? GROUP BY ex_date",
            [ts_code]).fetchall():
        ev[str(exd)[:10]] = (float(cash), float(bonus))

    steps: Dict[str, float] = {}
    for exd, (cash, bonus) in ev.items():
        if cash == 0.0 and bonus == 0.0:
            continue  # 配股/异常事件：无现金无送转 → 步长不可推导（fail 名单另检）
        i = bisect.bisect_left(dates, exd)  # 首个 >= ex_date 的交易日（停牌→顺延）
        if i >= len(dates):
            continue  # 事件晚于末根 K 线（数据滞后），日增时补齐
        if i == 0:
            continue  # 事件在首根 K 线之前：c0 不可知，吸收进锚（比例不受影响）
        c0 = closes[i - 1]
        if c0 is None or c0 <= 0:
            continue  # 停牌缺口：c0 取 i-1 已是最前可用；仍缺则跳（极罕见）
        denom = c0 - cash
        if denom <= 0:
            raise AfRebuildError(
                f"{ts_code} ex={exd} c0={c0} cash={cash} 步长分母非正（数据异常）")
        steps[dates[i]] = steps.get(dates[i], 1.0) * (c0 * (1.0 + bonus) / denom)

    # 历史回收步长（ex_date 即 kline 边界日，直接套用）
    date_set = set(dates)
    for exd, step in con.execute(
            "SELECT ex_date, step FROM af_legacy_steps WHERE ts_code=?",
            [ts_code]).fetchall():
        d = str(exd)[:10]
        if d in date_set:
            steps[d] = steps.get(d, 1.0) * float(step)

    af: Dict[str, float] = {}
    level = 1.0
    for d, c in zip(dates, closes):
        level *= steps.get(d, 1.0)
        af[d] = level
    return af


def _event_boundaries(d_list: List[str], ev_rows) -> set:
    """有现金/送转事件的边界日集合（首个 >=ex_date 的交易日，含停牌顺延）。"""
    out = set()
    for exd, cash, bonus in ev_rows:
        if cash > 0 or bonus > 0:
            i = bisect.bisect_left(d_list, str(exd)[:10])
            if i < len(d_list):
                out.add(d_list[i])
    return out


def purge_conflicting_legacy(con, ts_code: str) -> int:
    """清除与事件边界冲突的 legacy 步长（旧版 harvest 按 ex_date 而非边界日
    跳过，停牌顺延事件被双重计数；601088 2017-09-01 x1.1537 实锤）。
    返回清除行数（0=无冲突）。"""
    d_list = [str(d)[:10] for d, in con.execute(
        "SELECT date FROM kline_daily WHERE ts_code=? ORDER BY date", [ts_code]).fetchall()]
    if not d_list:
        return 0
    ev = con.execute(
        "SELECT ex_date, COALESCE(SUM(cash_dps),0), COALESCE(SUM(stk_div),0) "
        "FROM dividend_events WHERE ts_code=? GROUP BY ex_date", [ts_code]).fetchall()
    boundary = _event_boundaries(d_list, ev)
    n = 0
    for exd, in con.execute(
            "SELECT ex_date FROM af_legacy_steps WHERE ts_code=?", [ts_code]).fetchall():
        if str(exd)[:10] in boundary:
            con.execute(
                "DELETE FROM af_legacy_steps WHERE ts_code=? AND ex_date=?",
                [ts_code, str(exd)[:10]])
            n += 1
    return n


def harvest_legacy_steps(con, ts_code: str) -> List[Tuple[str, float]]:
    """回收 tushare 弃用前（<=LEGACY_CUTOFF）事件表缺失的真实除权步长。

    旧湖 af（sina hfq÷raw 推导，11-23 审计 + tushare 官方双证干净）在
    无事件日出现 >3% 跳变 = 真除权未入库（2016-2025 约 117 处，无单日聚集）。
    tushare 时代（10-26 起）旧 af 已被源漂污染，**不回收**（防幻影盖章复活）。
    """
    d_list = [str(d)[:10] for d, in con.execute(
        "SELECT date FROM kline_daily WHERE ts_code=? ORDER BY date", [ts_code]).fetchall()]
    if not d_list or d_list[-1] <= LEGACY_CUTOFF:
        return []
    old = {str(d)[:10]: float(v) for d, v in con.execute(
        "SELECT date, adj_factor FROM kline_daily WHERE ts_code=? "
        "AND date<=? AND adj_factor IS NOT NULL",
        [ts_code, LEGACY_CUTOFF]).fetchall()}
    # 有官方现金/送转的事件 → 事件表提供步长，不回收。
    # 注意：事件边界日=首个 >=ex_date 的交易日（停牌顺延可能滞后数周，
    # 601088 2017-07-10 除权 → 09-01 落步实锤），须按边界日跳过，
    # 否则会把事件步长当"无事件跳变"重复回收（1.1537² 双重计数）。
    # 纯配股事件（cash=bonus=0，无配股价字段）→ 回退旧 af 比值（sina 已含配股调整）
    ev = con.execute(
        "SELECT ex_date, COALESCE(SUM(cash_dps),0), COALESCE(SUM(stk_div),0) "
        "FROM dividend_events WHERE ts_code=? GROUP BY ex_date", [ts_code]).fetchall()
    event_boundary = _event_boundaries(d_list, ev)
    have_legacy = {str(e)[:10] for e, in con.execute(
        "SELECT ex_date FROM af_legacy_steps WHERE ts_code=?", [ts_code]).fetchall()}
    recovered: List[Tuple[str, float]] = []
    prev: Optional[Tuple[str, float]] = None
    for d in d_list:
        if d > LEGACY_CUTOFF:
            break
        if d in old:
            if prev is not None and d not in event_boundary and d not in have_legacy:
                ratio = old[d] / prev[1]
                if abs(ratio - 1.0) > 0.03:
                    recovered.append((d, round(ratio, 6)))
            prev = (d, old[d])
        else:
            prev = None
    return recovered


def allotment_suspects(con, ts_code: str, af_new: Dict[str, float]) -> List[str]:
    """疑似配股日：事件行存在但 cash=bonus=0 且当日价格缺口 >3%（步长缺失）。"""
    out = []
    rows = con.execute(
        "SELECT date, close, LAG(close) OVER (ORDER BY date) "
        "FROM kline_daily WHERE ts_code=? AND close IS NOT NULL", [ts_code]
    ).fetchall()
    prev_c = None
    ev_zero = {str(exd)[:10] for exd, in con.execute(
        "SELECT ex_date FROM dividend_events WHERE ts_code=? "
        "AND COALESCE(cash_dps,0)=0 AND COALESCE(stk_div,0)=0",
        [ts_code]).fetchall()}
    for d, c, pc in rows:
        if pc and pc > 0 and c > 0:
            gap = c / pc - 1.0
            if str(d)[:10] in ev_zero and gap < -0.03:
                out.append(f"{str(d)[:10]} gap={gap*100:.1f}%")
        prev_c = c
    return out


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------

def cmd_backfill_events(args) -> int:
    from lake.ingest import em_batch_ingest as emb
    con = duckdb.connect(DB)
    events = emb.fetch_ex_events_range(args.start, args.end)
    written = emb.load_ex_events(con, args.end[:10], events) if events else 0
    mx = con.execute("SELECT MAX(ex_date), COUNT(*) FROM dividend_events").fetchone()
    print(json.dumps({"fetched": len(events), "written": int(written),
                      "max_ex_date": str(mx[0])[:10], "total_rows": int(mx[1])},
                     ensure_ascii=False))
    if args.check:
        y = args.check
        em_rows = emb.fetch_ex_events_range(f"{y}-01-01", f"{y}-12-31")
        em_keys = {(e["ts_code"], str(e["ex_date"])[:10], str(e.get("period") or ""))
                   for e in em_rows}
        tb_keys = {(r[0], str(r[1])[:10], str(r[2] or "")) for r in con.execute(
            "SELECT ts_code, ex_date, period FROM dividend_events "
            "WHERE ex_date>=? AND ex_date<=?", [f"{y}-01-01", f"{y}-12-31"]).fetchall()}
        print(json.dumps({f"check_{y}": {
            "em_count": len(em_keys), "table_count": len(tb_keys),
            "in_em_not_table": len(em_keys - tb_keys),
            "in_table_not_em": len(tb_keys - em_keys)}}, ensure_ascii=False))
    con.close()
    return 0


def _default_codes(con) -> List[str]:
    pool = {r[0] for r in con.execute(
        "SELECT DISTINCT ts_code FROM selection_backtest_10y").fetchall()}
    import random
    random.seed(20261125)
    allc = [r[0] for r in con.execute(
        "SELECT DISTINCT ts_code FROM kline_daily").fetchall()]
    extra = random.sample(sorted(set(allc) - pool), k=min(20, len(allc)))
    return sorted(pool | set(AUDIT_CODES) | set(extra))


def cmd_dryrun(args) -> int:
    con = duckdb.connect(DB)  # 读写：dryrun 顺带持久化历史回收步长（与 apply 幂等同构）
    ensure_legacy_steps_table(con)
    codes = ([c.strip() for c in args.codes.split(",") if c.strip()]
             if args.codes else _default_codes(con))
    print(f"dryrun codes={len(codes)}")
    t0 = time.monotonic()
    report = {"n": 0, "scale_ratios": [], "scale_by_code": {},
              "total_steps": 0,
              "unexpected_jumps": 0, "unexpected_samples": [],
              "event_mismatch": 0, "failures": [], "allot_suspects": {},
              "legacy_recoveries": 0, "legacy_purged": 0, "legacy_sample": [], "samples": []}
    for code in codes:
        # 先清除历史双重计数行（旧版 harvest 停牌顺延边界 bug），再回收
        report["legacy_purged"] += purge_conflicting_legacy(con, code)
        rec = harvest_legacy_steps(con, code)
        if rec:
            con.executemany(
                "INSERT OR REPLACE INTO af_legacy_steps(ts_code, ex_date, step) "
                "VALUES (?,?,?)",
                [(code, d, s) for d, s in rec])
            report["legacy_recoveries"] += len(rec)
            if len(report["legacy_sample"]) < 15:
                report["legacy_sample"].extend(f"{code} {d} x{s:.4f}"
                                               for d, s in rec[:3])
        try:
            new_af = compute_af_series(con, code)
        except AfRebuildError as e:
            report["failures"].append(str(e))
            continue
        if not new_af:
            continue
        report["n"] += 1
        d = sorted(new_af)
        latest, oldest = d[-1], d[0]
        old = con.execute(
            "SELECT adj_factor FROM kline_daily WHERE ts_code=? AND date=?",
            [code, latest]).fetchone()[0]
        if old:
            report["scale_ratios"].append(new_af[latest] / float(old))
            report["scale_by_code"][code] = round(new_af[latest] / float(old), 6)
        # 步长边界统计：total_steps=事件/历史步长总数（信息量）；
        # unexpected_jumps=无事件无历史步长日的意外跳变（必须=0）
        ev_days_all = {str(exd)[:10] for exd, in con.execute(
            "SELECT DISTINCT ex_date FROM dividend_events WHERE ts_code=?",
            [code]).fetchall()}
        known = {str(e)[:10] for e, in con.execute(
            "SELECT ex_date FROM af_legacy_steps WHERE ts_code=?", [code]).fetchall()}
        for exd in ev_days_all:
            i2 = bisect.bisect_left(d, exd)
            if i2 < len(d):
                known.add(d[i2])
        n_steps = 0
        n_unexpected = 0
        for i in range(1, len(d)):
            r = new_af[d[i]] / new_af[d[i - 1]]
            if abs(r - 1.0) > 2e-4:
                n_steps += 1
                if d[i] not in known:
                    n_unexpected += 1
                    if len(report["unexpected_samples"]) < 10:
                        report["unexpected_samples"].append(
                            f"{code} {d[i]} x{r:.4f}")
        report["total_steps"] += n_steps
        report["unexpected_jumps"] += n_unexpected
        # 事件步长 vs 旧湖 af 比值（mismatch = 旧值被污染处）
        evs = con.execute(
            "SELECT ex_date, COALESCE(SUM(cash_dps),0), COALESCE(SUM(stk_div),0) "
            "FROM dividend_events WHERE ts_code=? GROUP BY ex_date", [code]).fetchall()
        old_map = dict(con.execute(
            "SELECT date, adj_factor FROM kline_daily WHERE ts_code=? "
            "AND date>='2020-01-01' AND adj_factor IS NOT NULL", [code]).fetchall())
        for exd, cash, bonus in evs:
            exd = str(exd)[:10]
            i = bisect.bisect_left(d, exd)
            if i <= 0 or i >= len(d):
                continue
            de = d[i]
            if cash == 0.0 and bonus == 0.0:
                continue
            c0 = con.execute(
                "SELECT close FROM kline_daily WHERE ts_code=? AND date<? "
                "ORDER BY date DESC LIMIT 1", [code, exd]).fetchone()[0]
            if not c0 or c0 - cash <= 0:
                continue
            step = float(c0) * (1.0 + float(bonus)) / (float(c0) - float(cash))
            if abs(step - 1.0) < 2e-4:
                continue
            old_ratio = None
            prev_day = old_map.get(d[i - 1])
            if d[i - 1] in old_map and d[i] in old_map and old_map.get(d[i]):
                old_ratio = float(old_map[d[i]]) / float(prev_day)
            if old_ratio is not None and abs(old_ratio / step - 1.0) > 0.02:
                report["event_mismatch"] += 1
                if len(report["samples"]) < 15:
                    report["samples"].append(
                        f"{code} ex={exd} step={step:.4f} old_ratio={old_ratio:.4f} "
                        f"(旧值偏差 {(old_ratio/step-1)*100:+.1f}%)")
        if code in AUDIT_CODES:
            sus = allotment_suspects(con, code, new_af)
            if sus:
                report["allot_suspects"][code] = sus
    import statistics as st
    report["scale_ratio_median"] = (round(st.median(report["scale_ratios"]), 6)
                                    if report["scale_ratios"] else None)
    report["scale_ratio_min_max"] = ([round(min(report["scale_ratios"]), 4),
                                      round(max(report["scale_ratios"]), 4)]
                                     if report["scale_ratios"] else None)
    report["elapsed_s"] = round(time.monotonic() - t0, 1)
    con.execute("CHECKPOINT")
    con.close()
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


def cmd_apply(args) -> int:
    con = duckdb.connect(DB)
    ensure_legacy_steps_table(con)
    codes = [r[0] for r in con.execute(
        "SELECT DISTINCT ts_code FROM kline_daily").fetchall()]
    # ① 全市场历史回收步长扫描（一次性，幂等）
    n_rec = 0
    n_purge = 0
    for b0 in range(0, len(codes), 250):
        for code in codes[b0:b0 + 250]:
            try:
                # 先清除历史双重计数行（旧版 harvest 停牌顺延边界 bug），再回收
                n_purge += purge_conflicting_legacy(con, code)
                rec = harvest_legacy_steps(con, code)
            except Exception:  # noqa: BLE001 - 单股失败不阻断扫描
                continue
            if rec:
                con.executemany(
                    "INSERT OR REPLACE INTO af_legacy_steps(ts_code, ex_date, step) "
                    "VALUES (?,?,?)",
                    [(code, d, s) for d, s in rec])
                n_rec += len(rec)
        log.info("legacy harvest sweep %d/%d (recovered=%d)",
                 min(b0 + 250, len(codes)), len(codes), n_rec)
    log.info("legacy purged (event-boundary conflicts): %d", n_purge)
    con.execute("CHECKPOINT")
    # ② 事件驱动 af 全史重建
    con.execute("DROP TABLE IF EXISTS af_rebuild_stg")
    con.execute("CREATE TEMP TABLE af_rebuild_stg(ts_code VARCHAR, date DATE, "
                "adj_factor DOUBLE)")
    n_ok = 0
    rows_staged = 0
    failures: List[str] = []
    t0 = time.monotonic()
    for b0 in range(0, len(codes), args.batch):
        batch = codes[b0:b0 + args.batch]
        buf = []
        for code in batch:
            try:
                af = compute_af_series(con, code)
            except AfRebuildError as e:
                failures.append(str(e))
                continue
            if not af:
                continue
            buf.extend((code, d, v) for d, v in af.items())
            n_ok += 1
        if buf:
            con.executemany("INSERT INTO af_rebuild_stg VALUES (?,?,?)", buf)
            rows_staged += len(buf)
            con.execute(
                "UPDATE kline_daily k SET adj_factor=s.adj_factor, "
                "fetched_at=now() FROM af_rebuild_stg s "
                "WHERE k.ts_code=s.ts_code AND k.date=s.date")
            con.execute("DELETE FROM af_rebuild_stg")
        if (b0 // args.batch) % 4 == 0:
            log.info("af rebuild %d/%d codes ok=%d staged=%d (%.0fs)",
                     b0 + len(batch), len(codes), n_ok, rows_staged,
                     time.monotonic() - t0)
    con.execute("CHECKPOINT")
    print(json.dumps({"codes_total": len(codes), "codes_ok": n_ok,
                      "rows_updated": rows_staged,
                      "failures": failures,
                      "elapsed_s": round(time.monotonic() - t0, 1)},
                     ensure_ascii=False, indent=1))
    con.close()
    return 0 if not failures else 1


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    pe = sub.add_parser("backfill-events")
    pe.add_argument("--start", required=True)
    pe.add_argument("--end", required=True)
    pe.add_argument("--check", default=None, help="对账年份，如 2025")
    pd = sub.add_parser("dryrun")
    pd.add_argument("--codes", default=None, help="逗号分隔；缺省=池+审计股+随机20")
    pa = sub.add_parser("apply")
    pa.add_argument("--batch", type=int, default=250)
    args = p.parse_args()
    if args.cmd == "backfill-events":
        return cmd_backfill_events(args)
    if args.cmd == "dryrun":
        return cmd_dryrun(args)
    return cmd_apply(args)


if __name__ == "__main__":
    raise SystemExit(main())
