#!/usr/bin/env bash
# Gate 1 观察期盘中延时复测（设计 §5/§6 D1，2026-11-10）：
# push2delay（备源候选）vs 腾讯（现主源）tick 级延时，交易日 3 时点
# （01:35/04:05/06:35 UTC = 北京 09:35 开盘后 / 12:05 午间 / 14:35 午后）。
# 判据（§5）：延时 >3 分钟 → 备源定位降「取价失败兜底」。
# 只读（两源各 1-2 批调用），落盘 data/observation/<date>.json（latency_samples 追加）。
set -uo pipefail
cd /home/ubuntu/stock-screener || exit 1
STAMP=$(date -u +%Y%m%d)
LOG="logs/lake_observation_${STAMP}.log"
timeout -k 10 120 .venv/bin/python scripts/lake_observation.py --latency >> "$LOG" 2>&1
RC=$?
echo "[$(date -u +%FT%TZ)] Gate1 延时探测 rc=$RC（0=OK；详见 $LOG 尾部）"
exit "$RC"
