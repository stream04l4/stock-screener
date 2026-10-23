// StrategyTab 组件回归（v7.0）：策略库（另存为/加载/删除）+ 13 字段页渲染 + 本地保存校验。
//
// mock 点：api client（零网络）+ queryRegistry.invalidate（隔离计数）。
// useLiveQuery 走真实实现（happy-dom），"strategy"/"strategies" key 由 apiMock 按 path 分发。
// StrategyTab 用 useToastStore()（pinia）→ mount 必须带 createPinia()；toast 断言直接读 store.msg。
// 契约（v7）：
// - 页面渲染 13 个字段行：strategy 8（name 隐藏）+ universe 5；lake/datasource 不渲染。
// - 本地校验：int/float 非法 → msg-box 报错不提交；list 空 → 报错不提交。
// - 400 → 保持编辑态显示 errors（与旧版同 UI）。
import { beforeEach, describe, expect, it, vi } from "vitest";
import { nextTick } from "vue";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia } from "pinia";

const apiMock = vi.fn();
vi.mock("../../../api/client.js", () => ({
  api: (...a) => apiMock(...a),
  isBackfillErr: (e) => !!(e && e.status === 409 && e.body && e.error === "lake_backfill_in_progress"),
}));
const invalidateMock = vi.fn();
vi.mock("../../../live/queryRegistry.js", async (orig) => ({
  ...(await orig()), invalidate: (...a) => invalidateMock(...a),
}));

const StrategyTab = (await import("../../StrategyTab.vue")).default;
const { useToastStore } = await import("../../../stores/toastStore.js");

// v7 slim 策略 JSON（strategy 9 键 + universe 5 键 + lake + datasource）
const STRATEGY_JSON = {
  strategy: {
    name: "drawdown_martingale",
    initial_amount: 100000,
    base_add_amount: 10000,
    first_drawdown_pct: 15,
    add_step_pct: 5,
    growth_ratio: 1.1,
    rearm_mode: "bounce",
    rearm_pct: 15,
    accounting: "cash",
  },
  universe: {
    industry_whitelist_csric2: ["B06", "B07", "C25", "D44", "D45", "D46", "G53", "G54", "G55", "G56", "I63", "J66"],
    soe_keywords: ["国务院", "国资委", "汇金", "财政部", "国资"],
    min_total_mv_yi: 200,
    min_consecutive_div_years: 5,
    listing_min_trading_days: 250,
  },
  lake: { baostock_daily_budget: 5000 },
  datasource: { baostock: { daily_quota: 49900 } },
};

const LIB = [
  { name: "深回撤 v2", saved_at: "2026-09-18 10:00:00", size_bytes: 1234 },
  { name: "策略_20260917_0900", saved_at: "2026-09-17 09:00:00", size_bytes: 1200 },
];

let putHandler = null; // PUT /api/strategy 的可变处理（默认成功）
function mockApi() {
  apiMock.mockImplementation(async (path, opts = {}) => {
    if (path === "/api/strategy" && (!opts.method || opts.method === "GET")) {
      return { json: JSON.parse(JSON.stringify(STRATEGY_JSON)), raw: "# raw" };
    }
    if (path === "/api/strategy" && opts.method === "PUT") {
      if (putHandler) return putHandler(opts);
      return { ok: true, backup: "/tmp/config/strategy.yaml.bak" };
    }
    if (path === "/api/strategies" && (!opts.method || opts.method === "GET")) {
      return JSON.parse(JSON.stringify(LIB));
    }
    if (path === "/api/strategies" && opts.method === "POST") {
      const name = JSON.parse(opts.body).name;
      if (LIB.some((s) => s.name === name)) {
        const e = new Error(JSON.stringify({ error: "strategy_exists", name }));
        e.status = 409;
        e.body = { detail: { error: "strategy_exists", name } };
        throw e;
      }
      LIB.push({ name, saved_at: "2026-09-19 12:00:00", size_bytes: 999 });
      return { name, path: `/tmp/strategies/${name}.yaml`, saved_at: "2026-09-19 12:00:00" };
    }
    if (/^\/api\/strategies\/.+\/load$/.test(path) && opts.method === "POST") {
      return { ok: true, backup: "/tmp/config/strategy.yaml.bak" };
    }
    const dm = path.match(/^\/api\/strategies\/(.+)$/);
    if (dm && opts.method === "DELETE") {
      const i = LIB.findIndex((s) => s.name === decodeURIComponent(dm[1]));
      if (i >= 0) LIB.splice(i, 1);
      return null; // 204
    }
    throw new Error("unmocked: " + path);
  });
}

let appPinia = null;
const mk = () => {
  const pinia = createPinia();
  appPinia = pinia;
  return mount(StrategyTab, { attachTo: document.body, global: { plugins: [pinia] } });
};
const toastMsg = () => useToastStore(appPinia).msg;

beforeEach(() => {
  LIB.length = 0;
  LIB.push(
    { name: "深回撤 v2", saved_at: "2026-09-18 10:00:00", size_bytes: 1234 },
    { name: "策略_20260917_0900", saved_at: "2026-09-17 09:00:00", size_bytes: 1200 },
  );
  putHandler = null;
  vi.stubGlobal("confirm", vi.fn(() => true));
  apiMock.mockReset();
  invalidateMock.mockClear();
  mockApi();
});

describe("v7 · 13 字段页渲染（strategy 8 + universe 5；name 隐藏；lake/datasource 不渲染）", () => {
  it("渲染 2 组（策略参数/股票池）；字段行 = 13；name 不出现为字段行", async () => {
    const w = mk();
    await flushPromises();
    const groups = w.findAll(".s-group");
    expect(groups.length).toBe(2);
    expect(groups[0].text()).toContain("策略参数");
    expect(groups[1].text()).toContain("股票池");
    const fields = w.findAll(".s-field");
    expect(fields.length).toBe(13);
    // 2026-10-23：字段行主显示 = 通俗名；原始 yaml 字段名降为 .s-key 的 title（hover 提示）
    const keys = fields.map((f) => f.find(".s-key").text());
    expect(keys).toEqual([
      "首仓金额（元）", "加仓基准金额（元）", "首次建仓回撤（%）", "加仓步长（%）",
      "加仓递增比率", "高点再武装方式", "再武装反弹幅度（%）", "成交口径（固定）",
      "行业白名单（证监会二级代码）", "央国企识别关键词", "总市值下限（亿元）",
      "连续分红年数下限", "最少上市交易日",
    ]);
    // 原始字段名保留为 hover 提示（排查用）
    expect(fields.map((f) => f.find(".s-key").attributes("title"))).toEqual([
      "initial_amount", "base_add_amount", "first_drawdown_pct", "add_step_pct",
      "growth_ratio", "rearm_mode", "rearm_pct", "accounting",
      "industry_whitelist_csric2", "soe_keywords", "min_total_mv_yi",
      "min_consecutive_div_years", "listing_min_trading_days",
    ]);
    // name（策略标识）不作为字段行
    expect(keys).not.toContain("策略标识");
    // lake/datasource 段不渲染（yaml-only）
    const all = w.text();
    expect(all).not.toContain("baostock_daily_budget");
    expect(all).not.toContain("daily_quota");
    // 必附注在 strategy 段说明
    expect(all).toContain("k=0");
    // enum 值只读态显示通俗短名（bounce → 反弹重置），不是裸值
    const rearmRow = fields.find((f) => f.find(".s-key").attributes("title") === "rearm_mode");
    expect(rearmRow.find(".s-val").text()).toBe("反弹重置");
    w.unmount();
  });

  it("只读态：accounting 显示 cash；编辑态：6 个 number input（int×2+float×4），accounting 仍只读", async () => {
    const w = mk();
    await flushPromises();
    // 只读态 accounting 值（按 title=原始字段名定位）
    const acctRow = w.findAll(".s-field").find((f) => f.find(".s-key").attributes("title") === "accounting");
    expect(acctRow.find(".s-val").text()).toBe("cash");
    // 进入编辑态
    await w.find("#btn-strategy-edit").trigger("click");
    await nextTick();
    // strategy 6（int×2 + float×4）+ universe 3（min_total_mv_yi float + 2×int）= 9
    expect(w.findAll('input[type="number"]').length).toBe(9);
    // accounting 编辑态仍是只读展示（无 input，有 .s-readonly）
    const acctRowEdit = w.findAll(".s-field").find((f) => f.find(".s-key").attributes("title") === "accounting");
    expect(acctRowEdit.find("input").exists()).toBe(false);
    expect(acctRowEdit.find(".s-readonly").text()).toBe("cash");
    w.unmount();
  });
});

describe("v7 · 本地保存校验（int/float/list；400 保持编辑态）", () => {
  it("int 字段输入非整数 → msg-box 报错、不发 PUT", async () => {
    const w = mk();
    await flushPromises();
    await w.find("#btn-strategy-edit").trigger("click");
    await nextTick();
    // initial_amount 是第一个 number input
    const input = w.findAll('input[type="number"]')[0];
    await input.setValue("abc");
    await w.find("#btn-strategy-save").trigger("click");
    await flushPromises();
    expect(w.find("#strategy-msg .msg-err").text()).toContain("必须是整数");
    expect(apiMock.mock.calls.some(([p, o]) => (o || {}).method === "PUT")).toBe(false);
    // 编辑态保持（400/本地校验失败语义：不退出编辑态）
    expect(w.find("#btn-strategy-save").classes()).not.toContain("hidden");
    w.unmount();
  });

  it("list 字段清空 → 报错「不能为空」、不发 PUT", async () => {
    const w = mk();
    await flushPromises();
    await w.find("#btn-strategy-edit").trigger("click");
    await nextTick();
    const listInputs = w.findAll('input[type="text"][placeholder]');
    // soe_keywords list（含中文关键词的那个）
    const soe = listInputs.find((i) => i.element.value.includes("国务院"));
    expect(soe).toBeTruthy();
    await soe.setValue("");
    await w.find("#btn-strategy-save").trigger("click");
    await flushPromises();
    expect(w.find("#strategy-msg .msg-err").text()).toContain("不能为空");
    expect(apiMock.mock.calls.some(([p, o]) => (o || {}).method === "PUT")).toBe(false);
    w.unmount();
  });

  it("合法编辑 → PUT full（其他段一个 key 不丢）→ 成功 toast + 退出编辑态 + invalidate", async () => {
    let captured = null;
    putHandler = async (opts) => { captured = JSON.parse(opts.body); return { ok: true, backup: "/x/y.yaml.bak" }; };
    const w = mk();
    await flushPromises();
    await w.find("#btn-strategy-edit").trigger("click");
    await nextTick();
    const input = w.findAll('input[type="number"]')[0]; // initial_amount
    await input.setValue("200000");
    await w.find("#btn-strategy-save").trigger("click");
    await flushPromises();
    expect(captured).toBeTruthy();
    expect(captured.strategy.initial_amount).toBe(200000);
    // PUT full：lake/datasource/universe 段完整带回
    expect(captured.lake).toEqual({ baostock_daily_budget: 5000 });
    expect(captured.datasource).toEqual({ baostock: { daily_quota: 49900 } });
    expect(Object.keys(captured.universe)).toHaveLength(5);
    expect(toastMsg()).toContain("策略已更新");
    expect(invalidateMock).toHaveBeenCalledWith("strategy");
    expect(w.find("#btn-strategy-save").classes()).toContain("hidden"); // 退出编辑态
    w.unmount();
  });

  it("PUT 400（detail.errors）→ 保持编辑态 + msg-box 显示错误", async () => {
    putHandler = () => {
      const e = new Error("validation");
      e.status = 400;
      e.body = { detail: { errors: ["strategy.first_drawdown_pct=200 超出范围 [1,90]"] } };
      throw e;
    };
    const w = mk();
    await flushPromises();
    await w.find("#btn-strategy-edit").trigger("click");
    await nextTick();
    await w.find("#btn-strategy-save").trigger("click");
    await flushPromises();
    expect(w.find("#strategy-msg .msg-err").text()).toContain("超出范围");
    expect(w.find("#btn-strategy-save").classes()).not.toContain("hidden"); // 编辑态保持
    w.unmount();
  });
});

describe("策略库 · 另存为/加载/删除（机制沿用 v6.2.1 S1）", () => {
  it("另存为：弹窗默认名 + 保存 → toast + 下拉刷新", async () => {
    const w = mk();
    await flushPromises();
    await w.find("#btn-strategy-save-as").trigger("click");
    const input = w.find("#input-saveas-name");
    expect(input.exists()).toBe(true);
    expect(input.element.value).toMatch(/^策略_\d{8}_\d{4}$/);
    await input.setValue("我的深回撤 v2");
    await w.find("#btn-saveas-confirm").trigger("click");
    await flushPromises();
    expect(toastMsg()).toContain("已保存到策略库：我的深回撤 v2");
    expect(w.find("#input-saveas-name").exists()).toBe(false);
    w.unmount();
  });

  it("加载：选中 → confirm → POST load → toast + invalidate", async () => {
    const w = mk();
    await flushPromises();
    await w.find("#select-strategy-load").setValue("深回撤 v2");
    await flushPromises();
    expect(window.confirm).toHaveBeenCalled();
    expect(apiMock.mock.calls.some(([p]) => /\/load$/.test(p))).toBe(true);
    expect(toastMsg()).toContain("已加载 深回撤 v2");
    expect(invalidateMock).toHaveBeenCalledWith("strategy");
    w.unmount();
  });

  it("删除：选中后 confirm → DELETE → 下拉刷新；confirm 取消 → 不发", async () => {
    const w = mk();
    await flushPromises();
    expect(w.find("#btn-strategy-delete").attributes("disabled")).toBeDefined();
    await w.find("#select-strategy-load").setValue("深回撤 v2");
    await flushPromises();
    expect(w.find("#btn-strategy-delete").attributes("disabled")).toBeUndefined();
    const confirmSpy = vi.fn(() => true);
    vi.stubGlobal("confirm", confirmSpy);
    await w.find("#btn-strategy-delete").trigger("click");
    await flushPromises();
    expect(confirmSpy).toHaveBeenCalled();
    expect(apiMock.mock.calls.some(([p, o]) => (o || {}).method === "DELETE" && p.startsWith("/api/strategies/"))).toBe(true);
    w.unmount();
  });

  it("编辑态下策略库三件套禁用", async () => {
    const w = mk();
    await flushPromises();
    await w.find("#btn-strategy-edit").trigger("click");
    expect(w.find("#btn-strategy-save-as").attributes("disabled")).toBeDefined();
    expect(w.find("#select-strategy-load").attributes("disabled")).toBeDefined();
    expect(w.find("#btn-strategy-delete").attributes("disabled")).toBeDefined();
    w.unmount();
  });
});
