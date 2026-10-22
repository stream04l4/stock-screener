#!/bin/bash
# T3 估值历史回填·断点续跑调度器 v2（2026-10-22 18:55 修复重启）
# 背景：T3 全市场回填 10-03 中断于 1451/5214。13:22 的 v1 watcher 用
#       `flock -n lake.duckdb` 探测湖锁 —— **误报**：DuckDB 写锁不用 flock(2)
#       （真实锁在 lake.duckdb.write.lock 文件），v1 在增量持锁期间误判"已释放"，
#       13:24 起跑的 t3-history 持锁冲突后静默死亡（零进度、零日志、无进程残留）。
# 修复：锁探测改为真实 DuckDB read_only 连接（写锁持有期间连 read_only 也被拒，
#       与生产互斥语义完全一致），成功 = 锁已释放。
# 语义：湖锁释放 + 120s 缓冲（等增量进程彻底退出）后起跑 t3-history（断点续跑，
#       checkpoint 每 50 只 flush）。若跑穿到次日 11:30 lake-sync，cron 检测到
#       backfill 在跑会自动跳过当日增量、再日窗口化增量补漏（既有设计语义）。
set -u
REPO=/home/ubuntu/stock-screener
DB=$REPO/data/lake/lake.duckdb
LOG=$REPO/logs/t3_history_resume_20261022_v2.log
PIDFILE=$REPO/logs/t3_resume_watcher2.pid
echo $$ > "$PIDFILE"
echo "[$(date -u)] v2 watcher 启动（pid $$）；轮询间隔 300s；锁=真实 read_only 连接探测" | tee -a "$LOG"

probe() {
  "$REPO/.venv/bin/python" - "$DB" <<'EOF' 2>/dev/null
import sys, duckdb
duckdb.connect(sys.argv[1], read_only=True).close()
EOF
}

# 湖锁被持有 → read_only 连接被拒（非零退出）→ 继续等；成功 → 退出循环
while ! probe; do
  echo "[$(date -u)] 湖锁持有中（增量在跑），300s 后重试" >> "$LOG"
  sleep 300
done
echo "[$(date -u)] 湖锁已释放（read_only 连接成功），120s 缓冲后起跑 T3 断点续跑" | tee -a "$LOG"
sleep 120
cd "$REPO"
exec .venv/bin/python scripts/lake_backfill.py t3-history --sleep 1.0 2>&1 | tee -a "$LOG"
