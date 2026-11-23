# -*- coding: utf-8 -*-
"""回归测试：缺陷 A（暴跌日 af 幻影盖章与反盖章）+ 缺陷 B（T4 区间分红增量与全链路修复）。"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import sys
from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _make_lake(tmp_path: Path):
    db_path = str(tmp_path / "lake.duckdb")
    con = duckdb.connect(db_path)
    from lake.ddl import init_schema
    init_schema(con)
    return db_path, con


# ---------------------------------------------------------------------------
# 1. 缺陷 A：暴跌日不得再被 suspected / unquantified_event 误盖章
# ---------------------------------------------------------------------------

def test_rebase_never_stamps_market_crash_or_unquantified_placeholder(tmp_path):
    import af_rebase
    db_path, con = _make_lake(tmp_path)
    try:
        # 案例 1：11-20 市场暴跌 -10% 且发生 5% 源漂移（无除权事件）→ suspected，严禁改成 10/9=1.1111
        con.executemany(
            "INSERT INTO kline_daily (ts_code, date, close, adj_factor, source) VALUES (?,?,?,?,?)",
            [
                ("sz.301219", "2026-11-19", 10.0, 3.4404, "em"),
                ("sz.301219", "2026-11-20", 9.0, 3.4404 * 1.05, "sina"),
                ("sz.301219", "2026-11-23", 9.1, 3.4404 * 1.05, "sina"),
            ],
        )
        # 案例 2：分红表只有日期占位（cash=0, stk=None）+ 大跌 + 源漂移 → unquantified_event，严禁吸收
        con.executemany(
            "INSERT INTO kline_daily (ts_code, date, close, adj_factor, source) VALUES (?,?,?,?,?)",
            [
                ("sh.600111", "2026-11-19", 20.0, 2.0, "em"),
                ("sh.600111", "2026-11-20", 17.0, 2.1, "sina"),
            ],
        )
        con.execute(
            "INSERT INTO dividend_events (ts_code, ex_date, cash_dps, stk_div, source) "
            "VALUES ('sh.600111', '2026-11-20', NULL, NULL, 'test')"
        )

        r1 = af_rebase.rebase_stock_af(con, "sz.301219")
        r2 = af_rebase.rebase_stock_af(con, "sh.600111")
        assert r1["rewritten"] == 0 and len(r1["suspected"]) == 1
        assert r1["suspected"][0]["kind"] == "suspected"
        assert r2["rewritten"] == 0 and len(r2["suspected"]) == 1
        assert r2["suspected"][0]["kind"] == "unquantified_event"

        # 源 af 原封不动（绝不写入 10/9 或 20/17 价格比）
        af_20 = con.execute(
            "SELECT adj_factor FROM kline_daily WHERE ts_code='sz.301219' AND date='2026-11-20'"
        ).fetchone()[0]
        assert af_20 == pytest.approx(3.4404 * 1.05)

        # repair_all_af 也不改写，仅计数 suspected_pairs
        rep = af_rebase.repair_all_af(con)
        assert rep["stocks_rebased"] == 0 and rep["suspected_pairs"] == 2
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 2. 缺陷 A：find_spurious_jumps 全史零截断 + 幻影检测与代数反盖章
# ---------------------------------------------------------------------------

def test_find_spurious_jumps_no_300k_truncation_and_phantom_unstamp(tmp_path):
    import af_rebase
    db_path, con = _make_lake(tmp_path)
    try:
        # 多幻影 + 真除权交错的合成股（模拟中国石油 sh.601857 / sz.301219）
        # d0=10-20(af=2.0), d1=10-30 暴跌 -13.2% 被盖章 ×1.152,
        # d2=11-05 真实除息 0.5 元 (10→9.5, exp_move=10/9.5),
        # d3=11-20 暴跌 -8.6% 再次被盖章 ×1.094, d4=11-23 正常日
        p_r1 = 10.0 / 8.68       # ≈ 1.152074
        exp_div = 10.0 / 9.5     # 真实分红步长
        p_r2 = 9.5 / 8.68        # ≈ 1.094470
        af0 = 2.0
        af1 = af0 * p_r1
        af2 = af1 * exp_div
        af3 = af2 * p_r2
        af4 = af3
        con.executemany(
            "INSERT INTO kline_daily (ts_code, date, close, adj_factor, source) VALUES (?,?,?,?,?)",
            [
                ("sz.301219", "2026-10-20", 10.00, af0, "em"),
                ("sz.301219", "2026-10-30", 8.68, af1, "em"),
                ("sz.301219", "2026-11-05", 9.50, af2, "em"),
                ("sz.301219", "2026-11-20", 8.68, af3, "em"),
                ("sz.301219", "2026-11-23", 8.80, af4, "em"),
            ],
        )
        # 构造前置平稳股票确保即使 max_pairs 场景外也能全量扫描（验证默认 max_pairs=None）
        con.executemany(
            "INSERT INTO kline_daily (ts_code, date, close, adj_factor, source) VALUES (?,?,?,?,?)",
            [
                ("sh.600000", "2026-10-20", 10.0, 1.0, "em"),
                ("sh.600000", "2026-10-21", 10.1, 1.0, "em"),
                ("zz.999999", "2026-10-20", 10.0, 1.0, "em"),
                ("zz.999999", "2026-10-21", 10.1, 3.0, "sina"),  # 尾股假跳变
            ],
        )
        # d2 上已有真实分红：d1 收盘 8.68 → 先设为 10.0 前收构造真实事件
        con.execute("UPDATE kline_daily SET close=10.0 WHERE ts_code='sz.301219' AND date='2026-10-30'")
        con.execute(
            "INSERT INTO dividend_events (ts_code, ex_date, cash_dps, stk_div, source) "
            "VALUES ('sz.301219', '2026-11-05', 0.5, 0.0, 'em_datacenter')"
        )

        # find_spurious_jumps 默认全扫描无截断
        all_jumps = af_rebase.find_spurious_jumps(con, since_date=None)
        assert any(j["ts_code"] == "zz.999999" and j["kind"] == "no_event" for j in all_jumps)

        # 扫描幻影边界：10-30（10.0/8.68 等调整后）——注意把 10-30 的 close 恢复为 8.68 后先测单一场景
        con.execute("UPDATE kline_daily SET close=8.68 WHERE ts_code='sz.301219' AND date='2026-10-30'")
        # exp_div_real 基于 8.68: 8.68/(8.68-0.5)
        exp_real = 8.68 / (8.68 - 0.5)
        con.execute("UPDATE kline_daily SET adj_factor=? WHERE ts_code='sz.301219' AND date='2026-11-05'",
                    [af1 * exp_real])
        con.execute("UPDATE kline_daily SET adj_factor=? WHERE ts_code='sz.301219' AND date IN ('2026-11-20','2026-11-23')",
                    [af1 * exp_real * p_r2])

        scan = af_rebase.find_phantom_stamps(con, since_date="2026-01-01")
        assert scan["stocks_affected"] == 1
        assert [p["date"] for p in scan["phantoms"]] == ["2026-10-30", "2026-11-20"]
        assert scan["event_distortions"] == []

        # Dry-run 不改库
        dry = af_rebase.un_stamp_phantoms(con, scan["phantoms"], dry_run=True)
        assert dry["boundaries"] == 2 and dry["rows_rewritten"] == 4
        assert con.execute(
            "SELECT adj_factor FROM kline_daily WHERE ts_code='sz.301219' AND date='2026-10-30'"
        ).fetchone()[0] == pytest.approx(af1)

        # Apply：精确恢复两个暴跌日平坦，同时完整保留 11-05 真实除息步长
        applied = af_rebase.un_stamp_phantoms(con, scan["phantoms"], dry_run=False)
        assert applied["rows_rewritten"] == 4
        afs = [
            r[0] for r in con.execute(
                "SELECT adj_factor FROM kline_daily WHERE ts_code='sz.301219' ORDER BY date"
            ).fetchall()
        ]
        assert afs[0] == pytest.approx(2.0)
        assert afs[1] == pytest.approx(2.0)                     # 10-30 暴跌日恢复平坦
        assert afs[2] == pytest.approx(2.0 * exp_real)          # 11-05 真实除息步长完整保留
        assert afs[3] == pytest.approx(2.0 * exp_real)          # 11-20 暴跌日恢复平坦
        assert afs[4] == pytest.approx(2.0 * exp_real)

        # qfq 下 10-30 的真实 -13.2% 跌幅恢复可见
        q0, q1 = con.execute(
            "SELECT close FROM kline_daily_qfq WHERE ts_code='sz.301219' AND date IN ('2026-10-20','2026-10-30') ORDER BY date"
        ).fetchall()
        assert q1[0] / q0[0] == pytest.approx(8.68 / 10.0)

        # 幂等重扫 0 幻影
        scan2 = af_rebase.find_phantom_stamps(con, since_date="2026-01-01")
        assert scan2["phantoms"] == []
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 3. 缺陷 B：区间分红抓取、事务落库、tushare 交叉校验、run_t4_sync 与日同步顺序
# ---------------------------------------------------------------------------

def test_em_fetch_range_load_and_tushare_crosscheck(monkeypatch, tmp_path):
    from lake.ingest import em_batch_ingest as emb
    from lake.ingest import tushare_ingest as ti
    import scripts.lake_backfill as drv

    pages = [
        {"result": {"pages": 2, "data": [
            {"SECURITY_CODE": "601998", "EX_DIVIDEND_DATE": 20261110,
             "IMPL_PLAN_NOTICE_DATE": 20261103, "REPORT_DATE": 20260630,
             "PRETAX_BONUS_RMB": 1.80, "BONUS_RATIO": 0.0, "IT_RATIO": 0.0,
             "ALLOT_RATIO": 0.0, "IMPL_PLAN_PROFILE": "10派1.8元", "ASSIGN_PROGRESS": "实施分配"},
            {"SECURITY_CODE": "600000", "EX_DIVIDEND_DATE": 20261110,
             "PRETAX_BONUS_RMB": 5.0, "BONUS_RATIO": 0.0, "IT_RATIO": 0.0,
             "ASSIGN_PROGRESS": "股东大会预案"},  # 未实施应过滤
            {"SECURITY_CODE": "920001", "EX_DIVIDEND_DATE": 20261110,
             "PRETAX_BONUS_RMB": 1.0, "ASSIGN_PROGRESS": "实施分配"},  # 北交所应过滤
        ]}},
        {"result": {"pages": 2, "data": [
            # 重复页漂移项应被去重
            {"SECURITY_CODE": "601998", "EX_DIVIDEND_DATE": 20261110,
             "IMPL_PLAN_NOTICE_DATE": 20261103, "REPORT_DATE": 20260630,
             "PRETAX_BONUS_RMB": 1.80, "BONUS_RATIO": 0.0, "IT_RATIO": 0.0,
             "ALLOT_RATIO": 0.0, "IMPL_PLAN_PROFILE": "10派1.8元", "ASSIGN_PROGRESS": "实施分配"},
            {"SECURITY_CODE": "300873", "EX_DIVIDEND_DATE": 20261112,
             "IMPL_PLAN_NOTICE_DATE": 20261105, "REPORT_DATE": 20260630,
             "PRETAX_BONUS_RMB": 1.00, "BONUS_RATIO": 2.0, "IT_RATIO": 2.0,
             "ALLOT_RATIO": 0.0, "IMPL_PLAN_PROFILE": "10派1送2转2", "ASSIGN_PROGRESS": "实施分配"},
        ]}},
    ]
    it = iter(pages)
    captured = []

    def fake_get(url, params):
        captured.append(params)
        return next(it)

    monkeypatch.setattr(emb, "_get_json", fake_get)
    monkeypatch.setattr(emb.time, "sleep", lambda s: None)

    db_path, con = _make_lake(tmp_path)
    try:
        # 旧数据停更在 2026-10-23
        con.execute(
            "INSERT INTO dividend_events (ts_code, ex_date, period, cash_dps, stk_div, source) "
            "VALUES ('sh.600001', '2026-10-23', '2025', 0.2, 0.0, 'em_local_static')"
        )
        # tushare 返回分歧（601998 故意差值大以便测 conflict_src 标记；300873 一致）
        monkeypatch.setattr(ti, "tushare_available", lambda: True)
        monkeypatch.setattr(
            ti, "fetch_dividend",
            lambda code: ([{"ex_date": "20261110", "div_cash_before_tax": 0.25}]
                          if code == "601998.SH"
                          else [{"ex_date": "20261112", "div_cash_before_tax": 0.10}]),
        )

        res = drv.run_t4_sync(con, db_path, end_date="2026-11-23", lookback_days=14)
        # 自动从 2026-10-23 - 14d = 2026-10-09 开始抓
        assert res["start_date"] == "2026-10-09"
        assert "(EX_DIVIDEND_DATE>='2026-10-09')(EX_DIVIDEND_DATE<='2026-11-23')" == captured[0]["filter"]
        assert res["fetched"] == 2 and res["written"] == 2
        assert res["max_ex_date"] == "2026-11-12"
        assert res["cross_check"]["checked"] == 2
        assert len(res["cross_check"]["mismatches"]) == 1

        # 校验落库每股口径 + ann_date + conflict_src
        r_300873 = con.execute(
            "SELECT cash_dps, stk_div, CAST(ann_date AS VARCHAR), period, conflict_src "
            "FROM dividend_events WHERE ts_code='sz.300873'"
        ).fetchone()
        assert r_300873 == (pytest.approx(0.1), pytest.approx(0.4), "2026-11-05", "2026", None)

        r_601998 = con.execute(
            "SELECT cash_dps, conflict_src FROM dividend_events WHERE ts_code='sh.601998'"
        ).fetchone()
        assert r_601998[0] == pytest.approx(0.18)
        assert "tushare:0.2500" in str(r_601998[1])

        # t4-sync 参数可正常解析
        args = drv.build_parser().parse_args(["t4-sync", "--lookback-days", "7", "--no-cross-check"])
        assert args.cmd == "t4-sync" and args.lookback_days == 7 and args.no_cross_check is True
    finally:
        con.close()


def test_run_lake_sync_daily_runs_t4_sync_before_incremental():
    sh = (PROJECT_ROOT / "scripts" / "run_lake_sync_daily.sh").read_text(encoding="utf-8")
    p_t4 = sh.find("lake_backfill.py t4-sync")
    p_inc = sh.find("lake_backfill.py incremental")
    assert p_t4 != -1 and p_inc != -1 and p_t4 < p_inc, "必须先执行 T4 分红增量，再跑 incremental(af_rebase)"


# ---------------------------------------------------------------------------
# 4. 生产修复工具端到端测试：同时修复幻影盖章 + 10送4失真 + 9/51被抹除权
# ---------------------------------------------------------------------------

def test_repair_af_and_dividends_end_to_end(tmp_path):
    import scripts.repair_af_and_dividends as rep

    db_path, con = _make_lake(tmp_path)
    try:
        # 1) 幻影盖章股 sz.301219（10-30 跌 -13.2% 被写成价格比 1.152，当日无除权）
        con.executemany(
            "INSERT INTO kline_daily (ts_code, date, close, adj_factor, source) VALUES (?,?,?,?,?)",
            [
                ("sz.301219", "2026-10-29", 10.00, 3.4404, "em"),
                ("sz.301219", "2026-10-30", 8.68, 3.4404 * (10.0 / 8.68), "em"),
                ("sz.301219", "2026-11-23", 8.50, 3.4404 * (10.0 / 8.68), "em"),
            ],
        )
        # 2) 被价格比覆盖失真的 10送4 股 sz.300873（官方步长 1.4138 被覆成 1.1758）
        c0_873, cash_873, bonus_873 = 10.0, 0.1, 0.4
        exp_873 = c0_873 * (1.0 + bonus_873) / (c0_873 - cash_873)  # 14/9.9 = 1.414141...
        con.executemany(
            "INSERT INTO kline_daily (ts_code, date, close, adj_factor, source) VALUES (?,?,?,?,?)",
            [
                ("sz.300873", "2026-06-01", c0_873, 1.0, "em"),
                ("sz.300873", "2026-06-02", c0_873 / 1.1758, 1.1758, "em"),
                ("sz.300873", "2026-11-23", 8.60, 1.1758, "em"),
            ],
        )
        # 3) 缺陷 B 停更导致被抹平 >2% 步长股 sh.601998（中信银行 1.0217）+ 0.5%~2% 中小步长股 sh.600036（1.012）
        con.executemany(
            "INSERT INTO kline_daily (ts_code, date, close, adj_factor, source) VALUES (?,?,?,?,?)",
            [
                ("sh.601998", "2026-11-04", 10.0, 2.0, "em"),
                ("sh.601998", "2026-11-05", 9.80, 2.0, "em"),  # 本该 2.0 * (10 / (10-0.2124))
                ("sh.601998", "2026-11-23", 9.90, 2.0, "em"),
                ("sh.600036", "2026-11-04", 10.0, 5.0, "em"),
                ("sh.600036", "2026-11-05", 9.90, 5.0, "em"),  # 中小步长 1.012 被抹平
                ("sh.600036", "2026-11-23", 9.95, 5.0, "em"),
            ],
        )
        # 现存分红表仅停在 2026-10-23
        con.execute(
            "INSERT INTO dividend_events (ts_code, ex_date, cash_dps, stk_div, source) "
            "VALUES ('sh.600001', '2026-10-23', 0.1, 0.0, 'em_local_static')"
        )
        con.execute("CHECKPOINT")
    finally:
        con.close()

    ev_file = tmp_path / "events.json"
    ev_file.write_text(json.dumps([
        {"ts_code": "sz.300873", "ex_date": "2026-06-02", "period": "2025",
         "cash": 0.1, "bonus": 0.4, "has_allot": False, "plan": "10派1送4"},
        {"ts_code": "sh.601998", "ex_date": "2026-11-05", "period": "2026",
         "cash": 0.2124, "bonus": 0.0, "has_allot": False, "plan": "10派2.124"},
        {"ts_code": "sh.600036", "ex_date": "2026-11-05", "period": "2026",
         "cash": 0.1186, "bonus": 0.0, "has_allot": False, "plan": "10派1.186"},
    ]), encoding="utf-8")

    report_dry = str(tmp_path / "dry.md")
    report_apply = str(tmp_path / "apply.md")

    # ① Dry-run：完全不改库，且可检出 sz.301219 与 sz.300873
    rc_dry = rep.main([
        "--db", db_path, "--events-json", str(ev_file),
        "--skip-tushare", "--skip-recompute", "--report-out", report_dry,
    ])
    assert rc_dry == 0
    con_ro = duckdb.connect(db_path, read_only=True)
    assert con_ro.execute("SELECT COUNT(*) FROM dividend_events").fetchone()[0] == 1
    assert con_ro.execute(
        "SELECT adj_factor FROM kline_daily WHERE ts_code='sz.301219' AND date='2026-10-30'"
    ).fetchone()[0] == pytest.approx(3.4404 * (10.0 / 8.68))
    con_ro.close()

    # ② Apply：全量修复 + 生成备份 + 验收清零
    rc_app = rep.main([
        "--db", db_path, "--apply", "--events-json", str(ev_file),
        "--skip-tushare", "--skip-recompute", "--report-out", report_apply,
    ])
    assert rc_app == 0
    assert any(p.name.startswith("lake.duckdb.bak_") for p in tmp_path.iterdir())

    con_v = duckdb.connect(db_path, read_only=True)
    try:
        #分红表补齐至 11-05
        assert str(con_v.execute("SELECT MAX(ex_date) FROM dividend_events").fetchone()[0])[:10] == "2026-11-05"

        # 1) sz.301219 暴跌日反盖章恢复平坦 3.4404
        afs_1219 = [r[0] for r in con_v.execute(
            "SELECT adj_factor FROM kline_daily WHERE ts_code='sz.301219' ORDER BY date").fetchall()]
        assert all(abs(a - 3.4404) < 1e-9 for a in afs_1219), afs_1219

        # 2) sz.300873 10送4 官方步长精准恢复
        afs_873 = [r[0] for r in con_v.execute(
            "SELECT adj_factor FROM kline_daily WHERE ts_code='sz.300873' ORDER BY date").fetchall()]
        assert afs_873[1] / afs_873[0] == pytest.approx(exp_873)
        assert afs_873[2] / afs_873[1] == pytest.approx(1.0)

        # 3) sh.601998 (>2%) 与 sh.600036 (0.5%~2%) 真实分红步长全部恢复
        afs_998 = [r[0] for r in con_v.execute(
            "SELECT adj_factor FROM kline_daily WHERE ts_code='sh.601998' ORDER BY date").fetchall()]
        assert afs_998[1] / afs_998[0] == pytest.approx(10.0 / (10.0 - 0.2124))
        assert afs_998[2] == pytest.approx(afs_998[1])

        afs_036 = [r[0] for r in con_v.execute(
            "SELECT adj_factor FROM kline_daily WHERE ts_code='sh.600036' ORDER BY date").fetchall()]
        assert afs_036[1] / afs_036[0] == pytest.approx(10.0 / (10.0 - 0.1186))
        assert afs_036[2] == pytest.approx(afs_036[1])

        # 4) 重扫零残余跳变、零幻影、零失真
        import af_rebase
        assert af_rebase.find_spurious_jumps(con_v, since_date=None) == []
        ph = af_rebase.find_phantom_stamps(con_v, since_date="2026-01-01")
        assert ph["phantoms"] == [] and ph["event_distortions"] == []
    finally:
        con_v.close()
    assert "残余幻影=0" in Path(report_apply).read_text(encoding="utf-8")
