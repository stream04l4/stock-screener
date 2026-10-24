# -*- coding: utf-8 -*-
"""test_timing —— v7.0.13：选股时机（候选池 ①价格分位 ②股息率 ③年线 + 新触发推送）。

契约（全离线，零真实湖/零网络）：
- config：timing 段缺省 → 生产默认值；段存在时 5 键齐全 + 范围/类型校验；
- refresh_timing：①分位/回撤计算、②股息边界、③年线开关与数据不足 fail-closed、
  OR/AND 双口径、整表刷新幂等；
- check_and_push 边沿语义：新触发才推、持续在买区不重推、掉出清除、再进入重推、
  推送失败不落状态（下轮重试）、缺 token 不推不炸；
- Web GET /api/timing（fake 湖：命中排序/push_eligible 标记/空表 fail-closed）。
"""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Dict, List

import duckdb
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import web.app as wapp  # noqa: E402
from martingale.config import (ConfigError, TimingParams, UniverseParams,  # noqa: E402
                               load_config)
from martingale import timing as tm  # noqa: E402
from martingale.universe import Member  # noqa: E402


def make_tp(**kw) -> TimingParams:
    base = dict(price_window_years=1, price_pctile_max=50.0, yield_min_pct=4.5,
                below_ma250=False, ma_period=250)
    base.update(kw)
    return TimingParams(**base)


def member(code, yield_pct=4.0, price=10.0) -> Member:
    return Member(ts_code=code, name=f"股{code}", industry_csric2="B06",
                  soe_basis="关键词命中", total_mv=300.0, ttm_yield_pct=yield_pct,
                  n_bars=2400, list_date="2010-01-01",
                  bt_pnl_pct=0.1, bt_n_buys=3, price_latest=price, is_manual=False)


def make_lake(series: Dict[str, List[float]],
              price_only: List[str] = ()) -> duckdb.DuckDBPyConnection:
    """内存湖：kline_daily（最新价）+ kline_daily_qfq（窗口序列，日期自 2025-10-24 起逐日）。

    :param price_only: 只有 kline_daily 最新价、无 qfq 序列的代码（fail-closed 用例）。
    """
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE kline_daily (ts_code VARCHAR, date DATE, close DOUBLE)")
    con.execute("""CREATE TABLE kline_daily_qfq (ts_code VARCHAR, date DATE,
                   open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
                   volume DOUBLE, amount DOUBLE, pct_chg DOUBLE, is_st INTEGER)""")
    from datetime import date, timedelta
    start = date(2025, 10, 24)
    for code, closes in series.items():
        for i, c in enumerate(closes):
            d = start + timedelta(days=i)
            con.execute("INSERT INTO kline_daily_qfq VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0, 0)",
                        [code, str(d), c, c, c, c])
        con.execute("INSERT INTO kline_daily VALUES (?, ?, ?)",
                    [code, str(start + timedelta(days=len(closes) - 1)), closes[-1]])
    for code in price_only:
        con.execute("INSERT INTO kline_daily VALUES (?, ?, ?)",
                    [code, "2026-10-23", 10.0])
    return con


# ---------------------------------------------------------------------------
# 1) config：timing 段缺省 + 校验
# ---------------------------------------------------------------------------
def _valid_yaml(with_timing: bool = True) -> str:
    y = """
strategy:
  name: drawdown_martingale
  initial_amount: 100000
  base_add_amount: 10000
  first_drawdown_pct: 15
  add_step_pct: 5
  growth_ratio: 1.1
  rearm_mode: bounce
  rearm_pct: 15
  accounting: cash
universe:
  industry_whitelist_csric2: [B06]
  soe_keywords: [国资]
  min_total_mv_yi: 200
  min_consecutive_div_years: 5
  listing_min_trading_days: 250
  backtest_positive: true
  backtest_window_years: 10
  min_ttm_yield_pct: 3
  price_min_yuan: 3
  price_max_yuan: 100
"""
    if with_timing:
        y += """
timing:
  price_window_years: 5
  price_pctile_max: 40
  yield_min_pct: 4.5
  below_ma250: false
  ma_period: 250
"""
    return y


def test_config_timing_defaults_when_missing(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text(_valid_yaml(with_timing=False), encoding="utf-8")
    _sp, _up, tp = load_config(str(p))
    assert tp == TimingParams()  # 缺省 = 生产默认（5/40/4.5/false/250）


def test_config_timing_valid_roundtrip(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text(_valid_yaml(), encoding="utf-8")
    _sp, _up, tp = load_config(str(p))
    assert (tp.price_window_years, tp.price_pctile_max, tp.yield_min_pct,
            tp.below_ma250, tp.ma_period) == (5, 40.0, 4.5, False, 250)


@pytest.mark.parametrize("mutate,match", [
    (lambda d: d["timing"].pop("ma_period"), "ma_period 缺失"),
    (lambda d: d["timing"].update(price_pctile_max=150), "price_pctile_max"),
    (lambda d: d["timing"].update(yield_min_pct=-1), "yield_min_pct"),
    (lambda d: d["timing"].update(below_ma250="yes"), "below_ma250 必须是布尔"),
    (lambda d: d["timing"].update(price_window_years=0), "price_window_years"),
    (lambda d: d["timing"].update(ma_period=10), "ma_period"),
])
def test_config_timing_rejects_invalid(tmp_path, mutate, match):
    d = yaml.safe_load(_valid_yaml())
    mutate(d)
    p = tmp_path / "s.yaml"
    p.write_text(yaml.safe_dump(d, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ConfigError, match=match):
        load_config(str(p))


# ---------------------------------------------------------------------------
# 2) refresh_timing：条件逻辑
# ---------------------------------------------------------------------------
def test_refresh_timing_conditions():
    N = 300
    series = {
        "a.1": [10.0] * N,                 # 分位 100（高位）+ 股息 5.0 → 仅②
        "b.2": [20.0] * (N - 1) + [10.0],  # 分位 ≈0.3（低位）+ 股息 4.6 → ①② → push
        "c.3": [10.0] * N,                 # 高位 + 股息 3.0 → 无触发
    }
    con = make_lake(series, price_only=["d.4"])  # d.4 无 qfq（fail-closed）
    members = [member("a.1", 5.0), member("b.2", 4.6),
               member("c.3", 3.0), member("d.4", 6.0)]
    tp = make_tp()
    out = tm.refresh_timing(con, up=None, tp=tp, members=members)
    assert out["n_codes"] == 4 and out["n_watchlist"] == 3 and out["n_push"] == 1

    rows = {r[0]: r for r in con.execute(f"SELECT * FROM {tm.TABLE}").fetchall()}
    # 列序: ts_code(0) name(1) as_of(2) close(3) pctile(4) dd(5) yield(6)
    #       ma(7) below_ma(8) c1(9) c2(10) c3(11) watch(12) push(13) reasons(14)
    assert rows["a.1"][9] is False and rows["a.1"][10] is True
    assert rows["a.1"][12] is True and rows["a.1"][13] is False
    assert rows["b.2"][9] is True and rows["b.2"][10] is True
    assert rows["b.2"][12] is True and rows["b.2"][13] is True
    assert abs(rows["b.2"][4] - 1 / N * 100) < 0.01      # 分位 ≈ 0.33%
    assert abs(rows["b.2"][5] - 50.0) < 1e-9             # 回撤 50%
    assert "价格分位" in rows["b.2"][14] and "股息率" in rows["b.2"][14]
    assert rows["c.3"][12] is False and rows["c.3"][13] is False
    # d.4 无 qfq → ① fail-closed False，② 命中
    assert rows["d.4"][9] is False and rows["d.4"][10] is True
    assert rows["d.4"][4] is None and rows["d.4"][5] is None

    # 整表刷新幂等（二次刷新不累积）
    tm.refresh_timing(con, up=None, tp=tp, members=members)
    assert con.execute(f"SELECT COUNT(*) FROM {tm.TABLE}").fetchone()[0] == 4
    con.close()


def test_refresh_timing_below_ma():
    N = 300
    series = {
        "a.1": [10.0] * N,                       # 均线=10，close<ma 假
        "b.2": [20.0] * (N - 1) + [10.0],        # 均线≈19.9，close<ma 真
        "e.5": [10.0] * 100,                     # 不足 ma_period → ma None（fail-closed）
    }
    con = make_lake(series)
    members = [member("a.1", 5.0), member("b.2", 4.6), member("e.5", 4.6)]
    tp = make_tp(below_ma250=True, ma_period=250)
    tm.refresh_timing(con, up=None, tp=tp, members=members)
    rows = {r[0]: r for r in con.execute(f"SELECT * FROM {tm.TABLE}").fetchall()}
    # a.1: ①假 ②真 ③假 → watch 真、push 假（③关闸 AND）
    assert rows["a.1"][9] is False and rows["a.1"][10] is True and rows["a.1"][11] is False
    assert rows["a.1"][12] is True and rows["a.1"][13] is False
    # b.2: ①真 ②真 ③真 → push 真
    assert rows["b.2"][9] and rows["b.2"][10] and rows["b.2"][11]
    assert rows["b.2"][12] and rows["b.2"][13]
    assert "低于250日线" in rows["b.2"][14]
    # e.5: 数据不足 → ma None、③ NULL（不触发）；①分位 100 假、②真 → 仅 watch
    assert rows["e.5"][7] is None and rows["e.5"][8] is None and rows["e.5"][11] is None
    assert rows["e.5"][9] is False and rows["e.5"][10] is True
    assert rows["e.5"][12] is True and rows["e.5"][13] is False
    con.close()


# ---------------------------------------------------------------------------
# 3) check_and_push：边沿状态语义
# ---------------------------------------------------------------------------
def _lake_with_push(con):
    con.execute(f"CREATE TABLE {tm.TABLE} (ts_code VARCHAR PRIMARY KEY, name VARCHAR, "
                "as_of_date DATE, close DOUBLE, pctile_window DOUBLE, dd_window DOUBLE, "
                "ttm_yield_pct DOUBLE, ma_value DOUBLE, below_ma BOOLEAN, "
                "cond_price_low BOOLEAN, cond_yield BOOLEAN, cond_below_ma BOOLEAN, "
                "in_watchlist BOOLEAN, push_eligible BOOLEAN, reasons VARCHAR)")
    con.execute(f"INSERT INTO {tm.TABLE} VALUES "
                "('b.2','测试股B','2026-10-23',10.0,0.33,50.0,4.6,NULL,NULL,"
                "TRUE,TRUE,NULL,TRUE,TRUE,'价格分位；股息率')")
    con.execute(f"INSERT INTO {tm.TABLE} VALUES "
                "('a.1','测试股A','2026-10-23',10.0,100.0,0.0,5.0,NULL,NULL,"
                "FALSE,TRUE,NULL,TRUE,FALSE,'股息率')")


def test_push_edge_semantics(tmp_path):
    con = duckdb.connect(":memory:")
    _lake_with_push(con)
    tp = make_tp()
    sp = str(tmp_path / "edge.json")
    calls = []
    send = lambda t, title, content: calls.append((title, content))  # noqa: E731

    # 1) 首跑：b.2 新触发 → 推 1 条 + 落状态
    res = tm.check_and_push(con, tp, state_path=sp, send_fn=send, token="tk")
    assert res["n_watchlist"] == 2 and res["n_eligible"] == 1 and res["n_new"] == 1
    assert res["pushed"] is True and len(calls) == 1
    assert "测试股B" in calls[0][0] and "新进入买入区" in calls[0][1]
    st = json.loads(Path(sp).read_text(encoding="utf-8"))["codes"]
    # state = push_eligible 集（a.1 仅观察名单、不提醒 → 不入 state）
    assert set(st) == {"b.2"}
    # 2) 重跑：持续在买区 → 不重推
    res = tm.check_and_push(con, tp, state_path=sp, send_fn=send, token="tk")
    assert res["n_new"] == 0 and res["pushed"] is False and len(calls) == 1
    # 3) b.2 掉出买区 → 状态清除（不推）
    con.execute(f"UPDATE {tm.TABLE} SET push_eligible=FALSE, in_watchlist=FALSE, "
                "cond_price_low=FALSE, reasons='' WHERE ts_code='b.2'")
    res = tm.check_and_push(con, tp, state_path=sp, send_fn=send, token="tk")
    assert res["n_new"] == 0 and res["pushed"] is False
    st = json.loads(Path(sp).read_text(encoding="utf-8"))["codes"]
    assert set(st) == set()  # 掉出后无 eligible → state 清空
    # 4) b.2 再进入 → 新触发可再推
    con.execute(f"UPDATE {tm.TABLE} SET push_eligible=TRUE, in_watchlist=TRUE, "
                "cond_price_low=TRUE WHERE ts_code='b.2'")
    res = tm.check_and_push(con, tp, state_path=sp, send_fn=send, token="tk")
    assert res["n_new"] == 1 and res["pushed"] is True and len(calls) == 2
    con.close()


def test_push_failure_keeps_state(tmp_path):
    con = duckdb.connect(":memory:")
    _lake_with_push(con)
    tp = make_tp()
    sp = str(tmp_path / "edge.json")

    def boom(t, title, content):
        raise RuntimeError("pushplus 推送失败: quota (code=999)")

    res = tm.check_and_push(con, tp, state_path=sp, send_fn=boom, token="tk")
    assert res["pushed"] is False and res["n_new"] == 1
    assert "推送失败" in res["error"]
    assert not Path(sp).exists()  # 不落状态 → 下轮重试
    calls = []
    res = tm.check_and_push(con, tp, state_path=sp,
                            send_fn=lambda t, ti, c: calls.append(ti), token="tk")
    assert res["pushed"] is True and res["n_new"] == 1 and len(calls) == 1
    con.close()


def test_push_no_token(tmp_path, monkeypatch):
    con = duckdb.connect(":memory:")
    _lake_with_push(con)
    monkeypatch.setattr(tm, "load_token", lambda: None)
    calls = []
    res = tm.check_and_push(con, make_tp(), state_path=str(tmp_path / "e.json"),
                            send_fn=lambda t, ti, c: calls.append(ti))
    assert res["pushed"] is False and res["n_new"] == 1
    assert "PUSHPLUS_TOKEN" in res["error"]
    assert calls == []  # 未发请求、未落状态
    con.close()


def test_push_dry_run(tmp_path):
    con = duckdb.connect(":memory:")
    _lake_with_push(con)
    calls = []
    res = tm.check_and_push(con, make_tp(), state_path=str(tmp_path / "e.json"),
                            send_fn=lambda t, ti, c: calls.append(ti), token="tk",
                            dry_run=True)
    assert res["n_new"] == 1 and res["pushed"] is False and res["dry_run"] is True
    assert calls == [] and not Path(tmp_path / "e.json").exists()
    con.close()


def test_render_message():
    rows = [{"ts_code": "sh.601006", "name": "大秦铁路", "as_of_date": "2026-10-23",
             "close": 4.52, "pctile_window": 21.9, "dd_window": 27.5,
             "ttm_yield_pct": 4.59, "reasons": "5年价格分位 21.9%（≤40%）；股息率 4.59%（≥4.5%）"}]
    title, content = tm.render_timing_message(rows, make_tp(price_window_years=5,
                                                            price_pctile_max=40.0,
                                                            yield_min_pct=4.5), 59)
    assert "大秦铁路" in title and "1 只" in title
    assert "sh.601006" in content and "新进入买入区" in content
    assert "5年分位 21.9%" in content and "候选池 59 只" in content


# ---------------------------------------------------------------------------
# 4) Web GET /api/timing（fake 湖）
# ---------------------------------------------------------------------------
@pytest.fixture()
def web_timing(monkeypatch, tmp_path):
    lake = tmp_path / "lake.duckdb"
    con = duckdb.connect(str(lake))
    con.execute(tm._SCHEMA)
    con.close()
    import martingale.data as mdata

    monkeypatch.setattr(mdata, "connect_readonly",
                        lambda p=None: duckdb.connect(str(lake), read_only=True))
    cfg = tmp_path / "s.yaml"
    cfg.write_text(_valid_yaml(), encoding="utf-8")
    monkeypatch.setattr(wapp, "CONFIG_PATH", cfg)
    return lake


def test_web_get_timing(web_timing):
    con = duckdb.connect(str(web_timing))
    con.execute(f"INSERT INTO {tm.TABLE} VALUES "
                "('sh.600000','浦发银行','2026-10-23',10.0,20.0,30.0,5.0,NULL,NULL,"
                "TRUE,TRUE,NULL,TRUE,TRUE,'①；②')")
    con.execute(f"INSERT INTO {tm.TABLE} VALUES "
                "('sh.600001','测试甲','2026-10-23',10.0,80.0,5.0,4.6,NULL,NULL,"
                "FALSE,TRUE,NULL,TRUE,FALSE,'②')")
    con.execute(f"INSERT INTO {tm.TABLE} VALUES "
                "('sh.600002','测试乙','2026-10-23',10.0,60.0,5.0,9.0,NULL,NULL,"
                "FALSE,TRUE,NULL,TRUE,FALSE,'②')")
    con.close()
    out = wapp.get_timing()
    assert out["as_of"] == "2026-10-23" and out["n_pool"] == 3
    assert out["n_watchlist"] == 3 and out["n_push"] == 1
    assert out["params"]["price_pctile_max"] == 40.0
    # push_eligible 优先 → 其次股息率降序
    assert [r["ts_code"] for r in out["rows"]] == [
        "sh.600000", "sh.600002", "sh.600001"]
    assert out["rows"][0]["push_eligible"] is True
    assert out["rows"][0]["reasons"] == "①；②"


def test_web_get_timing_empty_table(web_timing):
    out = wapp.get_timing()
    assert out["as_of"] is None and out["n_pool"] == 0
    assert out["n_watchlist"] == 0 and out["rows"] == []
