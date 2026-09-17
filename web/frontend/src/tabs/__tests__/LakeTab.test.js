// LakeTab 组装回归（DEFECT-D3-1：红横幅前缀单一事实源）。
//
// 契约（vanilla lakeSetError parity）：lake.lastError 只存**裸 msg**，前缀
// "数据湖不可用：" **恰由 errMsg computed 加一次**。所有 lastError 入口一致：
//   - 子组件 @error（LakeSearchBar/StockPanoramaCard/MarketTable）→ 直传裸 m；
//   - fetchStatus catch → e.message（裸）；!installed → "duckdb 未安装…"（裸）。
// 任一入口再拼前缀都会产生双前缀（DEFECT-D3-1 repro：横幅 = 前缀×2）。
import { beforeEach, describe, expect, it, vi } from "vitest";
import { defineComponent, h, nextTick } from "vue";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

// mock 点：api client（零网络）+ queryRegistry.invalidate（隔离计数）。
const apiMock = vi.fn(async () => ({ installed: true, initialized: true, backfill_in_progress: false,
                                     tasks: [], tables: [], views: [] }));
vi.mock("../../api/client.js", () => ({
  api: (...a) => apiMock(...a),
  isBackfillErr: (e) => !!(e && e.status === 409 && e.body && e.body.error === "lake_backfill_in_progress"),
}));
vi.mock("../../live/queryRegistry.js", async (orig) => ({
  ...(await orig()), invalidate: vi.fn(),
}));

const LakeTab = (await import("../LakeTab.vue")).default;
const { useLakeStore } = await import("../../stores/lakeStore.js");

// 子组件 stub：按钮触发 error/select（与 tester B12 探针同款，可复用断言口径）。
const SearchStub = defineComponent({
  emits: ["error", "select"],
  setup: (_, { emit }) => () => h("div", { id: "search-stub" }, [
    h("button", { id: "emit-error", onClick: () => emit("error", "HTTP 500") }, "err"),
    h("button", { id: "emit-select", onClick: () => emit("select", "sh.600000") }, "sel"),
  ]),
});
const MarketStub = defineComponent({
  emits: ["select", "error"],
  setup: (_, { emit }) => () => h("div", { id: "market-stub" }, [
    h("button", { id: "market-select", onClick: () => emit("select", "sz.000001") }, "row"),
    h("button", { id: "market-error", onClick: () => emit("error", "HTTP 503") }, "merr"),
  ]),
});
const CardStub = defineComponent({
  props: { code: { type: String, default: "" } },
  setup: (p) => () => h("div", { id: "lake-stock-card" }, "card:" + p.code),
});

const mk = () => mount(LakeTab, {
  attachTo: document.body,   // scrollIntoView 走 document.getElementById → 必须真实挂载
  global: { plugins: [createPinia()],
            stubs: { LakeSearchBar: SearchStub, MarketTable: MarketStub,
                     StockPanoramaCard: CardStub, LakeStatusCard: true } },
});

const PREFIX_COUNT = (text) => (text.match(/数据湖不可用：/g) || []).length;

beforeEach(() => {
  apiMock.mockClear();
  apiMock.mockImplementation(async () => ({ installed: true, initialized: true,
    backfill_in_progress: false, tasks: [], tables: [], views: [] }));
});

describe("DEFECT-D3-1 红横幅前缀恰一次（vanilla lakeSetError parity）", () => {
  it("子组件 error → 横幅 = '数据湖不可用：HTTP 500'（前缀恰一次，非 ×2）", async () => {
    setActivePinia(createPinia());
    const w = mk();
    await w.find("#emit-error").trigger("click");
    await nextTick();
    const banner = w.find("#lake-error");
    expect(banner.exists()).toBe(true);
    // lastError 只存裸 msg（前缀由 errMsg computed 统一加）
    expect(useLakeStore().lastError).toBe("HTTP 500");
    const text = banner.text();
    expect(PREFIX_COUNT(text)).toBe(1);
    expect(text).toBe("数据湖不可用：HTTP 500");
  });

  it("MarketTable error → 同样单前缀（三入口一致）", async () => {
    setActivePinia(createPinia());
    const w = mk();
    await w.find("#market-error").trigger("click");
    await nextTick();
    const text = w.find("#lake-error").text();
    expect(PREFIX_COUNT(text)).toBe(1);
    expect(text).toBe("数据湖不可用：HTTP 503");
  });

  it("fetchStatus 网络错误入口（e.message 裸值）→ 单前缀", async () => {
    setActivePinia(createPinia());
    const w = mk();
    apiMock.mockRejectedValueOnce(new Error("HTTP 502"));
    await useLakeStore().fetchStatus();   // 同 pinia（mount 后 activePinia=app pinia）
    await nextTick();
    const text = w.find("#lake-error").text();
    expect(PREFIX_COUNT(text)).toBe(1);
    expect(text).toBe("数据湖不可用：HTTP 502");
  });

  it("!installed 入口（duckdb 未安装裸文案）→ 单前缀", async () => {
    setActivePinia(createPinia());
    const w = mk();
    apiMock.mockResolvedValueOnce({ installed: false, initialized: false,
      backfill_in_progress: false, tasks: [], tables: [], views: [] });
    await useLakeStore().fetchStatus();
    await nextTick();
    const text = w.find("#lake-error").text();
    expect(PREFIX_COUNT(text)).toBe(1);
    expect(text).toBe("数据湖不可用：duckdb 未安装（uv sync --extra lake）");
  });

  it("错误清除：轮询成功一次后横幅消失", async () => {
    setActivePinia(createPinia());
    const w = mk();
    await w.find("#emit-error").trigger("click");
    await nextTick();
    expect(w.find("#lake-error").exists()).toBe(true);
    await useLakeStore().fetchStatus();   // 成功拉取 → lastError=""
    await nextTick();
    expect(w.find("#lake-error").exists()).toBe(false);
  });
});
