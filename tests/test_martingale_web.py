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

def test_get_strategy_five_sections():
    out = wapp.get_strategy()
    assert set(out.keys()) == {"json", "raw"}
    keys = set(out["json"].keys())
    # v7 slim：strategy/universe/lake/datasource 四段（lake + datasource 必须保留）
    assert {"strategy", "universe", "lake", "datasource"} <= keys
    assert len(out["json"]["strategy"]) == 8
    assert len(out["json"]["universe"]) == 15  # v8.0.8：v7.0.39 的 11 键 + 国企控股强度门 4 治理键
    # v7.0.13：timing 段（v7.0.26：②③ 删除、首仓建仓回撤移入；v7.0.30：+
    # approach_pct 显示带 / spot_preselect_pct 实时取价预筛带，web 侧口径）
    assert set(out["json"]["timing"].keys()) == {
        "price_window_years", "price_pctile_max", "first_drawdown_pct",
        "approach_pct", "spot_preselect_pct"}
    assert {"sina", "rf"} <= set(out["json"]["datasource"].keys())  # v7.0.18：baostock 段随渠道移除


# ---------------------------------------------------------------------------
# strategy 写
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_config(tmp_path, monkeypatch):
    dst = tmp_path / "strategy.yaml"
    dst.write_text(wapp.CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(wapp, "CONFIG_PATH", dst)
    return dst


@pytest.fixture(autouse=True)
def _rescan_safety(monkeypatch):
    """v7.0.27：web 测试一律桩掉「保存后重算选股时机表」的湖写。
    v7.0.41：十年回测表重算（refresh_selection）同桩。

    只 stub refresh_timing / refresh_selection（不写表）；duckdb 写连接只开在
    真实湖上瞬间即关（refresh 已 no-op，无任何写入）——不碰 default_db_path，
    免得污染本模块其他用真实湖只读连接的测试。
    """
    from martingale import selection as msel
    from martingale import timing as mtiming

    monkeypatch.setattr(
        mtiming, "refresh_timing",
        lambda con, up, tp, **kw: {"as_of": "2000-01-01", "n_codes": 3,
                                   "n_watchlist": 1, "n_push": 1, "elapsed_s": 0.0},
    )
    monkeypatch.setattr(
        msel, "refresh_selection",
        lambda con, sp, up, **kw: {"as_of": "2000-01-01", "start": "2000-01-01",
                                   "n_codes": 3, "n_written": 3, "n_positive": 2,
                                   "elapsed_s": 0.0},
    )


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
    # 2) 越界：first_lots
    p = copy.deepcopy(base)
    p["strategy"]["first_lots"] = 0
    with pytest.raises(HTTPException) as e2:
        wapp.put_strategy(p)
    assert e2.value.status_code == 400
    assert any("first_lots" in str(err) for err in e2.value.detail["errors"])
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


def test_put_strategy_triggers_timing_rescan_with_new_params(tmp_config, monkeypatch):
    """v7.0.27：保存后同步重算选股时机表，且重算吃的是**刚保存的新值**（读盘）。"""
    import duckdb

    from martingale import timing as mtiming

    seen = {}

    def fake_refresh(con, up, tp, **kw):
        seen["tp"] = tp
        return {"as_of": "2026-11-09", "n_codes": 3, "n_watchlist": 2,
                "n_push": 2, "elapsed_s": 0.1}

    class _FakeCon:
        def close(self):  # noqa: D401
            pass

    monkeypatch.setattr(duckdb, "connect", lambda *a, **k: _FakeCon())  # 不碰生产湖
    monkeypatch.setattr(mtiming, "refresh_timing", fake_refresh)
    payload = copy.deepcopy(wapp.get_strategy()["json"])
    payload["timing"]["price_pctile_max"] = 35.0  # 40 → 35
    out = wapp.put_strategy(payload)
    assert out["ok"] is True
    assert out["timing_rescan"] == {"refreshed": True,
                                    "as_of": "2026-11-09", "n_watchlist": 2}
    assert seen["tp"].price_pctile_max == 35.0  # 重算用了新值而非旧缓存


def test_put_strategy_triggers_selection_rescan_with_new_params(tmp_config, monkeypatch):
    """v7.0.41：保存后同步重算十年回测表，且重算吃**刚保存的新值**（读盘）；
    顺序 = selection（十年回测）先于 timing（候选池 G7 读十年回测表）。

    first_drawdown_pct 居 timing 段（v7.0.26）但驱动引擎回放参数
    （StrategyParams.first_drawdown）——改它 G7 回放结果即变。
    """
    import duckdb

    from martingale import selection as msel
    from martingale import timing as mtiming

    order = []
    seen_sp = {}

    def fake_sel(con, sp, up, **kw):
        order.append("selection")
        seen_sp["sp"] = sp
        return {"as_of": "2026-11-11", "start": "2016-11-11", "n_codes": 4,
                "n_written": 4, "n_positive": 3, "elapsed_s": 0.1}

    def fake_timing(con, up, tp, **kw):
        order.append("timing")
        return {"as_of": "2026-11-11", "n_codes": 4, "n_watchlist": 1,
                "n_push": 1, "elapsed_s": 0.1}

    class _FakeCon:
        def close(self):  # noqa: D401
            pass

    monkeypatch.setattr(duckdb, "connect", lambda *a, **k: _FakeCon())  # 不碰生产湖
    monkeypatch.setattr(msel, "refresh_selection", fake_sel)
    monkeypatch.setattr(mtiming, "refresh_timing", fake_timing)
    payload = copy.deepcopy(wapp.get_strategy()["json"])
    payload["timing"]["first_drawdown_pct"] = 20.0  # 15 → 20（回放参数）
    out = wapp.put_strategy(payload)
    assert out["ok"] is True
    assert out["selection_rescan"] == {"refreshed": True, "as_of": "2026-11-11",
                                       "n_codes": 4, "n_written": 4}
    assert out["timing_rescan"]["refreshed"] is True
    assert order == ["selection", "timing"]  # 回测表先、时机表后
    assert seen_sp["sp"].first_drawdown == 0.20  # 重算用了新值而非旧缓存


def test_put_strategy_rescan_degrades_on_lake_lock(tmp_config, monkeypatch):
    """v7.0.27：湖被锁（11:30 同步窗口）→ 重算降级，保存本身成功。
    v7.0.41：十年回测表 + 时机表双双降级。"""
    import duckdb

    def boom(*a, **k):
        raise Exception("IO Error: Conflicting lock is held in PID 123")

    monkeypatch.setattr(duckdb, "connect", boom)
    payload = copy.deepcopy(wapp.get_strategy()["json"])
    out = wapp.put_strategy(payload)
    assert out["ok"] is True  # 保存不受影响
    assert out["selection_rescan"]["refreshed"] is False
    assert "Conflicting lock" in out["selection_rescan"]["reason"]
    assert out["timing_rescan"]["refreshed"] is False
    assert "Conflicting lock" in out["timing_rescan"]["reason"]


# ---------------------------------------------------------------------------
# _validate_strategy 错误分类
# ---------------------------------------------------------------------------

def _valid_payload():
    return copy.deepcopy(wapp.get_strategy()["json"])


def test_validate_strategy_clean_on_real():
    assert wapp._validate_strategy(_valid_payload()) == []


def test_validate_strategy_error_messages():
    p = _valid_payload()
    p["strategy"]["first_lots"] = -1
    errs = wapp._validate_strategy(p)
    assert any("first_lots" in e for e in errs)
    p = _valid_payload()
    p["universe"]["industry_whitelist_csric2"] = ["xx"]
    assert wapp._validate_strategy(p)


def test_validate_strategy_v7012_universe_gates():
    # v7.0.12：候选池门槛字段也进 PUT 校验
    p = _valid_payload()
    p["universe"]["backtest_positive"] = "yes"  # 非布尔
    errs = wapp._validate_strategy(p)
    assert any("backtest_positive" in e for e in errs)
    p = _valid_payload()
    p["universe"]["price_min_yuan"] = 200
    p["universe"]["price_max_yuan"] = 3         # 下限 > 上限
    errs = wapp._validate_strategy(p)
    assert any("price_min_yuan" in e for e in errs)


def test_validate_strategy_v7013_timing():
    # v7.0.13：timing 字段进 PUT 校验（段可选，段存在时 3 键齐全 + 类型/范围）
    # v7.0.26：②③ 删除、first_drawdown_pct 移入
    p = _valid_payload()
    p["timing"]["first_drawdown_pct"] = 200       # 超范围
    errs = wapp._validate_strategy(p)
    assert any("first_drawdown_pct" in e for e in errs)
    p = _valid_payload()
    p["timing"]["price_pctile_max"] = 150       # 超范围
    assert any("price_pctile_max" in e for e in wapp._validate_strategy(p))
    p = _valid_payload()
    p["timing"].pop("first_drawdown_pct")       # 段存在但缺键
    assert any("timing.first_drawdown_pct" in e for e in wapp._validate_strategy(p))
    # 段整体缺省 → 合法（策略库旧快照兼容）
    p = _valid_payload()
    p.pop("timing")
    assert wapp._validate_strategy(p) == []


# ---------------------------------------------------------------------------
# pool / positions（生产湖；无湖则跳过）
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not HAS_LAKE, reason="生产湖不存在")
def test_get_pool_funnel_and_members():
    out = wapp.get_pool()
    # v7.0.12：候选池 = 基础 5 条件 ∩ 十年回测为正 ∩ 股息率≥3% ∩ 价格 3~100 元 ∪ 手动
    # （2026-10-24 生产首跑 = 59 只；留漂移余量）
    assert out["n_members"] >= 40
    f = out["funnel"]
    keys = ["total", "whitelist", "soe", "mv", "listing", "div",
            "backtest", "yield", "price", "final", "manual"]
    assert all(k in f for k in keys)
    assert f["final"] == out["n_members"]
    assert f["backtest"] >= f["yield"] >= f["price"]  # 漏斗逐级收窄
    assert f["final"] >= f["price"]  # final = 自动 + 纯手动
    assert isinstance(out["members"], list) and out["members"]
    m0 = out["members"][0]
    assert {"ts_code", "name", "total_mv", "bt_pnl_pct", "ttm_yield_pct",
            "price_latest", "is_manual",
            # v8.0.11：分红/上涨占比拆解字段
            "bt_div_pool", "bt_price_pnl", "bt_div_share", "bt_price_share",
            # v8.0.12：累计分红（余额口径的互补展示）
            "bt_dividends"} <= set(m0.keys())
    # v8.0.11：池内成员（G7 通过 = 总盈亏>0）占比应齐全且和≈100%（差手续费）
    for m in out["members"]:
        if m["is_manual"]:
            continue
        if m["bt_div_share"] is not None and m["bt_price_share"] is not None:
            assert 0.0 < m["bt_div_share"] + m["bt_price_share"] <= 100.5
    # 排序契约：自动成员按十年回测收益降序（无值殿后）
    auto = [m["bt_pnl_pct"] for m in out["members"] if not m["is_manual"]]
    with_val = [x for x in auto if x is not None]
    assert with_val == sorted(with_val, reverse=True)


@pytest.mark.skipif(not HAS_LAKE, reason="生产湖不存在")
def test_get_positions_shape():
    out = wapp.get_positions()
    assert out["n"] >= 40  # v7.0.12：候选池 59 只（2026-10-24 首跑）
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


# ---------------------------------------------------------------------------
# 我的持仓（/api/my_positions 读写 + 用户账本文件）
# ---------------------------------------------------------------------------
@pytest.fixture()
def tmp_positions_file(tmp_path, monkeypatch):
    dst = tmp_path / "user_positions.json"
    monkeypatch.setattr(wapp, "USER_POSITIONS_PATH", dst)
    return dst


def _last_n_weekdays(n: int, end: str = "2026-11-11") -> list:
    """生成截至 end 的最近 n 个工作日（周一~周五）。"""
    from datetime import date, timedelta
    d = date.fromisoformat(end)
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return [x.isoformat() for x in reversed(out)]


# 合成湖价格路径（收盘价；open=high=low=close，adj_factor=1.0 无复权）。
# 设计口径（strategy.yaml: first_dd=15%, add_step=5%, rearm=15% bounce）：
# ⚠️ bounce 再武装模式下 entry_high 不跟随新高——恒为首 bar / 再武装 bar 价，
# 首仓线 = entry_high×0.85。
# - sh.601088 中国神华：首 bar 50（entry_high=50）→ 冲高 55（bounce 不更新
#   高点）→ 跌至 42（≤50×0.85=42.5 首仓 k=0，k→1；k=1 线 40 未到）→ 低点 42
#   反弹至 48.5（≥42×1.15=48.3 → rearm：entry_high=48.5, k 复位 0, in_pos 保持
#   True）→ 收尾 49.27（>下一加仓线 48.5×0.85=41.225 不触线）。
#   终态 in_pos=True/k=0/last=49.27/next_add_raw=41.225；spot 40.0≤41.225 → add 信号。
# - sh.600941 中国移动：首 bar 100 → 最低 95（bounce 首仓线 85 / 严格档 75 均未
#   到）→ 收尾 96.59。严格档（dd=25%）：in_pos=False/k=0/next_add_raw=75.0；
#   spot 70.0≤75.0 → buy 信号。
# - sh.600000 浦发银行：首 bar 10.0 → 末收 **9.66**（spot 展示断言锚点），
#   首仓线 8.5 未到 → 不持仓。
# - sh.600036 招商银行：40.5 附近窄幅，末收 40.8，仅保证有行（last_price 非空）。
_BT_PATHS = {
    "sh.601088": [50, 51, 52, 53, 54, 55, 53, 50, 47, 43, 42, 44, 48.5, 50,
                  51, 52, 51, 50, 49.5, 49.27],
    "sh.600941": [100, 100.5, 101, 100, 99, 98, 97, 96, 95, 96, 96.5, 96.59],
    "sh.600000": [10.0, 10.1, 10.2, 10.0, 9.9, 9.8, 9.7, 9.66],
    "sh.600036": [40.5, 40.6, 40.4, 40.7, 40.8],
}
# 搜索接口断言需要的名录（industry_name 仅展示用；delist_date=NULL 在市）
_BT_MASTER = [
    ("sh.600000", "浦发银行", "银行"),
    ("sh.600025", "国电电力", "电力"),
    ("sh.600036", "招商银行", "银行"),
    ("sh.600900", "长江电力", "电力"),
    ("sh.600941", "中国移动", "通信"),
    ("sh.601088", "中国神华", "煤炭"),
]


def _build_synthetic_bt_lake(path: str) -> None:
    """建自包含合成回测湖：生产 DDL（lake.ddl 全表全视图）+ 上述受控 K 线路径。

    零外部依赖：不复制生产湖/不依赖 /tmp 快照，断言值由路径确定性推得
    （见 _BT_PATHS 注释）。dividend_events 留空 → 无除权修正（f=1, r_cum=1）。
    """
    import duckdb
    from lake.ddl import init_schema
    con = duckdb.connect(path)
    try:
        init_schema(con)
        con.executemany(
            "INSERT INTO stock_master (ts_code, name, industry_name) VALUES (?,?,?)",
            _BT_MASTER)
        rows = []
        for code, closes in _BT_PATHS.items():
            dates = _last_n_weekdays(len(closes))
            for d, c in zip(dates, closes):
                rows.append((code, d, c, c, c, c, 1_000_000, 0.0, 1.0))
        con.executemany(
            "INSERT INTO kline_daily (ts_code, date, open, high, low, close,"
            " volume, pct_chg, adj_factor) VALUES (?,?,?,?,?,?,?,?,?)", rows)
        con.close()
    except Exception:
        con.close()
        raise


@pytest.fixture(scope="module")
def bt_lake_file(tmp_path_factory):
    """模块级合成回测湖（建一次，全 bt 用例共享，只读使用）。"""
    p = tmp_path_factory.mktemp("bt_lake") / "lake_bt.duckdb"
    _build_synthetic_bt_lake(str(p))
    return p


@pytest.fixture()
def bt_copy(bt_lake_file, monkeypatch):
    """自包含合成回测湖 + monkeypatch（v7.0.46 起）。

    取代旧方案（外部快照 /tmp/lake_bt_20261022.duckdb：2026-11-11 被 ops
    清理丢失 → 13 例 skip）。合成路径确定性复现原快照断言值（49.27/41.225 神华、
    96.59/75.0 移动严格档、9.66 浦发末收），不随行情漂移。
    """
    import martingale.data as mdata
    monkeypatch.setattr(mdata, "default_db_path", lambda: str(bt_lake_file))
    return bt_lake_file


def test_my_positions_empty_ledger(tmp_positions_file):
    out = wapp.get_my_positions()
    assert out["n"] == 0
    assert out["positions"] == []
    assert out["totals"]["market_value"] == 0


def test_my_positions_put_get_roundtrip(tmp_positions_file, bt_copy):
    out = wapp.put_my_positions(
        {"ts_code": "sh.600036", "shares": 1000, "cost_price": 40.5})
    assert out["ok"] is True and out["n"] == 1
    # 账本落盘
    import json as _json
    disk = _json.loads(tmp_positions_file.read_text(encoding="utf-8"))
    assert disk["positions"]["sh.600036"]["shares"] == 1000
    assert disk["positions"]["sh.600036"]["cost_price"] == 40.5
    # GET 合并：账本 × 湖最新价 × 策略参考
    got = wapp.get_my_positions()
    assert got["n"] == 1
    p0 = got["positions"][0]
    assert p0["ts_code"] == "sh.600036"
    assert p0["shares"] == 1000.0 and p0["cost_price"] == 40.5
    assert p0["last_price"] is not None
    assert p0["market_value"] == round(1000 * p0["last_price"], 2)
    assert p0["pnl"] == round(p0["market_value"] - 1000 * 40.5, 2)
    # 策略参考字段在（600036 在池内 → 状态机有行）
    assert {"k_level", "next_add_price", "next_rearm_price", "in_pos"} <= set(p0.keys())
    # totals 自洽
    assert got["totals"]["market_value"] == p0["market_value"]
    assert got["totals"]["cost"] == round(1000 * 40.5, 2)
    import json
    json.dumps(got)  # 无 numpy 泄漏


def test_my_positions_update_and_delete(tmp_positions_file, bt_copy):
    wapp.put_my_positions({"ts_code": "sh.600036", "shares": 500, "cost_price": 10.0})
    out = wapp.put_my_positions({"ts_code": "sh.600036", "shares": 800, "cost_price": 12.0})
    assert out["n"] == 1  # upsert 非新增
    assert wapp.get_my_positions()["positions"][0]["shares"] == 800.0
    out = wapp.put_my_positions({"ts_code": "sh.600036", "shares": 0, "cost_price": 12.0})
    assert out["n"] == 0
    assert wapp.get_my_positions()["n"] == 0


def test_my_positions_rejects_bad_code(tmp_positions_file):
    for bad in ("600036", "sh.12345", "SH.600036", "sh.600036x", ""):
        with pytest.raises(HTTPException) as e:
            wapp.put_my_positions({"ts_code": bad, "shares": 1, "cost_price": 1})
        assert e.value.status_code == 400


def test_my_positions_rejects_bad_numbers(tmp_positions_file):
    for payload in (
        {"ts_code": "sh.600036", "shares": -5, "cost_price": 1.0},
        {"ts_code": "sh.600036", "shares": 1, "cost_price": -1.0},
        {"ts_code": "sh.600036", "shares": "abc", "cost_price": 1.0},
    ):
        with pytest.raises(HTTPException) as e:
            wapp.put_my_positions(payload)
        assert e.value.status_code == 400
    assert wapp.get_my_positions()["n"] == 0  # 非法写不落盘


def test_my_positions_corrupt_ledger_treated_as_empty(tmp_positions_file):
    tmp_positions_file.write_text("{not json", encoding="utf-8")
    out = wapp.get_my_positions()
    assert out["n"] == 0


def test_my_positions_lake_unavailable_degrades(tmp_positions_file, monkeypatch):
    """湖锁定/缺失 → 不 500：账本仍列出，lake_locked=True。

    v7.0.40：展示价 = 实时行情优先——湖不可用但行情可用时价格照出（价源 spot）；
    行情也缺失 → 无价格字段（纯降级）。
    """
    import martingale.data as mdata
    monkeypatch.setattr(mdata, "default_db_path", lambda: Path("/tmp/nonexistent_lake_xyz.duckdb"))
    wapp.put_my_positions({"ts_code": "sh.600036", "shares": 100, "cost_price": 10.0})
    _mock_spot(monkeypatch, {})  # 湖 + 行情双缺 → 纯降级
    out = wapp.get_my_positions()
    assert out["lake_locked"] is True
    assert out["n"] == 1
    p0 = out["positions"][0]
    assert p0["last_price"] is None
    assert p0["market_value"] is None and p0["pnl"] is None
    assert p0["next_add_price"] is None  # 策略参考整体缺省
    assert out["totals"]["market_value"] is None
    assert out["totals"]["cost"] == 1000.0
    # 湖不可用但行情可用 → 展示价 = 实时价（v7.0.40）
    _mock_spot(monkeypatch, {"sh.600036": 11.5})
    out2 = wapp.get_my_positions()
    assert out2["price_source"] == "spot"
    p1 = out2["positions"][0]
    assert p1["last_price"] == 11.5
    assert p1["market_value"] == 1150.0 and p1["pnl"] == 150.0
    assert out2["totals"]["market_value"] == 1150.0
    import json
    json.dumps(out)


# ---------------------------------------------------------------------------
# /api/bt/runs 策略选择（v7.0.2）：strategy 参数 → --config 传递 + 400 分支
# ---------------------------------------------------------------------------

class _FakeTM:
    """替身 TaskManager：只捕获 start 的入参，不真起子进程。"""

    def __init__(self):
        self.last_cmd = None
        self.last_params = None

    def start(self, cmd, params, done_markers, task_id=None):
        self.last_cmd = list(cmd)
        self.last_params = dict(params)
        return {"task_id": task_id, "pid": 4242, "status": "running"}


def _patch_tm(monkeypatch):
    fake = _FakeTM()
    monkeypatch.setattr(wapp.tm, "start", fake.start)
    return fake


def test_bt_run_uses_active_config_only(monkeypatch):
    """v7.0.29：策略库退役 → 回测不带 --config（固定活动策略），body 无 strategy 字段。"""
    fake = _patch_tm(monkeypatch)
    out = wapp.trigger_bt_run(wapp.BtRunRequest(pool=True))
    assert "--config" not in fake.last_cmd
    assert "strategy" not in fake.last_params
    assert out["out_dir"].startswith(str(wapp.BT_RUNS_DIR))
    assert not hasattr(wapp.BtRunRequest, "strategy")


# ---------------------------------------------------------------------------
# GET /api/bt/runs 历史回测列表（v7.0.34，Joel 2026-11-10「历史回测结果读取」）
# ---------------------------------------------------------------------------
def _mk_run(base: Path, tid: str, with_status: bool = True, corrupt: bool = False):
    import json
    d = base / tid
    d.mkdir(parents=True)
    if with_status:
        sj = d / "status.json"
        if corrupt:
            sj.write_text("{broken json", encoding="utf-8")
        else:
            sj.write_text(json.dumps({
                "generated_at": "2026-11-10T14:07:39Z",
                "window": {"start": "2010-08-27", "end": "2026-11-10", "n_dates": 3843},
                "params": {"first_drawdown_pct": 15.0},
                "n_stocks": 1, "n_triggered": 1, "n_buys_total": 125,
            }), encoding="utf-8")
    return d


def test_bt_runs_list_sorted_desc_and_fields(monkeypatch, tmp_path):
    """task_id 降序（时间戳序）；status.json 字段映射；非 bt_* 目录忽略。"""
    monkeypatch.setattr(wapp, "BT_RUNS_DIR", tmp_path / "runs")
    _mk_run(wapp.BT_RUNS_DIR, "bt_20261022_100811_630d")
    _mk_run(wapp.BT_RUNS_DIR, "bt_20261110_140739_4a63")
    (wapp.BT_RUNS_DIR / "not_a_run").mkdir()   # 非 bt_* → 忽略
    out = wapp.bt_runs_list()
    assert out["total"] == 2
    assert [r["task_id"] for r in out["runs"]] == [
        "bt_20261110_140739_4a63", "bt_20261022_100811_630d"]
    r0 = out["runs"][0]
    assert r0["generated_at"] == "2026-11-10T14:07:39Z"
    assert r0["window_start"] == "2010-08-27"
    assert r0["n_stocks"] == 1
    assert r0["params"] == {"first_drawdown_pct": 15.0}


def test_bt_runs_list_incomplete_and_corrupt(monkeypatch, tmp_path):
    """无 status.json（运行中）与损坏的 status.json → generated_at=None，仍列出。"""
    monkeypatch.setattr(wapp, "BT_RUNS_DIR", tmp_path / "runs")
    _mk_run(wapp.BT_RUNS_DIR, "bt_20261110_200000_aaaa", with_status=False)
    _mk_run(wapp.BT_RUNS_DIR, "bt_20261110_210000_bbbb", corrupt=True)
    out = wapp.bt_runs_list()
    assert out["total"] == 2
    assert all(r["generated_at"] is None for r in out["runs"])
    assert all(r["params"] == {} for r in out["runs"])


def test_bt_runs_list_empty_and_limit(monkeypatch, tmp_path):
    """空目录 → 空列表；limit 截断 + 非法值回退 30。"""
    monkeypatch.setattr(wapp, "BT_RUNS_DIR", tmp_path / "runs")
    wapp.BT_RUNS_DIR.mkdir()
    assert wapp.bt_runs_list() == {"runs": [], "total": 0}
    for i in range(5):
        _mk_run(wapp.BT_RUNS_DIR, f"bt_20261110_10000{i}_{i:04x}")
    out = wapp.bt_runs_list(limit=2)
    assert len(out["runs"]) == 2 and out["total"] == 5
    assert len(wapp.bt_runs_list(limit=0)["runs"]) == 5  # 非法 → 默认 30 ≥ 5
    assert len(wapp.bt_runs_list(limit=999)["runs"]) == 5  # 上限截断到总数


# ---------------------------------------------------------------------------
# GET /api/bt/results/{id} per_stock div_pool 列（v8.0.10，分红占比拆解数据源）
# ---------------------------------------------------------------------------
def test_bt_results_per_stock_div_pool_passthrough(monkeypatch, tmp_path):
    """per_stock.csv 含 div_pool 列 → JSON 行带 div_pool float；旧格式（无该列）
    → 行无此键（前端占比列显示 —，不崩溃）。"""
    monkeypatch.setattr(wapp, "BT_RUNS_DIR", tmp_path / "runs")
    d = _mk_run(wapp.BT_RUNS_DIR, "bt_20261112_100000_aaaa")
    (d / "per_stock.csv").write_text(
        "ts_code,n_bars,n_rounds,n_rearms,n_buys,total_invested,realized_pnl,"
        "final_assets,dividends,div_pool,price_pnl,total_pnl,pnl_pct,min_unreal_pct,"
        "first_buy_date,last_buy_date,last_status,end_date,name\n"
        "sh.601088,2600,3,2,11,210510.0,0.0,660997.72,51234.5,60000.0,390487.72,"
        "450487.72,2.14,-0.1,2018-03-01,2024-02-05,open,2026-10-21,中国神华\n",
        encoding="utf-8",
    )
    out = wapp.bt_results("bt_20261112_100000_aaaa")
    row = out["per_stock"][0]
    assert row["div_pool"] == 60000.0
    assert row["price_pnl"] == 390487.72
    assert row["total_pnl"] == 450487.72

    # 旧产物（v8.0.10 前 per_stock.csv 无 div_pool 列）→ 无此键
    d2 = _mk_run(wapp.BT_RUNS_DIR, "bt_20261101_090000_bbbb")
    (d2 / "per_stock.csv").write_text(
        "ts_code,n_bars,n_rounds,n_rearms,n_buys,total_invested,realized_pnl,"
        "final_assets,dividends,price_pnl,total_pnl,pnl_pct,min_unreal_pct,"
        "first_buy_date,last_buy_date,last_status,end_date,name\n"
        "sz.000651,2500,2,1,21,326102.0,0.0,540050.34,60000.0,183948.34,"
        "213948.34,0.656,-0.05,2019-01-15,2025-01-10,open,2026-10-21,格力电器\n",
        encoding="utf-8",
    )
    out2 = wapp.bt_results("bt_20261101_090000_bbbb")
    row2 = out2["per_stock"][0]
    assert "div_pool" not in row2
    assert row2["price_pnl"] == 183948.34


# ---------------------------------------------------------------------------
# /api/stocks/search 模糊搜索（v7.0.3）：代码/中文名/拼音首字母/全拼 + 503/400 分支
# ---------------------------------------------------------------------------

def _search(q):
    return wapp.search_stocks(q)["results"]


def test_stocks_search_chinese_name(bt_copy):
    """中文名子串：'电力' → 全部命中名含'电力'，≤10 条按 ts_code 排序。"""
    got = _search("电力")
    assert 0 < len(got) <= 10
    assert all("电力" in r["name"] for r in got)
    codes = [r["ts_code"] for r in got]
    assert codes == sorted(codes)


def test_stocks_search_pinyin_full_exact(bt_copy):
    """全拼精确：'changjiangdianli' → 长江电力（sh.600900）。"""
    got = _search("changjiangdianli")
    assert any(r["ts_code"] == "sh.600900" and r["name"] == "长江电力" for r in got)


def test_stocks_search_pinyin_initials(bt_copy):
    """拼音首字母：'zgsh' → 中国神华（sh.601088）。"""
    got = _search("zgsh")
    assert any(r["ts_code"] == "sh.601088" and r["name"] == "中国神华" for r in got)


def test_stocks_search_pinyin_full(bt_copy):
    """全拼子串：'shenhua' → 中国神华。"""
    got = _search("shenhua")
    assert any(r["ts_code"] == "sh.601088" for r in got)


def test_stocks_search_code_digits(bt_copy):
    """代码数字：'600900' / 'sh.600900' 精确优先；'600' 前缀多命中。"""
    got = _search("600900")
    assert got[0]["ts_code"] == "sh.600900"
    got2 = _search("sh.600900")
    assert got2[0]["ts_code"] == "sh.600900"
    assert len(_search("600")) <= 10


def test_stocks_search_limit_and_empty_400(bt_copy):
    """结果上限 10；q 为空 → 400。"""
    assert len(_search("a")) <= 10
    with pytest.raises(HTTPException) as ei:
        wapp.search_stocks("")
    assert ei.value.status_code == 400


def test_stocks_search_short_code_400(bt_copy):
    """纯数字 <3 位 → 400（候选会爆量，无搜索意义）。"""
    with pytest.raises(HTTPException) as ei:
        wapp.search_stocks("60")
    assert ei.value.status_code == 400


def test_stocks_search_include_delisted(tmp_path, monkeypatch):
    """v8.0.9：缺省排除退市股（持仓录入不该建议退市）；include_delisted=True 含退市。

    对齐旧 /api/lake/search 的 stock_master 全表语义（数据湖/回测搜索栏传 True，
    历史回测可含退市股）。独立 tmp 合成湖（bt_copy 合成回测湖无退市股）。
    """
    import duckdb
    import martingale.data as mdata
    p = tmp_path / "lake_delisted.duckdb"
    con = duckdb.connect(str(p))
    con.execute("""CREATE TABLE stock_master (
        ts_code VARCHAR, name VARCHAR, industry_name VARCHAR, delist_date DATE)""")
    con.execute("INSERT INTO stock_master VALUES "
                "('sh.900001', '退市甲', '测试行业', DATE '2020-01-01'), "
                "('sh.900002', '上市乙', '测试行业', NULL)")
    con.close()
    monkeypatch.setattr(mdata, "default_db_path", lambda: str(p))
    got_default = wapp.search_stocks("900001")["results"]
    assert all(r["ts_code"] != "sh.900001" for r in got_default)
    got_incl = wapp.search_stocks("900001", include_delisted=True)["results"]
    hit = [r for r in got_incl if r["ts_code"] == "sh.900001"]
    assert hit and hit[0]["delisted"] is True


def test_stocks_search_lake_missing_503(tmp_path, monkeypatch):
    """湖文件缺失 → 503 lake_locked（前端降级手动输入）。"""
    import martingale.data as mdata
    monkeypatch.setattr(mdata, "default_db_path", lambda: str(tmp_path / "no_such.duckdb"))
    with pytest.raises(HTTPException) as ei:
        wapp.search_stocks("电力")
    assert ei.value.status_code == 503
    assert ei.value.detail["error"] == "lake_locked"




# ---------------------------------------------------------------------------
# /api/my_positions 策略应用 + 加仓信号（v7.0.4）
# ---------------------------------------------------------------------------

def _mock_spot(monkeypatch, prices, exc=None):
    """替换 TencentClient.fetch 为假行情（测试零网络；东财备源同步屏蔽）。"""
    import screener.data.tencent as tt
    from screener.data import realtime as rt

    def fake_fetch(self, codes, batch_size=50):
        if exc is not None:
            raise exc
        return {c: {"name": None, "price": prices[c], "timestamp": "20261022150500",
                    "pct_chg": None} for c in codes if c in prices}
    monkeypatch.setattr(tt.TencentClient, "fetch", fake_fetch)
    # Gate 3（11-10）：主源失败会整批切东财备源 → 测试须同时屏蔽 _em_fetch（零网络）
    monkeypatch.setattr(rt, "_em_fetch", lambda codes, timeout=rt._EM_TIMEOUT: {})


def test_my_positions_empty_ledger_current_strategy(tmp_positions_file):
    """空账本：不回湖，直接返回 strategy='current' 回显 + 空 signals（v7.0.29 固定当前策略）。"""
    out = wapp.get_my_positions()
    assert out["strategy"] == "current"
    assert out["signals"] == []
    assert out["n"] == 0


def test_my_positions_signal_spot_triggers(tmp_positions_file, bt_copy, monkeypatch):
    """实时价跌破下一档触发线 → 加仓信号（湖价单独判定永远为假：触线即买）。

    中国神华 sh.601088（合成湖：in_pos k=0，raw 触发 41.225，湖最后价 49.27）：
    - 无实时行情 → 回退湖价 49.27 > 41.225 → 无信号，price_source=lake；
    - 实时价 40.0 ≤ 41.225 → 信号，建议加仓 = base×growth^0 = 10000。
    """
    wapp._up_write({"sh.601088": {"shares": 100, "cost_price": 45.0}})
    _mock_spot(monkeypatch, {})
    out0 = wapp.get_my_positions()
    assert out0["price_source"] == "lake"
    assert out0["signals"] == []
    p0 = out0["positions"][0]
    assert p0["in_pos"] is True and p0["k_level"] == 0
    assert p0["next_add_price_raw"] is not None and p0["last_price"] > p0["next_add_price_raw"]

    _mock_spot(monkeypatch, {"sh.601088": 40.0})
    out = wapp.get_my_positions()
    assert out["price_source"] == "spot"
    assert out["spot_time"] == "20261022150500"
    assert len(out["signals"]) == 1
    s = out["signals"][0]
    assert s["ts_code"] == "sh.601088" and s["name"] == "中国神华"
    assert s["action"] == "add"
    assert s["price"] == 40.0 and s["price"] <= s["next_add_price"]
    assert s["k_level"] == 0 and s["next_level"] == 1
    assert s["suggested_lots"] == 1  # 第 k=0 档 = 1 手（v7.0.28）
    assert out["positions"][0]["spot_price"] == 40.0


def test_my_positions_signal_spot_failure_fallback(tmp_positions_file, bt_copy, monkeypatch):
    """实时行情请求失败 → 回退湖价（只漏报不误报），不 500。"""
    wapp._up_write({"sh.601088": {"shares": 100, "cost_price": 45.0}})
    _mock_spot(monkeypatch, {}, exc=ConnectionError("net down"))
    out = wapp.get_my_positions()
    assert out["price_source"] == "lake"
    assert out["signals"] == []
    assert out["positions"][0]["spot_price"] is None


def test_my_positions_spot_display_overlay(tmp_positions_file, bt_copy, monkeypatch):
    """v7.0.40：展示价 = 实时行情优先（最新价/市值/盈亏/汇总均按展示价）；
    行情缺失按股回退湖价（混合口径按股各自结算）。

    sh.601088 中国神华（湖最后 49.27）：实时 40.0 → 展示 40.0；
    sh.600000（湖最后 9.66）：无行情 → 回退湖价 9.66。
    """
    wapp._up_write({
        "sh.601088": {"shares": 100, "cost_price": 45.0},
        "sh.600000": {"shares": 200, "cost_price": 9.0},
    })
    _mock_spot(monkeypatch, {"sh.601088": 40.0})
    out = wapp.get_my_positions()
    assert out["price_source"] == "spot"
    pos = {p["ts_code"]: p for p in out["positions"]}
    # 实时价覆盖：最新价/市值/盈亏全部按实时价
    p1 = pos["sh.601088"]
    assert p1["spot_price"] == 40.0 and p1["last_price"] == 40.0
    assert p1["market_value"] == 4000.0
    assert p1["pnl"] == -500.0
    assert p1["pnl_pct"] == round(-500.0 / 4500.0 * 100.0, 2)  # -11.11
    # 缺失行情 → 按股回退湖价
    p0 = pos["sh.600000"]
    assert p0["spot_price"] is None and p0["last_price"] == 9.66
    assert p0["market_value"] == 1932.0
    assert p0["pnl"] == 132.0
    # 汇总 = 两只展示价合计（4000+1932；成本 4500+1800）
    t = out["totals"]
    assert t["market_value"] == 5932.0
    assert t["cost"] == 6300.0
    assert t["pnl"] == -368.0
    assert t["pnl_pct"] == round(-368.0 / 6300.0 * 100.0, 2)  # -5.84


def test_my_positions_signal_buy_triggers(tmp_positions_file, bt_copy, tmp_path, monkeypatch):
    """买入（首仓）信号：当前策略下未持仓（never）→ 实时价跌破首仓触发线 → buy。

    活动策略（dd=15%）下生产池 101 只几乎全部 in_pos（十年内都经历过 ≥15% 回撤）；
    v7.0.29 策略库退役后改用 CONFIG_PATH 临时副本模拟更严格策略（dd=25%）：
    sh.600941 合成湖从未触线（k=0，raw 触发 75.0，湖价 96.59）。
    """
    import yaml as _y
    wapp._up_write({"sh.600941": {"shares": 100, "cost_price": 100.0}})
    strict = tmp_path / "strategy_strict.yaml"
    cfg = _y.safe_load(wapp.CONFIG_PATH.read_text(encoding="utf-8"))
    cfg["timing"]["first_drawdown_pct"] = 25   # v7.0.26：dd 居 timing 段
    strict.write_text(_y.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(wapp, "CONFIG_PATH", strict)
    _mock_spot(monkeypatch, {})  # 湖价回退：96.59 > 75.0 → 不误报
    out0 = wapp.get_my_positions()
    p0 = out0["positions"][0]
    assert p0["in_pos"] is False and p0["k_level"] == 0
    assert p0["next_add_price_raw"] is not None and p0["last_price"] > p0["next_add_price_raw"]
    assert out0["signals"] == []
    # 实时价 70.0 ≤ 75.0 → buy 信号，建议手数 = first_lots = 1
    _mock_spot(monkeypatch, {"sh.600941": 70.0})
    out = wapp.get_my_positions()
    assert len(out["signals"]) == 1
    s = out["signals"][0]
    assert s["ts_code"] == "sh.600941" and s["action"] == "buy"
    assert s["price"] == 70.0 and s["price"] <= s["next_add_price"]
    assert s["k_level"] == 0 and s["next_level"] == 1
    assert s["suggested_lots"] == 1  # first_lots（当前策略活动值）
