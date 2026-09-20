# -*- coding: utf-8 -*-
"""生产路径失败守卫（9/7 缺陷修复）回归测试 —— 离线、不碰 BaoStock。

缺陷：BaoStock 封禁/降级时，v2 生产路径把**数据源级失败**伪装成"筛选后 0 只入选"
——写出只有表头的 result_*.csv + "无候选股票"的 report_*.md，Web /api/runs 把它排
第一，用户看到"入选 0"被误导。

修复（本测试逐条验证）：
1. **数据源级失败显式失败**：股票池拉取抛 BaoStockError/DataSourceError（含空股票池）
   → 运行非零退出 + 写 status=failed sidecar，**不写** result_*.csv / report_*.md。
2. **合法"0 只入选"不受影响**：股票池正常拉取、筛选逻辑正常执行后 Top N=0 → 正常
   产出空结果报告（status=ok），不产生 failed sidecar。
3. **Web 展示**：失败 run 在 /api/runs 列表与详情页有 status=failed + 错误摘要，
   不与正常 run 混同；成功 run 带 status=ok。

注入方式：打桩 BaoStockClient.call_with_fields（网络入口）——空股票池场景返回
0 行 all_stock（复现 9/7 现场：login ok 但 query_all_stock 返回空），BaoStockError
场景直接抛异常。不碰真实网络、不触发 live BaoStock。
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from screener.data.baostock_client import (  # noqa: E402
    BaoStockClient,
    BaoStockError,
    DataSourceError,
)
import pandas as pd  # noqa: E402
from screener.data.fetchers import KlineData  # noqa: E402
import screener.__main__ as cli_mod  # noqa: E402
from screener import runstatus  # noqa: E402

# 用"今天"作为请求日期（动态，永不落入未来 → _resolve_run_day 不会先拒），
# 让注入的 BaoStockError / 空股票池真正在数据拉取阶段触发。
TODAY_ISO = date.today().isoformat()
TODAY_COMPACT = TODAY_ISO.replace("-", "")


# ---------------------------------------------------------------------------
# 公共 fixture / helper
# ---------------------------------------------------------------------------

@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """隔离 CLI 运行环境：PROJECT_ROOT 指向 tmp（output/cache/logs 全部落 tmp），
    不污染仓库。打桩 _setup_logging（避免写 logs/）。返回 (output_dir, cache_dir)。"""
    root = tmp_path / "proj"
    out = root / "output"
    cache = root / "cache"
    out.mkdir(parents=True, exist_ok=True)  # Web 测试需直接往里写 result/sidecar
    monkeypatch.setattr(cli_mod, "_setup_logging", lambda *a, **k: None)
    # PROJECT_ROOT → tmp：main() 用它解析相对 cache_dir（config/strategy.yaml 里是
    # 相对路径 "cache"）→ 落 tmp/proj/cache；真实 config 用绝对路径传入，不受影响。
    monkeypatch.setattr(cli_mod, "PROJECT_ROOT", root)
    return out, cache


def _run_cli(date_s: str, out: Path):
    """跑 CLI main()（config 用仓库真实 strategy.yaml 的绝对路径），返回 rc。"""
    return cli_mod.main([
        "--date", date_s,
        "--config", str(PROJECT_ROOT / "config" / "strategy.yaml"),
        "--output-dir", str(out),
    ])


# ---------------------------------------------------------------------------
# primary 感知：02_code 修正轮把 config/strategy.yaml 的 datasource.primary 切到 lake。
# 本文件的失败守卫语义（数据源级失败 → 非零退出 + failed sidecar，不写误导性
# result/report）必须在**两条主源路径**都成立：
#   - tencent/baostock（回退路径）：注入点 = BaoStockClient.call_with_fields（网络入口）。
#   - lake（生产路径）：LakeDataFetcher 直接 SQL 读 DuckDB、永不发 BaoStock/腾讯网络，
#     故注入点 = LakeDataFetcher 的取数方法（latest_trade_date/all_stock/kline_af3_
#     incremental），抛 DataSourceError（与 lake_source 锁定/空库失败同一异常族）。
# 用 load_config 包装强制 primary（不改仓库 yaml——yaml 已是 lake，tencent 参数是回退验证）。
# ---------------------------------------------------------------------------

def _force_primary(monkeypatch, primary: str):
    """让 CLI 内 run_screener 按指定 primary 构造 fetcher（不改磁盘 yaml）。"""
    from screener import config as cfgmod
    orig = cfgmod.load_config

    def _wrapped(path):
        cfg = orig(path)
        cfg.setdefault("datasource", {})["primary"] = primary
        return cfg

    monkeypatch.setattr(cfgmod, "load_config", _wrapped)


def _run_cli_primary(date_s: str, out: Path, primary: str, monkeypatch):
    """_run_cli 的 primary 感知版：先强制 datasource.primary 再跑 CLI。"""
    _force_primary(monkeypatch, primary)
    return cli_mod.main([
        "--date", date_s,
        "--config", str(PROJECT_ROOT / "config" / "strategy.yaml"),
        "--output-dir", str(out),
    ])


def _lake_one_row_kline():
    """KlineData(n_rows=1)：上市不足 listing_min_trading_days → 被硬剔除（合法 0 入选）。"""
    return KlineData(
        code="", dates=[TODAY_ISO], closes=[8.1], tradestatus=[1],
        last_date=TODAY_ISO, n_rows=1, current_price=8.1, is_st=0, run_day_tradestatus=1,
    )


def _inject_lake_failures(monkeypatch, *, latest=None, latest_return=None,
                          all_stock_df=None, kline_one_row=False):
    """lake 路径数据源失败/桩注入（打桩 LakeDataFetcher 取数方法）。

    - latest=异常 → latest_trade_date 抛该异常（交易日定位即炸，数据源级失败）；
    - latest_return=date → latest_trade_date 返回该日期（run_day 正常解析，继续往下走）；
    - all_stock_df → all_stock 返回该 DataFrame（空表→build_universe 抛"0 只证券"）；
    - kline_one_row=True → 每只 K线仅 1 行（上市不足被硬剔除，合法 0 入选路径）。
    参数缺省 = 不桩该方法（走真实生产 lake）。
    """
    from screener.data.lake_source import LakeDataFetcher
    if latest is not None:
        monkeypatch.setattr(
            LakeDataFetcher, "latest_trade_date",
            lambda self, on_or_before: (_ for _ in ()).throw(latest))
    elif latest_return is not None:
        monkeypatch.setattr(
            LakeDataFetcher, "latest_trade_date",
            lambda self, on_or_before: latest_return)
    if all_stock_df is not None:
        df = all_stock_df
        monkeypatch.setattr(LakeDataFetcher, "all_stock", lambda self, day: df)
    if kline_one_row:
        kl = _lake_one_row_kline()
        monkeypatch.setattr(
            LakeDataFetcher, "kline_af3_incremental", lambda self, code, run_day: kl)


# ---------------------------------------------------------------------------
# 1. 数据源级失败 → 非零退出 + sidecar，不写误导性 result/report
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("primary", ["tencent", "lake"], ids=["tencent_rollback", "lake_primary"])
def test_empty_pool_is_data_source_failure_not_zero_selected(cli_env, monkeypatch, primary):
    """9/7 现场复现：login ok 但 query_all_stock 返回 0 行（封禁/降级）。

    断言：①非零退出 ②不写 result_*.csv / report_*.md ③写 status=failed sidecar。
    primary=tencent：注入点=BaoStockClient.call_with_fields（all_stock 返回空）。
    primary=lake：LakeDataFetcher.all_stock 返回空 DataFrame → build_universe 抛"0 只证券"。
    """
    out, _ = cli_env

    if primary == "lake":
        _force_primary(monkeypatch, "lake")
        empty_df = pd.DataFrame(columns=["code", "tradeStatus", "code_name"])
        _inject_lake_failures(monkeypatch, latest_return=date.fromisoformat(TODAY_ISO),
                              all_stock_df=empty_df)
    else:
        _force_primary(monkeypatch, "tencent")

        def fake_call_with_fields(self, query_fn, *, label="", **kwargs):
            # trade_dates：今天为交易日（让 run_day 定位成功，进入股票池拉取）
            if label == "trade_dates":
                return ["calendar_date", "is_trading_day"], [[TODAY_ISO, "1"]]
            # all_stock：返回空（封禁现场）→ build_universe 抛 DataSourceError
            if label == "all_stock":
                return ["code", "tradeStatus", "code_name"], []
            raise AssertionError(f"unexpected call: {label}")

        monkeypatch.setattr(BaoStockClient, "call_with_fields", fake_call_with_fields)

    rc = _run_cli(TODAY_ISO, out)
    assert rc != 0, f"[{primary}] 数据源级失败必须非零退出"

    # 不写误导性空结果
    assert list(out.glob("result_*.csv")) == [], "失败不得写 result CSV"
    assert list(out.glob("report_*.md")) == [], "失败不得写 report"

    # 写 status=failed sidecar（run_day 已解析=今天 → 按运行日命名）
    sc_path = out / f"run_status_{TODAY_COMPACT}.json"
    assert sc_path.exists(), f"[{primary}] 数据源级失败必须写 failed sidecar"
    sc = json.loads(sc_path.read_text(encoding="utf-8"))
    assert sc["status"] == "failed"
    assert "query_all_stock" in sc["error"] and "0 只证券" in sc["error"], \
        f"[{primary}] sidecar error 应含空池守卫文案: {sc['error']}"
    assert sc["reason_code"] == "data_source"


@pytest.mark.parametrize("primary", ["tencent", "lake"], ids=["tencent_rollback", "lake_primary"])
def test_baostock_error_injection_fails_with_sidecar(cli_env, monkeypatch, primary):
    """数据源级失败注入（登录失败/重试耗尽）→ 非零退出 + sidecar，无 result/report。

    primary=tencent：BaoStockClient.call_with_fields 抛 BaoStockError（交易日定位即炸）。
    primary=lake：LakeDataFetcher.latest_trade_date 抛 DataSourceError（同数据源失败族，
    lake 的"库不可用/被锁定"现场——与 BaoStock 登录失败同语义，不碰真实网络）。
    """
    out, _ = cli_env

    if primary == "lake":
        _force_primary(monkeypatch, "lake")
        _inject_lake_failures(
            monkeypatch,
            latest=DataSourceError("数据湖被其他进程独占（backfill 灌数进行中）"))
    else:
        _force_primary(monkeypatch, "tencent")

        def fake_call_with_fields(self, query_fn, *, label="", **kwargs):
            # trade_dates 就炸（模拟 login 失败 → 任何查询都抛 BaoStockError）
            raise BaoStockError("baostock login 失败: 黑名单用户，请与管理员联系")

        monkeypatch.setattr(BaoStockClient, "call_with_fields", fake_call_with_fields)

    rc = _run_cli(TODAY_ISO, out)
    assert rc != 0, f"[{primary}] 数据源级失败必须非零退出"
    assert list(out.glob("result_*.csv")) == []
    assert list(out.glob("report_*.md")) == []
    sc_path = out / f"run_status_{TODAY_COMPACT}.json"
    assert sc_path.exists(), f"[{primary}] 必须写 failed sidecar"
    sc = json.loads(sc_path.read_text(encoding="utf-8"))
    assert sc["status"] == "failed"
    if primary == "tencent":
        assert "黑名单用户" in sc["error"]
    else:
        assert "数据湖被其他进程独占" in sc["error"]
    assert sc["reason_code"] == "data_source"


@pytest.mark.parametrize("primary", ["tencent", "lake"], ids=["tencent_rollback", "lake_primary"])
def test_sidecar_uses_requested_date_when_run_day_unresolved(cli_env, monkeypatch, primary):
    """失败发生在交易日定位之前（trade_dates 就炸）→ sidecar 用请求日期命名。

    注：这里请求日=今天（非未来），但 trade_dates 查询直接抛数据源异常 →
    run_day 无法解析（保持空）→ CLI 回退到请求日期命名 sidecar。
    primary=tencent：BaoStockClient.call_with_fields 抛 BaoStockError。
    primary=lake：LakeDataFetcher.latest_trade_date 抛 DataSourceError（定位即炸）。
    """
    out, _ = cli_env

    if primary == "lake":
        _force_primary(monkeypatch, "lake")
        _inject_lake_failures(
            monkeypatch, latest=DataSourceError("数据湖被其他进程独占"))
    else:
        _force_primary(monkeypatch, "tencent")

        def fake_call_with_fields(self, query_fn, *, label="", **kwargs):
            raise BaoStockError("baostock login 失败: 黑名单用户")

        monkeypatch.setattr(BaoStockClient, "call_with_fields", fake_call_with_fields)

    rc = _run_cli(TODAY_ISO, out)
    assert rc != 0, f"[{primary}] 必须非零退出"
    # run_day 未解析（定位前失败）→ sidecar 按请求日期命名（=今天）
    assert (out / f"run_status_{TODAY_COMPACT}.json").exists(), \
        f"[{primary}] run_day 未解析时 sidecar 应按请求日期命名"


def test_usage_error_future_date_writes_no_sidecar(cli_env, monkeypatch):
    """边界回归：--date 未来日期是**用法错误**（非数据源级失败）→ 非零退出但**不写 sidecar**。

    若误把用法错误也写成 status=failed sidecar，会给一个从未真正运行的日期凭空造出
    "运行失败"记录（幻影 failed run），并污染真实 output/。本测试锁定该边界：
    未来日期被 _resolve_run_day 拒绝 → rc!=0，但 output/ 里没有任何 run_status_*.json。
    （数据源打桩为"一碰就炸"，确保若守卫失效、流程走到拉数据也会立刻失败而非挂起。）
    """
    out, _ = cli_env
    from datetime import timedelta

    def fake_call_with_fields(self, query_fn, *, label="", **kwargs):
        raise BaoStockError("NETWORK_REACHED")  # 若误入数据路径立即炸

    monkeypatch.setattr(BaoStockClient, "call_with_fields", fake_call_with_fields)

    future = (date.today() + timedelta(days=30)).isoformat()
    rc = _run_cli(future, out)
    assert rc != 0, "未来日期必须非零退出"
    # 关键：用法错误不得写 failed sidecar（避免幻影 failed run）
    assert list(out.glob("run_status_*.json")) == [], \
        f"用法错误（未来日期）不得写 sidecar: {list(out.glob('run_status_*.json'))}"
    assert list(out.glob("result_*.csv")) == []
    assert list(out.glob("report_*.md")) == []


# ---------------------------------------------------------------------------
# 2. 合法"0 只入选"路径不受影响（正常产出空结果报告，无 sidecar）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("primary", ["tencent", "lake"], ids=["tencent_rollback", "lake_primary"])
def test_legit_zero_selected_still_writes_empty_result(cli_env, monkeypatch, primary):
    """股票池正常拉取、筛选逻辑正常执行后硬剔除到 0 → 正常写空 result/report。

    构造：all_stock 返回 2 只（数据源健康），但每只 K线仅 1 行（< listing_min_trading_days=250）
    → 上市不足被硬剔除 → 合法"0 入选"。这是必须保留的路径，不得被失败守卫误伤。

    primary=tencent：注入点=BaoStockClient.call_with_fields（all_stock/kline_af3_ 桩）。
    primary=lake：LakeDataFetcher.all_stock 返回 2 只、kline_af3_incremental 返回 1 行
    KlineData → stage2 上市不足剔除到 0 → _NoCandidates（在 industry/v5 硬过滤**之前**，
    故不触 em_dividend_all.csv）。两条路径都验证"合法 0 入选 ≠ 数据源失败"。
    """
    out, _ = cli_env
    pool_rows = [["sh.601398", "1", "工商银行"], ["sz.000001", "1", "平安银行"]]

    if primary == "lake":
        _force_primary(monkeypatch, "lake")
        pool_df = pd.DataFrame(
            [[c, int(ts), nm] for c, ts, nm in pool_rows],
            columns=["code", "tradeStatus", "code_name"])
        _inject_lake_failures(monkeypatch, latest_return=date.fromisoformat(TODAY_ISO),
                              all_stock_df=pool_df, kline_one_row=True)
    else:
        _force_primary(monkeypatch, "tencent")

        def fake_call_with_fields(self, query_fn, *, label="", **kwargs):
            if label == "trade_dates":
                return ["calendar_date", "is_trading_day"], [[TODAY_ISO, "1"]]
            if label == "all_stock":
                return ["code", "tradeStatus", "code_name"], pool_rows
            if label.startswith("kline_af3_"):
                # 仅 1 行（n_rows=1 < listing_min_trading_days）→ 上市不足被硬剔除
                return ["date", "code", "close", "isST", "tradestatus"], \
                       [[TODAY_ISO, kwargs.get("code", ""), "8.1", "0", "1"]]
            raise AssertionError(f"unexpected call: {label}")

        monkeypatch.setattr(BaoStockClient, "call_with_fields", fake_call_with_fields)

    rc = _run_cli(TODAY_ISO, out)
    assert rc == 0, f"[{primary}] 合法 0 入选必须成功退出"

    # 正常产出空结果（表头 CSV + 报告）
    result_files = list(out.glob("result_*.csv"))
    report_files = list(out.glob("report_*.md"))
    assert len(result_files) == 1, f"[{primary}] 合法 0 入选必须写 result CSV"
    assert len(report_files) == 1, f"[{primary}] 合法 0 入选必须写 report"

    # 不产生 failed sidecar（数据源健康）
    assert list(out.glob("run_status_*.json")) == [], \
        f"[{primary}] 合法 0 入选不得写 failed sidecar"


# ---------------------------------------------------------------------------
# 3. Web 展示：失败 run 标红 + 错误摘要，成功 run status=ok
# ---------------------------------------------------------------------------

def test_web_list_runs_marks_failed(cli_env, monkeypatch):
    """/api/runs：有 failed sidecar（无 result CSV）的日 → status=failed + error。"""
    out, _ = cli_env
    runstatus.write_failed_sidecar(
        str(out), "2026-09-07", "2026-09-07",
        BaoStockError("baostock login 失败: 黑名单用户"),
    )
    import web.app as appmod
    monkeypatch.setattr(appmod, "OUTPUT_DIR", out)

    data = appmod.list_runs()
    runs = {r["date"]: r for r in data["runs"]}
    assert "2026-09-07" in runs
    fr = runs["2026-09-07"]
    assert fr["status"] == "failed"
    assert fr["selected_count"] is None  # 失败无入选数（不是 0）
    assert "黑名单用户" in (fr.get("error") or "")
    assert fr["reason_code"] == "data_source"


def test_web_list_runs_success_is_ok(cli_env, monkeypatch):
    """/api/runs：有 result CSV 的成功 run → status=ok，selected_count 为整数。"""
    out, _ = cli_env
    (out / "result_20260904.csv").write_text(
        "code,name,top_n_selected\nsh.601398,工商银行,1\n", encoding="utf-8-sig"
    )
    import web.app as appmod
    monkeypatch.setattr(appmod, "OUTPUT_DIR", out)

    data = appmod.list_runs()
    runs = {r["date"]: r for r in data["runs"]}
    assert runs["2026-09-04"]["status"] == "ok"
    assert runs["2026-09-04"]["selected_count"] == 1


def test_web_list_runs_success_supersedes_failed_sidecar(cli_env, monkeypatch):
    """同日既有 result CSV（成功重跑）又有旧 failed sidecar → 只展示成功，不标红。"""
    out, _ = cli_env
    runstatus.write_failed_sidecar(
        str(out), "2026-09-07", "2026-09-07", BaoStockError("黑名单用户")
    )
    (out / "result_20260907.csv").write_text(
        "code,name,top_n_selected\nsh.601398,工商银行,1\n", encoding="utf-8-sig"
    )
    import web.app as appmod
    monkeypatch.setattr(appmod, "OUTPUT_DIR", out)

    data = appmod.list_runs()
    runs = {r["date"]: r for r in data["runs"]}
    # 只有一条，且是成功（sidecar 被 result CSV 取代）
    assert len(runs) == 1
    assert runs["2026-09-07"]["status"] == "ok"


def test_web_run_detail_failed_returns_error_not_404(cli_env, monkeypatch):
    """/api/runs/{day}：无 result CSV 但有 failed sidecar → 200 + status=failed。"""
    out, _ = cli_env
    runstatus.write_failed_sidecar(
        str(out), "2026-09-07", "2026-09-07",
        BaoStockError("baostock login 失败: 黑名单用户"),
    )
    import web.app as appmod
    monkeypatch.setattr(appmod, "OUTPUT_DIR", out)

    d = appmod.run_detail("2026-09-07")
    assert d["status"] == "failed"
    assert "黑名单用户" in (d.get("error") or "")
    assert d["selected"] == []
    assert d["survivors"] == []


def test_web_run_detail_missing_still_404(cli_env, monkeypatch):
    """/api/runs/{day}：既无 result CSV 也无 sidecar → 仍 404（原有行为不变）。"""
    out, _ = cli_env
    import web.app as appmod
    from fastapi import HTTPException
    monkeypatch.setattr(appmod, "OUTPUT_DIR", out)

    with pytest.raises(HTTPException) as ei:
        appmod.run_detail("2026-09-07")
    assert ei.value.status_code == 404
