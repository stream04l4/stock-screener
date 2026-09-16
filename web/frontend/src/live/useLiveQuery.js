// useLiveQuery —— 统一刷新引擎的 Vue hook（报告 §2.2）。
//
// 用法：const { data, loading, error } = useLiveQuery("runs", () => api("/api/runs"), { ttl: 30000 });
// - key/fetcher 可为静态值或 getter（如 () => `run:${day.value}`）；key 变化时自动
//   换订阅（旧 key 退订、新 key 按 TTL/会话缓存语义决定是否发请求）。
// - 生命周期：onMounted 订阅 / onUnmounted 退订（引用计数，refcount=0 停定时器）。
// - SWR：TTL 过期时 data 先返旧值（loading 不翻转），后台刷新完成后自动更新。
import { onMounted, onUnmounted, ref, watch } from "vue";
import { subscribe, unsubscribe } from "./queryRegistry.js";

export function useLiveQuery(keyOrGetter, fetcher, opts = {}) {
  const keyRef = typeof keyOrGetter === "function" ? keyOrGetter : () => keyOrGetter;

  const data = ref(undefined);
  // loading 仅在"还没有任何数据且无错误"时为 true（首次加载）；
  // SWR 后台刷新不翻转 loading（旧值照常展示，无闪烁）。
  const loading = ref(true);
  const error = ref(null);

  let entry = null;
  let currentKey = null;

  function sync() {
    if (!entry) return;
    data.value = entry.data;
    error.value = entry.lastError || null;
    loading.value = entry.data === undefined && !entry.lastError;
  }

  function detach() {
    if (!entry) return;
    entry.listeners.delete(sync);
    unsubscribe(currentKey);
    entry = null;
    currentKey = null;
  }

  function attach() {
    const key = keyRef();
    if (key == null) { detach(); return; } // 无 key（如未选中运行日）→ 不订阅不发请求
    detach();
    currentKey = key;
    entry = subscribe(key, fetcher, opts);
    entry.listeners.add(sync);
    sync();
  }

  onMounted(attach);
  onUnmounted(detach);
  watch(keyRef, (k) => { if (k != null) attach(); else detach(); });

  return { data, loading, error };
}
