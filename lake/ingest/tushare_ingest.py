# -*- coding: utf-8 -*-
"""lake.ingest.tushare_ingest —— P2 低频补充源（daily/dividend/daily_basic/adj_factor/
index_daily；token 读 .env TUSHARE_TOKEN）。

**纪律（v6 brief）**：Tushare 仅基础日线权限，**低频单调用**；无权限的财报类一律不碰。
tushare 未装 → :func:`tushare_available` False，本模块所有 fetch 抛 LakeUnavailable
（优雅降级，不影响主路径）。

定位：BaoStock/腾讯/新浪的**交叉校验 + 缺口补充**（P2），不进 P0/P1 热路径。
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

log = logging.getLogger("lake.ingest.tushare")


def tushare_available() -> bool:
    try:
        import tushare  # noqa: F401

        return True
    except ImportError:
        return False


def _load_token() -> Optional[str]:
    """从 .env 读 TUSHARE_TOKEN（不依赖 python-dotenv，手动解析）。"""
    env_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        ".env")
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("TUSHARE_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    except OSError:
        pass
    return os.environ.get("TUSHARE_TOKEN") or None


def _pro():
    """惰性构造 tushare pro 客户端（未装/无 token → LakeUnavailable）。"""
    if not tushare_available():
        raise RuntimeError("tushare 未安装（P2 源；uv pip install tushare）")
    token = _load_token()
    if not token:
        raise RuntimeError(".env 缺 TUSHARE_TOKEN（P2 源不可用）")
    import tushare as ts

    ts.set_token(token)
    return ts.pro_api()


# ---------------------------------------------------------------------------
# P2 fetch（低频单调用；每个接口独立函数，便于"装包后每接口验 1 次"）
# ---------------------------------------------------------------------------
def fetch_daily(ts_code: str, start: str, end: str) -> List[Dict[str, Any]]:
    """daily 日线（open/high/low/close/vol/amount/pct_chg）。低频。"""
    pro = _pro()
    df = pro.daily(ts_code=ts_code, start_date=start.replace("-", ""),
                   end_date=end.replace("-", ""))
    return _df_to_rows(df, ["trade_date", "open", "high", "low", "close",
                            "vol", "amount", "pct_chg"])


def fetch_adj_factor(ts_code: str) -> List[Dict[str, Any]]:
    """adj_factor 复权因子全序列（对照 BaoStock 前向填充口径）。低频。"""
    pro = _pro()
    df = pro.adj_factor(ts_code=ts_code)
    return _df_to_rows(df, ["trade_date", "adj_factor"])


def fetch_index_daily(index_code: str, start: str, end: str) -> List[Dict[str, Any]]:
    """index_daily 指数日线（四指数交叉校验 T7）。低频。"""
    pro = _pro()
    df = pro.index_daily(ts_code=index_code, start_date=start.replace("-", ""),
                         end_date=end.replace("-", ""))
    return _df_to_rows(df, ["trade_date", "open", "high", "low", "close",
                            "vol", "amount"])


def fetch_dividend(ts_code: str) -> List[Dict[str, Any]]:
    """dividend 分红（对照 T4 em 静态源）。低频。"""
    pro = _pro()
    df = pro.dividend(ts_code=ts_code)
    return _df_to_rows(df, ["end_date", "ann_date", "record_date", "ex_date",
                            "div_cash_before_tax"])


# ---------------------------------------------------------------------------
# v7.0.8 全市场单日批量（每日增量主轴）：pro.daily / pro.adj_factor /
# pro.daily_basic 均支持 trade_date 全市场单次取回（实测 5500+ 行/次，<2s）。
# 限频：每接口 1 次/分钟 → 每日 3 次调用 + 62s 等待重试（总耗时 ~2.5min）。
# 定位从 P2 交叉校验源升级为**每日增量 T2/T3 主源**（v7.0.8，Joel 拍板：
# 新浪逐只 ~9-10h 不可受）；逐只多源 fallback 全保留（批量失败→legacy 路径）。
# ---------------------------------------------------------------------------
def _to_lake_code(ts_code: str) -> Optional[str]:
    """tushare 601398.SH → 湖 sh.601398（非 sh/sz 市场 → None，湖不覆盖）。"""
    if not ts_code or "." not in ts_code:
        return None
    num, ex = ts_code.split(".", 1)
    if ex == "SH":
        return f"sh.{num}"
    if ex == "SZ":
        return f"sz.{num}"
    return None  # BJ 等


def _batch_call(pro, fn_name: str, trade_date: str, cols: List[str],
                max_retries: int = 6) -> List[Dict[str, Any]]:
    """全市场单日批量调用（1 次/分钟限频 → 超限等 62s 重试）。

    :return: list[dict]（ts_code 已转湖格式；非 sh/sz 行丢弃）。
    """
    import time as _time

    td = trade_date.replace("-", "")
    last: Optional[Exception] = None
    df = None
    for i in range(max_retries):
        try:
            df = getattr(pro, fn_name)(trade_date=td)
        except Exception as exc:  # noqa: BLE001
            last = exc
            if "频率超限" in str(exc):
                # 渐进退避（实测 62s 偶卡边界：服务端限频窗口与重试时刻重叠时
                # 连续多次超限）→ 62s × 轮次。3 接口/日 × 正常 1 次命中，成本可忽。
                _time.sleep(int(62 * (i + 1)))
                continue
            raise RuntimeError(
                f"tushare {fn_name}(trade_date={td}) 失败: {exc}") from exc
        if df is not None:
            break
        # tushare API code!=0 时**返回 None 不抛异常**（实测 adj_factor 瞬时
        # 错误）——按瞬时失败退避重试，绝不把 None 当"空市场"（防静默丢数）。
        last = RuntimeError(f"tushare {fn_name} 返回 None（API code!=0）")
        _time.sleep(int(62 * (i + 1)))
    if df is None:
        raise RuntimeError(
            f"tushare {fn_name}(trade_date={td}) 重试 {max_retries} 次仍未通过") from last
    rows = _df_to_rows(df, ["ts_code"] + cols)
    out: List[Dict[str, Any]] = []
    for r in rows:
        lc = _to_lake_code(r["ts_code"])
        if lc:
            r["ts_code"] = lc
            out.append(r)
    return out


def fetch_daily_batch(trade_date: str) -> List[Dict[str, Any]]:
    """T2 全市场单日 K线（v7.0.8 每日增量主源）。

    :return: [{ts_code(湖格式), date, open, high, low, close, preclose,
               pct_chg, volume(股), amount(元)}]。
    单位换算：vol 手→股(×100)、amount 千元→元(×1000)。
    """
    pro = _pro()
    rows = _batch_call(pro, "daily", trade_date,
                       ["trade_date", "open", "high", "low", "close",
                        "pre_close", "pct_chg", "vol", "amount"])
    out = []
    for r in rows:
        vol = r.pop("vol")
        amt = r.pop("amount")
        out.append({
            "ts_code": r["ts_code"], "date": r["trade_date"],
            "open": r["open"], "high": r["high"], "low": r["low"],
            "close": r["close"], "preclose": r.get("pre_close"),
            "pct_chg": r.get("pct_chg"),
            "volume": int(vol * 100) if vol is not None else None,   # 手→股
            "amount": amt * 1000.0 if amt is not None else None,     # 千元→元
        })
    return out


def fetch_adj_factor_batch(trade_date: str) -> Dict[str, Dict[str, float]]:
    """T2 全市场单日复权因子（事件日→af，供 load_t2 前向填充）。

    :return: {ts_code: {date: af}}。
    """
    pro = _pro()
    rows = _batch_call(pro, "adj_factor", trade_date,
                       ["trade_date", "adj_factor"])
    out: Dict[str, Dict[str, float]] = {}
    for r in rows:
        if r.get("adj_factor") is not None:
            out.setdefault(r["ts_code"], {})[r["trade_date"]] = r["adj_factor"]
    return out


def fetch_daily_basic_batch(trade_date: str) -> List[Dict[str, Any]]:
    """T3 全市场单日估值快照（v7.0.8 每日增量主源）。

    :return: [{ts_code, date, total_mv_yi, float_mv_yi, pe_ttm, pb,
               turnover, ttm_yield_pct}]。
    单位换算：total_mv/float_mv 万元→亿元(÷10000)。ttm_yield_pct=dv_ttm
    （交叉校验值；主用值仍由 factors.py 本地派生覆盖，同腾讯 idx64 语义）。
    """
    pro = _pro()
    rows = _batch_call(pro, "daily_basic", trade_date,
                       ["trade_date", "pe", "pe_ttm", "pb", "total_mv",
                        "float_mv", "turnover_rate", "dv_ttm"])
    out = []
    for r in rows:
        out.append({
            "ts_code": r["ts_code"], "date": r["trade_date"],
            "total_mv_yi": (r["total_mv"] / 10000.0
                            if r.get("total_mv") is not None else None),
            "float_mv_yi": (r["float_mv"] / 10000.0
                            if r.get("float_mv") is not None else None),
            "pe_ttm": r.get("pe_ttm"),
            "pb": r.get("pb"),
            "turnover": r.get("turnover_rate"),
            "ttm_yield_pct": r.get("dv_ttm"),
        })
    return out


def _df_to_rows(df, cols: List[str]) -> List[Dict[str, Any]]:
    """tushare DataFrame → list[dict]（缺列/空表安全）。"""
    if df is None or len(df) == 0:
        return []
    out = []
    for _, row in df.iterrows():
        out.append({c: (None if _isna(row.get(c)) else row.get(c)) for c in cols})
    return out


def _isna(v) -> bool:
    if v is None:
        return True
    try:
        return v != v  # NaN
    except Exception:  # noqa: BLE001
        return False
