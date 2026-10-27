// v7.0.18.3（tester DEF-6）：数据湖"复权因子覆盖"进度条可见性回归。
// 根因：填充元素曾用 <span>（display:inline，CSS width 无效）→ 布局 0×0 永不渲染；
// 修复：LakeStatusCard.vue 内 .progress-bar 改 <div>（与 LakeTab.vue 既有口径一致，
// 该文件注释早已警告此坑）。本测试钉死"块级元素 + width 内联生效"两条。
import { describe, it, expect, vi } from "vitest";
import { mount } from "@vue/test-utils";

vi.mock("../../../api/client.js", () => ({
  api: async () => ({ ok: true }),
  isBackfillErr: () => false,
}));
vi.mock("../../../live/useLiveQuery.js", () => ({
  useLiveQuery: () => ({ data: { value: null }, loading: { value: false }, error: { value: null } }),
}));
// lakeStore：最小 status 快照（afPct 唯一数据源 = adj_factor_coverage_pct）。
// 注意：af 进度条嵌在 <div v-if="views.length"> 内（视图区）→ views 必须非空。
vi.mock("../../../stores/lakeStore.js", () => ({
  useLakeStore: () => ({
    status: {
      installed: true, initialized: true, backfill_in_progress: false,
      tables: [], views: [{ key: "v_factor", name_cn: "因子视图", desc: "" }],
      adj_factor_coverage_pct: 100,
      duckdb_version: "1.5.5", updated_at: "2026-10-26T22:00:00",
    },
    syncState: "idle",
    refreshAll: () => {},
    sync: { start: () => {}, stop: () => {} },
  }),
}));

const LakeStatusCard = (await import("../LakeStatusCard.vue")).default;

function mountCard() {
  return mount(LakeStatusCard, {
    global: { stubs: { SyncControl: true, SourcePoolPanel: true } },
  });
}

describe("LakeStatusCard：af 覆盖进度条（DEF-6）", () => {
  it("填充元素是块级 <div>（<span> inline width 无效，永不渲染）", () => {
    const w = mountCard();
    const bar = w.find(".lake-af-track .progress-bar");
    expect(bar.exists()).toBe(true);
    expect(bar.element.tagName).toBe("DIV");
  });
  it("inline width 与 afPct 一致（100% 全填充）", () => {
    const w = mountCard();
    const bar = w.find(".lake-af-track .progress-bar");
    expect(bar.attributes("style")).toContain("width: 100%");
  });
});
