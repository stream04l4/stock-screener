// taskStore 单测（v7.0）：/api/bt/runs 族 ——
// - SSE log/progress/done/error 事件处理；logTail ≤400 截断；
// - 降级计数（3 次 onerror → 切轮询 /api/bt/runs/{id}/status）；
// - done 时 toast「回测完成！」；lastResults 缓存语义。
// SSE 通道用 __setUseSSEForTest 注入 fake（控制 open/message/error），fetch 打桩。
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { setActivePinia, createPinia } from "pinia";
import { useTaskStore, __setUseSSEForTest } from "../taskStore.js";
import { useToastStore } from "../toastStore.js";

// ---- fake SSE 通道（接口与 useSSE 返回一致：{ failCount, alive, close }）----
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
  inst.open = () => {};
  inst.message = (data) => inst.handlers.onEvent({ lastId: null, data });
  inst.error3 = () => { inst.closed = true; inst.handlers.onDowngrade && inst.handlers.onDowngrade(); };
  return { inst, impl };
}

// ---- fetch 打桩：按 URL 前缀路由 ----
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

const BT_OK = { task_id: "bt_t1", pid: 4242, status: "running", out_dir: "/tmp/x",
                meta: { start: null, end: null, codes: null, pool: true } };

beforeEach(() => {
  setActivePinia(createPinia());
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  delete globalThis.fetch;
});

describe("taskStore · bt SSE 事件处理", () => {
  it("startBtRun → POST /api/bt/runs 成功：state=running、SSE 指向 /api/bt/runs/{id}/events", async () => {
    const { inst, impl } = makeFakeSSE();
    __setUseSSEForTest(impl);
    mockFetch([["/api/bt/runs", jsonRes(200, BT_OK)]]);

    const store = useTaskStore();
    await store.startBtRun({ pool: true });

    expect(store.activeTaskId).toBe("bt_t1");
    expect(store.state).toBe("running");
    expect(inst.url).toBe("/api/bt/runs/bt_t1/events");
    expect(store.taskMeta).toContain("bt_t1");
    expect(store.taskMeta).toContain("股票池");
  });

  it("startBtRun（codes）→ meta 文案含标的列表", async () => {
    const { inst, impl } = makeFakeSSE();
    __setUseSSEForTest(impl);
    mockFetch([[["/api/bt/runs"].flat(), jsonRes(200, { ...BT_OK, meta: { codes: ["sh.601088", "sz.000651"] } })]]);

    const store = useTaskStore();
    await store.startBtRun({ codes: "sh.601088,sz.000651" });
    expect(store.taskMeta).toContain("sh.601088, sz.000651");
  });

  it("startBtRun → 409：toast「已有回测任务…」、不建 SSE、rethrow", async () => {
    const { inst, impl } = makeFakeSSE();
    __setUseSSEForTest(impl);
    mockFetch([["/api/bt/runs", jsonRes(409, { detail: "已有回测任务在进行中: bt_old" })]]);

    const store = useTaskStore();
    await expect(store.startBtRun({ pool: true })).rejects.toMatchObject({ status: 409 });

    expect(useToastStore().msg).toBe("✗ 已有回测任务在进行中，请稍候（可下方查看状态）");
    expect(inst.url).toBe(null);
    expect(store.activeTaskId).toBe(null);
  });

  it("log 事件 → logTail 追加；450 行后截断到 ≤400（保留最新 400 行）", async () => {
    const { inst, impl } = makeFakeSSE();
    __setUseSSEForTest(impl);
    mockFetch([["/api/bt/runs", jsonRes(200, BT_OK)]]);

    const store = useTaskStore();
    await store.startBtRun({ pool: true });
    for (let i = 1; i <= 450; i++) inst.message(JSON.stringify({ type: "log", text: `line-${i}` }));

    expect(store.logTail.length).toBe(400);
    expect(store.logTail[0]).toBe("line-51");
    expect(store.logTail[399]).toBe("line-450");
  });

  it("progress 事件 → progress{done,total,stage} + pct/label getter", async () => {
    const { inst, impl } = makeFakeSSE();
    __setUseSSEForTest(impl);
    mockFetch([["/api/bt/runs", jsonRes(200, BT_OK)]]);

    const store = useTaskStore();
    await store.startBtRun({ pool: true });
    inst.message(JSON.stringify({ type: "progress", stage: "bt", done: 3, total: 4 }));

    expect(store.progress).toEqual({ done: 3, total: 4, stage: "bt" });
    expect(store.pct).toBeCloseTo(75);
    expect(store.progressLabel).toBe("进度 [bt] 3/4（75.0%）");
  });

  it("heartbeat 事件 → 忽略（不产生日志/状态变化）", async () => {
    const { inst, impl } = makeFakeSSE();
    __setUseSSEForTest(impl);
    mockFetch([["/api/bt/runs", jsonRes(200, BT_OK)]]);

    const store = useTaskStore();
    await store.startBtRun({ pool: true });
    inst.message(JSON.stringify({ type: "heartbeat" }));
    expect(store.logTail).toEqual([]);
    expect(store.state).toBe("running");
  });

  it("done 事件 → state=done + toast「回测完成！结果区已刷新」", async () => {
    const { inst, impl } = makeFakeSSE();
    __setUseSSEForTest(impl);
    mockFetch([["/api/bt/runs", jsonRes(200, BT_OK)]]);

    const store = useTaskStore();
    await store.startBtRun({ pool: true });
    inst.message(JSON.stringify({ type: "done" }));

    expect(store.state).toBe("done");
    expect(store.runFinished).toBe(true);
    expect(useToastStore().msg).toBe("✓ 回测完成！结果区已刷新");
  });

  it("error 事件 → logTail 追加「[error 事件]」+ state=failed + 失败 toast", async () => {
    const { inst, impl } = makeFakeSSE();
    __setUseSSEForTest(impl);
    mockFetch([["/api/bt/runs", jsonRes(200, BT_OK)]]);

    const store = useTaskStore();
    await store.startBtRun({ pool: true });
    inst.message(JSON.stringify({ type: "log", text: "前置日志" }));
    inst.message(JSON.stringify({ type: "error" }));

    expect(store.logTail).toEqual(["前置日志", "[error 事件]"]);
    expect(store.state).toBe("failed");
    expect(useToastStore().msg).toBe("✗ 回测失败，请查看日志");
  });

  it("非法 JSON 事件 → 静默忽略", async () => {
    const { inst, impl } = makeFakeSSE();
    __setUseSSEForTest(impl);
    mockFetch([["/api/bt/runs", jsonRes(200, BT_OK)]]);

    const store = useTaskStore();
    await store.startBtRun({ pool: true });
    inst.message("{not json");
    expect(store.logTail).toEqual([]);
    expect(store.state).toBe("running");
  });

  it("fetchResults → GET /api/bt/results/{id} 缓存 lastResults；新任务触发时清空", async () => {
    const { inst, impl } = makeFakeSSE();
    __setUseSSEForTest(impl);
    mockFetch([
      ["/api/bt/results/bt_t1", jsonRes(200, { task_id: "bt_t1", per_stock: [{ ts_code: "sh.601088" }] })],
      ["/api/bt/runs", jsonRes(200, BT_OK)],
    ]);

    const store = useTaskStore();
    await store.startBtRun({ pool: true });
    inst.message(JSON.stringify({ type: "done" }));
    const data = await store.fetchResults("bt_t1");
    expect(data.per_stock).toHaveLength(1);
    expect(store.lastResults.taskId).toBe("bt_t1");

    // 新任务 → lastResults 作废（防串味）
    await store.startBtRun({ pool: true });
    expect(store.lastResults).toBe(null);
  });
});

describe("taskStore · SSE 断连降级（3 次 onerror → 3s 轮询 /status）", () => {
  it("onDowngrade → toast + 立即拉 /api/bt/runs/{id}/status + 每 3s 轮询", async () => {
    const { inst, impl } = makeFakeSSE();
    __setUseSSEForTest(impl);
    const statusCalls = [];
    mockFetch([
      ["/api/bt/runs/bt_t1/status", async () => {
        statusCalls.push(Date.now());
        return jsonRes(200, { task_id: "bt_t1", status: "running", log_tail: ["轮询日志行"] });
      }],
      ["/api/bt/runs", jsonRes(200, BT_OK)],
    ]);

    const store = useTaskStore();
    await store.startBtRun({ pool: true });
    inst.error3();

    expect(useToastStore().msg).toBe("✗ SSE 不可用，已回退到 3s 轮询");
    expect(store.sseAlive).toBe(false);
    await vi.advanceTimersByTimeAsync(0);
    expect(statusCalls.length).toBeGreaterThanOrEqual(1);
    expect(store.logTail).toEqual(["轮询日志行"]);

    const before = statusCalls.length;
    await vi.advanceTimersByTimeAsync(6000);
    expect(statusCalls.length).toBe(before + 2); // 3s 间隔
  });

  it("降级后 /status 返回 done → finish('done') 停止轮询", async () => {
    const { inst, impl } = makeFakeSSE();
    __setUseSSEForTest(impl);
    let n = 0;
    mockFetch([
      ["/api/bt/runs/bt_t1/status", async () => {
        n += 1;
        return jsonRes(200, { task_id: "bt_t1", status: n >= 2 ? "done" : "running", log_tail: [] });
      }],
      ["/api/bt/runs", jsonRes(200, BT_OK)],
    ]);

    const store = useTaskStore();
    await store.startBtRun({ pool: true });
    inst.error3();
    await vi.advanceTimersByTimeAsync(0); // tick#1 running
    await vi.advanceTimersByTimeAsync(3000); // tick#2 done → finish

    expect(store.state).toBe("done");
    expect(useToastStore().msg).toBe("✓ 回测完成！结果区已刷新");
    const callsAfter = n;
    await vi.advanceTimersByTimeAsync(9000);
    expect(n).toBe(callsAfter); // finish 后轮询已停
  });

  it("降级后 /status 404（服务重启任务未知）→ 停止轮询", async () => {
    const { inst, impl } = makeFakeSSE();
    __setUseSSEForTest(impl);
    let n = 0;
    mockFetch([
      ["/api/bt/runs/bt_t1/status", async () => { n += 1; return jsonRes(404, { detail: "任务不存在" }); }],
      ["/api/bt/runs", jsonRes(200, BT_OK)],
    ]);

    const store = useTaskStore();
    await store.startBtRun({ pool: true });
    inst.error3();
    await vi.advanceTimersByTimeAsync(0); // tick#1 → 404 → stopMonitors
    expect(n).toBe(1);
    await vi.advanceTimersByTimeAsync(9000);
    expect(n).toBe(1); // 不再轮询
  });
});
