# -*- coding: utf-8 -*-
"""命令行入口：python -m screener --date YYYY-MM-DD --config config/strategy.yaml

退出码：0=成功；1=运行失败（配置/网络/数据）；2=参数错误（argparse 默认）。
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime

# 项目根 = screener 包的上一级目录（保证相对路径与 CWD 无关）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _setup_logging(log_dir: str, run_tag: str) -> None:
    os.makedirs(log_dir, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    fh = logging.FileHandler(os.path.join(log_dir, f"run_{run_tag}.log"), encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # baostock 自身会 print，这里不处理；第三方库降噪
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m screener",
        description="A股四维选股（技术面+股息率+行业排名+基本面），主数据源 BaoStock",
    )
    p.add_argument("--date", required=True, help="筛选日期 YYYY-MM-DD（非交易日自动回退到最近交易日）")
    p.add_argument("--config", default="config/strategy.yaml", help="策略配置文件路径（默认 config/strategy.yaml）")
    p.add_argument("--output-dir", default=None, help="输出目录（默认 <项目根>/output）")
    p.add_argument("--no-crosscheck", action="store_true", help="跳过腾讯实时接口交叉验证")
    return p.parse_args(argv)


def _resolve(path: str | None, default_abs: str) -> str:
    if path is None:
        return default_abs
    return path if os.path.isabs(path) else os.path.join(PROJECT_ROOT, path)


def main(argv=None) -> int:
    args = parse_args(argv)

    # 解析日期（格式错误 → argparse 风格的退出码 2）
    try:
        requested = datetime.strptime(args.date, "%Y-%m-%d").date()
    except ValueError:
        print(f"错误: --date 格式应为 YYYY-MM-DD，收到 {args.date!r}", file=sys.stderr)
        return 2

    from . import config as cfgmod
    from . import runstatus

    try:
        cfg = cfgmod.load_config(_resolve(args.config, os.path.join(PROJECT_ROOT, "config", "strategy.yaml")))
    except Exception as exc:  # noqa: BLE001
        print(f"错误: 配置加载失败: {exc}", file=sys.stderr)
        return 1

    output_dir = _resolve(args.output_dir, os.path.join(PROJECT_ROOT, "output"))
    cache_dir = cfg["data"].get("cache_dir", "cache")
    if not os.path.isabs(cache_dir):
        cache_dir = os.path.join(PROJECT_ROOT, cache_dir)
    cfg["data"]["cache_dir"] = cache_dir  # 回填，供 data_cfg 使用

    # v5.2 Phase 1：raw/canonical 单例启用开关显式注入（尊重 --config 路径覆盖；
    # 否则单例默认读仓库 config/strategy.yaml，与自定义 yaml 的 enabled=false 不一致）
    from .data import rawstore as _rawmod
    from .data import canonical as _canonmod
    _v52_enabled = bool((cfg.get("canonical") or {}).get("enabled", True))
    _rawmod.set_enabled(_v52_enabled)
    _canonmod.set_enabled(_v52_enabled)

    run_tag = requested.strftime("%Y%m%d")
    _setup_logging(os.path.join(PROJECT_ROOT, "logs"), run_tag)
    log = logging.getLogger("screener.cli")

    log.info("启动: date=%s config=%s output=%s", args.date, args.config, output_dir)
    try:
        from .report import write_csv, write_report
        from .screener import run_screener

        result = run_screener(cfg, requested, output_dir=output_dir, do_crosscheck=not args.no_crosscheck)

        os.makedirs(output_dir, exist_ok=True)
        csv_path = os.path.join(output_dir, f"result_{result.run_day.replace('-', '')}.csv")
        md_path = os.path.join(output_dir, f"report_{result.run_day.replace('-', '')}.md")
        n_rows = write_csv(result, csv_path)
        write_report(result, cfg, md_path)

        # v5.2 Phase 1：健康度 JSON sidecar（Web badge 读取；canonical.enabled=false → 无 data_health）
        _dh = getattr(result, "data_health", None)
        if isinstance(_dh, dict):
            from . import health as _healthmod
            try:
                hp = _healthmod.save_payload(output_dir, _dh)
                if hp:
                    log.info("输出: %s（数据源健康度，Web badge 用）", hp)
            except Exception:  # noqa: BLE001 — sidecar 失败不得影响主路径
                pass

        log.info("输出: %s (%d 行)", csv_path, n_rows)
        log.info("输出: %s", md_path)
        # 成功运行 → 清除同日可能残留的 failed sidecar（新结果取代旧失败），
        # 避免 Web 把一次已成功重跑仍标红。run_day 是实际交易日（可能与请求日不同）。
        runstatus.clear_failed_sidecar(output_dir, result.run_day.replace("-", ""))

        print()
        print("=" * 60)
        print(f"筛选完成 · 运行日 {result.run_day}")
        for k, v in result.funnel.items():
            print(f"  {k}: {v}")
        print(f"CSV:  {csv_path}")
        print(f"报告: {md_path}")
        print("=" * 60)
        return 0
    except Exception as exc:  # noqa: BLE001
        log.exception("运行失败")
        print(f"错误: 运行失败: {exc}", file=sys.stderr)
        # 生产路径失败守卫（9/7 缺陷修复）：**仅数据源级失败**（DataSourceError 及其
        # 子类 BaoStockError，如 BaoStock 封禁导致 query_all_stock 返回空池、登录失败、
        # K线拉取中途被黑名单）→ 写 status=failed sidecar，**不写** result/report。
        # Web 据此把该日标红"运行失败"，与合法"0 只入选"的空结果严格区分。
        #
        # 为什么只限 DataSourceError（而非所有异常）：用法错误（如 --date 未来日期被
        # _resolve_run_day 拒绝）、非数据源的运行时 bug 都不是"数据源级失败"——若也写
        # sidecar，会给一个**从未真正运行**的日期凭空造出"运行失败"记录（幻影 failed run），
        # 且污染真实 output/。这类错误仍按原行为：非零退出 + ERROR 日志，但不落 sidecar。
        from .data.baostock_client import DataSourceError

        if isinstance(exc, DataSourceError):
            try:
                run_day = getattr(exc, "run_day", None) or args.date
                sc_path = runstatus.write_failed_sidecar(
                    output_dir, requested.isoformat(), run_day, exc
                )
                log.error("已写失败 sidecar（不产出误导性空结果）: %s", sc_path)
            except Exception as sc_exc:  # noqa: BLE001 - sidecar 写入失败不得掩盖原始错误
                log.error("写失败 sidecar 出错: %s", sc_exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
