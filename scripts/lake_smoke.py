# -*- coding: utf-8 -*-
"""v6 数据湖最小冒烟（brief：构建任务但不全量运行，仅校验链路）。

纪律（最高优先级）：
- 只允许最小冒烟：**20 只 × 1 天**证明链路通；
- BaoStock 一律走 BaoStockClient（自带 QuotaGuard），**≤5 次**（本脚本只用 1 次：
  sh.601398 adj_factor 验前向填充）；
- 腾讯批量接口为主源（免费）；新浪本期冒烟不碰（T6 留全量 backfill）；
- Tushare 本期冒烟不碰（P2，装包后单独验）。

链路：建库 → T4(分红,零网络) → T1(本地缓存,零网络) → T3(腾讯快照) →
      T2(腾讯K线 + BaoStock adj_factor 前向填充) → T7(四指数末行) →
      hfq 连续性验证 → Web /api/lake/* 4 端点（由外部 curl 验）。
"""
from __future__ import annotations

import csv
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

CACHE_DIR = os.path.join(ROOT, "cache")
LAKE_DIR = os.path.join(ROOT, "data", "lake")


def hr(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def main():
    # ---- 0. 记录 BaoStock 配额前值（验收：≤10 次）----
    from screener.data.baostock_client import QuotaGuard, default_quota_path

    guard = QuotaGuard(path=default_quota_path())
    q_date, q_before = guard.get_state()
    hr("STEP 0 · BaoStock 配额前值")
    print(f"date={q_date} used={q_before} quota=49900 path={default_quota_path()}")

    # ---- 1. 建库（fresh：先删旧库证明从零建）----
    from lake import conn as lconn
    from lake.ddl import init_schema, TABLES

    db = lconn.default_db_path()
    if os.path.exists(db):
        os.remove(db)
    hr("STEP 1 · 建库（fresh data/lake/lake.duckdb）")
    con = lconn.open(db)
    tables = [r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='main' "
        "ORDER BY table_name").fetchall()]
    print(f"DB: {db}")
    print(f"tables({len(tables)}): {tables}")
    assert set(TABLES) <= set(tables), "9 表未全部创建"
    views = [r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_type='VIEW'").fetchall()]
    print(f"views: {views}")

    # ---- 2. T4 分红（em_dividend_all.csv，零网络）抽 sh.601398 验 dps 口径 ----
    from lake.ingest.em_dividend_ingest import load_dividends

    hr("STEP 2 · T4 分红（本地 em_dividend_all.csv，零网络）")
    n = load_dividends(con, CACHE_DIR, ts_codes=["sh.601398"])
    print(f"灌入 sh.601398 分红 {n} 行")
    rows = con.execute(
        "SELECT ex_date, period, cash_dps FROM dividend_events WHERE ts_code='sh.601398' "
        "ORDER BY ex_date DESC LIMIT 5").fetchall()
    print("最近 5 笔（ex_date, period, cash_dps 元/股）:")
    for r in rows:
        print(f"   {r[0]}  period={r[1]}  dps={r[2]}")
    # dps 口径验证：em dps_pretax 已 /10（元/股），601398 应 <5 元/股（非 50）
    max_dps = con.execute(
        "SELECT MAX(cash_dps) FROM dividend_events WHERE ts_code='sh.601398'").fetchone()[0]
    print(f"MAX(cash_dps)={max_dps} → {'OK(<5元/股,已/10)' if max_dps and max_dps < 5 else 'FAIL(口径异常)'}")
    assert max_dps is not None and max_dps < 5, "dps 口径异常（应已 /10）"

    # ---- 3. T1 股票主表（本地 all_stock + industry 缓存，零网络）20 只 ----
    from lake.ingest.common import to_ts_code

    hr("STEP 3 · T1 股票主表（本地缓存，零网络）")
    result_csv = os.path.join(ROOT, "output", "result_20260911.csv")
    with open(result_csv, encoding="utf-8-sig") as f:
        codes = []
        for row in csv.DictReader(f):
            c = (row.get("code") or "").strip()
            if c and c not in codes:
                codes.append(c)
            if len(codes) >= 20:
                break
    print(f"候选池 20 只（来自 {os.path.basename(result_csv)}）:")
    print("  " + ",".join(codes))

    # 读本地 all_stock（code,tradeStatus,code_name）+ industry（code→J66货币金融服务）
    def read_cache(fn):
        p = os.path.join(CACHE_DIR, fn)
        with open(p, encoding="utf-8") as f:
            r = csv.reader(f); next(r); cols = next(r)
            return cols, [dict(zip(cols, row)) for row in r]

    _, allstock = read_cache("allstock_2026-09-14.csv")
    name_map = {d["code"]: d.get("code_name", "") for d in allstock}
    _, ind_rows = read_cache("industry.csv")
    ind_map = {}
    for d in ind_rows:
        raw_ind = d.get("industry", "")  # "J66货币金融服务"
        code = d["code"]
        # 拆 csric2 码（字母+数字前缀）与名称
        import re as _re
        m = _re.match(r"^([A-Z]\d{2})(.*)$", raw_ind or "")
        if m:
            ind_map[code] = (m.group(1), m.group(2))

    from lake.ingest.common import now_ts, DATA_VERSION
    t1_n = 0
    for c in codes:
        nm = name_map.get(c, "")
        csric2, ind_name = ind_map.get(c, (None, None))
        board = ("科创" if c[3:] .startswith("68") else "创业" if c.split(".")[-1].startswith("30")
                 else "北交" if c.split(".")[-1][:1] in ("4", "8") else "主板")
        is_st = 1 if ("ST" in (nm or "").upper()) else 0
        con.execute(
            "INSERT OR REPLACE INTO stock_master (ts_code,name,industry_csric2,"
            "industry_name,list_date,delist_date,board,is_st,st_since,soe_flag,"
            "soe_basis,source,fetched_at,data_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [c, nm, csric2, ind_name, None, None, board, is_st, None,
             None, None, "local_cache", now_ts(), DATA_VERSION])
        t1_n += 1
    print(f"T1 灌入 {t1_n} 只（soe_flag 留 NULL——v5 soe 规则依赖 T6 股东，留全量 backfill）")
    sample = con.execute(
        "SELECT ts_code,name,industry_csric2,board,is_st FROM stock_master "
        "WHERE ts_code IN ('sh.601398','sh.600036','sz.000429') ORDER BY ts_code").fetchall()
    for s in sample:
        print(f"   {s[0]} {s[1]} [{s[2]}] {s[3]} is_st={s[4]}")

    # ---- 4. T3 估值（腾讯快照，免费）20 只 × 1 天 ----
    from lake.ingest.tencent_ingest import fetch_snapshot, load_t3
    from screener.data.tencent import TencentClient

    hr("STEP 4 · T3 估值（腾讯快照 qt.gtimg.cn，免费）")
    tclient = TencentClient()
    snaps = fetch_snapshot(tclient, codes)
    today = "2026-09-14"
    t3_n = 0
    for c in codes:
        if c in snaps:
            load_t3(con, c, snaps[c], today)
            t3_n += 1
    print(f"T3 灌入 {t3_n}/20 只（{today}）")
    s3 = con.execute(
        "SELECT ts_code,total_mv,float_mv,pe_ttm,pb,ttm_yield_pct FROM valuation_daily "
        "WHERE ts_code IN ('sh.601398','sh.600036') ORDER BY ts_code").fetchall()
    for r in s3:
        print(f"   {r[0]} total_mv={r[1]}亿 pe_ttm={r[3]} pb={r[4]} ttm_yield={r[5]}%")

    # ---- 5. T2 K线（腾讯 raw，免费）20 只 × 1 天 + sh.601398 adj_factor 前向填充 ----
    from lake.ingest.tencent_ingest import fetch_kline_ohlcv, load_t2

    hr("STEP 5 · T2 K线（腾讯 raw fqkline，免费）20 只 × 1 天")
    t2_n = 0
    for c in codes:
        kl = fetch_kline_ohlcv(tclient, c, n=1)  # 1 天（最小冒烟）
        if kl:
            load_t2(con, c, kl, adj_map=None)
            t2_n += 1
    print(f"T2(腾讯K线) 灌入 {t2_n}/20 只 × 1 天")

    # ---- 6. BaoStock adj_factor（≤5 次，本脚本 1 次）+ 本地全历史 → T2 前向填充 ----
    hr("STEP 6 · BaoStock adj_factor（sh.601398，1 次调用）+ 本地 kline_af3 全历史 → T2")
    from screener.data.baostock_client import BaoStockClient
    from lake.ingest.baostock_ingest import fetch_adjust_factor, load_t2_adj_factor
    from lake.ingest.common import forward_fill_af, clean_date, to_float
    from lake.ingest.local_cache_ingest import _read_cache_csv

    bs = BaoStockClient()   # 构造即挂 QuotaGuard（默认路径）
    try:
        fields, adj_rows = fetch_adjust_factor(bs, "sh.601398", "2016-09-01", "2026-09-14")
        print(f"query_adjust_factor 返回 {len(adj_rows)} 行（仅除权日）fields={fields}:")
        adj_map = load_t2_adj_factor(con, "sh.601398", adj_rows)
        for d in sorted(adj_map):
            print(f"   {d}  af={adj_map[d]}")
        # 已知锚点：2026-05-13 af=2.5545（brief Q4）
        assert "2026-05-13" in adj_map, "缺 2026-05-13 除权事件"
        af_anchor = adj_map["2026-05-13"]
        ok = abs(af_anchor - 2.5545) < 0.001
        print(f"锚点 2026-05-13 af={af_anchor} → {'OK(≈2.5545)' if ok else 'FAIL'}")
        assert ok, "af 锚点不符（应≈2.5545）"

        # 本地 raw close 全历史（kline_af3，零网络；close=不复权价，见 reconstruct.py）
        kl = _read_cache_csv(os.path.join(CACHE_DIR, "kline_af3_sh.601398.csv"))
        assert kl and kl["rows"], "kline_af3_sh.601398.csv 缺失/损坏"
        local_kl = []
        for row in kl["rows"]:
            d = dict(zip(kl["columns"], row))
            dt, c = clean_date(d.get("date")), to_float(d.get("close"))
            if dt and c is not None:
                local_kl.append({"date": dt, "open": None, "high": None,
                                 "low": None, "close": c, "volume": None})
        dates = [r["date"] for r in local_kl]
        print(f"本地 kline_af3 历史：{len(local_kl)} 天（{dates[0]} → {dates[-1]}）")

        # 前向填充验证（除权日 2026-05-13 前后）
        af_filled = forward_fill_af(dates, adj_map)
        pre_date = max((d for d in dates if d < "2026-05-13"), default=None)
        on_date = "2026-05-13"
        assert on_date in dates, "本地历史缺 2026-05-13"
        print("\n前向填充验证（除权日 2026-05-13 前后）:")
        raw = {r["date"]: r["close"] for r in local_kl}
        for d in [x for x in (pre_date, on_date) if x]:
            afv, rc = af_filled.get(d), raw.get(d)
            hfq = (rc * afv) if (afv is not None and rc is not None) else None
            print(f"   {d}  raw={rc}  af={afv}  hfq_close={'%.4f' % hfq if hfq else None}")
        assert af_filled.get(on_date) == af_anchor, "除权日 af 未取到锚点值"
        prev_af = af_filled.get(pre_date) if pre_date else None
        ok2 = prev_af is not None and prev_af < af_anchor
        print(f"\n连续性判定：前一日({pre_date}) af={prev_af} < 除权日 af={af_anchor} → "
              f"{'OK(前向填充正确,事件间恒定)' if ok2 else 'FAIL'}")
        assert ok2, "前向填充方向错误"

        # 全历史灌 T2（raw close + af 前向填充；upsert 幂等）
        n6 = load_t2(con, "sh.601398", local_kl, adj_map=adj_map)
        print(f"sh.601398 T2 已灌 {n6} 天历史（含 af 前向填充）")
    finally:
        bs.close()

    # ---- 7. hfq view 数值验证（Q4：hfq close = raw × af，跨除权日连续）----
    hr("STEP 7 · hfq view 数值验证（kline_daily_hfq）")
    hfq_rows = con.execute(
        'SELECT date, "close", adj_factor FROM kline_daily_hfq '
        "WHERE ts_code='sh.601398' AND date BETWEEN '2026-05-11' AND '2026-05-15' "
        "ORDER BY date").fetchall()
    print("hfq close（raw×af）除权日窗口:")
    prev_hfq = None
    for d, hc, af in hfq_rows:
        gap = "" if prev_hfq is None else f"  Δ={(hc/prev_hfq-1)*100:+.2f}%"
        print(f"   {d}  hfq_close={hc:.4f}  (af={af}){gap}")
        prev_hfq = hc
    # 定量连续性：raw 在除权日因分红下跌 >> hfq（af 吸收分红）→ |hfq变动| < |raw变动|
    raw2 = {str(d): c for d, c in con.execute(
        "SELECT date, close FROM kline_daily WHERE ts_code='sh.601398' "
        "AND date IN ('2026-05-12','2026-05-13')").fetchall()}
    hfq2 = {str(d): c for d, c in con.execute(
        'SELECT date, "close" FROM kline_daily_hfq WHERE ts_code=\'sh.601398\' '
        "AND date IN ('2026-05-12','2026-05-13')").fetchall()}
    if {"2026-05-12", "2026-05-13"} <= set(raw2) and {"2026-05-12", "2026-05-13"} <= set(hfq2):
        raw_chg = raw2["2026-05-13"] / raw2["2026-05-12"] - 1
        hfq_chg = hfq2["2026-05-13"] / hfq2["2026-05-12"] - 1
        print(f"除权日变动：raw {raw_chg*100:+.2f}%  vs  hfq {hfq_chg*100:+.2f}%")
        assert abs(hfq_chg) < abs(raw_chg), "hfq 不连续（分红未被 af 吸收）"
        print("OK：hfq close 跨除权日连续（分红被 adj_factor 吸收，Q4 验证通过）")
    else:
        print(f"⚠️ 连续性对比数据不全：raw={sorted(raw2)} hfq={sorted(hfq2)}（unverified）")

    # ---- 8. T7 四指数末行（腾讯，免费；含 sh000922）----
    from lake.ingest.tencent_ingest import INDEX_CODES, load_t7

    hr("STEP 8 · T7 四指数末行（腾讯 K线，免费）")
    for ic in INDEX_CODES:
        kl = fetch_kline_ohlcv(tclient, ic, n=5)
        if kl:
            load_t7(con, ic, kl)
            last = kl[-1]
            print(f"   {ic} 末行 {last['date']} close={last['close']}")
        else:
            print(f"   {ic} ⚠️ 无数据")
    idx_last = con.execute(
        "SELECT index_code, MAX(date), (SELECT close FROM index_daily i2 "
        "WHERE i2.index_code=i1.index_code ORDER BY date DESC LIMIT 1) "
        "FROM index_daily i1 GROUP BY index_code ORDER BY index_code").fetchall()
    print("四指数末行汇总:")
    for r in idx_last:
        print(f"   {r[0]}  last_date={r[1]}  close={r[2]}")
    assert len(idx_last) == 4, "四指数未全部灌入"

    # ---- 9. 配额后值 + coverage 汇总 ----
    hr("STEP 9 · BaoStock 配额后值 + 数据湖 coverage")
    q_date2, q_after = guard.get_state()
    consumed = q_after - q_before
    print(f"date={q_date2} used={q_after} (前 {q_before}) → 本次冒烟消耗 {consumed} 次")
    assert consumed <= 10, f"BaoStock 消耗 {consumed} 次 > 10（超限！）"
    print(f"\n各表 coverage:")
    for t in TABLES:
        cnt = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        codes_n = con.execute(
            f"SELECT COUNT(DISTINCT ts_code) FROM {t}").fetchone()[0] if "ts_code" in [
                d[0] for d in con.execute(f"SELECT * FROM {t} LIMIT 1").description] else "-"
        print(f"   {t:24s} rows={cnt:6d}  codes={codes_n}")

    # ---- 10. T8 因子（纯本地，零网络）算 601398 低波验证链路 ----
    hr("STEP 10 · T8 因子（factors.py 纯本地计算，零网络）")
    from lake import factors as lf
    f = lf.compute_stock_factors(con, "sh.601398", "2026-09-14")
    print(f"sh.601398 T8 因子: {f}")
    if f:
        lf.write_factors(con, "sh.601398", "2026-09-14", f)
        nfac = con.execute(
            "SELECT COUNT(*) FROM factor_snapshot WHERE ts_code='sh.601398'").fetchone()[0]
        print(f"factor_snapshot 写入 {nfac} 行")

    con.close()
    hr("SMOKE OK · 全链路通（建库→T4→T1→T3→T2+af→T7→hfq→T8）")
    print(f"BaoStock 消耗 {consumed} 次 (≤10) ✓")


if __name__ == "__main__":
    main()
