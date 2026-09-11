# -*- coding: utf-8 -*-
"""一次性全市场稳定键迁移（TL 硬条件，报告 R1-c）。

把全部 A 股（query_all_stock 某交易日 + 前缀过滤，**含停牌**——历史数据仍需缓存）的：
- ``kline_af3_{code}``（不复权全历史，2000 起 ~ run_day；5 字段精简布局）
- ``adjfactor_{code}``（复权因子全历史，2000 起）
拉取并写入稳定键缓存。

**并行策略（实测校准）**：BaoStock 的连接是**进程级单例**（baostock 内部全局
socket），线程共享会串包/挂死 → 迁移用 **multiprocessing 多进程池**，每个 worker
独立 login/logout。实测 2 进程并发全历史拉取无服务端限速、墙钟≈最慢单股
（sh.600036 5.5s ∥ sz.000002 9.3s → wall 9.8s）。默认 3 worker，兼顾吞吐与
BaoStock 限速风险（TL 约束：串行 + sleep≥0.05s 的纪律在**每 worker 内部**保持——
worker 内每次查询后 sleep；worker 之间是独立连接，非同一 socket 的并发轰炸）。

特性：
- 失败沿用 BaoStockClient 的指数退避重试；单只失败不中断整体（断点续跑补齐）；
- **断点续跑**：稳定键天然支持——kline_af3 尾日期 == run_day → 跳过；adjfactor
  已有缓存 → 跳过。中断后重跑自动补齐缺失部分（缺哪只补哪只）。
- 进度日志 ``[PROGRESS] stage=migrate done=N total=M``（与引擎同格式，SSE 可解析）。

用法：
    .venv/bin/python -m screener.migrate --date 2026-09-04 --config config/strategy.yaml
    （--workers N 调整并行度；--sleep 每查询间隔秒，默认 0.05）
"""
from __future__ import annotations

import argparse
import logging
import multiprocessing as mp
import os
import sys
import time
from datetime import date, datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _setup_logging(log_dir: str) -> None:
    os.makedirs(log_dir, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):  # 幂等：重复 import 不叠加 handler
        root.removeHandler(h)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    fh = logging.FileHandler(os.path.join(log_dir, "migrate_stable_cache.log"), encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)


# ---------------------------------------------------------------------------
# worker 进程（每个独立 BaoStock 连接）
# ---------------------------------------------------------------------------

def _worker_loop(
    task_q: mp.Queue, result_q: mp.Queue, cfg_path: str, run_day: str, sleep_s: float
) -> None:
    """单 worker：从任务队列取 code → 拉 kline_af3 + adjfactor（各自断点续跑）。"""
    import logging as _logging

    from . import config as cfgmod
    from .data.baostock_client import BaoStockClient
    from .data.cache import DiskCache
    from .data.fetchers import DataFetcher

    _wlog = _logging.getLogger(f"screener.migrate.w{os.getpid()}")
    cfg = cfgmod.load_config(cfg_path)
    cache_dir = cfg["data"].get("cache_dir", "cache")
    if not os.path.isabs(cache_dir):
        cache_dir = os.path.join(PROJECT_ROOT, cache_dir)
    datac = cfgmod.data_cfg({**cfg, "data": {**cfg["data"], "cache_dir": cache_dir}})

    client = BaoStockClient(max_attempts=datac["retry_max_attempts"],
                            daily_quota=datac["daily_quota"])  # v5.2-p2 配额守卫（TL D5）
    fetcher = DataFetcher(client, DiskCache(cache_dir))
    fetcher.set_run_day(date.fromisoformat(run_day))

    try:
        while True:
            item = task_q.get()
            if item is None:  # 哨兵：退出
                break
            code = item
            try:
                # kline_af3：尾日期 == run_day → 跳过；否则全量拉取（1990 起，覆盖全部历史）
                if fetcher.kline_af3_last_date(code) != run_day:
                    fetcher.kline_af3_full(code, "1990-01-01", run_day)
                    time.sleep(sleep_s)
                # adjfactor：已有缓存（含空事件）→ 跳过；否则全量拉取。
                # 起点必须早于最早除权日——2000 会漏掉 1996-1997 除权的股票
                # （F 恒 1.0，af1 重建退化为不复权）。用 1990 兜底。
                if fetcher.adjfactor_history(code) is None:
                    fetcher.adjfactor_full(code, "1990-01-01", run_day)
                    time.sleep(sleep_s)
                result_q.put(("ok", code))
            except Exception as exc:  # noqa: BLE001 - 单只失败不中断（断点续跑补齐）
                _wlog.warning("迁移失败 %s: %s", code, exc)
                result_q.put(("fail", code))
    finally:
        client.close()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m screener.migrate")
    p.add_argument("--date", required=True, help="迁移基准日 YYYY-MM-DD（K线/因子拉到该日）")
    p.add_argument("--config", default="config/strategy.yaml")
    p.add_argument("--sleep", type=float, default=0.05,
                   help="worker 内每查询间隔秒（默认 0.05，BaoStock 限速纪律下限）")
    p.add_argument("--workers", type=int, default=3,
                   help="并行进程数（每个独立 BaoStock 连接；实测 2-4 无服务端限速）")
    args = p.parse_args(argv)

    try:
        run_day = datetime.strptime(args.date, "%Y-%m-%d").date()
    except ValueError:
        print(f"错误: --date 格式应为 YYYY-MM-DD，收到 {args.date!r}", file=sys.stderr)
        return 2

    from . import config as cfgmod
    from .data.baostock_client import BaoStockClient
    from .data.cache import DiskCache
    from .data.fetchers import DataFetcher

    cfg_path = args.config if os.path.isabs(args.config) else os.path.join(PROJECT_ROOT, args.config)
    try:
        cfg = cfgmod.load_config(cfg_path)
    except Exception as exc:  # noqa: BLE001
        print(f"错误: 配置加载失败: {exc}", file=sys.stderr)
        return 1

    cache_dir = cfg["data"].get("cache_dir", "cache")
    if not os.path.isabs(cache_dir):
        cache_dir = os.path.join(PROJECT_ROOT, cache_dir)
    datac = cfgmod.data_cfg({**cfg, "data": {**cfg["data"], "cache_dir": cache_dir}})
    uc = cfgmod.universe_cfg(cfg)

    _setup_logging(os.path.join(PROJECT_ROOT, "logs"))
    log = logging.getLogger("screener.migrate")
    log.info(
        "迁移启动: run_day=%s cache=%s workers=%d sleep=%.3fs",
        run_day.isoformat(), cache_dir, args.workers, args.sleep,
    )

    # 股票池：run_day（或其最近交易日）的 all_stock + A股前缀；**含停牌**
    client = BaoStockClient(max_attempts=datac["retry_max_attempts"],
                            daily_quota=datac["daily_quota"])  # v5.2-p2 配额守卫（TL D5）
    fetcher = DataFetcher(client, DiskCache(cache_dir))
    fetcher.set_run_day(run_day)
    pool_day = run_day.isoformat()
    try:
        latest = fetcher.latest_trade_date(run_day)
        if latest is not None:
            pool_day = latest.isoformat()
    except Exception as exc:  # noqa: BLE001
        log.warning("交易日历查询失败（用请求日直接拉 all_stock）: %s", exc)

    t0 = time.time()
    all_df = fetcher.all_stock(pool_day)
    codes = sorted(all_df.loc[all_df["code"].str.startswith(tuple(uc["prefixes"])), "code"].tolist())
    client.close()
    log.info("迁移范围: %d 只 A 股（pool_day=%s，含停牌）", len(codes), pool_day)

    total = len(codes)
    task_q: mp.Queue = mp.Queue()
    result_q: mp.Queue = mp.Queue()
    for c in codes:
        task_q.put(c)
    for _ in range(args.workers):
        task_q.put(None)  # 每 worker 一个退出哨兵

    procs = [
        mp.Process(
            target=_worker_loop,
            args=(task_q, result_q, cfg_path, run_day.isoformat(), args.sleep),
        )
        for _ in range(args.workers)
    ]
    for pr in procs:
        pr.start()

    n_done = 0
    n_fail = 0
    failed: list[str] = []
    last_report = 0.0
    while n_done + n_fail < total:
        kind, code = result_q.get()
        if kind == "ok":
            n_done += 1
        else:
            n_fail += 1
            failed.append(code)
        now = time.time()
        if (n_done + n_fail) % 50 == 0 or now - last_report > 300:
            last_report = now
            done = n_done + n_fail
            elapsed = now - t0
            rate = done / elapsed if elapsed > 0 else 0
            eta = (total - done) / rate if rate > 0 else 0
            log.info(
                "[PROGRESS] stage=migrate done=%d total=%d (fail=%d, %.2fs/只, ETA %.0fmin)",
                done, total, n_fail, elapsed / max(done, 1), eta / 60,
            )

    for pr in procs:
        pr.join()

    log.info(
        "迁移完成: total=%d ok=%d fail=%d | 用时 %.0fmin | 失败名单 %s",
        total, n_done, n_fail, (time.time() - t0) / 60,
        failed[:20] + (["..."] if len(failed) > 20 else []),
    )
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
