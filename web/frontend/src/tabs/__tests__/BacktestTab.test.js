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
      total_invested: 210510, final_assets: 660997.72, dividends: 51234.5,
      div_pool: 60000, price_pnl: 390487.72, total_pnl: 450487.72,
      pnl_pct: 2.14, min_unreal_pct: -0.1, first_buy_date: "2018-03-01", last_buy_date: "2024-02-05",
      last_status: "open", end_date: "2026-10-21" },
    { ts_code: "sz.000651", name: "格力电器", n_bars: 2500, n_rounds: 2, n_rearms: 1, n_buys: 21,
      total_invested: 326102, final_assets: 540050.34, dividends: 60000,
      div_pool: 30000, price_pnl: 183948.34, total_pnl: 213948.34,
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

// ---- v7.0.34 历史回测列表 fixture（实值取自 output/.../status.json 结构）----
const HISTORY_RUNS = [
  { task_id: "bt_20261110_140739_4a63", generated_at: "2026-11-10T14:07:39Z",
    window_start: "2010-08-27", window_end: "2026-11-10", n_dates: 3843,
    n_stocks: 1, n_triggered: 1, n_buys_total: 125,
    params: { first_drawdown_pct: 15.0, add_step_pct: 5.0, rearm_mode: "bounce" } },
  { task_id: "bt_20261022_100811_630d", generated_at: null,
    window_start: null, window_end: null, n_dates: null,
    n_stocks: null, n_triggered: null, n_buys_total: null, params: {} },
];

let sse = null;
function mockApi() {
  apiMock.mockImplementation(async (path, opts = {}) => {
    if (path === "/api/bt/runs" && opts.method === "POST") {
      return { task_id: "bt_t1", pid: 4242, status: "running", out_dir: "/tmp/bt_t1",
               meta: { start: null, end: null, codes: null, pool: true } };
    }
    if (path.startsWith("/api/bt/runs?")) return { runs: HISTORY_RUNS, total: HISTORY_RUNS.length };
    if (path === "/api/bt/results/bt_20261110_140739_4a63") {
      return JSON.parse(JSON.stringify({ ...RESULTS, task_id: "bt_20261110_140739_4a63" }));
    }
    if (/^\/api\/bt\/results\/bt_t1$/.test(path)) return JSON.parse(JSON.stringify(RESULTS));
    if (path.startsWith("/api/stocks/search?")) {   // v8.0.9 指定标的模糊搜索
      return { q: "mock", results: [
        { ts_code: "sh.601088", name: "中国神华", industry: "煤炭开采", delisted: false },
        { ts_code: "sz.300001", name: "退市特退", industry: "测试行业", delisted: true },
      ] };
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

  // ---- v7.0.29 策略库退役：无策略下拉，统一当前策略 strategy.yaml ----
  it("无策略下拉；POST body 不带 strategy 键；不请求 /api/strategies", async () => {
    const w = mk();
    await flushPromises();
    // v7.0.34 后页面有历史回测下拉——守卫范围限定参数表单（策略选择退役语义）
    expect(w.find(".bt-form select").exists()).toBe(false);
    await w.find("#btn-bt-run").trigger("click");
    await flushPromises();
    const call = apiMock.mock.calls.find(([p, o]) => p === "/api/bt/runs" && o.method === "POST");
    expect(call).toBeTruthy();
    expect("strategy" in JSON.parse(call[1].body)).toBe(false);
    expect(apiMock.mock.calls.some(([p]) => p.startsWith("/api/strategies"))).toBe(false);
    w.unmount();
  });

  it("跑完 → 结果 meta 行固定显示「策略 当前策略（strategy.yaml）」", async () => {
    const w = mk();
    await flushPromises();
    await w.find("#btn-bt-run").trigger("click");
    await flushPromises();
    sse.message(JSON.stringify({ type: "done" }));
    for (let i = 0; i < 8; i++) await flushPromises();
    expect(w.text()).toContain("策略 当前策略（strategy.yaml）");
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

  it("v7.0.33 展示收敛：per_stock 全精度 float → 2 位小数；ISO 日期截 T 后", async () => {
    // 真实产物精度（output/.../per_stock.csv 实值）：引擎写全精度 float +
    // ISO 时间戳，展示层须截断（排序/搜索仍走原值，此处只验展示）。
    apiMock.mockImplementation(async (path, opts = {}) => {
      if (path === "/api/bt/runs" && opts.method === "POST") {
        return { task_id: "bt_t1", pid: 4242, status: "running", out_dir: "/tmp/bt_t1",
                 meta: { start: null, end: null, codes: null, pool: true } };
      }
      if (/^\/api\/bt\/results\/bt_t1$/.test(path)) return JSON.parse(JSON.stringify({
        ...RESULTS,
        per_stock: [{
          ts_code: "sz.302132", name: "中航成飞", n_bars: 3843, n_rounds: 1,
          n_rearms: 118, n_buys: 125, total_invested: 887013.0240999996,
          final_assets: 4207827.437903744, dividends: 79359.52794443406,
          div_pool: 40000, price_pnl: 3280814.4138037446,
          total_pnl: 3320814.4138037446, pnl_pct: 3.743816971766769,
          min_unreal_pct: -0.3369759158631754,
          first_buy_date: "2010-09-29T00:00:00.000000000",
          last_buy_date: "2026-07-08T00:00:00.000000000",
          last_status: "open", end_date: "2026-11-10T00:00:00.000000000",
        }],
        buys: [{ ts_code: "sz.302132", seq: 1, date: "2010-09-29T00:00:00.000000000",
                 price: 32.6, amount: 3260.978 }],
      }));
      throw new Error("unmocked: " + path);
    });
    const w = mk();
    await flushPromises();
    await w.find("#btn-bt-run").trigger("click");
    await flushPromises();
    sse.message(JSON.stringify({ type: "done" }));
    for (let i = 0; i < 8; i++) await flushPromises();
    const t = w.text();
    // 金额 → 2 位小数
    expect(t).toContain("887013.02");
    expect(t).toContain("4207827.44");
    // v8.0.10：pnl_pct/min_unreal_pct 是比率 → 百分比显示（此前 fmt2 裸显示
    // 3.74 挂在「盈亏%」列名下，实为 +374.4%）
    expect(t).toContain("374.4%");
    expect(t).toContain("-33.7%");
    // 全精度长小数不得出现
    expect(t).not.toContain("887013.0240999996");
    expect(t).not.toContain("3.743816971766769");
    expect(t).not.toContain("3260.978");
    expect(t).toContain("3260.98");
    // 日期截 T 后
    expect(t).toContain("2010-09-29");
    expect(t).not.toContain("T00:00:00");
    // 整数列不受影响
    expect(t).toContain("3843");
    w.unmount();
  });
});

describe("BacktestTab · 分红占比 vs 上涨占比（v8.0.10，Joel：低波红利策略导向）", () => {
  it("有 div_pool/price_pnl 的行：显示分红余额/价差盈亏金额 + 两占比列（与 summary.md 拆分开口径一致）", async () => {
    apiMock.mockImplementation(async (path, opts = {}) => {
      if (path === "/api/bt/runs" && opts.method === "POST") {
        return { task_id: "bt_t1", pid: 4242, status: "running", out_dir: "/tmp/bt_t1",
                 meta: { start: null, end: null, codes: null, pool: true } };
      }
      if (/^\/api\/bt\/results\/bt_t1$/.test(path)) return JSON.parse(JSON.stringify(RESULTS));
      throw new Error("unmocked: " + path);
    });
    const w = mk();
    await flushPromises();
    await w.find("#btn-bt-run").trigger("click");
    await flushPromises();
    sse.message(JSON.stringify({ type: "done" }));
    for (let i = 0; i < 8; i++) await flushPromises();
    const t = w.text();
    // 新列头
    expect(t).toContain("分红余额");
    expect(t).toContain("价差盈亏");
    expect(t).toContain("分红占比");
    expect(t).toContain("上涨占比");
    // 中国神华：60000/450487.72 → 13.3%；390487.72/450487.72 → 86.7%
    expect(t).toContain("60000.00");
    expect(t).toContain("390487.72");
    expect(t).toContain("13.3%");
    expect(t).toContain("86.7%");
    // 格力电器：30000/213948.34 → 14.0%；183948.34/213948.34 → 86.0%
    expect(t).toContain("14.0%");
    expect(t).toContain("86.0%");
    w.unmount();
  });

  it("旧产物（无 div_pool 列）与总盈亏≤0 → 占比 null（表内显示 —），不崩溃", async () => {
    const legacyRow = {
      ts_code: "sh.600900", name: "长江电力", n_bars: 2000, n_rounds: 1,
      n_rearms: 1, n_buys: 4, total_invested: 150000, final_assets: 180000,
      dividends: 12000, total_pnl: 30000, pnl_pct: 0.2, min_unreal_pct: -0.08,
      first_buy_date: "2015-01-05", last_buy_date: "2020-01-06",
      last_status: "open", end_date: "2026-11-10",
    };  // 旧 per_stock.csv：无 div_pool / price_pnl 键
    const lossRow = {
      ts_code: "sh.601006", name: "大华股份", n_bars: 2000, n_rounds: 1,
      n_rearms: 1, n_buys: 3, total_invested: 100000, final_assets: 92000,
      dividends: 2000, div_pool: 2000, price_pnl: -8000, total_pnl: -8000,
      pnl_pct: -0.08, min_unreal_pct: -0.15, first_buy_date: "2020-01-01",
      last_buy_date: "2022-01-01", last_status: "open", end_date: "2026-11-10",
    };  // 总盈亏为负 → 不拆占比
    apiMock.mockImplementation(async (path, opts = {}) => {
      if (path === "/api/bt/runs" && opts.method === "POST") {
        return { task_id: "bt_t1", pid: 1, status: "running", out_dir: "/tmp/bt_t1",
                 meta: { start: null, end: null, codes: null, pool: true } };
      }
      if (/^\/api\/bt\/results\/bt_t1$/.test(path)) return JSON.parse(JSON.stringify({
        ...RESULTS, per_stock: [legacyRow, lossRow],
      }));
      throw new Error("unmocked: " + path);
    });
    const w = mk();
    await flushPromises();
    await w.find("#btn-bt-run").trigger("click");
    await flushPromises();
    sse.message(JSON.stringify({ type: "done" }));
    for (let i = 0; i < 8; i++) await flushPromises();
    const rows = w.vm.perStockRows;
    expect(rows).toHaveLength(2);
    expect(rows[0].div_share).toBeNull();   // 旧产物无 div_pool
    expect(rows[0].price_share).toBeNull();
    expect(rows[1].div_share).toBeNull();   // 总盈亏≤0 不拆
    expect(rows[1].price_share).toBeNull();
    const t = w.text();
    expect(t).toContain("长江电力");
    expect(t).toContain("大华股份");
    expect(t).toContain("—");
    w.unmount();
  });
});

describe("BacktestTab · 买入时间表折叠（v7.0.38）", () => {
  it("长 buys（>30 行）默认折叠：表体隐藏 + 已折叠提示；点标题展开", async () => {
    const longBuys = Array.from({ length: 40 }, (_, i) => ({
      ts_code: "sh.600737", seq: i + 1, date: `200${i % 10}-0${(i % 9) + 1}-01`,
      price: 10 + i * 0.1, amount: 1000 + i,
    }));
    apiMock.mockImplementation(async (path, opts = {}) => {
      if (path === "/api/bt/runs" && opts.method === "POST") {
        return { task_id: "bt_t1", pid: 1, status: "running", out_dir: "/tmp/bt_t1",
                 meta: { start: null, end: null, codes: null, pool: true } };
      }
      if (/^\/api\/bt\/results\/bt_t1$/.test(path)) {
        return JSON.parse(JSON.stringify({ ...RESULTS, buys: longBuys }));
      }
      throw new Error("unmocked: " + path);
    });
    const w = mk();
    await flushPromises();
    await w.find("#btn-bt-run").trigger("click");
    await flushPromises();
    sse.message(JSON.stringify({ type: "done" }));
    for (let i = 0; i < 8; i++) await flushPromises();

    // 标题含行数；默认折叠（v-show → inline display:none + 提示行）
    expect(w.text()).toContain("40 行");
    const tbl = w.find(".bt-buys .simple-table");
    expect(tbl.exists()).toBe(true);
    expect(tbl.element.style.display).toBe("none");
    expect(w.text()).toContain("已折叠");
    // 点标题 → 展开（v-show 移除 inline 样式）
    await w.find("#btn-bt-buys-toggle").trigger("click");
    expect(w.find(".bt-buys .simple-table").element.style.display).toBe("");
    expect(w.text()).not.toContain("已折叠");
    w.unmount();
  });

  it("短 buys（≤30 行）默认展开；点标题可折叠再展开", async () => {
    const w = mk();
    await flushPromises();
    await w.find("#btn-bt-run").trigger("click");
    await flushPromises();
    sse.message(JSON.stringify({ type: "done" }));
    for (let i = 0; i < 8; i++) await flushPromises();
    // 默认 RESULTS buys=3 行 → 展开
    expect(w.find(".bt-buys .simple-table").element.style.display).toBe("");
    expect(w.text()).toContain("3 行");
    await w.find("#btn-bt-buys-toggle").trigger("click");
    expect(w.find(".bt-buys .simple-table").element.style.display).toBe("none");
    expect(w.text()).toContain("已折叠");
    await w.find("#btn-bt-buys-toggle").trigger("click");
    expect(w.find(".bt-buys .simple-table").element.style.display).toBe("");
    w.unmount();
  });
});

describe("BacktestTab · 历史回测结果（v7.0.34）", () => {
  it("onMounted 拉 /api/bt/runs 列表 → 下拉 2 项（完整 + 运行中/不完整）", async () => {
    const w = mk();
    await flushPromises();
    const sel = w.find("#bt-history-select");
    expect(sel.exists()).toBe(true);
    const opts = w.findAll("#bt-history-select option");
    expect(opts.length).toBe(3); // 占位 + 2 历史
    expect(w.text()).toContain("2026-11-10 14:07 · 1 股 · 2010-08-27~2026-11-10");
    expect(w.text()).toContain("运行中/不完整");
    w.unmount();
  });

  it("选中历史 → GET /api/bt/results/{id} → 结果区渲染 + 【历史产物】标记", async () => {
    const w = mk();
    await flushPromises();
    await w.find("#bt-history-select").setValue("bt_20261110_140739_4a63");
    for (let i = 0; i < 8; i++) await flushPromises();
    expect(apiMock.mock.calls.some(([p]) => p === "/api/bt/results/bt_20261110_140739_4a63"))
      .toBe(true);
    const t = w.text();
    expect(t).toContain("回测结果");
    expect(t).toContain("【历史产物】");
    expect(t).toContain("中国神华"); // 历史结果的 per_stock 行
    expect(t).toContain("产物 bt_20261110_140739_4a63");
    // 下拉复位（可再次选择）
    expect(w.find("#bt-history-select").element.value).toBe("");
    w.unmount();
  });
});

// ---------------------------------------------------------------------------
// 指定标的模糊搜索（v8.0.9）：末段候选 / 选中续填 / 多段只搜末段 / 短码不请求
// ---------------------------------------------------------------------------
describe("BacktestTab · 指定标的模糊搜索（v8.0.9）", () => {
  async function toCodesMode(w) {
    await w.findAll('input[type="radio"]')[1].setValue("codes");
    await flushPromises();
  }
  const codesInput = (w) => w.find(".bt-codes-wrap input");

  it("末段中文输入 → 防抖后请求 include_delisted=1 → 候选渲染（含退市标记）", async () => {
    const w = mk();
    await flushPromises();
    await toCodesMode(w);
    await codesInput(w).setValue("神华");
    vi.advanceTimersByTime(300);
    await flushPromises();
    expect(apiMock.mock.calls.some(
      ([p]) => p === "/api/stocks/search?q=" + encodeURIComponent("神华") + "&include_delisted=1"
    )).toBe(true);
    const items = w.findAll(".bt-codes-sug li");
    expect(items.length).toBe(2);
    expect(items[0].text()).toContain("sh.601088");
    expect(items[0].text()).toContain("中国神华");
    expect(items[1].text()).toContain("（退市）");
    w.unmount();
  });

  it("选中候选 → 末段替换为 ts_code 并追加逗号（续填下一只）", async () => {
    const w = mk();
    await flushPromises();
    await toCodesMode(w);
    await codesInput(w).setValue("神华");
    vi.advanceTimersByTime(300);
    await flushPromises();
    await w.findAll(".bt-codes-sug li")[0].trigger("mousedown");
    await flushPromises();
    expect(codesInput(w).element.value).toBe("sh.601088,");
    expect(w.find(".bt-codes-sug").exists()).toBe(false);
    w.unmount();
  });

  it("多段输入只搜末段：'sz.000651,神火' → q=神火；选中 → 前段保留", async () => {
    const w = mk();
    await flushPromises();
    await toCodesMode(w);
    await codesInput(w).setValue("sz.000651,神火");
    vi.advanceTimersByTime(300);
    await flushPromises();
    expect(apiMock.mock.calls.some(
      ([p]) => p === "/api/stocks/search?q=" + encodeURIComponent("神火") + "&include_delisted=1"
    )).toBe(true);
    await w.findAll(".bt-codes-sug li")[0].trigger("mousedown");
    await flushPromises();
    expect(codesInput(w).element.value).toBe("sz.000651,sh.601088,");
    w.unmount();
  });

  it("纯数字 <3 位 / 逗号后空段 → 不发请求（服务端 400 同规则前置）", async () => {
    const w = mk();
    await flushPromises();
    await toCodesMode(w);
    await codesInput(w).setValue("60");
    vi.advanceTimersByTime(300);
    await flushPromises();
    await codesInput(w).setValue("sz.000651,");
    vi.advanceTimersByTime(300);
    await flushPromises();
    expect(apiMock.mock.calls.some(([p]) => p.startsWith("/api/stocks/search?"))).toBe(false);
    w.unmount();
  });
});
