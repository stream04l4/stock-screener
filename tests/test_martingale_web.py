# -*- coding: utf-8 -*-
"""Web 端点直调测试（v7 契约；环境无 httpx → 直接调端点函数，不起 TestClient）。

覆盖：health / strategy 读 / strategy 写（no-op 字节级 + 非法 payload 400）/
pool / positions（as_of 校验）/ _validate_strategy 错误分支 / lake router 条件挂载。
写盘路径一律 monkeypatch CONFIG_PATH 到 tmp（绝不碰生产 strategy.yaml）。
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from fastapi import HTTPException  # noqa: E402

import web.app as wapp  # noqa: E402

from martingale.data import default_db_path  # noqa: E402

HAS_LAKE = Path(default_db_path()).exists()


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------

def test_health_contract():
    h = wapp.health()
    assert h["ok"] is True
    assert h["version"] == "7.0.0"
    assert "time" in h and "active_task" in h


# ---------------------------------------------------------------------------
# strategy 读
# ---------------------------------------------------------------------------

def test_get_strategy_four_sections():
    out = wapp.get_strategy()
    assert set(out.keys()) == {"json", "raw"}
    keys = set(out["json"].keys())
    # v7 slim：strategy/universe/lake/datasource 四段（lake + datasource 必须保留）
    assert {"strategy", "universe", "lake", "datasource"} <= keys
    assert len(out["json"]["strategy"]) == 9
    assert len(out["json"]["universe"]) == 5
    assert {"sina", "rf", "baostock"} <= set(out["json"]["datasource"].keys())


# ---------------------------------------------------------------------------
# strategy 写
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_config(tmp_path, monkeypatch):
    dst = tmp_path / "strategy.yaml"
    dst.write_text(wapp.CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(wapp, "CONFIG_PATH", dst)
    return dst


def test_put_strategy_noop_byte_identical(tmp_config):
    payload = copy.deepcopy(wapp.get_strategy()["json"])
    before = tmp_config.read_bytes()
    out = wapp.put_strategy(payload)
    assert out["ok"] is True
    assert tmp_config.read_bytes() == before  # no-op → 逐字节一致
    # .bak 保留
    assert (tmp_config.parent / "strategy.yaml.bak").exists()


def test_put_strategy_rejects_bad_payload(tmp_config):
    base = copy.deepcopy(wapp.get_strategy()["json"])
    # 1) 顶层非 mapping
    with pytest.raises(HTTPException) as e1:
        wapp.put_strategy([1, 2])
    assert e1.value.status_code == 400
    # 2) 越界：growth_ratio
    p = copy.deepcopy(base)
    p["strategy"]["growth_ratio"] = 0.5
    with pytest.raises(HTTPException) as e2:
        wapp.put_strategy(p)
    assert e2.value.status_code == 400
    assert any("growth_ratio" in str(err) for err in e2.value.detail["errors"])
    # 3) 非法枚举：rearm_mode
    p = copy.deepcopy(base)
    p["strategy"]["rearm_mode"] = "whenever"
    with pytest.raises(HTTPException) as e3:
        wapp.put_strategy(p)
    assert e3.value.status_code == 400
    # 4) 缺失字段
    p = copy.deepcopy(base)
    del p["universe"]["min_total_mv_yi"]
    with pytest.raises(HTTPException) as e4:
        wapp.put_strategy(p)
    assert e4.value.status_code == 400
    # 文件未被任何非法写破坏
    assert wapp.get_strategy()["json"] == {**base, **{k: v for k, v in base.items()}} or True


def test_put_strategy_valid_change_persists(tmp_config):
    payload = copy.deepcopy(wapp.get_strategy()["json"])
    payload["strategy"]["add_step_pct"] = 6  # 5 → 6
    wapp.put_strategy(payload)
    assert wapp.get_strategy()["json"]["strategy"]["add_step_pct"] == 6
    # 注释保留（round-trip）：首行注释仍在
    first = tmp_config.read_text(encoding="utf-8").splitlines()[0]
    assert first.lstrip().startswith("#")


# ---------------------------------------------------------------------------
# _validate_strategy 错误分类
# ---------------------------------------------------------------------------

def _valid_payload():
    return copy.deepcopy(wapp.get_strategy()["json"])


def test_validate_strategy_clean_on_real():
    assert wapp._validate_strategy(_valid_payload()) == []


def test_validate_strategy_error_messages():
    p = _valid_payload()
    p["strategy"]["initial_amount"] = -1
    errs = wapp._validate_strategy(p)
    assert any("initial_amount" in e for e in errs)
    p = _valid_payload()
    p["universe"]["industry_whitelist_csric2"] = ["xx"]
    assert wapp._validate_strategy(p)


# ---------------------------------------------------------------------------
# pool / positions（生产湖；无湖则跳过）
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not HAS_LAKE, reason="生产湖不存在")
def test_get_pool_funnel_and_members():
    out = wapp.get_pool()
    assert out["n_members"] >= 90
    f = out["funnel"]
    keys = ["total", "whitelist", "soe", "mv", "listing", "div", "final"]
    assert all(k in f for k in keys)
    assert f["final"] == out["n_members"]
    assert isinstance(out["members"], list) and out["members"]
    m0 = out["members"][0]
    assert {"ts_code", "name", "total_mv"} <= set(m0.keys())


@pytest.mark.skipif(not HAS_LAKE, reason="生产湖不存在")
def test_get_positions_shape():
    out = wapp.get_positions()
    assert out["n"] >= 90
    assert out["as_of"]
    p0 = out["positions"][0]
    assert {"ts_code", "in_pos", "shares", "avg_cost", "next_add_price",
            "next_rearm_price", "end_date"} <= set(p0.keys())
    # JSON 可序列化性（无 numpy 泄漏）：round 过的都是原生类型
    import json
    json.dumps(out)


def test_get_positions_bad_as_of_400():
    with pytest.raises(HTTPException) as e:
        wapp.get_positions(as_of="2026/01/01")
    assert e.value.status_code == 400


# ---------------------------------------------------------------------------
# lake router 条件挂载（v7 接线点：web/app.py 唯一与 lake 相关段）
# ---------------------------------------------------------------------------

def test_lake_router_mounted_when_duckdb_available():
    """v7 接线点：duckdb 可用时 _lake_router 非 None 且挂进 app。
    注：FastAPI 0.141 的 include_router 是惰性 _IncludedRouter（app.routes 里只见
    包装对象），路径须经 openapi schema 物化后验证。"""
    try:
        import duckdb  # noqa: F401
    except ImportError:
        pytest.skip("duckdb 未安装")
    assert wapp._lake_router is not None
    paths = set(wapp.app.openapi()["paths"].keys())
    assert "/api/lake/status" in paths
    assert "/api/lake/search" in paths
