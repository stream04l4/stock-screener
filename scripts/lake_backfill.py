# -*- coding: utf-8 -*-
"""v6.0.2 数据湖灌数 driver（可运行 CLI：``python scripts/lake_backfill.py <sub>``）。

背景（v6.0.2 brief）：v6 backfill 是纯库无入口，TL 验收后需要实际执行初始化与
P0 灌数。本脚本 = **driver 层**：只做参数解析 + 编排 + summary 打印，
**全部抓取/限速/幂等逻辑复用现有 ingest + BackfillRunner**（不重写）：

- T1 stock_master     ← baostock_ingest.fetch_stock_basic/fetch_industry/load_t1
                        （BaoStockClient 构造即挂 QuotaGuard，~2 次调用；
                        name/is_st 由腾讯快照补——load_t1 只落 BaoStock 侧列）
- T4 dividend_events  ← em_dividend_ingest.load_dividends（cache/em_dividend_all.csv
                        静态导入全史，**零网络**）
- T7 index_daily      ← tencent_ingest.fetch_kline_ohlcv + load_t7（4 指数 × N 天）
- T2 kline_daily      ← v6.1 DEF-1 多源（source_pool：sina→tencent→tdx，窗口化近 N 天；
                        adj sina/tdx 推导 + cross_check tdx；池空回退腾讯单源逐字节不变）
- T3 valuation_daily  ← tencent_ingest.fetch_snapshot + load_t3（批量快照 50/批，
                        批间小睡；done 键同样幂等）
- history             ← P2 全史后台补（v6.0.7：腾讯 K线分页翻到 IPO + BaoStock
                        adj_factor；done 键固定 "full_history" 跨天续传；取空/失败
                        抛错不 mark_done），走 BackfillRunner（budget_per_day 门）。
                        **本批次只构建不跑**——TL 验收后由 TL 实际执行。

纪律（v6.0.2）：
- BaoStock 一律走 QuotaGuard（baostock_ingest 现有路径），不得裸调；日预算到顶
  当日停、次日续（BackfillRunner._quota_state + QuotaGuard 日期翻转）。
- 零回归红线：本脚本只 import lake/* + screener.data.*，**不改动 screener/**。
- duckdb 未装 → LakeUnavailable 友好报错退出（exit 3），不崩 traceback。
- **D-1（v6.0.3 rework）**：--db 指向 0 字节/无效库文件（duckdb 打不开）→
  LakeInvalidFile 友好报错退出（exit 3），同样不崩裸 traceback。

子命令：init / p0 / history / status（见 --help）。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

# 仓库根入 sys.path（scripts/ 下直接 python 运行）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

log = logging.getLogger("lake_backfill")

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_LAKE_UNAVAILABLE = 3
EXIT_RUNTIME = 1


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------
def _project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _cache_dir() -> str:
    return os.path.join(_project_root(), "cache")


def _today_beijing() -> str:
    from zoneinfo import ZoneInfo

    return _dt.datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")


def _open_db(db_path: Optional[str]):
    """打开写路径连接（duckdb 未装 → LakeUnavailable，main 统一友好退出）。"""
    from lake import conn as lconn

    return lconn.open(db_path or lconn.default_db_path())


def _write_lock(db_path: Optional[str]):
    """**B-4（v6.0.3）**：ingest/driver 写路径的统一串行化入口。

    为什么在 **connect 之前**持锁（而不是只包住 execute）：实测 DuckDB 对**同一文件**
    的跨进程连接是排他的——第二个进程连 ``duckdb.connect`` 都会立即抛
    "Could not set lock on file"（RW / read-only 皆然，见 smoke 实验）。若只在写语句
    周围持 flock，两个 driver 进程仍会在 connect 阶段互撞而崩。故把 **connect + 全部
    写入 + close** 都包在 LakeLock(flock) 内：并发 writer 会**阻塞等锁**（而非崩溃），
    前一个释放后干净接管——这才是"两进程并发写不冲突"的真实保证。

    - 同进程多连接 DuckDB 允许（v6.0.2 冒烟测试持有自己的 con 跨 main() 调用不受影响）；
    - 锁文件 = ``<db_path>.write.lock``，与库同目录（自定义 --db 时不落到生产 data/lake/）。
    """
    from lake.conn import LakeLock

    return LakeLock(db_path or _default_db_path())


def _default_db_path() -> str:
    from lake import conn as lconn

    return lconn.default_db_path()


def _print_summary(title: str, summary: Dict[str, Any]) -> None:
    """结构化 summary（JSON）——供 TL 核对。"""
    print(f"\n===== {title} =====")
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


def _quota_state() -> Dict[str, Any]:
    """QuotaGuard 当前状态（本地文件读，零网络）。失败 → {}（不阻断）。"""
    try:
        from screener.data.baostock_client import QuotaGuard, default_quota_path

        guard = QuotaGuard(path=default_quota_path())
        date_s, used = guard.get_state()
        return {"date": date_s, "used": used, "quota": guard.daily_quota}
    except Exception as exc:  # noqa: BLE001
        log.warning("QuotaGuard 状态读取失败: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# v6.1（Q6）：BaoStock 恢复探测——灌数启动时探一次，结果写日志+progress
# ---------------------------------------------------------------------------
def _run_bs_probe(db_path: Optional[str] = None) -> Dict[str, Any]:
    """灌数启动的 Q6 BaoStock 存活探测（p0/history/reconcile 各调一次）。

    - config ``baostock_probe_enabled=False`` → 跳过（按"死"处理，零网络——离线单测契约）。
    - env ``LAKE_MULTISOURCE=0``（测试隔离门）→ 同样跳过（与 resolve_source 一致）。
    - 探测本身 fail-fast：socket 10s 超时 + 墙钟硬预算（绝不阻塞灌数启动）；
      结果 :func:`set_baostock_alive` 进程内共享 → baostock adapter available() 门控。
    - 结果写日志 + progress 顶层 ``baostock_probe``（Web /status 可观测）。

    **progress 路径按 db_path 派生**（v6.1 隔离修复）：自定义 --db（E2E/tmp 库）→
    写到该库目录的 progress，**绝不污染生产 data/lake/backfill_progress.json**；
    缺省库 → None → load/save_progress 走生产默认路径（原行为不变）。
    """
    from lake.config import lake_cfg

    if os.environ.get("LAKE_MULTISOURCE") == "0":
        return {"enabled": False, "alive": False, "detail": "multisource off (test isolation)"}
    if not lake_cfg().get("baostock_probe_enabled", True):
        return {"enabled": False, "alive": False, "detail": "probe disabled by config"}
    from screener.data.baostock_client import probe_baostock_alive

    res = probe_baostock_alive(timeout_s=10.0)
    from lake.ingest.source_pool import set_baostock_alive

    set_baostock_alive(res.get("alive", False), res.get("detail", ""))
    log.info("Q6 BaoStock 恢复探测: alive=%s elapsed=%ss %s",
             res.get("alive"), res.get("elapsed_s"), res.get("detail"))
    # progress 落盘（Web /status 可观测）——失败不阻断灌数；路径按 db_path 派生（隔离）
    try:
        from lake.backfill import load_progress, save_progress

        prog_path = _progress_for_db(db_path)  # None=缺省库→生产默认路径；自定义→库目录
        prog = load_progress(prog_path)
        prog["baostock_probe"] = {"at": _dt.datetime.now(_dt.timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S"), "alive": bool(res.get("alive")),
            "elapsed_s": res.get("elapsed_s"), "detail": res.get("detail", "")}
        save_progress(prog, prog_path)
    except Exception as exc:  # noqa: BLE001 - progress 写失败不影响灌数
        log.warning("Q6 探测结果写 progress 失败（不阻断）: %s", exc)
    return {"enabled": True, **res}


# ---------------------------------------------------------------------------
# T1 stock_master（BaoStock ~2 次 + 腾讯快照补 name/is_st）
# ---------------------------------------------------------------------------
def run_t1(con, db_path: str, quota_before: int) -> Dict[str, Any]:
    """T1：BaoStock all_stock + 行业分类（QuotaGuard 内，~2 次调用）。

    load_t1 只落 BaoStock 侧列（name/is_st/soe_* 留 NULL）；随后用**一次**腾讯
    批量快照补 name/is_st（v5 口径：名称含 ST → is_st=1），不额外耗 BaoStock。
    """
    from lake.backfill import stop_requested as _bk_stop
    from lake.ingest import baostock_ingest as bsi
    from screener.data.baostock_client import BaoStockClient

    # v6.0.10：注入停止检查钩子——SIGTERM 后重试循环提前中断（收尾加速；
    # 依赖注入保持 screener 层零 import lake，见 baostock_client._query 注释）
    bs = BaoStockClient(stop_checker=_bk_stop)  # 构造即挂 QuotaGuard（默认路径，跨进程共享计数）
    try:
        basic_fields, basic_rows = bsi.fetch_stock_basic(bs)      # 1 次配额
        ind_fields, ind_rows = bsi.fetch_industry(bs)             # 1 次配额
    finally:
        bs.close()
    industry_map: Dict[str, Tuple[Optional[str], Optional[str]]] = \
        bsi.build_industry_map(ind_fields, ind_rows)
    n = bsi.load_t1(con, basic_rows, industry_map)

    # 腾讯快照补 name/is_st（批量 50/批，免费；失败不阻断——name 可后补）
    ts_codes = [r[0] for r in con.execute("SELECT ts_code FROM stock_master").fetchall()]
    enriched = 0
    try:
        from lake.ingest.tencent_ingest import fetch_snapshot
        from screener.data.tencent import TencentClient

        tclient = TencentClient()
        for i in range(0, len(ts_codes), 50):
            batch = ts_codes[i : i + 50]
            snaps = fetch_snapshot(tclient, batch)
            for c in batch:
                snap = snaps.get(c)
                if not snap or not snap.get("name"):
                    continue
                name = str(snap["name"])
                is_st = 1 if "ST" in name.upper() else 0
                con.execute(
                    "UPDATE stock_master SET name=?, is_st=? WHERE ts_code=?",
                    [name, is_st, c],
                )
                enriched += 1
            time.sleep(0.3)  # 批间小睡（限速纪律）
    except Exception as exc:  # noqa: BLE001 - name/is_st 补齐失败不阻断 T1 主体
        log.warning("T1 腾讯快照补 name/is_st 失败（不影响 BaoStock 侧列）: %s", exc)

    return {
        "table": "stock_master",
        "rows_written": n,
        "name_isst_enriched": enriched,
        "baostock_calls_this_step": 2,
        "note": "BaoStock 侧列=list_date/delist_date/board/industry；"
                "name/is_st 由腾讯快照补；soe_* 留 NULL（v5 规则依赖 T6，P2 补）",
    }


# ---------------------------------------------------------------------------
# T4 dividend_events（静态导入全史，零网络）
# ---------------------------------------------------------------------------
def run_t4(con, db_path: str, ts_codes: Optional[List[str]] = None) -> Dict[str, Any]:
    """T4：cache/em_dividend_all.csv 全史导入（复用 em_dividend_ingest，零网络）。

    :param ts_codes: 冒烟用过滤（None=全表）。
    """
    from lake.ingest.em_dividend_ingest import load_dividends

    t0 = time.monotonic()
    n = load_dividends(con, _cache_dir(), ts_codes=ts_codes)
    return {
        "table": "dividend_events",
        "rows_written": n,
        "scope": ",".join(ts_codes) if ts_codes else "full_history(1991→今)",
        "network": "none(local static csv)",
        "elapsed_s": round(time.monotonic() - t0, 2),
    }


# ---------------------------------------------------------------------------
# T7 index_daily（4 指数 × N 天）
# ---------------------------------------------------------------------------
def run_t7(con, db_path: str, days: int, runner, force: bool = False) -> Dict[str, Any]:
    """T7：四指数近 N 交易日（复用 tencent_ingest.load_t7；amount 缺→NULL）。

    **B-3（v6.0.3）幂等**：原实现每次 p0 都无条件重取 4 指数（腾讯免费接口，纯效率
    问题）。现走 BackfillRunner done 键——done 粒度 = ``(index_daily, index_code,
    "days=<N>")``（按请求窗口而非末日期：同参重跑跳过、改 --days 视为新任务重取；
    load_t7 是 upsert，即便重取也幂等不重复）。``force=True``（--force）→ 清空本表
    done 键强制重取。零网络断言靠 fake client 计数（见 test_lake_v603）。
    """
    from lake.backfill import Task
    from lake.config import crosscheck_threshold, lake_cfg
    from lake.ingest import source_pool as sp
    from lake.ingest.tencent_ingest import INDEX_CODES, load_t7
    from screener.data.tencent import TencentClient

    # n = 交易日数 + 缓冲（节假日/停牌不占行，多取一点保证覆盖 N 个交易日）
    tclient = TencentClient()
    period = f"days={days}"
    tasks = [Task(priority=3, table="index_daily", ts_code=ic,
                  period_or_date=period, tier="P0") for ic in INDEX_CODES]

    if force:
        # --force：清空 index_daily 的 done 键（仅本表，不碰 T2/T3 进度）→ 强制重取
        runner._done = {k for k in runner._done if k[0] != "index_daily"}
        runner.progress["done"] = [list(k) for k in sorted(runner._done)]

    t7_close_pct = crosscheck_threshold("t7_close_pct", 0.3)

    def worker(task: Task) -> None:
        # v6.1：T7 腾讯主源（现状稳定）+ tdx amount 补充 + close 交叉校验（0.3%）
        ic = task.ts_code
        srcs = sp.resolve_source("index_daily", "ohlcv")
        kl, source = None, "tencent"
        if srcs:
            for ad in srcs:
                try:
                    rows = ad.fetch_index_kline(ic, n=days + 30)
                except Exception as exc:  # noqa: BLE001 - 该源失败 → 下一源
                    log.warning("T7 %s source=%s 取数失败，回退下一源: %s", ic, ad.name, exc)
                    continue
                if rows:
                    kl, source = rows, ad.name
                    break
        else:
            # 池全不可用（离线单测/降级）→ 既有直连路径（行为逐字节不变，零回归兜底）
            from lake.ingest.tencent_ingest import fetch_kline_ohlcv

            kl = fetch_kline_ohlcv(tclient, ic, n=days + 30)
        # tdx amount 补充（腾讯指数行 amount 缺→NULL；tdx 提供则补上）
        tdx_ad = sp.get_adapter("tdx") if lake_cfg().get("tdx_enabled", True) else None
        conflict = None
        tdx_rows: List[Dict[str, Any]] = []
        if kl and tdx_ad is not None:
            try:
                if tdx_ad.available():
                    tdx_rows = tdx_ad.fetch_index_kline(ic, n=days + 30) or []
            except Exception as exc:  # noqa: BLE001 - amount 补充失败不阻断（留 NULL）
                log.warning("T7 %s tdx amount 补充失败（留 NULL）: %s", ic, exc)
        if kl and tdx_rows:
            tdx_map = {r["date"]: r for r in tdx_rows}
            for r in kl:
                tr = tdx_map.get(r["date"])
                if tr is not None and r.get("amount") is None and tr.get("amount") is not None:
                    r["amount"] = tr["amount"]  # 补缺口（不覆盖腾讯已有值）
            # close 交叉校验：腾讯 vs tdx >0.3% → conflict_src（不阻断，取主源值）
            conflict = sp.cross_check_kline(kl, source, tdx_rows, "tdx",
                                            close_pct=t7_close_pct)
        if kl:
            load_t7(con, ic, kl, source=source, conflict_src=conflict)
        time.sleep(0.3)  # 限速纪律（指数间小睡）

    stats = runner.run(tasks, worker)
    per_index: Dict[str, Any] = {}
    for ic in INDEX_CODES:
        if runner.is_done(Task(priority=3, table="index_daily", ts_code=ic,
                               period_or_date=period, tier="P0")):
            last_row = con.execute(
                "SELECT date, close FROM index_daily WHERE index_code=? "
                "ORDER BY date DESC LIMIT 1", [ic]).fetchone()
            per_index[ic] = {
                "rows": None if not last_row else int(con.execute(
                    "SELECT COUNT(*) FROM index_daily WHERE index_code=?", [ic]).fetchone()[0]),
                "written": 0,  # 已 done → 本次未重取（幂等跳过）
                "last_date": str(last_row[0]) if last_row else None,
                "last_close": float(last_row[1]) if last_row and last_row[1] is not None else None,
            }
        else:
            per_index[ic] = {"rows": 0, "written": 0, "last_date": None,
                             "last_close": None}
    return {"table": "index_daily", "indices": per_index, "requested_days": days,
            "skipped_done": stats.get("skipped_done", 0),
            "processed": stats.get("processed", 0)}


# ---------------------------------------------------------------------------
# T2/T3 全市场（逐股 K线 + 批量快照；BackfillRunner done 键 → 中断续跑）
# ---------------------------------------------------------------------------
def _universe_codes(con) -> List[str]:
    """T2/T3 股票全集：stock_master 中 type=1 已上市股票（p0 T1 已灌）。"""
    rows = con.execute(
        "SELECT ts_code FROM stock_master WHERE delist_date IS NULL OR delist_date > ? "
        "ORDER BY ts_code",
        [_today_beijing()],
    ).fetchall()
    return [r[0] for r in rows]


def run_t2(con, db_path: str, days: int, codes: Optional[List[str]],
           runner) -> Dict[str, Any]:
    """T2：K线逐股近 N 天增量（v6.1 DEF-1 修复：多源资源池，镜像 run_history worker）。

    **DEF-1 根因**：旧实现硬编码腾讯单源（fetch_kline_ohlcv + load_t2(adj_map=None)），
    对已 history-done 的股执行 p0 会用 source=tencent/amount=NULL/adj_factor=NULL
    覆盖多源写入的 sina 行，且 upsert 残留陈旧 conflict_src——违反 Q1 权威性不变式
    （tencent=1 覆盖 sina=0）。现改为与 run_history 相同的多源逻辑（**窗口化**）：

    - ``ohlcv_srcs = resolve_source("kline_daily","ohlcv_amount")``（config 序
      sina→tencent→tdx）、``adj_srcs = resolve_source(...,"adj_factor")``（sina→tdx→baostock）；
      按 Q1 优先级取第一个 available() 且成功的源（break）。
    - **窗口化**：p0 是"近 N 天增量"，非全史重写——``window_start = (今日北京时间 -
      (days+30) 自然日)``；对每个源 ``fetch_kline(code, start=window_start)``，取数后
      **只 upsert 落在窗口内的行**（更早历史行丢弃，不重写旧数据）。sina adapter 忽略
      start 恒返全史 → 取其尾部窗口即可（brief 明示可接受，不改 adapter）；tencent/tdx
      原生支持窗口。
    - adj_factor：主源自带推导值优先；否则按 adj_srcs 补取（同 history）。
    - cross_check：采用 sina 主源时用 tdx 最近窗口轻量验证（close/amount/末因子，
      阈值 config 集中）→ 分歧写 conflict_src（不阻断）；单源成功 → NULL。
    - **限速红线保持**：sina ≥1s/股、tdx ≥0.5s/股（adapter 内 RateLimiter 已含，勿绕过）；
      worker 末尾保留 time.sleep(0.3)。
    - **取空/全源失败 → worker 抛错不 mark_done**（防 done 键毒化，同 history）。

    legacy 兜底（零回归红线）：池全不可用（LAKE_MULTISOURCE=0 / config 源开关全关）
    → 既有腾讯单源路径逐字节不变（离线单测 conftest autouse 置 0 走此路径、零网络）。
    """
    from lake.backfill import Task
    from lake.config import crosscheck_threshold, lake_cfg
    from lake.ingest import source_pool as sp
    from lake.ingest.tencent_ingest import fetch_kline_ohlcv, load_t2
    from screener.data.tencent import TencentClient

    codes = codes or _universe_codes(con)
    tasks = [Task(priority=3, table="kline_daily", ts_code=c,
                  period_or_date=str(days), tier="P0") for c in codes]
    tclient = TencentClient()
    stats: Dict[str, Any] = {"codes_requested": len(codes)}

    # v6.1 DEF-1：多源池（门控同 run_history：LAKE_MULTISOURCE=0 / 源开关全关 → []）
    ohlcv_srcs = sp.resolve_source("kline_daily", "ohlcv_amount")
    adj_srcs = sp.resolve_source("kline_daily", "adj_factor")
    t2_close_pct = crosscheck_threshold("t2_close_pct", 0.5)
    t2_amount_pct = crosscheck_threshold("t2_amount_pct", 2.0)
    t2_af_pct = crosscheck_threshold("t2_adj_factor_pct", 0.5)
    # 增量窗口：近 (days+30) 自然日（p0 只覆盖该窗口，更早历史行不重写）
    window_start = (_dt.date.today() - _dt.timedelta(days=days + 30)).isoformat()

    def worker(task: Task) -> None:
        code = task.ts_code
        if not ohlcv_srcs and not adj_srcs:
            # ---- legacy 路径（池全不可用：离线单测/新源全降级）——v6.0.x 行为逐字节不变 ----
            kl = fetch_kline_ohlcv(tclient, code, n=days + 30)
            if kl:
                load_t2(con, code, kl, adj_map=None)
            time.sleep(0.3)  # brief 限速红线：≥0.3s/股
            return

        # ---- v6.1 DEF-1 多源路径（镜像 run_history，窗口化）----
        kl: Optional[List[Dict[str, Any]]] = None
        source = "tencent"
        adj_map: Optional[Dict[str, float]] = None
        for ad in ohlcv_srcs:
            try:
                res = ad.fetch_kline(code, start=window_start)  # 窗口（sina 忽略→全史取尾部）
            except Exception as exc:  # noqa: BLE001 - 该源失败 → 下一源（fallback）
                log.warning("T2 %s source=%s 取数失败，回退下一源: %s", code, ad.name, exc)
                continue
            if res and res.get("ohlcv"):
                kl = res["ohlcv"]
                source = ad.name
                adj_map = res.get("adj_factor")
                break
        if not kl:
            raise RuntimeError(f"T2 增量 K线所有源均失败 {code}（不标 done，下轮重试）")

        # 窗口化：只 upsert 落在最近 (days+30) 自然日内的行（更早历史丢弃，不重写旧数据）
        kl = [r for r in kl if str(r["date"]) >= window_start]
        if not kl:
            raise RuntimeError(f"T2 增量 K线窗口内无数据 {code}（不标 done，下轮重试）")

        # adj_factor：主源自带推导值优先；否则按 adj 优先级补取（同 history）
        if not adj_map:
            for ad in adj_srcs:
                try:
                    m = ad.fetch_adj_factor(code, window_start, _today_beijing())
                except Exception as exc:  # noqa: BLE001 - 该源失败 → 下一源
                    log.warning("T2 %s adj source=%s 失败，回退下一源: %s", code, ad.name, exc)
                    continue
                if m:
                    adj_map = m
                    break

        # cross_check：采用 sina 主源 → tdx 最近窗口轻量验证（close/amount/末因子）
        conflict_src: Optional[str] = None
        if source == "sina":
            tdx_ad = sp.get_adapter("tdx") if lake_cfg().get("tdx_enabled", True) else None
            if tdx_ad is not None:
                try:
                    if tdx_ad.available():
                        vres = tdx_ad.fetch_kline(code, start=window_start, end=None)
                        vrows = (vres or {}).get("ohlcv") or []
                        if vrows:
                            conflict_src = sp.cross_check_kline(
                                kl, "sina", vrows, "tdx",
                                close_pct=t2_close_pct, amount_pct=t2_amount_pct)
                            # 末因子交叉校验（af 误差累积进 hfq/qfq view，阈值从严）
                            if adj_map and (vres or {}).get("adj_factor"):
                                af_conf = sp.cross_check_adj_factor(
                                    adj_map, "sina", vres["adj_factor"], "tdx", pct=t2_af_pct)
                                conflict_src = _merge_conflict(conflict_src, af_conf)
                except Exception as exc:  # noqa: BLE001 - 验证失败不阻断（conflict=NULL）
                    log.warning("T2 %s tdx 交叉校验失败（不阻断）: %s", code, exc)

        load_t2(con, code, kl, adj_map=adj_map, source=source,
                conflict_src=conflict_src, volume_is_shares=(source != "tencent"))
        time.sleep(0.3)  # brief 限速红线：≥0.3s/股（保留）

    stats.update(runner.run(tasks, worker))
    return {"table": "kline_daily", **stats}


def run_t3(con, db_path: str, codes: Optional[List[str]], runner,
           as_of: str) -> Dict[str, Any]:
    """T3：腾讯批量快照（50/批，批间小睡）→ valuation_daily。

    快照是"当前时点"数据——as_of=今日；done 键 (valuation_daily, ts_code, as_of)
    → 同日重跑跳过已灌股（中断续跑），次日新 as_of 自动重新覆盖。
    """
    from lake.backfill import Task
    from lake.ingest.tencent_ingest import fetch_snapshot, load_t3
    from screener.data.tencent import TencentClient

    codes = codes or _universe_codes(con)
    tasks = [Task(priority=3, table="valuation_daily", ts_code=c,
                  period_or_date=as_of, tier="P0") for c in codes]
    tclient = TencentClient()
    stats: Dict[str, Any] = {"codes_requested": len(codes), "as_of": as_of}

    def worker(task: Task) -> None:
        # 批量快照：50/批（TencentClient.fetch 内部已分批），批间小睡
        snaps = fetch_snapshot(tclient, [task.ts_code])
        snap = snaps.get(task.ts_code)
        if snap:
            load_t3(con, task.ts_code, snap, as_of)
        time.sleep(0.3)

    stats.update(runner.run(tasks, worker))
    return {"table": "valuation_daily", **stats}


def _progress_for_db(db_path: Optional[str]) -> Optional[str]:
    """**B-2（v6.0.3）**：progress 文件与库同目录派生；缺省库回退原路径。

    - 自定义 --db /tmp/x.db → /tmp/backfill_progress.json（不再落到生产 data/lake/）。
    - 缺省库 data/lake/lake.duckdb → **返回 None**，让 BackfillRunner 走 _progress_path()
      （= data/lake/backfill_progress.json，与原硬编码逐字节一致；且保留测试对
      _progress_path 的 monkeypatch 注入能力——v6.0.2 冒烟用例依赖它）。

    为什么缺省库不直接返回派生路径：派生值与 _progress_path() 相同，但显式传参会绕过
    测试对 _progress_path 的 patch（导致 Run1/Run2 读到不同进度文件、续跑失效）。

    v6.0.9：判定逻辑收敛到 :func:`lake.backfill.progress_path_for_db`（BackfillRunner
    构造器同用一份口径——自定义 --db 不显式传 progress_path 时 runner 也落到库目录）；
    本函数保留为薄包装，driver 侧调用点与既有测试契约不变。
    """
    from lake import backfill as lb

    return lb.progress_path_for_db(db_path)


def run_p0(con, db_path: str, days: int, codes: Optional[List[str]],
           skip_t1: bool, t4_scope: Optional[List[str]], force: bool = False) -> Dict[str, Any]:
    """p0 编排：T1 → T4 → T7 → T2 → T3（当前数据优先，Joel"先灌当前"）。

    :param codes: 冒烟股票子集（None=stock_master 全集）。
    :param skip_t1: 跳过 BaoStock T1（重跑/冒烟用；T2/T3 universe 依赖已有 stock_master）。
    :param t4_scope: T4 过滤（None=全史全表）。
    :param force: **B-3** 强制重取 T7 指数（清空 index_daily done 键）。
    """
    quota_before = _quota_state().get("used", 0)
    from lake.backfill import BackfillRunner

    # B-2：coverage 改走 runner 自身 db_path（见 BackfillRunner._coverage_conn）。
    # ⚠️ progress 路径**不**在此派生——保持 _progress_path()（可被测试 monkeypatch +
    # 缺省行为逐字节不变）；v6.0.2 冒烟续跑用例依赖对 _progress_path 的注入，若此处
    # 按 db_path 派生会绕过 patch 导致 Run1/Run2 读不同进度文件、续跑失效。
    # status 报表的 progress 路径由 run_status 按 db_path 派生（brief B-2 明指的硬编码点）。
    runner = BackfillRunner(db_path=db_path)
    steps: Dict[str, Any] = {}

    if not skip_t1:
        print("[p0] T1 stock_master（BaoStock ~2 次，QuotaGuard 内）...")
        steps["t1"] = run_t1(con, db_path, quota_before)
    else:
        steps["t1"] = {"skipped": True}

    print("[p0] T4 dividend_events（本地静态 csv，零网络）...")
    steps["t4"] = run_t4(con, db_path, ts_codes=t4_scope)

    print(f"[p0] T7 index_daily（4 指数 × ~{days} 交易日{'；--force 强制重取' if force else '；done 键幂等'}）...")
    steps["t7"] = run_t7(con, db_path, days, runner, force=force)

    as_of = _today_beijing()
    print(f"[p0] T2 kline_daily（腾讯 K线，{len(codes or []) or 'universe'} 只 × {days} 天，"
          "限速 ≥0.3s/股，done 键续跑）...")
    steps["t2"] = run_t2(con, db_path, days, codes, runner)

    print(f"[p0] T3 valuation_daily（腾讯快照 as_of={as_of}，批间小睡，done 键续跑）...")
    steps["t3"] = run_t3(con, db_path, codes, runner, as_of)

    quota_after = _quota_state().get("used", 0)
    return {
        "sub": "p0",
        "db_path": db_path,
        "days": days,
        "codes_scope": ",".join(codes) if codes else "stock_master_full_universe",
        "steps": steps,
        "baostock_quota": {
            "before": quota_before,
            "after": quota_after,
            "consumed": max(0, quota_after - quota_before),
        },
    }


# ---------------------------------------------------------------------------
# history（P2 全史后台补——v6.1 多源资源池）
# ---------------------------------------------------------------------------
def run_history(con, db_path: str, codes: Optional[List[str]],
                start_date: str, end_date: str) -> Dict[str, Any]:
    """P2 全史（v6.1 多源资源池，Q1 拍板序）。

    走 BackfillRunner（budget_per_day 门 + done 键断点续传——**零改动**）。
    TL 验收后由 TL 实际执行（后台长跑，日预算到顶当日停、次日续）。

    **v6.1 worker 新逻辑**（brief §D；worker 粒度保持按股）：
      ``for src in [sina, tencent, tdx]: fetch_kline+adj → cross_check(≥2源时)
      → upsert(source=实际采用源, conflict_src=分歧摘要或NULL) → break``
    - OHLCV/amount 主源=**新浪** akshare stock_zh_a_daily（全史一次拉全、含 amount、
      volume=股），fallback=腾讯(分页 n≤2000)→tdx；
    - adj_factor 主源=**新浪 hfq÷raw 推导**（raw+hfq 两次调用一次拿齐），
      fallback=tdx hfq÷raw → BaoStock(Q6 探测存活时)；
    - cross_check：采用新浪主源时，用 tdx 最近窗口（raw+hfq）做轻量验证
      （close/amount/末因子，阈值 config 集中）→ 分歧写 conflict_src（不阻断）。
      单源成功 → conflict_src=NULL。
    - **池全不可用（离线单测/新源全降级）→ 回退既有路径**（腾讯全史 + BaoStock adj，
      v6.0.7 行为逐字节不变——零回归兜底）。

    v6.0.7 修复保留：done 键 ``period_or_date="full_history"``（固定字符串，跨天续传）；
    取空/失败 → worker 抛错**不 mark_done**（杜绝 done 键毒化）。

    **Q6**：启动时探一次 BaoStock（10s socket 超时判活；结果写日志+progress）——
    存活则参与 adj fallback/交叉校验，死亡则自动跳过（不 crash、不阻塞）。
    """
    from lake.backfill import BackfillRunner, Task, stop_requested as _bk_stop
    from lake.config import crosscheck_threshold, lake_cfg
    from lake.ingest import source_pool as sp
    from lake.ingest.tencent_ingest import load_t2
    from screener.data.baostock_client import BaoStockClient
    from screener.data.tencent import TencentClient

    # v6.1 Q6：灌数启动探一次 BaoStock（config 关/单测门控 → 跳过，零网络）；
    # progress 落盘按 db_path 派生（自定义 --db → 库目录，不污染生产 progress）
    probe_res = _run_bs_probe(db_path)

    codes = codes or _universe_codes(con)
    # v6.0.7：done 键稳定化——固定 "full_history"（不含日期），跨天断点续传有效
    tasks = [Task(priority=2, table="kline_history", ts_code=c,
                  period_or_date="full_history", tier="P2") for c in codes]
    # B-2：coverage 走 runner 自身 db_path（与 run_p0 一致）
    runner = BackfillRunner(db_path=db_path)  # budget_per_day 门：到顶当日停（state=blocked_quota）

    # v6.0.10：注入停止检查钩子——SIGTERM 后 BaoStock 重试退避提前中断（收尾加速；
    # 依赖注入保持 screener 层零 import lake，见 baostock_client._query 注释）。
    # 多源模式下本源仅 legacy 回退路径使用（构造不登录、零网络；用不到则零成本）。
    bs = BaoStockClient(stop_checker=_bk_stop)     # QuotaGuard 内；adj_factor 1 次/股
    tclient = TencentClient()
    stats: Dict[str, Any] = {"codes_requested": len(codes),
                             "start_date": start_date, "end_date": end_date}

    # 多源池（门控：单测 LAKE_MULTISOURCE=0 / config 源开关全关 → [] → legacy 路径）
    ohlcv_srcs = sp.resolve_source("kline_daily", "ohlcv_amount")
    adj_srcs = sp.resolve_source("kline_daily", "adj_factor")
    t2_close_pct = crosscheck_threshold("t2_close_pct", 0.5)
    t2_amount_pct = crosscheck_threshold("t2_amount_pct", 2.0)
    t2_af_pct = crosscheck_threshold("t2_adj_factor_pct", 0.5)
    # tdx 轻量验证窗口（最近 ~90 自然日≈60 交易日——成本可控的交叉校验，见假设记录）
    verify_start = (_dt.date.today() - _dt.timedelta(days=90)).isoformat()

    def worker(task: Task) -> None:
        code = task.ts_code
        if not ohlcv_srcs and not adj_srcs:
            # ---- legacy 路径（池全不可用：离线单测/新源全降级）——v6.0.7 行为逐字节不变 ----
            from lake.ingest import baostock_ingest as bsi
            from lake.ingest.tencent_ingest import fetch_kline_full_history

            kl = fetch_kline_full_history(tclient, code)
            if not kl:
                # 防御：fetch_kline_full_history 契约上取空即抛错，这里双保险——
                # 绝不在无数据时 mark_done（旧 done 键毒化根因）
                raise RuntimeError(f"腾讯K线全史为空 {code}（不标 done，下轮重试）")
            fields, adj_rows = bsi.fetch_adjust_factor(bs, code, start_date, end_date)
            adj_map = bsi.load_t2_adj_factor(con, code, adj_rows)
            load_t2(con, code, kl, adj_map=adj_map)
            time.sleep(0.3)
            return

        # ---- v6.1 多源路径：按 Q1 优先级取第一个成功源（break）----
        kl: Optional[List[Dict[str, Any]]] = None
        source = "tencent"
        adj_map: Optional[Dict[str, float]] = None
        for ad in ohlcv_srcs:
            try:
                res = ad.fetch_kline(code)  # 全史（start/end=None）
            except Exception as exc:  # noqa: BLE001 - 该源失败 → 下一源（fallback）
                log.warning("T2 %s source=%s 取数失败，回退下一源: %s", code, ad.name, exc)
                continue
            if res and res.get("ohlcv"):
                kl = res["ohlcv"]
                source = ad.name
                adj_map = res.get("adj_factor")
                break
        if not kl:
            raise RuntimeError(f"T2 全史 K线所有源均失败 {code}（不标 done，下轮重试）")

        # adj_factor：主源自带推导值（sina/tdx hfq÷raw）优先；否则按 adj 优先级补取
        if not adj_map:
            for ad in adj_srcs:
                try:
                    m = ad.fetch_adj_factor(code, start_date, end_date)
                except Exception as exc:  # noqa: BLE001 - 该源失败 → 下一源
                    log.warning("T2 %s adj source=%s 失败，回退下一源: %s", code, ad.name, exc)
                    continue
                if m:
                    adj_map = m
                    break

        # cross_check（≥2源时）：采用新浪主源 → tdx 最近窗口轻量验证（close/amount/末因子）
        conflict_src: Optional[str] = None
        if source == "sina":
            tdx_ad = sp.get_adapter("tdx") if lake_cfg().get("tdx_enabled", True) else None
            if tdx_ad is not None:
                try:
                    if tdx_ad.available():
                        vres = tdx_ad.fetch_kline(code, start=verify_start, end=None)
                        vrows = (vres or {}).get("ohlcv") or []
                        if vrows:
                            conflict_src = sp.cross_check_kline(
                                kl, "sina", vrows, "tdx",
                                close_pct=t2_close_pct, amount_pct=t2_amount_pct)
                            # 末因子交叉校验（af 误差累积进 hfq/qfq view，阈值从严）
                            if adj_map and (vres or {}).get("adj_factor"):
                                af_conf = sp.cross_check_adj_factor(
                                    adj_map, "sina", vres["adj_factor"], "tdx", pct=t2_af_pct)
                                conflict_src = _merge_conflict(conflict_src, af_conf)
                except Exception as exc:  # noqa: BLE001 - 验证失败不阻断（conflict=NULL）
                    log.warning("T2 %s tdx 交叉校验失败（不阻断）: %s", code, exc)

        load_t2(con, code, kl, adj_map=adj_map, source=source,
                conflict_src=conflict_src, volume_is_shares=(source != "tencent"))
        time.sleep(0.3)

    stats.update(runner.run(tasks, worker))
    bs.close()
    return {"sub": "history", **stats, "baostock_probe": probe_res,
            "multisource": bool(ohlcv_srcs or adj_srcs),
            "ohlcv_sources": [a.name for a in ohlcv_srcs],
            "adj_sources": [a.name for a in adj_srcs]}


def _merge_conflict(a: Optional[str], b: Optional[str]) -> Optional[str]:
    """合并两段 conflict_src 摘要（≤256B；去重、截断）。"""
    if not a:
        return b
    if not b:
        return a
    merged = f"{a};{b}"
    return merged[:256]


# ---------------------------------------------------------------------------
# T5 fundamentals_quarterly（v6.1：adata F10 主源 + BaoStock 交叉校验）
# ---------------------------------------------------------------------------
def run_t5(con, db_path: str, codes: Optional[List[str]], runner) -> Dict[str, Any]:
    """T5 基本面（v6.1 多源，brief §D）。

    - **adata F10 主源**（Q4：仅 fetch_f10；全报告期 PIT）→ load_t5 upsert。
    - **BaoStock 探测存活时交叉校验**（最近 4 季；>1pp 记 conflict_src，不阻断）。
      BaoStock 死（Q6 探测 False）→ 零调用、conflict_src=NULL（单源）。
    - done 键 (fundamentals_quarterly, ts_code, "f10_full")——固定字符串跨天续传。
    - adata 不可用/取空 → worker 抛错**不 mark_done**（下轮重试，防 done 键毒化）。
    """
    from lake.backfill import Task
    from lake.config import crosscheck_threshold, lake_cfg
    from lake.ingest import source_pool as sp
    from lake.ingest.adata_f10_adapter import load_t5

    codes = codes or _universe_codes(con)
    tasks = [Task(priority=2, table="fundamentals_quarterly", ts_code=c,
                  period_or_date="f10_full", tier="P2") for c in codes]
    stats: Dict[str, Any] = {"codes_requested": len(codes)}
    t5_pp = crosscheck_threshold("t5_pp", 1.0)

    def worker(task: Task) -> None:
        code = task.ts_code
        # adata F10 主源（Q4 硬编码边界：仅 fetch_f10）
        ad = sp.get_adapter("adata_f10")
        if ad is None or not lake_cfg().get("adata_f10_enabled", True) or not ad.available():
            raise RuntimeError(f"T5 adata F10 不可用 {code}（不标 done，下轮重试）")
        recs = ad.fetch_f10(code)
        if not recs:
            raise RuntimeError(f"T5 adata F10 取空 {code}（不标 done，下轮重试）")
        # BaoStock 交叉校验（仅 Q6 探测存活时；>1pp 记 conflict_src）
        conflict_src: Optional[str] = None
        bs_ad = sp.get_adapter("baostock")
        if bs_ad is not None and bs_ad.available():
            try:
                bs_recs = bs_ad.fetch_f10(code)  # 最近 4 季（配额内）
                if bs_recs:
                    conflict_src = sp.cross_check_f10(recs, "adata_f10",
                                                      bs_recs, "baostock", pp=t5_pp)
            except Exception as exc:  # noqa: BLE001 - 校验失败不阻断（取主源值）
                log.warning("T5 %s BaoStock 交叉校验失败（不阻断）: %s", code, exc)
        load_t5(con, code, recs, source="adata_f10", conflict_src=conflict_src)
        time.sleep(0.3)

    stats.update(runner.run(tasks, worker))
    return {"table": "fundamentals_quarterly", **stats}


# ---------------------------------------------------------------------------
# reconcile（v6.1 --reconcile：仅跨源校验补 conflict_src，不重取主源数据）
# ---------------------------------------------------------------------------
def run_reconcile(con, db_path: str, codes: Optional[List[str]],
                  start_date: str, end_date: str) -> Dict[str, Any]:
    """--reconcile：对**已灌数据**补 conflict_src（brief §D）。

    语义："仅跑跨源校验不重取"——
    - **不重取/不覆盖主源 OHLCV/amount/adj_factor**（只读 DB 现有行）；
    - 取**次源**（tdx，最近窗口）与 DB 行比对 → 超阈日期 ``UPDATE conflict_src``
      （只写审计列，OHLCV 原值逐字节不动）；
    - T2：kline_daily close(0.5%)/amount(2%) + 末因子 adj(0.5%)；T7：index_daily close(0.3%)。
    - tdx 不可用 → 零更新（友好返回，不报错）。

    :return: {table: {rows_compared, rows_conflicted, updated}} 汇总。
    """
    from lake.config import crosscheck_threshold, lake_cfg
    from lake.ingest import source_pool as sp

    t2_close_pct = crosscheck_threshold("t2_close_pct", 0.5)
    t2_amount_pct = crosscheck_threshold("t2_amount_pct", 2.0)
    t2_af_pct = crosscheck_threshold("t2_adj_factor_pct", 0.5)
    t7_close_pct = crosscheck_threshold("t7_close_pct", 0.3)

    tdx_ad = sp.get_adapter("tdx") if lake_cfg().get("tdx_enabled", True) else None
    if tdx_ad is None or not tdx_ad.available():
        return {"sub": "reconcile", "skipped": "tdx unavailable（零更新）"}

    result: Dict[str, Any] = {"sub": "reconcile"}

    # ---- T2 kline_daily ----
    if codes:
        t2_codes = [c for c in codes if "." in c]
    else:
        t2_codes = [r[0] for r in con.execute(
            "SELECT DISTINCT ts_code FROM kline_daily ORDER BY ts_code").fetchall()]
    t2_stat = {"rows_compared": 0, "rows_conflicted": 0, "updated": 0}
    for code in t2_codes:
        db_rows = con.execute(
            "SELECT date, close, amount, adj_factor FROM kline_daily "
            "WHERE ts_code=? ORDER BY date", [code]).fetchall()
        if not db_rows:
            continue
        try:
            vres = tdx_ad.fetch_kline(code)  # 全史（tdx 一次拿齐 raw+hfq）
        except Exception as exc:  # noqa: BLE001 - 该股校验失败跳过（不阻断整轮）
            log.warning("reconcile T2 %s tdx 取数失败，跳过: %s", code, exc)
            continue
        vrows = (vres or {}).get("ohlcv") or []
        if not vrows:
            continue
        vmap = {r["date"]: r for r in vrows}
        conflicted_dates: List[Tuple[str, str]] = []
        for d, close, amount, af in db_rows:
            vs = vmap.get(str(d))
            if vs is None:
                continue
            t2_stat["rows_compared"] += 1
            parts: List[str] = []
            if close is not None and vs.get("close") is not None and close != 0:
                if sp._rel_diff_pct(close, vs["close"]) > t2_close_pct:
                    parts.append(f"close:sina:{sp._fmt_num(close)}|tdx:{sp._fmt_num(vs['close'])}")
            if amount is not None and vs.get("amount") is not None and amount != 0:
                if sp._rel_diff_pct(amount, vs["amount"]) > t2_amount_pct:
                    parts.append(f"amount:sina:{sp._fmt_num(amount)}|tdx:{sp._fmt_num(vs['amount'])}")
            if parts:
                conflicted_dates.append((str(d), ";".join(parts)[:256]))
        # 末因子 adj 校验（DB 最新非空 af vs tdx 推导末因子）
        vadj = (vres or {}).get("adj_factor") or {}
        if vadj:
            last_af_row = con.execute(
                "SELECT date, adj_factor FROM kline_daily WHERE ts_code=? AND adj_factor IS NOT NULL "
                "ORDER BY date DESC LIMIT 1", [code]).fetchone()
            if last_af_row and str(last_af_row[0]) in vadj:
                db_af, tdx_af = float(last_af_row[1]), float(vadj[str(last_af_row[0])])
                if db_af != 0 and sp._rel_diff_pct(db_af, tdx_af) > t2_af_pct:
                    conflicted_dates.append(
                        (str(last_af_row[0]),
                         f"adj:sina:{sp._fmt_num(db_af)}|tdx:{sp._fmt_num(tdx_af)}"))
        for d, summary in conflicted_dates:
            t2_stat["rows_conflicted"] += 1
            con.execute("UPDATE kline_daily SET conflict_src=? WHERE ts_code=? AND date=?",
                        [summary, code, d])
            t2_stat["updated"] += 1
    result["kline_daily"] = t2_stat

    # ---- T7 index_daily ----
    from lake.ingest.tencent_ingest import INDEX_CODES

    t7_stat = {"rows_compared": 0, "rows_conflicted": 0, "updated": 0}
    for ic in INDEX_CODES:
        db_rows = con.execute(
            "SELECT date, close FROM index_daily WHERE index_code=? ORDER BY date",
            [ic]).fetchall()
        if not db_rows:
            continue
        try:
            vrows = tdx_ad.fetch_index_kline(ic)
        except Exception as exc:  # noqa: BLE001
            log.warning("reconcile T7 %s tdx 取数失败，跳过: %s", ic, exc)
            continue
        if not vrows:
            continue
        vmap = {r["date"]: r for r in vrows}
        for d, close in db_rows:
            vs = vmap.get(str(d))
            if vs is None or close is None or vs.get("close") is None or close == 0:
                continue
            t7_stat["rows_compared"] += 1
            if sp._rel_diff_pct(close, vs["close"]) > t7_close_pct:
                summary = (f"close:tencent:{sp._fmt_num(close)}|tdx:{sp._fmt_num(vs['close'])}")[:256]
                con.execute("UPDATE index_daily SET conflict_src=? WHERE index_code=? AND date=?",
                            [summary, ic, d])
                t7_stat["rows_conflicted"] += 1
                t7_stat["updated"] += 1
    result["index_daily"] = t7_stat
    return result


# ---------------------------------------------------------------------------
# status（coverage + progress 摘要）
# ---------------------------------------------------------------------------
def run_status(con, db_path: str) -> Dict[str, Any]:
    """各表 coverage（rows/codes/date_min/max，复用 backfill._refresh_coverage
    的口径）+ progress 文件摘要。"""
    from lake.backfill import load_progress

    # coverage：与 BackfillRunner._refresh_coverage 相同的 (table, code_col, date_col) 口径
    cov: Dict[str, Any] = {}
    for table, code_col, date_col in [
        ("kline_daily", "ts_code", "date"),
        ("valuation_daily", "ts_code", "date"),
        ("fundamentals_quarterly", "ts_code", None),
        ("dividend_events", "ts_code", "ex_date"),
        ("index_daily", "index_code", "date"),
    ]:
        try:
            rows = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            codes_n = con.execute(
                f"SELECT COUNT(DISTINCT {code_col}) FROM {table}").fetchone()[0]
            if date_col:
                dmin, dmax = con.execute(
                    f"SELECT MIN({date_col}), MAX({date_col}) FROM {table}"
                ).fetchone()
                cov[table] = {"rows": rows, "codes": codes_n,
                              "date_min": str(dmin) if dmin else None,
                              "date_max": str(dmax) if dmax else None}
            else:
                cov[table] = {"rows": rows, "codes": codes_n}
        except Exception:  # noqa: BLE001 - 表空/不存在 → 跳过（与 _refresh_coverage 一致）
            continue
    # stock_master 无日期列，单独补
    try:
        cov["stock_master"] = {
            "rows": con.execute("SELECT COUNT(*) FROM stock_master").fetchone()[0],
            "codes": None,
        }
    except Exception:  # noqa: BLE001
        pass

    # B-2：progress 路径与 db_path 同目录派生（缺省库→None 走 _progress_path()，可被测试
    # monkeypatch + 行为不变；自定义 --db → 该库旁，不再硬编码 data/lake/）。
    prog_path = _progress_for_db(db_path)
    prog = load_progress(prog_path)
    return {
        "sub": "status",
        "db_path": db_path,
        "coverage": cov,
        "progress_file": {
            "path": prog_path or os.path.join(_project_root(), "data", "lake", "backfill_progress.json"),
            "updated_at": prog.get("updated_at"),
            "done_keys": len(prog.get("done", [])),
            "tasks_view": prog.get("tasks", []),
        },
        "quota": _quota_state(),
    }


# ---------------------------------------------------------------------------
# init（幂等建 schema）
# ---------------------------------------------------------------------------
def run_init(con, db_path: str) -> Dict[str, Any]:
    """init：幂等建 schema（lake.conn.open 内部已执行 ddl.init_schema）。"""
    from lake.ddl import TABLES

    tables = [r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='main' AND table_type='BASE TABLE' ORDER BY table_name"
    ).fetchall()]
    views = [r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_type='VIEW' "
        "ORDER BY table_name").fetchall()]
    return {
        "sub": "init",
        "db_path": db_path,
        "tables_created": len(tables),
        "tables": tables,
        "views": views,
        "expected_tables_ok": set(TABLES) <= set(tables),
        "idempotent": True,  # open() → init_schema（全部 IF NOT EXISTS，可重复调用）
    }


# ---------------------------------------------------------------------------
# main / argparse
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python scripts/lake_backfill.py",
        description="v6 数据湖灌数 driver（init/p0/history/status；复用 ingest + BackfillRunner）",
    )
    p.add_argument("--db", default=None,
                   help="DuckDB 库路径（缺省 data/lake/lake.duckdb；测试可指 tmp）")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init", help="幂等建 schema，打印库路径 + 表清单")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("p0", help="当前数据优先灌数（T1/T4/T7/T2/T3）")
    sp.add_argument("--days", type=int, default=250,
                    help="T2/T7 近 N 交易日（默认 250）")
    sp.add_argument("--codes", default=None,
                    help="逗号分隔股票子集（缺省=stock_master 全集；冒烟用，如 sh.601398,sz.000001）")
    sp.add_argument("--skip-t1", action="store_true",
                    help="跳过 BaoStock T1（重跑/冒烟用）")
    sp.add_argument("--t4-codes", default=None,
                    help="T4 分红过滤（逗号分隔；缺省=全史全表）")
    sp.add_argument("--force", action="store_true",
                    help="B-3：强制重取 T7 四指数（清空 index_daily done 键，忽略幂等跳过）")
    sp.set_defaults(func=cmd_p0)

    sp = sub.add_parser("history", help="P2 全史后台补（v6.1 多源资源池：sina 主源→tencent→tdx）")
    sp.add_argument("--codes", default=None, help="逗号分隔股票子集（缺省=全集）")
    sp.add_argument("--start-date", default="1990-01-01", help="全史起点（默认 1990-01-01）")
    sp.add_argument("--end-date", default=None, help="全史终点（缺省=今日北京时间）")
    sp.add_argument("--t5", action="store_true",
                    help="v6.1：同时灌 T5 基本面（adata F10 主源 + BaoStock 探测存活时交叉校验）")
    sp.set_defaults(func=cmd_history)

    sp = sub.add_parser("reconcile",
                        help="v6.1：仅跑跨源校验补 conflict_src（不重取主源数据；tdx 次源比对）")
    sp.add_argument("--codes", default=None, help="逗号分隔股票子集（缺省=kline_daily 全集）")
    sp.add_argument("--start-date", default="1990-01-01", help=argparse.SUPPRESS)
    sp.add_argument("--end-date", default=None, help=argparse.SUPPRESS)
    sp.set_defaults(func=cmd_reconcile)

    sp = sub.add_parser("status", help="各表 coverage + progress 文件摘要")
    sp.set_defaults(func=cmd_status)
    return p


def _parse_codes(s: Optional[str]) -> Optional[List[str]]:
    if not s:
        return None
    return [c.strip() for c in s.split(",") if c.strip()]


def cmd_init(args, con, db_path: str) -> int:
    summary = run_init(con, db_path)
    _print_summary("init", summary)
    return EXIT_OK


def cmd_status(args, con, db_path: str) -> int:
    summary = run_status(con, db_path)
    _print_summary("status", summary)
    return EXIT_OK


def cmd_p0(args, con, db_path: str) -> int:
    codes = _parse_codes(args.codes)
    t4_scope = _parse_codes(args.t4_codes)
    summary = run_p0(con, db_path, args.days, codes, args.skip_t1, t4_scope,
                     force=args.force)
    _print_summary("p0", summary)
    return EXIT_OK


def cmd_history(args, con, db_path: str) -> int:
    codes = _parse_codes(args.codes)
    end_date = args.end_date or _today_beijing()
    summary = run_history(con, db_path, codes, args.start_date, end_date)
    # v6.1：--t5 同时灌 T5（adata F10 主源；独立 done 键，失败不影响 history 主体）
    if getattr(args, "t5", False):
        from lake.backfill import BackfillRunner

        runner = BackfillRunner(db_path=db_path)
        summary["t5"] = run_t5(con, db_path, codes, runner)
    _print_summary("history", summary)
    return EXIT_OK


def cmd_reconcile(args, con, db_path: str) -> int:
    codes = _parse_codes(args.codes)
    end_date = args.end_date or _today_beijing()
    # v6.1 Q6：reconcile 也探一次 BaoStock（一致性；结果写日志+progress，按 db_path 派生）
    _run_bs_probe(db_path)
    summary = run_reconcile(con, db_path, codes, args.start_date, end_date)
    _print_summary("reconcile", summary)
    return EXIT_OK


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)

    from lake.conn import LakeInvalidFile, LakeUnavailable  # 延迟 import：--help 不依赖 duckdb

    # B-4：写命令（init/p0/history/reconcile）整段包在 LakeLock(flock) 内——connect + 写入 + close
    # 全持锁，使并发 writer 阻塞等锁而非在 connect 阶段互撞崩溃。status 只读不持锁。
    write_cmd = args.cmd in ("init", "p0", "history", "reconcile")

    if not write_cmd:
        return _run_unlocked(args)

    try:
        with _write_lock(args.db):
            return _run_unlocked(args)
    except LakeInvalidFile as exc:  # D-1：无效库文件（0 字节/损坏）→ 友好报错，不崩 traceback
        print(f"错误: {exc}", file=sys.stderr)
        return EXIT_LAKE_UNAVAILABLE
    except LakeUnavailable as exc:
        print(f"错误: 数据湖不可用 —— {exc}", file=sys.stderr)
        return EXIT_LAKE_UNAVAILABLE


def _run_unlocked(args) -> int:
    """main() 主体（connect + 子命令 + close）。写路径由 main() 在 LakeLock 内调用。"""
    from lake.conn import LakeInvalidFile, LakeUnavailable

    try:
        con = _open_db(args.db)
    except LakeInvalidFile as exc:
        # D-1：0 字节/无效库文件（duckdb 打不开）→ 友好报错，不崩裸 traceback。
        # 与缺 duckdb 同用 EXIT_LAKE_UNAVAILABLE=3（库不可用的统一语义码）。
        print(f"错误: {exc}\n"
              f"       处理: 删除该空/损坏文件后重跑 init（python scripts/lake_backfill.py --db <path> init）",
              file=sys.stderr)
        return EXIT_LAKE_UNAVAILABLE
    except LakeUnavailable as exc:
        # brief 红线：lake extra 缺 duckdb → 友好报错，不崩 traceback
        print(f"错误: 数据湖不可用 —— {exc}\n"
              f"       安装方法: uv sync --extra lake（或 uv pip install 'duckdb>=1.5,<2'）",
              file=sys.stderr)
        return EXIT_LAKE_UNAVAILABLE

    try:
        rc = args.func(args, con, args.db or _default_db_path())
    except LakeUnavailable as exc:
        print(f"错误: 数据湖不可用 —— {exc}", file=sys.stderr)
        return EXIT_LAKE_UNAVAILABLE
    finally:
        try:
            con.close()
        except Exception:  # noqa: BLE001
            pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
