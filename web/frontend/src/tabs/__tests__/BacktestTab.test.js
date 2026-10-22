// BacktestTab 组件回归（v7.0）：参数表单 + 触发 + SSE 进度 + 结果区。
//
// mock 点：api client（零网络）+ echarts（happy-dom 无 canvas）+ SSE 通道
// （taskStore.__setUseSSEForTest 注入 fake——与 taskStore.test 同款）。
// 契约：
// - 表单：start/end 可选；标的 radio pool(默认)/codes；【▶ 开始回测】→ POST /api/bt/runs；
// - 409 → toast（taskStore 语义）；codes 模式空 codes → 本地 toast 不发请求；
// - SSE done → GET /api/bt/results/{id} → per_stock 表 / buys 表 / summary.md / 净值曲线（echarts stub）。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const apiMock = vi.fn();
vi.mock("../../api/client.js", () => ({
  api: (...a) => apiMock(...a),
  isBackfillErr: (e) => !!(e && e.status === 409 && e.body && e.error === "lake_backfill_in_progress"),
}));
vi.mock("../../live/queryRegistry.js", async (orig) => ({ ...(await orig()) }));
vi.mock("echarts", () => {
  // 真实 echarts ESM 有 default 导出（组件内 mod.default ?? mod）；mock 同步提供
  const m = { init: vi.fn(() => ({ setOption: vi.fn(), resize: vi.fn(), dispose: vi.fn() })) };
  return { ...m, default: m };
});

const BacktestTab = (await import("../BacktestTab.vue")).default;
const { useTaskStore, __setUseSSEForTest } = await import("../../stores/taskStore.js");
const { useToastStore } = await import("../../stores/toastStore.js");

// ---- fake SSE（与 taskStore.test 同款）----
function makeFakeSSE() {
  const inst = { url: null, handlers: {}, closed: false };
  const impl = (url, onEvent, opts) => {
    inst.url = url;
    inst.handlers = { onEvent, onDowngrade: opts && opts.onDowngrade };
    return { failCount: { value: 0 }, alive: { value: true }, close() { inst.closed = true; } };
  };
  inst.message = (data) => inst.handlers.onEvent({ lastId: null, data });
  return { inst, impl };
}

const RESULTS = {
  task_id: "bt_t1",
  per_stock: [
    { ts_code: "sh.601088", name: "中国神华", n_bars: 2600, n_rounds: 3, n_rearms: 2, n_buys: 11,
      total_invested: 210510, final_assets: 660997.72, dividends: 51234.5, total_pnl: 450487.72,
      pnl_pct: 2.14, min_unreal_pct: -0.1, first_buy_date: "2018-03-01", last_buy_date: "2024-02-05",
      last_status: "open", end_date: "2026-10-21" },
    { ts_code: "sz.000651", name: "格力电器", n_bars: 2500, n_rounds: 2, n_rearms: 1, n_buys: 21,
      total_invested: 326102, final_assets: 540050.34, dividends: 60000, total_pnl: 213948.34,
      pnl_pct: 0.656, min_unreal_pct: -0.05, first_buy_date: "2019-01-15", last_buy_date: "2025-01-10",
      last_status: "open", end_date: "2026-10-21" },
  ],
  buys: [
    { ts_code: "sh.601088", seq: 1, date: "2018-03-01", price: 20.5, amount: 100000 },
    { ts_code: "sh.601088", seq: 2, date: "2018-05-12", price: 18.9, amount: 11000 },
    { ts_code: "sz.000651", seq: 1, date: "2019-01-15", price: 42.1, amount: 100000 },
  ],
  equity_curve: [
    { date: "2018-03-01", open_stocks: 1, open_cost: 100000, open_value: 100500, open_pnl: 500, open_pnl_pct: 0.005 },
    { date: "2026-10-21", open_stocks: 2, open_cost: 536612, open_value: 1201048, open_pnl: 664436, open_pnl_pct: 1.238 },
  ],
  summary: "## 回测摘要\n- 标的 2 只\n- 窗口 2018 ~ 2026\n",
  run_meta: { window: { start: "2018-03-01", end: "2026-10-21" }, n_codes: 2, elapsed_s: 1.2 },
};

let sse = null;
function mockApi() {
  apiMock.mockImplementation(async (path, opts = {}) => {
    if (path === "/api/bt/runs" && opts.method === "POST") {
      return { task_id: "bt_t1", pid: 4242, status: "running", out_dir: "/tmp/bt_t1",
               meta: { start: null, end: null, codes: null, pool: true } };
    }
    if (/^\/api\/bt\/results\/bt_t1$/.test(path)) return JSON.parse(JSON.stringify(RESULTS));
    if (path === "/api/strategies" && (!opts.method || opts.method === "GET")) {
      return [
        { name: "保守_低档", saved_at: "2026-10-20 09:00:00", size_bytes: 1200 },
        { name: "aggressive_v2", saved_at: "2026-10-21 18:30:00", size_bytes: 1180 },
      ];
    }
    throw new Error("unmocked: " + path + " " + (opts.method || "GET"));
  });
}

let appPinia = null;
const mk = () => {
  const pinia = createPinia();
  appPinia = pinia;
  return mount(BacktestTab, { attachTo: document.body, global: { plugins: [pinia] } });
};
const toastMsg = () => useToastStore(appPinia).msg;

beforeEach(async () => {
  setActivePinia(createPinia());
  const f = makeFakeSSE();
  sse = f.inst;
  __setUseSSEForTest(f.impl);
  vi.useFakeTimers();
  apiMock.mockReset();
  mockApi();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("BacktestTab · 表单与触发", () => {
  it("渲染：参数表单（start/end/标的 radio，pool 默认选中）+ 触发按钮", async () => {
    const w = mk();
    await flushPromises();
    expect(w.find('input[type="date"]').exists()).toBe(true);
    const radios = w.findAll('input[type="radio"]');
    expect(radios.length).toBe(2);
    expect(radios[0].element.value).toBe("pool");
    expect(radios[0].element.checked).toBe(true);
    expect(w.find("#btn-bt-run").exists()).toBe(true);
    w.unmount();
  });

  it("pool 模式触发 → POST /api/bt/runs {pool:true}（start/end 留空不带）+ 状态 running", async () => {
    const w = mk();
    await flushPromises();
    await w.find("#btn-bt-run").trigger("click");
    await flushPromises();
    const call = apiMock.mock.calls.find(([p, o]) => p === "/api/bt/runs" && o.method === "POST");
    expect(call).toBeTruthy();
    expect(JSON.parse(call[1].body)).toEqual({ pool: true });
    const store = useTaskStore(appPinia);
    expect(store.state).toBe("running");
    expect(sse.url).toBe("/api/bt/runs/bt_t1/events");
    w.unmount();
  });

  it("填 start/end + codes 模式 → POST 带 start/end/codes", async () => {
    const w = mk();
    await flushPromises();
    await w.findAll('input[type="date"]')[0].setValue("2016-01-04");
    await w.findAll('input[type="date"]')[1].setValue("2026-10-21");
    await w.findAll('input[type="radio"]')[1].setValue("codes"); // 切到指定标的
    await flushPromises();
    await w.find('input[type="text"][placeholder*="sh.601088"]').setValue("sh.601088, sz.000651");
    await w.find("#btn-bt-run").trigger("click");
    await flushPromises();
    const call = apiMock.mock.calls.find(([p, o]) => p === "/api/bt/runs" && o.method === "POST");
    expect(JSON.parse(call[1].body)).toEqual({
      start: "2016-01-04", end: "2026-10-21", codes: "sh.601088,sz.000651",
    });
    w.unmount();
  });

  it("codes 模式空 codes → 本地 toast、不发 POST", async () => {
    const w = mk();
    await flushPromises();
    await w.findAll('input[type="radio"]')[1].setValue("codes");
    await flushPromises();
    await w.find("#btn-bt-run").trigger("click");
    await flushPromises();
    expect(apiMock.mock.calls.some(([p, o]) => p === "/api/bt/runs" && o.method === "POST")).toBe(false);
    expect(toastMsg()).toContain("codes 为空");
    w.unmount();
  });

  it("409 → toast「已有回测任务…」+ 状态不变", async () => {
    apiMock.mockImplementation(async (path, opts = {}) => {
      const e = new Error("busy");
      e.status = 409;
      e.body = { detail: "已有回测任务在进行中: bt_old" };
      throw e;
    });
    const w = mk();
    await flushPromises();
    await w.find("#btn-bt-run").trigger("click");
    await flushPromises();
    expect(toastMsg()).toContain("已有回测任务在进行中");
    expect(useTaskStore(appPinia).state).toBe(null);
    w.unmount();
  });

  // ---- v7.0.2 策略选择（策略库快照 → --config）----
  it("策略下拉：默认当前策略 + 策略库条目（GET /api/strategies）", async () => {
    const w = mk();
    await flushPromises();
    expect(apiMock.mock.calls.some(([p]) => p === "/api/strategies")).toBe(true);
    const sel = w.find("select");
    expect(sel.exists()).toBe(true);
    const opts = sel.findAll("option");
    expect(opts.length).toBe(3); // 当前策略 + 2 条策略库
    expect(opts[0].element.value).toBe("current");
    expect(opts[0].text()).toContain("当前策略");
    expect(opts[1].text()).toContain("保守_低档");
    expect(opts[2].text()).toContain("aggressive_v2");
    w.unmount();
  });

  it("选择策略库条目触发 → POST /api/bt/runs body 带 strategy", async () => {
    const w = mk();
    await flushPromises();
    await w.find("select").setValue("保守_低档");
    await flushPromises();
    await w.find("#btn-bt-run").trigger("click");
    await flushPromises();
    const call = apiMock.mock.calls.find(([p, o]) => p === "/api/bt/runs" && o.method === "POST");
    expect(JSON.parse(call[1].body)).toEqual({ pool: true, strategy: "保守_低档" });
    w.unmount();
  });

  it("策略库条目跑完 → 结果 meta 行显示所选策略名", async () => {
    const w = mk();
    await flushPromises();
    await w.find("select").setValue("aggressive_v2");
    await flushPromises();
    await w.find("#btn-bt-run").trigger("click");
    await flushPromises();
    sse.message(JSON.stringify({ type: "done" }));
    for (let i = 0; i < 8; i++) await flushPromises();
    expect(w.text()).toContain("策略 aggressive_v2");
    w.unmount();
  });

  it("策略库拉取失败 → 下拉仅当前策略 + 空库提示", async () => {
    apiMock.mockImplementation(async (path, opts = {}) => {
      if (path === "/api/bt/runs" && opts.method === "POST") {
        return { task_id: "bt_t1", pid: 4242, status: "running", out_dir: "/tmp/bt_t1" };
      }
      if (path === "/api/strategies") throw new Error("boom");
      throw new Error("unmocked: " + path);
    });
    const w = mk();
    await flushPromises();
    const opts = w.find("select").findAll("option");
    expect(opts.length).toBe(1);
    expect(w.text()).toContain("策略库为空");
    w.unmount();
  });
});

describe("BacktestTab · 结果区（SSE done → /api/bt/results）", () => {
  it("done 事件 → 拉 results：per_stock 表 + buys 表 + summary + 净值曲线渲染", async () => {
    const w = mk();
    await flushPromises();
    await w.find("#btn-bt-run").trigger("click");
    await flushPromises();
    sse.message(JSON.stringify({ type: "log", text: "[PROGRESS] stage=bt done=1 total=2" }));
    sse.message(JSON.stringify({ type: "done" }));
    // 结果链路 = 多个微任务轮（fetch → results 赋值 → 图表 watch → nextTick → 动态 import）
    for (let i = 0; i < 8; i++) await flushPromises();

    // 结果区出现
    expect(w.text()).toContain("回测结果");
    expect(w.text()).toContain("中国神华"); // per_stock 行
    expect(w.text()).toContain("格力电器");
    // buys 表
    expect(w.text()).toContain("2018-03-01");
    // summary（MarkdownView 渲染）
    expect(w.text()).toContain("回测摘要");
    // 净值曲线 echarts 已 init + setOption
    const echarts = await import("echarts");
    expect(echarts.init).toHaveBeenCalled();
    const setOpt = echarts.init.mock.results[0].value.setOption;
    expect(setOpt).toHaveBeenCalled();
    const opt = setOpt.mock.calls[0][0];
    expect(opt.series.map((s) => s.name)).toEqual(["组合市值", "累计投入"]);
    // run_meta 行
    expect(w.text()).toContain("2 只");
    w.unmount();
  });

  it("SSE 期间 progress 事件 → 进度面板更新（TaskProgress 只读 taskStore）", async () => {
    const w = mk();
    await flushPromises();
    await w.find("#btn-bt-run").trigger("click");
    await flushPromises();
    sse.message(JSON.stringify({ type: "progress", stage: "bt", done: 2, total: 101 }));
    await flushPromises();
    expect(w.text()).toContain("2/101");
    w.unmount();
  });
});
