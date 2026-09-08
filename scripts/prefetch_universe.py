#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BaoStock 股票池/行业 预取子进程（fix round 2：半封禁态依赖降级）。

背景：v4 目标"日常路径不依赖 BaoStock 健康"，但 ``DataFetcher.all_stock`` /
``industry`` 在当日缓存 miss / TTL 过期时仍会 **live** 调 ``query_all_stock`` /
``query_stock_industry``。BaoStock 是 ctypes C 库，socket 在原生层，Python
``setdefaulttimeout`` 对其无效 → 半封禁态下这些查询会**无限挂起**（稳定性探测
#28–30 全超时），把整个 cron 拖到 run_cron.sh 的 45min 进程超时才失败。

本脚本作为**独立子进程**在 run_cron.sh 的交易日历守卫通过后、主运行前执行：
    timeout -k 30 180 .venv/bin/python scripts/prefetch_universe.py "$TODAY"
- login + query_all_stock(TODAY) + query_stock_industry，经 **screener 缓存层**
  （DataFetcher/DiskCache）落盘（复用现有原子写路径，幂等：同一天重复运行不重复拉取）。
- 成功 exit 0；失败/异常非零。
- **不做任何重试循环**——挂起由父级 ``timeout`` 处理（子进程被 kill 后 run_cron.sh
  导出 BS_UNIVERSE_STALE_OK=1，主运行走陈旧回退）。
- login 失败立即非零退出（不进入查询）。

隔离模式沿用已验证的 bs_one_attempt.py：把"可能挂起的 BaoStock 数据查询"关进
一个可被父进程 timeout 杀死的子进程，主流程永远不被它拖死。
"""
from __future__ import annotations

import os
import sys
from datetime import date


def _ensure_project_root() -> str:
    """把项目根加入 sys.path（脚本可被任意 CWD 调用）。返回项目根绝对路径。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)
    return root


def _resolve_cache_dir(cfg: dict, project_root: str) -> str:
    """data.cache_dir（相对路径基于项目根）→ 绝对路径。"""
    cache_dir = cfg["data"].get("cache_dir", "cache")
    if not os.path.isabs(cache_dir):
        cache_dir = os.path.join(project_root, cache_dir)
    return cache_dir


def _load_cfg(project_root: str) -> dict:
    from screener import config as cfgmod
    return cfgmod.load_config(os.path.join(project_root, "config", "strategy.yaml"))


def prefetch_universe(day: str, cache_dir: str, client=None, fetcher=None) -> int:
    """login + query_all_stock(day) + query_stock_industry → 经缓存层落盘。

    :param day: YYYY-MM-DD（query_all_stock 始终显式传 day）。
    :param cache_dir: DiskCache 目录（绝对路径）。
    :param client: 可注入的 BaoStockClient（测试用 fake）；None → 新建真实 client。
    :param fetcher: 可注入的 DataFetcher（测试用）；None → 由 client+cache 新建。
    :return: 0=成功；非零=失败/异常。**不重试**（挂起由父级 timeout 处理）。
    """
    from screener import config as cfgmod
    from screener.data.baostock_client import BaoStockClient, DataSourceError
    from screener.data.cache import DiskCache
    from screener.data.fetchers import DataFetcher

    project_root = _ensure_project_root()
    try:
        cfg = _load_cfg(project_root)
    except Exception as exc:  # noqa: BLE001 - 配置故障 → 非零退出（父级降级）
        print(f"[prefetch] 配置加载失败: {exc}", file=sys.stderr)
        return 2

    own_client = client is None
    if fetcher is None:
        if own_client:
            datac = cfgmod.data_cfg({**cfg, "data": {**cfg["data"], "cache_dir": cache_dir}})
            client = BaoStockClient(max_attempts=datac["retry_max_attempts"])
        fetcher = DataFetcher(client, DiskCache(cache_dir))

    try:
        # 1) 股票池（query_all_stock(day)）：非空 → 永久缓存落盘；空/失败 → 抛错。
        all_df = fetcher.all_stock(day)
        if len(all_df) == 0:
            print(f"[prefetch] query_all_stock({day}) 返回 0 行（数据源异常）", file=sys.stderr)
            return 3
        # 2) 行业分类（query_stock_industry）：TTL 24h，落盘。
        ind_df = fetcher.industry()
        if len(ind_df) == 0:
            print(f"[prefetch] query_stock_industry 返回 0 行（数据源异常）", file=sys.stderr)
            return 4
        print(f"[prefetch] OK allstock={len(all_df)} industry={len(ind_df)} day={day}")
        return 0
    except DataSourceError as exc:
        # login 失败 / 查询失败（重试耗尽）→ 非零退出，父级降级到陈旧回退。
        print(f"[prefetch] BaoStock 数据源失败: {exc}", file=sys.stderr)
        return 5
    except Exception as exc:  # noqa: BLE001 - 任何异常 → 非零（不重试、不吞）
        print(f"[prefetch] 预取异常: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 6
    finally:
        # 只登出本进程新建的 client（注入的 fake/外部 client 由调用方管理）。
        if own_client and client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - 登出失败不影响退出码
                pass


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    day = argv[0] if argv else date.today().isoformat()
    project_root = _ensure_project_root()
    try:
        cfg = _load_cfg(project_root)
    except Exception as exc:  # noqa: BLE001 - 配置故障 → 非零退出（父级降级）
        print(f"[prefetch] 配置加载失败: {exc}", file=sys.stderr)
        return 2
    cache_dir = _resolve_cache_dir(cfg, project_root)
    return prefetch_universe(day, cache_dir)


if __name__ == "__main__":
    sys.exit(main())
