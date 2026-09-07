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
)
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
# 1. 数据源级失败 → 非零退出 + sidecar，不写误导性 result/report
# ---------------------------------------------------------------------------

def test_empty_pool_is_data_source_failure_not_zero_selected(cli_env, monkeypatch):
    """9/7 现场复现：login ok 但 query_all_stock 返回 0 行（封禁/降级）。

    断言：①非零退出 ②不写 result_*.csv / report_*.md ③写 status=failed sidecar。
    """
    out, _ = cli_env

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
    assert rc != 0, "数据源级失败必须非零退出"

    # 不写误导性空结果
    assert list(out.glob("result_*.csv")) == [], "失败不得写 result CSV"
    assert list(out.glob("report_*.md")) == [], "失败不得写 report"

    # 写 status=failed sidecar（run_day 已解析=今天 → 按运行日命名）
    sc_path = out / f"run_status_{TODAY_COMPACT}.json"
    assert sc_path.exists(), "数据源级失败必须写 failed sidecar"
    sc = json.loads(sc_path.read_text(encoding="utf-8"))
    assert sc["status"] == "failed"
    assert "query_all_stock" in sc["error"] and "0 只证券" in sc["error"]
    assert sc["reason_code"] == "data_source"


def test_baostock_error_injection_fails_with_sidecar(cli_env, monkeypatch):
    """BaoStockError 注入（登录失败/重试耗尽）→ 非零退出 + sidecar，无 result/report。"""
    out, _ = cli_env

    def fake_call_with_fields(self, query_fn, *, label="", **kwargs):
        # trade_dates 就炸（模拟 login 失败 → 任何查询都抛 BaoStockError）
        raise BaoStockError("baostock login 失败: 黑名单用户，请与管理员联系")

    monkeypatch.setattr(BaoStockClient, "call_with_fields", fake_call_with_fields)

    rc = _run_cli(TODAY_ISO, out)
    assert rc != 0
    assert list(out.glob("result_*.csv")) == []
    assert list(out.glob("report_*.md")) == []
    sc_path = out / f"run_status_{TODAY_COMPACT}.json"
    assert sc_path.exists()
    sc = json.loads(sc_path.read_text(encoding="utf-8"))
    assert sc["status"] == "failed"
    assert "黑名单用户" in sc["error"]
    assert sc["reason_code"] == "data_source"


def test_sidecar_uses_requested_date_when_run_day_unresolved(cli_env, monkeypatch):
    """失败发生在交易日定位之前（trade_dates 就炸）→ sidecar 用请求日期命名。

    注：这里请求日=今天（非未来），但 trade_dates 查询直接抛 BaoStockError →
    run_day 无法解析（保持空）→ CLI 回退到请求日期命名 sidecar。
    """
    out, _ = cli_env

    def fake_call_with_fields(self, query_fn, *, label="", **kwargs):
        raise BaoStockError("baostock login 失败: 黑名单用户")

    monkeypatch.setattr(BaoStockClient, "call_with_fields", fake_call_with_fields)

    rc = _run_cli(TODAY_ISO, out)
    assert rc != 0
    # run_day 未解析（定位前失败）→ sidecar 按请求日期命名（=今天）
    assert (out / f"run_status_{TODAY_COMPACT}.json").exists()


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

def test_legit_zero_selected_still_writes_empty_result(cli_env, monkeypatch):
    """股票池正常拉取、筛选逻辑正常执行后硬剔除到 0 → 正常写空 result/report。

    构造：all_stock 返回 2 只（数据源健康），但每只 K线仅 1 行（< 上市满 250 日）
    → 上市不足被硬剔除 → 合法"0 入选"。这是必须保留的路径，不得被失败守卫误伤。
    """
    out, _ = cli_env
    pool_rows = [["sh.601398", "1", "工商银行"], ["sz.000001", "1", "平安银行"]]

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
    assert rc == 0, "合法 0 入选必须成功退出"

    # 正常产出空结果（表头 CSV + 报告）
    result_files = list(out.glob("result_*.csv"))
    report_files = list(out.glob("report_*.md"))
    assert len(result_files) == 1, "合法 0 入选必须写 result CSV"
    assert len(report_files) == 1, "合法 0 入选必须写 report"

    # 不产生 failed sidecar（数据源健康）
    assert list(out.glob("run_status_*.json")) == [], "合法 0 入选不得写 failed sidecar"


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
