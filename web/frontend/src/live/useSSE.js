// useSSE —— EventSource 封装（报告 §2.2）：组件不直接碰 EventSource。
//
// - 断线续传靠浏览器原生行为：EventSource 重连自动携带 Last-Event-ID（服务端按字节偏移补发，
//   契约见 web/app.py run_events），本 hook 不自行管理 offset。
// - 连续失败计数：每次 onerror +1；onopen 成功归零。达到 maxFails（默认 3）→ close() +
//   调 opts.onDowngrade（调用方降级，如回退 3s 轮询 /status——保留 v1 兜底语义）。
// - 生命周期：setup 时立即 open；onUnmounted 自动 close。
import { getCurrentInstance, onUnmounted, ref } from "vue";

export function useSSE(url, onEvent, opts = {}) {
  const failCount = ref(0);
  const alive = ref(false);
  let es = null;
  let closedByUser = false;

  function open() {
    if (closedByUser) return;
    try {
      es = new EventSource(url);
    } catch {
      // EventSource 构造失败（极少数环境）→ 直接降级
      opts.onDowngrade && opts.onDowngrade();
      return;
    }
    es.onopen = () => { failCount.value = 0; alive.value = true; };
    es.onmessage = (ev) => {
      if (closedByUser) return;
      onEvent({ lastId: ev.lastEventId || null, data: ev.data });
    };
    es.onerror = () => {
      failCount.value += 1;
      if (failCount.value >= (opts.maxFails || 3)) {
        close();
        opts.onDowngrade && opts.onDowngrade();
      }
    };
  }

  function close() {
    closedByUser = true;
    alive.value = false;
    if (es) { try { es.close(); } catch { /* ignore */ } es = null; }
  }

  open();
  // D2：store（taskStore）等非组件上下文调用时跳过 unmount 钩子——close() 由调用方显式管理
  // （生命周期 = SPA 会话）。组件上下文行为与 D1 完全一致。
  if (getCurrentInstance()) onUnmounted(close);

  return { failCount, alive, close };
}
