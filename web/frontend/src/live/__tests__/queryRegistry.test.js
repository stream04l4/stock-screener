// 统一刷新引擎单测（brief D1 自测要求 #1）：
// 请求合并/去重、TTL stale-while-revalidate、refcount=0 停轮询、invalidate(prefix)。
// 纯逻辑层（queryRegistry），不依赖 Vue/DOM；时间全部 vi.useFakeTimers 控制。
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { subscribe, unsubscribe, invalidate, peek, __reset } from "../queryRegistry.js";

beforeEach(() => {
  vi.useFakeTimers();
  __reset();
});
afterEach(() => {
  vi.useRealTimers();
  __reset();
});

// 可控制 resolve 时机的 fetcher 工厂
function deferred() {
  let resolve, reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

describe("queryRegistry: 请求合并/去重", () => {
  it("同 key 并发订阅共享同一个 in-flight Promise（只发一次网络请求）", async () => {
    let calls = 0;
    const d = deferred();
    const fetcher = () => { calls += 1; return d.promise; };

    const e1 = subscribe("k", fetcher, {});
    await vi.advanceTimersByTimeAsync(0); // flush 微任务：fetcher 在 Promise.resolve().then 中执行
    expect(calls).toBe(1);
    const e2 = subscribe("k", fetcher, {}); // 同 key 第二次订阅：in-flight → 共享
    await vi.advanceTimersByTimeAsync(0);
    expect(calls).toBe(1);
    expect(e1.inFlight).toBe(e2.inFlight);

    d.resolve({ v: 1 });
    await vi.advanceTimersByTimeAsync(0);
    expect(calls).toBe(1); // 仍然只发了一次
    expect(peek("k")).toEqual({ v: 1 });
  });

  it("in-flight 期间 invalidate 不重复发请求（共享同一 Promise）", async () => {
    let calls = 0;
    const d = deferred();
    subscribe("k", () => { calls += 1; return d.promise; }, {});
    await vi.advanceTimersByTimeAsync(0); // fetcher 已执行（in-flight）
    invalidate("k"); // in-flight 中失效：at=null，但 refresh 看到 inFlight → 复用
    expect(calls).toBe(1);
    d.resolve("x");
    await vi.advanceTimersByTimeAsync(0);
    expect(calls).toBe(1);
    expect(peek("k")).toBe("x");
  });

  it("失败后再次订阅会重试（inFlight 已清）", async () => {
    let calls = 0;
    const d1 = deferred();
    subscribe("k", () => { calls += 1; return calls === 1 ? d1.promise : Promise.resolve("ok"); }, {});
    await vi.advanceTimersByTimeAsync(0);
    d1.reject(new Error("boom"));
    await vi.advanceTimersByTimeAsync(0); // 吞掉 rethrow（引擎内部 catch）
    expect(peek("k")).toBeUndefined();

    subscribe("k", () => { calls += 1; return Promise.resolve("ok"); }, {});
    await vi.advanceTimersByTimeAsync(0);
    expect(calls).toBe(2);
    expect(peek("k")).toBe("ok");
  });
});

describe("queryRegistry: TTL stale-while-revalidate", () => {
  it("TTL 内重订阅不发请求（会话缓存）", async () => {
    let calls = 0;
    const fetcher = () => { calls += 1; return Promise.resolve("v1"); };
    subscribe("k", fetcher, { ttl: 30000 });
    await vi.runAllTimersAsync();
    expect(calls).toBe(1);

    unsubscribe("k");
    subscribe("k", fetcher, { ttl: 30000 }); // TTL 内 → 不重发
    await vi.runAllTimersAsync();
    expect(calls).toBe(1);
    expect(peek("k")).toBe("v1");
  });

  it("TTL 过期 → 立即返旧值 + 后台刷新（stale-while-revalidate）", async () => {
    let calls = 0;
    const d2 = deferred();
    subscribe("k", () => {
      calls += 1;
      return calls === 1 ? Promise.resolve("old") : d2.promise;
    }, { ttl: 1000 });
    await vi.advanceTimersByTimeAsync(0); // flush 首次请求微任务（旧值入缓存）
    expect(peek("k")).toBe("old");

    unsubscribe("k");
    await vi.advanceTimersByTimeAsync(1500); // TTL(1s) 过期

    const entry = subscribe("k", () => {
      calls += 1;
      return calls === 1 ? Promise.resolve("old") : d2.promise;
    }, { ttl: 1000 });
    // SWR：此刻调用方立即拿到旧值（不是 undefined/等待）
    expect(entry.data).toBe("old");
    await vi.advanceTimersByTimeAsync(0); // flush 微任务：后台刷新已发出（第二次请求 in-flight）
    expect(calls).toBe(2);

    d2.resolve("new");
    await vi.advanceTimersByTimeAsync(0);
    expect(peek("k")).toBe("new"); // 新值到达后更新缓存
  });

  it("ttl=0/缺省 → 会话缓存：永不过期，重订阅不重取；invalidate 可强制刷新", async () => {
    // 报告 §2.1：GET /api/runs/{day} = 按需+会话缓存（键 day）——同 key 会话内不重发，
    // 新鲜度由事件触发（run 完成 → invalidate('run:')）驱动。
    let calls = 0;
    const fetcher = () => { calls += 1; return Promise.resolve(calls); };
    subscribe("k", fetcher, {});
    await vi.advanceTimersByTimeAsync(0);
    unsubscribe("k");
    subscribe("k", fetcher, {}); // TTL 永不过期 → 不重取
    await vi.advanceTimersByTimeAsync(0);
    expect(calls).toBe(1);

    invalidate("k"); // 事件触发强制刷新
    await vi.advanceTimersByTimeAsync(0);
    expect(calls).toBe(2);
  });
});

describe("queryRegistry: 引用计数订阅（refcount=0 停轮询）", () => {
  it("interval 定时器只在 refcount>0 时运行；全部退订后停止", async () => {
    let calls = 0;
    const fetcher = () => { calls += 1; return Promise.resolve(calls); };

    subscribe("poll", fetcher, { interval: 1000 });
    await vi.advanceTimersByTimeAsync(0); // flush 首次刷新微任务（interval 在跑，禁用 runAllTimersAsync）
    expect(calls).toBe(1); // 首次立即取

    await vi.advanceTimersByTimeAsync(3000);
    expect(calls).toBe(4); // 1s/次 × 3 → 定时器在跑

    unsubscribe("poll"); // refcount=0 → 停定时器
    const callsAtStop = calls;
    await vi.advanceTimersByTimeAsync(5000);
    expect(calls).toBe(callsAtStop); // 退订后不再发请求（杜绝忘停轮询）
  });

  it("两个订阅者共享一个定时器；各退一次才停", async () => {
    let calls = 0;
    const fetcher = () => { calls += 1; return Promise.resolve(calls); };
    subscribe("poll", fetcher, { interval: 1000 });
    subscribe("poll", fetcher, { interval: 1000 });
    await vi.advanceTimersByTimeAsync(0);
    expect(calls).toBe(1); // 合并：只发一次

    unsubscribe("poll"); // refcount 2→1，定时器保留
    await vi.advanceTimersByTimeAsync(1000);
    expect(calls).toBe(2);

    unsubscribe("poll"); // refcount 1→0，停
    const at = calls;
    await vi.advanceTimersByTimeAsync(3000);
    expect(calls).toBe(at);
  });

  it("退订后缓存保留（会话缓存语义），重订阅 TTL 内不重发", async () => {
    let calls = 0;
    const fetcher = () => { calls += 1; return Promise.resolve("v"); };
    subscribe("k", fetcher, { ttl: 60000 });
    await vi.runAllTimersAsync();
    unsubscribe("k");
    expect(peek("k")).toBe("v"); // 缓存仍在

    subscribe("k", fetcher, { ttl: 60000 });
    await vi.runAllTimersAsync();
    expect(calls).toBe(1); // TTL 内不重发
  });
});

describe("queryRegistry: invalidate(prefix) 事件总线", () => {
  it("前缀匹配失效：有订阅者的 key 立即刷新；无订阅者的标记过期但保留旧值（SWR 语义）", async () => {
    let runCalls = 0, lakeCalls = 0;
    subscribe("runs", () => { runCalls += 1; return Promise.resolve({ n: runCalls }); }, {});
    subscribe("lake:status", () => { lakeCalls += 1; return Promise.resolve({ n: lakeCalls }); }, {});
    await vi.advanceTimersByTimeAsync(0);
    expect(runCalls).toBe(1);
    expect(lakeCalls).toBe(1);

    // 无订阅者的 key：仅入缓存（先订阅再退订）
    let orphanCalls = 0;
    subscribe("lake:market", () => { orphanCalls += 1; return Promise.resolve("m"); }, {});
    await vi.advanceTimersByTimeAsync(0);
    unsubscribe("lake:market");

    invalidate("lake:"); // 命中 lake:status（有订阅者→刷新）+ lake:market（无订阅者→标记过期）
    await vi.advanceTimersByTimeAsync(0);
    expect(lakeCalls).toBe(2);       // 有订阅者 → 立即后台刷新
    expect(orphanCalls).toBe(1);     // 无订阅者 → 不发请求
    expect(peek("lake:market")).toBe("m"); // 旧值保留（SWR：重订阅先返旧值再后台刷新）

    invalidate("runs");
    await vi.advanceTimersByTimeAsync(0);
    expect(runCalls).toBe(2);
  });

  it("invalidate('run:') 只命中 run:* 键（不误伤 runs）", async () => {
    let runDetail = 0, runs = 0;
    subscribe("run:2026-09-11", () => { runDetail += 1; return Promise.resolve(runDetail); }, {});
    subscribe("runs", () => { runs += 1; return Promise.resolve(runs); }, {});
    await vi.advanceTimersByTimeAsync(0);

    invalidate("run:");
    await vi.advanceTimersByTimeAsync(0);
    expect(runDetail).toBe(2); // run:* 失效刷新
    expect(runs).toBe(1);      // "runs" 不以 "run:" 开头 → 不受影响
  });

  it("invalidate 后 TTL 计时重置（新值到达后重订阅不再立即重取）", async () => {
    let calls = 0;
    const fetcher = () => { calls += 1; return Promise.resolve(calls); };
    subscribe("k", fetcher, { ttl: 60000 });
    await vi.advanceTimersByTimeAsync(0);

    invalidate("k");
    await vi.advanceTimersByTimeAsync(0); // 刷新完成，at=now
    expect(calls).toBe(2);

    unsubscribe("k");
    subscribe("k", fetcher, { ttl: 60000 }); // TTL 内（刚刷新过）→ 不重发
    await vi.advanceTimersByTimeAsync(0);
    expect(calls).toBe(2);
  });
});
