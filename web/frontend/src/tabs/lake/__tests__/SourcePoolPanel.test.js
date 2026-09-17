// SourcePoolPanel 单测（brief D3：v6.1 多源资源池面板渲染）。
// 覆盖：by_source 堆叠条（9表/固定色板/百分比宽度）、adapters 三态（✓/✗/未知—）、
//       stale "探测数据过期"角标、conflict_rows total+非零逐行、baostock_probe alive badge、
//       backfill_in_progress / pool=null → 整块"灌数中暂不可用"降级。
import { describe, it, expect } from "vitest";
import { mount } from "@vue/test-utils";
import SourcePoolPanel from "../SourcePoolPanel.vue";

// 9 表恒定键（与后端 _build_source_pool 一致）
const NINE = ["stock_master", "kline_daily", "valuation_daily", "dividend_events",
  "fundamentals_quarterly", "holders_snapshot", "index_daily", "factor_snapshot", "macro_rf"];

function makePool(over = {}) {
  const by_source = {};
  for (const t of NINE) by_source[t] = {};
  by_source.kline_daily = { sina: 100, tencent: 50 };   // 混合源（生产真实形态）
  by_source.stock_master = { local: 5400 };
  const adapters = {
    sina: { available: true, probed_at: "2026-09-17T08:00:00", latency_ms: 120 },
    tencent: { available: false, probed_at: "2026-09-17T08:00:01", latency_ms: null },
    baostock: { available: null, probed_at: null, latency_ms: null },   // 未知—
    tdx: { available: true, probed_at: "2026-09-17T08:00:02", latency_ms: 3400 },
    adata_f10: { available: true, probed_at: "2026-09-17T08:00:03", latency_ms: 88 },
  };
  return {
    by_source,
    conflict_rows: { stock_master: 0, kline_daily: 12, valuation_daily: 3, dividend_events: 0,
      fundamentals_quarterly: 0, holders_snapshot: 0, index_daily: 0, total: 15 },
    adapters,
    baostock_probe: { alive: false, at: "2026-09-17T08:01:00", elapsed_s: 3.2, detail: "黑名单用户" },
    stale: false,
    ...over,
  };
}

function mountPanel(props) {
  return mount(SourcePoolPanel, { props });
}

describe("SourcePoolPanel：by_source 堆叠条", () => {
  it("9 表恒定行（无数据表 total=0 也渲染）", () => {
    const w = mountPanel({ pool: makePool() });
    const rows = w.findAll(".lake-spp-row");
    expect(rows.length).toBe(9);
  });

  it("混合源 kline_daily：两段宽度按占比（100/50 → 66.67%/33.33%）+ 固定色板", () => {
    const w = mountPanel({ pool: makePool() });
    const row = w.findAll(".lake-spp-row").find((r) => r.text().includes("T2 日K线"));
    const segs = row.findAll(".lake-spp-bar i");
    expect(segs.length).toBe(2);
    expect(segs[0].attributes("style")).toContain("width: 66.66666666666666%");
    expect(segs[0].attributes("style")).toContain("#2563eb");   // sina 蓝
    expect(segs[1].attributes("style")).toContain("#f97316");   // tencent 橙
    expect(row.find(".lake-spp-row-total").text()).toBe("150");
  });

  it("未知 source（legacy）→ 灰色兜底", () => {
    const pool = makePool();
    pool.by_source.index_daily = { legacy_src: 7 };
    const w = mountPanel({ pool });
    const row = w.findAll(".lake-spp-row").find((r) => r.text().includes("T7 指数日线"));
    expect(row.find(".lake-spp-bar i").attributes("style")).toContain("#cbd5e1");
  });
});

describe("SourcePoolPanel：adapters 三态", () => {
  it("available=true → ✓（绿）/ false → ✗（红）/ null → —（未知）", () => {
    const w = mountPanel({ pool: makePool() });
    const cards = w.findAll(".lake-spp-adapter");
    expect(cards.length).toBe(5);
    const byName = {};
    for (const c of cards) byName[c.find("b").text()] = c;
    expect(byName.sina.find(".lake-spp-avail").classes()).toContain("ok");
    expect(byName.sina.find(".lake-spp-avail").text().trim()).toBe("✓");
    expect(byName.tencent.find(".lake-spp-avail").classes()).toContain("bad");
    expect(byName.tencent.find(".lake-spp-avail").text().trim()).toBe("✗");
    expect(byName.baostock.find(".lake-spp-avail").text().trim()).toBe("—");
  });

  it("probed_at + latency_ms 展示（≥1000ms → s 单位）", () => {
    const w = mountPanel({ pool: makePool() });
    const cards = w.findAll(".lake-spp-adapter");
    const byName = {};
    for (const c of cards) byName[c.find("b").text()] = c;
    expect(byName.sina.text()).toContain("2026-09-17T08:00:00");
    expect(byName.sina.text()).toContain("latency 120ms");
    expect(byName.tdx.text()).toContain("latency 3.4s");
    expect(byName.baostock.text()).toContain("未探测");
  });
});

describe("SourcePoolPanel：stale 角标", () => {
  it("stale=true → by_source/adapters 区块各出现'探测数据过期'", () => {
    const w = mountPanel({ pool: makePool({ stale: true }) });
    const badges = w.findAll(".lake-spp-stale");
    expect(badges.length).toBe(2);
    for (const b of badges) expect(b.text()).toBe("探测数据过期");
  });

  it("stale=false → 无角标", () => {
    const w = mountPanel({ pool: makePool() });
    expect(w.findAll(".lake-spp-stale").length).toBe(0);
  });
});

describe("SourcePoolPanel：conflict_rows + baostock_probe", () => {
  it("total=15（非零表逐行：kline_daily 12 / valuation_daily 3；零值表不列出）", () => {
    const w = mountPanel({ pool: makePool() });
    const block = w.findAll(".lake-spp-block").find((b) => b.text().includes("跨源分歧"));
    expect(block.text()).toContain("total 15");
    expect(block.text()).toContain("kline_daily: 12 行");
    expect(block.text()).toContain("valuation_daily: 3 行");
    expect(block.text()).not.toContain("stock_master: 0");
  });

  it("全零 → '无分歧'文案 + fresh 徽章", () => {
    const pool = makePool();
    pool.conflict_rows = { total: 0, kline_daily: 0 };
    const w = mountPanel({ pool });
    const block = w.findAll(".lake-spp-block").find((b) => b.text().includes("跨源分歧"));
    expect(block.text()).toContain("无分歧");
    expect(block.find(".badge").classes()).toContain("lake-st-fresh");
  });

  it("baostock_probe dead → ✗ dead badge + at/elapsed/detail", () => {
    const w = mountPanel({ pool: makePool() });
    const block = w.findAll(".lake-spp-block").find((b) => b.text().includes("BaoStock 恢复探测"));
    expect(block.find(".badge").text()).toBe("✗ dead");
    expect(block.text()).toContain("2026-09-17T08:01:00");
    expect(block.text()).toContain("3.2s");
    expect(block.text()).toContain("黑名单用户");
  });

  it("baostock_probe=null → '未探测'占位", () => {
    const w = mountPanel({ pool: makePool({ baostock_probe: null }) });
    const block = w.findAll(".lake-spp-block").find((b) => b.text().includes("BaoStock 恢复探测"));
    expect(block.text()).toContain("未探测");
  });
});

describe("SourcePoolPanel：backfill 降级", () => {
  it("backfill_in_progress=true → 整块'灌数中暂不可用'（四区块不渲染）", () => {
    const w = mountPanel({ pool: makePool(), backfill: true });
    expect(w.find(".lake-spp-off").text()).toContain("灌数中暂不可用");
    expect(w.findAll(".lake-spp-row").length).toBe(0);
    expect(w.findAll(".lake-spp-adapter").length).toBe(0);
  });

  it("pool=null（locked 态无 source_pool 键）→ 同样降级", () => {
    const w = mountPanel({ pool: null, backfill: false });
    expect(w.find(".lake-spp-off").exists()).toBe(true);
    expect(w.findAll(".lake-spp-row").length).toBe(0);
  });
});
