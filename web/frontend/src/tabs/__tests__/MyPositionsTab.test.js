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
const { invalidate } = await import("../../live/queryRegistry.js");

const MY = {
  as_of: "2026-10-21",
  n: 1,
  signals: [],
  strategy: "current",
  positions: [
    { ts_code: "sh.601088", name: "中国神华", shares: 1200, cost_price: 25.0,
      last_price: 33.9, market_value: 40680, pnl: 10680, pnl_pct: 36.0,
      updated_at: "2026-10-22T01:00:00Z",
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
      k_level: 0, next_level: 1, suggested_amount: 100000, action: "add" },
  ],
};

const MY_EMPTY = { as_of: null, n: 0, positions: [],
  totals: { market_value: 0, cost: 0, pnl: 0, pnl_pct: null } };

function mockApi(my) {
  apiMock.mockImplementation(async (path, opts = {}) => {
    if (path.startsWith("/api/my_positions") && (!opts || !opts.method || opts.method === "GET")) {
      return JSON.parse(JSON.stringify(my));
    }
    if (path.startsWith("/api/my_positions") && opts && opts.method === "PUT") {
      const body = JSON.parse(opts.body);
      return { ok: true, ts_code: body.ts_code, n: 1 };
    }
    if (path === "/api/strategies") {
      return [{ name: "保守_低档", saved_at: "2026-10-22 17:48:00", size_bytes: 512 }];
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
  // 数据（或空数据）会被后续测试的挂载直接命中。
  invalidate("my_positions");
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

  it("有账本：表格行 + 汇总条（万为单位）+ 数据截至", async () => {
    mockApi(MY);
    const w = mk();
    await flushPromises();
    expect(w.text()).toContain("中国神华");
    expect(w.text()).toContain("sh.601088");
    expect(w.text()).toContain("¥4.1万"); // 40680 市值
    expect(w.text()).toContain("数据截至 2026-10-21");
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
    expect(inputs).toHaveLength(3);
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
      { ts_code: "sh.601088", shares: 100, cost_price: 30 });
    expect(w.text()).toContain("已保存 sh.601088");
    w.unmount();
  });
});

describe("MyPositionsTab · 策略应用 + 加仓信号（v7.0.4）", () => {
  it("有信号 → 红色提示条（名称/触发价/档位/建议金额）+ 表格信号列 🔔", async () => {
    mockApi(MY_SIGNAL);
    const w = mk();
    await flushPromises();
    expect(w.find(".signal-banner").exists()).toBe(true);
    expect(w.text()).toContain("加仓提示");
    expect(w.text()).toContain("1 只达到加仓触发价");
    expect(w.text()).toContain("中国神华");
    expect(w.text()).toContain("现价 40"); // 现价（实时价，数字渲染为 40）
    expect(w.text()).toContain("42.07"); // 触发价（原始口径）
    expect(w.text()).toContain("实时行情"); // 价源标注
    expect(w.text()).toContain("第 0 档 → 第 1 档");
    expect(w.text()).toContain("¥10.0万"); // suggested_amount 100000 → 10.0万
    // 表格信号列
    expect(w.text()).toContain("🔔 加仓");
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

  it("策略应用下拉：默认当前策略；切换策略库条目 → 重拉带 ?strategy=", async () => {
    mockApi(MY);
    const w = mk();
    await flushPromises();
    const sel = w.find(".strat-sel select");
    expect(sel.exists()).toBe(true);
    expect(sel.element.value).toBe("current");
    // 挂载后首次请求不带 strategy
    expect(apiMock.mock.calls.some(([p]) => p === "/api/my_positions")).toBe(true);
    // 切到策略库条目（mock 的 /api/strategies 返回两条）
    await sel.setValue("保守_低档");
    await flushPromises();
    expect(apiMock.mock.calls.some(([p]) => p === "/api/my_positions?strategy=" + encodeURIComponent("保守_低档")))
      .toBe(true);
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

describe("MyPositionsTab · 行点击弹层（策略参考 + 编辑）", () => {
  it("行点击 → 策略参考字段 + 编辑预填 + 保存 PUT + 删除 PUT shares=0", async () => {
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
    // 编辑 input 预填（股数/成本）
    const editInputs = w.findAll(".edit-bar input");
    expect(editInputs).toHaveLength(2);
    expect(editInputs[0].element.value).toBe("1200");
    expect(editInputs[1].element.value).toBe("25");
    // 改股数 → 保存
    await editInputs[0].setValue("1500");
    const editBtns = w.findAll(".edit-bar button");
    await editBtns.find((b) => b.text() === "保存").trigger("click");
    await flushPromises();
    expect(puts()).toHaveLength(1);
    expect(JSON.parse(puts()[0][1].body)).toEqual(
      { ts_code: "sh.601088", shares: 1500, cost_price: 25 });
    // 删除（confirm stub=true）→ PUT shares=0
    await editBtns.find((b) => b.text() === "删除").trigger("click");
    await flushPromises();
    expect(puts()).toHaveLength(2);
    expect(JSON.parse(puts()[1][1].body)).toEqual(
      { ts_code: "sh.601088", shares: 0, cost_price: 25 });
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
});
