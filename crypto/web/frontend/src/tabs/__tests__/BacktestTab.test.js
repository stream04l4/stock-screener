// BacktestTab（v7.0.48）：运行回测 → 渲染逐币结果/净值曲线/持仓中（mock fetch）。
import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import BacktestTab from "../BacktestTab.vue";

const BT_RESULT = {
  ok: true, elapsed_s: 0.1,
  params: { first_drop_pct: 70, add_grid_pct: 1, base_amount_usdt: 10,
            growth: 1.05, take_profit_mult: 10, fee_bps: 10, lookback_h: 8760 },
  results: [{
    symbol: "TESTUSDT", n_bars: 5, start: "2020-01-01", end: "2020-01-05",
    rounds: [{ entry_date: "2020-01-03", entry: 30, exit_date: "2020-01-04",
              exit: 300, k: 0, invested: 10, proceeds: 99.8, pnl_pct: 898 }],
    open_round: { entry_date: "2020-01-05", entry: 10, k: 0, spent: 10,
                  value: 9.99, unrealized_pct: -0.1, tp_line: 100 },
    events: [
      { date: "2020-01-03", type: "entry", level: 0, price: 30 },
      { date: "2020-01-04", type: "exit", level: 0, price: 300 },
      { date: "2020-01-05", type: "entry", level: 0, price: 10 },
    ],
    realized_cash: 89.8, realized_pnl_pct: 898,
    final_equity: 99.79, mdd_pct: 0,
    equity_curve: [["2020-01-01", 0], ["2020-01-03", 0], ["2020-01-05", 99.79]],
  }],
};

describe("BacktestTab", () => {
  beforeEach(() => vi.unstubAllGlobals());

  it("点击运行回测 → 渲染币卡/轮次表/持仓中/净值曲线", async () => {
    const fn = vi.fn(async (path, opts) => {
      if (path === "/crypto/api/backtest" && opts && opts.method === "POST") {
        return { ok: true, status: 200, json: async () => BT_RESULT };
      }
      return { ok: false, status: 404, json: async () => ({}) };
    });
    vi.stubGlobal("fetch", fn);
    const w = mount(BacktestTab);
    await flushPromises();
    expect(w.text()).toContain("运行回测");

    await w.find("button.primary").trigger("click");
    await flushPromises();

    expect(fn.mock.calls.length).toBe(1);
    expect(w.text()).toContain("TESTUSDT");
    expect(w.text()).toContain("已了结轮次");
    expect(w.text()).toContain("持仓中");
    expect(w.text()).toContain("100"); // 止盈线
    expect(w.find("svg.chart").exists()).toBe(true);
    expect(w.find("svg.chart polyline").exists()).toBe(true);
    // 时间轴刻度（跨度 4 天 < 366 → 完整日期 YYYY-MM-DD）
    expect(w.text()).toContain("2020-01-01");
    expect(w.text()).toContain("2020-01-05");
    // 事件标记（限定 svg 内，图例不算）：建仓 2 + 平仓 1（无加仓）
    const svg = w.find("svg.chart");
    expect(svg.findAll(".mk-entry").length).toBe(2);
    expect(svg.findAll(".mk-exit").length).toBe(1);
    expect(svg.findAll(".mk-add").length).toBe(0);
    // 图例
    expect(w.text()).toContain("加仓（每级）");
    expect(w.text()).toContain("悬停标记看明细");
    expect(w.text()).toContain("耗时 0.1s");
  });

  it("回测失败 → 显示错误信息", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({
      ok: false, status: 500, statusText: "boom",
      json: async () => ({ detail: "湖未就绪" }),
    })));
    const w = mount(BacktestTab);
    await flushPromises();
    await w.find("button.primary").trigger("click");
    await flushPromises();
    expect(w.text()).toContain("湖未就绪");
  });
});
