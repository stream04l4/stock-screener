// LakeKlineChart 数据映射（纯函数，单测可直接断言 option；报告 §3 T-数据湖）。
//
// A股配色：红涨绿跌——close≥open → --lake-up(#dc2626)，否则 --lake-down(#16a34a)。
// ECharts candlestick 约定：data 项 = [open, close, low, high]（**与 vanilla SVG 的
// OHLC 顺序不同**，此处显式重排）；itemStyle 颜色按"当日涨跌"逐根覆盖全局。
// 成交量 bar 与蜡烛同色弱化（opacity 0.55）。

export const UP = "#dc2626";    // --lake-up（红涨）
export const DOWN = "#16a34a";  // --lake-down（绿跌）

// rows[{date,open,high,low,close,volume}] → {dates, kline, volumes}
export function mapKlineData(rows) {
  const dates = [];
  const kline = [];
  const volumes = [];
  for (const r of rows || []) {
    const o = Number(r.open), c = Number(r.close);
    const l = Number(r.low), h = Number(r.high);
    const up = c >= o;   // A股：红涨绿跌（close≥open → up，与 vanilla 判定一致）
    dates.push(String(r.date));
    kline.push([o, c, l, h]);
    volumes.push({ value: r.volume == null ? 0 : Number(r.volume), itemStyle: { color: up ? UP : DOWN, opacity: 0.55 } });
  }
  return { dates, kline, volumes };
}

// 完整 ECharts option（candlestick + 成交量 bar + dataZoom inside+slider + tooltip 开高低收量）
export function buildKlineOption(rows) {
  const { dates, kline, volumes } = mapKlineData(rows);
  return {
    animation: false,
    // tooltip：十字轴触发；formatter 输出 日期/开/高/低/收/量（与 vanilla SVG tooltip 字段一致）
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "cross" },
      formatter: (params) => {
        if (!params || !params.length) return "";
        const p = params.find((x) => x.seriesType === "candlestick") || params[0];
        const i = p.dataIndex;
        const r = rows[i] || {};
        const c = Number(r.close ?? 0), o = Number(r.open ?? 0);
        const closeColor = c >= o ? UP : DOWN;
        const vol = r.volume == null ? "—" : Number(r.volume).toLocaleString("en-US");
        return (
          `<div style="font-weight:700;margin-bottom:2px">${dates[i] || ""}</div>` +
          `开 <b>${Number(r.open ?? 0).toFixed(2)}</b><br/>` +
          `高 <b>${Number(r.high ?? 0).toFixed(2)}</b><br/>` +
          `低 <b>${Number(r.low ?? 0).toFixed(2)}</b><br/>` +
          `收 <b style="color:${closeColor}">${c.toFixed(2)}</b><br/>` +
          `量 <b>${vol}</b>`
        );
      },
    },
    // 双 grid：上 K线（~72%）/ 下成交量（~28%）——与 vanilla viewBox 布局比例一致
    axisPointer: { link: [{ xAxisIndex: "all" }] },
    grid: [
      { left: 56, right: 16, top: 16, height: "62%" },
      { left: 56, right: 16, top: "74%", height: "18%" },
    ],
    xAxis: [
      { type: "category", data: dates, gridIndex: 0, boundaryGap: true, axisLine: { lineStyle: { color: "#e3e8f0" } }, axisLabel: { show: false } },
      { type: "category", data: dates, gridIndex: 1, boundaryGap: true, axisLine: { lineStyle: { color: "#e3e8f0" } } },
    ],
    yAxis: [
      { scale: true, gridIndex: 0, splitLine: { lineStyle: { color: "#eef2f7" } } },
      { scale: true, gridIndex: 1, splitNumber: 2, axisLabel: { show: false }, splitLine: { show: false } },
    ],
    // dataZoom：inside（滚轮/拖拽）+ slider（底部滑条）——两 xAxis 联动
    dataZoom: [
      { type: "inside", xAxisIndex: [0, 1], start: 0, end: 100 },
      { type: "slider", xAxisIndex: [0, 1], bottom: 4, height: 16 },
    ],
    series: [
      {
        name: "K线",
        type: "candlestick",
        data: kline,
        // A股红涨绿跌：全局 itemStyle + 逐根覆盖（ECharts candlestick 默认 color=涨色，
        // 与 A股习惯相反 → 显式设 color=UP / color0=DOWN 并逐根按 close≥open 覆盖）
        itemStyle: { color: UP, color0: DOWN, borderColor: UP, borderColor0: DOWN },
      },
      {
        name: "成交量",
        type: "bar",
        xAxisIndex: 1,
        yAxisIndex: 1,
        data: volumes,   // 每根 itemStyle 同色弱化（mapKlineData 内已按涨跌着色）
      },
    ],
  };
}
