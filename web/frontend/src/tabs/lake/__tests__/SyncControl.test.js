// SyncControl 单测（v6.1.6：三按钮合并为**单 toggle**——idle【▶ 启动数据补齐】/
// running【■ 停止数据补齐】(danger)；状态机 v6.0.10 移植回归保留）。
// 覆盖：idle 单按钮可点 / running 停止按钮+danger 样式 / starting/stopping 锁定态 /
//       错误态 status=null → "状态不可用"禁用 / !installed → "数据湖未安装" /
//       **phase 小字**（#lake-sync-phase："正在灌：T2 全史/T3 估值增量/T5 基本面"，
//       仅 running+status.phase 存在时渲染；无 phase 键不渲染——三态契约）。
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

describe("SyncControl v6.1.6：单按钮 toggle（三按钮合并）", () => {
  it("idle → 唯一按钮 [▶ 启动数据补齐] 可点；旧三按钮消失；phase 小字不渲染", async () => {
    const { lake, w } = mountSC();
    lake.status = statusBody();
    await tick();
    // v6.1.6：单 toggle 按钮（idle 态）
    expect(w.find("#btn-lake-sync-toggle").text()).toBe("▶ 启动数据补齐");
    expect(w.find("#btn-lake-sync-toggle").attributes("disabled")).toBeUndefined();
    expect(w.find("#btn-lake-sync-toggle").classes()).not.toContain("sync-danger");
    // v6.1.6：旧三按钮合并——增量/T5 按钮 DOM 消失（brief"以删除为主"）
    expect(w.find("#btn-lake-sync-incremental").exists()).toBe(false);
    expect(w.find("#btn-lake-sync-t5").exists()).toBe(false);
    // 页面上只剩一个同步按钮
    expect(w.findAll("button").length).toBe(1);
    // idle（未 running）→ phase 小字不渲染（v-if=false → 元素不存在）
    expect(w.find("#lake-sync-phase").exists()).toBe(false);
    expect(w.find("#lake-sync-meta").text()).toBe("");
  });

  it("running → [■ 停止数据补齐] + sync-danger 红色危险样式", async () => {
    const { lake, w } = mountSC();
    lake.status = statusBody({ backfill_in_progress: true, lock_holder_pid: 5 });
    await tick();
    const btn = w.find("#btn-lake-sync-toggle");
    expect(btn.text()).toBe("■ 停止数据补齐");
    expect(btn.attributes("disabled")).toBeUndefined();
    // v6.1.6：running 态红色危险样式（--bad 色板）
    expect(btn.classes()).toContain("sync-danger");
    // meta 仍含 PID（v6.0.10 状态机不变）
    expect(w.find("#lake-sync-meta").text()).toContain("PID 5");
  });

  it("running + phase=history → 小字 '正在灌：T2 全史'；p3/t5 同理", async () => {
    const { lake, w } = mountSC();
    lake.status = statusBody({ backfill_in_progress: true, lock_holder_pid: 9, phase: "history" });
    await tick();
    expect(w.find("#lake-sync-phase").text()).toBe("正在灌：T2 全史");

    lake.status = statusBody({ backfill_in_progress: true, lock_holder_pid: 9, phase: "p3" });
    await tick();
    expect(w.find("#lake-sync-phase").text()).toBe("正在灌：T3 估值增量");

    lake.status = statusBody({ backfill_in_progress: true, lock_holder_pid: 9, phase: "t5" });
    await tick();
    expect(w.find("#lake-sync-phase").text()).toBe("正在灌：T5 基本面");
  });

  it("running 但无 phase 键（非 full 进程/全结束）→ 小字不渲染（三态契约其余字段不动）", async () => {
    const { lake, w } = mountSC();
    lake.status = statusBody({ backfill_in_progress: true, lock_holder_pid: 9 });
    await tick();
    expect(w.find("#lake-sync-phase").exists()).toBe(false);
    // 其余 running 展示不受影响
    expect(w.find("#btn-lake-sync-toggle").text()).toBe("■ 停止数据补齐");
  });

  it("starting → [▶ 启动中…] disabled + sync-busy；stopping → [⏹ 停止中… (pid)] disabled", async () => {
    const { lake, w } = mountSC();
    lake.status = statusBody();
    await tick();
    expect(w.find("#btn-lake-sync-toggle").attributes("disabled")).toBeUndefined();

    lake.syncState = "starting";
    await tick();
    let btn = w.find("#btn-lake-sync-toggle");
    expect(btn.text()).toBe("▶ 启动中…");
    expect(btn.attributes("disabled")).toBeDefined();
    expect(btn.classes()).toContain("sync-busy");
    expect(w.find("#lake-sync-meta").text()).toBe("正在启动（确认进程拉起中）");

    lake.syncState = "stopping";
    lake.stoppingSince = Date.now() - 5000;   // 已等待 5s（<1分钟）
    lake.stoppingPid = 888;
    await tick();
    btn = w.find("#btn-lake-sync-toggle");
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

  it("running + stopping=true → meta '⏹ 停止收尾中（当前任务完成后退出）'，按钮仍是停止入口", async () => {
    const { lake, w } = mountSC();
    lake.status = statusBody({ backfill_in_progress: true, lock_holder_pid: 7, stopping: true });
    await tick();
    expect(w.find("#btn-lake-sync-toggle").text()).toBe("■ 停止数据补齐");
    expect(w.find("#lake-sync-meta").text()).toContain("⏹ 停止收尾中（当前任务完成后退出）");
  });

  it("running + tasks 含 quota → meta **不含**'今日配额'（v6.1.3：配额归数据源状态卡）", async () => {
    const { lake, w } = mountSC();
    lake.status = statusBody({
      backfill_in_progress: true, lock_holder_pid: 1,
      tasks: [{ quota_used_today: 300, quota_budget: 5000 }, { quota_used_today: 900, quota_budget: 5000 }],
    });
    await tick();
    const meta = w.find("#lake-sync-meta").text();
    expect(meta).not.toContain("今日配额");
    expect(meta).toContain("PID 1");
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

  it("点击 idle 按钮 → store.startSync() 无参调用（=full 全量）", async () => {
    const { lake, w } = mountSC();
    lake.status = statusBody();
    await tick();
    // happy-dom 无 window.confirm → 直接赋值 stub（afterEach 恢复由 vitest 环境隔离）
    globalThis.window.confirm = () => true;
    let startedWith;
    const origStart = lake.startSync;
    lake.startSync = function (...args) { startedWith = args; return Promise.resolve(); };
    await w.find("#btn-lake-sync-toggle").trigger("click");
    expect(startedWith).toEqual([]);   // v6.1.6：startSync 无参（后端缺省=full）
    lake.startSync = origStart;
  });

  it("点击 running 按钮 → store.stopSync() 被调用", async () => {
    const { lake, w } = mountSC();
    lake.status = statusBody({ backfill_in_progress: true, lock_holder_pid: 5 });
    await tick();
    let stopped = false;
    lake.stopSync = function () { stopped = true; };
    await w.find("#btn-lake-sync-toggle").trigger("click");
    expect(stopped).toBe(true);
  });
});
