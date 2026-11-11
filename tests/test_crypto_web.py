"""web 端点（v7.0.48）：策略参数读/存/重置 + 全史回测。

- TestClient + WEB_TEST_NO_AUTH=1（跳外层 auth gate）
- 覆盖文件 monkeypatch 到 tmp（不碰真实 data/crypto/strategy_params.json）
- 回测端点：DB_PATH monkeypatch 到合成小湖（5 bar 确定性路径：
  100,100,30 → 建仓 @30；300 → 10x 了结；10 → 再建仓（部分窗口））
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from fastapi.testclient import TestClient

import crypto.models as models
from crypto.web import app as webapp


@pytest.fixture()
def client(monkeypatch, tmp_path):
    # auth gate 读模块全局（请求时求值）→ 直接置 True（等价 WEB_TEST_NO_AUTH=1）
    monkeypatch.setenv("WEB_TEST_NO_AUTH", "1")
    monkeypatch.setattr(webapp, "_AUTH_DISABLED", True)
    monkeypatch.setattr(webapp, "STRATEGY_OVERRIDE_PATH",
                        tmp_path / "strategy_params.json")
    return TestClient(webapp.core_app)


def _build_lake(path: Path, closes) -> Path:
    """合成 1 币小湖（生产 schema：coin_master + kline_1h）。ts = epoch 秒。"""
    con = models.open_db(str(path))
    try:
        models.upsert_coins(con, ["TESTUSDT"])
        bars = [{"ts": 1577836800 + i * 3600,  # 2020-01-01T00:00Z 起每小时
                 "open": c, "high": c, "low": c, "close": c,
                 "volume": 1.0, "quote_volume": c}
                for i, c in enumerate(closes)]
        n = models.upsert_klines(con, "binance", "TESTUSDT", bars)
        assert n == len(closes)
        con.close()
    except Exception:
        con.close()
        raise
    return path


@pytest.fixture()
def lake_client(client, tmp_path, monkeypatch):
    p = _build_lake(tmp_path / "test_crypto.duckdb", [100.0, 100.0, 30.0,
                                                     300.0, 10.0])
    monkeypatch.setattr(webapp, "DB_PATH", p)
    return client


class TestStrategyEndpoint:
    def test_get_defaults(self, client):
        r = client.get("/api/strategy")
        assert r.status_code == 200
        d = r.json()
        assert d["ready"] is True
        assert d["params"]["first_drop_pct"] == 70        # crypto.yaml 默认
        assert d["params"]["take_profit_mult"] == 10
        assert d["has_override"] is False
        assert set(d["params"]) == set(d["defaults"])
        assert d["rule"]["exit"]

    def test_save_partial_merge(self, client):
        r = client.post("/api/strategy",
                        json={"params": {"first_drop_pct": 50}})
        assert r.status_code == 200
        assert r.json()["params"]["first_drop_pct"] == 50
        assert r.json()["params"]["take_profit_mult"] == 10  # 其余保持
        g = client.get("/api/strategy").json()
        assert g["has_override"] is True
        assert g["params"]["first_drop_pct"] == 50

    def test_save_merges_previous_override(self, client):
        client.post("/api/strategy", json={"params": {"fee_bps": 0}})
        r = client.post("/api/strategy", json={"params": {"growth": 1.1}})
        assert r.json()["params"]["fee_bps"] == 0      # 早先覆盖仍生效
        assert r.json()["params"]["growth"] == 1.1

    def test_reset(self, client):
        client.post("/api/strategy", json={"params": {"first_drop_pct": 50}})
        r = client.post("/api/strategy/reset")
        assert r.status_code == 200
        assert r.json()["has_override"] is False
        assert r.json()["params"]["first_drop_pct"] == 70

    def test_validation(self, client):
        # 越界
        r = client.post("/api/strategy", json={"params": {"growth": 5}})
        assert r.status_code == 400
        # 未知字段
        r = client.post("/api/strategy", json={"params": {"leverage": 10}})
        assert r.status_code == 400
        # 空
        r = client.post("/api/strategy", json={"params": {}})
        assert r.status_code == 400


class TestBacktestEndpoint:
    def test_run_on_synthetic_lake(self, lake_client):
        r = lake_client.post("/api/backtest",
                             json={"coins": ["TESTUSDT"]})
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True
        assert d["params"]["first_drop_pct"] == 70
        res = d["results"][0]
        assert res["symbol"] == "TESTUSDT"
        assert res["n_bars"] == 5
        # 第 3 根 bar（收 30 = 100×0.30）建仓 @30 → 第 4 根 300 = 10x 了结
        assert len(res["rounds"]) == 1
        rd = res["rounds"][0]
        assert rd["entry"] == pytest.approx(30.0)
        assert rd["exit"] == pytest.approx(300.0)
        assert rd["pnl_pct"] > 800          # 10x − 双向手续费
        # 第 5 根 bar（收 10 ≤ 滚动高点 300×0.3）再建仓 → 持仓中
        assert res["open_round"] is not None
        assert res["open_round"]["entry"] == pytest.approx(10.0)
        # 账户模型（v7.0.53）：首轮 10 全外部；了结所得 ≈99.8 全额滚入余额，
        # 再建仓 10 从余额扣 → 外部投入仍 = 10，期末余额 ≈ 89.8
        assert res["external_invested"] == pytest.approx(10.0, abs=0.01)
        assert res["wallet"] == pytest.approx(89.8, abs=0.01)
        # 净值 = 持仓市值 + 余额 ≈ 9.99 + 89.8
        assert res["final_equity"] == pytest.approx(
            res["open_round"]["value"] + res["wallet"], abs=0.05)
        # 净值曲线已降采样且非空
        assert 2 <= len(res["equity_curve"]) <= 401
        assert "equity" not in res and "dates" not in res  # 原始数组不外发
        # 价格曲线：同采样步长，末点 = 最后一根 bar 收盘价 10
        assert 2 <= len(res["price_curve"]) <= 401
        assert res["price_curve"][-1][1] == pytest.approx(10.0)
        # 事件序列：建仓@30 → 平仓@300 → 再建仓@10
        # （合成湖 5 根 1h bar 同属 2020-01-01 → 日期相同，事件按 bar 序区分）
        ev = res["events"]
        assert [e["type"] for e in ev] == ["entry", "exit", "entry"]
        assert ev[0]["date"] == "2020-01-01" and ev[0]["price"] == 30.0
        assert ev[1]["date"] == "2020-01-01" and ev[1]["price"] == 300.0
        assert ev[2]["date"] == "2020-01-01" and ev[2]["price"] == 10.0

    def test_unknown_coin(self, lake_client):
        r = lake_client.post("/api/backtest", json={"coins": ["NOPEUSDT"]})
        assert r.status_code == 200
        assert r.json()["results"][0]["n_bars"] == 0
        assert r.json()["results"][0]["equity_curve"] == []
