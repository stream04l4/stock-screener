// QueryTab 组件回归（v7.0 新增）：positions 表 + pool 表（漏斗文案）+ 行点击弹层（状态 + 历史）。
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const apiMock = vi.fn();
vi.mock("../../api/client.js", () => ({
  api: (...a) => apiMock(...a),
  isBackfillErr: (e) => !!(e && e.status === 409 && e.body && e.error === "lake_backfill_in_progress"),
}));
vi.mock("../../live/queryRegistry.js", async (orig) => ({ ...(await orig()) }));

const QueryTab = (await import("../QueryTab.vue")).default;
const { useTaskStore } = await import("../../stores/taskStore.js");

const POSITIONS = {
  as_of: "2026-10-21",
  n: 2,
  elapsed_s: 2.5,
  positions: [
    { ts_code: "sh.601088", name: "中国神华", in_pos: true, shares: 12345.67, avg_cost: 25.1,
      last_price: 33.9, market_value: 418518.21, cash_div: 51234.5, unreal_pct: 121.4,
      k_level: 0, entry_high: 158.27, low_since_high: 156.18,
      next_add_price: 134.53, next_rearm_price: 179.61, end_date: "2026-10-21" },
    { ts_code: "sz.000651", name: "格力电器", in_pos: true, shares: 8000.2, avg_cost: 41.5,
      last_price: 40.2, market_value: 321608.04, cash_div: 60000, unreal_pct: -3.1,
      k_level: 2, entry_high: 55.0, low_since_high: 38.9,
      next_add_price: 39.6, next_rearm_price: 44.74, end_date: "2026-10-21" },
  ],
};

const POOL = {
  funnel: { total: 5556, whitelist: 347, soe: 289, mv: 125, listing: 124, div: 101, final: 101 },
  n_members: 2,
  members: [
    { ts_code: "sh.601088", name: "中国神华", industry_csric2: "B06", soe_basis: "国有股",
      total_mv: 3000, ttm_yield_pct: 4.1, n_bars: 2600, list_date: "2007-01-01" },
    { ts_code: "sh.600900", name: "长江电力", industry_csric2: "D44", soe_basis: "名称:国资委",
      total_mv: 6800, ttm_yield_pct: 3.2, n_bars: 5500, list_date: "2003-11-18" },
  ],
};

function mockApi() {
  apiMock.mockImplementation(async (path) => {
    if (path === "/api/positions") return JSON.parse(JSON.stringify(POSITIONS));
    if (path === "/api/pool") return JSON.parse(JSON.stringify(POOL));
    throw new Error("unmocked: " + path);
  });
}

let appPinia = null;
const mk = () => {
  const pinia = createPinia();
  appPinia = pinia;
  return mount(QueryTab, { attachTo: document.body, global: { plugins: [pinia] } });
};

beforeEach(() => {
  setActivePinia(createPinia());
  apiMock.mockReset();
  mockApi();
});

describe("QueryTab · 两表渲染", () => {
  it("positions 表 + pool 表 + 漏斗文案", async () => {
    const w = mk();
    await flushPromises();
    expect(w.text()).toContain("当前持仓状态");
    expect(w.text()).toContain("中国神华");
    expect(w.text()).toContain("截至 2026-10-21");
    expect(w.text()).toContain("股票池");
    expect(w.text()).toContain("长江电力");
    // 漏斗文案（全市场 → … = 池 101 只）
    expect(w.text()).toContain("5556");
    expect(w.text()).toContain("池 101 只");
    w.unmount();
  });
});

describe("QueryTab · 行点击弹层（状态 + 历史）", () => {
  it("positions 行点击 → Modal 显示状态字段（含 next_add_price）", async () => {
    const w = mk();
    await flushPromises();
    const row = w.findAll("tbody tr").find((r) => r.text().includes("sh.601088"));
    expect(row).toBeTruthy();
    await row.trigger("click");
    await flushPromises();
    expect(w.text()).toContain("下次加仓触发价");
    expect(w.text()).toContain("134.53");
    expect(w.text()).toContain("再武装触发价");
    w.unmount();
  });

  it("有回测结果（taskStore.lastResults）→ 弹层显示该股逐笔买入历史", async () => {
    const w = mk(); // 先 mount（组件与 store 同一 pinia 实例）
    const store = useTaskStore(appPinia);
    store.lastResults = {
      taskId: "bt_t9",
      data: { buys: [
        { ts_code: "sh.601088", seq: 1, date: "2018-03-01", price: 20.5, amount: 100000 },
        { ts_code: "sz.000651", seq: 1, date: "2019-01-15", price: 42.1, amount: 100000 },
      ] },
    };
    await flushPromises();
    const row = w.findAll("tbody tr").find((r) => r.text().includes("sh.601088"));
    await row.trigger("click");
    await flushPromises();
    expect(w.text()).toContain("bt_t9");
    expect(w.text()).toContain("2018-03-01"); // 该股的历史买入（另一股 2019-01-15 不显示）
    expect(w.text()).not.toContain("2019-01-15");
    w.unmount();
  });

  it("无回测结果 → 弹层提示去回测页", async () => {
    const w = mk();
    await flushPromises();
    const row = w.findAll("tbody tr").find((r) => r.text().includes("sh.601088"));
    await row.trigger("click");
    await flushPromises();
    expect(w.text()).toContain("尚无回测结果");
    w.unmount();
  });

  it("pool 行点击（该股不在 positions）→ 弹层显示 pool 成员字段", async () => {
    const w = mk();
    await flushPromises();
    // 找 pool 表中 长江电力 行（positions 表无此股）
    const tables = w.findAll("table");
    expect(tables.length).toBe(2);
    const poolRows = tables[1].findAll("tbody tr");
    const row = poolRows.find((r) => r.text().includes("sh.600900"));
    expect(row).toBeTruthy();
    await row.trigger("click");
    await flushPromises();
    expect(w.text()).toContain("SOE依据");
    expect(w.text()).toContain("名称:国资委");
    w.unmount();
  });
});
