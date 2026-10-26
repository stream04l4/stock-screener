# -*- coding: utf-8 -*-
"""v7 验收红线守卫（grep 型，防回潮）。

1) tracked 文件无「已删多因子主 CLI / run_cron」活动引用：
   - 裸 `python -m screener`（后随空格/行尾 = 已删的 screener/__main__ 主 CLI）；
   - run_cron.sh 的**执行**引用（非注释行）。
   白名单语义：
   - screener/data/baostock_client.py 的 `python -m screener.data.baostock_client`
     是保留的 baostock 配额维护工具自身用法（调研 ① 保留清单），不是主 CLI 残留；
   - README.md / stages/ 为历史文档（删码记录）；
   - scripts/run_lake_sync_daily.sh 头注里的 09-23 事故说明是注释（历史）。
2) crontab 仅剩 lake-sync 一条业务行（绝不含 run_cron）。
3) git diff BASELINE -- lake/ 为空（lake 包相对基线零改动）。
  基线演进：8c49f71（v7.0.0 原始红线）→ b41fc5c（v7.0.7：Joel 2026-10-22
  拍板砍 T5 roe_avg/ocf + T6 controller_* 死列，lake/ 有意变更，基线前移）
  → bbc55f5（v7.0.8：每日增量提速 tushare 全市场批量 T2/T3 + None 重试
  加固 + 配额惩罚 fast-fail，lake/ 有意变更，基线前移）
  → 4c8e0a9（v7.0.9：表级完整性预检 + 退出 CHECKPOINT + 进度簿记批量化，
  lake/ 有意变更，基线前移）
  → 9b2e026（v7.0.16：lake.af_rebase 多源 adj_factor 基准对齐新增模块，
  lake/ 有意变更，基线前移；Joel 2026-10-26 确认）
  → 88cf6fc（v7.0.18：baostock 渠道完全移除，lake/ 有意变更，基线前移）。
4) pyproject：version 7.0.0 + packages 三包。
5) strategy.yaml schema 形状（9/5 键 + lake + datasource 三段齐全）。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# v7.0.18 baostock 渠道完全移除（88cf6fc）：lake/ 有意变更，基线前移。
BASELINE = "88cf6fc"


def _git(*args) -> str:
    r = subprocess.run(["git", *args], cwd=PROJECT_ROOT, capture_output=True, text=True)
    assert r.returncode == 0, f"git {' '.join(args)} 失败: {r.stderr}"
    return r.stdout


# ---------------------------------------------------------------------------
# 1) 残留引用扫描
# ---------------------------------------------------------------------------

_BARE_CLI = re.compile(r"python -m screener(\s|$)")          # 裸主 CLI（非 .data.* 子模块）
_RUNCORN_EXEC = re.compile(r"(?<![#\w])run_cron\.sh")          # 非注释上下文执行引用


def test_no_stale_multifactor_references_in_tracked_files():
    tracked = _git("ls-files").splitlines()
    bad = []
    for f in tracked:
        if f.startswith("stages/") or f == "README.md":
            continue  # 历史文档（调研/任务书/删码记录）
        if f == "tests/test_v7_guards.py":
            # D1 修复（tester 打回第 1 轮）：commit 后本文件被 tracked，自扫描会
            # 命中自身 4 处有意 fixture（docstring / 错误消息字面量 / gone_files
            # 删除断言），造成确定性自命中假红。此处豁免自身；对**其余**所有
            # tracked 文件的防回潮扫描不受影响（见 D1 轮 probe 证明）。
            continue
        p = PROJECT_ROOT / f
        if not p.is_file() or p.stat().st_size > 2_000_000:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if _BARE_CLI.search(line):
                bad.append(f"{f}:{i}: 裸 python -m screener 引用: {line.strip()}")
            stripped = line.strip()
            if _RUNCORN_EXEC.search(line) and not stripped.startswith("#") \
                    and not stripped.startswith("//"):
                bad.append(f"{f}:{i}: run_cron.sh 活动引用: {line.strip()}")
    assert not bad, "残留引用:\n" + "\n".join(bad)


# ---------------------------------------------------------------------------
# 2) crontab
# ---------------------------------------------------------------------------

def test_crontab_only_lake_sync_business_line():
    """stock-screener 的 crontab 业务行恰为 1 条 lake-sync（团队基础设施行
    ——hermes memory-backup/pipeline-watchdog——不属本项目，按路径过滤）。"""
    r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip("无 crontab 条目（非部署环境）")
    proj = [l for l in r.stdout.splitlines()
            if l.strip() and not l.strip().startswith("#") and "stock-screener" in l]
    assert len(proj) == 1, f"stock-screener crontab 业务行应恰为 1（lake-sync），实际 {len(proj)}: {proj}"
    assert "run_lake_sync_daily.sh" in proj[0]
    assert "run_cron" not in r.stdout, "crontab 不得含 run_cron（含注释外任何形态）"


# ---------------------------------------------------------------------------
# 3) lake/ 零改动
# ---------------------------------------------------------------------------

def test_lake_zero_diff_vs_baseline():
    out = _git("diff", BASELINE, "--", "lake/")
    assert out.strip() == "", f"lake/ 相对基线 {BASELINE} 有改动:\n{out[:2000]}"


# ---------------------------------------------------------------------------
# 4) pyproject
# ---------------------------------------------------------------------------

def test_pyproject_v7_shape():
    text = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'version = "7.0.0"' in text
    assert "martingale" in text
    assert re.search(r'packages\s*=\s*\[[^\]]*"martingale"[^\]]*\]', text)
    assert 'lake"' in text and '"screener"' in text


# ---------------------------------------------------------------------------
# 5) strategy.yaml schema 形状
# ---------------------------------------------------------------------------

def test_strategy_yaml_v7_schema_shape():
    cfg = yaml.safe_load((PROJECT_ROOT / "config" / "strategy.yaml").read_text(encoding="utf-8"))
    strat = set(cfg["strategy"].keys())
    assert strat == {"name", "initial_amount", "base_add_amount", "first_drawdown_pct",
                     "add_step_pct", "growth_ratio", "rearm_mode", "rearm_pct", "accounting"}
    assert set(cfg["universe"].keys()) == {
        "industry_whitelist_csric2", "soe_keywords", "min_total_mv_yi",
        "min_consecutive_div_years", "listing_min_trading_days",
        # v7.0.12 候选池选股门槛
        "backtest_positive", "backtest_window_years", "min_ttm_yield_pct",
        "price_min_yuan", "price_max_yuan"}
    # v7.0.13 选股时机
    assert set(cfg["timing"].keys()) == {
        "price_window_years", "price_pctile_max", "yield_min_pct",
        "below_ma250", "ma_period"}
    assert "lake" in cfg and "datasource" in cfg
    assert {"sina", "rf"} <= set(cfg["datasource"].keys())  # v7.0.18：baostock 段随渠道移除
    # 旧 12 段不得回潮
    gone = {"technical", "dividend", "industry", "fundamental", "scoring", "badges",
            "hard_filter", "data", "crosscheck", "reinvest", "canonical", "health",
            "backtest"}
    assert not (set(cfg.keys()) & gone), f"旧段回潮: {set(cfg.keys()) & gone}"


# ---------------------------------------------------------------------------
# 6) 已删模块不得回潮（文件级）
# ---------------------------------------------------------------------------

def test_deleted_artifacts_gone():
    gone_files = [
        "screener/__main__.py", "run_cron.sh", "backtest/", "scripts/backtest_martingale.py",
        "tests/smoke_cron_guard.py",
    ]
    for g in gone_files:
        p = PROJECT_ROOT / g
        assert not p.exists(), f"已删文件回潮: {g}"
