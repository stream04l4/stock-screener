#!/bin/bash
# 等东财 clist 恢复后自动重取 stock_master（2026-11-26）
# 探测 EU push2 + SG push2delay 的 clist 端点，任一 200 即执行 refresh_master_em.py
LOG=/home/ubuntu/stock-screener/logs/master_retry_20261126.log
cd /home/ubuntu/stock-screener

probe() {
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 10 \
    "https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=10&po=1&np=1&fltt=2&invt=2&fid=f3&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23&fields=f12,f14" \
    -H 'Referer: https://quote.eastmoney.com/' \
    -A 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)')
  echo "$code"
}

for i in $(seq 1 48); do   # 48 * 45min ≈ 36h
  code=$(probe)
  echo "[$(date -u '+%F %T')] probe #$i push2 EU -> $code" >> "$LOG"
  if [ "$code" = "200" ]; then
    echo "[$(date -u '+%F %T')] clist 恢复，执行 refresh_master_em.py" >> "$LOG"
    if .venv/bin/python scripts/refresh_master_em.py >> "$LOG" 2>&1; then
      echo "[$(date -u '+%F %T')] refresh_master_em.py 成功 exit=0" >> "$LOG"
    else
      echo "[$(date -u '+%F %T')] refresh_master_em.py 失败 exit=$?（可能中途又 502，下轮重试）" >> "$LOG"
    fi
    break
  fi
  sleep 2700
done
echo "[$(date -u '+%F %T')] 重试循环结束" >> "$LOG"
