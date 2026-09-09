#!/usr/bin/env bash
# 交易日守卫 + 选股运行（配合 cron：UTC 09:35，北京 17:35）。
# A股日K T+1 更新已确认：收盘后约北京 17:30 起可取当日数据，09:35 UTC 起跑安全。
#
# 生产路径失败守卫（9/7 缺陷修复）：交易日历守卫本身也依赖 BaoStock。若 BaoStock
# 封禁/降级导致 login 或 query_trade_dates 失败，旧实现把非零退出误读成"非交易日，
# 跳过"（exit 0），静默吞掉数据源级失败。现在区分三种结果：
#   守卫退出码 0 = 交易日 → 继续选股
#   守卫退出码 1 = 非交易日（数据源正常）→ 跳过
# 守卫退出码 3 = 数据源级失败 → 写 run_status sidecar + 向 cron 报告失败（不静默）
# fix r4：login + query_trade_dates 加重试（最多 3 次、指数退避 2s/5s）——一次瞬时抖动
# （如"网络接收错误"）不再直接 exit 3 跳过整个 cron；3 次全失败才走上述 exit 3。
# fix r5：query_trade_dates 成功后把 (today,is_trading) append 到 cache/trade_calendar.csv
# （本地静态交易日历，数据层 _prev_trade_day 只读它做"上一交易日"缺口判定，零 live 依赖；
# 失败/超时不写，下次成功再补）。
set -euo pipefail
cd /home/ubuntu/stock-screener || exit 1

TODAY=$(date -u +%F)

GUARD_RC=0
# 守卫自身也加进程级超时（300s）：半封禁态下 query_trade_dates 可能挂起，
# 挂起被 timeout 杀掉 → GUARD_RC=124 → 落入 * 分支显式失败（下方补 sidecar）。
timeout -k 30 300 .venv/bin/python - "$TODAY" <<'PYEOF' || GUARD_RC=$?
import os
import sys
import time
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


# fix r4：login + query_trade_dates 加**重试**（最多 3 次、指数退避 2s/5s）——今天 09:35
# cron 死在守卫的 login 瞬时抖动（"网络接收错误"），旧实现一次抖动就 exit 3 跳过整个
# cron。任一次成功即用；3 次全失败才走 fail()（exit 3 + sidecar）。
# baostock 是 ctypes C 库，单次调用挂起无法进程内超时——重试循环总时长受外层
# `timeout -k 30 300`（父级进程级）约束：某次 login/query 挂起 → 被 timeout 杀掉 →
# 整个守卫 exit 124 → run_cron.sh * 分支补 sidecar（现有语义不变）。
def _query_trade_row():
    """login + query_trade_dates(today) 一次尝试。

    :return: (ok, row_or_errmsg)。ok=True → row=[calendar_date, is_trading_day]；
             ok=False → errmsg（失败原因，供重试日志/最终 fail）。
    """
    lg = bs.login()
    if lg.error_code != "0":
        return False, f"baostock login 失败: {lg.error_msg}"
    try:
        rs = bs.query_trade_dates(start_date=today, end_date=today)
        if rs.error_code != "0":
            return False, f"query_trade_dates 失败: error_code={rs.error_code} {rs.error_msg}"
        row = None
        while rs.next():
            row = rs.get_row_data()
        if row is None:
            return False, "query_trade_dates 成功但无返回行（今日不在日历范围？）"
        return True, row
    finally:
        # 无论成败都 logout：失败重试时若会话仍开着，bs.login() 重入行为未定义
        try:
            bs.logout()
        except Exception:
            pass


row = None
last_err = ""
for attempt in (1, 2, 3):
    ok, val = _query_trade_row()
    if ok:
        row = val
        break
    last_err = val
    print(f"[guard] 第 {attempt}/3 次尝试失败: {val}", file=sys.stderr)
    if attempt < 3:
        time.sleep(2 * (2 ** (attempt - 1)))   # 指数退避：2s、5s

if row is None:
    fail(f"baostock 交易日历查询重试 3 次均失败（数据源不可用，无法判定交易日）；最后错误: {last_err}")


# fix r5：query_trade_dates **成功**后把 (today, is_trading) 落本地静态交易日历
# cache/trade_calendar.csv——数据层 fetchers._prev_trade_day 只读该文件做"上一交易日"
# 缺口判定（零 live BaoStock 依赖）。失败/超时不写（下次成功再补）；写失败不影响守卫
# 退出码（日历是尽力增强，缺失时数据层回退"前一个日历日"，保守无害）。
def _record_calendar(today, row):
    try:
        path = os.path.join("cache", "trade_calendar.csv")
        rows = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                for ln in fh.read().splitlines():
                    s = ln.strip()
                    if not s or s.startswith("stock-screener-cache") or s.startswith("date,"):
                        continue
                    parts = s.split(",")
                    if len(parts) >= 2 and parts[0].strip():
                        rows[parts[0].strip()] = parts[1].strip()
        rows[today] = row[1]                       # 按日期去重（同日重复运行→最新结果覆盖）
        items = sorted(rows.items())[-500:]        # 升序 + 保留最近 ~500 行（~2 年交易日）
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + f".tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("date,is_trading\n")
            for d, flag in items:
                fh.write(f"{d},{flag}\n")
        os.replace(tmp, path)                      # 原子写（与 DiskCache 同语义）
    except Exception as e:
        print(f"[guard] 交易日历写入失败（不影响守卫退出码）: {e}", file=sys.stderr)


_record_calendar(today, row)

# 退出码 0 = 交易日；1 = 非交易日（周末/节假日，数据源正常）
raise SystemExit(0 if row[1] == "1" else 1)
PYEOF

# 主运行进程级超时上限（秒）。正常 v2 增量日运行约 1–3min；60min 上限足以覆盖
# 冷启动，又能抓住 BaoStock 半封禁/限流态下"数据查询无限挂起"（baostock 是 ctypes
# C 库，socket 在原生层，Python setdefaulttimeout 无效 → 只能进程级 timeout）。
# TL 拍板 2026-09-09：首次切换日 bootstrap（cap=8000 → 全市场 ~5207 只各 1 次腾讯 K线
# ≈38min + 快照/选股）会逼近 2700s → 临时提到 5400s；稳态日运行 1–3min，切换完成后可回退。
# fix r5（TL 拍板 2026-09-09）：稳态日误判 gapped 修复后，稳态日运行 ~1-3min、真缺口日
# bootstrap ≤~40min → 回退到 3600s（覆盖最坏真切换日，同时比 5400 更早抓住挂死）。
RUN_TIMEOUT=3600

case "$GUARD_RC" in
  0)
    echo "[$TODAY] 交易日，开始选股 ..."

    # ---- fix round 2：BaoStock 股票池/行业预取（子进程隔离）----
    # v4 目标"日常路径不依赖 BaoStock 健康"，但 all_stock/industry 当日缓存 miss / TTL
    # 过期时仍会 live 调 query_all_stock / query_stock_industry。BaoStock 是 ctypes C 库，
    # Python setdefaulttimeout 无效 → 半封禁态下这些查询无限挂起，会把主运行拖到 45min
    # 进程超时才失败。这里在独立子进程里预取（复用 DataFetcher/DiskCache 写路径、原子落盘、
    # 幂等），父级用 timeout -k 30 180 兜底：成功 → 主运行命中缓存（0 次 live）；
    # 失败/超时 → 导出 BS_UNIVERSE_STALE_OK=1，主运行走 ≤stale_max_days 天陈旧池回退。
    # **不中断**：预取失败只降级股票池/行业为陈旧快照（安全性论证见 fetchers.all_stock
    # docstring），主筛选继续。
    set +e
    timeout -k 30 180 .venv/bin/python scripts/prefetch_universe.py "$TODAY"
    PF=$?
    set -e
    if [ "$PF" -ne 0 ]; then
      export BS_UNIVERSE_STALE_OK=1
      echo "[$TODAY] BaoStock 股票池/行业预取失败 (exit=$PF) → 主运行启用陈旧回退 (BS_UNIVERSE_STALE_OK=1)" >&2
    else
      echo "[$TODAY] BaoStock 股票池/行业预取成功（主运行命中缓存）"
    fi

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
       + ("（超过 60min 超时上限，疑似 BaoStock 数据查询挂起/半封禁态）" if sc == 124
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
