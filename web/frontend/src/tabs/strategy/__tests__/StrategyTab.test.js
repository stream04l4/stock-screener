// v6.2.1 S1 组件回归：StrategyTab 策略库（另存为弹窗 / 加载下拉 / 删除）+ S2 分组调整。
//
// mock 点：api client（零网络）+ queryRegistry.invalidate（隔离计数）。
// useLiveQuery 走真实实现（happy-dom），"strategy"/"strategies" key 由 apiMock 按 path 分发。
// StrategyTab 用 useToastStore()（pinia）→ mount 必须带 createPinia()；toast 断言直接读 store.msg。
// 契约：
// - 【💾 另存为】→ 弹窗（#input-saveas-name 默认 策略_YYYYMMDD_HHMM）+【保存】→ POST /api/strategies；
//   成功 → toast"已保存到策略库：<name>" + GET /api/strategies 重拉（下拉刷新）；409 → toast 重名提示。
// - 【📂 加载】= select（占位"— 选择策略 —"+ 全部策略名）；选中即 confirm → POST .../load；
//   成功 → toast"已加载 <name>" + invalidate("strategy")；confirm 取消 → 不发请求、下拉复位。
// - 【🗑️ 删除】= 删当前选中项（confirm 二次确认）→ DELETE → 刷新下拉；未选中时按钮禁用。
// - 编辑态下 另存为/加载/删除 全部禁用（防丢未保存修改）。
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

// 最小策略 JSON（含 lake/backtest/reinvest 段——S2 断言它们**不**渲染在 StrategyTab）
const STRATEGY_JSON = {
  technical: { ma_period: 20, min_return_pct: 3 },
  dividend: { window_days: 365, min_yield_pct: 4 },
  industry: { rank_by: "roeAvg", top_pct: 30 },
  fundamental: { roe_min_pct: 8 },
  universe: { a_share_prefixes: ["sh.60"], listing_min_trading_days: 250 },
  scoring: { mode: "zscore", top_n: 50, weights: { technical: 0.3, dividend: 0.2, industry: 0.1, fundamental: 0.4 } },
  badges: { industry_top_pct: 30, fscore_min: 5 },
  hard_filter: { st_enabled: true, min_consecutive_div_years: 3 },
  backtest: { start: "2021-01-01", top_n: 10 },
  reinvest: { target_ttm_yield_pct: 4 },
  datasource: { primary: "tencent" },
  data: { kline_calendar_days_back: 800 },
  crosscheck: { enabled: false },
  health: { enabled: true },
  canonical: { enabled: true },
  lake: { baostock_daily_budget: 20000 },
};

const LIB = [
  { name: "高股息变体", saved_at: "2026-09-18 10:00:00", size_bytes: 1234 },
  { name: "策略_20260917_0900", saved_at: "2026-09-17 09:00:00", size_bytes: 1200 },
];

function mockApi() {
  apiMock.mockImplementation(async (path, opts = {}) => {
    if (path === "/api/strategy" && (!opts.method || opts.method === "GET")) {
      return { json: JSON.parse(JSON.stringify(STRATEGY_JSON)), raw: "# raw" };
    }
    if (path === "/api/strategies" && (!opts.method || opts.method === "GET")) {
      // 与真实后端同语义：列表 = 当前库状态（保存后含新项）
      return JSON.parse(JSON.stringify(LIB));
    }
    if (path === "/api/strategies" && opts.method === "POST") {
      const name = JSON.parse(opts.body).name;
      if (LIB.some((s) => s.name === name)) {
        const e = new Error("strategy_exists"); e.status = 409; e.body = { error: "strategy_exists" }; throw e;
      }
      LIB.push({ name, saved_at: "2026-09-19 12:00:00", size_bytes: 999 }); // 落库（真实后端写文件）
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

const mk = () => {
  const pinia = createPinia();
  appPinia = pinia; // toast 断言用**同一个** pinia 实例（组件内 useToastStore() 走 app pinia）
  return mount(StrategyTab, { attachTo: document.body, global: { plugins: [pinia] } });
};
let appPinia = null;
const toastMsg = () => useToastStore(appPinia).msg;

beforeEach(() => {
  // LIB 是 mockApi 的可变状态（保存/删除会改它）——每用例重置，防跨用例串味
  LIB.length = 0;
  LIB.push(
    { name: "高股息变体", saved_at: "2026-09-18 10:00:00", size_bytes: 1234 },
    { name: "策略_20260917_0900", saved_at: "2026-09-17 09:00:00", size_bytes: 1200 },
  );
  vi.stubGlobal("confirm", vi.fn(() => true));
  apiMock.mockReset();
  invalidateMock.mockClear();
  mockApi();
});

describe("S1 · 另存为弹窗（默认名 + POST /api/strategies）", () => {
  it("点【💾 另存为】→ 弹窗打开，输入框默认名 = 策略_YYYYMMDD_HHMM", async () => {
    const w = mk();
    await flushPromises();
    expect(w.find("#input-saveas-name").exists()).toBe(false); // 未点前无弹窗
    await w.find("#btn-strategy-save-as").trigger("click");
    const input = w.find("#input-saveas-name");
    expect(input.exists()).toBe(true);
    expect(input.element.value).toMatch(/^策略_\d{8}_\d{4}$/);
    w.unmount();
  });

  it("改名为中文名 +【保存】→ POST {name} → toast「已保存到策略库：<name>」+ 下拉刷新", async () => {
    const w = mk();
    await flushPromises();
    await w.find("#btn-strategy-save-as").trigger("click");
    await w.find("#input-saveas-name").setValue("我的高股息 v2");
    await w.find("#btn-saveas-confirm").trigger("click");
    await flushPromises();
    const postCall = apiMock.mock.calls.find(([p, o]) => p === "/api/strategies" && (o || {}).method === "POST");
    expect(JSON.parse(postCall[1].body).name).toBe("我的高股息 v2");
    expect(toastMsg()).toContain("已保存到策略库：我的高股息 v2");
    // 下拉刷新 = GET /api/strategies 至少被调两次（首帧 + 保存后）
    const gets = apiMock.mock.calls.filter(([p]) => p === "/api/strategies").length;
    expect(gets).toBeGreaterThanOrEqual(2);
    expect(w.find("#input-saveas-name").exists()).toBe(false); // 弹窗关闭
    w.unmount();
  });

  it("重名 → 409 strategy_exists → toast 提示换名（弹窗不关，可改名重试）", async () => {
    const w = mk();
    await flushPromises();
    await w.find("#btn-strategy-save-as").trigger("click");
    await w.find("#input-saveas-name").setValue("高股息变体"); // LIB 中已存在
    await w.find("#btn-saveas-confirm").trigger("click");
    await flushPromises();
    expect(toastMsg()).toContain("已存在");
    expect(w.find("#input-saveas-name").exists()).toBe(true); // 弹窗仍开着
    w.unmount();
  });
});

describe("S1 · 加载下拉（渲染 + 选中 load + confirm 取消）", () => {
  it("下拉渲染：占位「— 选择策略 —」+ 全部策略名", async () => {
    const w = mk();
    await flushPromises();
    const sel = w.find("#select-strategy-load");
    expect(sel.exists()).toBe(true);
    const opts = sel.findAll("option").map((o) => o.text());
    expect(opts).toEqual([
      "— 选择策略 —",
      "高股息变体（2026-09-18 10:00:00）",
      "策略_20260917_0900（2026-09-17 09:00:00）",
    ]);
    w.unmount();
  });

  it("选中 → confirm → POST .../load → toast「已加载 <name>」+ invalidate('strategy')", async () => {
    const w = mk();
    await flushPromises();
    await w.find("#select-strategy-load").setValue("高股息变体");
    await flushPromises();
    expect(window.confirm).toHaveBeenCalledWith("将用 高股息变体 覆盖当前策略配置，确认？");
    expect(apiMock.mock.calls.some(([p]) => /\/load$/.test(p))).toBe(true);
    expect(toastMsg()).toContain("已加载 高股息变体");
    expect(invalidateMock).toHaveBeenCalledWith("strategy");
    w.unmount();
  });

  it("confirm 取消 → 不发 load 请求，下拉复位占位", async () => {
    vi.stubGlobal("confirm", vi.fn(() => false));
    const w = mk();
    await flushPromises();
    await w.find("#select-strategy-load").setValue("高股息变体");
    await flushPromises();
    expect(apiMock.mock.calls.some(([p]) => /\/load$/.test(p))).toBe(false);
    await nextTick(); // 复位 selectedName="" → 等 Vue 重渲染 select value
    expect(w.find("#select-strategy-load").element.value).toBe(""); // 复位占位
    w.unmount();
  });
});

describe("S1 · 删除（删当前选中项 + confirm 二次确认）", () => {
  it("未选中时【🗑️ 删除】禁用；选中后 → confirm → DELETE → 下拉刷新", async () => {
    const w = mk();
    await flushPromises();
    expect(w.find("#btn-strategy-delete").attributes("disabled")).toBeDefined(); // 未选中禁用
    await w.find("#select-strategy-load").setValue("高股息变体");
    await flushPromises(); // 选中即触发 load（confirm=true）
    expect(w.find("#btn-strategy-delete").attributes("disabled")).toBeUndefined();
    const confirmSpy = vi.fn(() => true);
    vi.stubGlobal("confirm", confirmSpy);
    await w.find("#btn-strategy-delete").trigger("click");
    await flushPromises();
    expect(confirmSpy).toHaveBeenCalled();
    expect(apiMock.mock.calls.some(([p, o]) => (o || {}).method === "DELETE" && p.startsWith("/api/strategies/"))).toBe(true);
    await nextTick(); // 复位 selectedName="" → 等 Vue 重渲染 select value
    expect(w.find("#select-strategy-load").element.value).toBe(""); // 复位占位
    w.unmount();
  });

  it("删除 confirm 取消 → 不发 DELETE", async () => {
    const w = mk();
    await flushPromises();
    vi.stubGlobal("confirm", vi.fn(() => true)); // load 的 confirm 先过
    await w.find("#select-strategy-load").setValue("高股息变体");
    await flushPromises();
    vi.stubGlobal("confirm", vi.fn(() => false)); // 删除确认取消
    await w.find("#btn-strategy-delete").trigger("click");
    await flushPromises();
    expect(apiMock.mock.calls.some(([p, o]) => (o || {}).method === "DELETE")).toBe(false);
    w.unmount();
  });
});

describe("S1 · 编辑态下策略库三件套禁用（防丢未保存修改）", () => {
  it("点【✏️ 编辑模式】后 另存为/加载下拉/删除 全部 disabled", async () => {
    const w = mk();
    await flushPromises();
    await w.find("#btn-strategy-edit").trigger("click");
    expect(w.find("#btn-strategy-save-as").attributes("disabled")).toBeDefined();
    expect(w.find("#select-strategy-load").attributes("disabled")).toBeDefined();
    expect(w.find("#btn-strategy-delete").attributes("disabled")).toBeDefined();
    // D-W02：编辑/保存按钮 hidden 状态随 editing 翻转
    expect(w.find("#btn-strategy-edit").classes()).toContain("hidden");
    expect(w.find("#btn-strategy-save").classes()).not.toContain("hidden");
    w.unmount();
  });
});

describe("S2 · StrategyTab 分组调整（8 核心段 + ⚙️高级配置；backtest/reinvest/lake 不渲染）", () => {
  it("只渲染 2 组：📌策略核心（8 段）+ ⚙️高级配置（5 段，默认折叠）", async () => {
    const w = mk();
    await flushPromises();
    const groups = w.findAll(".s-group");
    expect(groups.length).toBe(2);
    expect(groups[0].text()).toContain("策略核心");
    expect(groups[1].text()).toContain("高级配置");
    const g0 = groups[0].text();
    for (const t of ["技术面", "股息率", "行业排名", "基本面", "股票池", "打分模型", "Badge 阈值", "硬性剔除"]) {
      expect(g0).toContain(t);
    }
    // backtest/reinvest/lake 段不在 StrategyTab（S2 移走/移除）
    const all = w.text();
    expect(all).not.toContain("历史回测");
    expect(all).not.toContain("再投资参考");
    expect(all).not.toContain("数据湖（v6）");
    // 高级配置组默认折叠（defaultOpen=false → v-show=false）
    expect(groups[1].find(".cards").isVisible()).toBe(false);
    w.unmount();
  });

  it("编辑态：两组强制展开（防漏改），核心段字段出现 FieldEditor number input", async () => {
    const w = mk();
    await flushPromises();
    await w.find("#btn-strategy-edit").trigger("click");
    const groups = w.findAll(".s-group");
    expect(groups[1].find(".cards").isVisible()).toBe(true); // 高级配置编辑态强制展开
    expect(w.findAll('input[type="number"]').length).toBeGreaterThan(0);
    w.unmount();
  });
});
