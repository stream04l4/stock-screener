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
    expect(byName.sina.find(".lake-spp-avail").text().trim()).toBe("✓ 可达");
    expect(byName.tencent.find(".lake-spp-avail").classes()).toContain("bad");
    expect(byName.tencent.find(".lake-spp-avail").text().trim()).toBe("✗ 不可达");
    expect(byName.baostock.find(".lake-spp-avail").text().trim()).toBe("— 未探测");
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

// ===========================================================================
// v6.1.3：区块2"数据源状态"（每源一张卡；pool.sources 驱动）+ 配额列已删守卫
// ===========================================================================
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
function vueSrc(rel) {
  return readFileSync(resolve(__dirname, rel), "utf-8");
}

// v6.1.3 形态：pool.sources（后端数组，固定 5 源 + provides/quota/rate_limit/enabled）
function makeSourcesPool(over = {}) {
  const base = makePool();   // 保留 by_source/conflict/baostock_probe/stale（区块1/3/4 不变）
  return {
    ...base,
    sources: [
      { name: "sina", enabled: true, available: true, probed_at: "2026-09-17T08:00:00",
        latency_ms: 320, authority: 0,
        provides: [
          { table: "kline_daily", table_cn: "T2 日K线", field_group: "ohlcv_amount", role: "主源" },
          { table: "kline_daily", table_cn: "T2 日K线", field_group: "adj_factor", role: "主源(推导)" },
        ], quota: null, rate_limit: "≥1s/股" },
      { name: "tencent", enabled: true, available: false, probed_at: "2026-09-17T08:00:01",
        latency_ms: null, authority: 1,
        provides: [
          { table: "stock_master", table_cn: "T1 股票主档", field_group: "master", role: "主源" },
          { table: "valuation_daily", table_cn: "T3 估值日线", field_group: "valuation", role: "主源" },
        ], quota: null, rate_limit: null },
      { name: "baostock", enabled: true, available: null, probed_at: null,
        latency_ms: null, authority: 2,
        provides: [
          { table: "kline_daily", table_cn: "T2 日K线", field_group: "adj_factor", role: "fallback(探测存活时)" },
        ], quota: { used_today: 1234, budget: 5000 }, rate_limit: null },
      { name: "tdx", enabled: false, available: true, probed_at: "2026-09-17T08:00:02",
        latency_ms: 210, authority: 3,
        provides: [
          { table: "index_daily", table_cn: "T7 指数日线", field_group: "amount", role: "主源" },
        ], quota: null, rate_limit: "≥0.5s/股" },
      { name: "adata_f10", enabled: true, available: true, probed_at: "2026-09-17T08:00:03",
        latency_ms: 88, authority: 3,
        provides: [
          { table: "fundamentals_quarterly", table_cn: "T5 季度基本面", field_group: "f10", role: "主源" },
        ], quota: null, rate_limit: "≥1s/股" },
    ],
    ...over,
  };
}

function cardsBy(w) {
  const byName = {};
  for (const c of w.findAll(".lake-spp-adapter")) byName[c.find("b").text()] = c;
  return byName;
}

describe("SourcePoolPanel v6.1.3：数据源状态卡（pool.sources）", () => {
  it("区块标题='数据源状态（连通性·数据类型·配额 · 灌数启动时探测）'，5 源各一张卡", () => {
    const w = mountPanel({ pool: makeSourcesPool() });
    const block = w.findAll(".lake-spp-block").find((b) => b.text().includes("数据源状态"));
    expect(block.exists()).toBe(true);
    expect(block.find(".lake-spp-title").text()).toContain("连通性·数据类型·配额");
    expect(w.findAll(".lake-spp-adapter").length).toBe(5);
  });

  it("卡内四行：连通性（✓可达/✗不可达/—未探测 + latency）+ 探测时间", () => {
    const w = mountPanel({ pool: makeSourcesPool() });
    const byName = cardsBy(w);
    expect(byName.sina.find(".lake-spp-avail").text().trim()).toBe("✓ 可达");
    expect(byName.sina.text()).toContain("latency 320ms");
    expect(byName.tencent.find(".lake-spp-avail").text().trim()).toBe("✗ 不可达");
    expect(byName.baostock.find(".lake-spp-avail").text().trim()).toBe("— 未探测");
    // 行4 探测时间（probed_at / 未探测）
    expect(byName.sina.text()).toContain("探测：2026-09-17T08:00:00");
    expect(byName.baostock.text()).toContain("探测：未探测");
  });

  it("数据类型 chips（table_cn·字段组 + role 后缀小字 muted）", () => {
    const w = mountPanel({ pool: makeSourcesPool() });
    const byName = cardsBy(w);
    // sina：2 个 chip（OHLCV+amount 主源 / 复权因子 主源(推导)）
    const sinaChips = byName.sina.findAll(".lake-spp-chip");
    expect(sinaChips.length).toBe(2);
    expect(sinaChips[0].text()).toContain("T2 日K线·OHLCV+amount");
    expect(sinaChips[0].text()).toContain("主源");
    expect(sinaChips[1].text()).toContain("复权因子");
    expect(sinaChips[1].text()).toContain("主源(推导)");
    // tencent：T1 股票主档·主档（master→"主档"）+ T3 估值日线·估值
    const txText = byName.tencent.findAll(".lake-spp-chip").map((c) => c.text()).join("|");
    expect(txText).toContain("T1 股票主档·主档");
    expect(txText).toContain("T3 估值日线·估值");
    // role 后缀是小字 muted（span.muted.small）
    const roleSpan = sinaChips[0].find("span.muted");
    expect(roleSpan.exists()).toBe(true);
    expect(roleSpan.text().trim()).toBe("主源");
  });

  it("配额行：baostock='今日 1234/5000'；sina='≥1s/股'；tencent（无 quota 无 rate_limit）='无官方配额'", () => {
    const w = mountPanel({ pool: makeSourcesPool() });
    const byName = cardsBy(w);
    expect(byName.baostock.text()).toContain("配额：今日 1234/5000");
    expect(byName.sina.text()).toContain("配额：≥1s/股");
    expect(byName.tencent.text()).toContain("配额：无官方配额");
  });

  it("enabled=false → '已禁用'灰 badge 替代 ✓/✗（连通性行不渲染）", () => {
    const w = mountPanel({ pool: makeSourcesPool() });
    const byName = cardsBy(w);
    const tdx = byName.tdx;
    expect(tdx.find(".lake-spp-disabled").text()).toBe("已禁用");
    expect(tdx.find(".lake-spp-conn").exists()).toBe(false);   // 连通性行被 badge 替代
    // 其余 enabled=true 源无"已禁用"badge
    expect(byName.sina.find(".lake-spp-disabled").exists()).toBe(false);
  });

  it("baostock quota.used_today=null → '今日 —/5000'（从未灌数）", () => {
    const pool = makeSourcesPool();
    pool.sources[2].quota = { used_today: null, budget: 5000 };
    const w = mountPanel({ pool });
    expect(cardsBy(w).baostock.text()).toContain("配额：今日 —/5000");
  });

  it("SOURCE_COLORS 左边框着色（sina 蓝 #2563eb / tdx 青 #06b6d4）", () => {
    const w = mountPanel({ pool: makeSourcesPool() });
    const byName = cardsBy(w);
    expect(byName.sina.attributes("style")).toContain("#2563eb");
    expect(byName.tdx.attributes("style")).toContain("#06b6d4");
  });

  it("stale=true → 数据源状态区块出现'探测数据过期'角标（与 by_source 区共 2 处）", () => {
    const w = mountPanel({ pool: makeSourcesPool({ stale: true }) });
    expect(w.findAll(".lake-spp-stale").length).toBe(2);
  });

  it("旧形态（无 sources 键，仅 adapters）→ 回退渲染 5 卡 + '无官方配额'占位", () => {
    const w = mountPanel({ pool: makePool() });   // makePool 只有 adapters，无 sources
    expect(w.findAll(".lake-spp-adapter").length).toBe(5);
    const byName = cardsBy(w);
    expect(byName.sina.text()).toContain("✓ 可达");
    expect(byName.sina.text()).toContain("配额：无官方配额");   // rate_limit=null → 占位
  });
});

describe("v6.1.3：配额(今日)列已删（源码契约守卫）", () => {
  it("LakeTab.vue / LakeStatusCard.vue 不含'配额(今日)'th/td，SyncControl.vue 不含'今日配额'", () => {
    const lakeTab = vueSrc("../../LakeTab.vue");
    const card = vueSrc("../LakeStatusCard.vue");
    const sc = vueSrc("../SyncControl.vue");
    expect(lakeTab).not.toContain("配额(今日)");
    expect(card).not.toContain("配额(今日)");
    // tasks 表 quota td 一并移除（不再渲染 t.quota_used_today）
    expect(lakeTab).not.toContain("quota_used_today");
    expect(card).not.toContain("quota_used_today");
    // SyncControl 头部"今日配额 x/budget"片段已删（保留已耗时/进度更新时间）
    expect(sc).not.toContain("今日配额");
    expect(sc).toContain("进度更新于");   // 保留项仍在
  });

  it("lakeStore.js confirm 文案=v6.1.6 全量补齐（历史+增量+基本面，幂等可中断续传）", () => {
    const store = vueSrc("../../../stores/lakeStore.js");
    // v6.1.6：三按钮合并为单按钮——confirm 文案改为全量补齐（旧"全史数据补库/BaoStock
    // 每日配额 5000"文案随 mode 参数一并删除）。
    expect(store).toContain(
      "将启动全量数据补齐（历史+增量+基本面，后台长跑，幂等可中断续传）。确认启动？");
    expect(store).not.toContain("BaoStock 每日配额 5000 到顶自停");
  });
});
