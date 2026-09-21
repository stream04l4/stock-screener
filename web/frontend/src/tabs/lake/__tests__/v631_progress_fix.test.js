// v6.3.1 R4 前端：tasks 状态中文映射（taskState）+ 琥珀块陈旧快照告警。
// 与 v612_dom.test.js 同 harness（mock api client 驱动真实 lakeStore.fetchStatus →
// backfillView → 渲染），断言 rendered DOM：
//   R4-a：badge 文字=中文映射、class=既有 .badge.* 四色（stopped_by_signal→idle 灰，
//         语义"已停止"不是失败；未知 state 回退 raw 原文 + 同名 class 不崩）。
//   R4-b：backfillView.updated_at（UTC）距今 >120s → 出现 "⚠ 进度更新已超时" 告警行；
//         <120s → 不出现（mock Date.now，确定性）。
import { describe, it, expect, vi, afterEach } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { ref } from "vue";

// --- mock echarts（StockPanoramaCard 子组件动态 import；本用例 stub 掉卡片）---
vi.mock("echarts", () => ({ default: { init: () => ({ setOption() {}, resize() {}, dispose() {} }) } }));

// --- mock api client（零网络；/status 受控驱动 backfillView）---
const statusData = ref(null);
vi.mock("../../../api/client.js", () => ({
  api: async (url) => {
    const u = String(url);
    if (u.includes("/status")) return statusData.value || { installed: true, initialized: true };
    if (u.includes("/stock/")) return null;
    return { rows: [], count: 0 };
  },
  isBackfillErr: (e) => !!(e && e.status === 409 && e.body && e.body.error === "lake_backfill_in_progress"),
}));

vi.mock("../../../live/useLiveQuery.js", () => ({
  useLiveQuery: (keyOrGetter) => {
    const key = typeof keyOrGetter === "function" ? keyOrGetter() : keyOrGetter;
    if (key && String(key).startsWith("lake:stock:")) return { data: ref(null), loading: ref(false), error: ref(null) };
    return { data: ref({ rows: [], count: 0 }), loading: ref(false), error: ref(null) };
  },
}));

const LakeTab = (await import("../../LakeTab.vue")).default;
const { useLakeStore } = await import("../../../stores/lakeStore.js");

// --- mock Date.now（陈旧告警的 120s 阈值判定）---
const FIXED_NOW = Date.parse("2026-09-21T06:44:19Z");   // Joel 截图时刻（UTC）
const NOW_S = new Date(FIXED_NOW).toISOString().replace("T", " ").slice(0, 19);
function mockNow(ms = FIXED_NOW) {
  vi.spyOn(Date, "now").mockReturnValue(ms);
}
afterEach(() => {
  vi.restoreAllMocks();
  statusData.value = null;
});

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

function backfillBlock(w) {
  return w.find("#lake-backfill");
}

describe("R4-a：tasks 状态中文映射（rendered DOM）", () => {
  it("state→[class, 中文文字]：running/done/error/stopped_by_signal/pending 全映射", async () => {
    const tasks = [
      { table: "kline_daily", tier: "P3", state: "running", done: 5, total: 5219, eta_min: 12 },
      { table: "valuation_daily", tier: "P3", state: "done", done: 5219, total: 5219, eta_min: null },
      { table: "holders_snapshot", tier: "P2", state: "error", done: 0, total: 5219,
        last_error: "phase t6 failed: ..." },
      { table: "factor_snapshot", tier: "P3", state: "stopped_by_signal", done: 0, total: 5219, eta_min: null },
      { table: "macro_rf", tier: "P3", state: "pending", done: 0, total: 1, eta_min: null },
    ];
    const w = await mountLakeWithStatus({ installed: true, initialized: true,
      backfill_in_progress: true, lock_holder_pid: 4153163,
      updated_at: NOW_S, tasks });
    const block = backfillBlock(w);
    expect(block.exists()).toBe(true);
    const rows = block.findAll("#lake-backfill-tasks tbody tr");
    expect(rows.length).toBe(5);

    const expectBadge = (i, text, cls) => {
      const badge = rows[i].find(".badge");
      expect(badge.exists()).toBe(true);
      expect(badge.text()).toBe(text), `row${i} badge 文字应为 ${text}: ${badge.text()}`;
      expect(badge.classes()).toContain(cls), `row${i} badge class 应含 ${cls}: ${badge.classes()}`;
    };
    expectBadge(0, "运行中", "running");
    expectBadge(1, "已完成", "done");
    expectBadge(2, "失败", "error");
    // stopped_by_signal → idle 灰色（"已停止"语义不是失败，不得用 error 红）
    expectBadge(3, "已停止", "idle");
    expectBadge(4, "待处理", "idle");
    w.unmount();
  });

  it("未知 state 回退 raw（class+文字同串，不崩）；无 state 回退 idle/空闲", async () => {
    const tasks = [
      { table: "kline_daily", tier: "P3", state: "weird_new_state", done: 1, total: 10, eta_min: null },
      { table: "index_daily", tier: "P3", done: 2, total: 4, eta_min: null },   // 无 state 键
    ];
    const w = await mountLakeWithStatus({ installed: true, initialized: true,
      backfill_in_progress: true, lock_holder_pid: 1,
      updated_at: NOW_S, tasks });
    const rows = backfillBlock(w).findAll("#lake-backfill-tasks tbody tr");
    expect(rows[0].find(".badge").text()).toBe("weird_new_state");
    expect(rows[0].find(".badge").classes()).toContain("weird_new_state");
    expect(rows[1].find(".badge").text()).toBe("空闲");
    expect(rows[1].find(".badge").classes()).toContain("idle");
    w.unmount();
  });

  it("stopping → 停止中（running 色）；blocked_quota → 配额到顶（running 色）", async () => {
    const tasks = [
      { table: "kline_history", tier: "P2", state: "stopping", done: 1, total: 2, eta_min: null },
      { table: "fundamentals_quarterly", tier: "P2", state: "blocked_quota", done: 0, total: 2, eta_min: null },
    ];
    const w = await mountLakeWithStatus({ installed: true, initialized: true,
      backfill_in_progress: true, lock_holder_pid: 1,
      updated_at: NOW_S, tasks });
    const rows = backfillBlock(w).findAll("#lake-backfill-tasks tbody tr");
    expect(rows[0].find(".badge").text()).toBe("停止中");
    expect(rows[0].find(".badge").classes()).toContain("running");
    expect(rows[1].find(".badge").text()).toBe("配额到顶");
    expect(rows[1].find(".badge").classes()).toContain("running");
    w.unmount();
  });
});

describe("R4-b：陈旧快照告警（updated_at 距今 >120s）", () => {
  it("updated_at 距今 194s（Joel 事故同因：快照冻结 06:41:05、截图 06:44:19 UTC）→ 出现告警", async () => {
    mockNow();
    const w = await mountLakeWithStatus({ installed: true, initialized: true,
      backfill_in_progress: true, lock_holder_pid: 4153163,
      // 06:41:05 UTC（= 北京 14:41:05，Joel 截图的冻结快照时刻）；
      // 距今 06:44:19 UTC（= 北京 14:44:19）= 194s > 120s → 陈旧。
      updated_at: "2026-09-21 06:41:05",
      tasks: [{ table: "kline_daily", tier: "P3", state: "running",
                done: 12654, total: 31319, eta_min: 416 }] });
    const block = backfillBlock(w);
    expect(block.text()).toContain("进度更新已超时");
    expect(block.text()).toContain("进程可能已退出");
    w.unmount();
  });

  it("updated_at 距今 30s（<120s，正常 3s 轮询新鲜）→ 不出现告警", async () => {
    mockNow();
    const fresh = new Date(FIXED_NOW - 30000).toISOString().replace("T", " ").slice(0, 19);
    const w = await mountLakeWithStatus({ installed: true, initialized: true,
      backfill_in_progress: true, lock_holder_pid: 4153163,
      updated_at: fresh,
      tasks: [{ table: "kline_daily", tier: "P3", state: "running",
                done: 12654, total: 31319, eta_min: 416 }] });
    const block = backfillBlock(w);
    expect(block.exists()).toBe(true);
    expect(block.text()).not.toContain("进度更新已超时");
    w.unmount();
  });

  it("updated_at 恰好 121s（边界 >120s）→ 出现；119s（<120s）→ 不出现", async () => {
    mockNow();
    const stale121 = new Date(FIXED_NOW - 121000).toISOString().replace("T", " ").slice(0, 19);
    const w1 = await mountLakeWithStatus({ installed: true, initialized: true,
      backfill_in_progress: true, lock_holder_pid: 1, updated_at: stale121, tasks: [] });
    expect(backfillBlock(w1).text()).toContain("进度更新已超时");
    w1.unmount();

    const fresh119 = new Date(FIXED_NOW - 119000).toISOString().replace("T", " ").slice(0, 19);
    const w2 = await mountLakeWithStatus({ installed: true, initialized: true,
      backfill_in_progress: true, lock_holder_pid: 1, updated_at: fresh119, tasks: [] });
    expect(backfillBlock(w2).exists()).toBe(true);
    expect(backfillBlock(w2).text()).not.toContain("进度更新已超时");
    w2.unmount();
  });

  it("updated_at 缺失/非法 → 不出现告警（不崩）", async () => {
    mockNow();
    const w = await mountLakeWithStatus({ installed: true, initialized: true,
      backfill_in_progress: true, lock_holder_pid: 1,
      updated_at: null, tasks: [] });
    expect(backfillBlock(w).exists()).toBe(true);
    expect(backfillBlock(w).text()).not.toContain("进度更新已超时");
    w.unmount();
  });
});
