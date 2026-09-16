// useSSE 单测：连续失败计数 → 降级回调（3 次 onerror → close + onDowngrade）。
// Last-Event-ID 断线续传为浏览器 EventSource 原生行为（服务端按字节偏移补发，契约见
// web/app.py run_events），happy-dom 无真实网络不可测——此处只验证 hook 的降级/生命周期语义。
import { describe, it, expect, vi, beforeEach } from "vitest";
import { createApp, h } from "vue";
import { useSSE } from "../useSSE.js";

class FakeEventSource {
  static instances = [];
  constructor(url) {
    this.url = url;
    this.readyState = 0;
    this.closed = false;
    this.onopen = null;
    this.onmessage = null;
    this.onerror = null;
    FakeEventSource.instances.push(this);
  }
  close() {
    this.closed = true;
    this.readyState = 2;
  }
}

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

beforeEach(() => {
  FakeEventSource.instances = [];
  globalThis.EventSource = FakeEventSource;
});

describe("useSSE", () => {
  it("setup 时立即以给定 URL 建立连接", () => {
    mountHook(() => useSSE("/api/runs/web_x/events", () => {}));
    expect(FakeEventSource.instances.length).toBe(1);
    expect(FakeEventSource.instances[0].url).toBe("/api/runs/web_x/events");
  });

  it("onopen → failCount 归零、alive=true；onmessage 透传 data/lastId", () => {
    const seen = [];
    const { get } = mountHook(() => useSSE("u", (ev) => seen.push(ev)));
    const es = FakeEventSource.instances[0];

    es.onerror(); // 先失败一次
    expect(get().failCount.value).toBe(1);
    es.onopen();  // 重连成功 → 归零
    expect(get().failCount.value).toBe(0);
    expect(get().alive.value).toBe(true);

    es.onmessage({ data: '{"type":"log"}', lastEventId: "42" });
    expect(seen).toEqual([{ data: '{"type":"log"}', lastId: "42" }]);
  });

  it("连续 3 次 onerror → close + onDowngrade（只调一次），不再计数", () => {
    const downgrade = vi.fn();
    const { get } = mountHook(() => useSSE("u", () => {}, { onDowngrade: downgrade }));
    const es = FakeEventSource.instances[0];

    es.onerror();
    es.onerror();
    expect(es.closed).toBe(false); // 2 次未降级（EventSource 自带重连）
    expect(downgrade).not.toHaveBeenCalled();

    es.onerror(); // 第 3 次 → 通道不可用判定成立
    expect(es.closed).toBe(true);
    expect(get().alive.value).toBe(false);
    expect(downgrade).toHaveBeenCalledTimes(1);
  });

  it("组件卸载 → close（生命周期）", () => {
    const { app } = mountHook(() => useSSE("u", () => {}));
    const es = FakeEventSource.instances[0];
    expect(es.closed).toBe(false);
    app.unmount();
    expect(es.closed).toBe(true);
  });

  it("maxFails 可配（1 次即降级）", () => {
    const downgrade = vi.fn();
    mountHook(() => useSSE("u", () => {}, { maxFails: 1, onDowngrade: downgrade }));
    FakeEventSource.instances[0].onerror();
    expect(downgrade).toHaveBeenCalledTimes(1);
    expect(FakeEventSource.instances[0].closed).toBe(true);
  });
});
