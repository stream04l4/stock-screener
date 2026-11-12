// PoolMembers（候选池成员表）回归：v8.0.11 分红占比/上涨占比拆解列 +
// 数值列小数收敛（总市值(亿) 此前裸显示 2165.50348228，Joel 反馈小数太多）。
// mock 点：api client + useLiveQuery（立即调 fetcher，零网络）。
import { describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { ref } from "vue";

const apiMock = vi.fn();
vi.mock("../../../api/client.js", () => ({
  api: (...a) => apiMock(...a),
  isBackfillErr: (e) => !!(e && e.status === 409 && e.body && e.error === "lake_backfill_in_progress"),
}));
vi.mock("../../../live/useLiveQuery.js", () => ({
  useLiveQuery: (keyOrGetter, fetcher) => {
    const data = ref(null);
    const loading = ref(false);
    const error = ref(null);
    Promise.resolve().then(async () => {
      try { data.value = await fetcher(); } catch (e) { error.value = e; }
    });
    return { data, loading, error };
  },
}));

const PoolMembers = (await import("../PoolMembers.vue")).default;

// 服务端 bt_*_share 已是百分数数值（44.04 = 44.04%）；
// 全精度金额（2165.50348228）由展示层截断。
const POOL = {
  funnel: { total: 5000, whitelist: 5000, soe: 2200, mv: 420, listing: 400,
            div: 320, backtest: 170, yield: 70, price: 70, final: 70, manual: 0 },
  n_members: 2,
  members: [
    { ts_code: "sh.601088", name: "中国神华", industry_csric2: "B06",
      soe_basis: "国有股性质", total_mv: 2165.50348228, ttm_yield_pct: 7.752,
      n_bars: 2600, list_date: "2007-10-09",
      bt_pnl_pct: 311.89, bt_n_buys: 9,
      bt_div_pool: 17101.019641512157, bt_price_pnl: 58924.06479999999,
      bt_div_share: 22.49, bt_price_share: 77.51,
      price_latest: 24.35678, is_manual: false },
    { ts_code: "sz.000651", name: "格力电器", industry_csric2: "C38",
      soe_basis: "手动添加", total_mv: 2165.5, ttm_yield_pct: 7.75,
      n_bars: 2500, list_date: "1996-11-18",
      bt_pnl_pct: null, bt_n_buys: null,
      bt_div_pool: null, bt_price_pnl: null,
      bt_div_share: null, bt_price_share: null,   // 手动/不可拆 → —
      price_latest: 37.1437, is_manual: true },
  ],
};

function mk() {
  setActivePinia(createPinia());
  apiMock.mockImplementation(async (path) => {
    if (path === "/api/pool") return JSON.parse(JSON.stringify(POOL));
    throw new Error("unmocked: " + path);
  });
  return mount(PoolMembers, { global: { plugins: [createPinia()] } });
}

describe("PoolMembers · 分红占比/上涨占比 + 小数收敛（v8.0.11）", () => {
  it("新列渲染：占比百分数（2 位 + %）+ 金额 2 位；总市值截断（Joel：小数太多）", async () => {
    const w = mk();
    await flushPromises();
    const t = w.text();
    // 列头
    for (const h of ["10年回测%", "分红占比", "上涨占比", "分红余额", "价差盈亏",
                     "股息率%", "最新价", "总市值(亿)"]) {
      expect(t).toContain(h);
    }
    // 神华：占比 22.49% / 77.51%；金额 2 位
    expect(t).toContain("22.49%");
    expect(t).toContain("77.51%");
    expect(t).toContain("17101.02");
    expect(t).toContain("58924.06");
    // 回测/股息率 百分数显示
    expect(t).toContain("311.89%");
    expect(t).toContain("7.75%");
    // 总市值 2165.50348228 → 2165.50（全精度长小数不得出现）
    expect(t).toContain("2165.50");
    expect(t).not.toContain("2165.50348228");
    // 最新价 24.35678 → 24.36
    expect(t).toContain("24.36");
    expect(t).not.toContain("24.35678");
    w.unmount();
  });

  it("不可拆成员（占比 null）→ 占比/金额列显示 —（不崩）", async () => {
    const w = mk();
    await flushPromises();
    const t = w.text();
    expect(t).toContain("格力电器");
    expect(t).toContain("—");   // 手动股占比/分红余额/价差盈亏列
    w.unmount();
  });
});
