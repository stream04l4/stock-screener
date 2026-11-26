#!/usr/bin/env bash
# 数据湖 P3 每日增量（kline_daily 缺口 + valuation_daily 快照 + index_daily 近 N 日）。
# 背景：v6 lake 切 primary 后，daily 增量此前只靠 Web「开始同步」按钮手工触发，
# 09-23 无人点 → 湖停更 8 天（2026-10-01 排查确认；run_cron 选股 cron 同期也缺失）。
# 本脚本职责（早期 run_phase_c.sh 模式，该脚本已删）：
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
mkdir -p logs data/lake

# 原子文件锁防重入（替代 pgrep TOCTOU 竞态，不误伤 status 等只读子命令）
exec 9>data/lake/lake_sync_daily.lock
if ! flock -n 9; then
  echo "[$(date -u +%FT%TZ)] lake_sync_daily 锁已被持有，跳过本次增量"
  exit 0
fi

STAMP=$(date -u +%Y%m%d)
LOG="logs/lake_sync_${STAMP}.log"

run_step() {  # run_step <label> <timeout-s> <cmd...>
  local label="$1" tmo="$2"; shift 2
  local out="" rc=1 i
  for i in 1 2 3; do
    out=$( { timeout -k 30 "$tmo" "$@"; } 2>&1 )
    rc=$?
    if [ "$rc" -eq 0 ]; then
      printf '%s\n' "$out" >> "$LOG"; return 0
    fi
    if printf '%s' "$out" | grep -q "Conflicting lock"; then
      echo "[$(date -u +%FT%TZ)] $label: DuckDB 锁竞争 → 10s 后重试 ($i/3)"
      sleep 10
      continue
    fi
    printf '%s\n' "$out" >> "$LOG"
    return "$rc"
  done
  # 重试耗尽仍锁冲突：确保输出落盘不丢日志
  printf '%s\n' "$out" >> "$LOG"
  return "$rc"
}

echo "[$(date -u +%FT%TZ)] 湖增量开始 → $LOG"
# 先同步 T4 分红送配增量（EM 主通道+tushare 抽样校验），保证 subsequent incremental 的 af_rebase 基于完整事件表
run_step "T4分红增量" 300 .venv/bin/python scripts/lake_backfill.py t4-sync
T4RC=$?
if [ "$T4RC" -ne 0 ]; then
  echo "[$(date -u +%FT%TZ)] T4 分红增量失败 rc=$T4RC（不阻断主增量，日志见 $LOG）"
fi

timeout -k 60 36000 .venv/bin/python scripts/lake_backfill.py incremental >> "$LOG" 2>&1
RC=$?
echo "[$(date -u +%FT%TZ)] 湖增量退出 rc=$RC（0=完成；非0=部分失败，断点续传次日补）"

if [ "$RC" -ne 0 ]; then
  echo "[$(date -u +%FT%TZ)] 湖增量异常退出 rc=$RC → 跳过本轮 selection/timing 预计算以防脏快照覆盖" >> "$LOG"
  echo "[$(date -u +%FT%TZ)] 湖增量异常退出 rc=$RC → 跳过 selection/timing"
else
  run_step "候选池选股" 600 .venv/bin/python -m martingale selection
  SRC=$?
  if [ "$SRC" -ne 0 ]; then
    echo "[$(date -u +%FT%TZ)] 候选池选股失败 rc=$SRC（Web 回退旧快照，日志见 $LOG 尾部）"
  fi

  run_step "选股时机" 300 .venv/bin/python -m martingale timing
  TRC=$?
  if [ "$TRC" -ne 0 ]; then
    echo "[$(date -u +%FT%TZ)] 选股时机失败 rc=$TRC（Web 回退旧快照，日志见 $LOG 尾部）"
  fi
fi

# v1.1（Gate 1 观察期，设计 §6/§8，2026-11-10）：只读交叉验证（零写入——
# 湖 read_only 连接；唯一落盘 data/observation/）。东财 T1 列表/T2 池逐股/
# T3 报表/T7 指数/T4 端点探活 + Fuyao PIT，≈140 调用/日（东财无硬限）。
# 失败不阻断增量（§8）：观察输出是 Gate 2 晋主判据输入，缺失由 TL 补跑
# （.venv/bin/python scripts/lake_observation.py --daily [--date YYYY-MM-DD]）。
# 超时 600→1800s（2026-11-26 复盘：11-11..11-25 连续 8 天 rc=124——东财 502
# 限频时 T2 逐股 3 次退避重试 ≈18-25min，600s 预算不够；正常日 ≈2-5min）。
run_step "Gate1观察" 1800 .venv/bin/python scripts/lake_observation.py --daily
OBC=$?
if [ "$OBC" -ne 0 ]; then
  echo "[$(date -u +%FT%TZ)] Gate1 观察失败 rc=$OBC（不阻断增量；TL 次日补跑）"
fi
exit "$RC"
