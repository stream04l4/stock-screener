// v6.1.4 前端用例（O1 表头点击排序 / O4 源勾选+探测按钮 / O5 图例+中文 tooltip）。
// mock 模式与 v612_review_fixes.test.js 同源（api client + useLiveQuery 打桩，零网络）。
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { ref } from "vue";

// ---- mock api client（记录调用；market 返回受控分页数据）----
const marketCalls = [];
const postCalls = [];   // O4：POST /sources/toggle|probe
let toggleRes = { ok: true };
let apiFailWith = null;   // 非 null → api() 抛该 Error（O4 失败分支用例）
vi.mock("../../../api/client.js", () => ({
  api: async (url, opts) => {
    if (apiFailWith) throw new Error(apiFailWith);
    const u = String(url);
    if (u.startsWith("/api/lake/market")) { marketCalls.push(u); return { rows: [], total: 0, page: 1, page_size: 20, pages: 1 }; }
    if (u.startsWith("/api/lake/industries")) return { industries: [] };
    if (u.startsWith("/api/lake/sources/toggle")) { postCalls.push({ url: u, body: JSON.parse(opts.body) }); return toggleRes; }
    if (u.startsWith("/api/lake/sources/probe")) { postCalls.push({ url: u, body: JSON.parse(opts.body) }); return { results: {}, path: null }; }
    return { rows: [], count: 0 };
  },
  isBackfillErr: () => false,
}));

// ---- mock useLiveQuery：**调用 fetcher**（真实触发 api() → 记录 URL，断言 key 拼装）----
vi.mock("../../../live/useLiveQuery.js", () => ({
  useLiveQuery: (keyOrGetter, fetcher) => {
    const data = ref(null);
    const loading = ref(false);
    const error = ref(null);
    // 模拟真实 useLiveQuery：立即执行一次 fetcher（异步，fire-and-forget）
    Promise.resolve().then(async () => {
      try { data.value = await fetcher(); } catch (e) { error.value = e; }
    });
    return { data, loading, error };
  },
}));

const MarketTable = (await import("../MarketTable.vue")).default;
const SourcePoolPanel = (await import("../SourcePoolPanel.vue")).default;

beforeEach(() => { marketCalls.length = 0; postCalls.length = 0; toggleRes = { ok: true }; apiFailWith = null; });

describe("O1：MarketTable 表头可点排序（▲▼指示，再点切方向）", () => {
  async function mountMT() {
    const w = mount(MarketTable, { attachTo: document.body });
    await new Promise((r) => setTimeout(r, 0));
    return w;
  }

  it("初始 sort=code（后端默认列）→ 代码表头 active + ▼；首次请求带 sort=code&order=desc", async () => {
    const w = await mountMT();
    const ths = w.findAll("#lake-market-table thead th");
    const codeTh = ths.find((t) => t.text().startsWith("代码"));
    expect(codeTh.classes()).toContain("lake-sort-active");
    expect(codeTh.text()).toContain("▼");
    expect(marketCalls[0]).toContain("sort=code&order=desc");
    w.unmount();
  });

  it("点 PE 表头 → sort=pe_ttm（新列=desc 起）+ active 迁移 + 回第 1 页", async () => {
    const w = await mountMT();
    const peTh = w.findAll("#lake-market-table thead th").find((t) => t.text().startsWith("PE"));
    expect(peTh.attributes("class")).toContain("lake-sort-th");
    await peTh.trigger("click");
    expect(peTh.classes()).toContain("lake-sort-active");
    expect(peTh.text()).toContain("▼");
    // 旧列（代码）不再 active
    const codeTh = w.findAll("#lake-market-table thead th").find((t) => t.text().startsWith("代码"));
    expect(codeTh.classes()).not.toContain("lake-sort-active");
    w.unmount();
  });

  it("再点同列 → 方向切换 desc→asc（▲）", async () => {
    const w = await mountMT();
    const peTh = w.findAll("#lake-market-table thead th").find((t) => t.text().startsWith("PE"));
    await peTh.trigger("click");   // → pe_ttm desc
    expect(peTh.text()).toContain("▼");
    await peTh.trigger("click");   // 再点 → asc
    expect(peTh.text()).toContain("▲");
    w.unmount();
  });

  it("总市值(亿)/股息率% 列**不可排**（白名单不含 total_mv/ttm_yield_pct——无 .lake-sort-th）", async () => {
    const w = await mountMT();
    const ths = w.findAll("#lake-market-table thead th");
    const mvTh = ths.find((t) => t.text().includes("总市值"));
    const dyTh = ths.find((t) => t.text().includes("股息率"));
    expect(mvTh.attributes("class")).not.toContain("lake-sort-th");
    expect(dyTh.attributes("class")).not.toContain("lake-sort-th");
    // 可排列 = 代码/名称/行业/PE/PB（5 个）
    const sortable = ths.filter((t) => (t.attributes("class") || "").includes("lake-sort-th"));
    expect(sortable.length).toBe(5);
    w.unmount();
  });
});

// ===========================================================================
// O4/O5：SourcePoolPanel（图例 + 中文 tooltip + 勾选 + 探测按钮）
// ===========================================================================
function makePool() {
  const by_source = {};
  for (const t of ["stock_master", "kline_daily", "valuation_daily", "dividend_events",
    "fundamentals_quarterly", "holders_snapshot", "index_daily", "factor_snapshot", "macro_rf"]) {
    by_source[t] = {};
  }
  by_source.kline_daily = { sina: 12345, tencent: 678 };   // O5 tooltip 断言值
  by_source.stock_master = { local: 5400 };
  return {
    by_source,
    conflict_rows: { total: 0 },
    adapters: {},
    baostock_probe: null,
    stale: false,
    sources: [
      { name: "sina", enabled: true, available: true, probed_at: "2026-09-17T08:00:00", latency_ms: 320 },
      { name: "tencent", enabled: true, available: false, probed_at: null, latency_ms: null },
      { name: "baostock", enabled: true, available: null, probed_at: null, latency_ms: null },
      { name: "tdx", enabled: false, available: true, probed_at: null, latency_ms: null },
      { name: "adata_f10", enabled: true, available: true, probed_at: null, latency_ms: null },
    ],
  };
}

function mountSPP() {
  const pinia = createPinia();
  setActivePinia(pinia);   // O4 处理器惰性取 store——交互用例需 pinia（纯渲染不触发）
  return mount(SourcePoolPanel, { props: { pool: makePool() }, global: { plugins: [pinia] } });
}

describe("O5：SourcePoolPanel 固定色板图例行 + 中文悬停提示", () => {
  it("#lake-spp-legend 6 项（sina 新浪 / tencent 腾讯 / baostock BaoStock / tdx 通达信 / adata_f10 adata / local 本地缓存）+ 色点同源 SOURCE_COLORS", () => {
    const w = mountSPP();
    const items = w.findAll("#lake-spp-legend .lake-spp-legend-item");
    expect(items.length).toBe(6);
    const texts = items.map((i) => i.text());
    expect(texts).toEqual(["新浪", "腾讯", "BaoStock", "通达信", "adata", "本地缓存"]);
    // 色点颜色与 sourceMeta 同源（sina 蓝 #2563eb / tdx 青 #06b6d4）
    expect(items[0].find(".lake-spp-dot").attributes("style")).toContain("#2563eb");
    expect(items[3].find(".lake-spp-dot").attributes("style")).toContain("#06b6d4");
    w.unmount();
  });

  it("堆叠条 title 改中文源名+数量（'新浪:12345 腾讯:678'；原 'sina:12345 …'）", () => {
    const w = mountSPP();
    const row = w.findAll(".lake-spp-row").find((r) => r.text().includes("T2 日K线"));
    const bar = row.find(".lake-spp-bar");
    expect(bar.attributes("title")).toBe("新浪:12345 腾讯:678");
    w.unmount();
  });

  it("源卡片左边框/标题色与图例同源（sina #2563eb；O4/O5 防漂移——sourceMeta.js 单一事实来源）", () => {
    const w = mountSPP();
    const cards = w.findAll(".lake-spp-adapter");
    expect(cards[0].attributes("style")).toContain("#2563eb");   // sina 卡左边框
    expect(cards[0].find("b").attributes("style")).toContain("#2563eb");   // sina 标题色
    w.unmount();
  });
});

describe("O4：SourcePoolPanel 源勾选 + 手动探测按钮", () => {
  it("每源卡有 enabled checkbox（:id=src-enable-<name>）+【探测】按钮；区块标题有【全部探测】", () => {
    const w = mountSPP();
    for (const n of ["sina", "tencent", "baostock", "tdx", "adata_f10"]) {
      expect(w.find(`#src-enable-${n}`).exists()).toBe(true);
    }
    expect(w.findAll(".lake-spp-probe").length).toBe(5);
    const allBtn = w.find("#btn-lake-probe-all");
    expect(allBtn.exists()).toBe(true);
    expect(allBtn.text()).toBe("全部探测");
    // tdx enabled=false → checkbox 未勾选
    expect(w.find("#src-enable-tdx").element.checked).toBe(false);
    expect(w.find("#src-enable-sina").element.checked).toBe(true);
    w.unmount();
  });

  it("取消勾选 sina → POST /sources/toggle {name:'sina',enabled:false}；成功→toast+重拉（fetchStatus 被调）", async () => {
    const w = mountSPP();
    const cb = w.find("#src-enable-sina");
    await cb.setChecked(false);
    await flushPromises();
    expect(postCalls.length).toBe(1);
    expect(postCalls[0].url).toBe("/api/lake/sources/toggle");
    expect(postCalls[0].body).toEqual({ name: "sina", enabled: false });
    w.unmount();
  });

  it("toggle 失败（5xx）→ checkbox 回滚 + 错误 toast（不静默吞错）", async () => {
    apiFailWith = "Internal Server Error";   // mock api() 抛错（模拟 5xx → api client throw）
    const w = mountSPP();
    const cb = w.find("#src-enable-sina");
    await cb.setChecked(false);
    await flushPromises();
    // 回滚：DOM checked 复位为 true（api 抛错 → onToggle catch 分支 ev.target.checked=!enabled）
    expect(cb.element.checked).toBe(true);
    apiFailWith = null;
    w.unmount();
  });

  it("点【探测】(sina) → POST /sources/probe {names:['sina']}；按钮忙态'…'", async () => {
    const w = mountSPP();
    const btn = w.findAll(".lake-spp-probe")[0];   // sina 卡（固定 5 源顺序）
    await btn.trigger("click");
    await flushPromises();
    expect(postCalls.length).toBe(1);
    expect(postCalls[0].url).toBe("/api/lake/sources/probe");
    expect(postCalls[0].body).toEqual({ names: ["sina"] });
    w.unmount();
  });

  it("点【全部探测】→ POST /sources/probe {names:[5 源全列]}", async () => {
    const w = mountSPP();
    await w.find("#btn-lake-probe-all").trigger("click");
    await flushPromises();
    expect(postCalls.length).toBe(1);
    expect(postCalls[0].body.names).toEqual(["sina", "tencent", "baostock", "tdx", "adata_f10"]);
    w.unmount();
  });
});
