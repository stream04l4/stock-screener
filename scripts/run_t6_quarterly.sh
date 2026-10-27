#!/bin/bash
# 季度频 T6 前十大股东全市场重抓（v7.0.18.4，tester DEF-4）
#
# 为什么季度重抓：t6 语义=每只股"全史快照"一次性灌入（done 键 (t6, ts_code)
# 标记后永不重取）→ 新报告期（季度披露截止日过后）不会自动进表，数据冻结
# （2026-10-27 实锤：冻结 28 天仍显示"✅ 最新"）。每个季度披露截止日过后的
# 第 5 天全市场重抓一次（--reset-nodata 清全部 t6 done 键）：
#   - 09-05：中报(06-30)截止 08-31 已过
#   - 11-05：三季(09-30)截止 10-31 已过
#   - 05-05：年报(12-31)+Q1(03-31)截止 04-30 已过
# 11:30 UTC = 北京 19:30 静默窗；全市场 ~5556 只 ×(1s+20s/15 冷却) ≈ 2.5h，
# 期间持 DuckDB 锁，web 数据接口 409"灌数进行中"（设计内降级）。
# 若 5 号恰逢周末 cron 跳过 → 数据多滞后数天，状态面板"缺 X 报告期"徽章如实标注。
# 重试 10 次×60s：启动时与 web 短连接撞锁则等下一轮（web 请求 <2s）。
cd /home/ubuntu/stock-screener
for i in 1 2 3 4 5 6 7 8 9 10; do
    echo "=== $(date -u '+%F %T') attempt $i ==="
    if .venv/bin/python scripts/lake_backfill.py t6 --reset-nodata; then
        echo "T6_QUARTERLY_OK"
        exit 0
    fi
    echo "attempt $i failed, sleep 60s"
    sleep 60
done
echo "T6_QUARTERLY_FAILED after 10 attempts" >&2
exit 1
