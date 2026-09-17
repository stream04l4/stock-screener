// klineOption 单测（brief D3：ECharts candlestick 数据映射——mock echarts init 断言 option）。
// A股配色红涨绿跌；data 项 [open, close, low, high]（ECharts candlestick 约定，与 vanilla
// SVG OHLC 顺序不同）；成交量 bar 同色弱化；dataZoom inside+slider；tooltip 开高低收量。
import { describe, it, expect } from "vitest";
import { mapKlineData, buildKlineOption, UP, DOWN } from "../klineOption.js";

const ROWS = [
  { date: "2026-09-15", open: 10.0, high: 11.0, low: 9.8, close: 10.8, volume: 12345 },   // 涨（红）
  { date: "2026-09-16", open: 10.8, high: 10.9, low: 10.1, close: 10.2, volume: 9876 },    // 跌（绿）
  { date: "2026-09-17", open: 10.2, high: 10.3, low: 10.15, close: 10.2, volume: 0 },      // 平（close==open → 红）
];

describe("mapKlineData：OHLC → ECharts candlestick 数据映射", () => {
  it("data 项 = [open, close, low, high]（ECharts 约定，非 OHLC 顺序）", () => {
    const { kline } = mapKlineData(ROWS);
    expect(kline[0]).toEqual([10.0, 10.8, 9.8, 11.0]);
    expect(kline[1]).toEqual([10.8, 10.2, 10.1, 10.9]);
  });

  it("dates 升序透传为字符串", () => {
    const { dates } = mapKlineData(ROWS);
    expect(dates).toEqual(["2026-09-15", "2026-09-16", "2026-09-17"]);
  });

  it("成交量 bar 与蜡烛同色弱化（红涨绿跌；opacity 0.55）", () => {
    const { volumes } = mapKlineData(ROWS);
    expect(volumes[0].value).toBe(12345);
    expect(volumes[0].itemStyle.color).toBe(UP);     // 涨 → 红
    expect(volumes[1].itemStyle.color).toBe(DOWN);   // 跌 → 绿
    expect(volumes[0].itemStyle.opacity).toBe(0.55); // 弱化
  });

  it("close==open（平盘）按 A股习惯归涨色（红）", () => {
    const { volumes } = mapKlineData(ROWS);
    expect(volumes[2].itemStyle.color).toBe(UP);
  });

  it("volume 缺失 → 0（不 NaN）", () => {
    const { volumes } = mapKlineData([{ date: "d", open: 1, high: 1, low: 1, close: 1 }]);
    expect(volumes[0].value).toBe(0);
  });

  it("空 rows → 三个空数组（不抛）", () => {
    const r = mapKlineData([]);
    expect(r).toEqual({ dates: [], kline: [], volumes: [] });
  });
});

describe("buildKlineOption：完整 option 形状", () => {
  const opt = buildKlineOption(ROWS);

  it("series[0]=candlestick，红涨绿跌 itemStyle（color=UP/color0=DOWN）", () => {
    expect(opt.series[0].type).toBe("candlestick");
    expect(opt.series[0].itemStyle.color).toBe(UP);
    expect(opt.series[0].itemStyle.color0).toBe(DOWN);
  });

  it("series[1]=bar 成交量（挂第二 xAxis/yAxis）", () => {
    expect(opt.series[1].type).toBe("bar");
    expect(opt.series[1].xAxisIndex).toBe(1);
    expect(opt.series[1].yAxisIndex).toBe(1);
  });

  it("dataZoom = inside + slider（双 xAxis 联动）", () => {
    const types = opt.dataZoom.map((z) => z.type).sort();
    expect(types).toEqual(["inside", "slider"]);
    for (const z of opt.dataZoom) expect(z.xAxisIndex).toEqual([0, 1]);
  });

  it("tooltip：axis trigger + cross axisPointer；formatter 输出 开/高/低/收/量", () => {
    expect(opt.tooltip.trigger).toBe("axis");
    expect(opt.tooltip.axisPointer.type).toBe("cross");
    const html = opt.tooltip.formatter([{ seriesType: "candlestick", dataIndex: 0 }]);
    for (const w of ["开", "高", "低", "收", "量", "2026-09-15"]) expect(html).toContain(w);
    expect(html).toContain("10.80");   // close toFixed(2)
  });

  it("双 grid（K线 + 成交量）；xAxis/yAxis 各 2 条", () => {
    expect(opt.grid.length).toBe(2);
    expect(opt.xAxis.length).toBe(2);
    expect(opt.yAxis.length).toBe(2);
  });

  it("animation=false（数据量大时渲染性能；与 BacktestTab 一致）", () => {
    expect(opt.animation).toBe(false);
  });
});
