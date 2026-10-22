#!/bin/bash
# T3 估值历史回填·断点续跑调度器（2026-10-22 Joel 指令：先池回测报告，后 T3 续跑）
# 背景：T3 全市场回填 10-03 中断于 1451/5214；lake 写锁当前被当日 lake-sync cron 持有，
#       必须等其释放（预计 ~20:00-21:00 UTC）再起跑。
# 语义：湖锁释放后 120s 起跑 t3-history（断点续跑，checkpoint 每 50 只 flush）。
#       若跑穿到次日 11:30 lake-sync，cron 检测到 backfill 在跑会自动跳过当日增量、
#       次日窗口化增量补漏（09-23 事故后的设计语义，可接受）。
set -u
DB=/home/ubuntu/stock-screener/data/lake/lake.duckdb
LOG=/home/ubuntu/stock-screener/logs/t3_history_resume_20261022.log
echo "[$(date -u)] 等待湖锁释放（flock 探测 $DB）..." | tee -a "$LOG"
# 湖锁被持有 → flock -n 失败（exit 1）→ 继续等；释放 → 成功 → 退出循环
while ! flock -n "$DB" -c true 2>/dev/null; do
  sleep 120
done
echo "[$(date -u)] 湖锁已释放，120s 缓冲后起跑 T3 断点续跑" | tee -a "$LOG"
sleep 120
cd /home/ubuntu/stock-screener
exec .venv/bin/python scripts/lake_backfill.py t3-history --sleep 1.0 2>&1 | tee -a "$LOG"
