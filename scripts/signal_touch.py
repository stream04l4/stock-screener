#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""盘中触碰信号落盘（v7.0.20，Joel 2026-11-08：「盘中达成信号又涨回出区的，
保留到当日，第二天移入历史信号」）。

职责：每交易日 12:05 UTC（= 北京 20:05，lake-sync 11:30 UTC 灌完当日 bar 后，
cron run_signal_touch_daily.sh——job 的信号线与页面当日视图同口径）对
**股票池 ∪ 持仓账本**逐股判定「最新行情日最低价 ≤ 触发线」→ 触碰事件写入
data/alerts/signal_touch_state.json（90 天滚动窗口，原子写）。

消费方：Web /api/strategy_signals——
- 当前段：现价在区内 OR 最新行情日已触碰（status=touched，「已触回」，行情日
  滚动后自动移出）；
- 历史段：状态机回放 buys ∪ 本 job 落盘事件（同 代码|日期|档 去重，buy 优先）。

判定源 = 腾讯行情「最新行情日」当日最低价（盘后 = 全天）；**实时主备皆败 →
本地湖兜底**（v8.2.0 补：kline_daily 每只最新 bar 的 low/close——11-17 腾讯
整批空 + 东财 WAF 502 双杀致触碰丢失实锤；湖 19:30 同步完当日 bar，零网络）。
湖锁/开湖失败均退避重试；每 (代码,行情日)
只记下一档（页面信号线）；更深档位由次日状态机推进后的新信号线覆盖，收盘过线
的日内穿多档由回放 buys 全量记录。

退出码：0=正常（含无触碰）；2=配置错；3=数据湖不可用（下轮重试）；4=行情
（含湖兜底）整体失败（下轮重试，不清状态）。

补跑：cron 另有 23:30 UTC（北京 07:30）补跑槽——20:05 主跑失败（湖长锁等）
时次日开盘前补记（(代码,行情日) 键幂等，成功日补跑=空转）。

用法：
    python scripts/signal_touch.py          # 定时任务入口
    python scripts/signal_touch.py --dry-run  # 只算+打印，不落盘
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from martingale import touches  # noqa: E402
from martingale.config import load_config  # noqa: E402
from martingale.engine import state_rows  # noqa: E402

CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "strategy.yaml")
USER_POSITIONS_PATH = os.path.join(PROJECT_ROOT, "data", "user_positions.json")

log = logging.getLogger("signal_touch")

# v8.2.0 补：湖锁退避重试参数（11-18 20:05 被逐只 legacy 灌数持锁致主跑失败
# 实锤；短时锁 5min 内重试可解，多时锁由 23:30 UTC 补跑槽兜底）
_LAKE_RETRY_ATTEMPTS = 6
_LAKE_RETRY_DELAY_S = 60
_LAKE_PATH = os.path.join(PROJECT_ROOT, "data", "lake", "lake.duckdb")


def _is_lock_error(exc: Exception) -> bool:
    s = str(exc)
    return ("锁" in s) or ("lock" in s.lower())


def _open_lake_retry() -> Optional[Any]:
    """read_only 开湖 + 锁退避重试；失败返回 None。"""
    import duckdb
    con = None
    last = None
    for i in range(_LAKE_RETRY_ATTEMPTS):
        try:
            con = duckdb.connect(_LAKE_PATH, read_only=True)
            return con
        except Exception as exc:  # noqa: BLE001
            last = exc
            if not _is_lock_error(exc):
                break
            log.warning("数据湖被锁（重试 %d/%d，%ds 后）: %s",
                        i + 1, _LAKE_RETRY_ATTEMPTS, _LAKE_RETRY_DELAY_S, exc)
            time.sleep(_LAKE_RETRY_DELAY_S)
    if last is not None:
        log.warning("开湖失败（不重试）: %s", last)
    return None


def _lake_spot(codes) -> Dict[str, Dict[str, Any]]:
    """行情兜底：本地湖 kline_daily 每只**最新 bar** 的 low/close。

    停牌股最新 bar 停在旧日 → timestamp 落旧日（与实时源「停牌股行情停
    旧日」同语义，compute_touches 按 (代码,行情日) 幂等，不污染当日）。
    湖不可用 → {}（调用方回退原 rc=4 路径）。
    """
    if not codes:
        return {}
    con = _open_lake_retry()
    if con is None:
        return {}
    try:
        fmt = ",".join("?" * len(codes))
        rows = con.execute(f"""
            SELECT kk.ts_code, kk.date, kk.low, kk.close
            FROM kline_daily kk
            WHERE kk.ts_code IN ({fmt})
              AND kk.date = (SELECT MAX(k2.date) FROM kline_daily k2
                             WHERE k2.ts_code = kk.ts_code)
        """, list(codes)).fetchall()
    except Exception as exc:  # noqa: BLE001
        log.warning("湖兜底读取失败: %s", exc)
        rows = []
    finally:
        con.close()
    spot: Dict[str, Dict[str, Any]] = {}
    for c, d, low, close in rows:
        if low is None:
            continue
        ds = d.strftime("%Y%m%d") if hasattr(d, "strftime") else str(d)[:10].replace("-", "")
        spot[str(c)] = {"low": float(low),
                        "price": float(close) if close is not None else None,
                        "timestamp": ds}
    return spot


def _up_read() -> Dict[str, Any]:
    """读用户持仓账本（与 web.app._up_read 同口径；缺省/损坏 → {}）。"""
    if not os.path.exists(USER_POSITIONS_PATH):
        return {}
    try:
        with open(USER_POSITIONS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if isinstance(v, dict)}
    except (OSError, ValueError):
        return {}


def run(dry_run: bool = False) -> int:
    # 1) 配置预检（配置错 = 退出码 2，区别于湖不可用 3）+ 候选池 + 账本
    #    → universe（与 /api/strategy_signals 同口径）
    try:
        # v8.1.0：tp = 首仓双条件②参数（状态机与回测同口径）
        _sp_cfg, _up_cfg, _tp_cfg = load_config(CONFIG_PATH)
    except Exception as exc:  # noqa: BLE001 - ConfigError
        log.error("策略配置错误: %s", exc)
        return 2
    user = _up_read()
    from web.app import _load_pool_ctx

    rows = None
    universe: list = []
    for _attempt in range(_LAKE_RETRY_ATTEMPTS):
        try:
            # v8.1.0：tp = 首仓双条件②参数（状态机与回测同口径）
            _sp2, _up2, con, pool = _load_pool_ctx()
            try:
                universe = sorted(set(pool.codes) | set(user.keys()))
                # v7.0.32 除权日修正参照日（北京日期）：正常 20:05 跑时湖已同步
                # 当日 bar（无 pending 事件、零影响）；湖同步失败日兜底生效
                _bj_today = datetime.now(timezone(timedelta(hours=8))).date()
                rows = state_rows(_sp2, con, universe, include_buys=True,
                                  now=_bj_today, tp=_tp_cfg)
            finally:
                con.close()
            break
        except Exception as exc:  # noqa: BLE001 - 湖锁定/缺失/配置错（含 HTTPException）
            if _attempt + 1 < _LAKE_RETRY_ATTEMPTS and _is_lock_error(exc):
                log.warning("数据湖被锁（重试 %d/%d，%ds 后）: %s",
                            _attempt + 1, _LAKE_RETRY_ATTEMPTS,
                            _LAKE_RETRY_DELAY_S, exc)
                time.sleep(_LAKE_RETRY_DELAY_S)
                continue
            log.warning("数据湖不可用（下轮重试）: %s", exc)
            return 3
    if not universe:
        log.info("股票池与账本均为空，跳过")
        return 0

    # 2) 实时行情（批量一次；high/low = 最新行情日当日高/低）
    # v1.1（Gate 3，设计 §5）：一主一备统一入口（腾讯主 → push2delay 备，整批切换）
    spot = {}
    try:
        from screener.data.realtime import fetch_spot

        spot, _ = fetch_spot(universe, timeout=5.0, max_attempts=1)
    except Exception as exc:  # noqa: BLE001
        log.warning("实时行情主备皆败: %s", exc)
        spot = {}
    if not any(v.get("low") is not None for v in spot.values()):
        # v8.2.0 补：实时主备皆败（WAF/降级）→ 本地湖兜底（零网络）
        log.warning("行情整体失败（无当日 low）→ 本地湖兜底（kline_daily 最新 bar）")
        lake_spot = _lake_spot(universe)
        if lake_spot:
            spot = lake_spot
            log.warning("湖兜底生效: %d 只有当日 low", len(lake_spot))
        else:
            log.warning("行情+湖兜底皆败（下轮重试，不清状态）")
            return 4

    # 3) 触碰事件（date = 各股行情日；停牌股行情停在旧日 → 落旧日，不污染当日）。
    #    held_codes = 持仓账本：持仓股按加仓档口径、非持仓按首仓线口径（v7.0.23
    #    与页面信号线同一分支 touches.trigger_price(row, held)）
    events = touches.compute_touches(rows, spot, _sp2,
                                     held_codes=frozenset(user))
    if not events:
        log.info("无触碰（%d 只检查完毕）", len(universe))
        return 0

    # 4) 落盘（90 天滚动窗口；幂等：同日重跑覆盖）
    state = touches.load_state()
    merged = touches.merge_events(state, events)
    if not dry_run:
        touches.save_state(None, merged)
    print(f"[signal_touch] universe={len(universe)} touched={len(events)} "
          f"state_total={len(merged)} dry_run={dry_run}")
    for e in events:
        print(f"  {e['date']} {e['ts_code']} {e['name']} "
              f"{'首仓' if e['first_of_round'] else '第' + str(e['level']) + '档'} "
              f"line={e['level_price']} low={e['low_price']} lots={e['lots']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="只算+打印，不落盘")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
