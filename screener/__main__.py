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

        log.info("输出: %s (%d 行)", csv_path, n_rows)
        log.info("输出: %s", md_path)
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
        return 1


if __name__ == "__main__":
    sys.exit(main())
