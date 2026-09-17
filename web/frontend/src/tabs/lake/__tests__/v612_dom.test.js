// v6.1.2 rendered-DOM 断言（P0 行业无重复 + P2-A 琥珀块总进度）。
// 与 v612_review_fixes.test.js（组件行为）互补：这里挂载**完整卡片/页签**断言渲染输出，
// 满足 brief ④ "DOM 断言（全景卡行业无重复、琥珀块总进度行）"——生产库 locked 期间无法
// 在 :9090 上对 ready 态做浏览器实测，故用真实组件 rendered DOM 作为等价证据。
import { describe, it, expect, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { ref } from "vue";

// --- mock echarts（StockPanoramaCard 的子组件 LakeKlineChart 会动态 import）---
vi.mock("echarts", () => ({ default: { init: () => ({ setOption() {}, resize() {}, dispose() {} }) } }));

// --- mock api client（零网络；/stock、/status 返回受控数据，其余空）---
const stockData = ref(null);   // /stock 受控
const statusData = ref(null);  // /status 受控（P2-A：经真实 store fetchStatus 流程驱动）
vi.mock("../../../api/client.js", () => ({
  api: async (url) => {
    const u = String(url);
    if (u.includes("/status")) return statusData.value || { installed: true, initialized: true };
    if (u.includes("/stock/")) return stockData.value;
    return { rows: [], count: 0 };
  },
  isBackfillErr: (e) => !!(e && e.status === 409 && e.body && e.body.error === "lake_backfill_in_progress"),
}));

// --- mock useLiveQuery：按 key 前缀返回受控数据（确定性，免异步时序）---
vi.mock("../../../live/useLiveQuery.js", () => ({
  useLiveQuery: (keyOrGetter) => {
    const key = typeof keyOrGetter === "function" ? keyOrGetter() : keyOrGetter;
    if (key && String(key).startsWith("lake:stock:")) {
      return { data: stockData, loading: ref(false), error: ref(null) };
    }
    return { data: ref({ rows: [], count: 0 }), loading: ref(false), error: ref(null) };
  },
}));

const StockPanoramaCard = (await import("../StockPanoramaCard.vue")).default;
const { useLakeStore } = await import("../../../stores/lakeStore.js");
const LakeTab = (await import("../../LakeTab.vue")).default;

describe("P0：个股全景卡行业无重复（rendered DOM）", () => {
  it("industry_csric2=C39 + industry_name='C39计算机…' → 行业格只显示 name（无 'C39 C39' 重复）", async () => {
    stockData.value = {
      ts_code: "sh.601398",
      base: { name: "工商银行", industry_csric2: "C39",
              industry_name: "C39计算机、通信和其他电子设备制造业", board: "主板", is_st: 0, soe_flag: "央国企" },
      fundamental_latest: null, holders_top10: [], factors: {}, factors_as_of: null,
      dividends_recent: [],
    };
    const w = mount(StockPanoramaCard, { props: { code: "sh.601398" } });
    await new Promise((r) => setTimeout(r, 0));
    const kvs = w.findAll(".lake-kv");
    const ind = kvs.find((kv) => kv.find(".k").text() === "行业");
    expect(ind.exists()).toBe(true);
    const vText = ind.find(".v").text();
    expect(vText).toBe("C39计算机、通信和其他电子设备制造业");
    expect(vText).not.toContain("C39 C39"), `行业格出现代码重复: ${vText}`;
  });

  it("industry_name 为空 → 行业格显示 '—'（不拼 csric2）", async () => {
    stockData.value = {
      ts_code: "sh.601398",
      base: { name: "工商银行", industry_csric2: "C39", industry_name: "", board: "主板", is_st: 0 },
      fundamental_latest: null, holders_top10: [], factors: {}, factors_as_of: null,
      dividends_recent: [],
    };
    const w = mount(StockPanoramaCard, { props: { code: "sh.601398" } });
    await new Promise((r) => setTimeout(r, 0));
    const kvs = w.findAll(".lake-kv");
    const ind = kvs.find((kv) => kv.find(".k").text() === "行业");
    expect(ind.find(".v").text()).toBe("—");
  });
});

describe("P2-A：琥珀块总进度行（rendered DOM）", () => {
  // LakeTab onActivated → lake.activate() → fetchStatus()（真实 store 流程）→
  // status/backfillView 由受控 /status mock 驱动（避免手动赋值被 activate 的 fetch 覆盖）。
  async function mountLakeWithStatus(status) {
    const pinia = createPinia();
    setActivePinia(pinia);
    statusData.value = status;
    const w = mount(LakeTab, { attachTo: document.body,
      global: { plugins: [pinia],
                stubs: { LakeSearchBar: true, StockPanoramaCard: true,
                         MarketTable: true, LakeStatusCard: true } } });
    await useLakeStore().fetchStatus();   // 等 activate() 触发的 fetch 落地
    await new Promise((r) => setTimeout(r, 0));
    return w;
  }

  it("backfill_in_progress → #lake-backfill-total 显示 done/total + 预计剩余（max eta_min→N小时M分）", async () => {
    const w = await mountLakeWithStatus({ installed: true, initialized: true, backfill_in_progress: true,
      lock_holder_pid: 3979536, updated_at: "2026-09-17 21:38:18",
      tasks: [{ table: "kline_history", tier: "P3", state: "running",
                done: 2276, total: 5219, eta_min: 277 }] });
    const total = w.find("#lake-backfill-total");
    expect(total.exists()).toBe(true);
    expect(total.text()).toContain("总进度");
    expect(total.text()).toContain("2276/5219");
    // eta_min=277 → h=4, mm=37 → "约 4 小时 37 分"
    expect(total.text()).toContain("约 4 小时 37 分");
    w.unmount();
  });

  it("无 eta（eta_min=null）→ 预计剩余显示 '—'", async () => {
    const w = await mountLakeWithStatus({ installed: true, initialized: true, backfill_in_progress: true,
      lock_holder_pid: 1, updated_at: "2026-09-17 21:38:18",
      tasks: [{ table: "kline_history", tier: "P3", state: "running",
                done: 10, total: 100, eta_min: null }] });
    const total = w.find("#lake-backfill-total");
    expect(total.exists()).toBe(true);
    expect(total.text()).toContain("预计剩余 —");
    w.unmount();
  });
});
