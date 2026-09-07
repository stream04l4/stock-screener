# -*- coding: utf-8 -*-
"""运行状态 sidecar：数据源级失败时写 ``run_status_{YYYYMMDD}.json``（status=failed）。

生产路径失败守卫（9/7 缺陷修复）的核心落盘层，被三条触发路径共用：
- CLI / cron / Web 子进程都经 ``python -m screener`` → ``__main__.main`` 的异常
  处理器写 sidecar（单一收口点）。
- ``run_cron.sh`` 的交易日历守卫在 BaoStock 登录/查询失败时直接调用本模块写 sidecar。

语义约定：
- **数据源级失败**（BaoStockError / 空股票池 DataSourceError / 运行期其他异常）
  → 写 sidecar，**不写** result_*.csv / report_*.md（绝不把失败伪装成"0 只入选"）。
- **合法"0 只入选"**（股票池正常拉取、筛选逻辑正常执行后 Top N=0）→ 正常写空
  result/report，并清除同日可能残留的 failed sidecar（新结果取代旧失败）。

Web 端扫描 output/ 时：``result_*.csv`` = 成功；``run_status_*.json(status=failed)``
且无对应 ``result_*.csv`` = 失败（红色 badge + 错误摘要）。sidecar 用原子写
（临时文件 + os.replace），避免 Web 读到半截 JSON。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional


def sidecar_path(output_dir: str, day_compact: str) -> str:
    """sidecar 文件路径：``<output_dir>/run_status_{YYYYMMDD}.json``。"""
    return os.path.join(output_dir, f"run_status_{day_compact}.json")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _classify_reason(exc: BaseException) -> str:
    """按异常类型归类失败原因（仅用于展示/日志，不改变 status=failed 语义）。

    用 MRO 类名字符串匹配而非 isinstance，避免本模块反向 import 引擎/client
    （保持 runstatus 为纯 stdlib 叶子模块，cron 守卫可安全离线调用）。
    """
    names = {c.__name__ for c in type(exc).__mro__}
    if "BaoStockError" in names or "DataSourceError" in names:
        return "data_source"
    return "runtime"


def write_failed_sidecar(
    output_dir: str,
    requested_date: str,
    run_day: Optional[str],
    exc: BaseException,
) -> str:
    """数据源级/运行期失败 → 写 sidecar，返回文件路径。

    :param output_dir: 产物目录（result/report/sidecar 同目录）。
    :param requested_date: 用户/cron 请求日期（ISO YYYY-MM-DD），sidecar 命名兜底键。
    :param run_day: 实际解析出的交易日（ISO）；None=失败发生在交易日定位之前。
        非空时优先用于命名（与 result_*.csv 的命名口径一致，便于 Web 关联）。
    :param exc: 触发失败的异常（记录类型 + 消息作为错误摘要）。
    """
    day_iso = run_day or requested_date or ""
    day_compact = day_iso.replace("-", "")
    payload: Dict[str, Any] = {
        "status": "failed",
        "requested_date": requested_date,
        "run_day": run_day,
        "error_type": type(exc).__name__,
        "error": str(exc),
        "reason_code": _classify_reason(exc),
        "failed_at": _now_iso(),
    }
    os.makedirs(output_dir, exist_ok=True)
    path = sidecar_path(output_dir, day_compact)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)  # 原子写：Web 并发读不会拿到半截 JSON
    return path


def clear_failed_sidecar(output_dir: str, day_compact: str) -> bool:
    """成功运行后清除同日 failed sidecar（新结果取代旧失败）。返回是否删除了文件。"""
    path = sidecar_path(output_dir, day_compact)
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False


def read_sidecar(output_dir: str, day_compact: str) -> Optional[Dict[str, Any]]:
    """读同日 sidecar；不存在/损坏 → None（Web 不应因 sidecar 损坏而崩）。"""
    path = sidecar_path(output_dir, day_compact)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 - JSON 损坏视为无 sidecar
        return None
