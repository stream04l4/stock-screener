// v6.1.5 前端用例（F1 legacy 源名中文映射 / F2 local 色对比度 / F4 T5 一键启动按钮）。
// mock 模式与 v614_frontend.test.js 同源（api client + useLiveQuery 打桩，零网络）。
import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { ref } from "vue";

// ---- mock api client（SourcePoolPanel O4 处理器惰性取 store；纯渲染不触发，但模块级 import 需存在）----
vi.mock("../../../api/client.js", () => ({
  api: async () => ({ ok: true }),
  isBackfillErr: () => false,
}));

// ---- mock useLiveQuery（SourcePoolPanel 顶层不直接用，但同目录组件可能引用；打桩防 import 副作用）----
vi.mock("../../../live/useLiveQuery.js", () => ({
  useLiveQuery: (keyOrGetter, fetcher) => {
    const data = ref(null);
    return { data, loading: ref(false), error: ref(null) };
  },
}));

// sourceMeta 纯模块（F1/F2 单一事实来源）
import {
  SOURCE_COLORS, UNKNOWN_COLOR, SOURCE_LABELS, LEGEND_ORDER,
  LEGACY_SOURCE_ALIASES, sourceMeta,
} from "../sourceMeta.js";
const SourcePoolPanel = (await import("../SourcePoolPanel.vue")).default;
const SyncControl = (await import("../SyncControl.vue")).default;
import { useLakeStore } from "../../../stores/lakeStore.js";

// ===========================================================================
// F1：legacy source 别名映射（sourceMeta 三级解析，不再裸显英文）
// ===========================================================================
describe("F1：sourceMeta legacy 源名中文映射", () => {
  it("当前活跃 6 源 → 固定色板 + 中文名（不回归 O5）", () => {
    expect(sourceMeta("sina")).toEqual({ color: "#2563eb", label: "新浪" });
    expect(sourceMeta("tencent").label).toBe("腾讯");
    expect(sourceMeta("baostock").label).toBe("BaoStock");
    expect(sourceMeta("tdx").label).toBe("通达信");
    expect(sourceMeta("adata_f10").label).toBe("adata");
    expect(sourceMeta("local")).toEqual({ color: "#64748b", label: "本地缓存" });
  });

  it("已知 legacy 别名 → 灰(UNKNOWN_COLOR) + 中文名（brief 点名 t/em_local_static/lake_factors）", () => {
    // brief 逐字：t→"腾讯(legacy)"、em_local_static→"本地缓存"；lake_factors=生产库实际值
    expect(sourceMeta("t")).toEqual({ color: UNKNOWN_COLOR, label: "腾讯(legacy)" });
    expect(sourceMeta("em_local_static")).toEqual({ color: UNKNOWN_COLOR, label: "本地缓存" });
    expect(sourceMeta("lake_factors")).toEqual({ color: UNKNOWN_COLOR, label: "本地因子" });
  });

  it("真正未知值 → 灰 + '其他·<原值>'（保留可追溯性，不猜、不裸显英文）", () => {
    const m = sourceMeta("mystery_src");
    expect(m.color).toBe(UNKNOWN_COLOR);
    expect(m.label).toBe("其他·mystery_src");   // 原值保留在标签里
  });

  it("LEGACY_SOURCE_ALIASES 覆盖生产库 DISTINCT source 全部实际值（F1 核对清单）", () => {
    // 生产库 read_only 查询实际出现值 = {baostock,em_local_static,lake_factors,sina,tencent}；
    // 当前活跃(sina/tencent/baostock)在 SOURCE_LABELS，legacy(em_local_static/lake_factors)在此表。
    for (const s of ["em_local_static", "lake_factors"]) {
      expect(LEGACY_SOURCE_ALIASES[s]).toBeTruthy();
    }
    // brief 点名的 t 也列入（生产库当前无此值，保留可追溯）
    expect(LEGACY_SOURCE_ALIASES["t"]).toBe("腾讯(legacy)");
  });
});

// ===========================================================================
// F2：local 色对比度 #94a3b8 → #64748b（slate-500，白底可读）
// ===========================================================================
describe("F2：SOURCE_COLORS.local 色值改 slate-500", () => {
  it("SOURCE_COLORS.local === '#64748b'（非旧值 #94a3b8）", () => {
    expect(SOURCE_COLORS.local).toBe("#64748b");
    expect(SOURCE_COLORS.local).not.toBe("#94a3b8");
  });

  it("sourceMeta('local') 颜色同源（图例/堆叠条/源卡片边框单点改）", () => {
    expect(sourceMeta("local").color).toBe("#64748b");
  });
});

// ===========================================================================
// F1+F2：SourcePoolPanel 堆叠条 title 中文 + local 色点（组件级集成）
// ===========================================================================
function makePool() {
  return {
    by_source: {
      kline_daily: { sina: 100, tencent: 50 },          // 当前活跃源
      dividend_events: { em_local_static: 30 },          // legacy → title "本地缓存:30"
      stock_master: { baostock: 20, t: 5 },              // t(legacy) → "腾讯(legacy):5"
      factor_snapshot: { lake_factors: 8, mystery: 2 },  // lake_factors + 真正未知
    },
    conflict_rows: { total: 0 },
    adapters: {},
    baostock_probe: null,
    stale: false,
    sources: [
      { name: "sina", enabled: true, available: true, probed_at: null, latency_ms: null },
      { name: "tencent", enabled: true, available: true, probed_at: null, latency_ms: null },
      { name: "baostock", enabled: true, available: null, probed_at: null, latency_ms: null },
      { name: "tdx", enabled: true, available: true, probed_at: null, latency_ms: null },
      { name: "adata_f10", enabled: true, available: true, probed_at: null, latency_ms: null },
    ],
  };
}

function mountSPP() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return mount(SourcePoolPanel, { props: { pool: makePool() }, global: { plugins: [pinia] } });
}

describe("F1：SourcePoolPanel 堆叠条 title 走 sourceMeta（legacy 中文 + 未知'其他·'）", () => {
  it("dividend_events(em_local_static) → title '本地缓存:30'（不裸显 em_local_static）", () => {
    const w = mountSPP();
    const row = w.findAll(".lake-spp-row").find((r) => r.text().includes("T4 分红事件"));
    expect(row.exists()).toBe(true);
    expect(row.find(".lake-spp-bar").attributes("title")).toBe("本地缓存:30");
    w.unmount();
  });

  it("stock_master(baostock+t) → title 'BaoStock:20 腾讯(legacy):5'（t legacy 中文）", () => {
    const w = mountSPP();
    const row = w.findAll(".lake-spp-row").find((r) => r.text().includes("T1 股票主档"));
    expect(row.find(".lake-spp-bar").attributes("title")).toBe("BaoStock:20 腾讯(legacy):5");
    w.unmount();
  });

  it("factor_snapshot(lake_factors+mystery) → '本地因子:8 其他·mystery:2'（未知值保留原值）", () => {
    const w = mountSPP();
    const row = w.findAll(".lake-spp-row").find((r) => r.text().includes("T8 因子快照"));
    expect(row.find(".lake-spp-bar").attributes("title")).toBe("本地因子:8 其他·mystery:2");
    w.unmount();
  });

  it("kline_daily(sina+tencent) → '新浪:100 腾讯:50'（当前活跃源不回归）", () => {
    const w = mountSPP();
    const row = w.findAll(".lake-spp-row").find((r) => r.text().includes("T2 日K线"));
    expect(row.find(".lake-spp-bar").attributes("title")).toBe("新浪:100 腾讯:50");
    w.unmount();
  });
});

describe("F2：SourcePoolPanel 图例 local 色点 = #64748b", () => {
  it("#lake-spp-legend local 项色点背景 #64748b（非旧值）", () => {
    const w = mountSPP();
    const items = w.findAll("#lake-spp-legend .lake-spp-legend-item");
    expect(items.length).toBe(6);
    // LEGEND_ORDER 末位 = local
    const localItem = items[items.length - 1];
    expect(localItem.text()).toBe("本地缓存");
    expect(localItem.find(".lake-spp-dot").attributes("style")).toContain("#64748b");
    w.unmount();
  });
});

// ===========================================================================
// F4：SyncControl 第三个按钮【▶ 启动基本面(T5)】（状态机复用，running 时三按钮全禁）
// ===========================================================================
function statusBody(over = {}) {
  return {
    installed: true, initialized: true, backfill_in_progress: false,
    lock_holder_pid: null, stopping: false, updated_at: "2026-09-17T08:00:00",
    tables: [], views: [], tasks: [], ...over,
  };
}

let pinia;
function mountSC() {
  const lake = useLakeStore();
  const w = mount(SyncControl, { global: { plugins: [pinia] } });
  return { lake, w };
}
async function tick() { await import("@vue/test-utils").then((m) => m.flushPromises()); }

beforeEach(() => { pinia = createPinia(); setActivePinia(pinia); });

describe("F4：SyncControl T5 按钮（第三个，状态机复用增量按钮）", () => {
  it("idle → 三按钮均可点（全史/增量/T5），T5 文案 '▶ 启动基本面(T5)'", async () => {
    const { lake, w } = mountSC();
    lake.status = statusBody();
    await tick();
    expect(w.find("#btn-lake-sync-toggle").text()).toBe("▶ 启动全史补库");
    expect(w.find("#btn-lake-sync-incremental").text()).toBe("▶ 启动增量同步");
    const t5 = w.find("#btn-lake-sync-t5");
    expect(t5.exists()).toBe(true);
    expect(t5.text()).toBe("▶ 启动基本面(T5)");
    expect(t5.attributes("disabled")).toBeUndefined();   // idle 可点
    w.unmount();
  });

  it("running → T5 按钮禁用（brief：running 时三按钮全禁）", async () => {
    const { lake, w } = mountSC();
    lake.status = statusBody({ backfill_in_progress: true, lock_holder_pid: 5 });
    await tick();
    expect(w.find("#btn-lake-sync-toggle").text()).toBe("⏹ 停止同步 (5)");
    expect(w.find("#btn-lake-sync-incremental").attributes("disabled")).toBeDefined();
    expect(w.find("#btn-lake-sync-t5").attributes("disabled")).toBeDefined();   // T5 也禁
    w.unmount();
  });

  it("starting/stopping/错误态 → T5 按钮禁用（锁定态三按钮全禁）", async () => {
    const { lake, w } = mountSC();
    lake.status = statusBody();
    await tick();
    expect(w.find("#btn-lake-sync-t5").attributes("disabled")).toBeUndefined();   // idle 可点
    lake.syncState = "starting";
    await tick();
    expect(w.find("#btn-lake-sync-t5").attributes("disabled")).toBeDefined();
    lake.syncState = "stopping";
    lake.stoppingSince = Date.now() - 1000;
    await tick();
    expect(w.find("#btn-lake-sync-t5").attributes("disabled")).toBeDefined();
    lake.syncState = "idle";
    lake.status = null;   // 错误态
    await tick();
    expect(w.find("#btn-lake-sync-t5").attributes("disabled")).toBeDefined();
    w.unmount();
  });

  it("!installed → T5 按钮禁用（数据湖未安装占位）", async () => {
    const { lake, w } = mountSC();
    lake.status = statusBody({ installed: false });
    await tick();
    expect(w.find("#btn-lake-sync-t5").attributes("disabled")).toBeDefined();
    w.unmount();
  });
});
