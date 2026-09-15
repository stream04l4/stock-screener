# -*- coding: utf-8 -*-
"""test_lake_v607_history_fullhistory —— v6.0.7 history 全史补库运行时缺陷修复回归。

缺陷与验收点（v6.0.7 brief，TL 实测证据）：
1. **腾讯 fqkline n 上限=2000**（n>2000 → param error）→ 旧 run_history 用
   n=12000 全市场 K线 100% 取空。新增 ``fetch_kline_full_history``：日期段分页
   （param={tcode},day,{start},{end},{n},{fq}）从最新往回翻到 IPO 边界，升序合并
   去重；page_size>2000 → clamp（绝不越界发请求）。
2. **失败必须可感知**：单页重试耗尽（沿用 client.max_attempts）→ 抛 RuntimeError；
   部分成功（某页失败但已有数据）→ 同样抛错（宁缺毋滥，重跑幂等）。旧
   fetch_kline_ohlcv 签名与行为不变（p0/T7/smoke 调用方零影响）。
3. **done 键毒化**：worker 取空/失败 → 不 mark_done（下轮重试）；
   done 键 period_or_date 固定 "full_history"（不含日期）→ 断点续传跨天有效。

纪律：全部离线——腾讯 session.get / BaoStockClient / fetch_adjust_factor 均
monkeypatch fake（计数断言零真实网络），库一律 tmp_path，不碰 data/lake/。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys

import pytest

duckdb = pytest.importorskip("duckdb")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)


# ===========================================================================
# fake 基础设施（离线，零网络）
# ===========================================================================
def _row(date_s: str, i: int) -> list:
    """腾讯日K行格式 [date, open, close, high, low, volume]（解析顺序 r[1..5]）。"""
    return [date_s, "10.0", "10.5", "11.0", "9.5", str(100 + (i % 7))]


def _rows_ending(end_date: _dt.date, count: int) -> list:
    """生成 count 根连续日K（升序，末行=end_date）。"""
    return [_row((end_date - _dt.timedelta(days=count - 1 - i)).isoformat(), i)
            for i in range(count)]


class _PageSession:
    """假 requests.Session：按调用次序弹出预置页；记录每次请求的 (tcode,start,end,n)。

    pages 元素 = list[行]（正常页）或 Exception 实例（该次 get 直接抛出，模拟网络失败）。
    pages 耗尽 → 返回空 day（= 翻过 IPO 边界，翻页终止信号）。
    """

    def __init__(self, pages):
        self.pages = list(pages)
        self.requests = []

    def get(self, url, timeout=None):
        import re

        m = re.search(r"param=([^,&]+),day,([^,]*),([^,]*),(\d+)", url)
        tcode, start, end, n = (m.group(1), m.group(2), m.group(3), int(m.group(4)))
        self.requests.append((tcode, start, end, n))
        if not self.pages:
            body = {"code": 0, "msg": "", "data": {tcode: {"day": []}}}
        else:
            page = self.pages.pop(0)
            if isinstance(page, Exception):
                raise page
            body = {"code": 0, "msg": "", "data": {tcode: {"day": page}}}

        class _R:
            status_code = 200
            content = json.dumps(body).encode("utf-8")

        return _R()


class _FakeClient:
    """最小 TencentClient 替身：只带 session/timeout/max_attempts。"""

    def __init__(self, session, max_attempts=3):
        self.session = session
        self.timeout = 1.0
        self.max_attempts = max_attempts


def _import_ti():
    import lake.ingest.tencent_ingest as ti

    return ti


# ===========================================================================
# 1) fetch_kline_full_history：分页拼接（mock session.get，离线）
# ===========================================================================
def test_full_history_three_pages_ascending_and_dedup():
    """3 页拼接（2000+2000+812，TL 实测 sh601398 同款形状）→ 全量升序、无重复 date。

    翻页断言：page1 无日期段；page2 end=page1首日-1天；page3 end=page2首日-1天；
    page3 行数<请求数 → 停止（不再发第 4 个请求）。页间不重叠（TL 实测口径：
    end 含端点、与上页首日衔接），合并后 date 唯一。
    """
    import requests

    ti = _import_ti()
    latest = _dt.date(2026, 9, 15)
    p1 = _rows_ending(latest, 2000)
    oldest1 = _dt.date.fromisoformat(p1[0][0])
    p2 = _rows_ending(oldest1 - _dt.timedelta(days=1), 2000)
    oldest2 = _dt.date.fromisoformat(min(r[0] for r in p2))
    p3 = _rows_ending(oldest2 - _dt.timedelta(days=1), 812)

    sess = _PageSession([p1, p2, p3])
    client = _FakeClient(sess)
    out = ti.fetch_kline_full_history(client, "sh.601398")   # page_size 缺省 2000

    dates = [r["date"] for r in out]
    assert len(out) == 2000 + 2000 + 812, f"应合并 {4812} 行: {len(out)}"
    assert dates == sorted(dates), "结果必须升序"
    assert len(set(dates)) == len(dates), "date 不得重复"
    assert dates[0] == p3[0][0] and dates[-1] == latest.isoformat()
    # OHLCV 解析正确（r[1]=open, r[2]=close, r[3]=high, r[4]=low, r[5]=volume）
    assert out[-1] == {"date": latest.isoformat(), "open": 10.0, "close": 10.5,
                       "high": 11.0, "low": 9.5, "volume": float(p1[-1][5])}

    # 翻页算法断言（TL 实测同款）：n 恒=2000；end 逐页 = 上一页首日-1天
    assert len(sess.requests) == 3, f"应恰好 3 个请求: {sess.requests[:4]}"
    tcode = "sh601398"
    assert sess.requests[0] == (tcode, "", "", 2000), f"首页必须无日期段: {sess.requests[0]}"
    assert sess.requests[1][2] == (oldest1 - _dt.timedelta(days=1)).isoformat()
    assert sess.requests[2][2] == (oldest2 - _dt.timedelta(days=1)).isoformat()
    for _, s, e, n in sess.requests:
        assert n == 2000 and s == ""


def test_full_history_dedup_by_date_on_overlap():
    """去重 by date（防御契约）：若服务端页边界重叠（page2 多返回上页首日 1 行），
    合并结果仍 date 唯一、升序。"""
    ti = _import_ti()
    latest = _dt.date(2026, 9, 15)
    p1 = _rows_ending(latest, 5)                       # 满页 n=5 → 翻页
    oldest1 = _dt.date.fromisoformat(p1[0][0])
    # page2（end=oldest1-1）却含 1 行重叠日期 oldest1（防御场景）+ 2 行新数据
    p2 = [list(p1[0])] + _rows_ending(oldest1 - _dt.timedelta(days=2), 2)
    sess = _PageSession([p1, p2])
    out = ti.fetch_kline_full_history(_FakeClient(sess), "sh.601398", page_size=5)
    dates = [r["date"] for r in out]
    assert len(out) == 7, f"重叠行应去重（5+2）: {dates}"
    assert dates == sorted(dates) and len(set(dates)) == len(dates)


def test_full_history_single_page_is_boundary():
    """首页行数<请求数（新股/短历史）→ 1 页即边界，只发 1 个请求。"""
    ti = _import_ti()
    p1 = _rows_ending(_dt.date(2026, 9, 15), 812)
    sess = _PageSession([p1])
    out = ti.fetch_kline_full_history(client := _FakeClient(sess), "sz.300750")
    assert len(out) == 812
    assert [r["date"] for r in out] == sorted(r["date"] for r in out)
    assert len(sess.requests) == 1, f"短历史应只取 1 页: {sess.requests}"


def test_full_history_all_pages_fail_raises_runtimeerror():
    """整轮全部失败（网络异常，重试耗尽）→ 抛 RuntimeError（不得静默返回 []）。"""
    import requests

    ti = _import_ti()
    sess = _PageSession([requests.ConnectionError("boom")] * 10)
    client = _FakeClient(sess, max_attempts=3)
    with pytest.raises(RuntimeError) as ei:
        ti.fetch_kline_full_history(client, "sh.601398")
    assert "sh601398" in str(ei.value)
    # 只尝试了首页（重试 3 次后抛错，不翻页）
    assert len(sess.requests) == 3, f"应仅首页 3 次重试: {len(sess.requests)}"


def test_full_history_http_error_page_raises_after_retries():
    """单页 HTTP 500（非网络异常路径）→ 同样重试 max_attempts 次后抛 RuntimeError。"""
    ti = _import_ti()

    class _ErrSession:
        def __init__(self):
            self.calls = 0

        def get(self, url, timeout=None):
            self.calls += 1

            class _R:
                status_code = 500
                content = b"internal error"

            return _R()

    sess = _ErrSession()
    with pytest.raises(RuntimeError):
        ti.fetch_kline_full_history(_FakeClient(sess, max_attempts=3), "sh.601398")
    assert sess.calls == 3


def test_full_history_partial_success_page2_fails_raises():
    """部分成功（page1 已有数据、page2 重试耗尽）→ 也抛错（宁缺毋滥，重跑幂等）。"""
    import requests

    ti = _import_ti()
    p1 = _rows_ending(_dt.date(2026, 9, 15), 2000)   # 满页 → 必然触发翻页
    # page2 每次 get 都抛网络异常（重试 3 次耗尽）；page3 永不可达
    sess = _PageSession([p1] + [requests.Timeout("slow")] * 6)
    with pytest.raises(RuntimeError) as ei:
        ti.fetch_kline_full_history(_FakeClient(sess, max_attempts=3), "sh.601398")
    assert "end=" in str(ei.value)   # 报错须定位到失败的页（含 end 参数）
    assert len(sess.requests) == 4, f"首页 1 次 + page2 重试 3 次: {len(sess.requests)}"


# ===========================================================================
# 2) n=2000 上限：绝不越界发请求
# ===========================================================================
def test_full_history_page_size_clamped_to_2000():
    """page_size>2000（如旧代码的 12000）→ clamp 到 2000，URL 中 n 恒 ≤2000。"""
    ti = _import_ti()
    p1 = _rows_ending(_dt.date(2026, 9, 15), 812)
    sess = _PageSession([p1])
    out = ti.fetch_kline_full_history(_FakeClient(sess), "sh.601398", page_size=12000)
    assert len(out) == 812
    for _, s, e, n in sess.requests:
        assert n == 2000, f"n 越界（腾讯上限=2000）: {sess.requests}"


def test_full_history_ohlcv_legacy_unchanged():
    """fetch_kline_ohlcv 签名与行为保持不变（p0/T7/smoke 调用方零影响）。"""
    import inspect

    import requests

    ti = _import_ti()
    sig = inspect.signature(ti.fetch_kline_ohlcv)
    assert list(sig.parameters) == ["client", "ts_code", "n"]

    # 失败仍返回 []（旧语义不变——只有新函数改抛错）
    class _FailSession:
        def get(self, url, timeout=None):
            raise requests.ConnectionError("no network in test")

    out = ti.fetch_kline_ohlcv(_FakeClient(_FailSession(), max_attempts=2), "sh.601398", n=5)
    assert out == []


# ===========================================================================
# 3) run_history worker：失败不 mark_done + done 键稳定化（跨天续传）
# ===========================================================================
def _seed_db(db_path: str):
    from lake import conn as lconn

    con = lconn.open(db_path)
    con.close()


def _fake_bs_fields():
    return (["code", "dividOperateDate", "foreAdjustFactor",
             "backAdjustFactor", "adjustFactor"], [])


def _run_history_offline(monkeypatch, tmp_path, db, codes, start, end,
                         full_history_fake, adj_fake=None):
    """离线跑 run_history：fake 腾讯全史/BaoStock/快照 + progress 重定向 tmp。"""
    import lake_backfill as drv
    from lake import backfill as lb
    from lake.ingest import baostock_ingest as bsi
    import lake.ingest.tencent_ingest as ti
    import screener.data.baostock_client as bsc
    import screener.data.tencent as tmod

    class _FakeBS:
        def close(self):
            pass

    class _FakeTClient:
        pass

    monkeypatch.setattr(lb, "_progress_path", lambda: str(tmp_path / "progress.json"))
    monkeypatch.setattr(lb.BackfillRunner, "_quota_state", lambda self: (False, 0))
    monkeypatch.setattr(bsc, "BaoStockClient", _FakeBS)
    monkeypatch.setattr(tmod, "TencentClient", _FakeTClient)
    monkeypatch.setattr(ti, "fetch_kline_full_history", full_history_fake)
    if adj_fake is not None:
        monkeypatch.setattr(bsi, "fetch_adjust_factor", adj_fake)
    else:
        monkeypatch.setattr(bsi, "fetch_adjust_factor",
                            lambda bs, code, s, e: _fake_bs_fields())

    from lake import conn as lconn

    con = lconn.open(db)
    try:
        return drv.run_history(con, db, codes, start, end), str(tmp_path / "progress.json")
    finally:
        con.close()


def test_run_history_worker_failure_not_marked_done(tmp_path, monkeypatch):
    """worker 抛错（腾讯取空/网络失败）→ **不 mark_done**、kline_daily 零行、
    errors 记录该任务（下轮重跑重试）。"""
    db = str(tmp_path / "h_fail.duckdb")
    _seed_db(db)

    def boom(client, ts_code, page_size=2000):
        raise RuntimeError(f"腾讯K线全史单页重试 3 次仍失败 tcode={ts_code}")

    stats, prog_path = _run_history_offline(
        monkeypatch, tmp_path, db, ["sh.601398"], "1990-01-01", "2026-09-15", boom)

    assert stats["processed"] == 0
    assert len(stats["errors"]) == 1 and "sh.601398" in stats["errors"][0]
    # done 键未落盘（毒化修复核心断言）
    prog = json.load(open(prog_path, encoding="utf-8"))
    kh_done = [k for k in prog.get("done", []) if k[0] == "kline_history"]
    assert kh_done == [], f"失败任务不得标 done: {kh_done}"
    # 库零污染
    from lake import conn as lconn

    con = lconn.open(db)
    n = con.execute("SELECT COUNT(*) FROM kline_daily").fetchone()[0]
    con.close()
    assert n == 0


def test_run_history_success_marks_done_with_stable_key(tmp_path, monkeypatch):
    """成功路径：全史 K线落库 + adj_factor 前向填充 + done 键 = ("kline_history",
    code, "full_history")（固定字符串，不含日期）。"""
    db = str(tmp_path / "h_ok.duckdb")
    _seed_db(db)

    days = ["2026-09-10", "2026-09-11", "2026-09-14", "2026-09-15"]
    rows = [{"date": d, "open": 10.0, "high": 11.0, "low": 9.5,
             "close": 10.5, "volume": 100.0} for d in days]

    def fake_full(client, ts_code, page_size=2000):
        return [dict(r) for r in rows]

    # BaoStock adj_factor：仅除权日有行（2026-09-14 除权 af=2.5）→ load_t2 前向填充
    def fake_adj(bs, code, s, e):
        return (["code", "dividOperateDate", "foreAdjustFactor",
                 "backAdjustFactor", "adjustFactor"],
                [[code, "2026-09-14", "1.2", "1.3", "2.5"]])

    stats, prog_path = _run_history_offline(
        monkeypatch, tmp_path, db, ["sh.601398"], "1990-01-01", "2026-09-15",
        fake_full, adj_fake=fake_adj)

    assert stats["processed"] == 1 and not stats["errors"], f"{stats}"
    prog = json.load(open(prog_path, encoding="utf-8"))
    assert ["kline_history", "sh.601398", "full_history"] in prog.get("done", []), \
        f"done 键必须稳定为 full_history: {prog.get('done')}"

    from lake import conn as lconn

    con = lconn.open(db)
    got = con.execute(
        "SELECT date, adj_factor FROM kline_daily WHERE ts_code='sh.601398' "
        "ORDER BY date").fetchall()
    con.close()
    assert [str(g[0]) for g in got] == days   # duckdb DATE 列 → datetime.date，转 str 比
    # 前向填充：除权日之前的行 NULL、除权日起全部 =2.5
    assert got[0][1] is None and got[1][1] is None
    assert all(g[1] == 2.5 for g in got[2:])


def test_run_history_done_key_stable_across_days(tmp_path, monkeypatch):
    """跨天重跑（end_date 从今日变次日）→ done 键仍匹配 → **全跳过、零重取**。

    旧缺陷：period=f"{start}~{end}" 含当日日期 → 每天重跑全量失配。
    """
    db = str(tmp_path / "h_crossday.duckdb")
    _seed_db(db)
    rows = [{"date": "2026-09-15", "open": 10.0, "high": 11.0, "low": 9.5,
             "close": 10.5, "volume": 100.0}]

    counter = {"fetch": 0}

    def fake_full(client, ts_code, page_size=2000):
        counter["fetch"] += 1
        return [dict(r) for r in rows]

    # ---- Run 1（end=今日）----
    s1, _ = _run_history_offline(
        monkeypatch, tmp_path, db, ["sh.601398"], "1990-01-01", "2026-09-15", fake_full)
    assert s1["processed"] == 1 and counter["fetch"] == 1

    # ---- Run 2（end=次日，模拟次日重跑）→ done 键 "full_history" 不变 → 全跳过 ----
    counter["fetch"] = 0
    s2, _ = _run_history_offline(
        monkeypatch, tmp_path, db, ["sh.601398"], "1990-01-01", "2026-09-16", fake_full)
    assert s2["skipped_done"] == 1 and s2["processed"] == 0, f"次日重跑应全跳过: {s2}"
    assert counter["fetch"] == 0, f"跳过任务不得重新取数: {counter}"
