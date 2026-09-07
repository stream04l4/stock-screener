# -*- coding: utf-8 -*-
"""生产路径失败守卫 —— 端到端 smoke 自测（离线注入，不碰 live BaoStock）。

在真实 CLI 代码路径（main → run_screener → build_universe → sidecar）上注入
BaoStock 数据源级失败，捕获**真实**退出码 / stdout / stderr / 落盘产物。
零 live BaoStock：打桩 BaoStockClient.call_with_fields（网络唯一入口）。

场景：
  A. 9/7 现场复现 —— login ok 但 query_all_stock 返回空池（封禁/降级）
  B. BaoStockError 注入 —— 登录失败"黑名单用户"
  C. 合法 0 入选 —— 股票池正常拉取、筛选逻辑执行后硬剔除到 0（必须仍成功）
  D. Web /api/runs + /api/runs/{day} —— 失败 run 标红 + 错误摘要
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import contextlib
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from screener.data.baostock_client import BaoStockClient, BaoStockError
import screener.__main__ as cli_mod
import web.app as appmod
from screener import runstatus

TODAY = date.today()
TODAY_ISO = TODAY.isoformat()
TODAY_C = TODAY.strftime("%Y%m%d")


def _banner(title):
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def run_cli_inproc(date_s, out_dir, call_stub, label):
    """在进程内跑真实 CLI main()，注入网络入口打桩，捕获 rc/stdout/stderr。"""
    cli_mod._setup_logging = lambda *a, **k: None  # 不写 logs/
    old_root = cli_mod.PROJECT_ROOT
    cli_mod.PROJECT_ROOT = Path(out_dir).parent  # cache 落 tmp
    buf_out, buf_err = io.StringIO(), io.StringIO()
    rc = None
    try:
        with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            orig_cwf = BaoStockClient.call_with_fields
            BaoStockClient.call_with_fields = call_stub
            try:
                rc = cli_mod.main([
                    "--date", date_s,
                    "--config", str(PROJECT_ROOT / "config" / "strategy.yaml"),
                    "--output-dir", out_dir,
                ])
            finally:
                BaoStockClient.call_with_fields = orig_cwf
    finally:
        cli_mod.PROJECT_ROOT = old_root
    return rc, buf_out.getvalue(), buf_err.getvalue()


def show(rc, out, err):
    print(f"[returncode] {rc}")
    if out.strip():
        print("--- stdout ---")
        print(out.strip())
    if err.strip():
        print("--- stderr ---")
        print(err.strip())


def list_files(d):
    d = Path(d)
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


# ===========================================================================
# 场景 A：9/7 现场复现（login ok，query_all_stock 返回空池）
# ===========================================================================
def scenario_a(tmp):
    _banner("场景 A：9/7 现场复现 —— query_all_stock 返回空池（BaoStock 封禁/降级）")

    def stub(self, query_fn, *, label="", **kwargs):
        if label == "trade_dates":
            return ["calendar_date", "is_trading_day"], [[TODAY_ISO, "1"]]
        if label == "all_stock":
            return ["code", "tradeStatus", "code_name"], []  # ← 空池（封禁现场）
        raise AssertionError(f"unexpected: {label}")

    out = str(tmp / "a" / "output")
    rc, o, e = run_cli_inproc(TODAY_ISO, out, stub, "A")
    show(rc, o, e)
    print(f"[落盘产物] {list_files(out)}")
    sc = Path(out) / f"run_status_{TODAY_C}.json"
    if sc.exists():
        print("[sidecar 内容]")
        print(json.dumps(json.loads(sc.read_text()), ensure_ascii=False, indent=2))
    # 断言
    assert rc != 0, "A: 数据源级失败必须非零退出"
    assert not list(Path(out).glob("result_*.csv")), "A: 不得写 result CSV"
    assert not list(Path(out).glob("report_*.md")), "A: 不得写 report"
    assert sc.exists(), "A: 必须写 failed sidecar"
    print(">>> 场景 A PASS：非零退出 + 无误导性空结果 + status=failed sidecar")


# ===========================================================================
# 场景 B：BaoStockError 注入（登录失败"黑名单用户"）
# ===========================================================================
def scenario_b(tmp):
    _banner("场景 B：BaoStockError 注入 —— baostock login 失败: 黑名单用户")

    def stub(self, query_fn, *, label="", **kwargs):
        raise BaoStockError("baostock login 失败: 黑名单用户，请与管理员联系")

    out = str(tmp / "b" / "output")
    rc, o, e = run_cli_inproc(TODAY_ISO, out, stub, "B")
    show(rc, o, e)
    print(f"[落盘产物] {list_files(out)}")
    sc = Path(out) / f"run_status_{TODAY_C}.json"
    if sc.exists():
        print("[sidecar 内容]")
        print(json.dumps(json.loads(sc.read_text()), ensure_ascii=False, indent=2))
    assert rc != 0, "B: BaoStockError 必须非零退出"
    assert not list(Path(out).glob("result_*.csv")), "B: 不得写 result CSV"
    assert not list(Path(out).glob("report_*.md")), "B: 不得写 report"
    assert sc.exists(), "B: 必须写 failed sidecar"
    print(">>> 场景 B PASS：非零退出 + 无误导性空结果 + status=failed sidecar")


# ===========================================================================
# 场景 C：合法 0 入选（股票池正常、筛选逻辑执行后硬剔除到 0）
# ===========================================================================
def scenario_c(tmp):
    _banner("场景 C：合法 0 入选 —— 股票池正常拉取，K线不足上市天数被硬剔除")

    def stub(self, query_fn, *, label="", **kwargs):
        if label == "trade_dates":
            return ["calendar_date", "is_trading_day"], [[TODAY_ISO, "1"]]
        if label == "all_stock":
            # 数据源健康：返回 2 只正常证券
            return ["code", "tradeStatus", "code_name"], \
                   [["sh.601398", "1", "工商银行"], ["sz.000001", "1", "平安银行"]]
        if label.startswith("kline_af3_"):
            # 每只仅 1 行 K线（< listing_min_trading_days=250）→ 上市不足被硬剔除
            return ["date", "code", "close", "isST", "tradestatus"], \
                   [[TODAY_ISO, kwargs.get("code", ""), "8.1", "0", "1"]]
        raise AssertionError(f"unexpected: {label}")

    out = str(tmp / "c" / "output")
    rc, o, e = run_cli_inproc(TODAY_ISO, out, stub, "C")
    show(rc, o, e)
    print(f"[落盘产物] {list_files(out)}")
    assert rc == 0, "C: 合法 0 入选必须成功退出"
    assert list(Path(out).glob("result_*.csv")), "C: 合法 0 入选必须写 result CSV"
    assert list(Path(out).glob("report_*.md")), "C: 合法 0 入选必须写 report"
    assert not list(Path(out).glob("run_status_*.json")), "C: 不得写 failed sidecar"
    print(">>> 场景 C PASS：合法 0 入选仍正常产出空结果报告（未被失败守卫误伤）")


# ===========================================================================
# 场景 D：Web /api/runs + /api/runs/{day} 失败标红
# ===========================================================================
def scenario_d(tmp):
    _banner("场景 D：Web /api/runs + /api/runs/{day} —— 失败 run 标红 + 错误摘要")
    out = tmp / "d" / "output"
    out.mkdir(parents=True, exist_ok=True)

    # 一个失败日（sidecar，无 result CSV）+ 一个成功日（result CSV）
    runstatus.write_failed_sidecar(
        str(out), TODAY_ISO, TODAY_ISO, BaoStockError("baostock login 失败: 黑名单用户")
    )
    (out / "result_20260904.csv").write_text(
        "code,name,top_n_selected\nsh.601398,工商银行,1\n", encoding="utf-8-sig"
    )

    old_out = appmod.OUTPUT_DIR
    appmod.OUTPUT_DIR = out
    try:
        runs = appmod.list_runs()["runs"]
        print("[GET /api/runs]")
        print(json.dumps(runs, ensure_ascii=False, indent=2))
        by_date = {r["date"]: r for r in runs}
        assert by_date[TODAY_ISO]["status"] == "failed", "D: 失败日必须 status=failed"
        assert by_date[TODAY_ISO]["selected_count"] is None, "D: 失败日入选数=None(非0)"
        assert "黑名单用户" in (by_date[TODAY_ISO].get("error") or ""), "D: 须含错误摘要"
        assert by_date["2026-09-04"]["status"] == "ok", "D: 成功日必须 status=ok"

        detail = appmod.run_detail(TODAY_ISO)
        print(f"\n[GET /api/runs/{TODAY_ISO}]")
        print(json.dumps(
            {k: detail[k] for k in ("status", "error_type", "error", "selected", "survivors")},
            ensure_ascii=False, indent=2))
        assert detail["status"] == "failed", "D: 详情必须 status=failed"
        assert "黑名单用户" in (detail.get("error") or ""), "D: 详情须含错误摘要"
    finally:
        appmod.OUTPUT_DIR = old_out
    print(">>> 场景 D PASS：/api/runs 失败日标红+错误摘要，成功日 status=ok，不混同")


def main():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        scenario_a(tmp)
        scenario_b(tmp)
        scenario_c(tmp)
        scenario_d(tmp)
        _banner("全部 4 个场景 PASS ✅")


if __name__ == "__main__":
    main()
