#!/usr/bin/env bash
# 数据湖 P3 每日增量（kline_daily 缺口 + valuation_daily 快照 + index_daily 近 N 日）。
# 背景：v6 lake 切 primary 后，daily 增量此前只靠 Web「开始同步」按钮手工触发，
# 09-23 无人点 → 湖停更 8 天（2026-10-01 排查确认；run_cron 选股 cron 同期也缺失）。
# 本脚本职责（对齐 run_phase_c.sh 模式）：
#   1. 已有 lake_backfill 进程 → 跳过（防重叠双跑/防抢锁）
#   2. 前台执行（cron 会话内），日志按日落盘 logs/lake_sync_YYYYMMDD.log
#   3. 进程级超时 16.7h（v7.0.8 批量后正常 ~10-15min；超时是 tushare 全挂时
#      逐只 fallback 的最坏兜底 ~14h；须保证在次日 09:35 选股 cron 前
#      释放 DuckDB 锁——11:30 UTC + 16.7h = 次日 04:12 UTC < 09:35）；
#      incremental 自带断点续传（done 键 per code + 运行日）→ 中断次日续跑，无脏数据。
# v7.0.8（2026-10-22）：T2/T3 改 tushare 全市场单日批量（3 次调用 ~2.5min）
# 取代逐只 sina/腾讯（~9-10h）；批量失败自动回退逐只（stats batch 记痕）。
# 纪律：BaoStock 走 QuotaGuard（incremental 内部）；限速在 adapter 内。
set -uo pipefail
cd /home/ubuntu/stock-screener || exit 1

if pgrep -f "lake_backfill.py" > /dev/null 2>&1; then
  echo "[$(date -u +%FT%TZ)] lake_backfill 已在运行，跳过本次增量"
  exit 0
fi

STAMP=$(date -u +%Y%m%d)
LOG="logs/lake_sync_${STAMP}.log"
echo "[$(date -u +%FT%TZ)] 湖增量开始 → $LOG"
timeout -k 60 36000 .venv/bin/python scripts/lake_backfill.py incremental >> "$LOG" 2>&1
RC=$?
echo "[$(date -u +%FT%TZ)] 湖增量退出 rc=$RC（0=完成；非0=部分失败，断点续传次日补）"
exit "$RC"
