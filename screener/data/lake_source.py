# -*- coding: utf-8 -*-
"""screener.data.lake_source —— LakeDataFetcher：主筛选数据源切本地 DuckDB 数据湖。

背景（02_code brief + stages/01_research/research_report_lake_source.md A/B/D 节
+ tl_review_lake_source.md 拍板）：
- ``datasource.primary == "lake"`` 时，run_screener 用本类替换 DataFetcher。
  全部 19 个 DataFetcher 方法改为直接 SQL 读 ``data/lake/lake.duckdb``（T1–T7），
  **零 BaoStock / 腾讯K线网络请求**；缓存层（DiskCache/_cached）全 bypass——
  lake 是本地静态快照，PIT 语义由 WHERE date/pub_date <= run_day 承担。

⚠️ 边界纪律（tests/test_lake_zero_import.py AST 零容忍）：
- **禁止 import lake 包**——本模块直接 ``import duckdb``，SQL 自持。
  lake/ 包保持零反向耦合（单向依赖：lake → screener.data，不反过来）。

关键口径决定（TL 拍板，覆盖调研报告开放问题）：
- **D5'（is_st）**：T2.kline_daily.is_st 全表恒 0（load_t2 全部调用点未传 is_st），
  **本类所有 KlineData.is_st 一律取 T1 stock_master.is_st**（按 ts_code 广播），
  绝不读 T2.is_st——否则 ST 剔除静默失效。
- **D2'（roeAvg）**：映射 T5.roe_weighted（加权 ROE 代理平均 ROE，口径差 ~0.48pp
  在 crosscheck 容忍内）。⚠️ 非严格同口径，字段注释已标明。
- **D3'（ocf）**：T5.ocf=NULL → cashflow_data 因子字段全 None（neutral_renorm 兜底），
  Piotroski S2/S4/fcf 维持 N/A。
- **D1'（T7 日历）**：T7 index_daily 仅 2025-07-24→今；run_day < 2025-07 时
  latest_trade_date/trade_dates 无数据 → 返回 None/空（引擎按现状降级，不新增报错）。
- **D6'（industry）**：T1.industry_name 生产实测全部带 CSRC 前缀（如 "C39计算机…"），
  industry() 直接返回 m.industry_name 原文，whitelist ``_industry_code()`` 可生效。

连接策略（brief 规格 1）：
- **只读**：duckdb.connect(path, read_only=True)——生产库纪律"只允许 read_only 读"，
  **绝不降级为 read_write**（backfill flock 期间开 rw 会破坏数据湖且违反纪律）。
- backfill 持锁时 read_only 也连不上（实测两类错误文案：
  "Could not set lock … Conflicting lock is held (PID n)" /
  "Can't open a connection to same database file with a different configuration"）
  → 归一为 DataSourceError（明确提示"数据湖被锁定，backfill 完成后重试"），
  fail-fast 于运行启动阶段（_resolve_run_day 首次查询即触发），绝不带病跑完全市场。
- 库文件缺失/0 字节 → DataSourceError（schema 未建立）。
- 单测通过构造参数 ``db_path`` 注入临时库，不碰生产库。

新鲜度守卫（报告 D 节阈值照用；lake 是静态快照，backfill 停更会用旧数据筛选而不自知）：
- 首次查询校验 MAX(kline_daily.date) 与运行日（未 set_run_day 时用今天）的**交易日差**：
  >3 → warning（universe_notes + log）；>10 → raise DataSourceError 拒绝。
- T5 最新 pub_date 早于最近一个已过披露截止日（Q1→4/30、Q2→8/31、Q3→10/31、
  Q4→次年4/30）→ warning。
- 注记渠道：数据层无法直接写 ScreenResult.data_notes（screener.py/report.py 冻结，
  fetchers.py:169-172 既有约定）→ 落在 self.universe_notes + log.warning，
  与 DataFetcher 半封禁态陈旧回退注记同一渠道。

复权序列（#11 kline_af3_rebuilt / #12 adjfactor_history）——**存储 T2 adj_factor**
（02_code 修正轮 brief，TL 裁决 + Joel 拍板；r_event 推导路径已废弃删除）：
- 因子源 = ``kline_daily.adj_factor``（sina hfq÷raw 灌入、逐日前向填充）。
  TL 独立复测：存储 T2 vs BaoStock ground truth 近 251 交易日窗口 6/7 只 max<2%
  （仅 sh.601688=26% 近期单点损坏 → t2_repair.md 重灌清单）；而 r_event 法同窗口
  偏差 55~81%（T2 close 自身问题 + T4 事件与真实除权不对齐 → 永久水平偏移）。
- ``kline_af3_rebuilt``：af1_close[i] = raw_close[i] × 存储 af（前向填充），
  **必须 ORDER BY date**（漏排序会让前向填充乱序产生假偏差——brief 关键陷阱）。
- adj_factor NULL 行（早期历史，如 sh.601398 23 行 / sh.600018 2000-2006）→
  af1_close=None（MA 计算跳过，与 BaoStock 空 close 防御一致）。
- ``adjfactor_history``/``adjfactor_fetch``：返回存储 T2 的**事件序列**
  （af 变化点 + 首个非 NULL 基准日，BaoStock 布局列序）——与 kline_af3_rebuilt
  同源，保证 Web K线图 hfq view、筛选因子、_adjfactor_r_event（v5.2 除权日交叉校验）
  用同一套因子。
"""
from __future__ import annotations

import calendar
import logging
import os
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from .baostock_client import DataSourceError
from .cache import DiskCache
from .fetchers import DataFetcher, KlineData, to_float, to_int

log = logging.getLogger("screener.data.lake")

# 生产库默认路径（相对仓库根：screener/data/lake_source.py → 上溯 3 层）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_LAKE_DB = os.path.join(_PROJECT_ROOT, "data", "lake", "lake.duckdb")

# T7 交易日历指数（沪深300 之外的基准：上证指数，报告 A 表 #1/#2 口径）
_T7_INDEX_CODE = "sh000001"

# duckdb 连接失败 → "被另一进程锁定" 的文案标记（lake/conn.py 同套 markers +
# 本模块实测补充的 read_only 配置冲突文案；两者都意味着库文件合法、正被写入）
_LOCKED_MARKERS_A = ("Could not set lock", "Conflicting lock")
_LOCKED_MARKER_B = "Can't open a connection to same database file with a different configuration"

# 新鲜度阈值（报告 D 节，brief 规格 7 照用）
_FRESHNESS_WARN_TRADING_DAYS = 3
_FRESHNESS_RAISE_TRADING_DAYS = 10


def _is_locked_db_error(exc: BaseException) -> bool:
    """duckdb 连接异常文案匹配 → 库被另一进程独占（backfill 运行中）。"""
    msg = str(exc)
    return all(m in msg for m in _LOCKED_MARKERS_A) or _LOCKED_MARKER_B in msg


class LakeDataFetcher(DataFetcher):
    """本地 DuckDB 数据湖数据源（primary=lake）。

    与 DataFetcher 的差异：
    - 所有查询直接 SQL（PIT 在 WHERE），不碰 DiskCache/_cached、不发任何网络请求；
    - kline_af3_append/full、adjfactor_append/full、maybe_refresh_adjfactor → no-op
      （lake 数据已在库，读时 date<=run_day 过滤天然幂等）；
    - is_st 一律取 T1（D5'）；roeAvg 映射 roe_weighted（D2'）。

    :param client: BaoStockClient 占位（惰性 login，lake 路径永不触发网络）；
        保持构造签名与 DataFetcher 一致，run_screener 的 finally client.close() 安全。
    :param cache: DiskCache 占位（本类不读写；_kline_first_date 旧路径除外——
        本类提供 kline_af3_first_date 钩子，screener.py 优先走钩子）。
    :param datasource_cfg: 显式 datasource 段（含 exdate_detector.factor_sanity_cap_pct 等）。
    :param db_path: 库文件路径（缺省：env ``SCREENER_LAKE_DB`` → data/lake/lake.duckdb；
        单测/灰度可注入临时库，不碰生产库）。
    """

    def __init__(self, client, cache: DiskCache,
                 datasource_cfg: Optional[Dict[str, Any]] = None,
                 db_path: Optional[str] = None) -> None:
        super().__init__(client, cache, datasource_cfg=datasource_cfg)
        # 路径优先级：显式参数 > env SCREENER_LAKE_DB（单测/灰度注入）> 生产默认
        self.db_path = db_path or os.environ.get("SCREENER_LAKE_DB") or DEFAULT_LAKE_DB
        self._conn = None
        self._freshness_checked = False

    # ------------------------------------------------------------------ 连接
    def _con(self):
        """惰性只读连接（进程内缓存；失败归一 DataSourceError，fail-fast）。"""
        if self._conn is not None:
            return self._conn
        import duckdb  # 惰性：未装 duckdb → ImportError 由上层配置校验兜底

        if not os.path.exists(self.db_path):
            raise DataSourceError(
                f"数据湖库文件不存在: {self.db_path}——backfill 尚未初始化，"
                "无法以 lake 模式运行（先跑 backfill 或切回 primary=tencent）")
        if os.path.getsize(self.db_path) == 0:
            raise DataSourceError(
                f"数据湖库文件为 0 字节（未初始化/损坏）: {self.db_path}")
        try:
            # read_only=True：生产库纪律只读；backfill 持锁时此连接也会失败 → 下方归一。
            self._conn = duckdb.connect(self.db_path, read_only=True)
        except Exception as exc:  # noqa: BLE001 — 仅归一"被锁定"，其余透传
            if _is_locked_db_error(exc):
                raise DataSourceError(
                    f"数据湖被其他进程独占（backfill 灌数进行中）: {self.db_path}——"
                    "read_only 连接也被 DuckDB 单文件锁拒绝。等 backfill 完成后重试，"
                    "或临时切回 primary=tencent") from exc
            raise
        return self._conn

    # ------------------------------------------------------------ 新鲜度守卫
    def _latest_disclosure_deadline(self, ref: date) -> date:
        """ref 之前最近一个已过的季报披露截止日。

        (Y)Q1→(Y)-4/30、Q2→(Y)-8/31、Q3→(Y)-10/31、Q4→(Y+1)-4/30（监管最晚披露时点）。
        候选取近两个年度周期共 8 个截止日，取 ≤ ref 的最大者。
        """
        cands = []
        for y in (ref.year - 1, ref.year):
            cands += [date(y, 4, 30), date(y, 8, 31), date(y, 10, 31)]
        cands.append(date(ref.year + 1, 4, 30))   # (ref.year)Q4 → 次年 4/30
        passed = [d for d in cands if d <= ref]
        return max(passed)

    def _trading_days_between(self, a: date, b: date) -> int:
        """(a, b] 内 T7(sh000001) 交易日数；T7 未覆盖该区间 → 日历天近似（保守方向：
        日历天 ≥ 交易日，更易触发 warning——宁误报不漏报）。"""
        if a >= b:
            return 0
        con = self._con()
        cov = con.execute(
            "SELECT MIN(date), MAX(date) FROM index_daily WHERE index_code=?",
            [_T7_INDEX_CODE],
        ).fetchone()
        if cov and cov[0] is not None and cov[0] <= a and cov[1] is not None and cov[1] >= b:
            n = con.execute(
                "SELECT COUNT(*) FROM index_daily WHERE index_code=? AND date > ? AND date <= ?",
                [_T7_INDEX_CODE, a, b],
            ).fetchone()[0]
            return int(n)
        return (b - a).days

    def _check_freshness(self) -> None:
        """首次查询时的新鲜度守卫（报告 D 节）：>3 交易日 warning；>10 raise。"""
        if self._freshness_checked:
            return
        self._freshness_checked = True
        con = self._con()
        ref = self.run_day or date.today()

        row = con.execute("SELECT MAX(date) FROM kline_daily").fetchone()
        max_kline = row[0] if row else None
        if max_kline is None:
            raise DataSourceError(
                "数据湖 kline_daily 为空——backfill 尚未灌入K线，无法以 lake 模式运行")
        gap = self._trading_days_between(max_kline, ref)
        if gap > _FRESHNESS_RAISE_TRADING_DAYS:
            raise DataSourceError(
                f"数据湖陈旧: kline_daily 最新 {max_kline.isoformat()} 落后运行日 "
                f"{ref.isoformat()} {gap} 个交易日（>{_FRESHNESS_RAISE_TRADING_DAYS}，"
                "约两周停更）——拒绝用旧数据产出误导性筛选结果。先恢复 backfill 再跑")
        if gap > _FRESHNESS_WARN_TRADING_DAYS:
            note = (f"⚠️ 数据湖新鲜度: kline_daily 最新 {max_kline.isoformat()} 落后运行日 "
                    f"{ref.isoformat()} {gap} 个交易日（>{_FRESHNESS_WARN_TRADING_DAYS}，backfill 可能停更）")
            self.universe_notes.append(note)
            log.warning(note)

        row5 = con.execute("SELECT MAX(pub_date) FROM fundamentals_quarterly").fetchone()
        if row5 and row5[0] is not None:
            deadline = self._latest_disclosure_deadline(ref)
            if row5[0] < deadline:
                note = (f"⚠️ 数据湖新鲜度: fundamentals_quarterly 最新 pub_date "
                        f"{row5[0].isoformat()} 早于最近披露截止日 {deadline.isoformat()}"
                        "——基本面可能不完整")
                self.universe_notes.append(note)
                log.warning(note)

    # ------------------------------------------------------------ T1 is_st（D5'）
    def _t1_is_st(self, code: str) -> Optional[int]:
        """T1 stock_master.is_st（D5'：KlineData.is_st 唯一来源；T2.is_st 恒 0 弃用）。"""
        row = self._con().execute(
            "SELECT is_st FROM stock_master WHERE ts_code=?", [code]).fetchone()
        if row is None or row[0] is None:
            return None
        return int(row[0])

    # ------------------------------------------------------------ K线（T2）
    def _af3_rows(self, code: str, start: Optional[str] = None,
                  end: Optional[str] = None) -> List[List[str]]:
        """T2 raw → BaoStock kline_af3 五字段行 [date,code,close,isST,tradestatus]（升序）。

        PIT：end 钳制到 run_day（lake 是静态快照，run_day 之后的行=未来数据，不得泄漏）。
        isST 按 D5' 广播 T1 值；tradestatus 由 volume>0 派生（停牌日有 volume=0 行——
        G2 待生产库确认缺行分支，当前按"有行"口径写 SQL，两分支兼容：缺行时该行自然不存在）。
        close NULL → 空串（rebuild_kline_series 按 None 处理，与 BaoStock 空 close 防御一致）。
        """
        con = self._con()
        end_eff = end
        if self.run_day is not None and (end_eff is None or end_eff > self.run_day.isoformat()):
            end_eff = self.run_day.isoformat()
        sql = ("SELECT k.date, k.close, COALESCE(k.volume, 0), m.is_st "
               "FROM kline_daily k LEFT JOIN stock_master m ON m.ts_code = k.ts_code "
               "WHERE k.ts_code = ?")
        params: List[Any] = [code]
        if start is not None:
            sql += " AND k.date >= ?"
            params.append(start)
        if end_eff is not None:
            sql += " AND k.date <= ?"
            params.append(end_eff)
        sql += " ORDER BY k.date"
        out: List[List[str]] = []
        for d, close, vol, is_st in con.execute(sql, params).fetchall():
            out.append([
                d.isoformat(),
                code,
                "" if close is None else f"{close:.4f}",
                "0" if is_st is None else str(int(is_st)),   # D5'：T1 广播（非 T2.is_st）
                "1" if (vol or 0) > 0 else "0",
            ])
        return out

    def _run_day_bar(self, code: str, day: str) -> Optional[Tuple[Optional[float], int]]:
        """run_day 单根：(close, tradestatus)；无行 → None（停牌缺行/未上市）。"""
        row = self._con().execute(
            "SELECT close, COALESCE(volume, 0) FROM kline_daily WHERE ts_code=? AND date=?",
            [code, day],
        ).fetchone()
        if row is None:
            return None
        return (row[0], 1 if (row[1] or 0) > 0 else 0)

    def _history_row_count(self, code: str) -> int:
        """全历史K线行数（date<=run_day；上市时长代理，含停牌行）。"""
        end = self.run_day.isoformat() if self.run_day is not None else "9999-12-31"
        n = self._con().execute(
            "SELECT COUNT(*) FROM kline_daily WHERE ts_code=? AND date<=?", [code, end]
        ).fetchone()[0]
        return int(n)

    def _make_kline_data(self, code: str, day: str, close: Optional[float],
                         tradestatus: int, n_rows: int) -> KlineData:
        """单根 KlineData（is_st 按 D5' 取 T1；与 DataFetcher._read_kline_data 形状一致）。"""
        return KlineData(
            code=code,
            dates=[day],
            closes=[close if close is not None else 0.0],
            tradestatus=[tradestatus],
            last_date=day,
            n_rows=n_rows,
            current_price=close,
            is_st=self._t1_is_st(code),          # D5'：T1 stock_master.is_st
            run_day_tradestatus=tradestatus,
        )

    # ------------------------- 交易日历（T7；D1'：<2025-07 无数据 → None/空）
    def trade_dates(self, start: str, end: str) -> List[Tuple[str, bool]]:
        """[start, end] 的交易日列表（T7 sh000001 有行 ⟺ 交易日）。

        与 BaoStock 版差异：只返回**交易日**行（BaoStock 返回全部日历天+is_trading 标记）；
        消费方 latest_trade_date 只取 is_trading==1 项 → 行为等价。T7 覆盖缺口
        （D1'：仅 2025-07-24→今）→ 早于覆盖期的区间返回空列表，latest_trade_date 得 None。
        """
        self._check_freshness()
        rows = self._con().execute(
            "SELECT DISTINCT date FROM index_daily WHERE index_code=? AND date BETWEEN ? AND ? "
            "ORDER BY date", [_T7_INDEX_CODE, start, end],
        ).fetchall()
        return [(r[0].isoformat(), True) for r in rows]

    def latest_trade_date(self, on_or_before: date) -> Optional[date]:
        """≤ on_or_before 的最近交易日（T7）。无覆盖（D1'）→ None，引擎按现状降级。"""
        self._check_freshness()
        row = self._con().execute(
            "SELECT MAX(date) FROM index_daily WHERE index_code=? AND date<=?",
            [_T7_INDEX_CODE, on_or_before],
        ).fetchone()
        return row[0] if row and row[0] is not None else None

    # ------------------------- 股票池 / 行业（T1 ⋈ T2）
    def all_stock(self, day: str) -> pd.DataFrame:
        """某交易日全部证券：T1 stock_master LEFT JOIN T2(≤day 最新快照)。

        tradeStatus 由 run_day 是否有成交行派生（停牌/退市 → 0，G2 两分支兼容）；
        delist_date <= day 的退市股剔除（报告 A 表 #3 SQL）。空结果由 build_universe
        既有守卫抛 DataSourceError（语义不变）。
        """
        self._check_freshness()
        rows = self._con().execute(
            "SELECT m.ts_code, "
            "CASE WHEN k.close IS NOT NULL AND COALESCE(k.volume, 0) > 0 THEN 1 ELSE 0 END, "
            "COALESCE(m.name, '') "
            "FROM stock_master m "
            "LEFT JOIN (SELECT ts_code, close, volume FROM kline_daily "
            "            WHERE date = (SELECT MAX(date) FROM kline_daily WHERE date <= ?)) k "
            "  ON k.ts_code = m.ts_code "
            "WHERE m.delist_date IS NULL OR m.delist_date > ?",
            [day, day],
        ).fetchall()
        df = pd.DataFrame(rows, columns=["code", "tradeStatus", "code_name"])
        if len(df):
            df["tradeStatus"] = df["tradeStatus"].map(to_int).fillna(0).astype(int)
        return df

    def industry(self) -> pd.DataFrame:
        """行业分类：T1 当前快照。列 code/code_name/industry（D6'：industry_name 原文，
        含 CSRC 前缀如 "C39计算机、通信和其他电子设备制造业"，whitelist 可直接生效）。"""
        self._check_freshness()
        # COALESCE 兜底：T1 name/industry_name 为 NULL → 空串（防 pandas NaN 在
        # _industry_code `(ind or "").strip()` / str.contains 路径上炸——whitelist 激活时
        # industry_map_all.get(c,"") 拿到 NaN 会 AttributeError）
        rows = self._con().execute(
            "SELECT ts_code, COALESCE(name, ''), COALESCE(industry_name, '') FROM stock_master"
        ).fetchall()
        return pd.DataFrame(rows, columns=["code", "code_name", "industry"])

    # ------------------------- 日K（报告 A 表 #5/#6）
    def kline_window(self, code: str, start: str, end: str, adjustflag: str = "1"
                     ) -> Tuple[List[str], List[float], List[int]]:
        """后复权窗口K线——**无生产调用者**（grep 确认仅 def；backtest 走 DiskCache）。
        lake 实现读 T2 raw（af=3 口径，不复权）：end 钳制 run_day（PIT）。"""
        rows = self._af3_rows(code, start=start, end=end)
        dates: List[str] = []
        closes: List[float] = []
        status: List[int] = []
        for r in rows:
            c = to_float(r[2])
            if c is None:
                continue
            dates.append(r[0])
            closes.append(c)
            status.append(to_int(r[4]) or 0)
        return dates, closes, status

    def kline_run_day(self, code: str, day: str) -> KlineData:
        """运行日单根K线（af=3 不复权）。day > run_day（PIT 越界）→ 空 KlineData。"""
        self._check_freshness()
        empty = KlineData(code=code, dates=[], closes=[], tradestatus=[], last_date="",
                          n_rows=0, current_price=None, is_st=None, run_day_tradestatus=None)
        if self.run_day is not None and day > self.run_day.isoformat():
            return empty
        bar = self._run_day_bar(code, day)
        if bar is None:
            return empty
        close, ts = bar
        return self._make_kline_data(code, day, close, ts, 1)

    # ------------------------- v2 稳定键（报告 A 表 #7–#14）
    def kline_af3_history(self, code: str) -> Optional[Dict[str, Any]]:
        """全历史不复权K线（PIT：date<=run_day）。形状同 DiskCache.get。"""
        self._check_freshness()
        rows = self._af3_rows(code)
        if not rows:
            return None
        return {"columns": list(self.KLINE_AF3_FIELDS), "rows": rows}

    def kline_af3_last_date(self, code: str) -> Optional[str]:
        hit = self.kline_af3_history(code)
        if not hit or not hit["rows"]:
            return None
        last = str(hit["rows"][-1][0]).strip()
        return last or None

    def kline_af3_fetch(self, code: str, start: str, end: str) -> Tuple[List[str], List[List[str]]]:
        """[start, end] 不复权K线（PIT 钳制 run_day），不写缓存（lake 无需追加）。"""
        self._check_freshness()
        return list(self.KLINE_AF3_FIELDS), self._af3_rows(code, start=start, end=end)

    def kline_af3_append(self, code: str, new_rows: List[List[str]]) -> None:
        """no-op：lake 数据已在库，读时 date<=run_day 过滤天然幂等（报告 B 节）。"""
        return None

    def kline_af3_full(self, code: str, start: str, end: str) -> None:
        """no-op：同上。"""
        return None

    def _stored_af_events(self, code: str) -> List[Tuple[str, float]]:
        """存储 T2 adj_factor 的**事件序列**（af 变化点 + 首个非 NULL 基准日）。

        ``SELECT date, adj_factor FROM kline_daily WHERE ts_code=? AND
        adj_factor IS NOT NULL ORDER BY date``——**必须 ORDER BY date**：
        存储 af 是逐日前向填充的（每行都有值），事件=af 相对前一行发生变化的日期；
        漏排序会让"变化点"检测乱序，产生假事件/漏事件（TL 诊断脚本曾因此得出
        假阳性偏差——本 brief 关键陷阱）。

        返回 [(date_iso, af), ...] 升序：首行=首个非 NULL 日（基准 af，通常≈1.0），
        其后每个变化点一行。无 af 数据（全 NULL）→ []。
        与 BaoStock backAdjustFactor 事件序列同构（IPO/首个除权日起累计、单调非降）。
        """
        end = self.run_day.isoformat() if self.run_day is not None else "9999-12-31"
        rows = self._con().execute(
            "SELECT date, adj_factor FROM kline_daily WHERE ts_code=? AND date<=? "
            "AND adj_factor IS NOT NULL ORDER BY date", [code, end],
        ).fetchall()
        events: List[Tuple[str, float]] = []
        prev_af: Optional[float] = None
        for d, af in rows:
            if af is None:
                continue
            # 浮点变化判定：af 是 6 位小数精度灌入（sina round(ratio,6)），
            # 用精确值比较即可（同一次前向填充内逐行复制，无舍入漂移）。
            if prev_af is None or af != prev_af:
                events.append((d.isoformat(), float(af)))
                prev_af = af
        return events

    def adjfactor_history(self, code: str) -> Optional[Dict[str, Any]]:
        """全历史复权因子事件序列（**存储 T2 adj_factor**，BaoStock 布局列序）。

        与 kline_af3_rebuilt **同源**（同一张表同一口径的存储 af；本方法把逐日值
        提取为变化点事件）——Web K线图 hfq view、筛选因子、_adjfactor_r_event
        （v5.2 除权日 r_event 交叉校验）用同一套因子。无 af 数据 → None
        （与 DataFetcher 版"无缓存"语义一致）。
        """
        self._check_freshness()
        events = self._stored_af_events(code)
        if not events:
            return None
        rows = [[code, d, "1.0", f"{af:.6f}", f"{af:.6f}"] for d, af in events]
        return {"columns": ["code", "dividOperateDate", "foreAdjustFactor",
                            "backAdjustFactor", "adjustFactor"], "rows": rows}

    def adjfactor_last_date(self, code: str) -> Optional[str]:
        hit = self.adjfactor_history(code)
        if not hit or not hit["rows"]:
            return None
        last = str(hit["rows"][-1][1]).strip()
        return last or None

    def adjfactor_fetch(self, code: str, start: str, end: str) -> Tuple[List[str], List[List[str]]]:
        """[start, end] 复权因子事件（存储 T2；PIT：date<=run_day）。"""
        self._check_freshness()
        rows = [[code, d, "1.0", f"{af:.6f}", f"{af:.6f}"]
                for d, af in self._stored_af_events(code) if start <= d <= end]
        return ["code", "dividOperateDate", "foreAdjustFactor",
                "backAdjustFactor", "adjustFactor"], rows

    def adjfactor_append(self, code: str, new_rows: List[List[str]]) -> None:
        """no-op：lake 因子已全量在库（存储 T2 adj_factor），无需追加。"""
        return None

    def adjfactor_full(self, code: str, start: str, end: str) -> None:
        """no-op：同上。"""
        return None

    # ------------------------- v4 接缝（lake 下全部 no-op / 直接实现）
    def kline_af3_incremental(self, code: str, run_day: str) -> Optional[KlineData]:
        """stage2 热路径：run_day 单根 KlineData + 全历史行数（上市时长代理）。

        与 DataFetcher 版语义一致（返回运行日快照；无行→None=停牌/未上市），
        但零缓存、零网络：3 个索引点查（T2 run-day 行 / T2 COUNT / T1 is_st）。
        """
        self._check_freshness()
        bar = self._run_day_bar(code, run_day)
        if bar is None:
            return None
        close, ts = bar
        n_rows = self._history_row_count(code)
        if n_rows == 0:
            return None
        return self._make_kline_data(code, run_day, close, ts, n_rows)

    def kline_af3_rebuilt(self, code: str) -> Optional[Dict[str, List]]:
        """全历史 (dates, af3_close, af1_close)（本地、离线）。

        **因子源=存储 T2 adj_factor**（02_code 修正轮 brief，TL 裁决；方案 B）：
        ``af1_close[i] = raw_close[i] × stored_af_forward_filled[i]``——存储 af
        在库内已是逐日前向填充值（sina hfq÷raw 灌入时 forward_fill），直接逐行相乘，
        不经过 rebuild_kline_series 的事件因子逻辑（零事件提取误差）。

        - **必须 ORDER BY date**（brief 关键陷阱：TL 诊断脚本曾漏排序导致前向填充
          乱序产生假阳性偏差）。
        - PIT：date<=run_day（沿用 _af3_rows 的钳制口径）。
        - adj_factor NULL 行（早期历史，如 sh.601398 23 行 / sh.600018 2000-2006）
          → af1_close=None（该日因子未知，不得用前值填充——与 rebuild_kline_series
          对 None close 的处理一致：MA 计算跳过）。
        - 返回形状 {"dates","af3_close","af1_close"} 不变（调用方契约：
          screener._rebuild_window / ttm_pctile closes_map）。
        """
        self._check_freshness()
        end = self.run_day.isoformat() if self.run_day is not None else "9999-12-31"
        rows = self._con().execute(
            "SELECT date, close, adj_factor FROM kline_daily "
            "WHERE ts_code=? AND date<=? ORDER BY date", [code, end],
        ).fetchall()
        if not rows:
            return None
        dates: List[str] = []
        af3: List[Optional[float]] = []
        af1: List[Optional[float]] = []
        for d, c, af in rows:
            dates.append(d.isoformat())
            c_f = float(c) if c is not None else None
            af3.append(c_f)
            if c_f is None or af is None:
                af1.append(None)          # close 缺失 / 该日 adj_factor NULL（因子未知）
            else:
                af1.append(c_f * float(af))
        return {"dates": dates, "af3_close": af3, "af1_close": af1}

    def maybe_refresh_adjfactor(self, code: str, div_records: List[Dict[str, Any]]) -> bool:
        """no-op → False：lake 因子已全量在库（T4 事件实时推导），无需事件驱动刷新。"""
        return False

    # ------------------------- IPO 年钩子（screener._kline_first_date 优先调用）
    def kline_af3_first_date(self, code: str) -> Optional[str]:
        """K线首行日期（IPO 年代理；v5 连续分红新股规则用）。lake=T2 MIN(date)。"""
        row = self._con().execute(
            "SELECT MIN(date) FROM kline_daily WHERE ts_code=?", [code]
        ).fetchone()
        return row[0].isoformat() if row and row[0] is not None else None

    # ------------------------- 分红（T4；报告 A 表 #15）
    def dividend(self, code: str, year: int) -> List[Dict[str, Any]]:
        """单只股票单个自然年（除权年份）的分红记录（BaoStock 口径字段）。

        PIT：ex_date <= run_day（未来除权不可见）。"已实施"近似 = cash_dps>0
        （T4 无 progress 列；报告 A 表 #15 ⚠️ 注）。同 ex_date 多行 dedup 取
        ann_date 最新者（G3：生产库确认后固化 SQL 写法）——SQL 按 (ex_date,
        ann_date DESC) 排序 + Python first-wins，与 em_dividend_records 的
        "plan_notice_date 最新优先"口径一致。
        """
        self._check_freshness()
        end = self.run_day.isoformat() if self.run_day is not None else "9999-12-31"
        rows = self._con().execute(
            "SELECT ex_date, cash_dps FROM dividend_events "
            "WHERE ts_code=? AND ex_date BETWEEN ? AND ? AND ex_date <= ? AND cash_dps > 0 "
            "ORDER BY ex_date, ann_date DESC NULLS LAST", [code, f"{year}-01-01", f"{year}-12-31", end],
        ).fetchall()
        # dedup：同除权日=一次事件（first-wins；SQL 已保证 cash_dps>0 行在前）
        out: List[Dict[str, Any]] = []
        seen = set()
        for ex_d, dps in rows:
            key = ex_d.isoformat()
            if key in seen:
                continue
            seen.add(key)
            d = {f: None for f in self.DIVIDEND_FIELDS}
            d["code"] = code
            d["dividOperateDate"] = key
            d["dividCashPsBeforeTax"] = to_float(dps)
            out.append(d)
        return out

    # ------------------------- 季度基本面（T5；报告 A 表 #16–#19）
    def _t5_row(self, code: str, year: int, quarter: int) -> Optional[Tuple[Any, ...]]:
        """T5 PIT 点查：period=YYYYQn 且 pub_date<=run_day。无行 → None（未披露）。"""
        end = self.run_day.isoformat() if self.run_day is not None else "9999-12-31"
        return self._con().execute(
            "SELECT pub_date, roe_weighted, gross_margin, liability_pct, yoy_pni, npi "
            "FROM fundamentals_quarterly WHERE ts_code=? AND period=? AND pub_date<=?",
            [code, f"{year}Q{quarter}", end],
        ).fetchone()

    @staticmethod
    def _stat_date(year: int, quarter: int) -> str:
        """报告期截止日（BaoStock statDate 口径 YYYYMMDD）。"""
        month = quarter * 3
        last_day = calendar.monthrange(year, month)[1]
        return f"{year}{month:02d}{last_day:02d}"

    def profit_data(self, code: str, year: int, quarter: int) -> Optional[Dict[str, Any]]:
        """T5 → query_profit_data 口径。未披露（PIT 无行）→ None。

        ⚠️ D2'：roeAvg = T5.roe_weighted（**加权 ROE 代理平均 ROE，非严格同口径**，
        口径差 ~0.48pp 在 crosscheck 容忍内——TL 拍板保 ROE 核心因子活口）。
        gpMargin：T5 存百分数 → /100（BaoStock=小数）。npMargin/epsTTM/MBRevenue/
        totalShare/liqaShare：T5 schema 无对应列 → None（neutral_renorm 兜底）。
        """
        self._check_freshness()
        row = self._t5_row(code, year, quarter)
        if row is None:
            return None
        pub_date, roe_weighted, gross_margin, _liab, _yoy, npi = row
        return {
            "code": code,
            "pubDate": pub_date.isoformat() if pub_date else None,
            "statDate": self._stat_date(year, quarter),
            "roeAvg": to_float(roe_weighted),          # D2'：roe_weighted 代理（见 docstring）
            "npMargin": None,
            "gpMargin": (gross_margin / 100.0) if gross_margin is not None else None,
            "netProfit": to_float(npi),
            "epsTTM": None,
            "MBRevenue": None,
            "totalShare": None,
            "liqaShare": None,
        }

    def growth_data(self, code: str, year: int, quarter: int) -> Optional[Dict[str, Any]]:
        """T5 → query_growth_data 口径。YOYPNI=yoy_pni（已是%→小数？BaoStock YOYPNI=小数）。

        ⚠️ 单位：T5.yoy_pni 存百分数（baostock_adapter F2 量纲 ×100 对齐 adata）→ /100。
        """
        self._check_freshness()
        row = self._t5_row(code, year, quarter)
        if row is None:
            return None
        pub_date, _roe, _gm, _liab, yoy_pni, _npi = row
        return {
            "code": code,
            "pubDate": pub_date.isoformat() if pub_date else None,
            "statDate": self._stat_date(year, quarter),
            "YOYEquity": None,
            "YOYAsset": None,
            "YOYNI": None,
            "YOYEPSBasic": None,
            "YOYPNI": (yoy_pni / 100.0) if yoy_pni is not None else None,
        }

    def balance_data(self, code: str, year: int, quarter: int) -> Optional[Dict[str, Any]]:
        """T5 → query_balance_data 口径。liabilityToAsset=liability_pct/100（百分数→小数）。"""
        self._check_freshness()
        row = self._t5_row(code, year, quarter)
        if row is None:
            return None
        pub_date, _roe, _gm, liability_pct, _yoy, _npi = row
        return {
            "code": code,
            "pubDate": pub_date.isoformat() if pub_date else None,
            "statDate": self._stat_date(year, quarter),
            "currentRatio": None,
            "quickRatio": None,
            "cashRatio": None,
            "YOYLiability": None,
            "liabilityToAsset": (liability_pct / 100.0) if liability_pct is not None else None,
            "assetToEquity": None,
        }

    def cashflow_data(self, code: str, year: int, quarter: int) -> Optional[Dict[str, Any]]:
        """T5 → query_cash_flow_data 口径。D3'：T5.ocf=NULL（sina_cf 未接入 backfill）
        → 因子字段全 None（Piotroski S2/S4 N/A、fcf 降级代理；neutral_renorm 兜底）。
        T5 有已披露行 → 返回全 None 因子 dict（"已披露但 ocf 缺失"语义）；无行 → None。
        """
        self._check_freshness()
        row = self._t5_row(code, year, quarter)
        if row is None:
            return None
        pub_date = row[0]
        return {
            "code": code,
            "pubDate": pub_date.isoformat() if pub_date else None,
            "statDate": self._stat_date(year, quarter),
            "CAToAsset": None, "NCAToAsset": None, "tangibleAssetToAsset": None,
            "ebitToInterest": None, "CFOToOR": None, "CFOToNP": None, "CFOToGr": None,
        }
