#!/usr/bin/env bash
# v7.0.8 批量增量 E2E 彩排（一次性，2026-10-23 22:00 UTC systemd-run 触发）
# 背景：tushare adj_factor/daily_basic 免费档 5 次/天，2026-10-22 测试烧光进
# ~24h 硬封；本彩排在配额恢复窗口后自动跑真批量（副本 /tmp/v708_e2e.duckdb），
# 在 10-24 11:30 UTC 首次生产批量跑之前留出修复窗口。
# 流程：探针 2 端点（各 1 次，65s 间隔）→ 双 OK 才跑全市场增量；
#       配额未恢复 → 跳过（生产 cron 自证 + fast-fail 兜底，不硬跑）。
set -uo pipefail
cd /home/ubuntu/stock-screener || exit 1
LOG=/tmp/v708_e2e_run2.log

echo "[$(date -u +FT%TZ)] E2E rehearsal 开始" >> "$LOG"
if [ ! -f /tmp/v708_e2e.duckdb ]; then
    echo "[$(date -u +FT%TZ)] 副本缺失，跳过（生产 cron 自证）" >> "$LOG"
    exit 0
fi

if .venv/bin/python -u - <<'PY' >> "$LOG" 2>&1
import os, sys, time
from dotenv import load_dotenv

load_dotenv("/home/ubuntu/stock-screener/.env")
import tushare as ts

ts.set_token(os.environ["TUSHARE_TOKEN"])
pro = ts.pro_api()
ok = True
for name, fn in [("adj_factor", lambda: pro.adj_factor(trade_date="20261023")),
                 ("daily_basic", lambda: pro.daily_basic(trade_date="20261023"))]:
    try:
        r = fn()
        print(f"{name} 探针: {'OK' if r is not None else 'None'}")
        if r is None:
            ok = False
    except Exception as e:  # noqa: BLE001
        print(f"{name} 探针失败: {str(e)[:80]}")
        ok = False
    time.sleep(65)
sys.exit(0 if ok else 1)
PY
then
    echo "[$(date -u +FT%TZ)] 配额已恢复，E2E 起跑（副本全市场批量）" >> "$LOG"
    timeout 7200 .venv/bin/python scripts/lake_backfill.py \
        --db /tmp/v708_e2e.duckdb incremental >> "$LOG" 2>&1
    echo "[$(date -u +FT%TZ)] E2E 结束 rc=$?" >> "$LOG"
else
    echo "[$(date -u +FT%TZ)] 配额未恢复，跳过 E2E（明晨 cron 自证，fast-fail 兜底）" >> "$LOG"
fi
