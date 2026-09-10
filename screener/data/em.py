# -*- coding: utf-8 -*-
"""v5 东财 datacenter-web 客户端（TL D4，brief §1/§2）。

定位：**非官方接口**的受控批量取数通道。纪律（brief §1 + 调研报告 §6 风险1）：
- **串行、限速 >=0.5s/页**（config datasource.em.interval_s，config 层强制下限）、
  pageSize=500、timeout 15s、重试 <=3 次；每页耗时记录日志。
- **契约监控**：返回行数 == 接口 count（不等 → fail-fast）；关键字段非空率
  统计并告警（不静默）；10Y 国债 sanity 区间 [0.5%, 4.0%]（越界告警不静默）。
- **禁止写半截缓存**：全部行取齐并通过契约校验后，临时文件 + os.replace 原子落盘；
  中途失败 → 抛 EMDataError（DataSourceError 子类），不落盘、可幂等重跑。

独立缓存键（brief §2，不得改动任何现有 v4 缓存文件）：
- ``em_dividend_all.csv``        东财分红全表（code, report_date, plan_notice_date,
                                 ex_date, dps_pretax=PRETAX_BONUS_RMB/10, progress）。
                                 ⚠️单位陷阱：EM PRETAX_BONUS_RMB 是"每10股"元，入库 /10
                                 （601398 "10派1.689元"→1.689→每股 0.1689；BaoStock 口径是每股）。
                                 表内含 EX_DIVIDEND_DATE=null 的未实施预案行——取数保留、计算时过滤。
- ``cgb_10y_daily.csv``          东财 10Y 国债全史（date, yield_pct）+ 每日增量 1 页。
                                 ⚠️字段 EMM00166466 标签系**推断**（TL 已独立核对 last=1.6797%
                                 与外部一致；EMM00166462/469 是相邻期限，勿混用）→ 本模块内建
                                 sanity 区间监控防漂移。
- ``holders_top10_{end_date}.csv`` 东财前十大股东（最新报告期，END_DATE 自动探测最大值）。
- ``em_cashflow_{code}.csv``     候选股 OCF/capex 逐只（年度行；接口失败由调用方降级代理）。

零 BaoStock 依赖：本模块只用 requests 访问 datacenter-web.eastmoney.com。
"""
from __future__ import annotations

import csv
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests

from .baostock_client import DataSourceError

log = logging.getLogger("screener.data.em")

EM_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"

# 缓存文件哨兵（与 DiskCache 同语义：区分本程序写入的有效缓存）
EM_CACHE_SENTINEL = "stock-screener-em-cache-v1"


class EMDataError(DataSourceError):
    """东财接口级失败（重试耗尽 / 契约校验不过）。

    继承 DataSourceError：引擎失败守卫按数据源级失败路由（非零退出 + sidecar，
    不写误导性空结果）——与 BaoStock 封禁同语义。
    """


# ---------------------------------------------------------------------------
# 原子 CSV 落盘（临时文件 + os.replace；禁止半截缓存）
# ---------------------------------------------------------------------------

def em_atomic_write_csv(path: str, columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
    """原子写 EM 独立缓存 CSV（首行哨兵 + 表头 + 数据）。"""
    tmp = path + f".tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow([EM_CACHE_SENTINEL])
        w.writerow(list(columns))
        for row in rows:
            w.writerow(["" if v is None else str(v) for v in row])
    os.replace(tmp, path)


def em_read_csv(path: str) -> Optional[Tuple[List[str], List[List[str]]]]:
    """读 EM 独立缓存 CSV；不存在/损坏（无哨兵）→ None。"""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            reader = csv.reader(f)
            sentinel = next(reader, None)
            if not sentinel or sentinel[0] != EM_CACHE_SENTINEL:
                return None
            columns = next(reader, None)
            if columns is None:
                return None
            rows = [row for row in reader]
        return columns, rows
    except (OSError, csv.Error) as exc:  # noqa: BLE001
        log.warning("EM 缓存读取失败 %s: %s（将重新拉取）", path, exc)
        return None


def _clean_date(v: Any) -> str:
    """东财 'YYYY-MM-DD 00:00:00' / null → ISO 日期串（null/空 → ''）。"""
    if v is None:
        return ""
    s = str(v).strip()
    if not s:
        return ""
    return s.split(" ")[0]


def _fnum(v: Any) -> Optional[float]:
    """数值字段 → float（null/空/非法 → None）。"""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------

class EMClient:
    """东财 datacenter-web 受控客户端（串行 + 限速 + 重试 + 契约监控）。"""

    def __init__(self, em_cfg: Dict[str, Any], session: Optional[requests.Session] = None) -> None:
        self.page_size = int(em_cfg["page_size"])
        self.interval_s = float(em_cfg["interval_s"])      # >=0.5（config 层强制）
        self.timeout_s = float(em_cfg["timeout_s"])
        self.max_attempts = int(em_cfg["max_attempts"])   # <=3（config 层强制）
        # 周期性长冷却（防 EM 滑动窗口限流；键缺失→默认值，单测可省略）
        self.cooldown_every_pages = int(em_cfg.get("cooldown_every_pages", 25))
        self.cooldown_seconds = float(em_cfg.get("cooldown_seconds", 6.0))
        self.session = session or requests.Session()
        self.request_count = 0      # 实际发出的 HTTP 请求数（含重试）
        self.page_timings: List[float] = []  # 每页耗时（秒；纪律：记录每页耗时）
        self._last_request_ts = 0.0  # 全局限速锚点（跨 fetch 调用，防逐只取数密集突发）

    # ---------- 单页（重试 + 限速 + 耗时记录） ----------
    def get_page(
        self,
        report_name: str,
        columns: str,
        page_number: int,
        filter_expr: Optional[str] = None,
        sort_columns: str = "",
        sort_types: str = "",
        busy_budget_s: float = 1800.0,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """取单页，返回 (rows, count)。失败重试 max_attempts 次后抛 EMDataError。

        "服务器繁忙"（限流长窗口）→ 外层等待后从本页续取；累计等待超 busy_budget_s
        （brief：某接口 30min 无果 → 停止并报告 TL，不得死磕）→ 抛 EMDataError。
        """
        params: Dict[str, str] = {
            "reportName": report_name,
            "columns": columns,
            "pageSize": str(self.page_size),
            "pageNumber": str(page_number),
        }
        if filter_expr:
            params["filter"] = filter_expr
        if sort_columns:
            params["sortColumns"] = sort_columns
            params["sortTypes"] = sort_types

        last_err = "unknown"
        # 全局限速锚点：任何两次请求（含跨 fetch_all_pages / 逐只取数）间隔 >= interval_s。
        # 逐只现金流取数是独立 get_page 调用，fetch_all_pages 内的 page 间隔管不到它——
        # 若无此锚点，~400 只候选的逐只请求会形成密集突发 → 触发 EM 滑动窗口限流。
        if self.interval_s > 0:
            elapsed = time.time() - self._last_request_ts
            if elapsed < self.interval_s:
                time.sleep(self.interval_s - elapsed)
        # ---- 外层：服务器繁忙耐心等待（EM 限流是**长窗口**，实测一次 ~100 页全表拉取后
        #      持续数分钟；短退避穿不过去）→ 等窗口过期后返回本页行，分页从断点续走。----
        busy_wait = 300.0
        waited_total = 0.0
        while True:
            last_err = "unknown"
            for attempt in range(1, self.max_attempts + 1):
                t0 = time.time()
                try:
                    resp = self.session.get(EM_URL, params=params, timeout=self.timeout_s)
                    self.request_count += 1
                    self._last_request_ts = time.time()
                    dt = time.time() - t0
                    if resp.status_code != 200:
                        last_err = f"HTTP {resp.status_code}"
                    else:
                        body = resp.json()
                        if not body.get("success"):
                            last_err = f"success=false message={body.get('message')!r}"
                        else:
                            result = body.get("result") or {}
                            rows = result.get("data") or []
                            count = int(result.get("count") or 0)
                            self.page_timings.append(dt)
                            log.info(
                                "[EM] %s page=%d rows=%d count=%d %.2fs (累计请求 %d)",
                                report_name, page_number, len(rows), count, dt, self.request_count,
                            )
                            return rows, count
                except (requests.RequestException, ValueError) as exc:
                    last_err = f"{type(exc).__name__}: {exc}"
                if attempt < self.max_attempts:
                    # 短退避（普通失败 1s/2s）；"服务器繁忙"交给外层长等待处理。
                    delay = 1.0 * (2 ** (attempt - 1))
                    log.warning("[EM] %s page=%d 第%d次失败(%s)，%.1fs 后重试",
                                report_name, page_number, attempt, last_err, delay)
                    time.sleep(delay)
            # 内层 <=max_attempts 次均失败：
            busy = ("繁忙" in last_err) or ("busy" in last_err.lower())
            if not busy:
                raise EMDataError(
                    f"东财 {report_name} page={page_number} 重试 {self.max_attempts} 次均失败: {last_err}"
                )
            # 限流长窗口：等待后**从本页续取**（不整表重拉——分页断点保留，已取行不丢）。
            # brief 纪律："某接口 30min 无果 → 停止并报告 TL，不得死磕"→ 累计等待超预算即停。
            if waited_total + busy_wait > busy_budget_s:
                raise EMDataError(
                    f"东财 {report_name} page={page_number} 持续'服务器繁忙'，累计等待 "
                    f"{waited_total:.0f}s 超预算 {busy_budget_s:.0f}s——停止（择时重跑，已取页缓存保留）"
                )
            log.warning(
                "[EM] %s page=%d 遇'服务器繁忙'（限流长窗口），%.0fs 后等待窗口过期再续取本页",
                report_name, page_number, busy_wait,
            )
            time.sleep(busy_wait)
            waited_total += busy_wait
            busy_wait = min(busy_wait * 2.0, 300.0)  # 60→120→240→300(封顶5min)

    # ---------- 全表分页（契约：行数 == count） ----------
    def fetch_all_pages(
        self,
        report_name: str,
        columns: str,
        filter_expr: Optional[str] = None,
        sort_columns: str = "",
        sort_types: str = "1",
        checkpoint_path: Optional[str] = None,
        busy_budget_s: float = 1800.0,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """串行分页取全表。契约监控：累计行数必须 == count，否则 fail-fast（不写缓存）。

        :param checkpoint_path: 可选的**逐页持久检查点**（JSONL，每行一页 ``{"page","count","rows"}``）。
            长全表拉取（~100+ 页）中途被限流/中断时，重跑从最后完整页续取——不丢已取页、
            不整表重拉（整表重拉会再次撞限流窗口）。成功完成后自动删除检查点文件。
        :param busy_budget_s: "服务器繁忙"累计等待预算（秒）。全表级取数用长预算(1800s)；
            逐只取数用短预算(60s) fail-fast → 调用方降级代理（TL D6，避免 N 只候选挂起数小时）。
        """
        all_rows: List[Dict[str, Any]] = []
        count = -1
        page = 1

        # 从检查点续取（自愈：截断的末行=不完整页 → 忽略，该页重取，幂等安全）
        if checkpoint_path and os.path.exists(checkpoint_path):
            ck_rows, ck_count, last_page = _ck_read(checkpoint_path)
            if last_page >= 1:
                all_rows = ck_rows
                count = ck_count
                page = last_page + 1
                log.info("[EM] %s 从检查点续取: 已持久 %d 页 / %d 行 (count=%d)，从第 %d 页继续",
                         report_name, last_page, len(all_rows), count, page)

        while True:
            rows, c = self.get_page(report_name, columns, page, filter_expr, sort_columns,
                                    sort_types, busy_budget_s=busy_budget_s)
            if count < 0:
                count = c
            all_rows.extend(rows)
            # 逐页持久检查点（append+fsync；崩溃最多丢正在写的这一页 → 重取，幂等）
            if checkpoint_path:
                _ck_append_page(checkpoint_path, page, count, rows)
            if len(all_rows) >= count or not rows:
                break
            page += 1
            if self.interval_s > 0:
                time.sleep(self.interval_s)
            # 周期性长冷却：EM 是滑动窗口限流（实测 ~100 页连续请求后"服务器繁忙"，
            # 且限流窗口比单次退避更长）→ 匀速间隔不够，必须周期性降密度。
            if self.cooldown_every_pages > 0 and page % self.cooldown_every_pages == 0:
                log.info("[EM] %s 已取 %d 页 → 长冷却 %.1fs（防滑动窗口限流）",
                         report_name, len(all_rows), self.cooldown_seconds)
                time.sleep(self.cooldown_seconds)
        # 契约监控（brief §1）：行数 == count。不等 = 分页漂移/接口改版 → fail-fast。
        if len(all_rows) != count:
            raise EMDataError(
                f"东财 {report_name} 契约校验失败: 取回 {len(all_rows)} 行 != 接口 count={count}"
                "（分页漂移或接口改版；不落盘，重跑幂等）"
            )
        # 成功完成 → 删除检查点（最终 CSV 已落盘，检查点使命结束）
        if checkpoint_path and os.path.exists(checkpoint_path):
            try:
                os.remove(checkpoint_path)
            except OSError as exc:
                log.warning("[EM] 检查点删除失败 %s: %s", checkpoint_path, exc)
        return all_rows, count


def _ck_append_page(path: str, page: int, count: int, rows: List[Dict[str, Any]]) -> None:
    """追加一页到检查点 JSONL（append + flush + fsync，保证落盘）。"""
    line = json.dumps({"page": page, "count": count, "rows": rows}, ensure_ascii=False)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())


def _ck_read(path: str) -> Tuple[List[Dict[str, Any]], int, int]:
    """读检查点 → (rows 按页序拼接, count, last_page)。截断/坏行忽略（该页重取）。"""
    rows: List[Dict[str, Any]] = []
    count = -1
    last_page = 0
    pages: Dict[int, List[Dict[str, Any]]] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    obj = json.loads(ln)
                except (json.JSONDecodeError, ValueError):
                    continue  # 截断的末行（不完整页）→ 忽略，该页重取
                p = int(obj.get("page", 0))
                if p <= 0 or not isinstance(obj.get("rows"), list):
                    continue
                pages[p] = obj["rows"]
                if count < 0 and isinstance(obj.get("count"), int) and obj["count"] > 0:
                    count = obj["count"]
    except OSError:
        return [], -1, 0
    for p in sorted(pages):
        rows.extend(pages[p])
    last_page = max(pages) if pages else 0
    return rows, count, last_page


# ---------------------------------------------------------------------------
# 三张一次性/增量表（brief §2）
# ---------------------------------------------------------------------------

DIVIDEND_COLUMNS = ("code", "report_date", "plan_notice_date", "ex_date", "dps_pretax", "progress")


def fetch_dividend_all(client: EMClient, cache_dir: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """东财分红全表 → ``em_dividend_all.csv``（幂等：已存在直接读缓存）。

    单位换算（⚠️每10股陷阱）：dps_pretax = PRETAX_BONUS_RMB / 10（元/股，BaoStock 口径）。
    EX_DIVIDEND_DATE=null 的未实施预案行**保留入库**（ex_date=''），计算层过滤。

    :return: (rows[{code, report_date, plan_notice_date, ex_date, dps_pretax, progress}], meta)
    """
    path = os.path.join(cache_dir, "em_dividend_all.csv")
    hit = em_read_csv(path)
    if hit is not None:
        cols, rows = hit
        log.info("[EM] 分红全表命中缓存 %s（%d 行）", path, len(rows))
        return [_row_to_div(r) for r in rows], {"source": "cache", "rows": len(rows), "count": len(rows)}

    raw, count = client.fetch_all_pages(
        "RPT_SHAREBONUS_DET", "ALL",
        sort_columns="SECURITY_CODE,EX_DIVIDEND_DATE", sort_types="1,1",
        # 逐页持久检查点：~114 页长拉取中途被限流/中断时，重跑从最后完整页续取（不整表重拉）
        checkpoint_path=os.path.join(cache_dir, ".em_ck_dividend.jsonl"),
    )
    # 关键字段非空率（契约监控：告警不静默；dps 允许 null=纯送转/转增预案，ex_date 低非空率才异常）
    n = len(raw) or 1
    ex_nonnull = sum(1 for r in raw if r.get("EX_DIVIDEND_DATE"))
    dps_nonnull = sum(1 for r in raw if r.get("PRETAX_BONUS_RMB") is not None)
    log.info("[EM] 分红全表字段非空率: ex_date=%.1f%% dps_pretax=%.1f%%",
             ex_nonnull / n * 100, dps_nonnull / n * 100)
    if ex_nonnull / n < 0.5:
        log.warning("[EM] 契约告警: 分红表 ex_date 非空率 %.1f%% 异常偏低（接口字段漂移?）",
                    ex_nonnull / n * 100)

    rows = []
    for r in raw:
        dps10 = _fnum(r.get("PRETAX_BONUS_RMB"))
        rows.append([
            str(r.get("SECURITY_CODE") or ""),
            _clean_date(r.get("REPORT_DATE")),
            _clean_date(r.get("PLAN_NOTICE_DATE")),
            _clean_date(r.get("EX_DIVIDEND_DATE")),
            "" if dps10 is None else f"{dps10 / 10.0:.6f}",  # ⚠️ /10：每10股 → 每股
            str(r.get("ASSIGN_PROGRESS") or ""),
        ])
    em_atomic_write_csv(path, DIVIDEND_COLUMNS, rows)
    log.info("[EM] 分红全表落盘 %s（%d 行 == count，HTTP 请求 %d 次）", path, len(rows), client.request_count)
    return [_row_to_div(r) for r in rows], {"source": "fetched", "rows": len(rows), "count": count}


def _row_to_div(r: Sequence[Any]) -> Dict[str, Any]:
    code, report_date, plan_notice, ex_date, dps_s, progress = list(r) + [""] * (6 - len(r))
    return {
        "code": str(code), "report_date": str(report_date),
        "plan_notice_date": str(plan_notice), "ex_date": str(ex_date),
        "dps_pretax": _fnum(dps_s), "progress": str(progress),
    }


CGB10Y_COLUMNS = ("date", "yield_pct")
# ⚠️ 10Y 字段标签系推断（TL D4）：EMM00166466 经期限单调序 + 外部交叉验证确认；
# EMM00166462/469 是相邻期限，勿混用。sanity 区间监控防漂移（越界告警不静默）。
CGB10Y_FIELD = "EMM00166466"


def fetch_cgb10y(client: EMClient, cache_dir: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """东财 10Y 国债 → ``cgb_10y_daily.csv``（date, yield_pct）。

    首次全史（~19 页）；之后每次运行**增量取最新 1 页**（SOLAR_DATE 降序 page1），
    合并 date > 已缓存末日的行。sanity：最新值越界 [0.5%,4.0%] → 告警不静默（TL D4）。
    """
    path = os.path.join(cache_dir, "cgb_10y_daily.csv")
    hit = em_read_csv(path)

    if hit is None:
        # ---- 首次全史 ----
        raw, count = client.fetch_all_pages(
            "RPTA_WEB_TREASURYYIELD", f"SOLAR_DATE,{CGB10Y_FIELD}",
            sort_columns="SOLAR_DATE", sort_types="-1",
        )
        by_date: Dict[str, float] = {}
        for r in raw:
            d = _clean_date(r.get("SOLAR_DATE"))
            y = _fnum(r.get(CGB10Y_FIELD))
            if d and y is not None:
                by_date[d] = y
        rows = [[d, f"{by_date[d]:.4f}"] for d in sorted(by_date)]
        em_atomic_write_csv(path, CGB10Y_COLUMNS, rows)
        log.info("[EM] 10Y国债全史落盘 %s（%d 行 == count，HTTP 请求 %d 次）",
                 path, len(rows), client.request_count)
        data = [{"date": d, "yield_pct": by_date[d]} for d in sorted(by_date)]
        _cgb10y_sanity(data, client)
        return data, {"source": "fetched", "rows": len(rows), "count": count}

    # ---- 增量：最新 1 页 ----
    _, cached_rows = hit
    by_date: Dict[str, float] = {}
    for r in cached_rows:
        d, y = str(r[0]), _fnum(r[1])
        if d and y is not None:
            by_date[d] = y
    rows_page, _count = client.get_page(
        "RPTA_WEB_TREASURYYIELD", f"SOLAR_DATE,{CGB10Y_FIELD}", 1,
        sort_columns="SOLAR_DATE", sort_types="-1",
    )
    n_new = 0
    for r in rows_page:
        d = _clean_date(r.get("SOLAR_DATE"))
        y = _fnum(r.get(CGB10Y_FIELD))
        if d and y is not None and d > max(by_date, default=""):
            by_date[d] = y
            n_new += 1
    if n_new:
        rows = [[d, f"{by_date[d]:.4f}"] for d in sorted(by_date)]
        em_atomic_write_csv(path, CGB10Y_COLUMNS, rows)
        log.info("[EM] 10Y国债增量 + %d 行 → %s（累计 %d 行）", n_new, path, len(rows))
    else:
        log.info("[EM] 10Y国债无新增（最新 %s）", max(by_date, default="?"))
    data = [{"date": d, "yield_pct": by_date[d]} for d in sorted(by_date)]
    _cgb10y_sanity(data[-5:], client)  # sanity 只看近期值（历史低利率期不告警）
    return data, {"source": "incremental", "rows": len(by_date), "new": n_new}


def _cgb10y_sanity(recent: Sequence[Dict[str, Any]], client: EMClient) -> None:
    """sanity 区间 [0.5%, 4.0%]（config em.cgb10y_sanity_pct）：越界告警不静默（TL D4）。"""
    lo, hi = _CGB10Y_SANITY
    bad = [x for x in recent if not (lo <= x["yield_pct"] <= hi)]
    if bad:
        log.warning(
            "[EM] 契约告警: 10Y国债最新值越界 sanity[%.2f%%, %.2f%%]: %s"
            "（字段 EMM00166466 标签系推断，疑似漂移——人工核对中债官网）",
            lo, hi, [(x["date"], x["yield_pct"]) for x in bad[-3:]],
        )


# config 层校验过的 sanity 区间（模块级缓存，避免每次读 yaml）
_CGB10Y_SANITY = (0.5, 4.0)


def set_cgb10y_sanity(lo: float, hi: float) -> None:
    """由引擎在运行时注入 config 的 sanity 区间（零硬编码：值来自 strategy.yaml）。"""
    global _CGB10Y_SANITY
    _CGB10Y_SANITY = (float(lo), float(hi))


HOLDERS_COLUMNS = ("code", "name", "holder_name", "hold_ratio", "is_sjkzr", "notice_date", "holder_rank", "end_date")


def detect_holders_end_date(client: EMClient, today: str, min_rows: int) -> str:
    """自动探测最新报告期 END_DATE（brief §2：当前应为 2026-06-30）。

    方法：从"<=today 的最近季末"往前逐季探测 ``filter=(END_DATE='...')`` 的 count，
    首个 count >= min_rows 的季末即为最新完整报告期。

    ⚠️不能直接取全表 max(END_DATE)：该表混有**日常股东变动行**（END_DATE=事件日，
    单只少量行），max 会落在近期某个交易日而非季度报告期（实测 2026-09-09 count 极小）。
    """
    from datetime import date as _date

    t = _date.fromisoformat(today)
    qend = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}
    y, q = t.year, (t.month - 1) // 3 + 1
    # today 尚未到本季季末 → 从上一季开始（未披露完的报告期 count 不足）
    if (t.month, t.day) < qend[q]:
        q -= 1
        if q == 0:
            q = 4
            y -= 1
    candidates: List[str] = []
    for _ in range(8):
        m, d = qend[q]
        candidates.append(f"{y}-{m:02d}-{d}")
        q -= 1
        if q == 0:
            q = 4
            y -= 1
    for cand in candidates:
        try:
            _rows, count = client.get_page(
                "RPT_F10_EH_FREEHOLDERS", "SECURITY_CODE", 1,
                filter_expr=f"(END_DATE='{cand}')",
            )
        except EMDataError as exc:
            log.warning("[EM] holders END_DATE 探测 %s 失败: %s（回退上一季）", cand, exc)
            continue
        if count >= min_rows:
            log.info("[EM] holders 最新报告期探测命中: END_DATE=%s (count=%d)", cand, count)
            return cand
    raise EMDataError(f"holders 最新报告期探测失败：{candidates} 均无 >= {min_rows} 行")


def fetch_holders_top10(client: EMClient, end_date: str, cache_dir: str) -> List[Dict[str, Any]]:
    """东财前十大股东（指定报告期）→ ``holders_top10_{end_date}.csv``（幂等）。

    列含 HOLDER_NAME/HOLD_RATIO/IS_SJKZR/NOTICE_DATE（TL D3 央国企识别输入）。
    PIT：调用方按 NOTICE_DATE <= run_day 过滤（本函数只负责取数落盘）。
    """
    path = os.path.join(cache_dir, f"holders_top10_{end_date}.csv")
    hit = em_read_csv(path)
    if hit is not None:
        _, rows = hit
        log.info("[EM] 前十大股东命中缓存 %s（%d 行）", path, len(rows))
        return [_row_to_holder(r) for r in rows]

    raw, count = client.fetch_all_pages(
        "RPT_F10_EH_FREEHOLDERS",
        "SECURITY_CODE,SECURITY_NAME_ABBR,HOLDER_NAME,HOLD_RATIO,IS_SJKZR,NOTICE_DATE,HOLDER_RANK,END_DATE",
        filter_expr=f"(END_DATE='{end_date}')",
        sort_columns="SECURITY_CODE,HOLDER_RANK", sort_types="1,1",
        # 逐页持久检查点：~113 页长拉取中途被限流/中断时，重跑从最后完整页续取（不整表重拉）
        checkpoint_path=os.path.join(cache_dir, f".em_ck_holders_{end_date}.jsonl"),
    )
    rows = []
    for r in raw:
        rows.append([
            str(r.get("SECURITY_CODE") or ""),
            str(r.get("SECURITY_NAME_ABBR") or ""),
            str(r.get("HOLDER_NAME") or ""),
            "" if _fnum(r.get("HOLD_RATIO")) is None else f"{_fnum(r.get('HOLD_RATIO')):.4f}",
            str(r.get("IS_SJKZR") or "0"),
            _clean_date(r.get("NOTICE_DATE")),
            str(r.get("HOLDER_RANK") or ""),
            _clean_date(r.get("END_DATE")),
        ])
    em_atomic_write_csv(path, HOLDERS_COLUMNS, rows)
    log.info("[EM] 前十大股东落盘 %s（%d 行 == count，HTTP 请求 %d 次）",
             path, len(rows), client.request_count)
    return [_row_to_holder(r) for r in rows]


def _row_to_holder(r: Sequence[Any]) -> Dict[str, Any]:
    vals = list(r) + [""] * (8 - len(r))
    code, name, holder_name, ratio_s, sjkzr, notice, rank, end_date = vals
    return {
        "code": str(code), "name": str(name), "holder_name": str(holder_name),
        "hold_ratio": _fnum(ratio_s), "is_sjkzr": str(sjkzr).strip(),
        "notice_date": str(notice), "holder_rank": str(rank), "end_date": str(end_date),
    }


CASHFLOW_COLUMNS = ("code", "report_date", "notice_date", "date_type_code", "netcash_operate", "construct_long_asset")


def fetch_cashflow(client: EMClient, code6: str, cache_dir: str, busy_budget_s: float = 60.0) -> List[Dict[str, Any]]:
    """候选股 OCF/capex 逐只 → ``em_cashflow_{code}.csv``（幂等；单页足够，~84 期）。

    字段：NETCASH_OPERATE（经营现金流净额）+ CONSTRUCT_LONG_ASSET（购建固定资产等
    支付的现金=capex），绝对额（元）。年度行 = REPORT_DATE 以 -12-31 结尾且
    DATE_TYPE_CODE=001（实测口径）。PIT：调用方按 NOTICE_DATE <= run_day 取报告期。

    ⚠️限流策略与全表拉取不同：**短预算 fail-fast**（busy_budget_s，默认 60s）——
    逐只取数是 N 次独立请求，若每只在"服务器繁忙"时都 patient-wait 30min，N 只候选会
    挂起数小时。TL D6：接口失败才降级代理 cfo_to_np/payout → 限流即抛 EMDataError，
    调用方 catch 后对该股用代理值（不阻塞、不静默）。全表级取数（分红/股东/国债）
    才用长预算+检查点续取。

    :param code6: 6 位证券代码（如 "601398"）
    """
    path = os.path.join(cache_dir, f"em_cashflow_{code6}.csv")
    hit = em_read_csv(path)
    if hit is not None:
        _, rows = hit
        return [_row_to_cashflow(r) for r in rows]

    raw, count = client.fetch_all_pages(
        "RPT_DMSK_FN_CASHFLOW",
        "SECURITY_CODE,REPORT_DATE,NOTICE_DATE,DATE_TYPE_CODE,NETCASH_OPERATE,CONSTRUCT_LONG_ASSET",
        filter_expr=f'(SECURITY_CODE="{code6}")',
        sort_columns="REPORT_DATE", sort_types="-1",
        busy_budget_s=busy_budget_s,  # 逐只短预算：限流即 fail-fast → 调用方降级代理（TL D6）
    )
    rows = []
    for r in raw:
        rows.append([
            str(r.get("SECURITY_CODE") or code6),
            _clean_date(r.get("REPORT_DATE")),
            _clean_date(r.get("NOTICE_DATE")),
            str(r.get("DATE_TYPE_CODE") or ""),
            "" if _fnum(r.get("NETCASH_OPERATE")) is None else f"{_fnum(r.get('NETCASH_OPERATE')):.0f}",
            "" if _fnum(r.get("CONSTRUCT_LONG_ASSET")) is None else f"{_fnum(r.get('CONSTRUCT_LONG_ASSET')):.0f}",
        ])
    em_atomic_write_csv(path, CASHFLOW_COLUMNS, rows)
    log.info("[EM] 现金流 %s 落盘 %s（%d 行 == count）", code6, path, len(rows))
    return [_row_to_cashflow(r) for r in rows]


def _row_to_cashflow(r: Sequence[Any]) -> Dict[str, Any]:
    vals = list(r) + [""] * (6 - len(r))
    code, report_date, notice, dtype, ocf_s, capex_s = vals
    return {
        "code": str(code), "report_date": str(report_date), "notice_date": str(notice),
        "date_type_code": str(dtype),
        "netcash_operate": _fnum(ocf_s), "construct_long_asset": _fnum(capex_s),
    }


def annual_cashflow_rows(rows: List[Dict[str, Any]], run_day: str) -> List[Dict[str, Any]]:
    """从逐只现金流行中取**年度行**（-12-31 且 DATE_TYPE_CODE=001）并按 PIT 过滤。

    PIT 纪律（brief §3）：NOTICE_DATE <= run_day（未披露的年报不可见）。
    返回按 report_date 升序、只保留 (report_date, netcash_operate, construct_long_asset)。
    """
    out = []
    for r in rows:
        if not str(r.get("report_date", "")).endswith("-12-31"):
            continue
        if str(r.get("date_type_code", "")) != "001":
            continue
        notice = str(r.get("notice_date", ""))
        if notice and notice > run_day:
            continue  # PIT：运行日尚未披露
        out.append({
            "report_date": r["report_date"],
            "ocf": r.get("netcash_operate"),
            "capex": r.get("construct_long_asset"),
        })
    out.sort(key=lambda x: x["report_date"])
    return out


def load_cgb10y_asof(cache_dir: str, run_day: str) -> Optional[float]:
    """读取 cgb_10y_daily.csv 中 <= run_day 的最近一行收益率（**小数**，PIT）。

    ⚠️单位：缓存列 yield_pct 存的是**百分数**（1.6797 = 1.6797%），此处 /100 转小数
    ——yield_spread = ttm_yield - rf_10y 两侧都是小数口径。
    无缓存/无 <=run_day 的行 → None（yield_spread 因子记缺失，不阻塞主流程）。
    """
    hit = em_read_csv(os.path.join(cache_dir, "cgb_10y_daily.csv"))
    if hit is None:
        return None
    best_d, best_y = "", None
    for r in hit[1]:
        d, y = str(r[0]), _fnum(r[1])
        if d and y is not None and d <= run_day and d > best_d:
            best_d, best_y = d, y
    return None if best_y is None else best_y / 100.0
