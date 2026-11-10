// tradingWindow —— 交易时段门（与 scripts/position_alert.py 同一口径）：
// 北京时间 周一至周五 09:15-11:35 / 12:55-15:05。v7.0.30 轮询门：非时段
// 不发实时请求。2026-11-09 = 周一，2026-11-13 = 周五。
import { describe, it, expect } from "vitest";
import { inTradingWindow } from "./tradingWindow.js";

const t = (iso) => inTradingWindow(new Date(iso));

describe("tradingWindow", () => {
  it("上午窗边界 09:15 / 11:35（含端点）", () => {
    expect(t("2026-11-09T09:14:00+08:00")).toBe(false);
    expect(t("2026-11-09T09:15:00+08:00")).toBe(true);
    expect(t("2026-11-09T10:00:00+08:00")).toBe(true);
    expect(t("2026-11-09T11:35:00+08:00")).toBe(true);
    expect(t("2026-11-09T11:36:00+08:00")).toBe(false);
  });

  it("午休 11:36-12:54 非时段", () => {
    expect(t("2026-11-09T12:00:00+08:00")).toBe(false);
    expect(t("2026-11-09T12:54:59+08:00")).toBe(false);
  });

  it("下午窗边界 12:55 / 15:05（含端点）", () => {
    expect(t("2026-11-09T12:55:00+08:00")).toBe(true);
    expect(t("2026-11-09T14:59:00+08:00")).toBe(true);
    expect(t("2026-11-09T15:05:00+08:00")).toBe(true);
    expect(t("2026-11-09T15:06:00+08:00")).toBe(false);
  });

  it("周末非时段", () => {
    expect(t("2026-11-07T10:00:00+08:00")).toBe(false); // 周六
    expect(t("2026-11-08T10:00:00+08:00")).toBe(false); // 周日
  });

  it("周五末窗覆盖 15:05", () => {
    expect(t("2026-11-13T15:05:00+08:00")).toBe(true);
    expect(t("2026-11-13T15:06:00+08:00")).toBe(false);
  });

  it("凌晨 00:00 非时段（h23 格式化回归：hour 不得为 24）", () => {
    expect(t("2026-11-09T00:00:00+08:00")).toBe(false);
    expect(t("2026-11-09T00:30:00+08:00")).toBe(false);
  });

  it("跨时区输入（UTC 表示的同一北京时刻）", () => {
    // 北京 10:00 = UTC 02:00
    expect(t("2026-11-09T02:00:00Z")).toBe(true);
    // 北京 20:00（UTC 12:00）非时段
    expect(t("2026-11-09T12:00:00Z")).toBe(false);
  });
});
