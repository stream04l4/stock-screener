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
- full                ← v6.1.6 单按钮全量补齐（Joel 拍板：一个按钮负责启动停止，
                        启动了就是要把所有历史及现状数据全部补上）。**一个 backfill
                        进程内顺序执行四阶段**（v6.1.7 加 t6），同一把 DuckDB 独占锁
                        贯穿全程（main() 的 LakeLock 包住整个 full——天然无竞争）：
                          phase1 history（T2 全史 + adj_factor，幂等 done 键全跳过；
                                  --t5 不在此处——T5 是独立阶段 3）
                          phase2 P3 增量（kline_daily→valuation_daily→index_daily，
                                  run_incremental 原样复用）
                          phase3 T5 fundamentals_quarterly（run_t5 原样复用：
                                  adata F10 主源 + BaoStock 探测存活时交叉校验，
                                  限速 adata_f10_min_interval_s）
                          phase4 T6 holders_snapshot（v6.1.7：SinaClient.fetch_holders
                                  全史前十大股东 → sina_ingest.load_t6；限速/WAF退避/
                                  熔断全在 SinaClient 内部 ≥1s/只；done 键 (t6, ts_code)
                                  幂等续传；--no-t6 可跳过——排障用）
                        每阶段开始/结束打 sync.log 段标（===== phase: X =====）；
                        任一阶段异常 → last_error + 该任务 state=error，**继续下一
                        阶段**（单表故障不拖死全量；hang1 R3 停滞看门狗照旧兜底）。

纪律（v6.0.2）：
- BaoStock 一律走 QuotaGuard（baostock_ingest 现有路径），不得裸调；日预算到顶
  当日停、次日续（BackfillRunner._quota_state + QuotaGuard 日期翻转）。
- 零回归红线：本脚本只 import lake/* + screener.data.*，**不改动 screener/**。
- duckdb 未装 → LakeUnavailable 友好报错退出（exit 3），不崩 traceback。
- **D-1（v6.0.3 rework）**：--db 指向 0 字节/无效库文件（duckdb 打不开）→
  LakeInvalidFile 友好报错退出（exit 3），同样不崩裸 traceback。

子命令：init / p0 / history / full / incremental / reconcile / status（见 --help）。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import sys
import threading
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
def _q6_probe_timeout_s() -> float:
    """Q6 启动探测墙钟硬上限（秒）。缺省 **20.0**（brief F3 红线"不得超过 20s"逐字值）。

    env ``BS_Q6_PROBE_TIMEOUT_S`` 可覆盖——**仅供离线测试提速**（生产不设=20s）。
    与 baostock_adapter._available_timeout_s 同模式：每次现读 env。非法值回退 20.0。
    """
    try:
        return max(1.0, float(os.environ.get("BS_Q6_PROBE_TIMEOUT_S", "20")))
    except ValueError:
        return 20.0


def _run_bs_probe(db_path: Optional[str] = None) -> Dict[str, Any]:
    """灌数启动的 Q6 BaoStock 存活探测（p0/history/reconcile 各调一次）。

    - config ``baostock_probe_enabled=False`` → 跳过（按"死"处理，零网络——离线单测契约）。
    - env ``LAKE_MULTISOURCE=0``（测试隔离门）→ 同样跳过（与 resolve_source 一致）。
    - 探测本身 fail-fast：socket 10s 超时 + **F3/v6.1.5 墙钟硬上限 20s**（绝不阻塞灌数启动；
      红线"任何代码路径在 baostock 上阻塞不得超过 20s"）。实现=``_with_timeout``（与
      O4/adapter.available() 同一超时工具，不各写一套）：内层 probe 显式 wall_budget_s=15
      （<20，正常 hang 由内层先判死并给出干净 detail），外层 20s 硬兜底——任一层先到即
      alive=false（detail="probe timeout >20s"）。**红线：screener/ 零改动**——超时全部在
      lake/scripts 层施加，probe_baostock_alive 本身未动。
    - 结果写日志 + progress 顶层 ``baostock_probe``（Web /status 可观测）。

    **progress 路径按 db_path 派生**（v6.1 隔离修复）：自定义 --db（E2E/tmp 库）→
    写到该库目录的 progress，**绝不污染生产 data/lake/backfill_progress.json**；
    缺省库 → None → load/save_progress 走生产默认路径（原行为不变）。
    """
    from lake.config import lake_cfg

    if os.environ.get("LAKE_MULTISOURCE") == "0":
        return {"enabled": False, "alive": False, "detail": "multisource off (test isolation)"}
    # DEFECT-HANG-1（repro 纪律）：env LAKE_DISABLE_BAOSTOCK=1 → 跳过 BaoStock 探测
    # （baostock_alive 恒 False → adapter available()=False → 各池排除 baostock，零连接）。
    # 用途：生产 backfill 正在跑（BaoStock 串行占用）时，repro/并行作业必须避免**第二个**
    # BaoStock 连接源（>~4 并发连接触发服务端黑名单，见团队纪律）。sina/tencent/tdx 不受
    # 影响——HANG-1 卡死点恰在这三源网络路径（ESTAB→Tencent:443 + CLOSE-WAIT），R1 证据完整。
    if os.environ.get("LAKE_DISABLE_BAOSTOCK") == "1":
        return {"enabled": False, "alive": False,
                "detail": "baostock disabled (LAKE_DISABLE_BAOSTOCK=1)"}
    if not lake_cfg().get("baostock_probe_enabled", True):
        return {"enabled": False, "alive": False, "detail": "probe disabled by config"}
    from lake.ingest.source_pool import _with_timeout, set_baostock_alive

    # F3/v6.1.5：Q6 启动探测 ≤20s 硬上限（brief 逐字"同样 ≤20s 硬超时，超时记
    # alive=false detail='probe timeout >20s'"）。内层 wall_budget_s=15 <20（正常 hang
    # 由 probe 自身先判死、detail 干净）；外层 _with_timeout(缺省 20s) 兜底——协议层若
    # 吞掉 socket.timeout 进内部循环，必在预算内返回。screener/ 零改动：超时全在本调用点施加。
    tmo = _q6_probe_timeout_s()   # 缺省 20.0（brief 红线值）；env BS_Q6_PROBE_TIMEOUT_S 仅测试提速
    t0 = time.monotonic()
    try:
        from screener.data.baostock_client import probe_baostock_alive

        res = _with_timeout(
            lambda: probe_baostock_alive(timeout_s=10.0, wall_budget_s=15.0),
            secs=tmo, name="bs-q6-probe")
    except TimeoutError:
        log.warning("Q6 BaoStock 探测超时 >%.0fs（按死处理，不阻塞灌数启动）", tmo)
        res = {"alive": False, "elapsed_s": round(time.monotonic() - t0, 2),
               "detail": f"probe timeout >{tmo:.0f}s"}

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
# v6.1 D3：资源池健康探测——灌数 driver 启动时探各 adapter available() 记 latency，
# 落 data/lake/source_health.json（Web /status.source_pool.adapters 只读消费）
# ---------------------------------------------------------------------------
def _probe_all_adapters(db_path: Optional[str] = None) -> Dict[str, Any]:
    """灌数 driver 启动的 adapter 健康探测（报告 §4：source_health.json 写入方）。

    - env ``LAKE_MULTISOURCE=0``（测试隔离门）→ 跳过，零网络（与 _run_bs_probe /
      resolve_source 同口径；conftest autouse 默认置位 → 离线单测不触网）。
    - 探测对象 = AUTHORITY 的 5 个外部源（sina/tencent/baostock/tdx/adata_f10）；
      ``local`` 是本地推导（静态 csv/factors 派生），无网络可达性概念，不进健康度。
    - 各 adapter ``available()`` 记 latency_ms：首次调用触发 EU 自检（网络探测），
      失败→False 自动跳过该源（不 crash、不阻塞灌数启动——单源异常只记 false）。
      baostock adapter 的 available() = Q6 探测结果（零网络）——**history/reconcile
      路径须先跑 _run_bs_probe 再调本函数**，否则按"未探测=死"保守记录。
    - adapter 未注册（依赖库未装/import 失败）→ 记 available=false（该源在本环境
      恒不可用——resolve_source 同样跳过它；不猜 null）。
    - 落点 = progress 同目录的 ``source_health.json``（B-2 纪律：自定义 --db → 库
      目录，绝不污染生产 data/lake/；缺省库 → 生产默认路径）。原子写（tmp+rename）；
      写失败不阻断灌数（与 _run_bs_probe 的 progress 写纪律一致）。

    :return: ``{"enabled": bool, "path": str|None, "adapters": {...}}``（summary 用）。
    """
    from lake.ingest.source_pool import AUTHORITY, get_adapter

    if os.environ.get("LAKE_MULTISOURCE") == "0":
        return {"enabled": False, "path": None,
                "detail": "multisource off (test isolation)"}
    names = [n for n in ("sina", "tencent", "baostock", "tdx", "adata_f10")
             if n in AUTHORITY]
    now_s = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    adapters: Dict[str, Any] = {}
    for name in names:
        t0 = time.monotonic()
        try:
            ad = get_adapter(name)
            if ad is None:
                # 依赖库未装/import 失败 → 本环境恒不可用（resolve_source 同语义跳过）
                adapters[name] = {"available": False, "probed_at": now_s,
                                  "latency_ms": None}
                continue
            ok = bool(ad.available())
            adapters[name] = {"available": ok, "probed_at": now_s,
                              "latency_ms": int((time.monotonic() - t0) * 1000)}
        except Exception as exc:  # noqa: BLE001 - 单源探测异常→false（不 crash、不阻塞）
            log.warning("adapter %s available() 探测异常 → 记 false: %s", name, exc)
            adapters[name] = {"available": False, "probed_at": now_s,
                              "latency_ms": int((time.monotonic() - t0) * 1000)}

    # 落点：progress 同目录（B-2 派生纪律；缺省库→生产 data/lake/）。
    # **DEFECT-D3-3**：fallback 统一经 ``lb._progress_path()``（lake.backfill），**不再**
    # 直接调 ``lconn.progress_path()``——后者不可被测试 monkeypatch，多源用例 patch 了
    # _progress_path→tmp 时健康文件仍会落生产 data/lake/（红线违反）。口径：
    #   - 缺省库：_progress_for_db(None)=None → lb._progress_path()=data/lake/（行为不变）；
    #   - 测试 patch _progress_path→tmp：base=tmp（与 progress 同目录，patch 生效）；
    #   - **未 patch + 自定义 --db**：base 仍是真实生产默认 → B-2 派生到库目录
    #     （health 与 progress 同目录；绝不污染生产 data/lake/）。
    health_path = _source_health_path_for_db(db_path)
    try:
        import tempfile

        os.makedirs(os.path.dirname(health_path) or ".", exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".lake_sh_", suffix=".tmp",
                                   dir=os.path.dirname(health_path))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(adapters, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, health_path)
    except Exception as exc:  # noqa: BLE001 - 健康度写失败不阻断灌数（Web 显"未知"）
        log.warning("source_health.json 写入失败（不阻断）: %s", exc)
        health_path = None
    log.info("资源池健康探测: %s",
             {n: a["available"] for n, a in adapters.items()})
    return {"enabled": True, "path": health_path, "adapters": adapters}


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

    # v6.1.1 FIX-2：industry_csric2 自愈回填（BaoStock 挂时 csric2 全空 → 从
    # industry_name 提取 [A-Z]\d{1,3} 前缀；幂等、零网络；失败不阻断 T1 主体）
    try:
        industry_backfilled = backfill_industry_csric2(con)
    except Exception as exc:  # noqa: BLE001 - 回填失败不阻断（下轮 T1/手动 UPDATE 再补）
        log.warning("T1 industry_csric2 自愈回填失败（不影响 T1 主体）: %s", exc)
        industry_backfilled = 0

    return {
        "table": "stock_master",
        "rows_written": n,
        "name_isst_enriched": enriched,
        "industry_csric2_backfilled": industry_backfilled,
        "baostock_calls_this_step": 2,
        "note": "BaoStock 侧列=list_date/delist_date/board/industry；"
                "name/is_st 由腾讯快照补；soe_* 留 NULL（v5 规则依赖 T6，P2 补）",
    }


# ---------------------------------------------------------------------------
# v6.1.1 FIX-2：industry_csric2 自愈回填（独立函数——REG-1 行业测试直接调用，零网络）
# ---------------------------------------------------------------------------
def backfill_industry_csric2(con) -> int:
    """v6.1.1 FIX-2：industry_csric2 自愈回填（从 industry_name 提取 CSRC 行业代码前缀）。

    背景：P0 灌 T1 时 BaoStock 挂 → ``industry_csric2`` 全空（生产实测 0/5556），
    只有 ``industry_name`` 有值（83 个 distinct，**全部**带 ``[A-Z]\\d{1,3}`` 代码前缀，
    如 "C39计算机、通信和其他电子设备制造业"）。行业下拉（/api/lake/industries 按
    csric2 分组）与 market 筛选（``m.industry_csric2 = ?``）因此空转。本函数无损回填：

    - ``regexp_extract(industry_name, '^([A-Z][0-9]{1,3})', 1)`` 提取前缀；
    - **提取不到前缀的行保持 NULL**——⚠️ DuckDB ``regexp_extract`` 无匹配时返回
      **空串 ''（不是 NULL）**（实测），故 WHERE 必须显式排除空串，否则会把
      "无前缀"行回填成 ''（industries API 的 ``<> ''`` 过滤虽能挡住，但库内脏值
      违反"提取不到保持 NULL"契约）；
    - 幂等：已非空的行不碰（BaoStock 恢复后 fetch_industry 给出真 csric2 时，
      load_t1 的 upsert 整行覆盖回填值——无冲突，回填值本就是 name 里的真代码）。

    :return: 实际回填行数（UPDATE 前 COUNT 同条件口径；零网络、纯 SQL）。
    """
    # 回填候选 = csric2 空 + name 非空 + name 有合法前缀（与 UPDATE 条件逐字一致）
    n_candidates = con.execute(
        "SELECT COUNT(*) FROM stock_master WHERE "
        "(industry_csric2 IS NULL OR industry_csric2 = '') "
        "AND industry_name IS NOT NULL "
        "AND regexp_extract(industry_name, '^([A-Z][0-9]{1,3})', 1) <> ''"
    ).fetchone()[0]
    if not n_candidates:
        return 0
    con.execute(
        "UPDATE stock_master SET industry_csric2 = "
        "regexp_extract(industry_name, '^([A-Z][0-9]{1,3})', 1) WHERE "
        "(industry_csric2 IS NULL OR industry_csric2 = '') "
        "AND industry_name IS NOT NULL "
        "AND regexp_extract(industry_name, '^([A-Z][0-9]{1,3})', 1) <> ''"
    )
    log.info("FIX-2 industry_csric2 自愈回填: %d 行（从 industry_name 提取前缀）", n_candidates)
    return int(n_candidates)


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


def _source_health_path_for_db(db_path: Optional[str]) -> str:
    """**DEFECT-D3-3（B-2 统一口径）**：source_health.json 落点 = progress 同目录。

    口径与 :func:`_progress_for_db` / ``BackfillRunner`` 完全一致（健康文件必须与
    progress 文件同目录，测试 patch ``lake.backfill._progress_path``→tmp 时两者一起
    落 tmp，绝不污染生产 data/lake/）：

    - 缺省库（db_path=None/默认路径）且未 patch → ``data/lake/source_health.json``
      （= 原行为逐字节不变——生产 driver 唯一真实场景）；
    - 测试 patch _progress_path→tmp → tmp 目录（patch 生效，与 progress 同目录）；
    - **未 patch + 自定义 --db** → B-2 派生到库目录（progress 也落库目录，两者一致）。

    ⚠️ 不直接调 ``lake.conn.progress_path()``——它不可被 monkeypatch，正是 DEFECT-D3-3
    的根因（多源用例 patch 了 _progress_path→tmp，fallback 却打到生产路径）。
    """
    from lake import backfill as lb

    base = _progress_for_db(db_path) or lb._progress_path()
    return os.path.join(os.path.dirname(os.path.abspath(base)), "source_health.json")


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

    # v6.1 D3：p0 启动也探资源池健康（Web /status.source_pool.adapters 数据源；
    # LAKE_MULTISOURCE=0 → 跳过零网络）。baostock adapter available() 读 Q6 进程内
    # 状态——p0 本身不跑 _run_bs_probe，未探测时按"死"保守记录（与 baostock_alive
    # 的保守语义一致；history/reconcile 路径先探 Q6 再探健康度）。
    health_res = _probe_all_adapters(db_path)

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
        # v6.1 D3：资源池健康探测结果（summary 可观测；落盘在 _probe_all_adapters 内）
        "source_health": health_res,
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
    from lake.backfill import BackfillRunner, Task, save_progress, stop_requested as _bk_stop
    from lake.config import crosscheck_threshold, lake_cfg
    from lake.ingest import source_pool as sp
    from lake.ingest.common import HangWatchdogError, ProgressWatchdog
    from lake.ingest.tencent_ingest import load_t2
    from screener.data.baostock_client import BaoStockClient
    from screener.data.tencent import TencentClient

    # v6.1 Q6：灌数启动探一次 BaoStock（config 关/单测门控 → 跳过，零网络）；
    # progress 落盘按 db_path 派生（自定义 --db → 库目录，不污染生产 progress）
    probe_res = _run_bs_probe(db_path)
    # v6.1 D3：资源池健康探测（Q6 之后——baostock adapter available() 读 Q6 结果；
    # LAKE_MULTISOURCE=0 → 跳过零网络）。落 source_health.json，Web /status 消费。
    health_res = _probe_all_adapters(db_path)

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

    # DEFECT-HANG-1（R3）：进度停滞看门狗——progress 文件 mtime 连续 hang_stall_minutes
    # （config，缺省 10min）无推进 → 主线程 SIGINT abort（抛 HangWatchdogError）。
    # 为什么需要：HANG-1 的卡死是"进程活着但零产出数小时"（线程池/连接池 stateful 死锁），
    # 单任务 R2 超时救不了跨任务的池级死锁；看门狗在 runner 层兜底——干净退出（flock/
    # DuckDB lock 随进程释放、done 键已落盘）让下次 relaunch 从 stable done keys 续传。
    # arm() 必须在主线程调用（signal.signal 限制）——run_history 由 driver cmd_history
    # 在主线程执行，满足。<=0 → 禁用（config 可调）。
    _stall_min = float(lake_cfg().get("hang_stall_minutes", 10.0))
    _wd: Optional[ProgressWatchdog] = None
    if _stall_min > 0:
        _wd = ProgressWatchdog(runner.progress_path, _stall_min).arm()
    try:
        stats.update(runner.run(tasks, worker))
    except HangWatchdogError as exc:
        # R3 abort：记 last_error + state=hang_watchdog + 落盘（Web/TL 可观测"为何停"），
        # 干净退出 rc=1。done 键已在每次 mark_done 原子落盘 → 下次续传无损。
        log.error("R3 看门狗触发，history 干净退出（续传无损）: %s", exc)
        _stats_hang = dict(stats)
        _stats_hang["hang_watchdog"] = True
        _stats_hang["last_error"] = f"hang_watchdog: {exc}"
        for _entry in runner.progress.get("tasks", []):
            if isinstance(_entry, dict) and _entry.get("table") == "kline_history":
                _entry["state"] = "hang_watchdog"
                _entry["last_error"] = f"progress 停滞 >{_stall_min:.0f}min → 看门狗 abort"
        runner.progress["stopping_at"] = None
        save_progress(runner.progress, runner.progress_path)
        bs.close()   # R4：baostock send_msg 紧循环守卫已装（CLOSE-WAIT 立即返回，不 spin）
        return _stats_hang
    finally:
        if _wd is not None:
            _wd.stop()   # 恢复原 SIGINT handler + 停看门狗线程（正常收尾/异常都执行）

    bs.close()
    return {"sub": "history", **stats, "baostock_probe": probe_res,
            "source_health": health_res,
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
# T6 holders_snapshot（v6.1.7：full 模式第 4 阶段——SinaClient.fetch_holders → load_t6）
# ---------------------------------------------------------------------------
def _t6_client_factory():
    """T6 SinaClient 构造点（**独立函数=离线单测注入点**）。

    brief §A 逐字口径：``SinaClient(sina_cfg(yaml strategy.yaml))``——screener/config.py
    ``load_config(config/strategy.yaml)`` + ``sina_cfg(cfg)``（interval_s>=1.0 等纪律由
    config 层校验）。限速/WAF456退避/连续失败熔断**全部在 SinaClient 内部**（≥1s/只、
    每 cooldown_every_n 次请求长冷却）——本函数不另设节奏。
    """
    import os

    from screener.config import load_config, sina_cfg
    from screener.data.sina import SinaClient

    cfg = load_config(os.path.join(_project_root(), "config", "strategy.yaml"))
    return SinaClient(sina_cfg(cfg))


def run_t6(con, db_path: str, codes: Optional[List[str]], runner) -> Dict[str, Any]:
    """T6 前十大股东快照（v6.1.7 full 阶段 4；brief §A）。

    - **全市场逐只** ``SinaClient.fetch_holders(code6)`` → ``sina_ingest.load_t6``：
      一次返回该股**全部报告期**（TL 实测工行 79 期 2006→今，每期前十大
      {holder_rank, holder_name, hold_shares, circ_ratio_pct, share_nature}）；
      load_t6 as_of_date=None → 删该股全部快照后整体重灌（幂等；零行 no-op 不删旧）。
    - **done 键 (t6, ts_code)**（brief 逐字；固定字符串跨天续传——与 kline_history 的
      "full_history" / T5 的 "f10_full" 同模式）：中断重跑跳过已灌股，不重复耗新浪配额。
    - **单只失败不阻断后续**（runner 既有语义：worker 抛错 → 记 errors、不 mark_done、
      继续下一只；下轮续传自动重试该股）。SinaClient 熔断（连续 N 只失败）→ 后续
      fetch_holders 立即抛 SinaDataError → 同走单任务失败路径，不拖死整阶段。
    - **progress tasks 条目升级**（brief 逐字：由 no_source/total=0 升级为真实任务，
      tier=P2）：阶段开始时把 holders_snapshot entry 置 total=stock_master 全集行数、
      state=running——runner.run 的 _update_task_view/_refresh_task_view 随后按
      (holders_snapshot, P2) 分组推进 done/eta；收尾 _refresh_incremental_task_view
      再按全集口径归位（done==total→done，否则 pending）。
    - 限速纪律：SinaClient 内部 ≥1s/只 + WAF 冷却（5219 只约 1.5h——长跑正常）；
      worker 尾 0.3s 兜底小睡（同 T2/T5 节奏，不绕过 adapter 限速）。

    :param codes: 股票子集（None=stock_master 全集；透传 --codes 冒烟口径）。
    """
    from lake.backfill import Task, save_progress
    from lake.ingest.sina_ingest import load_t6

    codes = codes or _universe_codes(con)
    tasks = [Task(priority=2, table="holders_snapshot", ts_code=c,
                  period_or_date="t6", tier="P2") for c in codes]
    stats: Dict[str, Any] = {"codes_requested": len(codes)}

    # brief 逐字：progress tasks 的 holders_snapshot 条目**在 t6 阶段开始时**由
    # no_source/total=0 升级为真实任务（total=stock_master 行数、tier=P2、
    # state pending→running）。runner.run 开始处 _update_task_view 会把 state 推成
    # running——这里先落一次"升级"快照（total/tier/state），让阶段边界即可观测。
    try:
        e6 = next((t for t in runner.progress.get("tasks", [])
                   if t.get("table") == "holders_snapshot"), None)
        if e6 is None:
            e6 = {"table": "holders_snapshot", "tier": "P2", "total": 0, "done": 0,
                  "quota_used_today": runner.quota_used_today(),
                  "quota_budget": runner.budget_per_day,
                  "state": "pending", "eta_min": None, "last_error": ""}
            runner.progress.setdefault("tasks", []).append(e6)
        e6["tier"] = "P2"   # 升级：原 no_source 条目是 P3（v6.1.4 O3 占位）→ P2
        e6["total"] = len(codes)
        e6["state"] = "running"
        e6["note"] = "SinaClient.fetch_holders 全史前十大股东（sina_f10；controller_* 待补源）"
        save_progress(runner.progress, runner.progress_path)
    except Exception as exc:  # noqa: BLE001 - 视图升级写失败不阻断灌数（run 内仍会建 entry）
        log.warning("T6 progress 条目升级落盘失败（不阻断）: %s", exc)

    client = _t6_client_factory()

    def worker(task: Task) -> None:
        code = task.ts_code
        # ts_code "sh.601398" → code6 "601398"（新浪 F10 URL 口径，同 screener 引擎）
        periods = client.fetch_holders(code.split(".")[1])
        if not periods:
            # fetch_holders 契约上取空即抛 SinaDataError；双保险——绝不在无数据时
            # mark_done（done 键毒化防线，同 history/T5 纪律）
            raise RuntimeError(f"T6 新浪 F10 股东页取空 {code}（不标 done，下轮重试）")
        n = load_t6(con, code, periods)   # as_of_date=None → 全部报告期（全史快照）
        if n <= 0:
            raise RuntimeError(f"T6 load_t6 转换零行 {code}（页面结构漂移?不标 done，下轮重试）")
        time.sleep(0.3)   # 兜底小睡（SinaClient 内部 ≥1s/只限速之外的节奏余量，同 T2/T5）

    stats.update(runner.run(tasks, worker))
    return {"table": "holders_snapshot", **stats}


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
# ---------------------------------------------------------------------------
# v6.1.4 O2：incremental（P3 每日增量——kline_daily 最近缺口 / valuation_daily 最新
# 快照 / index_daily 近 N 日；占位 tasks 从未有 runner 消费，本命令首次实现）
# ---------------------------------------------------------------------------
def _kline_gap_start(con, code: str, fallback_days: int) -> str:
    """T2 增量起点：该股库内 date_max+1（无行 → 近 fallback_days 自然日）。

    为什么按股取缺口而非全市场统一窗口：history 灌完的股与漏灌/新股的 date_max
    可能不同（停牌、补录），统一窗口要么重复拉老数据要么漏掉真缺口。date_max+1
    精确到"缺的第一天"；无行（新股/漏灌）回退近 250 日（≈一年，覆盖 IPO 首日）。
    """
    row = con.execute(
        "SELECT MAX(date) FROM kline_daily WHERE ts_code=?", [code]).fetchone()
    dmax = row[0] if row else None
    if dmax is None:
        return (_dt.date.today() - _dt.timedelta(days=fallback_days)).isoformat()
    # DATE 列 → datetime.date；+1 天 = 缺口第一天（ISO 字符串，与窗口比较同口径）
    return (dmax + _dt.timedelta(days=1)).isoformat()


def run_incremental(con, db_path: str, codes: Optional[List[str]],
                    days: int) -> Dict[str, Any]:
    """P3 每日增量（v6.1.4 O2，brief 逐字）：

    - **kline_daily**：对全市场取**最近缺口**（每股 date_max+1→今日；无行→近 250 日），
      走 source_pool ``resolve_source("kline_daily","ohlcv_amount")`` + adj_factor 组
      （Q1 优先级 sina→tencent→tdx / sina→tdx→baostock，与 history/p0 同一 worker 语义）；
      upsert 复用 load_t2（内部 common.upsert）。限速：sina/tdx adapter 内置
      RateLimiter（config sina_min_interval_s 等）+ worker 尾 0.3s。
    - **valuation_daily**：Tencent load_t3 逐只取最新快照（date=今日；腾讯快照是
      "当前时点"数据，滞后多少补多少 = 每次覆盖写最新值）。取空前置一批探测
      （全空=网络故障 → 抛错不标 done；个别缺失=停牌/批次失败 → 跳过该只）。
    - **index_daily**：四指数腾讯主源近 N 日 upsert（同 run_t7 取数，done 键换新周期）。

    **幂等**：done 键 per-table+code+**运行日**（``inc:YYYY-MM-DD`` / T3=日期本身）——
    同日重跑跳过已灌；次日新键自动重取。全源失败 → worker 抛错**不 mark_done**
    （下轮重试，防 done 键毒化）。"缺口内无新数据"（节假日/未收盘：源正常返回但
    窗口 [start..今日] 无行）→ **视为完成**（确实无可补，标 done 避免每日空转报错；
    下一交易日新运行日键自动重取）。

    **quota/progress 落盘沿用 BackfillRunner 机制**（done 键/mark_done/save_progress
    零改动复用）；收尾把 tasks 占位条目刷成真实 total/done（见
    :func:`_refresh_incremental_task_view`——O3：kline_history 完成态如实标 done、
    T5 加 pending 条目、T6 标 no_source）。

    :param days: T7 指数窗口（交易日数，默认 250；取数 n=days+30 自然日缓冲，同 run_t7）。
    """
    from lake.backfill import BackfillRunner, Task
    from lake.config import crosscheck_threshold, lake_cfg
    from lake.ingest import source_pool as sp
    from lake.ingest.tencent_ingest import (INDEX_CODES, fetch_kline_ohlcv,
                                            fetch_snapshot, load_t2, load_t3, load_t7)
    from screener.data.tencent import TencentClient

    today_s = _today_beijing()
    inc_period = f"inc:{today_s}"   # T2/T7 幂等键的周期段（per-table+code+运行日）

    # Q6/健康探测（与 p0/history 一致：baostock adapter available() 读 Q6 结果；
    # 落 source_health.json 按 db_path 派生——自定义 --db 不污染生产目录）
    probe_res = _run_bs_probe(db_path)
    health_res = _probe_all_adapters(db_path)

    codes = codes or _universe_codes(con)
    runner = BackfillRunner(db_path=db_path)
    tclient = TencentClient()
    stats: Dict[str, Any] = {"codes_requested": len(codes), "as_of": today_s}

    # 多源池（门控同 run_t2/run_history：LAKE_MULTISOURCE=0 / 源开关全关 → [] → legacy）
    ohlcv_srcs = sp.resolve_source("kline_daily", "ohlcv_amount")
    adj_srcs = sp.resolve_source("kline_daily", "adj_factor")
    t2_close_pct = crosscheck_threshold("t2_close_pct", 0.5)
    t2_amount_pct = crosscheck_threshold("t2_amount_pct", 2.0)
    t2_af_pct = crosscheck_threshold("t2_adj_factor_pct", 0.5)

    # ---- T2 kline_daily：每股最近缺口（date_max+1→今日；无行→近 250 日）----
    gap_starts: Dict[str, str] = {c: _kline_gap_start(con, c, 250) for c in codes}

    def t2_worker(task: Task) -> None:
        code = task.ts_code
        start = gap_starts[code]
        if not ohlcv_srcs and not adj_srcs:
            # legacy 单源路径（池全不可用：离线单测/新源全降级）——腾讯窗口取数
            kl = fetch_kline_ohlcv(tclient, code, n=days + 30) or []
            kl = [r for r in kl if str(r["date"]) >= start]
            if not kl:
                return   # 缺口内无新数据（节假日/未收盘）→ 视为完成（不写库）
            load_t2(con, code, kl, adj_map=None)
            time.sleep(0.3)
            return

        kl: Optional[List[Dict[str, Any]]] = None
        source = "tencent"
        adj_map: Optional[Dict[str, float]] = None
        for ad in ohlcv_srcs:
            try:
                res = ad.fetch_kline(code, start=start)   # sina 忽略 start→全史取尾部窗口
            except Exception as exc:  # noqa: BLE001 - 该源失败 → 下一源（fallback）
                log.warning("incremental T2 %s source=%s 取数失败，回退下一源: %s",
                            code, ad.name, exc)
                continue
            if res and res.get("ohlcv"):
                kl = res["ohlcv"]
                source = ad.name
                adj_map = res.get("adj_factor")
                break
        if kl is None:
            raise RuntimeError(f"T2 增量 K线所有源均失败 {code}（不标 done，下轮重试）")

        # 窗口化：只 upsert 缺口 [start..今日] 内的行（更早历史不动）
        kl = [r for r in kl if str(r["date"]) >= start]
        if not kl:
            return   # 源正常但缺口内无新数据（节假日/未收盘）→ 视为完成

        # adj_factor：主源自带推导值优先；否则按 adj 优先级补取（同 run_t2）
        if not adj_map:
            for ad in adj_srcs:
                try:
                    m = ad.fetch_adj_factor(code, start, today_s)
                except Exception as exc:  # noqa: BLE001 - 该源失败 → 下一源
                    log.warning("incremental T2 %s adj source=%s 失败，回退下一源: %s",
                                code, ad.name, exc)
                    continue
                if m:
                    adj_map = m
                    break

        # cross_check：采用 sina 主源 → tdx 缺口窗口轻量验证（不阻断，写 conflict_src）
        conflict_src: Optional[str] = None
        if source == "sina":
            tdx_ad = sp.get_adapter("tdx") if lake_cfg().get("tdx_enabled", True) else None
            if tdx_ad is not None:
                try:
                    if tdx_ad.available():
                        vres = tdx_ad.fetch_kline(code, start=start, end=None)
                        vrows = (vres or {}).get("ohlcv") or []
                        if vrows:
                            conflict_src = sp.cross_check_kline(
                                kl, "sina", vrows, "tdx",
                                close_pct=t2_close_pct, amount_pct=t2_amount_pct)
                            if adj_map and (vres or {}).get("adj_factor"):
                                af_conf = sp.cross_check_adj_factor(
                                    adj_map, "sina", vres["adj_factor"], "tdx", pct=t2_af_pct)
                                conflict_src = _merge_conflict(conflict_src, af_conf)
                except Exception as exc:  # noqa: BLE001 - 验证失败不阻断（conflict=NULL）
                    log.warning("incremental T2 %s tdx 交叉校验失败（不阻断）: %s", code, exc)

        load_t2(con, code, kl, adj_map=adj_map, source=source,
                conflict_src=conflict_src, volume_is_shares=(source != "tencent"))
        time.sleep(0.3)   # 限速纪律（adapter RateLimiter 之外的兜底小睡，同 run_t2）

    t2_tasks = [Task(priority=3, table="kline_daily", ts_code=c,
                     period_or_date=inc_period, tier="P3") for c in codes]
    stats["t2"] = runner.run(t2_tasks, t2_worker)

    # ---- T3 valuation_daily：腾讯批量快照（date=今日；滞后多少补多少）----
    # 前置一批探测：全空 = 网络/接口故障 → 抛错中止（不标 done，防"假完成"毒化）；
    # 个别缺失 = 停牌/单批失败 → 该只跳过（次日新运行日键自动补）。
    probe_snaps = fetch_snapshot(tclient, codes[:50]) if codes else {}
    if not probe_snaps:
        raise RuntimeError("T3 估值快照全空（腾讯接口故障？）——本轮增量中止，"
                           "不标 done（下轮重试）")

    def t3_worker(task: Task) -> None:
        snaps = fetch_snapshot(tclient, [task.ts_code])
        snap = snaps.get(task.ts_code)
        if snap:
            load_t3(con, task.ts_code, snap, today_s)
        # 缺失（停牌/批次失败）→ 跳过不写（视为完成；次日新键重取）
        time.sleep(0.3)

    t3_tasks = [Task(priority=3, table="valuation_daily", ts_code=c,
                     period_or_date=today_s, tier="P3") for c in codes]
    stats["t3"] = runner.run(t3_tasks, t3_worker)

    # ---- T7 index_daily：四指数腾讯主源近 N 日 upsert（同 run_t7 取数口径）----
    def t7_worker(task: Task) -> None:
        ic = task.ts_code
        srcs = sp.resolve_source("index_daily", "ohlcv")
        kl, source = None, "tencent"
        if srcs:
            for ad in srcs:
                try:
                    rows = ad.fetch_index_kline(ic, n=days + 30)
                except Exception as exc:  # noqa: BLE001 - 该源失败 → 下一源
                    log.warning("incremental T7 %s source=%s 取数失败，回退下一源: %s",
                                ic, ad.name, exc)
                    continue
                if rows:
                    kl, source = rows, ad.name
                    break
        else:
            kl = fetch_kline_ohlcv(tclient, ic, n=days + 30)   # legacy 直连（零回归兜底）
        # tdx amount 补充 + close 交叉校验（同 run_t7：腾讯指数行 amount 缺→NULL；
        # tdx 提供则补上，>0.3% 记 conflict_src 不阻断）
        tdx_ad = sp.get_adapter("tdx") if lake_cfg().get("tdx_enabled", True) else None
        conflict = None
        tdx_rows: List[Dict[str, Any]] = []
        if kl and tdx_ad is not None:
            try:
                if tdx_ad.available():
                    tdx_rows = tdx_ad.fetch_index_kline(ic, n=days + 30) or []
            except Exception as exc:  # noqa: BLE001 - amount 补充失败不阻断（留 NULL）
                log.warning("incremental T7 %s tdx amount 补充失败（留 NULL）: %s", ic, exc)
        if kl and tdx_rows:
            tdx_map = {r["date"]: r for r in tdx_rows}
            for r in kl:
                tr = tdx_map.get(r["date"])
                if tr is not None and r.get("amount") is None \
                        and tr.get("amount") is not None:
                    r["amount"] = tr["amount"]   # 补缺口（不覆盖腾讯已有值）
            conflict = sp.cross_check_kline(kl, source, tdx_rows, "tdx",
                                            close_pct=crosscheck_threshold("t7_close_pct", 0.3))
        if kl:
            load_t7(con, ic, kl, source=source, conflict_src=conflict)
        time.sleep(0.3)   # 指数间小睡（同 run_t7）

    t7_tasks = [Task(priority=3, table="index_daily", ts_code=ic,
                     period_or_date=inc_period, tier="P3") for ic in INDEX_CODES]
    stats["t7"] = runner.run(t7_tasks, t7_worker)

    # O2/O3：tasks 占位条目刷成真实 total/done + kline_history 完成态 + T5/T6 条目
    _refresh_incremental_task_view(runner, con, today_s)
    return {"sub": "incremental", "db_path": db_path, "as_of": today_s, **stats,
            "baostock_probe": probe_res, "source_health": health_res,
            "multisource": bool(ohlcv_srcs or adj_srcs),
            "ohlcv_sources": [a.name for a in ohlcv_srcs],
            "adj_sources": [a.name for a in adj_srcs]}


def _refresh_incremental_task_view(runner, con, today_s: str) -> None:
    """O2/O3：incremental 收尾把 progress tasks 视图刷成**真实** total/done。

    - kline_daily/valuation_daily（P3）：total=stock_master 行数（brief 逐字；含
      --codes 冒烟子集时也按全集口径，done=今日已灌数），done==total → state="done"；
    - index_daily（P3）：total=四指数数、done=今日已灌指数数；
    - **kline_history（P2）**：完成态如实标 done——done 明细从 progress ``done`` 键集
      重算（**不依赖已死进程自报**：history 长跑被杀后 entry 停在 state=running，
      incremental/status 路径负责纠正）；total=当前 universe 行数、done=universe 内
      已灌数（退市股 done 键不计入，防 done>total）；done==total → state="done"；
    - **fundamentals_quarterly（T5）**：加进 tasks（total=universe 行数、state=pending、
      备注"history --t5 或增量均可灌"；已跑过 history --t5 的按 f10_full done 键计 done）；
    - **holders_snapshot（T6）**：v6.1.7 起有源 → 真实任务（total=universe 行数、tier=P2、
      done 按固定键 "t6" 计、done==total→done，否则 pending；full 阶段 4 灌数）。

    runner.run 的 _refresh_task_view 只回填**本次队列**分组（--codes 子集时 total=子集数）——
    本函数在其后按全集口径覆盖写 + save_progress（Web /status 3s 轮询可见）。
    """
    from lake.backfill import save_progress
    from lake.ingest.tencent_ingest import INDEX_CODES

    prog = runner.progress
    universe = set(_universe_codes(con))
    universe_n = len(universe)
    inc_period = f"inc:{today_s}"

    def _entry(table: str, tier: str = "P3") -> Dict[str, Any]:
        e = next((t for t in prog["tasks"] if t.get("table") == table), None)
        if e is None:
            e = {"table": table, "tier": tier, "total": 0, "done": 0,
                 "quota_used_today": runner.quota_used_today(),
                 "quota_budget": runner.budget_per_day,
                 "state": "pending", "eta_min": None, "last_error": ""}
            prog["tasks"].append(e)
        return e

    def _done_codes(table: str, period: Optional[str] = None,
                    codes: Optional[set] = None) -> set:
        """该表 done 键的 code 集合（∩ codes——默认 universe；T7 传指数集，
        指数 code 不在股票 universe 里，交集恒空=done 恒 0 的 bug 源）。"""
        out = set()
        for k in runner._done:
            if k[0] != table:
                continue
            if period is not None and k[2] != period:
                continue
            out.add(k[1])
        return out & (codes if codes is not None else universe)

    # T2/T3（per-code+运行日 done 键）：total=universe、done=今日已灌
    for table, period in (("kline_daily", inc_period), ("valuation_daily", today_s)):
        e = _entry(table)
        done_n = len(_done_codes(table, period))
        e["total"] = universe_n
        e["done"] = done_n
        if universe_n > 0 and done_n >= universe_n:
            e["state"] = "done"
        elif e.get("state") in ("running", "stopping"):
            # runner.run 对未灌完的分组把 state 留在 "running"（v6.0.8 视图语义）——
            # 但本函数在**进程收尾**调用，此刻无活跃 writer → 非 done 一律归位 pending
            # （防 Web /status 永久误报"灌数中"；真在跑时 status 走 locked 分支不读此文件）
            e["state"] = "pending"

    # T7（四指数）——done 键按**指数集**计（指数 code 不在股票 universe，不能走默认交集）
    e7 = _entry("index_daily")
    done7 = len(_done_codes("index_daily", inc_period, codes=set(INDEX_CODES)))
    e7["total"] = len(INDEX_CODES)
    e7["done"] = min(done7, len(INDEX_CODES))
    if e7["done"] >= e7["total"]:
        e7["state"] = "done"
    elif e7.get("state") in ("running", "stopping"):
        e7["state"] = "pending"   # 同 T2/T3：收尾态无活跃 writer，非 done 归位 pending

    # kline_history（P2 全史）：完成态如实标 done（不依赖已死进程自报）
    eh = _entry("kline_history", tier="P2")
    hist_done = len(_done_codes("kline_history"))   # 固定键 full_history，跨天累计
    eh["total"] = universe_n
    eh["done"] = hist_done
    if universe_n > 0 and hist_done >= universe_n:
        eh["state"] = "done"
    elif eh.get("state") in ("running", "stopping"):
        # 死进程残留纠正（生产实况：history 被杀后停在 running，done 明细齐全）
        eh["state"] = "pending"

    # T5 fundamentals_quarterly：加进 tasks（pending；已灌的按 f10_full done 键计）
    e5 = _entry("fundamentals_quarterly", tier="P2")
    done5 = len(_done_codes("fundamentals_quarterly"))   # 固定键 f10_full
    e5["total"] = universe_n
    e5["done"] = done5
    e5["state"] = "done" if (universe_n > 0 and done5 >= universe_n) else "pending"
    e5["note"] = "history --t5 或增量均可灌（adata F10 主源）"

    # T6 holders_snapshot：v6.1.7 起有源（SinaClient.fetch_holders → load_t6，full 阶段 4）
    # ——由 v6.1.4 O3 的 no_source/total=0 占位升级为**真实任务**（brief §A 逐字：
    # total=stock_master 行数、tier=P2、state pending→running→done）：
    # done 按 (holders_snapshot, *, "t6") done 键计（∩ universe，防退市股虚增）；
    # done==total → done；否则 running/stopping 归位 pending（收尾态无活跃 writer，
    # 同 T2/T3/T5 口径）。full t6 阶段运行中由 run_t6 置 running + runner 逐任务推进。
    e6 = _entry("holders_snapshot", tier="P2")
    done6 = len(_done_codes("holders_snapshot", period="t6"))   # 固定键 "t6"（run_t6）
    e6["total"] = universe_n
    e6["done"] = done6
    if universe_n > 0 and done6 >= universe_n:
        e6["state"] = "done"
    elif e6.get("state") in ("running", "stopping"):
        e6["state"] = "pending"   # 收尾态无活跃 writer，非 done 归位 pending（同 T2/T3）
    e6["note"] = "SinaClient.fetch_holders 全史前十大股东（full 阶段 4；controller_* 待补源）"

    save_progress(prog, runner.progress_path)


def cmd_incremental(args, con, db_path: str) -> int:
    """incremental 子命令入口（v6.1.4 O2）。"""
    codes = _parse_codes(args.codes)
    summary = run_incremental(con, db_path, codes, args.days)
    _print_summary("incremental", summary)
    return EXIT_OK


# ---------------------------------------------------------------------------
# full（v6.1.6：单按钮全量补齐——一个 backfill 进程顺序跑完所有阶段；
#      v6.1.7：+阶段 4 T6 holders_snapshot）
# ---------------------------------------------------------------------------
# v6.1.6 brief §A：Joel 拍板"三个按钮没必要，一个按钮负责启动停止——只要启动了
# 就是需要把所有历史及现状数据全部都补上"。full = 同一进程内**顺序**执行四阶段，
# 同一把 DuckDB 独占锁贯穿全程（main() 的 LakeLock 包住整个 full → 天然无竞争）：
#   phase1 history（T2 全史 + adj_factor；幂等——done 键全跳过；--t5 不在此处）
#   phase2 P3 增量（kline_daily→valuation_daily→index_daily，run_incremental 原样复用）
#   phase3 T5 fundamentals_quarterly（run_t5 原样复用：adata F10 主源 + BaoStock
#          探测存活时交叉校验；限速 adata_f10_min_interval_s 在 adapter 内）
#   phase4 T6 holders_snapshot（v6.1.7：run_t6——SinaClient.fetch_holders 全史前十大
#          股东 → sina_ingest.load_t6；限速/WAF退避/熔断在 SinaClient 内部 ≥1s/只；
#          done 键 (t6, ts_code) 幂等续传；--no-t6 可跳过——排障用）
#
# 阶段间契约（brief 逐字）：
# - 每阶段开始/结束打 sync.log 段标（===== phase: X =====）——Web spawn 时 stdout
#   append 到 sync.log，log.info 即落该文件；
# - progress tasks 各自 done/state 正常推进（各阶段走 BackfillRunner 既有视图）；
# - **任一阶段异常 → last_error + 该任务 state=error，继续下一阶段**（单表故障
#   不拖死全量）。为什么 try/except 包在 run_full 而非各 run_* 内部：run_history/
#   run_incremental/run_t5 的既有异常语义（HangWatchdogError 穿透、T3 全空中止等）
#   逐字节不动，full 只在编排层兜底——零回归。
# - hang1 R3 停滞看门狗照旧兜底：run_history 内 arm()（主线程调用，full 也在主
#   线程顺序执行各阶段 → 满足 signal.signal 限制）；R3 abort 两路都致 rc=1：
#   ①直接抛出的 HangWatchdogError（BaseException）→ full 的 except Exception
#   **不捕获**它 → 穿透到 main → rc=1；②run_history 内部收尾 handler 转成的
#   hang_watchdog=True 返回值（v6.1.5 cmd_history 契约，不 re-raise）→ run_full
#   检查该标志：phase 记 error + break 不执行后续阶段 → cmd_full rc=1。两路都
#   干净退出（done 键已落盘，下次续传无损）。
_FULL_PHASES = (
    ("history", "T2 全史"),
    ("p3", "P3 增量"),
    ("t5", "T5 基本面"),
    # v6.1.7：第 4 阶段 T6（Joel 拍板方案 A——holders_snapshot 接进 full）。
    # t6 是最后阶段、无后续：单只/单表故障 last_error+state=error 不阻断的纪律
    # 保持一致（收尾视图刷新后重标 error，同前三阶段）；R3 hang_watchdog abort
    # 穿透 rc=1 的 D-1 修复语义不变（run_history 在 phase1，abort 时 break 终止）。
    ("t6", "T6 股东"),
)


def _full_phase_tables(phase: str) -> List[str]:
    """该阶段关联的 progress tasks 表名（error 时标 state=error + last_error 用）。"""
    return {
        "history": ["kline_history"],
        "p3": ["kline_daily", "valuation_daily", "index_daily"],
        "t5": ["fundamentals_quarterly"],
        "t6": ["holders_snapshot"],
    }[phase]


def _full_ensure_entry(prog: Dict[str, Any], table: str, tier: str) -> Dict[str, Any]:
    """取该表 tasks entry；不存在则建（phase 级异常可能在 runner 建 entry 前就抛——
    此时须补建 entry 才能标 error，否则 Web /status 看不到故障）。"""
    e = next((t for t in prog.get("tasks", []) if t.get("table") == table), None)
    if e is None:
        e = {"table": table, "tier": tier, "total": 0, "done": 0,
             "quota_used_today": 0, "quota_budget": 0,
             "state": "pending", "eta_min": None, "last_error": ""}
        prog.setdefault("tasks", []).append(e)
    return e


def _full_set_phase_entries_error(phase: str, db_path: str, exc: BaseException) -> None:
    """progress 该阶段 tasks entry 置 state=error + last_error（幂等，可重复调用）。

    对阶段关联的**每张表** upsert entry 再标 error——收尾 _refresh_incremental_task_view
    会覆盖 T2/T3/T5 entry 的 state，故 full 收尾须重标（见 run_full）；且 phase 级异常
    可能在 runner 建 entry 前就抛 → 这里补建。
    """
    from lake.backfill import load_progress, save_progress

    prog_path = _progress_for_db(db_path)   # None=缺省库→生产默认路径；自定义→库目录
    prog = load_progress(prog_path)
    tiers = {"kline_history": "P2", "fundamentals_quarterly": "P2",
             "holders_snapshot": "P2"}   # v6.1.7：T6 升级真实任务后 tier=P2（run_t6）
    for table in _full_phase_tables(phase):
        e = _full_ensure_entry(prog, table, tiers.get(table, "P3"))
        e["state"] = "error"
        e["last_error"] = f"phase {phase} failed: {exc}"[:500]
    save_progress(prog, prog_path)


def _full_mark_phase_error(phase: str, db_path: str, exc: BaseException) -> None:
    """阶段异常 → progress 该阶段任务 state=error + last_error（brief 逐字）。

    只改**本阶段**的 tasks entry（不碰其它阶段——单表故障不拖死全量）；写失败
    不抛（编排层兜底本身不能成为新故障点，段标日志已记录原因）。
    """
    from lake.backfill import load_progress, save_progress

    try:
        prog_path = _progress_for_db(db_path)   # None=缺省库→生产默认路径；自定义→库目录
        prog = load_progress(prog_path)
        now_s = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        tiers = {"kline_history": "P2", "fundamentals_quarterly": "P2",
                 "holders_snapshot": "P2"}   # v6.1.7：T6 tier=P2（同 _full_set_phase_entries_error）
        for table in _full_phase_tables(phase):
            e = _full_ensure_entry(prog, table, tiers.get(table, "P3"))
            e["state"] = "error"
            e["last_error"] = f"phase {phase} failed: {exc}"[:500]
        prog["phase_errors"] = prog.get("phase_errors", []) + [
            {"phase": phase, "at": now_s, "error": str(exc)[:500]}]
        save_progress(prog, prog_path)
    except Exception as wexc:  # noqa: BLE001 - progress 写失败不阻断（日志已有）
        log.warning("full phase=%s error 标记落盘失败（不阻断，段标日志已记录）: %s",
                    phase, wexc)


def run_full(con, db_path: str, codes: Optional[List[str]],
             start_date: str, end_date: str, days: int,
             t6_enabled: bool = True) -> Dict[str, Any]:
    """v6.1.7 full 模式：单进程顺序执行 history → P3 增量 → T5 → T6（brief §A）。

    :param con: driver 主连接（LakeLock 内；四阶段共用——同锁天然无竞争）。
    :param codes: 股票子集（None=stock_master 全集；透传各阶段）。
    :param start_date/end_date: history 全史窗口（与 cmd_history 同口径）。
    :param days: P3 T7 指数窗口交易日数（run_incremental 的 days，默认 250）。
    :param t6_enabled: **v6.1.7 --no-t6 开关**（默认 True=跑 T6；False=跳过——排障用）。
        跳过时 summary.phases["t6"] = {"ok": True, "skipped": True}（all_ok 不受影响，
        holders_snapshot entry 不动——保持收尾视图刷新后的真实态）。

    :return: ``{"sub": "full", "phases": {phase: stats|error}, ...}``——summary 打印。
        各阶段异常**不抛**（记 phases[phase]["error"] + progress state=error），
        唯一例外 HangWatchdogError（R3 看门狗 abort）：①直接抛出时 BaseException
        穿透到 main → rc=1；②被 run_history 内部收尾 handler 转成返回值
        ``hang_watchdog=True``（v6.1.5 cmd_history 已验收的契约，逐字不动）时由本
        编排层检查该标志 → 该 phase 记 ok=False + state=error + last_error +
        phase_errors 留痕并 **break 不再执行后续阶段**（cmd_full 据此 rc=1）。
    """
    from lake.backfill import BackfillRunner

    summary: Dict[str, Any] = {"sub": "full", "db_path": db_path,
                               "start_date": start_date, "end_date": end_date,
                               "days": days, "phases": {}}
    failed: Dict[str, Any] = {}   # phase → 异常/错误摘要（收尾视图刷新后重标 error 用）
    for phase, label in _FULL_PHASES:
        if phase == "t6" and not t6_enabled:
            # v6.1.7 --no-t6（排障用）：跳过阶段 4——不打段标、不碰 progress/holders_snapshot
            # entry（保持收尾视图刷新后的真实态），summary 留痕 skipped（all_ok 不受影响）。
            summary["phases"]["t6"] = {"ok": True, "skipped": True}
            continue
        log.info("===== phase: %s =====（%s 开始）", phase, label)
        print(f"===== phase: {phase} =====（{label} 开始）")
        t0 = time.monotonic()
        try:
            if phase == "history":
                # 阶段 1：T2 全史 + adj_factor（幂等——done 键全跳过；--t5 不在此处，
                # T5 是独立阶段 3）。run_history 原样复用（Q6/健康探测在其内部）。
                stats = run_history(con, db_path, codes, start_date, end_date)
            elif phase == "p3":
                # 阶段 2：P3 增量（kline_daily→valuation_daily→index_daily，
                # 现有 incremental 逻辑原样复用——含收尾 tasks 视图刷新）。
                stats = run_incremental(con, db_path, codes, days)
            elif phase == "t5":
                # 阶段 3：T5 fundamentals_quarterly（现有 --t5 逻辑抽出复用：
                # adata F10 主源 + BaoStock 探测存活时交叉校验，限速在 adapter）。
                stats = run_t5(con, db_path, codes, BackfillRunner(db_path=db_path))
            else:   # t6（v6.1.7）
                # 阶段 4：T6 holders_snapshot——SinaClient.fetch_holders 全史前十大股东
                # → sina_ingest.load_t6（限速/WAF退避/熔断在 SinaClient 内部 ≥1s/只；
                # done 键 (t6, ts_code) 幂等续传；单只失败不阻断后续）。
                stats = run_t6(con, db_path, codes, BackfillRunner(db_path=db_path))
            summary["phases"][phase] = {"ok": True, "elapsed_s": round(time.monotonic() - t0, 1),
                                        **stats}
            # R3 看门狗 abort（D-1 修复）：run_history 的 `except HangWatchdogError`
            # （L951，v6.1.5 cmd_history 已验收的收尾契约——记 hang_watchdog=True 后
            # return，不抛）会先把 abort 转成普通返回值，run_full 若只靠"BaseException
            # 穿透"就漏掉了这条真实路径（full 继续 p3/t5、rc=0、all_ok=True 误报成功）。
            # 故编排层显式检查该标志：命中 → 该 phase 记 ok=False + state=error +
            # last_error + phase_errors 留痕，**break 不再执行后续阶段**（看门狗线程已在
            # run_history finally stop()，后续阶段无 R3 保护——卡死根因若延续则进程可
            # 无限挂起）。cmd_full 见 phases[phase]["hang_watchdog"] → rc=1（与
            # cmd_history L1897-1898 同口径）。run_history 本身**不 re-raise**——那会改
            # v6.1.5 已验收的 cmd_history rc 语义（回归基线风险）。
            if stats.get("hang_watchdog"):
                _wd_msg = (f"R3 看门狗 abort: {stats.get('last_error', 'progress 停滞')}"
                           f" → full 终止后续阶段（rc=1，done 键已落盘，续传无损）")
                log.error("===== phase: %s =====（%s R3 看门狗 abort，终止 full）: %s",
                          phase, label, stats.get("last_error"))
                print(f"===== phase: {phase} =====（{label} R3 看门狗 abort，终止 full）: "
                      f"{stats.get('last_error')}")
                summary["phases"][phase]["ok"] = False
                summary["phases"][phase]["error"] = _wd_msg[:500]
                failed[phase] = stats.get("last_error") or _wd_msg
                _full_mark_phase_error(phase, db_path,
                                       stats.get("last_error") or _wd_msg)
                break
            # brief"任一阶段异常 → last_error + 该任务 state=error"：runner 吞单任务
            # 异常（记 stats["errors"]、entry.state=error 但**不写 last_error**——
            # _update_task_view 无此字段）→ 编排层补标 last_error（可观测"错在哪"）。
            # p3 阶段 run_incremental 返回嵌套 {t2:{errors}, t3:{...}, t7:{...}}，
            # history/t5 是扁平 stats["errors"]——两种形状都收集。
            errs: List[str] = list(stats.get("errors") or [])
            for _sub in ("t2", "t3", "t7"):
                if isinstance(stats.get(_sub), dict):
                    errs += list(stats[_sub].get("errors") or [])
            if errs:
                failed[phase] = (f"{len(errs)} task(s) failed; first: {errs[0]}"
                                 [:500])
                # summary 口径与异常分支一致：有任务失败 → ok=False + error（all_ok
                # 如实反映"全量是否干净跑完"；progress 里各表 state=error 是权威态）。
                summary["phases"][phase]["ok"] = False
                summary["phases"][phase]["error"] = failed[phase]
                _full_mark_phase_error(phase, db_path, failed[phase])
            log.info("===== phase: %s =====（%s 结束，%.1fs）",
                     phase, label, time.monotonic() - t0)
            print(f"===== phase: {phase} =====（{label} 结束，"
                  f"{round(time.monotonic() - t0, 1)}s）")
        except Exception as exc:  # noqa: BLE001 - 单阶段故障不拖死全量（brief 逐字）
            # ⚠️ HangWatchdogError 是 BaseException（R3 abort）——本 except **不捕获**，
            # 穿透到 main → rc=1（干净退出 + done 键续传无损），与 cmd_history 同语义。
            log.error("===== phase: %s =====（%s 异常，继续下一阶段）: %s",
                      phase, label, exc)
            print(f"===== phase: {phase} =====（{label} 异常，继续下一阶段）: {exc}")
            summary["phases"][phase] = {"ok": False, "error": str(exc)[:500],
                                        "elapsed_s": round(time.monotonic() - t0, 1)}
            failed[phase] = exc
            _full_mark_phase_error(phase, db_path, exc)
    # 收尾：tasks 视图按全集口径归位（incremental 收尾已刷过；这里兜底——若 p3 阶段
    # 异常没跑到 _refresh_incremental_task_view，kline_history/T5/T6 entry 仍如实）。
    try:
        _refresh_incremental_task_view(BackfillRunner(db_path=db_path), con,
                                       _today_beijing())
    except Exception as exc:  # noqa: BLE001 - 视图刷新失败不阻断（summary 已含各阶段结果）
        log.warning("full 收尾 tasks 视图刷新失败（不阻断）: %s", exc)
    # ⚠️ _refresh_incremental_task_view 会**无条件覆盖** fundamentals_quarterly/T2/T3
    # entry 的 state（done==total→done / 否则 pending）——上面刚标的 phase error 会被
    # 冲掉。视图刷新后对失败阶段**重标** error（幂等；brief"该任务 state=error"以最终
    # 落盘为准，Web /status 3s 轮询读到的必须是 error 而非 pending）。
    for phase, exc in failed.items():
        try:
            _full_set_phase_entries_error(phase, db_path, exc)
        except Exception as wexc:  # noqa: BLE001 - 重标失败不阻断（日志已记录）
            log.warning("full 收尾 %s error 重标失败（不阻断）: %s", phase, wexc)
    summary["all_ok"] = all(p.get("ok") for p in summary["phases"].values())
    return summary


def cmd_full(args, con, db_path: str) -> int:
    """full 子命令入口（v6.1.6：单按钮全量补齐；v6.1.7：+阶段 4 T6，--no-t6 可跳）。"""
    from lake.ingest.common import HangWatchdogError

    codes = _parse_codes(args.codes)
    end_date = args.end_date or _today_beijing()
    summary = run_full(con, db_path, codes, args.start_date, end_date, args.days,
                       t6_enabled=bool(getattr(args, "t6", True)))
    _print_summary("full", summary)
    # 单阶段故障不拖死全量（各阶段 error 已记 progress）→ 进程 rc=0；R3 看门狗
    # abort 两路都致 rc=1：①直接抛出的 HangWatchdogError（BaseException）穿透到
    # main → 异常路径 rc=1；②run_history 内部收尾转成的 hang_watchdog=True 返回值
    # ——run_full 已把该 phase 记 error + break，这里**不捕获、向上抛**（与 ① 同路：
    # main 无 handler → 进程干净退出 rc=1；__main__ 的 sys.exit(main()) 下同样 rc=1）。
    # 为什么 raise 而非 return EXIT_RUNTIME：brief §A"HangWatchdogError 不捕获、穿透
    # rc=1"——cmd_history L1897-1898 的 return 口径仅对 `python -m`/sys.exit(main())
    # 调用链成立，driver 被 API/脚本直接调 main() 时 return 值不映射进程码；raise 在
    # 两种调用方式下都保证 rc=1（"卡死自愈退出"与"正常完成"可区分）。run_history 本身
    # **不 re-raise**——cmd_history 的既有行为逐字不变（v6.1.5 已验收，零回归）。
    for _ph, _st in summary["phases"].items():
        if _st.get("hang_watchdog"):
            raise HangWatchdogError(
                f"phase {_ph}: R3 看门狗 abort —— full 终止（done 键已落盘，续传无损）: "
                f"{_st.get('last_error', 'progress 停滞')}")
    return EXIT_OK


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
    # v6.1.4 O3：status 路径刷新 tasks 视图（kline_history 完成态如实标 done、T5/T6 条目）——
    # **能拿到有效 con = 无活跃 writer**（DuckDB 独占写锁下在跑灌数会让 status 的
    # connect 直接 LakeLocked，到不了这里）→ 无条件刷新安全。纠正场景：history 长跑被
    # 强杀后 entry 停在 state=running（生产实况），由 incremental/status 路径负责纠正——
    # **不依赖已死进程自报**。load_progress 每次重读文件 → 纠正后 Web /status（直接读
    # 同一文件）立即可见。
    try:
        from lake.backfill import BackfillRunner

        _refresh_incremental_task_view(BackfillRunner(db_path=db_path), con,
                                       _today_beijing())
        prog = load_progress(prog_path)
    except Exception as exc:  # noqa: BLE001 - status 只读语义：刷新失败不阻断（旧视图）
        log.warning("status tasks 视图刷新失败（不阻断，沿用文件现值）: %s", exc)
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

    sp = sub.add_parser(
        "incremental",
        help="v6.1.4 O2：P3 每日增量（kline_daily 最近缺口 / valuation_daily 最新快照 / index_daily 近 N 日）")
    sp.add_argument("--codes", default=None, help="逗号分隔股票子集（缺省=全集）")
    sp.add_argument("--days", type=int, default=250,
                    help="T7 指数窗口交易日数（默认 250；取数 n=days+30 自然日缓冲）")
    sp.set_defaults(func=cmd_incremental)

    sp = sub.add_parser(
        "full",
        help="v6.1.7：单按钮全量补齐——一个进程顺序跑完 history→P3 增量→T5→T6（同一把锁贯穿全程；"
             "任一阶段异常记 error 后继续下一阶段）")
    sp.add_argument("--codes", default=None, help="逗号分隔股票子集（缺省=全集）")
    sp.add_argument("--start-date", default="1990-01-01", help="全史起点（默认 1990-01-01）")
    sp.add_argument("--end-date", default=None, help="全史终点（缺省=今日北京时间）")
    sp.add_argument("--days", type=int, default=250,
                    help="P3 T7 指数窗口交易日数（默认 250）")
    # v6.1.7：T6 独立开关——默认开（full 含阶段 4）；--no-t6 跳过（排障用）。
    # deprecated 单模式通道（history/incremental/t5）不加 t6（brief 红线：逐字节不变）。
    sp.add_argument("--t6", dest="t6", action="store_true", default=True,
                    help="跑 T6 股东阶段（默认开；显式 --t6 无行为变化）")
    sp.add_argument("--no-t6", dest="t6", action="store_false",
                    help="跳过 T6 股东阶段（排障用；默认不跳）")
    sp.set_defaults(func=cmd_full)

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
    # DEFECT-HANG-1（R3）：看门狗 abort → 非零退出码（区别于"正常跑完/优雅停止"的 rc=0）。
    # Web start_sync 把"5s 内提前退出"判为启动失败——但 R3 abort 发生在长跑后（非启动期），
    # 不受该判定影响；rc=1 让 TL/运维脚本能区分"卡死自愈退出"与"正常完成"。
    if summary.get("hang_watchdog"):
        return EXIT_RUNTIME
    return EXIT_OK


def cmd_reconcile(args, con, db_path: str) -> int:
    codes = _parse_codes(args.codes)
    end_date = args.end_date or _today_beijing()
    # v6.1 Q6：reconcile 也探一次 BaoStock（一致性；结果写日志+progress，按 db_path 派生）
    _run_bs_probe(db_path)
    summary = run_reconcile(con, db_path, codes, args.start_date, end_date)
    _print_summary("reconcile", summary)
    return EXIT_OK


def _install_fault_hook() -> None:
    """DEFECT-HANG-1：faulthandler 诊断钩子（SIGUSR1 → 全线程栈转储到 stderr/sync.log）。

    为什么加：HANG-1 的冻结签名是"全部线程 futex_wait、io/CPU 全平"——纯 Python 层
    锁死锁/挂起，无 ptrace 权限（yama ptrace_scope=2）时 py-spy/gdb/strace 都 attach
    不了。faulthandler.register(SIGUSR1) 是**无需 ptrace** 的线程栈转储通道：
    ``kill -SIGUSR1 <pid>`` → 所有线程 Python 栈写到 stderr（Web spawn 时落 sync.log）→
    直接定位死锁在哪个 lock/调用点。对正常运行零副作用（只注册信号，不占 CPU；
    SIGUSR1 默认行为本就是 kill，注册后变成转储——比被杀好）。仅主线程可注册
    （signal.signal 限制）→ 非主线程静默跳过。
    """
    import faulthandler
    import signal as _sig

    try:
        if threading.current_thread() is threading.main_thread():
            faulthandler.register(_sig.SIGUSR1, all_threads=True)
    except (ValueError, OSError, RuntimeError):  # noqa: BLE001 - 注册失败不阻断灌数
        pass


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    _install_fault_hook()   # DEFECT-HANG-1：SIGUSR1 全线程栈转储（诊断钩子）
    parser = build_parser()
    args = parser.parse_args(argv)

    from lake.conn import LakeInvalidFile, LakeUnavailable  # 延迟 import：--help 不依赖 duckdb

    # B-4：写命令（init/p0/history/full/incremental/reconcile）整段包在 LakeLock(flock) 内——connect + 写入 + close
    # 全持锁，使并发 writer 阻塞等锁而非在 connect 阶段互撞崩溃。status 只读不持锁。
    # full（v6.1.6）：同一把锁贯穿三阶段全程（brief §A"天然无竞争"）。
    write_cmd = args.cmd in ("init", "p0", "history", "full", "incremental", "reconcile")

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
