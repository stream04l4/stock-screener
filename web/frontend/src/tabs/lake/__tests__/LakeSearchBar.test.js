// LakeSearchBar（v8.0.9）：端点换 /api/stocks/search 同口径模糊匹配
// （代码数字 / 中文名 / 拼音首字母 / 全拼）+ include_delisted=1（对齐旧
// /api/lake/search 全表语义）+ 503 lake_locked 中性处理（不弹红横幅）。
// mock 点：api client（零网络）。
import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";

const apiMock = vi.fn();
vi.mock("../../../api/client.js", () => ({
  api: (...a) => apiMock(...a),
  isBackfillErr: (e) => !!(e && e.status === 409
    && e.body && e.body.error === "lake_backfill_in_progress"),
}));

const LakeSearchBar = (await import("../LakeSearchBar.vue")).default;

const RESULTS = [
  { ts_code: "sh.601398", name: "工商银行", industry: "银行", delisted: false },
  { ts_code: "sz.300001", name: "特退股", industry: "测试", delisted: true },
];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function typeAndSettle(text) {
  const w = mount(LakeSearchBar);
  await w.find("#lake-search-input").setValue(text);
  await sleep(320); // 防抖 250ms
  return w;
}

beforeEach(() => {
  apiMock.mockReset();
  apiMock.mockImplementation(async (path) => {
    if (path.startsWith("/api/stocks/search?")) return { q: "x", results: RESULTS };
    throw new Error("unmocked: " + path);
  });
});

describe("LakeSearchBar · v8.0.9 同口径模糊搜索", () => {
  it("中文名输入 → 请求 /api/stocks/search?...&include_delisted=1 → 候选渲染（industry/退市标记）", async () => {
    const w = await typeAndSettle("工行");
    expect(apiMock.mock.calls[0][0])
      .toBe("/api/stocks/search?q=" + encodeURIComponent("工行") + "&include_delisted=1");
    const items = w.findAll(".sr-item");
    expect(items.length).toBe(2);
    expect(items[0].text()).toContain("sh.601398");
    expect(items[0].text()).toContain("银行");
    expect(items[1].text()).toContain("（退市）");
    w.unmount();
  });

  it("选中候选 → emit select(ts_code) + 下拉收起", async () => {
    const w = await typeAndSettle("工行");
    await w.findAll(".sr-item")[0].trigger("click");
    expect(w.emitted("select")).toBeTruthy();
    expect(w.emitted("select")[0]).toEqual(["sh.601398"]);
    expect(w.find(".lake-search-results").exists()).toBe(false);
    w.unmount();
  });

  it("空查询 / 纯数字 <3 位 → 不发请求、不下拉（服务端 400 同规则前置）", async () => {
    let w = await typeAndSettle("");
    expect(apiMock.mock.calls.length).toBe(0);
    expect(w.find(".lake-search-results").exists()).toBe(false);
    w.unmount();
    w = await typeAndSettle("60");
    expect(apiMock.mock.calls.length).toBe(0);
    w.unmount();
  });

  it("6 位代码 → 正常请求", async () => {
    const w = await typeAndSettle("601398");
    expect(apiMock.mock.calls[0][0])
      .toBe("/api/stocks/search?q=601398&include_delisted=1");
    w.unmount();
  });

  it("503 lake_locked → 中性处理（不 emit error，不弹红横幅）", async () => {
    apiMock.mockRejectedValue(Object.assign(new Error("lake locked"),
      { status: 503, body: { detail: { error: "lake_locked", message: "x" } } }));
    const w = await typeAndSettle("工行");
    expect(w.emitted("error")).toBeUndefined();
    expect(w.find(".lake-search-results").exists()).toBe(false);
    w.unmount();
  });

  it("其余错误 → emit error（父组件红横幅）", async () => {
    apiMock.mockRejectedValue(Object.assign(new Error("boom"), { status: 500, body: null }));
    const w = await typeAndSettle("工行");
    expect(w.emitted("error")).toBeTruthy();
    expect(w.emitted("error")[0][0]).toBe("boom");
    w.unmount();
  });
});
