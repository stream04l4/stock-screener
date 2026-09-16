// 统一刷新引擎 · 中央数据层（调研报告 §2.2，~150 行自研；不上 TanStack Query）。
//
// 契约（brief D1）：
// 1) 同 key in-flight Promise 共享（请求合并/去重）——多订阅者并发只发一个网络请求。
// 2) TTL stale-while-revalidate：ttl>0 且缓存过期 → 调用方立即拿到旧值（无 loading 闪烁），
//    引擎同时后台刷新；新值到达后通知所有订阅者更新。
// 3) 引用计数订阅：subscribe/unsubscribe 按 key 增减 refcount；refcount=0 停该 key 的
//    interval 定时器（切走页签即停，杜绝"忘停轮询"类 bug）。缓存数据保留（会话缓存语义，
//    报告 §2.1 "按需+会话缓存"），重订阅时 TTL 内不再发请求。
// 4) invalidate(prefix) 事件总线：前缀匹配 → at=null 标记过期；仍有订阅者（refcount>0）的 key
//    立即触发刷新，无订阅者的只标记过期不发请求（旧值保留——重订阅时按 SWR 先返旧值再后台刷新）。

const registry = new Map(); // key -> entry

function now() {
  return Date.now();
}

function isStale(entry) {
  const ttl = entry.opts.ttl || 0;
  return entry.at != null && ttl > 0 && now() - entry.at >= ttl;
}

// 触发一次刷新（in-flight 去重：同 key 已有在途请求 → 共享同一 Promise）。
// 失败时保留旧值与 lastError，向调用方 rethrow。
async function refresh(key) {
  const entry = registry.get(key);
  if (!entry) throw new Error(`queryRegistry: unknown key "${key}"`);
  if (entry.inFlight) return entry.inFlight;

  const p = Promise.resolve()
    .then(() => entry.fetcher())
    .then((data) => {
      entry.data = data;
      entry.at = now();
      entry.lastError = null;
      if (entry.inFlight === p) entry.inFlight = null; // 完成才清（失败路径见 catch）
      notify(entry);
      return data;
    })
    .catch((err) => {
      entry.lastError = err;
      if (entry.inFlight === p) entry.inFlight = null;
      throw err;
    });
  entry.inFlight = p;
  return p;
}

function notify(entry) {
  for (const fn of [...entry.listeners]) {
    try { fn(); } catch { /* 订阅者异常不影响引擎与其他订阅者 */ }
  }
}

// interval 定时器：refcount>0 且 opts.interval>0 时运行；间隔变化则重建。
function ensureTimer(entry) {
  const iv = entry.opts.interval || 0;
  if (!iv) return;
  if (entry.timer && entry.timer.iv === iv) return;
  if (entry.timer) clearInterval(entry.timer.id);
  entry.timer = {
    iv,
    id: setInterval(() => { refresh(entry.key).catch(() => {}); }, iv),
  };
}

// 订阅一个 key。返回 entry（含 data/at/inFlight）。
// - 首次订阅或缓存过期 → 立即触发刷新（SWR：调用方此刻读到的可能是旧值/undefined）；
// - TTL 内且已有缓存 → 不发请求（会话缓存语义）。
export function subscribe(key, fetcher, opts = {}) {
  let entry = registry.get(key);
  if (!entry) {
    entry = {
      key,
      data: undefined,
      at: null,          // 最近一次成功时间戳（null=从未成功）
      inFlight: null,    // 在途 Promise（去重锚点）
      timer: null,       // interval 定时器句柄
      refcount: 0,
      listeners: new Set(),
      fetcher,
      opts,
      lastError: null,
    };
    registry.set(key, entry);
  }
  entry.refcount += 1;
  // 重复订阅（如 key 切换后切回）：更新 fetcher/opts，保证后续刷新用最新闭包
  entry.fetcher = fetcher;
  entry.opts = opts;
  ensureTimer(entry);
  if (entry.at == null || isStale(entry)) {
    refresh(key).catch(() => {}); // 首次/过期：后台刷新，错误由 lastError 承接
  }
  return entry;
}

// 取消订阅。refcount=0 → 停定时器（缓存保留供会话内重订阅）。
export function unsubscribe(key) {
  const entry = registry.get(key);
  if (!entry) return;
  entry.refcount -= 1;
  if (entry.refcount <= 0) {
    entry.refcount = 0;
    if (entry.timer) { clearInterval(entry.timer.id); entry.timer = null; }
  }
}

// 事件总线：前缀失效。at=null 标记过期；refcount>0 → 立即后台刷新，否则仅清缓存。
export function invalidate(prefix = "") {
  for (const [key, entry] of [...registry]) {
    if (!key.startsWith(prefix)) continue;
    entry.at = null;
    if (entry.refcount > 0) refresh(key).catch(() => {});
  }
}

// 读取某 key 的当前缓存（不订阅、不触发请求）。无 → undefined。
export function peek(key) {
  const entry = registry.get(key);
  return entry ? entry.data : undefined;
}

// 测试专用：清空整个注册表（含定时器）。生产代码不得调用。
export function __reset() {
  for (const entry of registry.values()) {
    if (entry.timer) clearInterval(entry.timer.id);
  }
  registry.clear();
}
