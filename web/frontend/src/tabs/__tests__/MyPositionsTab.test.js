// MyPositionsTab 组件回归（v7.0.1，原 QueryTab.test.js 重命名重写）：
// 我的持仓 = 用户账本（/api/my_positions GET）+ 录入条/弹层编辑（PUT）+ 策略参考列 + 买入历史。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const apiMock = vi.fn();
vi.mock("../../api/client.js", () => ({
  api: (...a) => apiMock(...a),
  isBackfillErr: (e) => !!(e && e.status === 409 && e.body && e.error === "lake_backfill_in_progress"),
}));
vi.mock("../../live/queryRegistry.js", async (orig) => ({ ...(await orig()) }));

const MyPositionsTab = (await import("../MyPositionsTab.vue")).default;
const { useTaskStore } = await import("../../stores/taskStore.js");
const { invalidate, __reset } = await import("../../live/queryRegistry.js");

const MY = {
  as_of: "2026-10-21",
  n: 1,
  signals: [],
  strategy: "current",
  positions: [
    { ts_code: "sh.601088", name: "中国神华", shares: 1200, cost_price: 25.0,
      last_price: 33.9, market_value: 40680, pnl: 10680, pnl_pct: 36.0,
      updated_at: "2026-10-22T01:00:00Z",
      since_date: "2026-10-20",
      buys: [{ date: "2026-10-20", shares: 1200, price: 25.0 }],
      in_pos: true, strat_shares: 12345.67, k_level: 0,
      next_add_price: 115.79, next_rearm_price: 179.61,
      next_add_price_raw: 42.07, next_rearm_price_raw: 56.34, end_date: "2026-10-21" },
  ],
  totals: { market_value: 40680, cost: 30000, pnl: 10680, pnl_pct: 35.6 },
};

const MY_SIGNAL = {
  ...JSON.parse(JSON.stringify(MY)),
  price_source: "spot",
  spot_time: "20261022150500",
  signals: [
    { ts_code: "sh.601088", name: "中国神华", price: 40.0, next_add_price: 42.07,
      k_level: 0, next_level: 1, suggested_lots: 1, action: "add" },
  ],
};

const MY_BUY = {
  ...JSON.parse(JSON.stringify(MY)),
  price_source: "spot",
  spot_time: "20261022150500",
  n: 2,
  positions: [
    ...JSON.parse(JSON.stringify(MY.positions)),
    { ts_code: "sh.600941", name: "中国移动", shares: 200, cost_price: 95.0,
      last_price: 96.59, market_value: 19318, pnl: 318, pnl_pct: 1.67,
      updated_at: "2026-10-22T01:00:00Z",
      in_pos: false, strat_shares: null, k_level: 0,
      next_add_price: 72.36, next_rearm_price: null,
      next_add_price_raw: 72.36, next_rearm_price_raw: null, end_date: "2026-10-21" },
  ],
  signals: [
    { ts_code: "sh.600941", name: "中国移动", price: 70.0, next_add_price: 72.36,
      k_level: 0, next_level: 1, suggested_lots: 1, action: "buy" },
  ],
  totals: { market_value: 60000, cost: 50000, pnl: 10000, pnl_pct: 20.0 },
};

const MY_EMPTY = { as_of: null, n: 0, positions: [],
  totals: { market_value: 0, cost: 0, pnl: 0, pnl_pct: null } };

// v7.0.19 策略信号中心 fixture（v7.0.23 口径更新）：
// active 5（持仓加仓 + 非持仓首仓时机过 + 非持仓模拟已建仓时机过【大秦：
// 非持仓恒走首仓线，价格态 active + 时机✓ → 当前信号·买入首仓】+ 非持仓
// 时机未过【江苏银行：不入当前信号段，v7.0.23 时机门】+ 非持仓模拟已建仓
// 时机未过【紫金矿业：不入段】）/ 1 touched（持仓盘中触碰已触回）/
// 3 即将进入（v7.0.22：持仓 1 + 非持仓时机过 1 + 非持仓时机未过 1【不入段】）/
// 4 历史（含微信推送标记 + 1 条触碰事件）
const SIG = {
  as_of: "2026-11-06", strategy: "current", lake_locked: false, approach_pct: 5,
  spot_preselect_pct: 10, spot_fetched: 9,
  price_source: "spot", spot_time: "20261107093000", n_stocks: 10,
  history_window_days: 90,
  stocks: [
    { ts_code: "sh.601088", name: "中国神华", held: true, in_pos: true, k_level: 1,
      price: 40.0, next_add_price_raw: 42.07, status: "active", touched: false,
      day_low: 39.8, distance_pct: -4.9,
      action: "add", suggested_lots: 2, n_history: 2, end_date: "2026-11-06",
      timing_ok: null, timing_reasons: null, bt_pnl_pct: 88.5, bt_n_buys: 6 },
    { ts_code: "sh.600941", name: "中国移动", held: false, in_pos: false, k_level: 0,
      price: 70.0, next_add_price_raw: 72.36, status: "active", touched: false,
      day_low: 69.5, distance_pct: -3.26,
      action: "buy", suggested_lots: 1, n_history: 0, end_date: "2026-11-06",
      timing_ok: true, timing_reasons: "✓ 5年分位 21.9%（需≤40%） · ✓ 股息率 4.59%（需≥4.5%）",
      bt_pnl_pct: 45.2, bt_n_buys: 4 },
    // v7.0.23 反例（实例化）：非持仓 + 时机 ✗✗ → 价格态 active 也不入
    // 「当前信号」段（首仓双条件缺时机门；v7.0.22「即将进入」同门）
    { ts_code: "sh.600988", name: "江苏银行", held: false, in_pos: false, k_level: 0,
      price: 8.0, next_add_price_raw: 8.5, status: "active", touched: false,
      day_low: 7.9, distance_pct: -5.9,
      action: null, suggested_lots: null, n_history: 0, end_date: "2026-11-06",
      timing_ok: false, timing_reasons: "✗ 5年分位 62.3%（需≤40%） · ✗ 股息率 2.10%（需≥4.5%）",
      bt_pnl_pct: null, bt_n_buys: null },
    { ts_code: "sh.601985", name: "中国核电", held: true, in_pos: true, k_level: 1,
      price: 10.2, next_add_price_raw: 10.0, status: "touched", touched: true,
      day_low: 9.96, distance_pct: 2.0,
      action: "add", suggested_lots: 2, n_history: 0, end_date: "2026-11-06",
      timing_ok: null, timing_reasons: null, bt_pnl_pct: 12.3, bt_n_buys: 5 },
    // 非持仓 + 模拟已建仓（in_pos=true）+ 时机 ✗✗：不入当前信号段
    //（v7.0.23 时机门；触碰记录走历史）
    { ts_code: "sh.601899", name: "紫金矿业", held: false, in_pos: true, k_level: 2,
      price: 18.0, next_add_price_raw: 18.5, status: "active", touched: false,
      day_low: 17.9, distance_pct: -2.7,
      action: null, suggested_lots: null, n_history: 0, end_date: "2026-11-06",
      timing_ok: false, timing_reasons: "✗ 5年分位 70.0%（需≤40%） · ✗ 股息率 1.50%（需≥4.5%）",
      bt_pnl_pct: null, bt_n_buys: null },
    // v7.0.23 正例（大秦铁路实例）：非持仓 + 模拟已建仓（in_pos=true k=3）
    // → 触发线恒为**首仓线** 5.2811（不再 next 档 4.3491）；价 4.88 ≤ 5.2811
    // 且时机✓（19.1%/4.58%）→ 当前信号·买入·首仓（10 万）
    { ts_code: "sh.601006", name: "大秦铁路", held: false, in_pos: true, k_level: 3,
      price: 4.88, next_add_price_raw: 5.2811, status: "active", touched: false,
      day_low: 4.75, distance_pct: -7.59,
      action: "buy", suggested_lots: 1, n_history: 0, end_date: "2026-11-06",
      timing_ok: true, timing_reasons: "✓ 5年分位 19.1%（需≤40%） · ✓ 股息率 4.58%（需≥4.5%）",
      bt_pnl_pct: 120.4, bt_n_buys: 7 },
    { ts_code: "sh.600900", name: "长江电力", held: true, in_pos: true, k_level: 0,
      price: 28.56, next_add_price_raw: 28.0, status: "approaching", touched: false,
      day_low: 27.9, distance_pct: 2.0,
      action: "add", suggested_lots: 1, n_history: 1, end_date: "2026-11-06",
      timing_ok: null, timing_pctile: null, timing_reasons: null,
      bt_pnl_pct: 30.1, bt_n_buys: 3 },
    // v7.0.22 时机门入「即将进入」段：非持仓 + 时机达标 + 价格临区 → 展示
    //（临区跌破即产生真实买入信号）
    { ts_code: "sh.601328", name: "交通银行", held: false, in_pos: false, k_level: 0,
      price: 7.55, next_add_price_raw: 7.40, status: "approaching", touched: false,
      day_low: 7.5, distance_pct: 2.0,
      action: "buy", suggested_lots: 1, n_history: 0, end_date: "2026-11-06",
      timing_ok: true, timing_pctile: 33.5,
      timing_reasons: "✓ 5年分位 33.5%（需≤40%） · ✓ 股息率 5.02%（需≥4.5%）",
      bt_pnl_pct: null, bt_n_buys: null },
    // v7.0.22 反例（中国海油实例）：非持仓（模拟已建仓 in_pos=true）+ 价格临区
    // 但时机 ✗✗（分位 85.7% > 50% 放宽带）→ 不入「即将进入」段
    { ts_code: "sh.600938", name: "中国海油", held: false, in_pos: true, k_level: 0,
      price: 31.07, next_add_price_raw: 30.3159, status: "approaching", touched: false,
      day_low: 30.8, distance_pct: 2.49,
      action: null, suggested_lots: null, n_history: 10, end_date: "2026-11-06",
      timing_ok: false, timing_pctile: 85.7,
      timing_reasons: "✗ 5年分位 85.7%（需≤40%） · ✗ 股息率 4.17%（需≥4.5%）",
      bt_pnl_pct: 87.28, bt_n_buys: 10 },
    // v7.0.31 C 案（山煤国际实例，Joel 定案）：非持仓 + 价格临区 + 分位 45%
    //（>40% 时机未过、≤50% 放宽带内）→ 展示并标「未过」——原口径下翻转前
    // 完全不可见
    { ts_code: "sh.600546", name: "山煤国际", held: false, in_pos: false, k_level: 0,
      price: 11.01, next_add_price_raw: 11.2478, status: "approaching", touched: false,
      day_low: 10.96, distance_pct: 2.14,
      action: null, suggested_lots: null, n_history: 0, end_date: "2026-11-06",
      timing_ok: false, timing_pctile: 45.0,
      timing_reasons: "✗ 5年分位 45.0%（需≤40%）",
      bt_pnl_pct: null, bt_n_buys: null },
  ],
  history: [
    { ts_code: "sh.601088", name: "中国神华", held: true, date: "2026-11-02",
      price: 43.1, amount: 43100, level: 1, first_of_round: false, pushed: true, lots: 2 },
    { ts_code: "sh.601985", name: "中国核电", held: true, date: "2026-11-05",
      price: 10.0, amount: 20000, level: 1, first_of_round: false, pushed: false, lots: 2,
      source: "touch" },
    { ts_code: "sh.601088", name: "中国神华", held: true, date: "2026-10-20",
      price: 45.0, amount: 45000, level: 0, first_of_round: true, pushed: false, lots: 1 },
    { ts_code: "sh.600900", name: "长江电力", held: true, date: "2026-10-11",
      price: 29.0, amount: 29000, level: 0, first_of_round: true, pushed: false, lots: 1 },
  ],
  totals: { n_stocks: 10, n_active: 5, n_touched: 1, n_approaching: 4, n_idle: 0,
            n_no_data: 0, n_active_held: 1, n_active_unheld: 4, n_history: 4 },
};
const SIG_EMPTY = { as_of: null, strategy: "current", lake_locked: false, approach_pct: 5,
  spot_preselect_pct: 10, spot_fetched: 0,
  price_source: "lake", spot_time: null, n_stocks: 0, stocks: [], history: [],
  totals: { n_stocks: 0, n_active: 0, n_touched: 0, n_approaching: 0, n_idle: 0,
            n_no_data: 0, n_active_held: 0, n_active_unheld: 0, n_history: 0 } };

function mockApi(my, trades = [], signals = SIG_EMPTY) {
  apiMock.mockImplementation(async (path, opts = {}) => {
    if (path.startsWith("/api/my_positions") && (!opts || !opts.method || opts.method === "GET")) {
      return JSON.parse(JSON.stringify(my));
    }
    if (path.startsWith("/api/my_positions") && opts && opts.method === "PUT") {
      const body = JSON.parse(opts.body);
      return { ok: true, ts_code: body.ts_code, n: 1 };
    }
    // v7.0.17 交易登记：按传入账本 my 模拟服务端记账（买加权 / 卖减股 / 清仓移除）
    if (path === "/api/my_trades" && opts && opts.method === "POST") {
      const body = JSON.parse(opts.body);
      const pos0 = (my.positions || []).find((p) => p.ts_code === body.ts_code) || {};
      const oldShares = pos0.shares || 0;
      const oldCost = pos0.cost_price || 0;
      const ns = body.shares;
      let newShares, newCost, removed = false;
      if (ns > 0) {
        newShares = oldShares + ns;
        newCost = Math.round(((oldShares * oldCost + ns * body.price) / newShares) * 1000) / 1000;
      } else {
        newShares = Math.max(oldShares + ns, 0);
        newCost = oldCost;
        removed = oldShares + ns <= 0;
      }
      const trade = {
        id: "t" + (trades.length + 1) + "00",
        ts_code: body.ts_code, name: body.name || "",
        direction: ns > 0 ? "buy" : "sell", shares: Math.abs(ns), price: body.price,
        amount: Math.abs(ns) * body.price, date: "2026-10-26",
        realized_pnl: ns < 0 ? Math.round((body.price - oldCost) * Math.abs(ns) * 100) / 100 : null,
        created_at: "2026-10-26T01:00:00Z",
      };
      trades.unshift(trade);
      return { ok: true, trade, position: { ts_code: body.ts_code, shares: newShares,
        cost_price: removed ? null : newCost, removed } };
    }
    if (path.startsWith("/api/my_trades?") ) {
      const code = decodeURIComponent((path.split("ts_code=")[1] || "").trim());
      const rows = trades.filter((t) => t.ts_code === code);
      return { n: rows.length, trades: JSON.parse(JSON.stringify(rows)) };
    }
    if (path.startsWith("/api/my_trades/") && opts && opts.method === "DELETE") {
      const id = decodeURIComponent(path.split("/").pop());
      const i = trades.findIndex((t) => t.id === id);
      if (i < 0) { const e = new Error("record not found"); e.status = 404; throw e; }
      trades.splice(i, 1);
      return { ok: true, n: trades.length };
    }
    if (path.startsWith("/api/strategy_signals")) {
      return JSON.parse(JSON.stringify(signals));
    }
    if (path.startsWith("/api/stocks/search")) {
      const q = decodeURIComponent((path.split("q=")[1] || "").trim());
      const SUG = {
        zgsh: [{ ts_code: "sh.601088", name: "中国神华", industry: "B06煤炭开采和洗选业" }],
        "600900": [{ ts_code: "sh.600900", name: "长江电力", industry: "D44电力、热力生产和供应业" }],
        电力: [{ ts_code: "sh.600900", name: "长江电力", industry: "D44电力、热力生产和供应业" }],
      };
      return { q, results: SUG[q] || [] };
    }
    throw new Error("unmocked: " + path);
  });
}

let appPinia = null;
// 测试卫生：记录所有挂载的 wrapper；afterEach 统一 unmount。
// 若某用例断言失败（提前 throw 跳过行内 unmount），泄漏的订阅会 refcount>0，
// 使下一个 beforeEach 的 invalidate 触发"对已重置 mock 的刷新"→ registry 缓存
// 被 undefined 污染且 TTL 新鲜 → 后续挂载直接命中空数据。统一 unmount 杜绝污染。
const mounted = new Set();
const mk = () => {
  const pinia = createPinia();
  appPinia = pinia;
  const w = mount(MyPositionsTab, { attachTo: document.body, global: { plugins: [pinia] } });
  mounted.add(w);
  return w;
};

beforeEach(() => {
  setActivePinia(createPinia());
  apiMock.mockReset();
  // registry 是模块级单例（会话缓存语义）→ 测试间必须清缓存，否则首个测试的
  // 数据（或空数据）会被后续测试的挂载直接命中。v7.0.30 时段门会在
  // 「非时段 + 有缓存」时直接返回旧缓存不发请求，故必须 __reset 清数据（仅
  // invalidate 只打过期标记不清数据，会让下一个测试拿到上一个测试的旧值）。
  __reset();
  invalidate("my_positions");
  invalidate("strategy_signals");
  localStorage.clear(); // 策略选择持久化 → 测试间隔离
  vi.stubGlobal("confirm", vi.fn(() => true));
});

afterEach(() => {
  for (const w of [...mounted]) { try { w.unmount(); } catch { /* 已 unmount */ } }
  mounted.clear();
});

const puts = () => apiMock.mock.calls.filter((c) => c[1] && c[1].method === "PUT");

describe("MyPositionsTab · 渲染", () => {
  it("空账本：标题 + 空态提示，不发 PUT", async () => {
    mockApi(MY_EMPTY);
    const w = mk();
    await flushPromises();
    expect(w.text()).toContain("我的持仓");
    expect(w.text()).toContain("尚未录入持仓");
    expect(puts()).toHaveLength(0);
    w.unmount();
  });

  it("有账本：表格行 + 汇总条（万为单位）+ 数据截至 + 价源标注", async () => {
    mockApi(MY);
    const w = mk();
    await flushPromises();
    expect(w.text()).toContain("中国神华");
    expect(w.text()).toContain("sh.601088");
    expect(w.text()).toContain("¥4.1万"); // 40680 市值
    expect(w.text()).toContain("数据截至 2026-10-21");
    expect(w.text()).toContain("最新收盘（T+1）"); // 无 price_source → 湖价口径（v7.0.40）
    w.unmount();
  });

  it("lake_locked（灌数持锁降级）→ 琥珀提示条 + 账本仍列出", async () => {
    const locked = {
      ...JSON.parse(JSON.stringify(MY)),
      lake_locked: true,
      as_of: null,
      positions: [{ ...MY.positions[0], last_price: null, market_value: null, pnl: null, pnl_pct: null,
        in_pos: null, strat_shares: null, k_level: null, next_add_price: null, next_rearm_price: null, end_date: null }],
      totals: { market_value: null, cost: 30000, pnl: null, pnl_pct: null },
    };
    mockApi(locked);
    const w = mk();
    await flushPromises();
    expect(w.text()).toContain("灌数");
    expect(w.text()).toContain("中国神华"); // 账本仍可见
    expect(w.text()).not.toContain("¥4.1万"); // 无价格 → 无市值汇总
    w.unmount();
  });
});

describe("MyPositionsTab · 录入条", () => {
  it("非法代码不发 PUT；合法代码 PUT 正确 body（大写小写化）", async () => {
    mockApi(MY_EMPTY);
    const w = mk();
    await flushPromises();
    const inputs = w.findAll(".add-bar input");
    expect(inputs).toHaveLength(4); // 代码/股数/成本价/建仓日（v8.2.0）
    await inputs[0].setValue("601088"); // 缺 sh. 前缀
    await inputs[1].setValue("100");
    await inputs[2].setValue("30");
    await w.find(".add-bar button").trigger("click");
    await flushPromises();
    expect(w.text()).toContain("代码应为");
    expect(puts()).toHaveLength(0);

    await inputs[0].setValue("SH.601088"); // 大写 → 组件内小写化
    await w.find(".add-bar button").trigger("click");
    await flushPromises();
    expect(puts()).toHaveLength(1);
    expect(JSON.parse(puts()[0][1].body)).toEqual(
      { ts_code: "sh.601088", shares: 100, cost_price: 30, since_date: "" });
    expect(w.text()).toContain("已保存 sh.601088");
    w.unmount();
  });
});

describe("MyPositionsTab · 策略应用 + 加仓信号（v7.0.4）", () => {
  it("有信号 → 红色提示条（名称/触发价/档位/建议手数）+ 表格信号列 🔔", async () => {
    mockApi(MY_SIGNAL);
    const w = mk();
    await flushPromises();
    expect(w.find(".signal-banner").exists()).toBe(true);
    expect(w.text()).toContain("信号提示");
    expect(w.text()).toContain("1 只达到触发价位");
    expect(w.text()).toContain("加仓"); // 信号 tag
    expect(w.text()).toContain("中国神华");
    expect(w.text()).toContain("现价 40"); // 现价（实时价，数字渲染为 40）
    expect(w.text()).toContain("42.07"); // 触发价（原始口径）
    expect(w.text()).toContain("实时行情"); // 价源标注
    // v7.0.40：持仓卡价源标注（实时行情 + 时间戳）
    expect(w.text()).toContain("价源：实时行情 20261022150500");
    expect(w.text()).toContain("第 0 档 → 第 1 档");
    expect(w.text()).toContain("1 手"); // v7.0.28 建议手数（k=0 加仓 = 1 手）
    expect(w.text()).toContain("¥0.4万"); // 1 手×100 股×现价 40 → 0.4 万
    // 表格信号列
    expect(w.text()).toContain("🔔 加仓");
    w.unmount();
  });

  it("买入（首仓）信号 → 提示条「买入·首仓」+ 表格信号列 🔔 买入", async () => {
    mockApi(MY_BUY);
    const w = mk();
    await flushPromises();
    expect(w.find(".signal-banner").exists()).toBe(true);
    expect(w.text()).toContain("买入·首仓");
    expect(w.text()).toContain("中国移动");
    expect(w.text()).toContain("现价 70");
    expect(w.text()).toContain("首仓（第 1 档）");
    expect(w.text()).toContain("1 手"); // v7.0.28 首仓 = 1 手
    expect(w.text()).toContain("¥0.7万"); // 1 手×100 股×现价 70 → 0.7 万
    // 表格信号列：只有触发行有标记
    expect(w.text()).toContain("🔔 买入");
    expect(w.text()).not.toContain("🔔 加仓");
    w.unmount();
  });

  it("无信号 → 无提示条，信号列空", async () => {
    mockApi(MY);
    const w = mk();
    await flushPromises();
    expect(w.find(".signal-banner").exists()).toBe(false);
    expect(w.text()).not.toContain("🔔 加仓");
    w.unmount();
  });

  it("v7.0.29 策略固定当前：无下拉/不发 ?strategy=/不读 ss.mypos.strategy", async () => {
    mockApi(MY);
    localStorage.setItem("ss.mypos.strategy", "保守_低档"); // 旧版残留选择
    const w = mk();
    await flushPromises();
    expect(w.find(".strat-sel select").exists()).toBe(false);
    expect(w.text()).toContain("策略应用：当前策略（strategy.yaml）");
    expect(apiMock.mock.calls.some(([pth]) => pth.startsWith("/api/my_positions?"))).toBe(false);
    expect(apiMock.mock.calls.some(([pth]) => pth.startsWith("/api/strategy_signals?"))).toBe(false);
    expect(apiMock.mock.calls.some(([pth]) => pth === "/api/strategies")).toBe(false);
    expect(localStorage.getItem("ss.mypos.strategy")).toBe("保守_低档"); // 组件不碰该键，原样保留
    w.unmount();
  });
});

describe("MyPositionsTab · 代码模糊搜索（v7.0.3）", () => {
  it("输拼音首字母 → 候选下拉，点击选中回填完整代码", async () => {
    mockApi(MY_EMPTY);
    const w = mk();
    await flushPromises();
    const codeInput = w.find(".code-sug-wrap input");
    await codeInput.setValue("zgsh");
    await vi.waitFor(() => expect(w.find(".code-sug-list").exists()).toBe(true));
    expect(w.text()).toContain("中国神华");
    await w.find(".code-sug-list li").trigger("mousedown");
    expect(codeInput.element.value).toBe("sh.601088");
    expect(w.find(".code-sug-list").exists()).toBe(false);
    expect(puts()).toHaveLength(0); // 选中 ≠ 提交
    w.unmount();
  });

  it("输代码数字 → 候选出现；Enter 确认第一条候选（不发 PUT）", async () => {
    mockApi(MY_EMPTY);
    const w = mk();
    await flushPromises();
    const codeInput = w.find(".code-sug-wrap input");
    await codeInput.setValue("600900");
    await vi.waitFor(() => expect(w.find(".code-sug-list").exists()).toBe(true));
    await codeInput.trigger("keydown", { key: "Enter" });
    expect(codeInput.element.value).toBe("sh.600900");
    expect(w.find(".code-sug-list").exists()).toBe(false);
    expect(puts()).toHaveLength(0);
    w.unmount();
  });

  it("无候选（空结果）→ 不下拉；搜索失败（503）→ 静默降级仍可手动输入", async () => {
    apiMock.mockImplementation(async (path, opts = {}) => {
      if (path === "/api/my_positions" && (!opts || !opts.method || opts.method === "GET")) {
        return JSON.parse(JSON.stringify(MY_EMPTY));
      }
      if (path.startsWith("/api/stocks/search")) {
        const e = new Error("lake locked");
        e.status = 503;
        throw e;
      }
      throw new Error("unmocked: " + path);
    });
    const w = mk();
    await flushPromises();
    const codeInput = w.find(".code-sug-wrap input");
    await codeInput.setValue("电力");
    await new Promise((r) => setTimeout(r, 450)); // debounce + 失败返回
    expect(w.find(".code-sug-list").exists()).toBe(false);
    // 手动输入完整代码不受影响
    await codeInput.setValue("sh.600036");
    expect(codeInput.element.value).toBe("sh.600036");
    w.unmount();
  });

  it("Esc 关闭下拉", async () => {
    mockApi(MY_EMPTY);
    const w = mk();
    await flushPromises();
    const codeInput = w.find(".code-sug-wrap input");
    await codeInput.setValue("zgsh");
    await vi.waitFor(() => expect(w.find(".code-sug-list").exists()).toBe(true));
    await codeInput.trigger("keydown", { key: "Escape" });
    expect(w.find(".code-sug-list").exists()).toBe(false);
    w.unmount();
  });
});

describe("MyPositionsTab · 行点击弹层（策略参考 + 交易登记 + 删除）", () => {
  it("行点击 → 策略参考字段 + 建仓时间线 + 删除 PUT shares=0（弹层无编辑区）", async () => {
    mockApi(MY);
    const w = mk();
    await flushPromises();
    const row = w.findAll("tbody tr").find((r) => r.text().includes("sh.601088"));
    expect(row).toBeTruthy();
    await row.trigger("click");
    await flushPromises();
    expect(w.text()).toContain("下一加仓触发价");
    expect(w.text()).toContain("42.07"); // 弹层展示原始口径（与最新价同单位）
    expect(w.find(".d-row").exists()).toBe(true);
    expect(w.text()).toContain("再武装触发价");
    // v8.2.0：弹层编辑区已移除（更新走顶部录入条），只留提示 + 删除
    expect(w.findAll(".edit-bar input")).toHaveLength(0);
    expect(w.text()).toContain("添加/更新持仓");
    // v8.2.0 建仓时间线（手动加仓自动记录）展示
    expect(w.text()).toContain("建仓时间线");
    expect(w.text()).toContain("2026-10-20");
    // 删除持仓（confirm stub=true）→ PUT shares=0
    const editBtns = w.findAll(".edit-bar button");
    await editBtns.find((b) => b.text() === "删除持仓").trigger("click");
    await flushPromises();
    expect(puts()).toHaveLength(1);
    expect(JSON.parse(puts()[0][1].body)).toEqual(
      { ts_code: "sh.601088", shares: 0, cost_price: 25, since_date: "" });
    w.unmount();
  });

  it("有回测结果（taskStore.lastResults）→ 弹层显示该股逐笔买入历史", async () => {
    mockApi(MY);
    const w = mk();
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
    mockApi(MY);
    const w = mk();
    await flushPromises();
    const row = w.findAll("tbody tr").find((r) => r.text().includes("sh.601088"));
    await row.trigger("click");
    await flushPromises();
    expect(w.text()).toContain("尚无回测结果");
    w.unmount();
  });

  it("关闭：右上角 ✕ 与 Esc 都能关（存量 bug 回归：曾漏绑 @close）", async () => {
    mockApi(MY);
    const w = mk();
    await flushPromises();
    const row = w.findAll("tbody tr").find((r) => r.text().includes("sh.601088"));
    await row.trigger("click");
    await flushPromises();
    expect(w.find(".modal").exists()).toBe(true);
    // ✕ 按钮
    await w.find(".modal-head button").trigger("click");
    await flushPromises();
    expect(w.find(".modal").exists()).toBe(false);
    // 重开 → Esc
    await row.trigger("click");
    await flushPromises();
    expect(w.find(".modal").exists()).toBe(true);
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    await flushPromises();
    expect(w.find(".modal").exists()).toBe(false);
    w.unmount();
  });
});

describe("MyPositionsTab · 弹层交易登记（v7.0.17，买 + 卖）", () => {
  async function openRow(w, code = "sh.601088") {
    const row = w.findAll("tbody tr").find((r) => r.text().includes(code));
    await row.trigger("click");
    await flushPromises();
    return w;
  }
  const buyInputs = (w) => w.findAll(".buy-inputs input");
  const buyBtn = (w) => w.findAll(".buy-bar button").find((b) => b.text() === "登记");
  const posts = () => apiMock.mock.calls.filter((c) => c[1] && c[1].method === "POST");

  it("有信号 → 股数预填建议值（建议手数×100）+ 价格预填最新价", async () => {
    mockApi(MY_SIGNAL); // sh.601088：suggested_lots 1 → 100 股；last_price 33.9
    const w = mk();
    await flushPromises();
    await openRow(w);
    const ins = buyInputs(w);
    expect(ins).toHaveLength(3); // 股数/价格/交易日期（v8.2.0 日期留空=今天）
    expect(ins[0].element.value).toBe("100"); // 1 手 × 100 股
    expect(ins[1].element.value).toBe("33.9"); // 最新价预填
    expect(ins[2].element.value).toBe(""); // 日期默认空（=今天）
    expect(w.find(".buy-preview").exists()).toBe(true);
    w.unmount();
  });

  it("无信号 → 股数不预填，价格仍预填最新价", async () => {
    mockApi(MY);
    const w = mk();
    await flushPromises();
    await openRow(w);
    const ins = buyInputs(w);
    expect(ins[0].element.value).toBe("");
    expect(ins[1].element.value).toBe("33.9");
    w.unmount();
  });

  it("正数登记（买入）→ POST /api/my_trades 加权更新 + 成功文案 + 账本快照同步", async () => {
    mockApi(MY); // 1200 股 @ 25
    const w = mk();
    await flushPromises();
    await openRow(w);
    const ins = buyInputs(w);
    await ins[0].setValue("200");
    await ins[1].setValue("30");
    // 预览：(1200×25 + 200×30) / 1400 = 25.714
    expect(w.find(".buy-preview").text()).toContain("1400 股 @ 25.714");
    await buyBtn(w).trigger("click");
    await flushPromises();
    expect(posts()).toHaveLength(1);
    expect(JSON.parse(posts()[0][1].body)).toEqual(
      { ts_code: "sh.601088", shares: 200, price: 30, date: "", name: "中国神华" });
    expect(w.text()).toContain("已登记买入 200 股 @ 30 → 1400 股 @ 25.714");
    // 交易历史即时出现新记录（登记后 loadTrades 刷新）
    expect(w.find(".trade-hist").exists()).toBe(true);
    expect(w.text()).toContain("已登记买入");
    w.unmount();
  });

  it("负数登记（卖出）→ POST 负股数 + 预览含已实现盈亏 + 成功文案", async () => {
    mockApi(MY); // 1200 股 @ 25
    const w = mk();
    await flushPromises();
    await openRow(w);
    const ins = buyInputs(w);
    await ins[0].setValue("-200");
    await ins[1].setValue("30");
    // 卖出预览：减股数 + 均价不变 + 已实现盈亏 (30−25)×200 = +1000
    expect(w.find(".buy-preview").text()).toContain("→ 1000 股 @ 25");
    expect(w.find(".buy-preview").text()).toContain("已实现盈亏 +1000.00");
    await buyBtn(w).trigger("click");
    await flushPromises();
    expect(posts()).toHaveLength(1);
    expect(JSON.parse(posts()[0][1].body).shares).toBe(-200);
    expect(w.text()).toContain("已登记卖出 200 股 @ 30 → 1000 股 @ 25.000");
    w.unmount();
  });

  it("卖光（负数 = −当前持仓）→ 清仓移除提示", async () => {
    mockApi(MY); // 1200 股
    const w = mk();
    await flushPromises();
    await openRow(w);
    const ins = buyInputs(w);
    await ins[0].setValue("-1200");
    await ins[1].setValue("30");
    await buyBtn(w).trigger("click");
    await flushPromises();
    expect(posts()).toHaveLength(1);
    expect(w.text()).toContain("清仓移除");
    w.unmount();
  });

  it("交易日期：手填历史日期回填（body date）；留空=今天（body date 空串）", async () => {
    mockApi(MY);
    const w = mk();
    await flushPromises();
    await openRow(w);
    const ins = buyInputs(w);
    // 手填历史日期 → 回填
    await ins[0].setValue("200");
    await ins[1].setValue("30");
    await ins[2].setValue("2026-11-01");
    await buyBtn(w).trigger("click");
    await flushPromises();
    expect(posts()).toHaveLength(1);
    expect(JSON.parse(posts()[0][1].body).date).toBe("2026-11-01");
    // 日期成功后清空 → 再登记一笔留空 = 今天
    await ins[0].setValue("100");
    await ins[1].setValue("31");
    await buyBtn(w).trigger("click");
    await flushPromises();
    expect(posts()).toHaveLength(2);
    expect(JSON.parse(posts()[1][1].body).date).toBe("");
    w.unmount();
  });

  it("校验：非整手 / 非整数 / 价格非正 / 卖出超持仓 → 报错不发 POST", async () => {
    mockApi(MY);
    const w = mk();
    await flushPromises();
    await openRow(w);
    const ins = buyInputs(w);

    await ins[0].setValue("150");
    await buyBtn(w).trigger("click");
    await flushPromises();
    expect(w.text()).toContain("整手");
    expect(posts()).toHaveLength(0);

    await ins[0].setValue("abc");
    await buyBtn(w).trigger("click");
    await flushPromises();
    expect(w.text()).toContain("非零整数");
    expect(posts()).toHaveLength(0);

    await ins[0].setValue("0");
    await buyBtn(w).trigger("click");
    await flushPromises();
    expect(w.text()).toContain("非零整数");
    expect(posts()).toHaveLength(0);

    await ins[0].setValue("-2000"); // 超持仓 1200
    await buyBtn(w).trigger("click");
    await flushPromises();
    expect(w.text()).toContain("卖出不能超过持仓");
    expect(posts()).toHaveLength(0);

    await ins[0].setValue("200");
    await ins[1].setValue("0");
    await buyBtn(w).trigger("click");
    await flushPromises();
    expect(w.text()).toContain("本次价格应为正数");
    expect(posts()).toHaveLength(0);
    w.unmount();
  });
});

describe("MyPositionsTab · 弹层交易历史（v7.0.17）", () => {
  const HISTORY = [
    { id: "t200", ts_code: "sh.601088", name: "中国神华", direction: "buy", shares: 200,
      price: 30, amount: 6000, date: "2026-09-10", realized_pnl: null,
      created_at: "2026-09-10T01:00:00Z" },
    { id: "t100", ts_code: "sh.601088", name: "中国神华", direction: "sell", shares: 100,
      price: 20, amount: 2000, date: "2026-08-01", realized_pnl: -500,
      created_at: "2026-08-01T01:00:00Z" },
    { id: "t300", ts_code: "sz.000651", name: "格力电器", direction: "buy", shares: 100,
      price: 40, amount: 4000, date: "2026-09-01", realized_pnl: null,
      created_at: "2026-09-01T01:00:00Z" },
  ];

  it("弹层打开 → 按当前股加载交易历史（买/卖标签 + 已实现盈亏 + 仅本股条目）", async () => {
    mockApi(MY, JSON.parse(JSON.stringify(HISTORY)));
    const w = mk();
    await flushPromises();
    const row = w.findAll("tbody tr").find((r) => r.text().includes("sh.601088"));
    await row.trigger("click");
    await flushPromises();
    expect(w.find(".trade-hist").exists()).toBe(true);
    const rows = w.findAll(".trade-hist tbody tr");
    expect(rows).toHaveLength(2); // sz.000651 的条目不显示
    expect(rows[0].text()).toContain("2026-09-10");
    expect(rows[0].text()).toContain("买");
    expect(rows[1].text()).toContain("2026-08-01");
    expect(rows[1].text()).toContain("卖");
    expect(rows[1].text()).toContain("-500"); // 已实现盈亏（负）
    // 删除按钮每行一个
    expect(w.findAll(".trade-hist tbody button")).toHaveLength(2);
    w.unmount();
  });

  it("删除（confirm）→ DELETE /api/my_trades/{id} + 列表刷新", async () => {
    mockApi(MY, JSON.parse(JSON.stringify(HISTORY)));
    const w = mk();
    await flushPromises();
    const row = w.findAll("tbody tr").find((r) => r.text().includes("sh.601088"));
    await row.trigger("click");
    await flushPromises();
    const delBtns = w.findAll(".trade-hist tbody button");
    await delBtns[0].trigger("click"); // 最新一条 t200（confirm stub=true）
    await flushPromises();
    const dels = apiMock.mock.calls.filter((c) => c[1] && c[1].method === "DELETE");
    expect(dels).toHaveLength(1);
    expect(dels[0][0]).toBe("/api/my_trades/t200");
    expect(w.findAll(".trade-hist tbody tr")).toHaveLength(1); // 剩 t100
    w.unmount();
  });

  it("无交易记录 → 空态提示", async () => {
    mockApi(MY);
    const w = mk();
    await flushPromises();
    const row = w.findAll("tbody tr").find((r) => r.text().includes("sh.601088"));
    await row.trigger("click");
    await flushPromises();
    expect(w.text()).toContain("暂无交易记录");
    expect(w.find(".trade-hist").exists()).toBe(false);
    w.unmount();
  });
});

describe("MyPositionsTab · 策略信号中心 (v7.0.19)", () => {

  it("渲染卡片 + 三档分段计数 + 默认当前信号（持仓加仓/非持仓首仓 + 时机门 + 回测 + 价源）", async () => {
    mockApi(MY, [], SIG);
    const w = mk();
    await flushPromises();
    // 卡片标题 + 覆盖说明 + 价源标注
    expect(w.text()).toContain("📡 策略信号");
    expect(w.text()).toContain("股票池 ∪ 我的持仓");
    expect(w.text()).toContain("5% 以内"); // approach_pct（v7.0.30 3→5）
    expect(w.text()).toContain("其余用最新收盘"); // spot_preselect_pct 实时取价覆盖说明
    expect(w.text()).toContain("近 90 天"); // history_window_days
    expect(w.text()).toContain("实时行情"); // price_source=spot
    // 分段计数（当前 = active 显示 3 + touched 1；active 价格态 5 只中非持仓
    // 时机未过 2 只被 v7.0.23 时机门滤出）；即将进入 = 价格临区 4 只：
    // 长江电力（持仓）+ 交通银行（分位 33.5%≤50%）+ 山煤国际（v7.0.31 C 案：
    // 分位 45% 未过 40% 门但 ≤50% 放宽带）；中国海油（85.7%）被滤出
    expect(w.text()).toContain("当前信号（4）");
    expect(w.text()).toContain("即将进入（3）");
    expect(w.text()).toContain("历史信号（4）");
    // 默认 active：持仓加仓（2 手 → 参考 0.8 万）+ 非持仓首仓（1 手 → 0.7 万）
    expect(w.text()).toContain("● 持仓");
    expect(w.text()).toContain("○ 非持仓");
    expect(w.text()).toContain("加仓");
    expect(w.text()).toContain("买入·首仓");
    expect(w.text()).toContain("2 手");
    expect(w.text()).toContain("1 手");
    expect(w.text()).toContain("0.8");
    expect(w.text()).toContain("0.7");
    // 盘中触碰（v7.0.20）：已触回标注 + 日内低列（保留到当日，次日移入历史）
    expect(w.text()).toContain("· 已触回");
    expect(w.text()).toContain("日内低");
    expect(w.text()).toContain("9.96");
    expect(w.text()).toContain("已触线");
    // v7.0.23 首仓双条件：非持仓 + 时机✓ + 价破首仓线（大秦：in_pos=true
    // 模拟已建仓也走首仓线）→ 入当前信号段；时机 ✗✗ 的（江苏银行/紫金矿业）
    // 价格态 active 也不入段（首仓缺时机门不成信号）
    expect(w.text()).toContain("大秦铁路");
    expect(w.text()).toContain("✓ 5年分位 19.1%（需≤40%） · ✓ 股息率 4.58%（需≥4.5%）");
    expect(w.text()).not.toContain("江苏银行");
    expect(w.text()).not.toContain("紫金矿业");
    // 时机门：逐条件 ✓/✓ 明细（无总判定前缀——全 ✓ 即过、任一 ✗ 即未过）
    expect(w.text()).toContain("✓ 5年分位 21.9%（需≤40%） · ✓ 股息率 4.59%（需≥4.5%）");
    // 十年回测列：+88.5% · 6档（持仓神华）/ 无回测 → —
    expect(w.text()).toContain("+88.5% · 6档");
    expect(w.text()).toContain("+45.2% · 4档");
    expect(w.text()).toContain("+120.4% · 7档"); // 大秦
    w.unmount();
  });

  it("切「即将进入」显示距触发%；切「历史信号」按日期降序 + 档位标签（首仓/第N档）", async () => {
    mockApi(MY, [], SIG);
    const w = mk();
    await flushPromises();
    const segBtns = () => w.findAll(".sig-seg button");
    await segBtns()[1].trigger("click"); // 即将进入
    await flushPromises();
    expect(w.text()).toContain("2.0%"); // 距触发 distance_pct
    expect(w.text()).toContain("长江电力"); // 持仓 approaching 股在表内
    // v7.0.22 时机门：非持仓 + 时机达标（交通银行 33.5%）展示
    expect(w.text()).toContain("交通银行");
    // v7.0.31 C 案（山煤国际实例）：分位 45% 未过 40% 门但 ≤50% 放宽带 →
    // 展示并标「未过」；分位 85.7% 超放宽带 → 不入段
    expect(w.text()).toContain("山煤国际");
    expect(w.text()).toContain("✗ 5年分位 45.0%（需≤40%）");
    expect(w.text()).not.toContain("中国海油");
    await segBtns()[2].trigger("click"); // 历史信号
    await flushPromises();
    // 历史日期降序：2026-11-02 在前，2026-10-11 在后
    const idx1102 = w.text().indexOf("2026-11-02");
    const idx1011 = w.text().indexOf("2026-10-11");
    expect(idx1102).toBeGreaterThanOrEqual(0);
    expect(idx1011).toBeGreaterThanOrEqual(idx1102);
    // 档位标签：首仓（first_of_round && level=0）+ 第2档（level=1）
    expect(w.text()).toContain("首仓");
    expect(w.text()).toContain("第2档");
    // 微信推送标记：第1档 2026-11-02 已推 ✓，其余 —
    expect(w.text()).toContain("✓ 已推");
    // 触碰事件行（source=touch，收盘 job 落盘次日移入历史）
    expect(w.text()).toContain("中国核电");
    w.unmount();
  });

  it("lake_locked 降级提示条（仅列账本）", async () => {
    const locked = { ...JSON.parse(JSON.stringify(SIG)), lake_locked: true };
    mockApi(MY, [], locked);
    const w = mk();
    await flushPromises();
    expect(w.text()).toContain("数据湖正被灌数进程占用");
    w.unmount();
  });
});

// ---------------------------------------------------------------------------
// v7.0.30 实时刷新轮询门：交易时段内 30s 轮询；非时段保留缓存不发网络请求；
// 首载恒取一次；手动「刷新」按钮经 forceKeys 强制走网络（非时段也可）。
// 时段判定用 fake timers 固定系统时钟（2026-11-09 周一 20:00 北京 = 非时段）。
// ---------------------------------------------------------------------------
describe("MyPositionsTab · v7.0.30 实时刷新轮询门", () => {
  const getCalls = () => apiMock.mock.calls.filter(
    (c) => !c[1] || !c[1].method || c[1].method === "GET");

  it("非时段：首载取一次、30s 轮询空转零网络、手动刷新强制走网络", async () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(new Date("2026-11-09T20:00:00+08:00")); // 周一 20:00 北京
      mockApi(MY, [], SIG);
      const w = mk();
      await flushPromises();
      // 首载：两个 key 各取一次（非时段也取——首载恒取，保证有最近数据）
      expect(getCalls().length).toBe(2);

      // 30s 轮询 tick ×2：非时段 + 有缓存 → 返回缓存，零网络
      vi.advanceTimersByTime(30000);
      await flushPromises();
      vi.advanceTimersByTime(30000);
      await flushPromises();
      expect(getCalls().length).toBe(2); // 仍只有首载 2 次

      // 手动「刷新」→ forceKeys 强制走网络（非时段同样生效）
      const btn = w.findAll("button").find((b) => b.text().includes("刷新"));
      expect(btn).toBeTruthy();
      await btn.trigger("click");
      await flushPromises();
      expect(getCalls().length).toBe(4); // 两 key 各补一次
      w.unmount();
    } finally {
      vi.useRealTimers();
    }
  });
});
