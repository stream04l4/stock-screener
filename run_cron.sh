#!/usr/bin/env bash
# 交易日守卫 + 选股运行（配合 cron：UTC 09:35，北京 17:35）。
# A股日K T+1 更新已确认：收盘后约北京 17:30 起可取当日数据，09:35 UTC 起跑安全。
#
# 生产路径失败守卫（9/7 缺陷修复）：交易日历守卫本身也依赖 BaoStock。若 BaoStock
# 封禁/降级导致 login 或 query_trade_dates 失败，旧实现把非零退出误读成"非交易日，
# 跳过"（exit 0），静默吞掉数据源级失败。现在区分三种结果：
#   守卫退出码 0 = 交易日 → 继续选股
#   守卫退出码 1 = 非交易日（数据源正常）→ 跳过
#   守卫退出码 3 = 数据源级失败 → 写 run_status sidecar + 向 cron 报告失败（不静默）
set -euo pipefail
cd /home/ubuntu/stock-screener || exit 1

TODAY=$(date -u +%F)

GUARD_RC=0
# 守卫自身也加进程级超时（300s）：半封禁态下 query_trade_dates 可能挂起，
# 挂起被 timeout 杀掉 → GUARD_RC=124 → 落入 * 分支显式失败（下方补 sidecar）。
timeout -k 30 300 .venv/bin/python - "$TODAY" <<'PYEOF' || GUARD_RC=$?
import sys
import baostock as bs

today = sys.argv[1]

# sidecar / 异常类型（纯 stdlib + client，离线可导入）；导入失败则降级为不写 sidecar
try:
    from screener import runstatus
except Exception:
    runstatus = None
try:
    from screener.data.baostock_client import DataSourceError
except Exception:
    DataSourceError = RuntimeError


def fail(msg):
    """数据源级失败：显式报错 + 写 status=failed sidecar，退出码 3（≠非交易日）。"""
    print(f"[guard] ERROR: {msg}", file=sys.stderr)
    if runstatus is not None:
        try:
            runstatus.write_failed_sidecar("output", today, today, DataSourceError(msg))
        except Exception as e:
            print(f"[guard] 写 sidecar 失败: {e}", file=sys.stderr)
    raise SystemExit(3)


lg = bs.login()
if lg.error_code != "0":
    fail(f"baostock login 失败: {lg.error_msg}（数据源不可用，无法判定交易日）")

rs = bs.query_trade_dates(start_date=today, end_date=today)
row = None
if rs.error_code != "0":
    fail(f"query_trade_dates 失败: error_code={rs.error_code} {rs.error_msg}")
else:
    while rs.next():
        row = rs.get_row_data()

try:
    bs.logout()
except Exception:
    pass

# 退出码 0 = 交易日；1 = 非交易日（周末/节假日，数据源正常）
raise SystemExit(0 if row and row[1] == "1" else 1)
PYEOF

# 主运行进程级超时上限（秒）。正常 v2 增量日运行约 1–3min；45min 上限足以覆盖
# 冷启动，又能抓住 BaoStock 半封禁/限流态下"数据查询无限挂起"（baostock 是 ctypes
# C 库，socket 在原生层，Python setdefaulttimeout 无效 → 只能进程级 timeout）。
RUN_TIMEOUT=2700

case "$GUARD_RC" in
  0)
    echo "[$TODAY] 交易日，开始选股 ..."
    set +e
    timeout -k 60 "$RUN_TIMEOUT" .venv/bin/python -m screener --date "$TODAY" --config config/strategy.yaml
    SC=$?
    set -e
    if [ "$SC" -ne 0 ]; then
      # __main__ 的异常处理器只在 Python 层异常时写 sidecar；被 timeout 杀掉(124)或
      # 硬崩溃时它没机会执行 → 这里兜底补写 run_status sidecar（幂等：同路径原子覆盖）。
      .venv/bin/python - "$TODAY" "$SC" <<'PYEOF2' || true
import sys
from screener import runstatus
from screener.data.baostock_client import DataSourceError

today, sc = sys.argv[1], int(sys.argv[2])
msg = (f"选股运行异常终止 exit={sc}"
       + ("（超过 45min 超时上限，疑似 BaoStock 数据查询挂起/半封禁态）" if sc == 124
          else "（非零退出，详见 logs/）"))
runstatus.write_failed_sidecar("output", today, today, DataSourceError(msg))
print(f"[cron] 已补写失败 sidecar: {msg}", file=sys.stderr)
PYEOF2
      echo "[$TODAY] 选股运行失败 (exit=$SC)，已写 run_status sidecar，不静默" >&2
      exit "$SC"
    fi
    ;;
  1)
    echo "[$TODAY] 非交易日，跳过"
    exit 0
    ;;
  *)
    # 数据源级失败（BaoStock 登录/查询失败）或守卫超时(124)：向 cron 报告失败，绝不静默。
    # 守卫自身抛错时已写 sidecar；被 timeout 杀掉(124)时没机会写 → 这里兜底补写（幂等）。
    if [ "$GUARD_RC" -eq 124 ]; then
      .venv/bin/python - "$TODAY" <<'PYEOF3' || true
import sys
from screener import runstatus
from screener.data.baostock_client import DataSourceError

today = sys.argv[1]
runstatus.write_failed_sidecar(
    "output", today, today,
    DataSourceError("交易日历守卫超时（>300s，疑似 BaoStock 数据查询挂起/半封禁态）"))
print("[cron] 已补写守卫超时 sidecar", file=sys.stderr)
PYEOF3
      echo "[$TODAY] 交易日历守卫超时（BaoStock 数据源疑似挂起），已写 run_status sidecar" >&2
    else
      echo "[$TODAY] 交易日历守卫失败（BaoStock 数据源不可用），已写 run_status sidecar，不静默跳过" >&2
    fi
    exit "$GUARD_RC"
    ;;
esac
