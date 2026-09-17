// taskStore 单测（brief D2 自测要求）：
// - SSE log/progress/done/error 事件处理；logTail ≤400 截断；
// - 降级计数（3 次 onerror → 切轮询 /api/runs/{id}/status，保留 v1 兜底）；
// - done 时 toast + invalidate('runs') 触发（跨页签核心验收点的单测层）。
// SSE 通道用 __setUseSSEForTest 注入 fake（控制 open/message/error），fetch 打桩。
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { setActivePinia, createPinia } from "pinia";
import { __reset } from "../../live/queryRegistry.js";
import { useTaskStore, __setUseSSEForTest } from "../taskStore.js";
import { useToastStore } from "../toastStore.js";

// ---- fake SSE 通道（接口与 D1 useSSE 返回一致：{ failCount, alive, close }）----
function makeFakeSSE() {
  const inst = { url: null, handlers: {}, closed: false };
  const impl = (url, onEvent, opts) => {
    inst.url = url;
    inst.handlers = { onEvent, onDowngrade: opts && opts.onDowngrade };
    return {
      failCount: { value: 0 },
      alive: { value: true },
      close() { inst.closed = true; },
    };
  };
  inst.open = () => {}; // EventSource 建连（fake：无真实网络）
  inst.message = (data) => inst.handlers.onEvent({ lastId: null, data });
  // useSSE 语义：连续 3 次 onerror → close + onDowngrade **只调一次**（此处直接模拟降级结果）
  inst.error3 = () => { inst.closed = true; inst.handlers.onDowngrade && inst.handlers.onDowngrade(); };
  return { inst, impl };
}

// ---- fetch 打桩：按 URL 路由（route 值 = Response 对象或 () => Response 工厂）----
function mockFetch(routes) {
  globalThis.fetch = vi.fn(async (url) => {
    for (const [prefix, res] of routes) {
      if (String(url).startsWith(prefix)) {
        return typeof res === "function" ? await res() : res;
      }
    }
    return jsonRes(404, { detail: "unknown route: " + url });
  });
}

function jsonRes(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: "S" + status,
    json: async () => body,
  };
}

beforeEach(() => {
  setActivePinia(createPinia());
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  __reset();
  delete globalThis.fetch;
});

describe("taskStore · SSE 事件处理", () => {
  it("startRun → POST /api/runs 成功：state=running、SSE 指向 /api/runs/{id}/events、meta 文案同旧版", async () => {
    const { inst, impl } = makeFakeSSE();
    __setUseSSEForTest(impl);
    mockFetch([["/api/runs", jsonRes(200, { task_id: "web_t1", pid: 4242, date: "2026-09-17", status: "running" })]]);

    const store = useTaskStore();
    await store.startRun("2026-09-17");

    expect(store.activeTaskId).toBe("web_t1");
    expect(store.state).toBe("running");
    expect(inst.url).toBe("/api/runs/web_t1/events");
    expect(store.taskMeta).toBe("任务 web_t1 · PID 4242 · 日期 2026-09-17");
  });

  it("startRun → 409：toast「已有运行任务…」（文案逐字同旧版），不建 SSE", async () => {
    const { inst, impl } = makeFakeSSE();
    __setUseSSEForTest(impl);
    mockFetch([["/api/runs", jsonRes(409, { detail: "已有运行任务在进行中: web_old" })]]);

    const store = useTaskStore();
    await expect(store.startRun("2026-09-17")).rejects.toMatchObject({ status: 409 });

    expect(useToastStore().msg).toBe("✗ 已有运行任务在进行中，请稍候（可下方查看状态）");
    expect(inst.url).toBe(null); // SSE 未建立
    expect(store.activeTaskId).toBe(null);
  });

  it("log 事件 → logTail 追加；450 行后截断到 ≤400（保留最新 400 行）", async () => {
    const { inst, impl } = makeFakeSSE();
    __setUseSSEForTest(impl);
    mockFetch([["/api/runs", jsonRes(200, { task_id: "web_t1", pid: 1, date: "2026-09-17" })]]);

    const store = useTaskStore();
    await store.startRun("2026-09-17");
    for (let i = 1; i <= 450; i++) inst.message(JSON.stringify({ type: "log", text: `line-${i}` }));

    expect(store.logTail.length).toBe(400); // ≤400 截断（旧版 appendLogLine 一致）
    expect(store.logTail[0]).toBe("line-51"); // 最旧 50 行被挤出
    expect(store.logTail[399]).toBe("line-450");
  });

  it("progress 事件 → progress{done,total,stage} + pct/label getter（旧版 updateProgressBar 语义）", async () => {
    const { inst, impl } = makeFakeSSE();
    __setUseSSEForTest(impl);
    mockFetch([["/api/runs", jsonRes(200, { task_id: "web_t1", pid: 1, date: "2026-09-17" })]]);

    const store = useTaskStore();
    await store.startRun("2026-09-17");
    inst.message(JSON.stringify({ type: "progress", stage: "kline", done: 3, total: 4 }));

    expect(store.progress).toEqual({ done: 3, total: 4, stage: "kline" });
    expect(store.pct).toBeCloseTo(75);
    expect(store.progressLabel).toBe("进度 [kline] 3/4（75.0%）");
  });

  it("heartbeat 事件 → 忽略（不产生日志/状态变化）", async () => {
    const { inst, impl } = makeFakeSSE();
    __setUseSSEForTest(impl);
    mockFetch([["/api/runs", jsonRes(200, { task_id: "web_t1", pid: 1, date: "2026-09-17" })]]);

    const store = useTaskStore();
    await store.startRun("2026-09-17");
    inst.message(JSON.stringify({ type: "heartbeat" }));
    expect(store.logTail).toEqual([]);
    expect(store.state).toBe("running");
  });

  it("done 事件 → state=done + toast「筛选完成！结果页已刷新」+ invalidate('runs')（跨页签核心）", async () => {
    const { inst, impl } = makeFakeSSE();
    __setUseSSEForTest(impl);
    mockFetch([["/api/runs", jsonRes(200, { task_id: "web_t1", pid: 1, date: "2026-09-17" })]]);

    const store = useTaskStore();
    await store.startRun("2026-09-17");

    // 订阅 'runs' key（模拟结果页 RunList 在挂载）→ invalidate 应触发其重拉
    let refreshes = 0;
    const { subscribe } = await import("../../live/queryRegistry.js");
    subscribe("runs", async () => { refreshes += 1; return { runs: [] }; });
    await vi.advanceTimersByTimeAsync(0); // 等首订阅的 in-flight 落地（否则 invalidate 触发被去重）

    inst.message(JSON.stringify({ type: "done" }));

    expect(store.state).toBe("done");
    expect(useToastStore().msg).toBe("✓ 筛选完成！结果页已刷新");
    await vi.advanceTimersByTimeAsync(0); // invalidate → 后台刷新 Promise 落地
    expect(refreshes).toBeGreaterThanOrEqual(2); // 首订阅 1 次 + invalidate 触发 ≥1 次
  });

  it("error 事件 → logTail 追加「[error 事件]」+ state=failed + 失败 toast", async () => {
    const { inst, impl } = makeFakeSSE();
    __setUseSSEForTest(impl);
    mockFetch([["/api/runs", jsonRes(200, { task_id: "web_t1", pid: 1, date: "2026-09-17" })]]);

    const store = useTaskStore();
    await store.startRun("2026-09-17");
    inst.message(JSON.stringify({ type: "log", text: "前置日志" }));
    inst.message(JSON.stringify({ type: "error" }));

    expect(store.logTail).toEqual(["前置日志", "[error 事件]"]);
    expect(store.state).toBe("failed");
    expect(useToastStore().msg).toBe("✗ 运行失败，请查看日志");
  });

  it("非法 JSON 事件 → 静默忽略（旧版 try/catch return 语义）", async () => {
    const { inst, impl } = makeFakeSSE();
    __setUseSSEForTest(impl);
    mockFetch([["/api/runs", jsonRes(200, { task_id: "web_t1", pid: 1, date: "2026-09-17" })]]);

    const store = useTaskStore();
    await store.startRun("2026-09-17");
    inst.message("{not json");
    expect(store.logTail).toEqual([]);
    expect(store.state).toBe("running");
  });
});

describe("taskStore · SSE 断连降级（3 次 onerror → 3s 轮询 /status，保留 v1 兜底）", () => {
  it("onDowngrade → toast「SSE 不可用，已回退到 3s 轮询」+ 立即拉 /status + 每 3s 轮询", async () => {
    const { inst, impl } = makeFakeSSE();
    __setUseSSEForTest(impl);
    const statusCalls = [];
    mockFetch([
      ["/api/runs/web_t1/events", jsonRes(200, {})],
      ["/api/runs/web_t1/status", async () => {
        statusCalls.push(Date.now());
        return jsonRes(200, { task_id: "web_t1", status: "running", date: "2026-09-17", log_tail: ["轮询日志行"] });
      }],
      ["/api/runs", jsonRes(200, { task_id: "web_t1", pid: 1, date: "2026-09-17" })],
    ]);

    const store = useTaskStore();
    await store.startRun("2026-09-17");
    inst.error3(); // 连续 3 次 onerror（useSSE 已 close）→ onDowngrade ×1

    expect(useToastStore().msg).toBe("✗ SSE 不可用，已回退到 3s 轮询");
    expect(store.sseAlive).toBe(false);
    await vi.advanceTimersByTimeAsync(0);
    expect(statusCalls.length).toBeGreaterThanOrEqual(1); // tick() 立即执行
    expect(store.logTail).toEqual(["轮询日志行"]); // log_tail 整段替换（旧版语义）

    const before = statusCalls.length;
    await vi.advanceTimersByTimeAsync(6000); // 2 个轮询周期
    expect(statusCalls.length).toBe(before + 2); // 3s 间隔
  });

  it("降级后 /status 返回 done → finish('done')：toast+invalidate、停止轮询", async () => {
    const { inst, impl } = makeFakeSSE();
    __setUseSSEForTest(impl);
    let n = 0;
    mockFetch([
      ["/api/runs/web_t1/status", async () => {
        n += 1;
        return jsonRes(200, { task_id: "web_t1", status: n >= 2 ? "done" : "running", date: "", log_tail: [] });
      }],
      ["/api/runs", jsonRes(200, { task_id: "web_t1", pid: 1, date: "2026-09-17" })],
    ]);

    const store = useTaskStore();
    await store.startRun("2026-09-17");
    inst.error3();
    await vi.advanceTimersByTimeAsync(0); // tick#1 running
    await vi.advanceTimersByTimeAsync(3000); // tick#2 done → finish

    expect(store.state).toBe("done");
    expect(useToastStore().msg).toBe("✓ 筛选完成！结果页已刷新");
    const callsAfter = n;
    await vi.advanceTimersByTimeAsync(9000);
    expect(n).toBe(callsAfter); // finish 后轮询已停
  });

  it("降级后 /status 404（服务重启任务未知）→ 停止轮询（旧版语义原样）", async () => {
    const { inst, impl } = makeFakeSSE();
    __setUseSSEForTest(impl);
    let n = 0;
    mockFetch([
      ["/api/runs/web_t1/status", async () => { n += 1; return jsonRes(404, { detail: "任务不存在" }); }],
      ["/api/runs", jsonRes(200, { task_id: "web_t1", pid: 1, date: "2026-09-17" })],
    ]);

    const store = useTaskStore();
    await store.startRun("2026-09-17");
    inst.error3();
    await vi.advanceTimersByTimeAsync(0); // tick#1 → 404 → stopMonitors
    expect(n).toBe(1);
    await vi.advanceTimersByTimeAsync(9000);
    expect(n).toBe(1); // 不再轮询
  });
});
