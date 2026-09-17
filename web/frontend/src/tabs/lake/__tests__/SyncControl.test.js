// SyncControl 单测（brief D3：v6.0.10 状态机**移植回归**——按钮态/文案与 vanilla 对齐）。
// 覆盖：idle→[▶ 启动同步] / running→[⏹ 停止同步 (pid)]+meta（配额/已耗时/进度更新）/
//       stopping=true 友好文案 / starting 锁定 / stopping 锁定（>90s 文案升级）/
//       错误态 status=null → "状态不可用"禁用 / !installed → "数据湖未安装"。
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { setActivePinia, createPinia } from "pinia";
import SyncControl from "../SyncControl.vue";
import { useLakeStore, STOP_SOFT_MS } from "../../../stores/lakeStore.js";

function statusBody(over = {}) {
  return {
    installed: true, initialized: true, backfill_in_progress: false,
    lock_holder_pid: null, stopping: false, updated_at: "2026-09-17T08:00:00",
    tables: [], views: [], tasks: [], ...over,
  };
}

// ⚠️ pinia 实例共享：mount() 不传全局 pinia 时组件会用 app 级默认 pinia（与
// setActivePinia 的测试实例不是同一个）→ store 改动对组件不可见。显式传同一实例。
let pinia;
function mountSC() {
  const lake = useLakeStore();
  const w = mount(SyncControl, { global: { plugins: [pinia] } });
  return { lake, w };
}

// store 改动 → 组件重渲染是响应式微任务 → 断言 DOM 前等一拍
async function tick() { await flushPromises(); }

beforeEach(() => { vi.useFakeTimers(); pinia = createPinia(); setActivePinia(pinia); });
afterEach(() => { vi.useRealTimers(); });

describe("SyncControl：按钮态（vanilla lakeRenderSyncControl 对齐）", () => {
  it("idle → [▶ 启动同步] 可点、meta 空", async () => {
    const { lake, w } = mountSC();
    lake.status = statusBody();
    await tick();
    expect(w.find("#btn-lake-sync-toggle").text()).toBe("▶ 启动同步");
    expect(w.find("#btn-lake-sync-toggle").attributes("disabled")).toBeUndefined();
    expect(w.find("#lake-sync-meta").text()).toBe("");
  });

  it("running → [⏹ 停止同步 (pid)] + meta 'PID x · 进度更新于 …'（会话未观察跃迁）", async () => {
    const { lake, w } = mountSC();
    lake.status = statusBody({ backfill_in_progress: true, lock_holder_pid: 4242 });
    await tick();
    const btn = w.find("#btn-lake-sync-toggle");
    expect(btn.text()).toBe("⏹ 停止同步 (4242)");
    expect(btn.attributes("disabled")).toBeUndefined();
    // runningSince=null（测试直接置 status，未走 fetchStatus 跃迁观察）→ 显示进度更新时间
    expect(w.find("#lake-sync-meta").text()).toContain("PID 4242");
    // vanilla 口径：String(updated_at).slice(5,16) → "09-17T08:00"（T 保留，与 app.js 一致）
    expect(w.find("#lake-sync-meta").text()).toContain("进度更新于 09-17T08:00");
  });

  it("running + 今日配额（tasks max）→ meta '今日配额 x/budget'", async () => {
    const { lake, w } = mountSC();
    lake.status = statusBody({
      backfill_in_progress: true, lock_holder_pid: 1,
      tasks: [{ quota_used_today: 300, quota_budget: 5000 }, { quota_used_today: 900, quota_budget: 5000 }],
    });
    await tick();
    expect(w.find("#lake-sync-meta").text()).toContain("今日配额 900/5000");   // max（防御滞后视图）
  });

  it("running + stopping=true（SIGTERM 已发）→ meta '⏹ 停止收尾中（当前任务完成后退出）'，按钮仍是停止入口", async () => {
    const { lake, w } = mountSC();
    lake.status = statusBody({ backfill_in_progress: true, lock_holder_pid: 7, stopping: true });
    await tick();
    expect(w.find("#btn-lake-sync-toggle").text()).toBe("⏹ 停止同步 (7)");
    expect(w.find("#lake-sync-meta").text()).toContain("⏹ 停止收尾中（当前任务完成后退出）");
  });

  it("starting → [▶ 启动中…] disabled + sync-busy + meta '正在启动（确认进程拉起中）'", async () => {
    const { lake, w } = mountSC();
    lake.syncState = "starting";
    await tick();
    const btn = w.find("#btn-lake-sync-toggle");
    expect(btn.text()).toBe("▶ 启动中…");
    expect(btn.attributes("disabled")).toBeDefined();
    expect(btn.classes()).toContain("sync-busy");
    expect(w.find("#lake-sync-meta").text()).toBe("正在启动（确认进程拉起中）");
  });

  it("stopping → [⏹ 停止中… (pid)] disabled + meta 'PID x · 已等待 …'", async () => {
    const { lake, w } = mountSC();
    lake.syncState = "stopping";
    lake.stoppingSince = Date.now() - 5000;   // 已等待 5s（<1分钟）
    lake.stoppingPid = 888;
    await tick();
    const btn = w.find("#btn-lake-sync-toggle");
    expect(btn.text()).toBe("⏹ 停止中… (888)");
    expect(btn.attributes("disabled")).toBeDefined();
    expect(w.find("#lake-sync-meta").text()).toContain("PID 888 · 已等待 <1分钟");
  });

  it("stopping >90s → 文案升级'（当前任务收尾中，最长约几分钟）'", async () => {
    const { lake, w } = mountSC();
    lake.syncState = "stopping";
    lake.stoppingSince = Date.now() - (STOP_SOFT_MS + 1000);
    lake.stoppingPid = null;   // pid 未知 → "(未知)"（<90s 分支）；>90s 分支不带 pid
    await tick();
    expect(w.find("#btn-lake-sync-toggle").text()).toBe("⏹ 停止中…（当前任务收尾中，最长约几分钟）");
  });

  it("错误态 status=null → [状态不可用] disabled（不白屏；锁定态除外见 store 测试）", async () => {
    const { lake, w } = mountSC();
    lake.status = null;   // 5xx/网络失败
    await tick();
    const btn = w.find("#btn-lake-sync-toggle");
    expect(btn.text()).toBe("状态不可用");
    expect(btn.attributes("disabled")).toBeDefined();
    expect(w.find("#lake-sync-meta").text()).toBe("");
  });

  it("!installed → [数据湖未安装] disabled", async () => {
    const { lake, w } = mountSC();
    lake.status = statusBody({ installed: false });
    await tick();
    const btn = w.find("#btn-lake-sync-toggle");
    expect(btn.text()).toBe("数据湖未安装");
    expect(btn.attributes("disabled")).toBeDefined();
  });

  it("runningSince 会话观察（fetchStatus 跃迁）→ meta '已耗时 …' 而非进度更新时间", async () => {
    const { lake, w } = mountSC();
    // 模拟 fetchStatus 的跃迁观察逻辑（store 内 nowBf && !runningSince → 记起点）
    lake.runningSince = Date.now() - 5 * 60000;   // 已运行 5 分钟
    lake.status = statusBody({ backfill_in_progress: true, lock_holder_pid: 3 });
    await tick();
    expect(w.find("#lake-sync-meta").text()).toContain("已耗时 5分钟");
    expect(w.find("#lake-sync-meta").text()).not.toContain("进度更新于");
  });
});
