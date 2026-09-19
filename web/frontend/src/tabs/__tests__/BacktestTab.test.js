// v6.2.1 S2 组件回归：BacktestTab 回测参数区（backtest + reinvest 两段编辑）。
//
// mock 点：api client（零网络）+ queryRegistry.invalidate（隔离计数）+ echarts（动态 import，
// happy-dom 无 canvas → stub init/setOption/resize/dispose）。
// 契约（brief S2"load full json → 编辑子集 → PUT full"保存模式）：
// - "回测参数"区 = StrategyCards(groups=BACKTEST_GROUPS)：只读态展示 backtest/reinvest 两段；
// - 【✏️ 编辑模式】→ draft = GET /api/strategy 完整 JSON 深拷贝（FieldEditor 只改子集段）；
// - 【💾 保存策略】→ 逐字段校验 → PUT **整份 payload**（其他段原样带回，一个 key 不丢）；
//   成功 → toast + invalidate("strategy")；400 → 保持编辑态显示 errors。
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia } from "pinia";

const apiMock = vi.fn();
vi.mock("../../api/client.js", () => ({
  api: (...a) => apiMock(...a),
  isBackfillErr: (e) => !!(e && e.status === 409 && e.body && e.error === "lake_backfill_in_progress"),
}));
const invalidateMock = vi.fn();
vi.mock("../../live/queryRegistry.js", async (orig) => ({
  ...(await orig()), invalidate: (...a) => invalidateMock(...a),
}));
// echarts stub（动态 import("echarts")；happy-dom 无 canvas，只断言调用形状）
vi.mock("echarts", () => ({
  init: vi.fn(() => ({ setOption: vi.fn(), resize: vi.fn(), dispose: vi.fn() })),
}));

const BacktestTab = (await import("../BacktestTab.vue")).default;
const { useToastStore } = await import("../../stores/toastStore.js");

// 完整策略 JSON（16 段全——PUT full 断言"其他段不丢"用）
const STRATEGY_JSON = {
  technical: { ma_period: 20, min_return_pct: 3 },
  dividend: { window_days: 365, min_yield_pct: 4 },
  industry: { rank_by: "roeAvg", top_pct: 30 },
  fundamental: { roe_min_pct: 8 },
  universe: { a_share_prefixes: ["sh.60"], listing_min_trading_days: 250 },
  scoring: { mode: "zscore", top_n: 50, weights: { technical: 0.3, dividend: 0.2, industry: 0.1, fundamental: 0.4 } },
  badges: { industry_top_pct: 30, fscore_min: 5 },
  hard_filter: { st_enabled: true, min_consecutive_div_years: 3 },
  backtest: { start: "2021-01-01", end: "2026-08-31", rebalance: "monthly", top_n: 10, risk_free_pct: 2.0 },
  reinvest: { target_ttm_yield_pct: 4, yield_pctile_lookback_years: 5, dps_smooth_years: 3, dps_growth_years: 5 },
  datasource: { primary: "tencent" },
  data: { kline_calendar_days_back: 800 },
  crosscheck: { enabled: false },
  health: { enabled: true },
  canonical: { enabled: true },
  lake: { baostock_daily_budget: 20000 },
};

// 回测产物（/api/backtest）：最小 equity_curve（stub echarts 后只走 setOption）
const BACKTEST = {
  equity_curve: [
    { date: "2026-08-31", strategy: 1.0 },
    { date: "2026-09-30", strategy: 1.05 },
  ],
  metrics: { window: { start: "2021-01-31", end: "2026-09-30", n_days: 1400 },
             full: { annual_return_pct: 8.5, sharpe: 0.9, max_drawdown_pct: -12.3 },
             slices: { "1y": { annual_return_pct: 6.2 } } },
  holdings: [], benchmarks: ["sh.000300"], report_md: "",
};

function mockApi() {
  apiMock.mockImplementation(async (path, opts = {}) => {
    if (path === "/api/strategy" && (!opts.method || opts.method === "GET")) {
      return { json: JSON.parse(JSON.stringify(STRATEGY_JSON)), raw: "# raw" };
    }
    if (path === "/api/backtest") return JSON.parse(JSON.stringify(BACKTEST));
    if (path === "/api/strategy" && opts.method === "PUT") {
      return { ok: true, backup: "/tmp/config/strategy.yaml.bak" };
    }
    throw new Error("unmocked: " + path);
  });
}

let appPinia = null;
const mk = () => {
  const pinia = createPinia();
  appPinia = pinia;
  return mount(BacktestTab, { attachTo: document.body, global: { plugins: [pinia] } });
};
const toastMsg = () => useToastStore(appPinia).msg;

beforeEach(() => {
  vi.stubGlobal("confirm", vi.fn(() => true));
  apiMock.mockReset();
  invalidateMock.mockClear();
  mockApi();
});

describe("S2 · BacktestTab 回测参数区（backtest + reinvest）", () => {
  it("渲染：净值曲线卡 + 回测参数卡；只读态展示 backtest/reinvest 两段值", async () => {
    const w = mk();
    await flushPromises();
    expect(w.find("#bt-chart").exists()).toBe(true); // 净值曲线区仍在（原功能不回归）
    expect(w.find(".bt-params").exists()).toBe(true); // 回测参数区（S2 新增）
    const text = w.text();
    expect(text).toContain("历史回测");   // backtest 段卡片标题
    expect(text).toContain("再投资参考"); // reinvest 段卡片标题
    // 只读态值展示
    expect(text).toContain("2021-01-01"); // backtest.start
    expect(text).toContain("monthly");    // backtest.rebalance
    expect(text).toContain("4");          // reinvest.target_ttm_yield_pct
    w.unmount();
  });

  it("编辑态：FieldEditor 出现；改 backtest.top_n → 【💾 保存策略】→ PUT **完整 payload**（其他段不丢）", async () => {
    const w = mk();
    await flushPromises();
    await w.find("#btn-bt-edit").trigger("click");
    // 编辑态控件出现（backtest.top_n int → number input；rebalance enum2 → select）
    expect(w.findAll(".bt-params input[type='number']").length).toBeGreaterThan(0);
    // 改 backtest.top_n：10 → 15（第一个出现的 number input 顺序不保证，按 label 定位太脆——
    // 直接遍历找 value=10 的 number input）
    const nums = w.findAll(".bt-params input[type='number']");
    const target = nums.find((i) => i.element.value === "10");
    expect(target).toBeTruthy();
    await target.setValue("15");
    await flushPromises();
    await w.find("#btn-bt-save").trigger("click");
    await flushPromises();
    // PUT 被调用，payload = 完整 JSON（16 段全在）+ backtest.top_n=15
    const putCall = apiMock.mock.calls.find(([p, o]) => p === "/api/strategy" && o.method === "PUT");
    expect(putCall).toBeTruthy();
    const payload = JSON.parse(putCall[1].body);
    expect(Object.keys(payload).length).toBe(Object.keys(STRATEGY_JSON).length); // 段数不丢
    for (const k of Object.keys(STRATEGY_JSON)) expect(payload).toHaveProperty(k);
    expect(payload.backtest.top_n).toBe(15);
    // 未编辑段原样带回（抽样）
    expect(payload.technical.ma_period).toBe(20);
    expect(payload.lake.baostock_daily_budget).toBe(20000);
    // 成功路径：toast + invalidate("strategy") + 退出编辑态
    expect(toastMsg()).toContain("回测参数已更新");
    expect(invalidateMock).toHaveBeenCalledWith("strategy");
    expect(w.find("#btn-bt-edit").classes()).not.toContain("hidden");
    w.unmount();
  });

  it("本地校验拦截：backtest.top_n 改非法（非整数文本）→ 不提交 PUT，msg-box 报错", async () => {
    const w = mk();
    await flushPromises();
    await w.find("#btn-bt-edit").trigger("click");
    const nums = w.findAll(".bt-params input[type='number']");
    const target = nums.find((i) => i.element.value === "10");
    await target.setValue("abc"); // number input 允许输入文本（type=number 的 value 可能为空）
    await flushPromises();
    await w.find("#btn-bt-save").trigger("click");
    await flushPromises();
    expect(apiMock.mock.calls.some(([p, o]) => p === "/api/strategy" && o.method === "PUT")).toBe(false);
    // 保持编辑态（400/本地校验失败语义：可改正后重试）
    expect(w.find("#btn-bt-save").classes()).not.toContain("hidden");
    w.unmount();
  });

  it("PUT 400 → 保持编辑态 + msg-box 显示 errors", async () => {
    const w = mk();
    await flushPromises();
    apiMock.mockImplementation(async (path, opts = {}) => {
      if (path === "/api/strategy" && (!opts.method || opts.method === "GET")) {
        return { json: JSON.parse(JSON.stringify(STRATEGY_JSON)), raw: "# raw" };
      }
      if (path === "/api/backtest") return JSON.parse(JSON.stringify(BACKTEST));
      if (path === "/api/strategy" && opts.method === "PUT") {
        const e = new Error("bad"); e.status = 400;
        e.body = { detail: { errors: ["backtest.top_n=99 超出范围 [1,50]"] } }; throw e;
      }
      throw new Error("unmocked: " + path);
    });
    await w.find("#btn-bt-edit").trigger("click");
    const nums = w.findAll(".bt-params input[type='number']");
    const target = nums.find((i) => i.element.value === "10");
    await target.setValue("99"); // 本地 int 校验通过（99 是整数），后端范围校验拒绝
    await flushPromises();
    await w.find("#btn-bt-save").trigger("click");
    await flushPromises();
    expect(w.text()).toContain("保存被拒绝（400）");
    expect(w.text()).toContain("backtest.top_n=99 超出范围 [1,50]");
    expect(w.find("#btn-bt-save").classes()).not.toContain("hidden"); // 保持编辑态
    w.unmount();
  });

  it("取消 → 退出编辑态 + invalidate('strategy')", async () => {
    const w = mk();
    await flushPromises();
    await w.find("#btn-bt-edit").trigger("click");
    await w.find("#btn-bt-cancel").trigger("click");
    expect(w.find("#btn-bt-edit").classes()).not.toContain("hidden");
    expect(invalidateMock).toHaveBeenCalledWith("strategy");
    w.unmount();
  });
});
