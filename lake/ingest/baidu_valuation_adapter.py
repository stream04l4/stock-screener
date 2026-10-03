# -*- coding: utf-8 -*-
"""lake/ingest/baidu_valuation_adapter.py —— T3 估值历史一次性回填源（百度股市通）。

**定位（v6.3.6，Joel 定案 T3 选 A：全市场 PE-TTM/PB/息率 3 列全史）**：
低频/一次性回填源，**不进每日主链路**（日更当前快照仍走腾讯 T3）。同 T9
macro_rf（东财中债）纪律：akshare 仅作回填，主链路不依赖。

**源约束（2026-10-03 spike 验证）**：
- 百度股市通（akshare ``stock_zh_valuation_baidu``）估值序列为**月度**粒度
  （600000 27 年 615 行≈23/年，月末锚点；日线级估值全史无免费可靠源——
  Tushare daily_basic 1次/分钟且 1991 年 PE/PB 字段为空，已排除）。
- 深度 = 各股**上市日**（000001 平安银行 1992-03-14 起；上市初期 ~1 年缺
  失是源限制，估值分位按个股存续期计算，无实质影响）。
- 3 指标各 1 次调用（总市值/市盈率(TTM)/市净率），~2-3s/次。

**单位对齐**（同 T3 tencent 口径，lake/ddl.py valuation_daily）：
- total_mv = 亿元（baidu 总市值单位即亿）
- pe_ttm / pb = 倍
- ttm_yield_pct = **本地派生**（T4 分红 TTM DPS ÷ 当日 close，%），非 baidu
  指标——与 factors.py 主用口径一致，且 1991 起有分红史的股才有值（无分红=NULL）
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from .common import now_ts

log = logging.getLogger("lake.ingest.baidu_valuation")

# baidu 指标名 → valuation_daily 列名
INDICATOR_COLS: Dict[str, str] = {
    "市盈率(TTM)": "pe_ttm",
    "市净率": "pb",
    "总市值": "total_mv",
}

# 源标记（回填行；当前快照行 source=tencent 不受影响）
SOURCE = "bd_static"

# 限速：3 次调用间小睡（百度 gushitong 非登录接口，温和限速防风控）
_CALL_SLEEP_S = 1.0


def baidu_available() -> bool:
    try:
        import akshare  # noqa: F401
        return True
    except ImportError:
        return False


def code6(ts_code: str) -> str:
    """sh.600000/sz.000001 → 600000/000001（baidu 只认 6 位纯数字）。"""
    return ts_code.split(".")[-1] if "." in ts_code else ts_code


def fetch_valuation_baidu(symbol6: str, sleep: float = _CALL_SLEEP_S,
                          _ak=None) -> Dict[str, List[Tuple[str, float]]]:
    """拉单股全史估值（月度锚点）：{pe_ttm/pb/total_mv: [(date_iso, value), ...]}。

    :param _ak: 测试注入用（monkeypatch akshare 模块）。
    :raises RuntimeError: akshare 未装 / 任一指标调用失败。
    """
    if _ak is None:
        if not baidu_available():
            raise RuntimeError("akshare 未安装（T3 历史回填源；uv pip install akshare）")
        import akshare as _ak
    out: Dict[str, List[Tuple[str, float]]] = {}
    for indicator, col in INDICATOR_COLS.items():
        try:
            df = _ak.stock_zh_valuation_baidu(symbol=symbol6, period="全部",
                                              indicator=indicator)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"baidu 估值 {symbol6} {indicator} 失败: {exc}") from exc
        rows: List[Tuple[str, float]] = []
        if df is not None and len(df):
            for _, r in df.iterrows():
                d, v = r["date"], r["value"]
                if v is None or v != v:  # NaN
                    continue
                rows.append((str(d)[:10], float(v)))
        rows.sort(key=lambda x: x[0])
        out[col] = rows
        if sleep > 0 and indicator != "总市值":
            time.sleep(sleep)
    return out


def merge_monthly_rows(ts_code: str, hist: Dict[str, List[Tuple[str, float]]]
                       ) -> List[Dict[str, Any]]:
    """3 指标按 date 对齐 → 月度锚点行（date 全集 = 3 指标并集，缺值 NULL）。

    行只含回填相关列（ts_code/date/pe_ttm/pb/total_mv/source）；
    ttm_yield_pct 由灌库侧本地 SQL 派生（需 T4/T2，放 ingest 层）。
    """
    by_date: Dict[str, Dict[str, Any]] = {}
    for d in {x for rows in hist.values() for x, _ in rows}:
        by_date[d] = {"ts_code": ts_code, "date": d, "source": SOURCE,
                      "pe_ttm": None, "pb": None, "total_mv": None}
    for col, rows in hist.items():
        for d, v in rows:
            by_date[d][col] = v
    return [by_date[d] for d in sorted(by_date)]


def load_valuation_history(con, ts_code: str,
                           rows: List[Dict[str, Any]]) -> int:
    """upsert 历史行：**只写/更新 bd_static 行**（不覆盖 tencent 当前快照——
    同日已有 tencent 行时本函数 INSERT 会撞 PK，故 ON CONFLICT DO UPDATE
    限定 WHERE source='bd_static'，tencent 行原样保留）。

    ttm_yield_pct 本函数不写（灌库后由 :func:`derive_yield_for_rows` 统一派生）。
    """
    if not rows:
        return 0
    # 需要"只更新 bd_static 行"的条件 upsert（通用 upsert 是 REPLACE 语义会覆盖
    # tencent 同日快照）→ 直接 SQL：INSERT ... ON CONFLICT DO UPDATE WHERE 限定。
    # 列集与 DDL 对齐（float_mv/turnover_pct/ttm_yield_pct/conflict_src 缺省 NULL）。
    sql = """
    INSERT INTO valuation_daily (ts_code, date, total_mv, pe_ttm, pb, source, fetched_at)
    VALUES (?, ?::DATE, ?, ?, ?, ?, ?)
    ON CONFLICT (ts_code, date) DO UPDATE SET
        total_mv = EXCLUDED.total_mv,
        pe_ttm   = EXCLUDED.pe_ttm,
        pb       = EXCLUDED.pb,
        source   = EXCLUDED.source,
        fetched_at = EXCLUDED.fetched_at
    WHERE valuation_daily.source = ?
    """
    # rowcount 不可靠（duckdb 1.5.5 DML 返回 -1，无 changes()）→ 前后 COUNT 差
    # 返回语义 = 本次新增的 bd_static 行数（conflict no-op/覆盖不计）。
    def _bd_cnt():
        return con.execute(
            "SELECT COUNT(*) FROM valuation_daily WHERE ts_code=? AND source=?",
            [ts_code, SOURCE]).fetchone()[0]

    before = _bd_cnt()
    ts = now_ts()
    for r in rows:
        con.execute(sql, [r["ts_code"], r["date"], r.get("total_mv"),
                          r.get("pe_ttm"), r.get("pb"), SOURCE, ts, SOURCE])
    return int(_bd_cnt() - before)


def derive_yield_for_rows(con, ts_codes: Optional[List[str]] = None) -> int:
    """本地派生 ttm_yield_pct：TTM 分红 DPS ÷ 锚点日 close（%）。

    只对 source='bd_static' 行；close 缺（停牌月）或 TTM 无分红 → NULL。
    幂等（重跑覆盖同值）。:return: 更新行数。
    """
    code_f = "AND v.ts_code IN ({})".format(",".join("?" * len(ts_codes))) if ts_codes else ""
    args: List[Any] = [SOURCE]
    if ts_codes:
        args.extend(ts_codes)
    # 注意：内层子查询别名不得与外层冲突（DuckDB 作用域解析）。
    # 两标量子查询逗号连接：任一为 NULL/0 → 整式 NULL → 该行保持 NULL。
    # 返回语义 = 派生成功的行数（ttm_yield_pct 非 NULL 的 bd_static 行），
    # 前后 COUNT 差（rowcount 不可靠）。
    pre_sql = (f"SELECT COUNT(*) FROM valuation_daily v WHERE v.source=? AND v.ttm_yield_pct IS NOT NULL {code_f}")
    before = con.execute(pre_sql, args).fetchone()[0]
    sql = f"""
    UPDATE valuation_daily v SET ttm_yield_pct = (
        SELECT dps.s / k.close * 100
        FROM (SELECT SUM(cash_dps) AS s FROM dividend_events dd
              WHERE dd.ts_code = v.ts_code
                AND dd.ex_date <= v.date
                AND dd.ex_date > v.date - INTERVAL 365 DAY) dps,
             (SELECT close FROM kline_daily kk
              WHERE kk.ts_code = v.ts_code AND kk.date = v.date) k
        WHERE dps.s IS NOT NULL AND dps.s > 0 AND k.close IS NOT NULL
    )
    WHERE v.source = ? {code_f}
    """
    con.execute(sql, args)
    after = con.execute(pre_sql, args).fetchone()[0]
    return int(after - before)
