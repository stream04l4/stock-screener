# -*- coding: utf-8 -*-
"""zscore 模式基本面/分红缓存预热（与 K线迁移并行，同一 BaoStock 限速纪律）。

v2 zscore 引擎对**硬剔除后全体候选**（~4800 只）逐股拉取：
- dividend(code, 2025) / dividend(code, 2026)   # TTM 窗口 [run_day-365d, run_day] 跨两个自然年
- profit/growth/balance(code, {2023,2024,2025}, Q4)  # ROE 近3年 + Piotroski prior
- growth/balance/cashflow(code, 2025, Q4)       # 当前年报（部分与上面重合，缓存键相同自动跳过）

缓存键与 screener/data/fetchers.py 完全一致（make_cache_name 同参）→ 预热后验收运行
的 BaoStock 请求 ≈0。多进程并行（每进程独立 BaoStock 连接），sleep>=0.05s/查询，
断点续跑：已缓存键直接跳过（cache.get 命中）。

用法：.venv/bin/python -m screener.prewarm_fundamentals --date 2026-09-04 [--workers 4]
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import date, timedelta
from multiprocessing import Pool

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

log = logging.getLogger("screener.prewarm")


def _setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    log_dir = os.path.join(PROJECT_ROOT, "logs")
    os.makedirs(log_dir, exist_ok=True)
    fh = logging.FileHandler(os.path.join(log_dir, "prewarm_fundamentals.log"), encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)


def _universe_codes(run_day: date):
    """与引擎相同的股票池：query_all_stock(缓存) + A股前缀过滤 + 当日正常交易。"""
    from screener.config import load_config
    from screener.data.baostock_client import BaoStockClient
    from screener.data.cache import DiskCache
    from screener.data.fetchers import DataFetcher
    from screener.universe import build_universe

    cfg = load_config(os.path.join(PROJECT_ROOT, "config", "strategy.yaml"))
    cache = DiskCache(os.path.join(PROJECT_ROOT, "cache"))
    client = BaoStockClient()
    try:
        fetcher = DataFetcher(client, cache)
        pool, _stats = build_universe(
            fetcher, run_day.isoformat(),
            cfg["universe"]["a_share_prefixes"], cfg["universe"]["st_name_keyword"],
        )
        return sorted(pool["code"].tolist())
    finally:
        client.close()


def _annual_year(run_day: date) -> int:
    """与 screener._resolve_annual_year 同口径（不依赖网络：5月及以后=run_year-1）。"""
    y = run_day.year - 1 if run_day.month > 4 else run_day.year - 2
    return y


def _worker_init(cache_dir: str, max_attempts: int) -> None:
    global _W_CACHE, _W_CLIENT
    from screener.data.baostock_client import BaoStockClient
    from screener.data.cache import DiskCache

    _W_CACHE = DiskCache(cache_dir)
    _W_CLIENT = BaoStockClient(max_attempts=max_attempts, base_delay=0.5)


_W_CACHE = None
_W_CLIENT = None


def _warm_one(code: str, run_day_iso: str, annual_year: int) -> int:
    """预热单只股票的全部 v2 基本面/分红缓存键。返回新增查询数（命中缓存=0）。

    限速纪律（TL 约束）：**每次 BaoStock 查询后** sleep ≥0.05s（不是每股一次）。
    """
    from screener.data.fetchers import DataFetcher

    fetcher = DataFetcher(_W_CLIENT, _W_CACHE)
    fetcher.set_run_day(date.fromisoformat(run_day_iso))
    before = _W_CLIENT.request_count

    def q(fn):
        fn()
        time.sleep(0.05)  # BaoStock 限速纪律（≥0.05s/查询）

    years_div = sorted({run_day_iso[:4], str(int(run_day_iso[:4]) - 1)})
    for y in years_div:
        q(lambda yy=int(y): fetcher.dividend(code, yy))
    # profit：近3年年报Q4（ROE稳定性 + Piotroski prior），2023/2024/2025
    for y in (annual_year - 2, annual_year - 1, annual_year):
        q(lambda yy=y: fetcher.profit_data(code, yy, 4))
    # growth：仅当前年报（YOYPNI，S7 + 行业yoy排名）——与引擎 _run_zscore 完全一致
    q(lambda: fetcher.growth_data(code, annual_year, 4))
    # balance：当前 + prior（Piotroski S1/S5/S8 需资产负债表同比）
    q(lambda: fetcher.balance_data(code, annual_year, 4))
    q(lambda: fetcher.balance_data(code, annual_year - 1, 4))
    # cashflow：当前年报（S9 经营现金流）
    q(lambda: fetcher.cashflow_data(code, annual_year, 4))

    return _W_CLIENT.request_count - before


def _warm_task(args: tuple) -> int:
    """模块级任务函数（Pool 要求可 pickle；lambda 不行）。

    单只失败不中断整体（断点续跑补齐）：BaoStock 限速/黑名单期间返回 -1，
    主循环统计为 skipped，重跑本命令即可继续。
    """
    code, run_day_iso, annual_year = args
    try:
        return _warm_one(code, run_day_iso, annual_year)
    except Exception as exc:  # noqa: BLE001 - 单只失败不中断（断点续跑补齐）
        log.warning("预热失败 %s: %s", code, exc)
        return -1


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--date", required=True, help="运行日 YYYY-MM-DD")
    p.add_argument("--workers", type=int, default=4)
    args = p.parse_args()

    _setup_logging()
    run_day = date.fromisoformat(args.date)
    annual_year = _annual_year(run_day)
    cache_dir = os.path.join(PROJECT_ROOT, "cache")

    t0 = time.time()
    codes = _universe_codes(run_day)
    log.info("预热股票池: %d 只；分红年份=%s 基本面年度Q4=%s~%s；workers=%d",
             len(codes), sorted({run_day.year - 1, run_day.year}),
             annual_year - 2, annual_year, args.workers)

    fetched = 0
    skipped = 0
    done = 0
    tasks = [(c, run_day.isoformat(), annual_year) for c in codes]
    with Pool(args.workers, initializer=_worker_init,
              initargs=(cache_dir, 5)) as pool:
        try:
            for f in pool.imap_unordered(_warm_task, tasks, chunksize=8):
                done += 1
                if f < 0:
                    skipped += 1
                else:
                    fetched += f
                if done % 200 == 0 or done == len(codes):
                    rate = (time.time() - t0) / done
                    eta = rate * (len(codes) - done)
                    log.info("[PROGRESS] stage=prewarm_fundamentals done=%d total=%d "
                             "(new_queries=%d skipped=%d, %.2fs/只, ETA %.0fmin)",
                             done, len(codes), fetched, skipped, rate, eta / 60)
        except Exception:
            log.exception("预热中断（断点续跑：重跑本命令即可从缓存继续）")
            return 1

    log.info("预热完成: %d 只，新增 BaoStock 查询 %d 次，跳过(失败待补) %d 只，用时 %.0fmin",
             done, fetched, skipped, (time.time() - t0) / 60)
    return 1 if skipped else 0


if __name__ == "__main__":
    sys.exit(main())
