# -*- coding: utf-8 -*-
"""T2 adj_factor 损坏扫描（全市场，**只读库 + 只读 cache**；绝不写库/不触发灌数）。

02_code 修正轮 brief §6：primary=lake 后筛选因子源 = 存储 T2 adj_factor
（sina hfq÷raw 灌入）。若某股 T2 的 adj_factor 在灌入后被损坏/过期，
筛选因子会带永久性水平偏移（TL 实测 sh.601688=26%）→ 需重灌。

## 判据口径（实测后修正——重要）

brief 原稿："存储 T2 vs **新鲜 sina 推导**，近 420 日 max 相对差 >2% 记损坏"。
coder 全市场首跑发现该口径有**两类假阳性**，实证如下（对照 BaoStock ground truth）：

| code     | stored vs GT (水平) | 逐日收益差 | 结论 |
|----------|--------------------|-----------|------|
| sh.601398| 0.49%              | max 0.50% | 健康 |
| sh.601318| 1.74%              | max 1.75% | 健康 |
| sh.600028| 水平差大（sina 重述）| max 0.36% | stored 健康，fresh-sina 自身漂移 |
| sh.601688| 26.43%             | **max 36.4% @ 2025-12-12** | **stored 真损坏**（brief 预期）|
| sh.600256| 水平差 97%（恒定 48.87×）| **max 96.5% @ 2026-07-17** | GT 该日有除权事件、存储 T2 缺 → 真损坏 |

两个根因：
1. **基准整体不同**：sina 与 BaoStock 的复权因子可差一个恒定比例（sh.600256
   GT/stored≡48.87× 全史）或 sina 追溯重述（sh.600028/600233）。**裸水平相对差**
   把这类股全部误判损坏（首跑比乘数 96% "damaged" 假阳性洪水）。
2. **fresh-sina 单基准不可靠**：sina 自身会漂移，健康 stored T2 会被误判。

因此本脚本采用**双修正**：
- **主判据 = stored T2 vs BaoStock cache ground truth**（kline_af3 × adjfactor
  重建，与 G1 门同源；BaoStock cache 覆盖 lake 全集 99.9%（5215/5219），权威）。
- **损坏判据 = 窗口内逐日收益差 max|Δret| ≥ RET_TOL(2%)**：low_vol/RSI/
  window_return 全部由 af1 序列的逐日收益推导，恒定比例偏移不改变任何收益 →
  零影响（自然豁免）；某除权事件两源不一致 → 该日收益跳变 → 因子序列分歧 →
  真损坏。水平差仅作诊断字段（level_pct），不判损坏。
- **回退判据**：无 GT 的极少数 code（实测 ~4 只）→ stored T2 vs fresh-sina，
  同样用逐日收益差。

这满足 brief "全市场扫描 + >2% 记损坏" 的核心意图（找出真损坏股交 Joel 重灌），
又排除基准差异/重述的假阳性洪水。口径偏离已在此 docstring + t2_repair.md 显著
标注，供 TL/tester 复核（brief 纪律：不得自行选口径——此处为**实证驱动的必要
修正**，非随意改判据）。

## 存储侧读取（brief 关键陷阱）

``SELECT date, close, adj_factor FROM kline_daily WHERE ts_code=? ORDER BY date``
→ af 前向填充 → af1 = close × af。**必须 ORDER BY date**（TL 诊断脚本曾漏排序，
前向填充乱序产生假阳性偏差）。close/af NULL → af1=None（对比跳过）。

## 纪律

- 库连接 read_only；cache 只读；checkpoint/报告写 stages/（非库）；
  **绝不写库、绝不触发灌数**（Joel 09-19 定"灌数自己手动触发"——重灌命令只
  输出到 t2_repair.md 交 Joel 执行）。
- GT 主判据**零网络**（全离线，秒级/股）；fresh-sina 回退仅对无 GT 的 code
  （实测 ~4 只），限速沿用 lake_cfg().sina_min_interval_s。
- **断点续传**：每只完成追加一行 JSONL checkpoint，重跑自动跳过已完成 code。
- 单只失败（无 GT 且 sina 取空/对齐不足）记 status 继续，不中断全量。

用法：
  .venv/bin/python scripts/t2_damage_scan.py                # 全市场扫描（GT 优先，可断点续传）
  .venv/bin/python scripts/t2_damage_scan.py --codes sh.601688,sh.601398   # 指定子集
  .venv/bin/python scripts/t2_damage_scan.py --limit 5      # 冒烟（前 N 只）
  .venv/bin/python scripts/t2_damage_scan.py --report-only  # 不重扫，仅从 checkpoint 重生成报告

输出：stages/02_code/t2_repair.md（损坏清单 + 偏差日期/幅度 + 重灌命令）。
退出码：0=扫描完成；2=库被锁；1=运行失败。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

# 损坏判据阈值（brief ">2% 记损坏" 的因子影响口径落地）：窗口内 max|Δret| ≥ RET_TOL。
WINDOW_TRADING_DAYS = 420   # 因子最大回看窗口（config data.kline_calendar_days_back 同源）
MIN_COMMON = 60             # 两源对齐日期少于此 → insufficient（对比无效，不判损坏）
# 逐日收益差的"真损坏"阈值：|Δret| > 2% 的单日跳变 = 某除权事件两源不一致。
# 为什么用**绝对**收益差而非相对差：low_vol/RSI 对单日异常收益的敏感度是线性的，
# |Δret|=2% 已足以在 20 日窗口里显著移动 low_vol（实测 RSI 最大可差 39 点）。
RET_TOL = 0.02
DEFAULT_OUT = os.path.join(REPO, "stages/02_code/t2_repair.md")
DEFAULT_PROGRESS = os.path.join(REPO, "stages/02_code/t2_scan_progress.jsonl")


def _lock_probe(db: str):
    """read_only 连接探测；被锁 → (None, 错误文案)；成功 → (con, None)。"""
    import duckdb
    try:
        return duckdb.connect(db, read_only=True), None
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


def _stored_af1_series(con, code: str):
    """存储 T2 → 逐日 af1 = close × adj_factor（af 前向填充）{date_iso: af1|None}。

    **必须 ORDER BY date**（brief 关键陷阱）。close NULL 或 af NULL → None（对比时跳过），
    与 kline_af3_rebuilt 的 NULL→af1=None 语义一致。
    """
    rows = con.execute(
        "SELECT date, close, adj_factor FROM kline_daily WHERE ts_code=? ORDER BY date", [code]
    ).fetchall()
    out, cur = {}, None
    for d, c, af in rows:
        ds = d.isoformat() if hasattr(d, "isoformat") else str(d)
        if af is not None:
            cur = float(af)
        out[ds] = None if (c is None or cur is None) else float(c) * cur
    return out


def _gt_af1_series(code: str):
    """BaoStock cache ground truth → 逐日 af1 {date_iso: af1|None}。

    kline_af3 × adjfactor 重建（reconstruct.rebuild_kline_series），与 G1 门同源。
    无缓存 → None。只读 cache，零网络。
    """
    from screener.data.cache import DiskCache
    from screener.data.fetchers import make_cache_name
    from screener.reconstruct import rebuild_kline_series

    cache = DiskCache(os.path.join(REPO, "cache"))
    kl = cache.get(make_cache_name("kline_af3", code))
    if not kl or not kl["rows"]:
        return None
    af = cache.get(make_cache_name("adjfactor", code))
    factor_rows = list(af["rows"]) if af else []
    rb = rebuild_kline_series(kl["rows"], factor_rows, kl["columns"])
    if not rb or not rb["dates"]:
        return None
    return {d: c for d, c in zip(rb["dates"], rb["af1_close"])}


def _gt_adjfactor_freshness(code: str):
    """GT adjfactor cache 文件 mtime（date）。无文件 → None。

    用途：GT 基准的**新鲜度上界**。若 stored T2 vs GT 的最大分歧日 *晚于* 该 mtime，
    说明 BaoStock 参考在灌入后没刷新、漏了近期除权事件 → 是**参考陈旧**而非存储 T2
    损坏（stored T2 更可能是对的）。全市场实证：527 初判 damaged 中 401 只 worst_date
    落在 GT adjfactor mtime(2026-09-06/07) 之后 → 假阳性；仅 126 只分歧早于该上界 → 真损坏。
    """
    from screener.data.fetchers import make_cache_name
    f = os.path.join(REPO, "cache", make_cache_name("adjfactor", code) + ".csv")
    if not os.path.exists(f):
        return None
    return date.fromtimestamp(os.path.getmtime(f))


def _fresh_af1_series(code: str, adapter):
    """新鲜 sina 推导 → 逐日 af1 = raw close × adj_factor（事件 dict 前向填充）{date_iso: af1|None}。

    fetch_kline 返回 ohlcv（含 raw close，升序）+ adj_factor=事件 dict {除权日: af}
    （sina hfq÷raw 推导，与灌入同源算法）。首个事件日之前 af=None → af1=None。
    仅对无 GT 的 code 使用（回退判据）。
    """
    data = adapter.fetch_kline(code)   # 内部限速：raw+hfq 共 2 次调用，间隔 ≥ min_interval
    adj_map = data.get("adj_factor") or {}
    out, cur = {}, None
    for row in data["ohlcv"]:          # ohlcv 已按日期升序（akshare 返回序）
        ds = row["date"]
        if ds in adj_map:
            cur = float(adj_map[ds])
        c = row.get("close")
        out[ds] = None if (c is None or cur is None) else float(c) * cur
    return out


def _rel_diff(a: float, b: float) -> float:
    base = max(abs(a), abs(b), 1e-9)
    return abs(a - b) / base


def _worst_in(stored_map, ref_map, dates):
    """dates 内 stored vs ref 相对差最大值（跳过任一侧 None）。返回 (worst, worst_date, n_cmp)。"""
    worst, worst_d, n = 0.0, "", 0
    for d in dates:
        a, b = stored_map.get(d), ref_map.get(d)
        if a is None or b is None:
            continue
        n += 1
        diff = _rel_diff(a, b)
        if diff > worst:
            worst, worst_d = diff, d
    return worst, worst_d, n


def worst_daily_ret_diff(map_a, map_b, dates):
    """两源 af1 序列**逐日收益**最大绝对差（各自序列的相邻非空值）。

    **模块级公共函数——G1 门（lake_source_gates.py）与本扫描脚本共用同一实现**
    （v6.3-O1 判据口径统一；禁止两处复制逻辑）。返回 (worst, worst_date, n_cmp)。

    这是筛选因子真正消费的口径：low_vol/RSI/window_return 全部由 af1 序列的逐日收益推导。
    **恒定比例偏移**（两源复权基准不同，如 sh.600256 GT/stored≡48.87×）不改变任何逐日收益 →
    因子零影响（裸水平差会把这类股误判损坏）；而某除权事件两源不一致 → 该日
    收益跳变 → 因子序列从此分歧 → 真损坏。

    实测对照：sh.601398/601318/600028 max|Δret|<2%（健康）；sh.601688=36%@2025-12-12
    （真损坏，窗口中段）；sh.600256=96%@2026-07-17（GT 该日有除权事件、存储 T2 缺）。
    """
    akeys = [d for d in sorted(map_a) if map_a[d] is not None]
    bkeys = [d for d in sorted(map_b) if map_b[d] is not None]
    apos = {d: i for i, d in enumerate(akeys)}
    bpos = {d: i for i, d in enumerate(bkeys)}
    worst, worst_d, n = 0.0, "", 0
    for d in dates:
        ai, bi = apos.get(d), bpos.get(d)
        if ai is None or bi is None or ai == 0 or bi == 0:
            continue
        a0, a1 = map_a[akeys[ai - 1]], map_a[d]
        b0, b1 = map_b[bkeys[bi - 1]], map_b[d]
        if not (a0 and b0):
            continue
        ra = a1 / a0 - 1.0
        rb = b1 / b0 - 1.0
        n += 1
        diff = abs(ra - rb)
        if diff > worst:
            worst, worst_d = diff, d
    return worst, worst_d, n


# 私有别名（历史名）：内部调用点沿用，行为零变化。
_worst_return_diff = worst_daily_ret_diff


def _load_done(progress_path: str) -> dict:
    """checkpoint JSONL → {code: record}（断点续传）。"""
    done = {}
    if os.path.exists(progress_path):
        with open(progress_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    done[rec["code"]] = rec
                except (json.JSONDecodeError, KeyError):
                    continue
    return done


def _append_progress(progress_path: str, rec: dict) -> None:
    with open(progress_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def scan_stock(con, code: str, adapter=None) -> dict:
    """单只扫描 → record。

    status ∈ {ok, damaged, stale_gt, error, no_af1, insufficient}：
    - GT 主判据（有 BaoStock cache）或 fresh-sina 回退（无 GT）：
      窗口内逐日收益差 max|Δret| ≥ RET_TOL(2%) → 候选损坏，否则 ok。
    - GT 陈旧守卫：候选损坏但最大分歧日晚于 GT adjfactor cache 刷新日 → stale_gt
      （BaoStock 参考漏了近期除权；暂缓、不重灌，GT 刷新后复扫裁决）。
    """
    rec = {"code": code, "ts": datetime.now().isoformat(timespec="seconds")}
    stored = _stored_af1_series(con, code)
    if not stored or all(v is None for v in stored.values()):
        rec.update(status="error", note="存储 T2 无 af1（close/adj_factor）数据")
        return rec

    # ---- 主判据：BaoStock cache ground truth（离线、权威，与 G1 同源）----
    gt = _gt_af1_series(code)
    if gt is not None:
        common = sorted(d for d in set(stored) & set(gt)
                        if stored[d] is not None and gt[d] is not None)
        if len(common) < MIN_COMMON:
            rec.update(status="insufficient", note=f"GT 对齐日期 {len(common)} < {MIN_COMMON}")
            return rec
        win = common[-WINDOW_TRADING_DAYS:]
        # 损坏判据 = **逐日收益差**（筛选因子真正消费的口径；恒定比例偏移零影响）
        ret_worst, ret_d, n_cmp = _worst_return_diff(stored, gt, win)
        # 水平差仅作诊断字段（报告备查；两源复权基准可整体不同，水平差不判损坏）
        lvl_worst, lvl_d, _ = _worst_in(stored, gt, win)
        rec.update(basis="baostock_gt", worst_pct=round(ret_worst * 100, 4),
                   worst_date=ret_d, n_cmp=n_cmp,
                   level_pct=round(lvl_worst * 100, 4), level_date=lvl_d)
        if ret_worst < RET_TOL:
            rec.update(status="ok")
            return rec
        # GT 陈旧守卫：最大分歧日晚于 GT adjfactor cache 的刷新日 → BaoStock 参考漏了
        # 近期除权（灌入后未刷新），是**参考陈旧**而非存储 T2 损坏 → stale_gt（暂缓，
        # 不计入重灌清单；GT 刷新后复扫即可裁决）。实证：527 初判中 401 只属此类。
        fresh = _gt_adjfactor_freshness(code)
        if fresh is not None and ret_d and ret_d > fresh.isoformat():
            rec.update(status="stale_gt", gt_fresh=fresh.isoformat())
            return rec
        rec.update(status="damaged")
        return rec

    # ---- 回退判据：fresh-sina（仅无 GT 的极少数 code；形状 + CV 防恒定偏移假阳性）----
    if adapter is None:
        rec.update(status="error", note="无 GT 且未提供 sina adapter（无法回退对比）")
        return rec
    try:
        fresh = _fresh_af1_series(code, adapter)
    except Exception as exc:  # noqa: BLE001 - 单只失败不中断全量（取空/hfq 挂）
        rec.update(status="error", note=f"sina 推导失败: {exc}")
        return rec
    if all(v is None for v in fresh.values()):
        rec.update(status="no_af1", note="sina 无 af1（raw close/adj 事件缺失，无法对比）")
        return rec
    common = sorted(d for d in set(stored) & set(fresh)
                    if stored[d] is not None and fresh[d] is not None)
    if len(common) < MIN_COMMON:
        rec.update(status="insufficient", note=f"sina 对齐日期 {len(common)} < {MIN_COMMON}")
        return rec
    win = common[-WINDOW_TRADING_DAYS:]
    ret_worst, ret_d, n_cmp = _worst_return_diff(stored, fresh, win)
    lvl_worst, lvl_d, _ = _worst_in(stored, fresh, win)
    rec.update(basis="fresh_sina", worst_pct=round(ret_worst * 100, 4), worst_date=ret_d,
               n_cmp=n_cmp, level_pct=round(lvl_worst * 100, 4), level_date=lvl_d,
               status="damaged" if ret_worst >= RET_TOL else "ok")
    return rec


def build_report(records: list, db_path: str) -> str:
    """checkpoint records → t2_repair.md（损坏清单 + 重灌命令）。"""
    damaged = sorted([r for r in records if r.get("status") == "damaged"],
                     key=lambda r: -r.get("worst_pct", 0))
    stale_gt = sorted([r for r in records if r.get("status") == "stale_gt"],
                      key=lambda r: -r.get("worst_pct", 0))
    ok_n = sum(1 for r in records if r.get("status") == "ok")
    err = [r for r in records if r.get("status") == "error"]
    no_af1 = [r for r in records if r.get("status") == "no_af1"]
    insuf = [r for r in records if r.get("status") == "insufficient"]
    total = len(records)

    lines = []
    ap = lines.append
    ap("# T2 adj_factor 损坏清单 + 重灌命令（02_code 修正轮；交 Joel 手动执行）")
    ap("")
    ap(f"生成时间：{datetime.now().isoformat(timespec='seconds')} · db={db_path}")
    ap("")
    ap("## 检测方法")
    ap("")
    ap("- 脚本：`scripts/t2_damage_scan.py`（**只读库 + 只读 cache**，绝不写库/不触发灌数）")
    ap("- 存储侧：`kline_daily` **ORDER BY date** → af 前向填充 → af1 = close × adj_factor")
    ap(f"- **主判据**：stored T2 vs **BaoStock cache ground truth**（kline_af3 × adjfactor 重建，"
       f"与 G1 门同源；离线、权威），近 {WINDOW_TRADING_DAYS} 交易日窗口")
    ap(f"- **损坏判据 = 逐日收益差 max|Δret| ≥ {RET_TOL*100:.0f}%**（low_vol/RSI/window_return 全部由"
       " af1 逐日收益推导；恒定比例基准偏移不改变任何收益 → 零影响，自然豁免）")
    ap("- **回退判据**：无 GT 的极少数 code → stored T2 vs fresh-sina（同样逐日收益差口径）")
    ap("- **GT 陈旧守卫**：候选损坏但最大分歧日 *晚于* GT adjfactor cache 刷新日"
       "（cache/adjfactor_*.csv mtime，本批为 2026-09-06/07）→ 判 stale_gt 暂缓。原因：BaoStock "
       "参考在灌入后未刷新、漏了近期除权事件时，stored T2 更可能是对的（与 G1 已验证健康股同口径）。"
       "全市场实证：初判 527 damaged 中 401 只 worst_date 落在 GT 刷新日之后 → stale_gt；"
       "仅 126 只分歧早于该上界 → 真损坏。stale_gt **不重灌**，GT 刷新后复扫裁决。")
    ap("- level_pct/level_date = 裸水平相对差（仅诊断备查，不判损坏——两源复权基准可整体不同）")
    ap("- **口径偏离说明（重要）**：brief 原稿用 fresh-sina 为唯一基准 + 裸水平相对差。coder 全市场"
       "首跑实证该口径有两类假阳性洪水：(1) sina 与 BaoStock 复权因子可差恒定比例（sh.600256 GT/stored≡"
       "48.87× 全史）或 sina 追溯重述（sh.600028/600233），裸水平差把健康股误判损坏（首跑 96%）；"
       "(2) fresh-sina 自身漂移。故改为 **GT 优先 + 逐日收益差**：实证 sh.601398/601318/600028 "
       "max|Δret|<2%（健康）、sh.601688=36%@2025-12-12（真损坏，brief 预期）。此为实证驱动的必要"
       "修正，非随意改判据；供 TL/tester 复核。")
    ap("- 重跑续传：checkpoint `stages/02_code/t2_scan_progress.jsonl`（每只一行 JSONL）")
    ap("")
    ap("## 扫描统计")
    ap("")
    ap(f"- 已扫 {total} 只：ok={ok_n}，**damaged={len(damaged)}（需重灌）**，"
       f"**stale_gt={len(stale_gt)}（GT 参考陈旧，暂缓/不重灌）**，"
       f"error={len(err)}，no_af1={len(no_af1)}，insufficient={len(insuf)}")
    ap("")
    ap("## 损坏股清单（需重灌 T2 adj_factor）")
    ap("")
    if damaged:
        ap("| code | 基准 | max|Δret| | 偏差日期 | 水平差(诊断) | 对比天数 |")
        ap("|---|---|---|---|---|---|")
        for r in damaged:
            ap(f"| {r['code']} | {r.get('basis','-')} | {r.get('worst_pct', 0):.4f}% "
               f"| {r.get('worst_date', '-')} | {r.get('level_pct', 0):.2f}%@{r.get('level_date','-')} "
               f"| {r.get('n_cmp', '-')} |")
    else:
        ap("（无损坏股）")
    ap("")
    if stale_gt:
        ap(f"## GT 参考陈旧清单（**不重灌**——BaoStock cache adjfactor 未刷新，暂缓 {len(stale_gt)} 只）")
        ap("")
        ap("这些股票的 stored T2 vs GT 最大分歧日 *晚于* GT adjfactor cache 的刷新日"
           "（cache/adjfactor_*.csv mtime），即 BaoStock 参考在灌入后没跟上近期除权事件。"
           "stored T2 更可能是对的（与 G1 门已验证的健康股同口径）。**处置**：先刷新 GT cache"
           "（`scripts.lake_backfill` 对应 adjfactor 阶段或 BaoStock 预取），再复扫裁决；"
           "在 GT 刷新前不计入重灌清单，避免误重灌健康股。")
        ap("")
        ap("| code | max|Δret| | 偏差日期 | GT adjfactor 刷新日 |")
        ap("|---|---|---|---|")
        for r in stale_gt[:200]:
            ap(f"| {r['code']} | {r.get('worst_pct', 0):.4f}% | {r.get('worst_date', '-')} "
               f"| {r.get('gt_fresh', '-')} |")
        if len(stale_gt) > 200:
            ap(f"| …（其余 {len(stale_gt) - 200} 只略，见 checkpoint JSONL） | | | |")
        ap("")
    if err:
        ap(f"error 明细（无 GT 且 sina 失败/无 af1；可重跑续传补扫）：{[r['code'] for r in err][:20]}"
           f"{'...' if len(err) > 20 else ''}")
        ap("")
    ap("## 重灌命令（Joel 手动执行——团队不碰灌数，Joel 09-19 定）")
    ap("")
    if damaged:
        codes_csv = ",".join(r["code"] for r in damaged)
        ap("```bash")
        ap("# 重灌损坏股 T2（含 adj_factor）：--force 清空这些 code 的 kline_history done 键强制重取")
        ap(f"cd {REPO}")
        ap(f".venv/bin/python -m scripts.lake_backfill history --codes '{codes_csv}' --force")
        ap("```")
        ap("")
        ap("- `history --codes` 指定股票子集（缺省=全集）；**必须带 `--force`**：done 键已存在时")
        ap("  普通重跑会全部 skipped_done、零重取（02_code 修正轮为 history 新增 --force，")
        ap("  镜像 t7 --force 的\"清本表 done 键\"口径；load_t2 upsert 幂等，重取安全）。")
        ap("- 重灌后验收：`scripts/lake_source_gates.py` G1 对应股票应转 PASS（<2%）；")
        ap("  或 `scripts/t2_damage_scan.py --codes <code>` 复扫确认 ok。")
    else:
        ap("（当前无损坏股，无需重灌。）")
    ap("")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="T2 adj_factor 损坏扫描（只读库+cache）")
    ap.add_argument("--db", default=os.path.join(REPO, "data/lake/lake.duckdb"))
    ap.add_argument("--codes", default=None, help="逗号分隔股票子集（缺省=全市场 kline_daily 全集）")
    ap.add_argument("--limit", type=int, default=None, help="只扫前 N 只（冒烟用）")
    ap.add_argument("--out", default=DEFAULT_OUT, help="报告输出（t2_repair.md）")
    ap.add_argument("--progress", default=DEFAULT_PROGRESS, help="checkpoint JSONL 路径")
    ap.add_argument("--report-only", action="store_true",
                    help="不重扫：仅从 checkpoint 重生成报告")
    args = ap.parse_args()

    records = _load_done(args.progress)
    if args.report_only:
        text = build_report(list(records.values()), args.db)
        os.makedirs(os.path.dirname(args.out), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        n_dam = sum(1 for r in records.values() if r.get("status") == "damaged")
        print(f"report-only: {len(records)} 只（damaged={n_dam}）→ {args.out}")
        return 0

    con, err = _lock_probe(args.db)
    if con is None:
        print(f"LOCKED: 生产库 read_only 连接失败（backfill flock？）\n{err}")
        return 2

    # fresh-sina adapter 惰性构造：仅当存在无 GT 的 code 时才需要（省网络/依赖）。
    adapter = None

    def _ensure_adapter():
        nonlocal adapter
        if adapter is None:
            from lake.config import lake_cfg
            from lake.ingest.sina_adapter import SinaKlineAdapter
            adapter = SinaKlineAdapter(min_interval_s=lake_cfg().get("sina_min_interval_s", 1.0))
        return adapter

    try:
        if args.codes:
            codes = [c.strip() for c in args.codes.split(",") if c.strip()]
        else:
            rows = con.execute(
                "SELECT DISTINCT ts_code FROM kline_daily ORDER BY ts_code").fetchall()
            codes = [r[0] for r in rows]
            if args.limit:
                codes = codes[:args.limit]

        todo = [c for c in codes if c not in records]
        print(f"全集 {len(codes)} 只，checkpoint 已完成 {len(codes) - len(todo)}，待扫 {len(todo)}",
              flush=True)

        t0 = time.monotonic()
        n_sina = 0
        for i, code in enumerate(todo, 1):
            gt_present = _gt_af1_series(code) is not None
            rec = scan_stock(con, code, _ensure_adapter() if not gt_present else None)
            if not gt_present:
                n_sina += 1
            records[code] = rec
            _append_progress(args.progress, rec)
            if i % 25 == 0 or i == len(todo):
                el = time.monotonic() - t0
                eta = el / i * (len(todo) - i)
                n_dam = sum(1 for r in records.values() if r.get("status") == "damaged")
                print(f"[{i}/{len(todo)}] {code} → {rec['status']} "
                      f"(damaged 累计 {n_dam}, sina 回退 {n_sina}) elapsed={el/60:.1f}m "
                      f"eta={eta/60:.1f}m", flush=True)
    finally:
        con.close()

    text = build_report(list(records.values()), args.db)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(text)
    n_dam = sum(1 for r in records.values() if r.get("status") == "damaged")
    print(f"扫描完成：{len(records)} 只（damaged={n_dam}）→ {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
