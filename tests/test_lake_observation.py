# -*- coding: utf-8 -*-
"""Gate 1 观察期只读交叉验证（scripts/lake_observation.py）离线测试。

规格（重设计 §6/§8）：
- **零写入红线**：湖 read_only；唯一落盘 data/observation/；
- T2 分歧判据 close<0.1% / amount<0.5%；T7 close<0.3% / amount<0.5%；
- T3 每页 50 样本、checked<100 → 判不通过（字段名映射错也能被抓住）；
- 东财 9701/软失败 → 端点级 3 次退避重试 → 记 detail；
- _write_day：daily 覆盖 + latency_samples 追加合并；summary.jsonl 追加。

纪律：全离线——tmp duckdb + monkeypatch _get_retry/TencentClient/_em_fetch，
零真实网络、零触碰生产 data/lake/。
"""
import datetime as dt
import json
import os
import sys

import duckdb
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "scripts"))

import lake_observation as obs  # noqa: E402

TODAY = "2026-11-11"


# ---------------------------------------------------------------------------
# fixtures：tmp 湖 + tmp observation 目录
# ---------------------------------------------------------------------------
@pytest.fixture
def lake_env(tmp_path, monkeypatch):
    db = tmp_path / "lake.duckdb"
    con = duckdb.connect(str(db))
    con.execute("""
        CREATE TABLE kline_daily (ts_code VARCHAR, date DATE, close DOUBLE,
                                  amount DOUBLE, source VARCHAR);
        CREATE TABLE selection_backtest_10y (ts_code VARCHAR, as_of_date DATE);
        CREATE TABLE valuation_daily (ts_code VARCHAR, date DATE, pe_ttm DOUBLE,
                                      pb DOUBLE, total_mv DOUBLE);
        CREATE TABLE stock_master (ts_code VARCHAR);
        CREATE TABLE index_daily (index_code VARCHAR, date DATE, close DOUBLE,
                                  amount DOUBLE);
    """)
    # T2：池 2 只 + 持仓 1 只；湖当日行
    con.execute("INSERT INTO selection_backtest_10y VALUES ('sh.600546', '2026-11-11'),"
                " ('sz.000001', '2026-11-11')")
    con.execute("INSERT INTO kline_daily VALUES "
                "('sh.600546', '2026-11-11', 11.00, 100000000.0, 'tushare'),"
                "('sz.000001', '2026-11-11', 11.86, 200000000.0, 'tencent')")
    # T3 湖当日行
    con.execute("INSERT INTO valuation_daily VALUES "
                "('sh.600546', '2026-11-11', 8.5, 1.2, 110.0),"
                "('sz.000001', '2026-11-11', 5.0, 0.6, 2200.0)")
    con.execute("INSERT INTO stock_master VALUES ('sh.600546'), ('sz.000001')")
    # T7 湖行
    con.execute("INSERT INTO index_daily VALUES ('sh.000001', '2026-11-11', 3663.72, 673950000000.0)")
    con.close()
    monkeypatch.setattr(obs, "LAKE_DB", str(db))
    obs_dir = tmp_path / "observation"
    monkeypatch.setattr(obs, "OBS_DIR", str(obs_dir))
    pos_file = tmp_path / "user_positions.json"
    pos_file.write_text(json.dumps({"sh.600028": {}}), encoding="utf-8")
    monkeypatch.setattr(obs, "USER_POS", str(pos_file))
    return tmp_path


def _fake_kline(date: str, close: float, amount: float):
    return {"data": {"klines": [f"{date},{close},999,{amount}"]}}


# ---------------------------------------------------------------------------
# T2：分歧判据
# ---------------------------------------------------------------------------
def test_t2_pass_and_divergence(lake_env, monkeypatch):
    def fake_retry(url, headers=None, timeout=15.0):
        if "600546" in url:
            return _fake_kline(TODAY, 11.00, 100000000.0), ""     # 完全一致
        if "000001" in url:
            return _fake_kline(TODAY, 11.90, 200000000.0), ""     # close +0.34% > 0.1%
        if "000028" in url or "1.600028" in url:
            return _fake_kline(TODAY, 10.0, 50000000.0), ""       # 持仓股，湖无行 → missing
        return None, "404"
    monkeypatch.setattr(obs, "_get_retry", fake_retry)
    monkeypatch.setattr(obs.time, "sleep", lambda s: None)
    t2 = obs.obs_t2(TODAY)
    assert t2["universe"] == 3            # 池 2 + 持仓 1
    assert t2["checked"] == 2
    assert t2["missing_lake"] == 1
    assert t2["close_div"] == 1           # 000001 超 0.1% 线
    assert t2["amount_div"] == 0
    assert t2["pass"] is False


def test_t2_em_fail_counted(lake_env, monkeypatch):
    monkeypatch.setattr(obs, "_get_retry",
                        lambda url, headers=None, timeout=15.0: (None, "9701 服务器繁忙"))
    monkeypatch.setattr(obs.time, "sleep", lambda s: None)
    t2 = obs.obs_t2(TODAY)
    assert t2["checked"] == 0 and t2["em_fail"] == 3 and t2["pass"] is False
    assert "9701" in t2["detail"]


# ---------------------------------------------------------------------------
# T3：字段映射 + checked<100 判不通过
# ---------------------------------------------------------------------------
def test_t3_field_mapping_and_gate(lake_env, monkeypatch):
    page = [{"SECURITY_CODE": "600546", "TRADE_DATE": TODAY,
             "PE_TTM": 8.5, "PB_MRQ": 1.2, "TOTAL_MARKET_CAP": 11000000000},
            {"SECURITY_CODE": "000001", "TRADE_DATE": TODAY,
             "PE_TTM": 9.9, "PB_MRQ": 0.6, "TOTAL_MARKET_CAP": 22000000000}]
    calls = []

    def fake_retry(url, headers=None, timeout=15.0):
        calls.append(url)
        return {"success": True, "result": {"data": page}}, ""
    monkeypatch.setattr(obs, "_get_retry", fake_retry)
    t3 = obs.obs_t3(TODAY)
    assert t3["checked"] == 2
    assert t3["rows"] == 2
    assert t3["fields_seen"] and "PE_TTM" in t3["fields_seen"]
    # 600546 三字段全中；000001 PE 8.5 vs 9.9 = +16.5% > 1% → pe_div=1
    assert t3["pe_div"] == 1
    # checked=2 < 100 → 判不通过（样本不足门，字段映射错同样被此门兜住）
    assert t3["pass"] is False
    # 单趟（不再双取页）：第 1 页 len<500 → 停
    assert len(calls) == 1


def test_t3_soft_fail_detail(lake_env, monkeypatch):
    def fake_retry(url, headers=None, timeout=15.0):
        return {"success": False, "code": 9701, "message": "服务器繁忙"}, ""
    monkeypatch.setattr(obs, "_get_retry", fake_retry)
    t3 = obs.obs_t3(TODAY)
    assert "9701" in t3["detail"] and t3["pass"] is False


# ---------------------------------------------------------------------------
# T7 / T4 / Fuyao
# ---------------------------------------------------------------------------
def test_t7_match(lake_env, monkeypatch):
    def fake_retry(url, headers=None, timeout=15.0):
        if "1.000001" in url:
            return _fake_kline(TODAY, 3663.72, 673950000000.0), ""
        return {"data": {"klines": [f"{TODAY},4100.0,999,880000000000.0"]}}, ""
    monkeypatch.setattr(obs, "_get_retry", fake_retry)
    t7 = obs.obs_t7(TODAY)
    # 000001 全中；000300 湖无行 → per_index 记「湖无当日行」
    assert t7["checked"] == 1 and t7["close_div"] == 0
    assert t7["pass"] is True
    assert "湖无当日行" in t7["per_index"]["sh.000300"]["note"]


def test_t4_endpoint_ok(lake_env, monkeypatch):
    monkeypatch.setattr(obs, "_get_retry",
                        lambda url, headers=None, timeout=15.0: (
                            {"success": True,
                             "result": {"data": [{"EX_DIVIDEND_DATE": TODAY,
                                                  "SECURITY_CODE": "600546"}]}}, ""))
    t4 = obs.obs_t4()
    assert t4["ok"] is True
    assert "EX_DIVIDEND_DATE" in t4["pit_fields_seen"]


def test_fuyao_pit(monkeypatch):
    monkeypatch.setattr(obs, "_load_env_key", lambda k: "FAKE_KEY")
    period_end = 1790000000000
    monkeypatch.setattr(obs, "_get_retry",
                        lambda url, headers=None, timeout=15.0: (
                            {"code": 0, "data": {"items": [
                                {"report_date_ms": period_end + 40 * 86400000,
                                 "period_end_ms": period_end}]}}, ""))
    fy = obs.obs_fuyao()
    assert fy["ok"] is True and fy["disclosure_lag_days"] == 40.0
    # 缺披露日 → PIT 不合格（原则 6：无 PIT 不得任主源/备源）
    monkeypatch.setattr(obs, "_get_retry",
                        lambda url, headers=None, timeout=15.0: (
                            {"code": 0, "data": {"items": [
                                {"report_date_ms": None,
                                 "period_end_ms": period_end}]}}, ""))
    assert obs.obs_fuyao()["ok"] is False
    monkeypatch.setattr(obs, "_load_env_key", lambda k: None)
    assert obs.obs_fuyao()["ok"] is False   # 缺 key


# ---------------------------------------------------------------------------
# _write_day 合并 + summary
# ---------------------------------------------------------------------------
def test_write_day_merge(lake_env):
    day = obs._write_day({"date": TODAY, "run": "daily", "ts_utc": "t0",
                          "t2": {"pass": True, "close_div_pct": 0.0,
                                 "amount_div_pct": 0.0},
                          "t3": {"pass": True}, "t7": {"pass": True},
                          "t1": {"total": 5400}, "t4": {"ok": True},
                          "fuyao": {"ok": True}})
    obs._write_day({"date": TODAY, "run": "latency", "ts_utc": "t1",
                    "latency": {"max_s": 120.0, "median_s": 118.0, "n": 10}})
    d = json.load(open(day, encoding="utf-8"))
    assert d["run"] == "daily"           # daily 主体保留
    assert len(d["latency_samples"]) == 1   # latency 追加
    assert d["latency_samples"][0]["max_s"] == 120.0
    lines = open(os.path.join(str(lake_env), "observation", "summary.jsonl"),
                 encoding="utf-8").read().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["t2_pass"] is True     # daily 行
    assert json.loads(lines[1])["latency_max_s"] == 120.0   # latency 行


# ---------------------------------------------------------------------------
# secid 映射（脚本内独立实现）
# ---------------------------------------------------------------------------
def test_obs_secid():
    assert obs._bs_to_secid("sh.600546") == "1.600546"
    assert obs._bs_to_secid("sz.000001") == "0.000001"
    assert obs._bs_to_secid("sz.300750") == "0.300750"
    assert obs._bs_to_secid("bj.430047") is None
