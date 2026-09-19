// v6.1.2 数据湖页复查修复 —— 前端组件行为回归（vitest）。
// 覆盖：
// - LakeKlineChart：复权三态按钮组（默认 qfq 高亮）+ 点击 emit adjust-change +
//   adjustNote 降级提示小字（P1-A）。
// - LakeStatusCard：tasks 表显隐（v6.1.4 O3：仅 backfill_in_progress=true 渲染——
//   取代 v6.1.2 P1-B"灌数中不渲染去冗余"；含'对应表'列）。v6.1.7：T6 holders_snapshot
//   接入 full 阶段 4 → 不再是 no_source，灰 badge"暂无数据源"分支移除（brief §B），
//   T6 行走真实任务态通用渲染（pending→"⏳ 待补"）。
// - MarketTable：跳页输入框 clamp[1,pages] + Go/回车触发 page 变化 → 重取（P3）。
import { describe, it, expect, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";

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

describe("LakeStatusCard：tasks 表显隐（v6.1.4 O3：仅 backfill_in_progress=true 显示）", () => {
  it("backfill_in_progress=true → #lake-tasks-table 渲染 + '对应表'列（O3；取代 v6.1.2 P1-B 去冗余规则）", () => {
    const w = mountStatus({
      installed: true, initialized: true, backfill_in_progress: true,
      lock_holder_pid: 12345, updated_at: "2026-09-17 21:00:00",
      tasks: [
        { table: "kline_history", tier: "P2", state: "running", done: 10, total: 100 },
        // v6.1.7：T6 holders_snapshot 接入 full 阶段 4 → 真实任务（tier=P2、pending、
        // total=universe）——不再是 no_source/total=0 占位。
        { table: "holders_snapshot", tier: "P2", state: "pending", done: 0, total: 5219 },
      ],
    });
    expect(w.find("#lake-tasks-table").exists()).toBe(true);
    // O3：'对应表'列（T1~T9 ↔ tasks 映射）
    const heads = w.findAll("#lake-tasks-table thead th").map((h) => h.text());
    expect(heads).toContain("对应表");
    const rows = w.findAll("#lake-tasks-table tbody tr");
    expect(rows.length).toBe(2);
    expect(rows[0].text()).toContain("= T2 全史（kline_daily）");
    // v6.1.7：T6 行走真实任务态通用渲染——pending → "⏳ 待补"，进度 0/5219；
    // 旧的 no_source 灰 badge"暂无数据源"不再出现（brief §B）。
    expect(rows[1].text()).toContain("T6 前十大股东");
    expect(rows[1].text()).toContain("⏳ 待补");
    expect(rows[1].text()).toContain("0/5219");
    expect(rows[1].text()).not.toContain("暂无数据源");
  });

  it("非灌数态（backfill_in_progress=false）→ #lake-tasks-table **整块隐藏**（O3：非同步态不显示补齐进度）", () => {
    const w = mountStatus({
      installed: true, initialized: true, backfill_in_progress: false,
      updated_at: "2026-09-17 21:00:00",
      tasks: [{ table: "kline_history", tier: "P2", state: "done", done: 100, total: 100 }],
    });
    expect(w.find("#lake-tasks-table").exists()).toBe(false);
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

// ---- D-1 回归守卫（v6.1.2 rework）：琥珀块总进度条必须是块级元素 ----
// 缺陷根因：P2-A 总进度行原用 <span class="progress-bar">（display:inline），CSS
// app.css .progress-bar{width:...} 对非替换 inline 元素不生效 → 渲染宽度恒 0，
// 用户看不到进度填充。tester Playwright 实测 rect_w=0 / fill_pct_of_track=0。
// 主仓自守（stages/ 的 tester 独立守卫不入库）：断言 #lake-backfill-total .progress-bar
// 是块级元素（tagName=DIV，或 style/display 显式 block/inline-block）。
// 注：api client 已在文件顶部 mock（marketMock，零网络）；此处 4 子组件全 stub，
// 且 onActivated 在无 <KeepAlive> 时不触发 → 直接置位 store.backfillView 渲染琥珀块。
const LakeTab = (await import("../../LakeTab.vue")).default;

function mountLakeTabWithBackfill(tasks) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useLakeStore();
  // backfillView 仅成功拉取且 backfill_in_progress=true 时非 null（store.fetchStatus 语义）；
  // 此处直接置位以渲染琥珀块（与真实 /status locked 态响应同形）。
  store.backfillView = {
    installed: true, initialized: true, backfill_in_progress: true, stopping: false,
    lock_holder_pid: 3979536, updated_at: "2026-09-18 00:00:00",
    coverage: {}, tasks,
  };
  return mount(LakeTab, {
    attachTo: document.body,   // getBoundingClientRect / CSS display 需真实挂载
    global: { plugins: [pinia],
              stubs: { LakeSearchBar: true, StockPanoramaCard: true,
                       MarketTable: true, LakeStatusCard: true } },
  });
}

describe("D-1 回归守卫：琥珀块总进度条必须是块级元素（v6.1.2 rework）", () => {
  it("#lake-backfill-total .progress-bar 为块级元素（tagName=DIV 或显式 block/inline-block），非 inline span", async () => {
    // done/total≈60%（3126/5219，与 tester 复现口径一致）
    const w = mountLakeTabWithBackfill([
      { table: "kline_history", tier: "P3", state: "running", done: 3126, total: 5219, eta_min: 480 },
    ]);
    await nextTick();
    const bar = w.find("#lake-backfill-total .progress-bar");
    expect(bar.exists()).toBe(true);
    // 核心断言：块级元素——tagName=DIV，或 style/display 显式 block/inline-block。
    // <span>（inline）两者皆不满足 → 回归即 FAIL。
    const tag = bar.element.tagName;
    const inlineStyle = (bar.attributes("style") || "").toLowerCase();
    const explicitBlock = /display\s*:\s*(block|inline-block)/.test(inlineStyle);
    expect(tag === "DIV" || explicitBlock).toBe(true);
    // 双保险：不得是 inline <span>（D-1 原缺陷形态）
    expect(tag).not.toBe("SPAN");
    // width 绑定生效（60% → 3126/5219*100=59.9→60），证明 :style 落在块级元素上
    expect(inlineStyle).toContain("width: 60%");
    w.unmount();
  });
});
