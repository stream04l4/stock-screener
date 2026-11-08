#!/usr/bin/env bash
# 盘中触碰信号落盘（v7.0.20）：每交易日 12:05 UTC（= 北京 20:05，收盘后、
# lake-sync 灌完当日 bar 后）对 股票池 ∪ 账本 逐股
# 判定「最新行情日最低价 ≤ 触发线」→ data/alerts/signal_touch_state.json（90 天
# 滚动窗口）。Web「当前信号」保留盘中触碰（已触回）；次日行情日滚动后移入
# 「历史信号」。失败不阻断（Web 读旧状态，下轮重试）。
set -uo pipefail
cd /home/ubuntu/stock-screener || exit 1

STAMP=$(TZ=Asia/Shanghai date +%Y%m%d)
LOG="logs/signal_touch_${STAMP}.log"
mkdir -p logs
echo "[$(TZ=Asia/Shanghai date +%FT%T%z)] 触碰信号落盘开始 → $LOG"
timeout -k 30 300 .venv/bin/python scripts/signal_touch.py >> "$LOG" 2>&1
RC=$?
echo "[$(TZ=Asia/Shanghai date +%FT%T%z)] 触碰信号落盘退出 rc=$RC（0=完成；3=湖不可用；4=行情失败，下轮重试）"
exit "$RC"
