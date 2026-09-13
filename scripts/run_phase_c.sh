#!/usr/bin/env bash
# Phase C 历史数据迁移 —— 每日续跑包装（cron 用）。
# migrate_history.py 自带断点续传 + 单日 30k 预算干净暂停；本脚本职责：
#   1. 已有 migrate_history 进程在跑 → 跳过（防重叠双跑）
#   2. 前台执行（cron 会话内），日志按日落盘 logs/migrate_phase_c_YYYYMMDD.log
#   3. 进程级超时 10h（30k 条 × 最坏 ~1s/条 ≈ 8.3h，留余量）
# 全部完成后计划项全命中缓存 → 快速退出"本次执行 0 条"，可长期挂 cron 无害；
# 确认完成后可手动从 crontab 移除本行。
set -uo pipefail
cd /home/ubuntu/stock-screener || exit 1

if pgrep -f "backtest.migrate_history" > /dev/null 2>&1; then
  echo "[$(date -u +%FT%TZ)] migrate_history 已在运行，跳过本次 cron"
  exit 0
fi

STAMP=$(date -u +%Y%m%d)
LOG="logs/migrate_phase_c_${STAMP}.log"
echo "[$(date -u +%FT%TZ)] Phase C 续跑开始 → $LOG"
timeout -k 60 36000 .venv/bin/python -m backtest.migrate_history >> "$LOG" 2>&1
RC=$?
echo "[$(date -u +%FT%TZ)] Phase C 本轮退出 rc=$RC（0=完成/预算暂停；2=login失败，次日重试）"
exit "$RC"
