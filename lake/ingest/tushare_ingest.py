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
