#!/usr/bin/env bash
# 交易日守卫 + 选股运行（配合 cron：UTC 09:35，北京 17:35）。
# A股日K T+1 更新已确认：收盘后约北京 17:30 起可取当日数据，09:35 UTC 起跑安全。
set -euo pipefail
cd /home/ubuntu/stock-screener || exit 1

TODAY=$(date -u +%F)

# 用交易日历判断今天是否交易日（BaoStock query_trade_dates，结果走本地缓存）
if ! .venv/bin/python - "$TODAY" <<'EOF'
import sys
from datetime import date
import baostock as bs

today = sys.argv[1]
bs.login()
rs = bs.query_trade_dates(start_date=today, end_date=today)
row = None
while rs.error_code == "0" and rs.next():
    row = rs.get_row_data()
bs.logout()
# 退出码 0 = 交易日；1 = 非交易日（周末/节假日）
raise SystemExit(0 if row and row[1] == "1" else 1)
EOF
then
    echo "[$TODAY] 非交易日，跳过"
    exit 0
fi

echo "[$TODAY] 交易日，开始选股 ..."
.venv/bin/python -m screener --date "$TODAY" --config config/strategy.yaml
