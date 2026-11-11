// PositionsTab（v8.0.7）：策略当前轮表格 + Gate 模拟盘状态卡（mock fetch）。
import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import PositionsTab from "../PositionsTab.vue";

const POS_RESULT = {
  ready: true,
  params: { first_drop_pct: 50, take_profit_mult: 3 },
  positions: [{
    symbol: "BTCUSDT", entry_date: "2026-02-05", entry: 62909.86,
    k_level: 7, spent_usdt: 133.39, shares: 0.002217, avg_cost: 60154.56,
    last_price: 74408.01, value_usdt: 165, unrealized_usdt: 31.61,
    unrealized_pct: 23.7, tp_price: 188729.58, bars_holding: 5000,
  }],
  gate: { connected: false, detail: "ACCOUNT_LOCKED: api_key deleted",
          btc_avail: null, usdt_avail: null },
};

describe("PositionsTab", () => {
  beforeEach(() => vi.unstubAllGlobals());

  it("渲染策略当前轮表格 + Gate 未连通状态", async () => {
    const fn = vi.fn(async (path) => {
      if (path === "/crypto/api/positions") {
        return { ok: true, status: 200, json: async () => POS_RESULT };
      }
      return { ok: false, status: 404, json: async () => ({}) };
    });
    vi.stubGlobal("fetch", fn);
    const w = mount(PositionsTab);
    await flushPromises();

    expect(fn.mock.calls.length).toBe(1);
    expect(w.text()).toContain("策略当前轮");
    expect(w.text()).toContain("BTCUSDT");
    expect(w.text()).toContain("2026-02-05");
    expect(w.text()).toContain("L7");
    expect(w.text()).toContain("0.002217");
    expect(w.text()).toContain("+31.61");
    expect(w.text()).toContain("188,729.58"); // 止盈线千分位
    expect(w.text()).toContain("未连通");
    expect(w.text()).toContain("ACCOUNT_LOCKED");
  });

  it("无开仓轮 → 空态；Gate 已连通 → 显示余额", async () => {
    const fn = vi.fn(async (path) => {
      if (path === "/crypto/api/positions") {
        return { ok: true, status: 200, json: async () => ({
          ...POS_RESULT, positions: [],
          gate: { connected: true, detail: "ok",
                  btc_avail: 0.002, usdt_avail: 1200.5 },
        })};
      }
      return { ok: false, status: 404, json: async () => ({}) };
    });
    vi.stubGlobal("fetch", fn);
    const w = mount(PositionsTab);
    await flushPromises();

    expect(w.text()).toContain("当前无开仓轮");
    expect(w.text()).toContain("已连通");
    expect(w.text()).toContain("0.002");
    expect(w.text()).toContain("1,200.50");
  });
});
