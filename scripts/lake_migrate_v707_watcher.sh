#!/bin/bash
# v7.0.7 生产湖迁移调度器：等湖锁空窗（真实 read_only 连接探测——DuckDB 写锁
# 持有期间连 read_only 也被拒，与生产互斥语义一致）→ 跑 lake_migrate_v707.py
# （幂等：逐列探测存在性，已砍 skip；迁移=ALTER DROP COLUMN ×5，纯 schema 无数据
# 改写，秒级）→ 完成退出。
# 纪律：120s 缓冲等前持有者彻底退出；若与 T3 续跑（t3_resume_watcher2）抢同一空窗，
# 先到先跑——迁移秒级结束，互不伤害。重复执行安全（幂等）。
set -u
REPO=/home/ubuntu/stock-screener
DB=$REPO/data/lake/lake.duckdb
LOG=$REPO/logs/lake_migrate_v707.log
echo "[$(date -u)] migrate-v707 调度器启动（pid $$）；轮询 300s" | tee -a "$LOG"

probe() {
  "$REPO/.venv/bin/python" - "$DB" <<'EOF' 2>/dev/null
import sys, duckdb
duckdb.connect(sys.argv[1], read_only=True).close()
EOF
}

while ! probe; do
  echo "[$(date -u)] 湖锁持有中（增量/T3 在跑），300s 后重试" >> "$LOG"
  sleep 300
done
echo "[$(date -u)] 湖锁空窗，120s 缓冲后执行迁移" | tee -a "$LOG"
sleep 120
cd "$REPO"
if .venv/bin/python scripts/lake_migrate_v707.py 2>&1 | tee -a "$LOG"; then
  echo "[$(date -u)] 迁移完成（exit 0）——重启 web 服务加载新代码（旧进程内存中
  仍是查 roe_avg/ocf/controller_* 的旧 SQL，列已砍必须重启）" | tee -a "$LOG"
  systemctl --user restart stock-screener-web.service
  sleep 5
  if curl -sf http://127.0.0.1:9090/api/health >/dev/null 2>&1; then
    echo "[$(date -u)] web 服务已重启，health OK" | tee -a "$LOG"
  else
    echo "[$(date -u)] ⚠️ web 服务重启后 health 失败——需人工介入" | tee -a "$LOG"
    exit 1
  fi
else
  echo "[$(date -u)] 迁移失败（非零退出）——需人工介入" | tee -a "$LOG"
  exit 1
fi
