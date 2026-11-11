// StrategyTab（v7.0.48）：参数表单渲染 + 保存/重置流程（mock fetch，/crypto 前缀）。
import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import StrategyTab from "../StrategyTab.vue";

const PARAMS = {
  first_drop_pct: 70, add_grid_pct: 1, base_amount_usdt: 10,
  growth: 1.05, take_profit_mult: 10, fee_bps: 10, lookback_h: 8760,
};

function mockFetch(map) {
  const fn = vi.fn(async (path) => {
    const r = map[path] || { ok: false, status: 404, body: {} };
    return { ok: r.ok, status: r.status, statusText: "", json: async () => r.body };
  });
  vi.stubGlobal("fetch", fn);
  return fn;
}

describe("StrategyTab", () => {
  beforeEach(() => vi.unstubAllGlobals());

  it("渲染 7 个参数字段与规则（GET /api/strategy）", async () => {
    mockFetch({
      "/crypto/api/strategy": {
        ok: true, status: 200,
        body: { ready: true, params: PARAMS, defaults: PARAMS,
                has_override: false,
                rule: { first: "建仓规则", add: "加仓规则", hold: "持仓规则",
                        exit: "止盈规则" } },
      },
    });
    const w = mount(StrategyTab);
    await flushPromises();
    expect(w.findAll('input[type="number"]').length).toBe(7);
    expect(w.text()).toContain("建仓规则");
    expect(w.text()).toContain("止盈规则");
    expect(w.text()).toContain("crypto.yaml 默认参数");
    expect(w.text()).toContain("保存参数");
  });

  it("保存参数 → POST 成功后显示已保存 + 自定义标记", async () => {
    const fn = vi.fn(async (path, opts) => {
      if (path === "/crypto/api/strategy" && opts && opts.method === "POST") {
        return { ok: true, status: 200, json: async () => ({
          ok: true, params: { ...PARAMS, first_drop_pct: 50 },
          has_override: true,
        }) };
      }
      if (path === "/crypto/api/strategy") {
        return { ok: true, status: 200, json: async () => ({
          ready: true, params: PARAMS, defaults: PARAMS, has_override: false,
          rule: { first: "a", add: "b", hold: "c", exit: "d" },
        }) };
      }
      return { ok: false, status: 404, json: async () => ({}) };
    });
    vi.stubGlobal("fetch", fn);
    const w = mount(StrategyTab);
    await flushPromises();
    await w.findAll('input[type="number"]')[0].setValue("50");
    await w.find("button.primary").trigger("click");
    await flushPromises();
    const postCalls = fn.mock.calls.filter((c) => c[1] && c[1].method === "POST");
    expect(postCalls.length).toBe(1);
    expect(JSON.parse(postCalls[0][1].body).params.first_drop_pct).toBe(50);
    expect(w.text()).toContain("已保存 ✓");
    expect(w.text()).toContain("页面自定义参数生效");
    expect(w.text()).toContain("恢复默认");
  });
});
