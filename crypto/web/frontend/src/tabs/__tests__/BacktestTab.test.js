// BacktestTab（v7.0.53）：运行回测 → 渲染逐币结果/净值曲线/价格曲线
// （对数坐标）/总外部投入/持仓中（mock fetch）。
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
    final_equity: 89.89, mdd_pct: 0,
    external_invested: 10, wallet: 79.9,
    equity_curve: [["2020-01-01", 0], ["2020-01-03", 0], ["2020-01-05", 89.89]],
    price_curve: [["2020-01-01", 100], ["2020-01-03", 30],
                  ["2020-01-04", 300], ["2020-01-05", 10]],
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
    // 时间范围输入：两个日期框；缺省全史（body start/end 为空）
    const dateInputs = w.findAll('input[type="date"]');
    expect(dateInputs.length).toBe(2);
    const body0 = JSON.parse(fn.mock.calls[0][1].body);
    expect(body0.start).toBe("");
    expect(body0.end).toBe("");
    expect(w.text()).toContain("全史");
    expect(w.text()).toContain("TESTUSDT");
    expect(w.text()).toContain("已了结轮次");
    expect(w.text()).toContain("持仓中");
    expect(w.text()).toContain("100"); // 止盈线
    // 总结模块：总外部投入（账户模型 v7.0.53）
    expect(w.text()).toContain("总外部投入");
    expect(w.text()).toContain("10 USDT");
    // 双图：净值 + 价格
    const svgs = w.findAll("svg.chart");
    expect(svgs.length).toBe(2);
    expect(svgs[0].find("polyline").exists()).toBe(true);
    expect(svgs[1].find("polyline").exists()).toBe(true);
    expect(w.text()).toContain("价格曲线（对数坐标 · 标记 = 成交价）");
    // 时间轴刻度（跨度 4 天 < 366 → 完整日期 YYYY-MM-DD）
    expect(w.text()).toContain("2020-01-01");
    expect(w.text()).toContain("2020-01-05");
    // 净值图事件标记：建仓 2 + 平仓 1（无加仓）
    const svg = svgs[0];
    expect(svg.findAll(".mk-entry").length).toBe(2);
    expect(svg.findAll(".mk-exit").length).toBe(1);
    expect(svg.findAll(".mk-add").length).toBe(0);
    // 价格图事件标记：同样 3 个（落在精确成交价上）
    const psvg = svgs[1];
    expect(psvg.findAll(".mk-entry").length).toBe(2);
    expect(psvg.findAll(".mk-exit").length).toBe(1);
    // 价格图对数 y 轴刻度（10~300 区间内的 1/2/5 网格）
    const ptickTexts = psvg.findAll("text").map((t) => t.text());
    expect(ptickTexts).toContain("200");
    expect(ptickTexts).toContain("50");
    // 图例
    expect(w.text()).toContain("加仓（每级）");
    expect(w.text()).toContain("悬停标记看明细");
    expect(w.text()).toContain("耗时 0.1s");
  });

  it("设置回测时间范围 → body 携带 start/end 且标题显示范围", async () => {
    const fn = vi.fn(async (path, opts) => {
      if (path === "/crypto/api/backtest" && opts && opts.method === "POST") {
        return { ok: true, status: 200, json: async () => BT_RESULT };
      }
      return { ok: false, status: 404, json: async () => ({}) };
    });
    vi.stubGlobal("fetch", fn);
    const w = mount(BacktestTab);
    await flushPromises();

    const dateInputs = w.findAll('input[type="date"]');
    await dateInputs[0].setValue("2020-01-03");
    await w.find("button.primary").trigger("click");
    await flushPromises();

    const body = JSON.parse(fn.mock.calls[0][1].body);
    expect(body.start).toBe("2020-01-03");
    expect(body.end).toBe("");
    expect(w.text()).toContain("2020-01-03 → 最新");
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
