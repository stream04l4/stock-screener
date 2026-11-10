// 交易时段门（前端版，与 scripts/position_alert.py 的 in_trading_window 同一
// 口径）：北京时间 周一至周五 09:15-11:35 / 12:55-15:05（留缓冲；15:05 末窗
// 覆盖收盘价判定）。节假日不识别——由「非交易日行情静止 + 数据不变」自然消化
// （与 Python 版同策略）。
//
// 用途：v7.0.30 实时刷新——只在交易时段内对 my_positions / strategy_signals
// 发起轮询请求；非时段保留最近一次数据不发网络请求。
export function inTradingWindow(now = new Date()) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "Asia/Shanghai",
    weekday: "short",
    hourCycle: "h23",
    hour: "2-digit",
    minute: "2-digit",
  }).formatToParts(now);
  const get = (t) => {
    const p = parts.find((x) => x.type === t);
    return p ? p.value : undefined;
  };
  const wd = { Mon: 0, Tue: 1, Wed: 2, Thu: 3, Fri: 4, Sat: 5, Sun: 6 }[get("weekday")];
  if (wd === undefined || wd > 4) return false;
  const hm = parseInt(get("hour"), 10) * 60 + parseInt(get("minute"), 10);
  return (9 * 60 + 15) <= hm && hm <= (11 * 60 + 35) ||
         (12 * 60 + 55) <= hm && hm <= (15 * 60 + 5);
}
