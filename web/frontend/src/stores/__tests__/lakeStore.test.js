// lakeStore 单测（brief D3：/status 自适应轮询 + v6.0.10 同步状态机移植回归）。
// 覆盖：
//   - 自适应轮询：激活3s / stopping 1s / 非激活但 running 10s / idle+非激活停；切页先立即拉一次；
//   - running→idle 跃迁 → toast"数据灌入完成" + invalidate('lake:*')（跨页签核心）；
//   - v6.0.10：锁定态不被轮询失败覆盖 / stopping 成功确认释放 / 5min 硬超时释放 / lastPid 记忆。
// fetch 按 URL 路由打桩（与 taskStore.test.js 同模式）。
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { setActivePinia, createPinia } from "pinia";
import { __reset, peek, subscribe } from "../../live/queryRegistry.js";
import { useLakeStore, STOP_HARD_MS } from "../lakeStore.js";
import { useToastStore } from "../toastStore.js";

// 鸭子类型响应对象（不用 happy-dom Response：其 json() 在 fake timers 下不 resolve）。
// api() client 只依赖 res.ok / res.status / res.json()。
function jsonRes(status, body) {
  return { ok: status < 400, status, statusText: "MOCK", json: async () => body };
}

// /status 响应工厂（ready 态最小形状）
function statusBody(over = {}) {
  return {
    installed: true, initialized: true, backfill_in_progress: false,
    lock_holder_pid: null, stopping: false, updated_at: "2026-09-17T08:00:00",
    tables: [], views: [], tasks: [], ...over,
  };
}

// fetch 路由：/status 恒返回 state.status（body，测试显式改写；确定性优于队列——
// startSync/stopSync 内部 finally fetchStatus() 会额外消耗一次调用）。
// state.fail = 鸭子响应对象时优先返回它（模拟 5xx 网络错误态）。
function mockFetch(status) {
  const state = {
    status: status || statusBody(),      // 可变：测试随时改写下一次 /status 的 body
    fail: null,                          // 非空 → /status 直接返回该响应（如 jsonRes(500, …)）
    statusCalls: 0,
    startRes: jsonRes(200, { started: true, pid: 4321 }),
    stopRes: jsonRes(200, { waiting_task: true }),
    startCalls: 0, stopCalls: 0,
  };
  globalThis.fetch = vi.fn(async (url) => {
    const u = String(url);
    if (u.startsWith("/api/lake/status")) {
      state.statusCalls += 1;
      return state.fail || jsonRes(200, state.status);
    }
    if (u.startsWith("/api/lake/sync/start")) { state.startCalls += 1; return state.startRes; }
    if (u.startsWith("/api/lake/sync/stop")) { state.stopCalls += 1; return state.stopRes; }
    return jsonRes(404, { detail: "unknown route: " + u });
  });
  return state;
}

// toastStore 形状：单一 msg（"✓ msg"/"✗ msg"）+ ok。返回当前 msg 与历史不可得 →
// 测试在断言前读取；多次 toast 以最后一次为准（6s 自动清空，fake timers 下不触发）。
function toastMsg() {
  const t = useToastStore();
  return t.msg || "";
}

beforeEach(() => {
  vi.useFakeTimers();
  setActivePinia(createPinia());
  __reset();
  globalThis.window.confirm = () => true;   // start/stop 确认框直接通过
});
afterEach(() => {
  vi.useRealTimers();
  __reset();
});

async function flush() {
  // advanceTimersByTimeAsync(0) 只跑一轮微任务；activate() 内 fetchStatus 是未 await 的
  // async（fetch → res.json() → store 赋值 共 ~3 跳微任务）→ 多跑几轮确保落值。
  for (let i = 0; i < 5; i++) await vi.advanceTimersByTimeAsync(0);
}

describe("lakeStore：自适应轮询（报告 §2.3）", () => {
  it("activate → 立即拉一次 + 3s 间隔；deactivate(idle) → 停", async () => {
    const s = mockFetch();
    const lake = useLakeStore();
    lake.activate();
    await flush();
    expect(s.statusCalls).toBe(1);          // 切页先立即拉一次
    await vi.advanceTimersByTimeAsync(3000);
    expect(s.statusCalls).toBe(2);          // 激活期 3s
    await vi.advanceTimersByTimeAsync(6000);
    expect(s.statusCalls).toBe(4);

    lake.deactivate();                      // idle + 非激活 → 停（不空转）
    const before = s.statusCalls;
    await vi.advanceTimersByTimeAsync(120000);
    expect(s.statusCalls).toBe(before);
    lake._stopTimerForTest();
  });

  it("deactivate(running) → 10s 跨页签感知", async () => {
    const s = mockFetch(statusBody({ backfill_in_progress: true, lock_holder_pid: 99 }));
    const lake = useLakeStore();
    lake.activate();
    await flush();
    expect(lake.backfillRunning).toBe(true);

    lake.deactivate();                      // 非激活但 running → 10s
    const before = s.statusCalls;
    await vi.advanceTimersByTimeAsync(9999);
    expect(s.statusCalls).toBe(before);     // 3s/6s/9s 都不触发（不是 3s 节奏）
    await vi.advanceTimersByTimeAsync(1);   // 第 10s → 恰好一轮
    expect(s.statusCalls).toBe(before + 1);
    lake._stopTimerForTest();
  });

  it("stopping 期 → 1s 快轮询（v6.0.10）", async () => {
    const s = mockFetch(statusBody({ backfill_in_progress: true, lock_holder_pid: 99 }));
    const lake = useLakeStore();
    lake.activate();
    await flush();

    await lake.stopSync();                  // → stopping（1s）
    expect(lake.syncState).toBe("stopping");
    const before = s.statusCalls;
    await vi.advanceTimersByTimeAsync(3000);
    expect(s.statusCalls - before).toBeGreaterThanOrEqual(3);   // 1s 节奏（≥3 轮/3s）
    lake._stopTimerForTest();
  });
});

describe("lakeStore：running→idle 跃迁（Joel 核心诉求）", () => {
  it("backfill true→false → toast'数据灌入完成' + invalidate('lake:*')", async () => {
    const s = mockFetch(statusBody({ backfill_in_progress: true, lock_holder_pid: 99 }));
    const lake = useLakeStore();
    lake.activate();
    await flush();
    expect(lake.backfillRunning).toBe(true);

    // 订阅一个湖 key（模拟 MarketTable 在场）→ invalidate 应触发重拉
    let fetches = 0;
    subscribe("lake:market:1:total_mv::", () => { fetches += 1; return Promise.resolve({ rows: [] }); }, { ttl: 0 });
    await flush();
    expect(fetches).toBe(1);

    // 下一轮 /status：running→idle 跃迁
    s.status = statusBody();
    await lake.fetchStatus();
    await flush();
    expect(lake.backfillRunning).toBe(false);
    expect(toastMsg()).toBe("✓ 数据灌入完成");
    expect(fetches).toBe(2);                // invalidate('lake:*') → 订阅中 key 立即重拉
    lake._stopTimerForTest();
  });

  it("false→true 不触发（只有 running→idle 才 toast）", async () => {
    const s = mockFetch(statusBody());
    const lake = useLakeStore();
    lake.activate();
    await flush();
    s.status = statusBody({ backfill_in_progress: true, lock_holder_pid: 9 });
    await lake.fetchStatus();
    await flush();
    expect(toastMsg()).toBe("");   // false→true 不 toast（只有 running→idle 才提示）
    lake._stopTimerForTest();
  });
});

describe("lakeStore：v6.0.10 同步状态机（移植回归）", () => {
  it("startSync：点击即锁定 → POST 返回后释放 + toast（200 started）", async () => {
    const s = mockFetch();
    const lake = useLakeStore();
    await flush();
    const p = lake.startSync();
    expect(lake.syncState).toBe("starting");   // 点击即锁定（POST 在途）
    await p; await flush();
    expect(lake.syncState).toBe("idle");       // POST 返回即释放
    expect(toastMsg()).toBe("✓ 同步已启动（PID 4321）");
    expect(s.startCalls).toBe(1);
    lake._stopTimerForTest();
  });

  it("startSync('incremental')：URL 带 ?mode=incremental + toast'增量同步已启动'（v6.1.4 O2）", async () => {
    const s = mockFetch();
    let lastStartUrl = "";
    // 在 mockFetch 路由基础上包一层记录 start URL（其余行为不变）
    globalThis.fetch = vi.fn(async (url) => {
      const u = String(url);
      if (u.startsWith("/api/lake/sync/start")) { s.startCalls += 1; lastStartUrl = u; }
      return fetchImpl(u);
    });
    // 复用 mockFetch 的路由语义（status/start/stop）——fetchImpl 捕获原实现不可行，
    // 直接按同一契约重写路由（s.status/s.startRes/s.stopRes 共享状态）。
    async function fetchImpl(u) {
      if (u.startsWith("/api/lake/status")) return jsonRes(200, s.status || statusBody());
      if (u.startsWith("/api/lake/sync/start")) return s.startRes;
      if (u.startsWith("/api/lake/sync/stop")) return s.stopRes;
      return jsonRes(404, {});
    }
    const lake = useLakeStore();
    await flush();
    await lake.startSync("incremental");
    await flush();
    expect(lastStartUrl).toBe("/api/lake/sync/start?mode=incremental");
    expect(toastMsg()).toBe("✓ 增量同步已启动（PID 4321）");
    // history 缺省：URL 显式带 ?mode=history（后端 default 兜底，前端恒传 mode）
    lastStartUrl = "";
    await lake.startSync();
    await flush();
    expect(lastStartUrl).toBe("/api/lake/sync/start?mode=history");
    lake._stopTimerForTest();
  });

  it("startSync('t5')：URL 带 ?mode=t5 + toast'T5 基本面灌数已启动'（v6.1.5 F4）", async () => {
    const s = mockFetch();
    let lastStartUrl = "";
    globalThis.fetch = vi.fn(async (url) => {
      const u = String(url);
      if (u.startsWith("/api/lake/sync/start")) { s.startCalls += 1; lastStartUrl = u; }
      async function fetchImpl(v) {
        if (v.startsWith("/api/lake/status")) return jsonRes(200, s.status || statusBody());
        if (v.startsWith("/api/lake/sync/start")) return s.startRes;
        if (v.startsWith("/api/lake/sync/stop")) return s.stopRes;
        return jsonRes(404, {});
      }
      return fetchImpl(u);
    });
    const lake = useLakeStore();
    await flush();
    await lake.startSync("t5");
    await flush();
    expect(lastStartUrl).toBe("/api/lake/sync/start?mode=t5");
    expect(toastMsg()).toBe("✓ T5 基本面灌数已启动（PID 4321）");
    lake._stopTimerForTest();
  });

  it("stopSync：waiting_task → 锁定 stopping；轮询失败（status=null）**不覆盖锁定态**", async () => {
    const s = mockFetch(statusBody({ backfill_in_progress: true, lock_holder_pid: 99 }));
    const lake = useLakeStore();
    lake.activate();
    await flush();

    await lake.stopSync();
    expect(lake.syncState).toBe("stopping");
    expect(lake.stoppingPid).toBe(99);         // pid 取自当前 /status lock_holder_pid

    // 网络抖动：下一轮 /status 5xx → status=null，但锁定态保持（不解锁、防连点 stop）
    s.fail = jsonRes(500, { detail: "boom" });
    await lake.fetchStatus();
    await flush();
    expect(lake.status).toBeNull();
    expect(lake.syncState).toBe("stopping");   // **不被轮询覆盖**（v6.0.10 红线）
    lake._stopTimerForTest();
  });

  it("stopping 成功确认：/status backfill_in_progress=false → toast'已停止，进度已保存'+释放", async () => {
    const s = mockFetch(statusBody({ backfill_in_progress: true, lock_holder_pid: 99 }));
    const lake = useLakeStore();
    lake.activate();
    await flush();
    await lake.stopSync();
    expect(lake.syncState).toBe("stopping");

    // 进程退出、锁释放 → 下一轮成功拉取判定完成
    s.status = statusBody();
    await lake.fetchStatus();
    await flush();
    expect(lake.syncState).toBe("idle");       // 释放回启动按钮
    expect(toastMsg()).toBe("✓ 已停止，进度已保存");
    lake._stopTimerForTest();
  });

  it("5min 硬超时：stopping >STOP_HARD_MS 且仍 running → toast'停止超时…'+释放", async () => {
    const s = mockFetch(statusBody({ backfill_in_progress: true, lock_holder_pid: 99 }));
    const lake = useLakeStore();
    lake.activate();
    await flush();
    await lake.stopSync();
    // 直接回拨 stoppingSince（模拟等待超过硬超时）；state.status 保持 running
    lake.stoppingSince = Date.now() - STOP_HARD_MS - 1000;

    await lake.fetchStatus();   // 仍在跑 + elapsed>hard → 硬超时释放
    await flush();
    expect(lake.syncState).toBe("idle");       // 硬超时释放（不无限锁死）
    expect(toastMsg()).toBe("✗ 停止超时，进程可能仍在收尾，请刷新查看");
    lake._stopTimerForTest();
  });

  it("lastPid 记忆：running 时记住 lock_holder_pid；stopping=true 点击停止 → pid 回退 lastPid（非'未知'）", async () => {
    const s = mockFetch(statusBody({ backfill_in_progress: true, lock_holder_pid: 777 }));
    const lake = useLakeStore();
    lake.activate();
    await flush();
    expect(lake.lastPid).toBe(777);

    // stopping=true（meta 不显示 PID）时点击停止 → pid 回退 lastPid
    s.status = statusBody({ backfill_in_progress: true, lock_holder_pid: 777, stopping: true });
    await lake.fetchStatus();
    await flush();
    expect(lake.lastPid).toBe(777);            // 持续记忆
    await lake.stopSync();
    expect(lake.stoppingPid).toBe(777);        // 停止按钮文案 "(777)" 而非 "(未知)"
    lake._stopTimerForTest();
  });

  it("startSync 409（已有灌数在跑）→ toast hint + 释放不白屏", async () => {
    const s = mockFetch();
    s.startRes = jsonRes(409, { error: "lake_backfill_in_progress", hint: "已有灌数任务在运行" });
    const lake = useLakeStore();
    await flush();
    await lake.startSync();
    await flush();
    expect(lake.syncState).toBe("idle");       // 409 → 释放
    expect(toastMsg()).toBe("✗ 已有灌数任务在运行");
    lake._stopTimerForTest();
  });

  it("!installed（200 但 duckdb 缺失）→ lastError='duckdb 未安装…'（红横幅数据源）", async () => {
    mockFetch(statusBody({ installed: false }));
    const lake = useLakeStore();
    await lake.fetchStatus();
    await flush();
    expect(lake.lastError).toContain("duckdb 未安装");
    lake._stopTimerForTest();
  });
});

describe("lakeStore：DEFECT-D3-2 非激活 running→idle 定时器收敛（brief 9：idle 停）", () => {
  it("切走后灌数结束 → 下一轮 10s 读到 idle → interval 降为 0（不再空转 /status）", async () => {
    const s = mockFetch(statusBody({ backfill_in_progress: true, lock_holder_pid: 7 }));
    const lake = useLakeStore();
    lake.activate();
    await flush();
    expect(lake.backfillRunning).toBe(true);

    lake.deactivate();                      // 用户切到其它页签（灌数仍在跑）
    expect(lake._timerIv).toBe(10000);      // 非激活但 running → 10s 跨页签感知

    s.status = statusBody();                // 灌数完成
    await vi.advanceTimersByTimeAsync(10000); // 下一轮 10s 轮询读到 idle
    expect(lake.status.backfill_in_progress).toBe(false);
    expect(lake._prevBf).toBe(false);
    expect(lake._timerIv).toBe(0);          // **DEFECT-D3-2**：跃迁被观察到 → 停（修复前恒 10s）

    const n = s.statusCalls;
    await vi.advanceTimersByTimeAsync(60000); // 之后 1 分钟零请求（不空转）
    expect(s.statusCalls).toBe(n);
    lake._stopTimerForTest();
  });

  it("回归：激活期 3s 节奏不被 fetchStatus 内新增 recompute 破坏（幂等）", async () => {
    const s = mockFetch(statusBody({ backfill_in_progress: true, lock_holder_pid: 7 }));
    const lake = useLakeStore();
    lake.activate();
    await flush();
    expect(lake._timerIv).toBe(3000);       // 激活优先于 running 分支（3s）
    const before = s.statusCalls;
    await vi.advanceTimersByTimeAsync(9000);
    expect(s.statusCalls - before).toBe(3); // 恰 3 轮/9s（每轮 recompute 同间隔幂等，无漂移）
    lake._stopTimerForTest();
  });

  it("B9.2 回归（定时器驱动）：running→idle toast + invalidate 各恰一次，随后停轮询", async () => {
    const s = mockFetch(statusBody({ backfill_in_progress: true, lock_holder_pid: 7 }));
    const lake = useLakeStore();
    lake.activate();
    await flush();

    // 订阅一个湖 key（模拟 MarketTable 在场）→ invalidate 触发重拉可计数
    let fetches = 0;
    subscribe("lake:market:1:total_mv::", () => { fetches += 1; return Promise.resolve({ rows: [] }); }, { ttl: 0 });
    await flush();
    expect(fetches).toBe(1);

    lake.deactivate();
    s.status = statusBody();                // 灌数完成（下一轮 10s 读到 idle）
    await vi.advanceTimersByTimeAsync(10000);
    await flush();
    expect(toastMsg()).toBe("✓ 数据灌入完成");   // toast 恰一次（跃迁判定在 _prevBf，与定时器无关）
    expect(fetches).toBe(2);                       // invalidate('lake:*') → 重拉恰一次
    expect(lake._timerIv).toBe(0);                 // 且轮询已停

    await vi.advanceTimersByTimeAsync(30000);      // 无重复跃迁 → 不再 invalidate/重拉
    await flush();
    expect(fetches).toBe(2);
    lake._stopTimerForTest();
  });
});
