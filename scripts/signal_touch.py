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

判定源 = 腾讯行情「最新行情日」当日最低价（盘后 = 全天）。每 (代码,行情日)
只记下一档（页面信号线）；更深档位由次日状态机推进后的新信号线覆盖，收盘过线
的日内穿多档由回放 buys 全量记录。

退出码：0=正常（含无触碰）；2=配置错；3=数据湖不可用（下轮重试）；4=行情整体
失败（下轮重试，不清状态）。

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
from typing import Any, Dict

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from martingale import touches  # noqa: E402
from martingale.config import load_config  # noqa: E402
from martingale.engine import state_rows  # noqa: E402

CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "strategy.yaml")
USER_POSITIONS_PATH = os.path.join(PROJECT_ROOT, "data", "user_positions.json")

log = logging.getLogger("signal_touch")


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
        load_config(CONFIG_PATH)
    except Exception as exc:  # noqa: BLE001 - ConfigError
        log.error("策略配置错误: %s", exc)
        return 2
    user = _up_read()
    try:
        from web.app import _load_pool_ctx

        _sp2, _up2, con, pool = _load_pool_ctx()
        try:
            universe = sorted(set(pool.codes) | set(user.keys()))
            rows = state_rows(_sp2, con, universe, include_buys=True)
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001 - 湖锁定/缺失/配置错（含 HTTPException）
        log.warning("数据湖不可用（下轮重试）: %s", exc)
        return 3
    if not universe:
        log.info("股票池与账本均为空，跳过")
        return 0

    # 2) 实时行情（批量一次；high/low = 最新行情日当日高/低）
    try:
        from screener.data.tencent import TencentClient

        spot = TencentClient(timeout=5.0, max_attempts=1).fetch(universe) or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("腾讯实时行情异常: %s", exc)
        spot = {}
    if not any(v.get("low") is not None for v in spot.values()):
        log.warning("行情整体失败（无当日 low，下轮重试，不清状态）")
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
              f"line={e['level_price']} low={e['low_price']} amount={e['amount']}")
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
