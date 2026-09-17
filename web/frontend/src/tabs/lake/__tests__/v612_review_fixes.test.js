// v6.1.2 数据湖页复查修复 —— 前端组件行为回归（vitest）。
// 覆盖：
// - LakeKlineChart：复权三态按钮组（默认 qfq 高亮）+ 点击 emit adjust-change +
//   adjustNote 降级提示小字（P1-A）。
// - LakeStatusCard：backfill_in_progress=true → #lake-tasks-table 不渲染（去双份冗余，
//   P1-B）；非灌数态照常渲染。
// - MarketTable：跳页输入框 clamp[1,pages] + Go/回车触发 page 变化 → 重取（P3）。
import { describe, it, expect, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

// ---- LakeKlineChart（纯 props，无 store；echarts 动态 import 需 mock 防 happy-dom 无 canvas）----
vi.mock("echarts", () => ({
  default: { init: () => ({ setOption: () => {}, resize: () => {}, dispose: () => {} }) },
}));
const LakeKlineChart = (await import("../LakeKlineChart.vue")).default;

describe("LakeKlineChart：复权三态切换（v6.1.2 P1-A）", () => {
  it("渲染 3 个复权按钮（原始/前复权(qfq)/后复权(hfq)），默认 qfq 高亮 cur", () => {
    const w = mount(LakeKlineChart, { props: { rows: [], tsCode: "sh.601398", range: "250" } });
    const btns = ["none", "qfq", "hfq"].map((k) => w.find(`#lake-adj-${k}`));
    for (const b of btns) expect(b.exists()).toBe(true);
    // 默认 adjust=qfq → qfq 按钮 cur，其余非 cur
    expect(btns[1].classes()).toContain("cur");
    expect(btns[0].classes()).not.toContain("cur");
    expect(btns[2].classes()).not.toContain("cur");
    expect(btns[1].text()).toBe("前复权(qfq)");
  });

  it("点击 hfq → emit adjust-change('hfq')；父组件回传新态后再点当前态不重复 emit", async () => {
    const w = mount(LakeKlineChart, { props: { rows: [], tsCode: "sh.601398", range: "250" } });
    await w.find("#lake-adj-hfq").trigger("click");
    expect(w.emitted("adjust-change")).toEqual([["hfq"]]);
    // 父组件据 emit 更新 adjust=hfq → prop 回传；再点 hfq（已是当前态）→ 不重复 emit
    await w.setProps({ adjust: "hfq" });
    await w.find("#lake-adj-hfq").trigger("click");
    expect(w.emitted("adjust-change")).toEqual([["hfq"]]);
  });

  it("adjustNote 非空 → 图表上方渲染降级提示小字；空 → 不渲染", () => {
    const withNote = mount(LakeKlineChart, {
      props: { rows: [], tsCode: "sh.601398", range: "250", adjustNote: "复权因子补齐中，暂显示原始价" },
    });
    const note = withNote.find(".lake-kline-adjust-note");
    expect(note.exists()).toBe(true);
    expect(note.text()).toBe("复权因子补齐中，暂显示原始价");

    const noNote = mount(LakeKlineChart, { props: { rows: [], tsCode: "sh.601398", range: "250" } });
    expect(noNote.find(".lake-kline-adjust-note").exists()).toBe(false);
  });
});

// ---- LakeStatusCard（pinia lakeStore；mock SyncControl/SourcePoolPanel 子组件）----
const { useLakeStore } = await import("../../../stores/lakeStore.js");
const LakeStatusCard = (await import("../LakeStatusCard.vue")).default;

function mountStatus(status) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useLakeStore();
  store.status = status;
  return mount(LakeStatusCard, {
    global: { plugins: [pinia], stubs: { SyncControl: true, SourcePoolPanel: true } },
  });
}

describe("LakeStatusCard：tasks 表灌数中不渲染（v6.1.2 P1-B）", () => {
  it("backfill_in_progress=true → #lake-tasks-table 不渲染（琥珀块已展示同一份 tasks）", () => {
    const w = mountStatus({
      installed: true, initialized: true, backfill_in_progress: true,
      lock_holder_pid: 12345, updated_at: "2026-09-17 21:00:00",
      tasks: [{ table: "kline_history", tier: "P3", state: "running", done: 10, total: 100 }],
    });
    expect(w.find("#lake-tasks-table").exists()).toBe(false);
  });

  it("非灌数态（backfill_in_progress=false）→ #lake-tasks-table 照常渲染", () => {
    const w = mountStatus({
      installed: true, initialized: true, backfill_in_progress: false,
      updated_at: "2026-09-17 21:00:00",
      tasks: [{ table: "kline_history", tier: "P3", state: "done", done: 100, total: 100 }],
    });
    expect(w.find("#lake-tasks-table").exists()).toBe(true);
    // tasks 行渲染
    expect(w.findAll("#lake-tasks-table tbody tr").length).toBe(1);
  });
});

// ---- MarketTable（mock api + queryRegistry；跳页 clamp）----
const marketMock = vi.fn(async () => ({
  rows: [{ ts_code: "sh.600000", name: "浦发银行" }], total: 278, page: 1, page_size: 20, pages: 14,
}));
vi.mock("../../../api/client.js", () => ({
  api: (...a) => marketMock(...a),
  isBackfillErr: (e) => !!(e && e.status === 409 && e.body && e.body.error === "lake_backfill_in_progress"),
}));
const MarketTable = (await import("../MarketTable.vue")).default;

describe("MarketTable：跳页输入框（v6.1.2 P3）", () => {
  it("渲染 #lake-market-jump + Go 按钮；初始 page=1", async () => {
    const w = mount(MarketTable, { attachTo: document.body });
    await new Promise((r) => setTimeout(r, 0));   // 等 useLiveQuery 首次 fetch
    expect(w.find("#lake-market-jump").exists()).toBe(true);
    expect(w.find("#btn-lake-market-jump").exists()).toBe(true);
    w.unmount();
  });

  it("跳页 clamp：输入 99（>pages=14）→ page=14；输入 0 → page=1", async () => {
    const w = mount(MarketTable, { attachTo: document.body });
    await new Promise((r) => setTimeout(r, 0));
    // 跳到最后页边界（clamp upper）
    await w.find("#lake-market-jump").setValue("99");
    await w.find("#btn-lake-market-jump").trigger("click");
    await new Promise((r) => setTimeout(r, 0));
    expect(marketMock.mock.calls.some((c) => String(c[0]).includes("page=14"))).toBe(true);
    // 跳到下边界（clamp lower）
    await w.find("#lake-market-jump").setValue("0");
    await w.find("#btn-lake-market-jump").trigger("click");
    await new Promise((r) => setTimeout(r, 0));
    expect(marketMock.mock.calls.some((c) => String(c[0]).includes("page=1"))).toBe(true);
    w.unmount();
  });

  it("非数字输入 → 不触发重取（不猜、不清空）", async () => {
    const w = mount(MarketTable, { attachTo: document.body });
    await new Promise((r) => setTimeout(r, 0));
    const before = marketMock.mock.calls.length;
    await w.find("#lake-market-jump").setValue("abc");
    await w.find("#btn-lake-market-jump").trigger("click");
    await new Promise((r) => setTimeout(r, 0));
    expect(marketMock.mock.calls.length).toBe(before);   // 无新请求
    w.unmount();
  });
});
