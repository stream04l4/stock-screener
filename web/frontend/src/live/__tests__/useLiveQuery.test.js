// useLiveQuery 单测（DEFECT-1 修复补测，brief D1-缺陷轮要求 #2）：
// tester 指出该文件（D1 交付物）完全无测试。覆盖三类核心契约：
//   1) 成功路径：loading/data 正常翻转（首载 loading true→false、error 恒 null）；
//   2) 首次加载失败且组件保持挂载 → error=Error、loading=false（不得永久卡"加载中…"，
//      对齐旧版 app.js loadRuns/loadRunDetail catch → "加载失败: msg"）；
//   3) 已有数据时后台刷新失败（invalidate / TTL 过期重订阅触发）→ data 保留旧值、
//      error 透出、loading 不翻转（SWR 语义，无闪烁）。
// 另在引擎层直接断言 refresh() 失败分支会 notify(entry)（DEFECT-1 根因点）。
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { createApp, h } from "vue";
import { useLiveQuery } from "../useLiveQuery.js";
import { subscribe, __reset, invalidate, peek } from "../queryRegistry.js";

function mountHook(fn) {
  const el = document.createElement("div");
  let result;
  const app = createApp({
    setup() {
      result = fn();
      return () => h("div");
    },
  });
  app.mount(el);
  return { app, get: () => result };
}

// 可控制 settle 时机的 Promise（延迟 reject/resolve，模拟网络失败）
function deferred() {
  let resolve, reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

beforeEach(() => {
  vi.useFakeTimers();
  __reset();
});
afterEach(() => {
  vi.useRealTimers();
  __reset();
});

describe("useLiveQuery: 成功路径", () => {
  it("首载成功：loading true→false、data 更新、error 恒 null", async () => {
    const d = deferred();
    const { get, app } = mountHook(() => useLiveQuery("runs", () => d.promise, {}));
    // 挂载瞬间（请求 in-flight）：loading=true、无数据、无错误
    expect(get().loading.value).toBe(true);
    expect(get().data.value).toBeUndefined();
    expect(get().error.value).toBeNull();

    d.resolve([{ id: 1 }]);
    await vi.advanceTimersByTimeAsync(0); // flush 微任务：notify → sync
    expect(get().loading.value).toBe(false);
    expect(get().data.value).toEqual([{ id: 1 }]);
    expect(get().error.value).toBeNull();
    app.unmount();
  });

  it("后续成功刷新：data 更新、error 清除（失败→恢复路径）", async () => {
    let n = 0;
    const d2 = deferred();
    const fetcher = () => {
      n += 1;
      return n === 1 ? Promise.resolve("v1") : d2.promise;
    };
    const { get, app } = mountHook(() => useLiveQuery("k", fetcher, {}));
    await vi.advanceTimersByTimeAsync(0);
    expect(get().data.value).toBe("v1");

    invalidate("k"); // 触发第 2 次刷新（in-flight）
    await vi.advanceTimersByTimeAsync(0);
    d2.resolve("v2");
    await vi.advanceTimersByTimeAsync(0);
    expect(get().data.value).toBe("v2");
    expect(get().error.value).toBeNull();
    expect(get().loading.value).toBe(false);
    app.unmount();
  });
});

describe("useLiveQuery: 首次加载失败（组件保持挂载）", () => {
  it("首载失败 → error=Error、loading=false（不得永久卡'加载中…'）", async () => {
    const d = deferred();
    const { get, app } = mountHook(() => useLiveQuery("runs", () => d.promise, {}));
    await vi.advanceTimersByTimeAsync(0); // 请求 in-flight
    expect(get().loading.value).toBe(true);

    d.reject(new Error("boom"));
    await vi.advanceTimersByTimeAsync(0); // flush：catch → notify → sync

    // 契约（对齐旧版 app.js "加载失败: msg"）：error 透出、loading 退出，
    // 组件 v-else-if="error" 分支可达
    expect(get().error.value).toBeInstanceOf(Error);
    expect(get().error.value.message).toBe("boom");
    expect(get().loading.value).toBe(false);
    expect(get().data.value).toBeUndefined();
    app.unmount();
  });

  it("首载失败后恢复刷新 → error 清除、data 落值", async () => {
    let n = 0;
    const d1 = deferred();
    const fetcher = () => (n += 1) === 1 ? d1.promise : Promise.resolve("recovered");
    const { get, app } = mountHook(() => useLiveQuery("runs", fetcher, {}));
    await vi.advanceTimersByTimeAsync(0);
    d1.reject(new Error("boom"));
    await vi.advanceTimersByTimeAsync(0);
    expect(get().error.value).toBeInstanceOf(Error);

    invalidate("runs"); // 手动 ⟳ / 事件触发重试
    await vi.advanceTimersByTimeAsync(0);
    expect(get().data.value).toBe("recovered");
    expect(get().error.value).toBeNull();
    expect(get().loading.value).toBe(false);
    app.unmount();
  });
});

describe("useLiveQuery: 已有数据时后台刷新失败（SWR）", () => {
  it("invalidate 触发刷新失败 → data 保留旧值、error 透出、loading 不翻转", async () => {
    let n = 0;
    const d2 = deferred();
    const fetcher = () => (n += 1) === 1 ? Promise.resolve("v1") : d2.promise;
    const { get, app } = mountHook(() => useLiveQuery("run:2026-09-11", fetcher, {}));
    await vi.advanceTimersByTimeAsync(0);
    expect(get().data.value).toBe("v1");
    expect(get().loading.value).toBe(false);

    invalidate("run:"); // 后台刷新（D1 真实路径：ResultsTab ⟳ / run 完成事件）
    await vi.advanceTimersByTimeAsync(0); // 第 2 次请求 in-flight
    d2.reject(new Error("boom2"));
    await vi.advanceTimersByTimeAsync(0); // flush：catch → notify → sync

    expect(get().data.value).toBe("v1"); // 旧值保留（不清空、不闪烁）
    expect(get().error.value).toBeInstanceOf(Error); // 失败透出（非静默）
    expect(get().loading.value).toBe(false); // SWR：loading 不翻转
    app.unmount();
  });

  it("TTL 过期重订阅触发刷新失败 → 新挂载组件仍拿旧值、error 透出", async () => {
    let calls = 0;
    const d2 = deferred();
    const fetcher = () => {
      calls += 1;
      return calls === 1 ? Promise.resolve("old") : d2.promise;
    };

    // 第一个组件挂载并成功，随后卸载（会话缓存保留）
    const el = document.createElement("div");
    const app1 = createApp({ setup: () => { useLiveQuery("run:x", fetcher, { ttl: 1000 }); return () => h("div"); } });
    app1.mount(el);
    await vi.advanceTimersByTimeAsync(0);
    app1.unmount();

    await vi.advanceTimersByTimeAsync(1500); // TTL(1s) 过期

    // 第二个组件挂载：SWR 立即拿旧值（loading 不翻转），后台刷新 in-flight
    let r2;
    const app2 = createApp({ setup() { r2 = useLiveQuery("run:x", fetcher, { ttl: 1000 }); return () => h("div"); } });
    app2.mount(el);
    expect(r2.data.value).toBe("old");
    expect(r2.loading.value).toBe(false);
    await vi.advanceTimersByTimeAsync(0); // 后台刷新已发出
    expect(calls).toBe(2);

    d2.reject(new Error("boom3"));
    await vi.advanceTimersByTimeAsync(0);
    expect(r2.data.value).toBe("old"); // 旧值保留
    expect(r2.error.value).toBeInstanceOf(Error); // 失败透出
    expect(r2.loading.value).toBe(false); // loading 不翻转
    app2.unmount();
  });
});

describe("queryRegistry: refresh() 失败分支 notify（DEFECT-1 根因点）", () => {
  it("刷新失败时调用 notify(entry)（订阅者收到通知，不再静默）", async () => {
    const d = deferred();
    let notified = 0;
    const entry = subscribe("k", () => d.promise, {});
    entry.listeners.add(() => { notified += 1; });
    await vi.advanceTimersByTimeAsync(0); // fetcher in-flight
    expect(notified).toBe(0);

    d.reject(new Error("boom"));
    await vi.advanceTimersByTimeAsync(0); // catch → notify（修复点）→ rethrow 被引擎内部吞掉
    expect(notified).toBe(1); // 失败也通知订阅者
    expect(entry.lastError).toBeInstanceOf(Error);
    expect(entry.inFlight).toBeNull(); // inFlight 已清（可重试）
  });

  it("成功路径 notify 次数不变（修复不改变成功时序/去重语义）", async () => {
    let n = 0;
    const d1 = deferred(), d2 = deferred();
    const fetcher = () => (n += 1) === 1 ? d1.promise : d2.promise;
    const entry = subscribe("k", fetcher, {});
    let notified = 0;
    entry.listeners.add(() => { notified += 1; });

    await vi.advanceTimersByTimeAsync(0);
    d1.resolve("a");
    await vi.advanceTimersByTimeAsync(0);
    expect(notified).toBe(1); // 首次成功 → 恰好 1 次 notify

    invalidate("k");
    await vi.advanceTimersByTimeAsync(0);
    d2.resolve("b");
    await vi.advanceTimersByTimeAsync(0);
    expect(notified).toBe(2); // 第二次成功 → 再 1 次（无多余通知）
    expect(entry.data).toBe("b");
  });

  it("失败后 in-flight 去重不受影响：失败期间并发订阅共享同一 Promise", async () => {
    let calls = 0;
    const d = deferred();
    subscribe("k", () => { calls += 1; return d.promise; }, {});
    await vi.advanceTimersByTimeAsync(0); // in-flight（将失败）
    const e2 = subscribe("k", () => { calls += 1; return d.promise; }, {}); // 并发订阅 → 共享
    await vi.advanceTimersByTimeAsync(0);
    expect(calls).toBe(1);

    d.reject(new Error("boom"));
    await vi.advanceTimersByTimeAsync(0);
    expect(e2.inFlight).toBeNull(); // 失败后锚点已清
    subscribe("k", () => { calls += 1; return Promise.resolve("ok"); }, {}); // 可重试（at=null → 立即刷新）
    await vi.advanceTimersByTimeAsync(0);
    expect(calls).toBe(2);
    expect(peek("k")).toBe("ok");
  });
});
