#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase B：历史数据迁移脚本（调研报告 R4 规格；TL Q5 拍板：严格串行分多日）。

**本轮只交付脚本 + ``--dry-run`` 验证**（BaoStock 封禁中，零 live 拉取；
Phase C 由 TL 解封后触发真实执行）。

迁移内容（缺哪补哪——稳定键 = 缓存文件名，存在即跳过）：
1. **历史财报**：回测窗口内各调仓期 annual_year=Y 所需的 Q4 基本面键
   （引擎访问模式同 v2 zscore：profit(Y,Y-1,Y-2 Q4)+growth(Y Q4)
   +balance(Y,Y-1 Q4)+cashflow(Y Q4)），逐股补缺；
2. **分红**：TTM 窗口 [T-365d, T] 覆盖的自然年（缺 2020–2024）逐年补；
3. **退市股**：行业快照 A 股代码 − 当前 K线缓存 = 历史退市/吸收名单，
   每只补 K线(af3)+复权因子+分红年份+基本面键（幸存者偏差修复）；
4. **指数K线**：sh.000300 / sh.000905 / 中证红利试码序列
   （R3：依次试 sh.000922/sz.399324/sh.000015，取有数据且名称含"红利"者）；
5. **allstock 快照**：每个调仓期日一次（严格 PIT 股票池，替代 K线跨度近似）。

红线（TL 硬性要求 + R6 风险 1）：
- **严格串行、单连接**（baostock 进程级单例，禁止并发——>~4 并发即触发
  服务端黑名单，封禁数小时）；
- **sleep >= --min-sleep（默认 0.1s）** 每次查询之间；
- **每批 ``bs.login()`` 探活**（--batch-size 默认 200：批前登录/批末登出，
  断点续跑粒度）；
- **每日预算 --daily-budget（默认 30000）**：当日查询数达到即干净退出
  （次日重跑同一命令自动续传——5y≈116k 查询须分多日滚动）；
- **稳定键缺哪补哪**：缓存文件存在 → 跳过（0 查询），中断后重跑不重复。

用法::

    # 本轮验证（离线，零 live）：打印将执行的查询计划与量级
    .venv/bin/python -m backtest.migrate_history --dry-run

    # Phase C（TL 解封后）：真实执行，分多日滚动
    .venv/bin/python -m backtest.migrate_history                 # 窗口取 config backtest.start/end
    .venv/bin/python -m backtest.migrate_history --start 2021-09-30 --end 2026-09-04 \
        --daily-budget 30000 --min-sleep 0.1 --batch-size 200

退出码：0=完成或干净暂停（达日预算/计划清空）；2=登录失败（封禁中，等待重试）。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# 常量（数据源选择，非策略阈值；量级口径与调研 R4 _r4_budget.py 一致）
# ---------------------------------------------------------------------------
A_SHARE_PREFIXES = ("sh.60", "sh.68", "sz.00", "sz.30")   # A股前缀（同 v2 universe 约定）
KLINE_START = "1990-01-01"                                 # 全历史起点（同 v2 kline_af3_full）
INDEX_CODES = ["sh.000300", "sh.000905"]                   # 沪深300 / 中证500（R3）
DIVIDEND_INDEX_CANDIDATES = ["sh.000922", "sz.399324", "sh.000015"]  # R3 红利试码序列
FUND_TABLES = ("profit", "growth", "balance", "cashflow")   # 四张季度基本面表
DELISTED_LO, DELISTED_HI = 60, 120                          # R4：窗口内退市股数量估计区间

# 缓存键正则（稳定键 = 文件名；与 fetchers.make_cache_name 布局一致）
_RE_FUND = re.compile(r"^(profit|growth|balance|cashflow)_(sh\.\d{6}|sz\.\d{6})_(\d{4})_([1-4])\.csv$")
_RE_DIV = re.compile(r"^dividend_(sh\.\d{6}|sz\.\d{6})_(\d{4})\.csv$")
_RE_KLINE = re.compile(r"^kline_af3_(sh\.\d{6}|sz\.\d{6})\.csv$")
_RE_ADJFACTOR = re.compile(r"^adjfactor_(sh\.\d{6}|sz\.\d{6})\.csv$")
_RE_ALLSTOCK = re.compile(r"^allstock_(\d{4}-\d{2}-\d{2})\.csv$")


# ---------------------------------------------------------------------------
# 纯函数：窗口 → annual_year / 缺失键（与 R4 _r4_budget.py 同口径，离线可测）
# ---------------------------------------------------------------------------
def annual_years_for_window(start: date, end: date) -> List[int]:
    """窗口内各月末调仓期会用到的 distinct annual_year。

    v2 基准年度逻辑：5 月及以后 → y-1，否则 y-2（PIT 回退一年是运行期行为，
    迁移按最坏情况多备一年——缺哪补哪的冗余无害）。
    """
    ys = set()
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        ys.add(y - 1 if m > 4 else y - 2)
        m += 1
        if m == 13:
            m, y = 1, y + 1
    return sorted(ys)


def needed_fund_keys(annual_years: Sequence[int]) -> set:
    """annual_year=Y 的引擎访问模式（v2 zscore，报告 R4）：
    profit(Y,Y-1,Y-2 Q4)+growth(Y Q4)+balance(Y,Y-1 Q4)+cashflow(Y Q4)。"""
    need = set()
    for Y in annual_years:
        for t in FUND_TABLES:
            need.add((t, Y, 4))
        for dy in (Y - 1, Y - 2):
            need.add(("profit", dy, 4))
        need.add(("balance", Y - 1, 4))
    return need


def dividend_years_for_window(start: date, end: date) -> List[int]:
    """TTM 窗口 [T-365d, T] 覆盖的自然年并集（R4：start.year-1 .. end.year）。"""
    return list(range(start.year - 1, end.year + 1))


def monthly_rebalance_days(cal: Sequence[str]) -> List[str]:
    """交易日序列 → 每月最后一个交易日（调仓期日，allstock 快照用）。"""
    by_month: Dict[str, str] = {}
    for d in cal:
        by_month[d[:7]] = d
    return [by_month[m] for m in sorted(by_month)]


# ---------------------------------------------------------------------------
# 缓存盘点（离线）
# ---------------------------------------------------------------------------
def inventory_cache(cache_dir: str) -> Dict[str, Any]:
    """扫描缓存目录：各表已缓存键集合 + K线/行业代码集合。"""
    fund: Dict[str, set] = {t: set() for t in FUND_TABLES}
    div_years: set = set()
    kline_codes: set = set()
    adjfactor_codes: set = set()
    allstock_days: set = set()
    index_klines: set = set()
    for fn in os.listdir(cache_dir):
        m = _RE_FUND.match(fn)
        if m:
            fund[m.group(1)].add((int(m.group(3)), int(m.group(4))))
            continue
        m = _RE_DIV.match(fn)
        if m:
            div_years.add(int(m.group(2)))
            continue
        m = _RE_KLINE.match(fn)
        if m:
            kline_codes.add(m.group(1))
            continue
        m = _RE_ADJFACTOR.match(fn)
        if m:
            adjfactor_codes.add(m.group(1))
            continue
        m = _RE_ALLSTOCK.match(fn)
        if m:
            allstock_days.add(m.group(1))
            continue
        if fn.startswith("kline_af3_sh.000") or fn.startswith("kline_af3_sz.399"):
            index_klines.add(fn[len("kline_af3_"):-len(".csv")])
    return {
        "fund": fund, "div_years": div_years, "kline_codes": kline_codes,
        "adjfactor_codes": adjfactor_codes, "allstock_days": allstock_days,
        "index_klines": index_klines,
    }


def current_pool(cache_dir: str) -> List[str]:
    """当前 A 股池：K线缓存代码 ∩ A股前缀（R4 用 allstock 快照，这里离线等价）。"""
    inv = inventory_cache(cache_dir)
    return sorted(c for c in inv["kline_codes"] if c.startswith(A_SHARE_PREFIXES))


def delisted_candidates(cache_dir: str, candidates_csv: Optional[str] = None) -> List[Tuple[str, str]]:
    """历史退市/吸收股名单：(code, name)。

    优先用 --delisted-csv（调研留样 delisted_candidates_from_industry.csv）；
    否则离线推导：行业快照 A 股代码 − K线缓存代码。
    """
    if candidates_csv and os.path.exists(candidates_csv):
        out: List[Tuple[str, str]] = []
        with open(candidates_csv, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                code = (row.get("code") or "").strip()
                if code.startswith(A_SHARE_PREFIXES):
                    out.append((code, (row.get("name") or "").strip()))
        return sorted(out)
    inv = inventory_cache(cache_dir)
    ind_path = os.path.join(cache_dir, "industry.csv")
    names: Dict[str, str] = {}
    if os.path.exists(ind_path):
        with open(ind_path, newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            next(reader, None)  # sentinel
            header = next(reader, None) or []
            ic = header.index("code") if "code" in header else 1
            in_ = header.index("code_name") if "code_name" in header else 2
            for r in reader:
                if len(r) > max(ic, in_) and r[ic].startswith(A_SHARE_PREFIXES):
                    names[r[ic]] = r[in_]
    out = [(c, names.get(c, "")) for c in sorted(set(names) - inv["kline_codes"])]
    return out


# ---------------------------------------------------------------------------
# 查询计划（有序；每条 = 一个稳定键。dry-run 与真实执行共用同一计划）
# ---------------------------------------------------------------------------
@dataclass
class QueryItem:
    """一条待执行查询（kind + 参数 → 稳定缓存键）。"""

    kind: str                     # profit/growth/balance/cashflow/dividend/
                                  # kline_af3/adjfactor/index_kline/allstock
    code: str = ""
    year: int = 0
    quarter: int = 0              # 基本面用（1-4）
    start: str = ""               # K线/指数用
    end: str = ""

    @property
    def key(self) -> str:
        """稳定缓存键（= 文件名 stem；缺哪补哪的判据）。"""
        if self.kind in FUND_TABLES:
            return f"{self.kind}_{self.code}_{self.year}_{self.quarter}"
        if self.kind == "dividend":
            return f"dividend_{self.code}_{self.year}"
        if self.kind in ("kline_af3", "adjfactor"):
            return f"{self.kind}_{self.code}"
        if self.kind == "index_kline":
            return f"kline_af3_{self.code}"
        if self.kind == "allstock":
            return f"allstock_{self.code}"   # code 字段存调仓期日 ISO
        raise ValueError(f"unknown kind {self.kind}")

    @property
    def label(self) -> str:
        if self.kind in FUND_TABLES:
            return f"{self.kind}_{self.code}_{self.year}Q{self.quarter}"
        if self.kind == "dividend":
            return f"dividend_{self.code}_{self.year}"
        if self.kind in ("kline_af3", "adjfactor"):
            return f"{self.kind}_{self.code}"
        if self.kind == "index_kline":
            return f"index_kline_{self.code} [{self.start}~{self.end}]"
        if self.kind == "allstock":
            return f"allstock_{self.code}"
        return self.kind


@dataclass
class QueryPlan:
    """完整查询计划（分节有序；执行顺序 = 列表顺序，严格串行）。"""

    items: List[QueryItem] = field(default_factory=list)
    sections: Dict[str, int] = field(default_factory=dict)   # 节名 → 条数

    def add(self, section: str, item: QueryItem) -> None:
        self.items.append(item)
        self.sections[section] = self.sections.get(section, 0) + 1


def build_plan(
    cache_dir: str,
    start: date,
    end: date,
    delisted_csv: Optional[str] = None,
    include_delisted: bool = True,
) -> QueryPlan:
    """离线构建查询计划（零 live）：只列**缓存缺失**的稳定键。

    量级口径与调研 R4 一致（5y≈116k–118k；3y≈63k–64k）。
    """
    inv = inventory_cache(cache_dir)
    pool = current_pool(cache_dir)
    n = len(pool)
    ays = annual_years_for_window(start, end)
    need_fund = needed_fund_keys(ays)
    div_years = dividend_years_for_window(start, end)

    plan = QueryPlan()

    # ---- 1) 历史财报（逐股 × 缺失 (table,year,Q4) 键）----
    missing_fund = sorted(k for k in need_fund if (k[1], 4) not in inv["fund"][k[0]])
    for code in pool:
        for t, y, q in missing_fund:
            plan.add("fundamentals", QueryItem(kind=t, code=code, year=y, quarter=q))

    # ---- 2) 分红（逐股 × 缺失自然年）----
    missing_div = sorted(y for y in div_years if y not in inv["div_years"])
    for code in pool:
        for y in missing_div:
            plan.add("dividends", QueryItem(kind="dividend", code=code, year=y))

    # ---- 3) 退市股（K线+因子+分红年份+基本面键；引擎访问模式同 need_fund，逐股缺哪补哪）----
    if include_delisted:
        dl = delisted_candidates(cache_dir, delisted_csv)
        for code, _name in dl:
            plan.add("delisted_kline", QueryItem(kind="kline_af3", code=code,
                                                 start=KLINE_START, end=end.isoformat()))
            plan.add("delisted_adjfactor", QueryItem(kind="adjfactor", code=code,
                                                     start=KLINE_START, end=end.isoformat()))
            for y in div_years:
                plan.add("delisted_dividend", QueryItem(kind="dividend", code=code, year=y))
            for t, y, q in sorted(need_fund):
                # 退市股按定义不在当前池 → 其财报缓存文件不存在（盘点是表级、无逐股键），
                # 全部 need_fund 键入计划；真实执行时"缓存已存在→跳过"兜底（缺哪补哪）
                plan.add("delisted_fundamental",
                         QueryItem(kind=t, code=code, year=y, quarter=q))

    # ---- 4) 指数K线（沪深300/500 + 红利试码序列；已缓存跳过）----
    for bcode in INDEX_CODES + DIVIDEND_INDEX_CANDIDATES:
        if bcode not in inv["index_klines"]:
            plan.add("index_kline", QueryItem(kind="index_kline", code=bcode,
                                              start=start.isoformat(), end=end.isoformat()))

    # ---- 5) allstock 快照（每调仓期日；交易日序列离线用参考股K线近似）----
    ref = os.path.join(cache_dir, "kline_af3_sh.601398.csv")
    cal: List[str] = []
    if os.path.exists(ref):
        with open(ref, newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            next(reader, None)  # sentinel
            header = next(reader, None) or []
            idate = header.index("date") if "date" in header else 0
            for r in reader:
                d = (r[idate] if len(r) > idate else "").strip()
                if start.isoformat() <= d <= end.isoformat():
                    cal.append(d)
    for day in monthly_rebalance_days(cal):
        if day not in inv["allstock_days"]:
            plan.add("allstock", QueryItem(kind="allstock", code=day))

    return plan


# ---------------------------------------------------------------------------
# 断点续跑状态（稳定键集合；JSON 原子写）
# ---------------------------------------------------------------------------
class Checkpoint:
    """已完成查询的稳定键集合（--state 文件；中断重跑自动跳过）。"""

    def __init__(self, path: str) -> None:
        self.path = path
        self.done: set = set()
        self.executed_total = 0
        self.skipped_cached = 0
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
            self.done = set(doc.get("done", []))
            self.executed_total = int(doc.get("executed_total", 0))

    def save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                       "executed_total": self.executed_total,
                       "skipped_cached": self.skipped_cached,
                       "done": sorted(self.done)}, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    def mark_done(self, key: str) -> None:
        self.done.add(key)


# ---------------------------------------------------------------------------
# 真实执行（Phase C；baostock 惰性导入 → dry-run/计划构建零 live 依赖）
# ---------------------------------------------------------------------------
def execute_plan(
    plan: QueryPlan,
    cache_dir: str,
    min_sleep: float = 0.1,
    batch_size: int = 200,
    daily_budget: int = 30000,
    state_path: Optional[str] = None,
) -> int:
    """严格串行执行计划（单连接、sleep≥min_sleep、每批 login 探活、日预算暂停）。

    :return: 退出码（0=完成/干净暂停；2=登录失败）。
    """
    import baostock as bs  # 惰性：dry-run 不触发网络依赖

    from screener.data.cache import DiskCache, make_cache_name

    cache = DiskCache(cache_dir)
    ckpt = Checkpoint(state_path) if state_path else None

    # 当日计数（跨日重跑归零；--daily-budget 控制单日风险敞口）
    day_count = 0
    batch_since_login = 0
    logged_in = False

    def _login() -> bool:
        nonlocal logged_in, batch_since_login
        lg = bs.login()
        if lg.error_code != "0":
            print(f"[ABORT] baostock login 失败: {lg.error_msg}"
                  f"（error_code={lg.error_code}；封禁中 → 稍后重跑同一命令续传）",
                  file=sys.stderr)
            return False
        logged_in = True
        batch_since_login = 0
        print(f"[LOGIN] ok（探活，批粒度 {batch_size}）")
        return True

    def _logout() -> None:
        nonlocal logged_in
        if logged_in:
            bs.logout()
            logged_in = False

    # 每批前 login 探活（TL 红线：断网/封禁快速失败，不带病连发）
    if not _login():
        return 2

    pending = [it for it in plan.items
               if ckpt is None or it.key not in ckpt.done]
    print(f"[PLAN] 计划 {len(plan.items)} 条；已完成(断点) {len(plan.items) - len(pending)} 条"
          f"；本次待执行 {len(pending)} 条（日预算 {daily_budget}，sleep≥{min_sleep}s）")

    try:
        for i, it in enumerate(pending):
            # ---- 稳定键缺哪补哪：缓存已存在 → 跳过（0 查询）----
            # it.key 本身就是完整稳定键（文件名 stem，无 .csv）→ 直接 has(it.key)
            if cache.has(it.key):
                if ckpt:
                    ckpt.done.add(it.key)
                continue

            if not logged_in and not _login():
                return 2

            ok = False
            try:
                ok = _run_one(bs, cache, it)
            except Exception as exc:  # noqa: BLE001 - 单条失败不中断整批（记日志续跑）
                print(f"[WARN] {it.label} 失败: {exc}（跳过，下轮重试）", file=sys.stderr)

            day_count += 1
            batch_since_login += 1
            # ok=True 写缓存 / "empty"=查询成功但无数据（历史年度=永久缺失，标记完成不再重试）
            # ok=False 失败 → 不标记，下轮重试（断点续跑语义）
            if ckpt and ok is not False:
                ckpt.mark_done(it.key)
            if ckpt and ok is True:
                ckpt.executed_total += 1
            # 断点落盘节流：每 50 条一次（JSON 全量写，逐条写会拖慢串行速度）
            if ckpt and (i % 50 == 49 or not ok):
                ckpt.save()
            if day_count >= daily_budget:
                print(f"[PAUSE] 当日预算 {daily_budget} 条用尽 → 干净退出"
                      f"（次日重跑同一命令自动续传）")
                break
            time.sleep(max(0.1, min_sleep))

            if batch_size > 0 and batch_since_login >= batch_size:
                _logout()
                print(f"[BATCH] {batch_size} 条完成 → 登出，下批重新 login 探活")
                if not _login():
                    return 2
    finally:
        _logout()
        if ckpt:
            ckpt.save()

    print(f"[DONE] 本次执行 {day_count} 条；累计 "
          f"{ckpt.executed_total if ckpt else day_count} 条（断点状态已保存）")
    return 0


def _run_one(bs, cache: Any, it: QueryItem):
    """执行单条查询并写缓存（列布局与 v2 fetchers 完全一致 → 回测层直接可读）。

    :return: True=已写缓存；"empty"=查询成功但无数据（历史年度/退市股=永久缺失，
             调用方标记完成不再重试）；False=失败（下轮重试）。
    """
    from screener.data.cache import make_cache_name

    def _fetch(query_fn, label: str, **kwargs) -> Tuple[List[str], List[List[str]]]:
        rs = query_fn(**kwargs)
        if rs.error_code != "0":
            raise RuntimeError(f"{label} error_code={rs.error_code} {rs.error_msg}")
        rows: List[List[str]] = []
        while rs.next():
            rows.append(rs.get_row_data())
        return list(rs.fields), rows

    if it.kind in FUND_TABLES:
        fn = getattr(bs, f"query_{it.kind}_data")
        _, rows = _fetch(fn, it.label, code=it.code, year=it.year, quarter=it.quarter)
        # 列布局同 v2 fetchers（单季一行）；历史年度无数据=永久缺失 → "empty"
        if not rows:
            return "empty"
        cols = {"profit": ["code", "pubDate", "statDate", "roeAvg", "npMargin", "gpMargin",
                           "netProfit", "epsTTM", "MBRevenue", "totalShare", "liqaShare"],
                "growth": ["code", "pubDate", "statDate", "YOYEquity", "YOYAsset",
                           "YOYNI", "YOYEPSBasic", "YOYPNI"],
                "balance": ["code", "pubDate", "statDate", "currentRatio", "quickRatio",
                            "cashRatio", "YOYLiability", "liabilityToAsset", "assetToEquity"],
                "cashflow": ["code", "pubDate", "statDate", "CAToAsset", "NCAToAsset",
                             "tangibleAssetToAsset", "ebitToInterest", "CFOToOR",
                             "CFOToNP", "CFOToGr"]}[it.kind]
        cache.put(it.key, cols, rows)   # it.key = 完整稳定键（= make_cache_name(kind,code,year,q)）
        return True

    if it.kind == "dividend":
        _, rows = _fetch(bs.query_dividend_data, it.label, code=it.code, year=it.year,
                         yearType="operate")
        # 空年也要写缓存（稳定键"已查过"→ 不再重复查询；回测层读空=无分红）
        cache.put(it.key,
                  ["code", "dividPreNoticeDate", "dividAgmPumDate", "dividPlanAnnounceDate",
                   "dividPlanDate", "dividRegistDate", "dividOperateDate", "dividPayDate",
                   "dividStockMarketDate", "dividCashPsBeforeTax", "dividCashPsAfterTax",
                   "dividStocksPs", "dividCashStock", "dividReserveToStockPs"], rows)
        return True

    if it.kind == "kline_af3":
        # v2 五字段布局（date,code,close,isST,tradestatus）——与现有缓存/v2 fetcher 完全一致。
        # 只请求 5 字段：15 字段逐行传输慢 ~4x（v2 实测 28-41s vs 5.5-9.3s/股）。
        fields = "date,code,close,isST,tradestatus"
        _, rows = _fetch(bs.query_history_k_data_plus, it.label, code=it.code, fields=fields,
                         start_date=it.start, end_date=it.end, frequency="d", adjustflag="3")
        if not rows:
            return "empty"   # 退市股无K线 → 永久缺失，不再重试
        cache.put(it.key, ["date", "code", "close", "isST", "tradestatus"], rows)
        return True

    if it.kind == "adjfactor":
        # 复权因子用专用接口 query_adjust_factor（与 v2 adjfactor_fetch 一致）
        _, rows = _fetch(bs.query_adjust_factor, it.label, code=it.code,
                         start_date=it.start, end_date=it.end)
        if not rows:
            return "empty"   # 无除权事件 → 永久缺失（F≡1），不再重试
        rows.sort(key=lambda r: str(r[1]).strip())   # 按除权日升序（稳定键约定）
        cache.put(it.key, ["code", "dividOperateDate", "foreAdjustFactor",
                           "backAdjustFactor", "adjustFactor"], rows)
        return True

    if it.kind == "index_kline":
        fields = "date,code,close"
        _, rows = _fetch(bs.query_history_k_data_plus, it.label, code=it.code, fields=fields,
                         start_date=it.start, end_date=it.end, frequency="d", adjustflag="3")
        if not rows:
            return False  # 红利试码序列：该代码无数据 → 试下一个（R3 降级链）
        cache.put(it.key, ["date", "code", "close"], rows)   # it.key = kline_af3_{index}
        return True

    if it.kind == "allstock":
        _, rows = _fetch(bs.query_all_stock, it.label, day=it.code)
        if not rows:
            return False
        cache.put(it.key, ["code", "tradeStatus", "code_name"], rows)   # it.key = allstock_{day}
        return True

    raise ValueError(f"unknown kind {it.kind}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _print_plan_summary(plan: QueryPlan, start: date, end: date, pool_n: int) -> None:
    total = len(plan.items)
    print("=" * 72)
    print(f"迁移查询计划（dry-run，零 live）：窗口 {start} ~ {end}")
    print("=" * 72)
    for section, cnt in plan.sections.items():
        print(f"  {section:<20s} {cnt:>8,d} 条")
    print("-" * 72)
    print(f"  {'TOTAL':<20s} {total:>8,d} 条")
    core = plan.sections.get("fundamentals", 0) + plan.sections.get("dividends", 0)
    dl_total = sum(v for k, v in plan.sections.items() if k.startswith("delisted"))
    # R4 量级对照（5y≈116k–118k / 3y≈63k–64k）——R4 的退市股按窗口内 60–120 只估下界，
    # 本脚本取行业快照全量历史退市名单（超集）→ 对照口径 = 核心缺口 + 退市节，
    # 与 R4 总量偏差即"超集 vs 窗口估计"的差值（多拉无害，真实执行缺哪补哪）。
    span_y = (end - start).days / 365.25
    lo, hi = (116_000, 118_000) if span_y >= 4 else (63_000, 65_000)
    total_est = core + dl_total   # index/allstock 仅 ~65 条，忽略不计
    tag = "✓ 与 R4 量级一致" if lo <= total_est <= hi * 1.15 else "⚠ 偏离 R4 量级，请核对"
    print(f"  R4 对照（{span_y:.1f}y 窗口）: {lo:,d}–{hi:,d} vs 本计划(核心+退市节)={total_est:,d} → {tag}")
    print(f"  其中核心缺口(财报+分红, N×缺失键/年) = {core:,d}"
          f"（R4: 5y=114,730 / 3y=62,580，逐股盘点口径）")
    if dl_total:
        n_dl = plan.sections.get("delisted_kline", 0)
        print(f"  退市股节 = {dl_total:,d}（行业快照全量历史退市名单 {n_dl} 只 × "
              f"[K线+因子+分红年+财报键]；R4 按窗口内 60–120 只估下界，"
              f"本脚本取全量超集——缺哪补哪，多拉无害）")
    for rate in (0.5, 0.7):
        print(f"  串行 @ {rate}s/条: {total * rate / 3600:.1f}h"
              f"（分 {(total // 30000) + 1} 日 × ≤30k，sleep≥0.1s，每批 login 探活）")
    print("-" * 72)
    print("  计划样例（前 5 / 后 3）：")
    for it in plan.items[:5]:
        print(f"    {it.label}")
    if len(plan.items) > 8:
        print("    ...")
    for it in plan.items[-3:]:
        print(f"    {it.label}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Phase B 历史数据迁移（严格串行/断点续跑；--dry-run 零 live）")
    ap.add_argument("--config", default="config/strategy.yaml", help="strategy.yaml 路径")
    ap.add_argument("--cache-dir", default=None, help="缓存目录（默认取 config data.cache_dir）")
    ap.add_argument("--start", default=None, help="窗口起 YYYY-MM-DD（默认 backtest.start）")
    ap.add_argument("--end", default=None, help="窗口止 YYYY-MM-DD（默认 backtest.end）")
    ap.add_argument("--delisted-csv", default=None,
                    help="退市股名单 CSV（调研留样；缺省=行业快照−K线缓存离线推导）")
    ap.add_argument("--no-delisted", action="store_true", help="跳过退市股节")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印查询计划，不实际拉取（本轮验证模式）")
    ap.add_argument("--min-sleep", type=float, default=0.1, help="每次查询间 sleep（秒，≥0.1）")
    ap.add_argument("--batch-size", type=int, default=200,
                    help="每批条数（批末登出、下批 login 探活；0=不重登录）")
    ap.add_argument("--daily-budget", type=int, default=30000,
                    help="单日查询预算（达到即干净退出，次日续传）")
    ap.add_argument("--state", default=None, help="断点状态 JSON 路径（默认 logs/migrate_state.json）")
    args = ap.parse_args(argv)

    # 窗口/缓存目录：CLI > config backtest/data 段（config 相对路径以 CWD 解析，
    # 与 v2 CLI 约定一致——从仓库根运行）
    start_s, end_s = args.start, args.end
    cache_dir = args.cache_dir
    if not os.path.exists(args.config) and os.path.exists("config/strategy.yaml"):
        args.config = "config/strategy.yaml"
    if (not start_s or not end_s or not cache_dir):
        try:
            from screener.config import load_config
            cfg = load_config(args.config)
            bt = cfg.get("backtest") or {}
            start_s = start_s or str(bt.get("start"))
            end_s = end_s or str(bt.get("end"))
            cache_dir = cache_dir or str((cfg.get("data") or {}).get("cache_dir", "cache"))
        except Exception as exc:  # noqa: BLE001
            print(f"[ERROR] 读取 config 失败: {exc}", file=sys.stderr)
            return 2

    if not start_s or not end_s or not cache_dir:
        ap.error("需要 --start/--end/--cache-dir（或可读取的 config backtest/data 段）")
    start = date.fromisoformat(start_s)
    end = date.fromisoformat(end_s)
    if args.min_sleep < 0.1:
        print("[WARN] --min-sleep < 0.1s 违反 TL 红线，已强制为 0.1", file=sys.stderr)
        args.min_sleep = 0.1

    plan = build_plan(cache_dir, start, end,
                      delisted_csv=args.delisted_csv,
                      include_delisted=not args.no_delisted)
    pool_n = len(current_pool(cache_dir))
    print(f"[INFO] 缓存 {cache_dir}：A股池 N={pool_n}；"
          f"annual_years={annual_years_for_window(start, end)}")
    _print_plan_summary(plan, start, end, pool_n)

    if args.dry_run:
        print("\n[DRY-RUN] 未执行任何 live 查询（BaoStock 封禁中；Phase C 由 TL 触发）。")
        return 0

    state = args.state or os.path.join("logs", "migrate_state.json")
    os.makedirs(os.path.dirname(state) or ".", exist_ok=True)
    return execute_plan(plan, cache_dir, min_sleep=args.min_sleep,
                        batch_size=args.batch_size, daily_budget=args.daily_budget,
                        state_path=state)


if __name__ == "__main__":
    sys.exit(main())
