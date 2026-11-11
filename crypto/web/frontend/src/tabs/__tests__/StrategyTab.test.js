// StrategyTab（v7.0.50 编辑模式，与 A 股 tab 同语义）：
// 默认只读 → 编辑模式解锁 → 保存锁死（400 保持编辑态）/ 取消 / 恢复默认。
import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import StrategyTab from "../StrategyTab.vue";

const PARAMS = {
  first_drop_pct: 70, add_grid_pct: 1, base_amount_usdt: 10,
  growth: 1.05, take_profit_mult: 10, fee_bps: 10, lookback_h: 8760,
};
const RULE = { first: "建仓规则", add: "加仓规则", hold: "持仓规则", exit: "止盈规则" };

function getBody(overrides = {}) {
  return { ready: true, params: PARAMS, defaults: PARAMS, has_override: false,
           rule: RULE, ...overrides };
}

// fetch mock：按 method 分派 /crypto/api/strategy
function mockFetch({ get = getBody(), post, postReset } = {}) {
  const fn = vi.fn(async (path, opts) => {
    const method = (opts && opts.method) || "GET";
    if (path === "/crypto/api/strategy" && method === "GET") {
      const b = typeof get === "function" ? get() : get;
      return { ok: true, status: 200, statusText: "", json: async () => b };
    }
    if (path === "/crypto/api/strategy" && method === "POST") {
      const b = post ? post(JSON.parse(opts.body)) : { ok: true, params: PARAMS, has_override: true };
      return { ok: true, status: 200, statusText: "", json: async () => b };
    }
    if (path === "/crypto/api/strategy/reset" && method === "POST") {
      const b = postReset || { ok: true, params: PARAMS, has_override: false };
      return { ok: true, status: 200, statusText: "", json: async () => b };
    }
    return { ok: false, status: 404, statusText: "", json: async () => ({}) };
  });
  vi.stubGlobal("fetch", fn);
  return fn;
}

function mountTab() {
  return mount(StrategyTab);
}
function btn(w, text) {
  const b = w.findAll("button").find((x) => x.text().includes(text));
  // 找不到 → 返回恒假包装（exists() 为 false），断言 .exists() 不炸
  return b || { exists: () => false, text: () => "" };
}

describe("StrategyTab（编辑模式）", () => {
  beforeEach(() => vi.unstubAllGlobals());

  it("默认只读：无输入框，显示 7 个参数值 + 编辑模式按钮", async () => {
    mockFetch();
    const w = mountTab();
    await flushPromises();
    expect(w.findAll('input[type="number"]').length).toBe(0);
    expect(w.findAll(".val").length).toBe(7);
    expect(w.text()).toContain("建仓规则");
    expect(w.text()).toContain("70");
    expect(btn(w, "编辑模式").exists()).toBe(true);
    expect(btn(w, "保存策略").exists()).toBe(false);
    expect(btn(w, "取消").exists()).toBe(false);
  });

  it("进入编辑模式：7 个输入框 + 保存策略/取消 出现", async () => {
    mockFetch();
    const w = mountTab();
    await flushPromises();
    await btn(w, "编辑模式").trigger("click");
    await flushPromises();
    expect(w.findAll('input[type="number"]').length).toBe(7);
    expect(btn(w, "编辑模式").exists()).toBe(false);
    expect(btn(w, "保存策略").exists()).toBe(true);
    expect(btn(w, "取消").exists()).toBe(true);
  });

  it("保存 → POST 成功后锁死（回只读 + 提示已保存 + 自定义标记）", async () => {
    const fn = mockFetch({
      get: () => getBody({ has_override: true,
                           params: { ...PARAMS, first_drop_pct: 50 } }),
      post: (body) => ({ ok: true,
                        params: { ...PARAMS, first_drop_pct: body.params.first_drop_pct },
                        has_override: true }),
    });
    const w = mountTab();
    await flushPromises();
    await btn(w, "编辑模式").trigger("click");
    await flushPromises();
    await w.findAll('input[type="number"]')[0].setValue("50");
    await btn(w, "保存策略").trigger("click");
    await flushPromises();
    const posts = fn.mock.calls.filter((c) => c[1] && c[1].method === "POST");
    expect(posts.length).toBe(1);
    expect(JSON.parse(posts[0][1].body).params.first_drop_pct).toBe(50);
    expect(w.findAll('input[type="number"]').length).toBe(0); // 锁死
    expect(w.text()).toContain("策略已保存");
    expect(w.text()).toContain("页面自定义");
    expect(btn(w, "恢复默认").exists()).toBe(true);
  });

  it("400 拒绝 → 保持编辑态并回显错误（可改正重试）", async () => {
    const fn = vi.fn(async (path, opts) => {
      const method = (opts && opts.method) || "GET";
      if (method === "POST" && path === "/crypto/api/strategy") {
        return { ok: false, status: 400, statusText: "",
                 json: async () => ({ detail: "参数 growth=5 超出范围 [1, 3]" }) };
      }
      return { ok: true, status: 200, statusText: "", json: async () => getBody() };
    });
    vi.stubGlobal("fetch", fn);
    const w = mountTab();
    await flushPromises();
    await btn(w, "编辑模式").trigger("click");
    await flushPromises();
    await btn(w, "保存策略").trigger("click");
    await flushPromises();
    expect(w.findAll('input[type="number"]').length).toBe(7); // 仍编辑态
    expect(w.text()).toContain("超出范围");
  });

  it("取消 → 不发 POST，回只读并重拉", async () => {
    const fn = mockFetch();
    const w = mountTab();
    await flushPromises();
    const getBefore = fn.mock.calls.filter((c) => !(c[1] && c[1].method === "POST")).length;
    await btn(w, "编辑模式").trigger("click");
    await flushPromises();
    await w.findAll('input[type="number"]')[0].setValue("99");
    await btn(w, "取消").trigger("click");
    await flushPromises();
    const posts = fn.mock.calls.filter((c) => c[1] && c[1].method === "POST");
    expect(posts.length).toBe(0);
    expect(w.findAll('input[type="number"]').length).toBe(0); // 回只读
    expect(fn.mock.calls.length).toBeGreaterThan(getBefore);  // 重拉了 GET
  });

  it("恢复默认 → POST reset，只读视图回 yaml 默认", async () => {
    const fn = mockFetch({
      get: () => getBody({ has_override: true }),
    });
    const w = mountTab();
    await flushPromises();
    expect(btn(w, "恢复默认").exists()).toBe(true);
    await btn(w, "恢复默认").trigger("click");
    await flushPromises();
    const resets = fn.mock.calls.filter((c) => c[0].includes("/reset"));
    expect(resets.length).toBe(1);
    expect(w.text()).toContain("已恢复 crypto.yaml 默认参数");
  });
});
