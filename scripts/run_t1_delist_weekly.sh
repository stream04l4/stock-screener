#!/bin/bash
# 周频 T1 退市档刷新（2026-11-26 起：东财 clist fs=m:0+s:3 单页取回；
# 原 tushare stock_basic D 单调用随源弃用移除。退市日=湖内 K 线最后日期，无 K 线跳过）
# 周日 11:30 UTC = 北京周日晚 19:30：无交易/无 lake-sync(1-5)/控制台静默。
# 重试 10 次×60s：DuckDB 文件锁独占，若与 web 短连接撞锁则等下一轮（web 请求 <2s）。
cd /home/ubuntu/stock-screener
for i in 1 2 3 4 5 6 7 8 9 10; do
    echo "=== $(date -u '+%F %T') attempt $i ==="
    if .venv/bin/python scripts/lake_backfill.py t1-delist; then
        echo "T1_DELIST_OK"
        exit 0
    fi
    echo "attempt $i failed, sleep 60s"
    sleep 60
done
echo "T1_DELIST_FAILED after 10 attempts" >&2
exit 1
