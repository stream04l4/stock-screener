# -*- coding: utf-8 -*-
"""lake.ingest.common —— ingest 共用工具（溯源字段 / upsert / 代码格式）。

设计：所有 ingest 复用现有客户端，本层只做"客户端输出 → lake 行"的薄转换 +
统一溯源三列（source/fetched_at/data_version，继承 v5.2 canonical 契约）。
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, Iterable, List, Optional, Sequence

# 溯源：数据湖 schema 版本（v6 首版）
DATA_VERSION = "v6.0"


def now_ts() -> str:
    """UTC ISO 时间戳（fetched_at 列，TIMESTAMP）。"""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def to_ts_code(code6_or_bs: str) -> str:
    """把代码统一成 ts_code 格式 sh.601398。

    - 已是 bs 格式（含点，sh.601398）→ 原样。
    - 6 位裸码（601398）→ 按前缀补 sh./sz.（6/9→sh，0/2/3→sz；北交所 4/8/92→bj）。
    """
    c = str(code6_or_bs).strip()
    if "." in c:
        return c
    c = c.zfill(6)
    if c[0] in ("5", "6", "9"):      # 沪市（含科创板 688、基金 5）
        return f"sh.{c}"
    if c[0] in ("4", "8") or c.startswith("92"):  # 北交所
        return f"bj.{c}"
    return f"sz.{c}"                  # 深市（含创业板 300）


def code6(ts_code: str) -> str:
    """sh.601398 → 601398（新浪/东财等裸码接口用）。"""
    return ts_code.split(".")[-1]


# ---------------------------------------------------------------------------
# upsert：INSERT OR REPLACE（仅 PK 表；无 PK 表如 dividend_events/holders_snapshot
#   必须用 delete_where + insert_many "先删后插"——DuckDB 的 INSERT OR REPLACE
#   要求目标表有 UNIQUE/PK 约束，否则 BinderException）
# ---------------------------------------------------------------------------
class _WriteNull:
    """哨兵：显式写 NULL（与"不写该列"区分）。不可序列化、单例语义。"""

    __slots__ = ()


def write_null() -> "_WriteNull":
    """upsert 的 conflict_src 参数取值——**显式把 conflict_src 写成 NULL**。

    v6.1 DEF-1：INSERT OR REPLACE 是整行替换——若"无分歧（None）"时不写该列，
    旧行的陈旧 conflict_src 摘要会残留在新写入的行上（tester 复现的审计列错误）。
    故 kline_daily 等每次写入都必须让 conflict_src **反映本次写入**：
    有分歧→传摘要字符串；无分歧→传 ``write_null()``（显式 NULL，清掉旧值）。
    """
    return _WriteNull()


def upsert(con, table: str, columns: Sequence[str], rows: Iterable[Sequence[Any]],
           conflict_src: Optional[Any] = None) -> int:
    """批量 INSERT OR REPLACE。返回写入行数。**仅用于有 PK/UNIQUE 约束的表**。

    - 列名/值显式对应（不依赖顺序），None → NULL。
    - 空行集 → 0（no-op，幂等）。
    - 单条失败立即抛错（不静默吞；调用方按 backfill 进度语义处理）。
    - ⚠️ 无 PK 表调用本函数会抛 BinderException（"specify ON CONFLICT columns
      manually"）——请改用 delete_where + insert_many。

    v6.1：``conflict_src`` 可选参数——跨源分歧摘要（写 ``conflict_src`` 列，≤256B）。
    **仅当表 DDL 含 conflict_src 列时传**（T1-T7 有、T8/T9 无——Q5）；对无此列的
    表传非 None/哨兵会抛 BinderException，由调用方保证（load_t2/load_t7 等按 Q5
    清单的表才传）。

    v6.1 DEF-1：三态语义（向后兼容 + 审计列正确性）——
    - ``None``（缺省）= **不写该列**：旧调用方零影响（T3/T4/T8/T9 等既有行为不变；
      含 load_t2 的 legacy 单源路径与 v6.0.x 逐字节一致）。
    - 字符串 = 写分歧摘要。
    - :func:`write_null` 哨兵 = **显式写 NULL**：kline_daily 每次写入必传（无分歧
      →NULL），REPLACE 后不残留上一次写入的陈旧值。
    """
    rows = [list(r) for r in rows]
    if not rows:
        return 0
    cols = list(columns)
    if conflict_src is not None:  # 字符串摘要 或 write_null() 哨兵（→NULL）
        cols = cols + ["conflict_src"]
        val = None if isinstance(conflict_src, _WriteNull) else conflict_src
        rows = [r + [val] for r in rows]
    cols_sql = ", ".join(f'"{c}"' for c in cols)
    placeholders = ", ".join(["?"] * len(cols))
    sql = f'INSERT OR REPLACE INTO {table} ({cols_sql}) VALUES ({placeholders})'
    con.executemany(sql, rows)
    return len(rows)


def insert_many(con, table: str, columns: Sequence[str], rows: Iterable[Sequence[Any]]) -> int:
    """批量普通 INSERT（executemany）。返回写入行数。**用于无 PK 的表**。

    - 与 upsert 相同的列名/值显式对应、空行集 no-op 语义；
    - 幂等性由调用方保证：先 delete_where 删掉本批将覆盖的键，再 insert_many
      （"先删后插"——事件表无 PK，INSERT OR REPLACE 不可用）。
    """
    rows = [list(r) for r in rows]
    if not rows:
        return 0
    cols = ", ".join(f'"{c}"' for c in columns)
    placeholders = ", ".join(["?"] * len(columns))
    sql = f"INSERT INTO {table} ({cols}) VALUES ({placeholders})"
    con.executemany(sql, rows)
    return len(rows)


def delete_where(con, table: str, col: str, val: Any) -> None:
    """删除某键全部行（无 PK 事件表"先删后插"幂等用）。"""
    con.execute(f'DELETE FROM {table} WHERE "{col}" = ?', [val])


def clean_date(v: Any) -> Optional[str]:
    """'YYYY-MM-DD ...'/8位/YYYYMMDD → ISO；非法/空 → None。"""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    import re
    m = re.search(r"\d{4}-\d{2}-\d{2}", s)
    if m:
        return m.group(0)
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return None


def to_float(v: Any) -> Optional[float]:
    """数值字段 → float（null/空/非法 → None）。"""
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if not s or s.lower() in ("nan", "none", "null"):
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    return None if f != f else f  # NaN → None


def forward_fill_af(dates: Sequence[str], adj_map: Dict[str, float]) -> Dict[str, Optional[float]]:
    """adj_factor 前向填充（Q4：BaoStock 仅除权日有行 → 标准做法是事件值前向填充）。

    :param dates: 交易日序列（升序，ISO）。
    :param adj_map: {除权日: af}——事件日的复权因子。
    :return: {date: af}——每个交易日的有效 af = 该日或之前最近一个除权日的 af；
        首个除权日之前 → None（无历史 af，hfq/qfq view 该段 NULL，属预期）。

    语义（为什么前向填充）：复权因子在两次除权事件之间恒定——除权当日跳到新值，
    之后每个交易日沿用，直到下一次除权。故对升序日期序列做"最近事件日取值"扫描。
    """
    if not dates:
        return {}
    # 事件日排序（只保留落在 dates 范围内的；范围外的事件不影响本窗口填充）
    events = sorted((d, af) for d, af in adj_map.items() if af is not None)
    out: Dict[str, Optional[float]] = {}
    cur: Optional[float] = None
    ei = 0
    n_ev = len(events)
    # 逐交易日扫描：应用所有 event_date <= d 的事件，cur = 最近事件日 af（前向填充）
    for d in dates:
        while ei < n_ev and events[ei][0] <= d:
            cur = events[ei][1]
            ei += 1
        out[d] = cur
    return out
