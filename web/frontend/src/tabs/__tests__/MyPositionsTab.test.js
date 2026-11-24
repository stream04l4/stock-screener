// MyPositionsTab 组件回归（v7.0.1，原 QueryTab.test.js 重命名重写）：
// 我的持仓 = 用户账本（/api/my_positions GET）+ 录入条/弹层编辑（PUT）+ 策略参考列 + 买入历史。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const apiMock = vi.fn();
vi.mock("../../api/client.js", () => ({
  api: (...a) => apiMock(...a),
  isBackfillErr: (e) => !!(e && e.status === 409 && e.body && e.error === "lake_backfill_in_progress"),
}));
vi.mock("../../live/queryRegistry.js", async (orig) => ({ ...(await orig()) }));

const MyPositionsTab = (await import("../MyPositionsTab.vue")).default;
const { useTaskStore } = await import("../../stores/taskStore.js");
const { invalidate, __reset } = await import("../../live/queryRegistry.js");

const MY = {
  as_of: "2026-10-21",
  n: 1,
  signals: [],
  strategy: "current",
  positions: [
    { ts_code: "sh.601088", name: "中国神华", shares: 1200, cost_price: 25.0,
      last_price: 33.9, market_value: 40680, pnl: 10680, pnl_pct: 36.0,
      updated_at: "2026-10-22T01:00:00Z",
      since_date: "2026-10-20",
      buys: [{ date: "2026-10-20", shares: 1200, price: 25.0 }],
      in_pos: true, strat_shares: 12345.67, k_level: 0,
      next_add_price: 115.79, next_rearm_price: 179.61,
      next_add_price_raw: 42.07, next_rearm_price_raw: 56.34, end_date: "2026-10-21" },
  ],
  totals: { market_value: 40680, cost: 30000, pnl: 10680, pnl_pct: 35.6 },
};

const MY_SIGNAL = {
  ...JSON.parse(JSON.stringify(MY)),
  price_source: "spot",
  spot_time: "20261022150500",
  signals: [
    { ts_code: "sh.601088", name: "中国神华", price: 40.0, next_add_price: 42.07,
      k_level: 0, next_level: 1, suggested_lots: 1, action: "add" },
  ],
};

const MY_BUY = {
  ...JSON.parse(JSON.stringify(MY)),
  price_source: "spot",
  spot_time: "20261022150500",
  n: 2,
  positions: [
    ...JSON.parse(JSON.stringify(MY.positions)),
    { ts_code: "sh.600941", name: "中国移动", shares: 200, cost_price: 95.0,
      last_price: 96.59, market_value: 19318, pnl: 318, pnl_pct: 1.67,
      updated_at: "2026-10-22T01:00:00Z",
      in_pos: false, strat_shares: null, k_level: 0,
      next_add_price: 72.36, next_rearm_price: null,
      next_add_price_raw: 72.36, next_rearm_price_raw: null, end_date: "2026-10-21" },
  ],
  signals: [
    { ts_code: "sh.600941", name: "中国移动", price: 70.0, next_add_price: 72.36,
      k_level: 0, next_level: 1, suggested_lots: 1, action: "buy" },
  ],
  totals: { market_value: 60000, cost: 50000, pnl: 10000, pnl_pct: 20.0 },
};

const MY_EMPTY = { as_of: null, n: 0, positions: [],
  totals: { market_value: 0, cost: 0, pnl: 0, pnl_pct: null } };

// v7.0.19 策略信号中心 fixture（v7.0.23 口径更新）：
// active 5（持仓加仓 + 非持仓首仓时机过 + 非持仓模拟已建仓时机过【大秦：
// 非持仓恒走首仓线，价格态 active + 时机✓ → 当前信号·买入首仓】+ 非持仓
// 时机未过【江苏银行：不入当前信号段，v7.0.23 时机门】+ 非持仓模拟已建仓
// 时机未过【紫金矿业：不入段】）/ 1 touched（持仓盘中触碰已触回）/
// 3 即将进入价格带（v7.0.22：持仓 1 + 非持仓时机过 1 + 非持仓时机未过 1
// 【不入段】）/ 4 历史（v8.2.5d：前端仅显示 pushed=true 的 1 条——微信推送
// 过的真实触发信号；含 1 条未推送触碰事件，被滤出）
// v8.2.5（Joel 2026-11-24 定案，分区重规整）+ v8.2.5b 行着色：
// 「当前信号」= 信号区（action 非空）。classify_signal 对 approaching
// 也返回 action 预告 → 未到线行也在本段，位置不变、行着色区分：浅红
// sig-row-hit = 真正触发（active/touched，其中时机门全过 = 微信推送
// 信号），浅灰 sig-row-near = 即将触发（approaching）。fixture 中信特钢
// （+2.01% approaching + action=buy）即浅灰行实案。11-23「touched 保留
// 到当日」作废——触线但时机未过（action 空）不入段（云南铜业 → 潜力段）。
// 「潜力信号」（原「即将进入」）满足任一即入段：① 价格距触发线 ≤5%
// （绝对距离，含已破线）② 5年分位 ≤50%（无下界，含已过门 ≤40%）；与
// 「当前」互斥。fixture 分布：
// - ① 入段：云南铜业（+1.1% touched）/ 紫金矿业（-2.7%）/ 中国海油（+2.5%）/
//   山煤国际（+2.1%）/ 国电电力（+2.1%）/ 长江电力（+2.0%）
// - ② 入段：中国石化（45%；价 -5.6% 带外）
// - ①② 入段：山煤国际（45%）/ 长江电力（38% 已过门）
// - 不入段：江苏银行（-5.9% 带外 + 62.3%>50）/ 中粮糖业（-12.2% + 76.8%）
const SIG = {
  as_of: "2026-11-06", strategy: "current", lake_locked: false, approach_pct: 5,
  spot_preselect_pct: 10, spot_fetched: 9,
  price_source: "spot", spot_time: "20261107093000", n_stocks: 12,
  history_window_days: 90,
  stocks: [
    { ts_code: "sh.601088", name: "中国神华", held: true, in_pos: true, k_level: 1,
      price: 40.0, next_add_price_raw: 42.07, status: "active", touched: false,
      day_low: 39.8, distance_pct: -4.9,
      action: "add", suggested_lots: 2, n_history: 2, end_date: "2026-11-06",
      timing_ok: null, timing_reasons: null, bt_pnl_pct: 88.5, bt_n_buys: 6 },
    { ts_code: "sh.600941", name: "中国移动", held: false, in_pos: false, k_level: 0,
      price: 70.0, next_add_price_raw: 72.36, status: "active", touched: false,
      day_low: 69.5, distance_pct: -3.26,
      action: "buy", suggested_lots: 1, n_history: 0, end_date: "2026-11-06",
      timing_ok: true, timing_reasons: "✓ 5年分位 21.9%（需≤40%） · ✓ 股息率 4.59%（需≥4.5%）",
      bt_pnl_pct: 45.2, bt_n_buys: 4 },
    // v7.0.23 反例（实例化）：非持仓 + 时机 ✗✗ → 价格态 active 也不入
    // 「当前信号」段（首仓双条件缺时机门；v7.0.22「即将进入」同门）
    { ts_code: "sh.600988", name: "江苏银行", held: false, in_pos: false, k_level: 0,
      price: 8.0, next_add_price_raw: 8.5, status: "active", touched: false,
      day_low: 7.9, distance_pct: -5.9,
      action: null, suggested_lots: null, n_history: 0, end_date: "2026-11-06",
      timing_ok: false, timing_reasons: "✗ 5年分位 62.3%（需≤40%） · ✗ 股息率 2.10%（需≥4.5%）",
      bt_pnl_pct: null, bt_n_buys: null },
    { ts_code: "sh.601985", name: "中国核电", held: true, in_pos: true, k_level: 1,
      price: 10.2, next_add_price_raw: 10.0, status: "touched", touched: true,
      day_low: 9.96, distance_pct: 2.0,
      action: "add", suggested_lots: 2, n_history: 0, end_date: "2026-11-06",
      timing_ok: null, timing_reasons: null, bt_pnl_pct: 12.3, bt_n_buys: 5 },
    // v8.2.3（Joel 2026-11-23 轮次口径，山煤实案更新）：持仓 k=0（再武装·
    // 二次建仓位）盘中触线（touched）但时机门未过（分位 62.3%>40%）→
    // action=null → 仍保留在「当前信号」段至次日（信号事实保留），
    // 动作列 = 「时机未过」（v8.2.0「持仓已够」抑制已退役）
    { ts_code: "sh.600362", name: "云南铜业", held: true, in_pos: true, k_level: 0,
      price: 18.2, next_add_price_raw: 18.0, status: "touched", touched: true,
      day_low: 17.9, distance_pct: 1.1,
      action: null, suggested_lots: null, next_lots: 4, n_history: 1,
      end_date: "2026-11-06", timing_ok: false,
      timing_reasons: "✗ 5年分位 62.3%（需≤40%）",
      new_round: true, round_no: null,
      bt_pnl_pct: null, bt_n_buys: null },
    // 非持仓 + 模拟已建仓（in_pos=true）+ 时机 ✗✗：不入当前信号段
    //（v7.0.23 时机门；触碰记录走历史）
    { ts_code: "sh.601899", name: "紫金矿业", held: false, in_pos: true, k_level: 2,
      price: 18.0, next_add_price_raw: 18.5, status: "active", touched: false,
      day_low: 17.9, distance_pct: -2.7,
      action: null, suggested_lots: null, n_history: 0, end_date: "2026-11-06",
      timing_ok: false, timing_reasons: "✗ 5年分位 70.0%（需≤40%） · ✗ 股息率 1.50%（需≥4.5%）",
      bt_pnl_pct: null, bt_n_buys: null },
    // v7.0.23 正例（大秦铁路实例）：非持仓 + 模拟已建仓（in_pos=true k=3）
    // → 触发线恒为**首仓线** 5.2811（不再 next 档 4.3491）；价 4.88 ≤ 5.2811
    // 且时机✓（19.1%/4.58%）→ 当前信号·买入·首仓（10 万）
    { ts_code: "sh.601006", name: "大秦铁路", held: false, in_pos: true, k_level: 3,
      price: 4.88, next_add_price_raw: 5.2811, status: "active", touched: false,
      day_low: 4.75, distance_pct: -7.59,
      action: "buy", suggested_lots: 1, n_history: 0, end_date: "2026-11-06",
      timing_ok: true, timing_reasons: "✓ 5年分位 19.1%（需≤40%） · ✓ 股息率 4.58%（需≥4.5%）",
      bt_pnl_pct: 120.4, bt_n_buys: 7 },
    // v8.2.5b（中信特钢实案，Joel 2026-11-24）：非持仓 + in_pos=true（策略
    // 已建仓）+ 价未到线（+2.01% approaching）→ classify_signal 对
    // approaching 也返回 action 预告（「买入·首仓 2 手」到线执行）→ 仍入
    // 「当前」段（Joel：位置不变）但标浅灰「即将触发」sig-row-near；真正
    // 触发行（active/touched）标浅红 sig-row-hit——着色即区分
    { ts_code: "sz.000708", name: "中信特钢", held: false, in_pos: true, k_level: 1,
      price: 12.60, next_add_price_raw: 12.3519, status: "approaching", touched: false,
      day_low: 12.46, distance_pct: 2.01,
      action: "buy", suggested_lots: 2, n_history: 0, end_date: "2026-11-23",
      timing_ok: true, timing_pctile: 33.2,
      timing_reasons: "✓ 5年分位 33.2%（实时，需≤40%）",
      bt_pnl_pct: 31.05, bt_n_buys: 19 },
    // v8.2.5：未到线（价在线上方 +2.0%）→ action 必为 null（旧 fixture 的
    // action:"add" 与 status=approaching 矛盾，已修正）
    { ts_code: "sh.600900", name: "长江电力", held: true, in_pos: true, k_level: 0,
      price: 28.56, next_add_price_raw: 28.0, status: "approaching", touched: false,
      day_low: 27.9, distance_pct: 2.0,
      action: null, suggested_lots: null, next_lots: 1, n_history: 1, end_date: "2026-11-06",
      timing_ok: true, timing_pctile: 38.0,
      timing_reasons: "✓ 5年分位 38.0%（需≤40%）",
      bt_pnl_pct: 30.1, bt_n_buys: 3 },
    // v8.2.5：非持仓 + 时机达标（33.5%）+ 价格临区（+2.0% 未到线 →
    // action 必为 null，旧 fixture 的 "buy" 已修正）→ 潜力段 ①+②
    { ts_code: "sh.601328", name: "交通银行", held: false, in_pos: false, k_level: 0,
      price: 7.55, next_add_price_raw: 7.40, status: "approaching", touched: false,
      day_low: 7.5, distance_pct: 2.0,
      action: null, suggested_lots: null, next_lots: null, n_history: 0,
      end_date: "2026-11-06",
      timing_ok: true, timing_pctile: 33.5,
      timing_reasons: "✓ 5年分位 33.5%（需≤40%） · ✓ 股息率 5.02%（需≥4.5%）",
      bt_pnl_pct: null, bt_n_buys: null },
    // v7.0.22 反例（中国海油实例）：非持仓（模拟已建仓 in_pos=true）+ 价格临区
    // 但时机 ✗✗（分位 85.7% > 50% 放宽带）→ 不入「即将进入」段
    { ts_code: "sh.600938", name: "中国海油", held: false, in_pos: true, k_level: 0,
      price: 31.07, next_add_price_raw: 30.3159, status: "approaching", touched: false,
      day_low: 30.8, distance_pct: 2.49,
      action: null, suggested_lots: null, n_history: 10, end_date: "2026-11-06",
      timing_ok: false, timing_pctile: 85.7,
      timing_reasons: "✗ 5年分位 85.7%（需≤40%） · ✗ 股息率 4.17%（需≥4.5%）",
      bt_pnl_pct: 87.28, bt_n_buys: 10 },
    // v7.0.31 C 案（山煤国际实例，Joel 定案）：非持仓 + 价格临区 + 分位 45%
    //（>40% 时机未过、≤50% 放宽带内）→ 展示并标「未过」——原口径下翻转前
    // 完全不可见
    { ts_code: "sh.600546", name: "山煤国际", held: false, in_pos: false, k_level: 0,
      price: 11.01, next_add_price_raw: 11.2478, status: "approaching", touched: false,
      day_low: 10.96, distance_pct: 2.14,
      action: null, suggested_lots: null, next_lots: 1, n_history: 0,
      end_date: "2026-11-06",
      timing_ok: false, timing_pctile: 45.0,
      timing_reasons: "✗ 5年分位 45.0%（需≤40%）",
      bt_pnl_pct: null, bt_n_buys: null },
    // v8.2.0 补（Joel 2026-11-13，中粮糖业实案）：持仓 + 再武装后新一轮首仓
    // （in_pos k=0 = 二次建仓位）+ 价已在区但时机门未过（分位 76.8% > 50%
    // 放宽带）→ 无信号：不入「当前信号」，也不入「即将进入」（远离时机线
    // = 两区均不展示，避免噪音）
    { ts_code: "sh.600737", name: "中粮糖业", held: true, in_pos: true, k_level: 0,
      price: 11.94, next_add_price_raw: 13.5913, status: "active", touched: false,
      day_low: 11.8, distance_pct: -12.2,
      action: null, suggested_lots: null, n_history: 0, end_date: "2026-11-06",
      timing_ok: false, timing_pctile: 76.8,
      timing_reasons: "✗ 实时 5年分位 76.8%（需≤40%，池外现算）",
      bt_pnl_pct: null, bt_n_buys: null },
    // v8.2.0 补（中国石化实例）：同上但分位 45% ∈ (40%, 50%] = 临近时机线
    // → 入「即将进入」段标「临近时机线」（分位回落到 ≤40% 当晚即出信号）
    { ts_code: "sh.600500", name: "中国石化", held: true, in_pos: true, k_level: 0,
      price: 6.12, next_add_price_raw: 6.48, status: "active", touched: false,
      day_low: 6.1, distance_pct: -5.6,
      action: null, suggested_lots: null, next_lots: 1, n_history: 0,
      end_date: "2026-11-06",
      timing_ok: false, timing_pctile: 45.0,
      timing_reasons: "✗ 5年分位 45.0%（需≤40%）",
      bt_pnl_pct: null, bt_n_buys: null },
    // v8.2.0 补三分类反例（国电电力实例）：持仓 k=0 再武装位 + 价格临区
    // （5% 带）但分位 76.8% 远离时机线（>50%）→ 不入段（旧 v7.0.22「持仓
    // approaching 恒展示」随 k=0 时机门废除：双条件信号时机远离 = 噪音）
    { ts_code: "sh.600795", name: "国电电力", held: true, in_pos: true, k_level: 0,
      price: 6.72, next_add_price_raw: 6.58, status: "approaching", touched: false,
      day_low: 6.68, distance_pct: 2.1,
      action: null, suggested_lots: null, n_history: 0, end_date: "2026-11-06",
      timing_ok: false, timing_pctile: 76.8,
      timing_reasons: "✗ 5年分位 76.8%（需≤40%）",
      bt_pnl_pct: null, bt_n_buys: null },
  ],
  history: [
    { ts_code: "sh.601088", name: "中国神华", held: true, date: "2026-11-02",
      price: 43.1, amount: 43100, level: 1, first_of_round: false, pushed: true, lots: 2 },
    { ts_code: "sh.601985", name: "中国核电", held: true, date: "2026-11-05",
      price: 10.0, amount: 20000, level: 1, first_of_round: false, pushed: false, lots: 2,
      source: "touch" },
    { ts_code: "sh.601088", name: "中国神华", held: true, date: "2026-10-20",
      price: 45.0, amount: 45000, level: 0, first_of_round: true, pushed: false, lots: 1 },
    { ts_code: "sh.600900", name: "长江电力", held: true, date: "2026-10-11",
      price: 29.0, amount: 29000, level: 0, first_of_round: true, pushed: false, lots: 1 },
  ],
  totals: { n_stocks: 11, n_active: 5, n_touched: 2, n_approaching: 4, n_idle: 0,
            n_no_data: 0, n_active_held: 1, n_active_unheld: 4, n_history: 4 },
};
const SIG_EMPTY = { as_of: null, strategy: "current", lake_locked: false, approach_pct: 5,
  spot_preselect_pct: 10, spot_fetched: 0,
  price_source: "lake", spot_time: null, n_stocks: 0, stocks: [], history: [],
  totals: { n_stocks: 0, n_active: 0, n_touched: 0, n_approaching: 0, n_idle: 0,
            n_no_data: 0, n_active_held: 0, n_active_unheld: 0, n_history: 0 } };

function mockApi(my, trades = [], signals = SIG_EMPTY) {
  apiMock.mockImplementation(async (path, opts = {}) => {
    if (path.startsWith("/api/my_positions") && (!opts || !opts.method || opts.method === "GET")) {
      return JSON.parse(JSON.stringify(my));
    }
    if (path.startsWith("/api/my_positions") && opts && opts.method === "PUT") {
      const body = JSON.parse(opts.body);
      return { ok: true, ts_code: body.ts_code, n: 1 };
    }
    // v7.0.17 交易登记：按传入账本 my 模拟服务端记账（买加权 / 卖减股 / 清仓移除）
    if (path === "/api/my_trades" && opts && opts.method === "POST") {
      const body = JSON.parse(opts.body);
      const pos0 = (my.positions || []).find((p) => p.ts_code === body.ts_code) || {};
      const oldShares = pos0.shares || 0;
      const oldCost = pos0.cost_price || 0;
      const ns = body.shares;
      let newShares, newCost, removed = false;
      if (ns > 0) {
        newShares = oldShares + ns;
        newCost = Math.round(((oldShares * oldCost + ns * body.price) / newShares) * 1000) / 1000;
      } else {
        newShares = Math.max(oldShares + ns, 0);
        newCost = oldCost;
        removed = oldShares + ns <= 0;
      }
      const trade = {
        id: "t" + (trades.length + 1) + "00",
        ts_code: body.ts_code, name: body.name || "",
        direction: ns > 0 ? "buy" : "sell", shares: Math.abs(ns), price: body.price,
        amount: Math.abs(ns) * body.price, date: "2026-10-26",
        realized_pnl: ns < 0 ? Math.round((body.price - oldCost) * Math.abs(ns) * 100) / 100 : null,
        created_at: "2026-10-26T01:00:00Z",
      };
      trades.unshift(trade);
      return { ok: true, trade, position: { ts_code: body.ts_code, shares: newShares,
        cost_price: removed ? null : newCost, removed } };
    }
    if (path.startsWith("/api/my_trades?") ) {
      const code = decodeURIComponent((path.split("ts_code=")[1] || "").trim());
      const rows = trades.filter((t) => t.ts_code === code);
      return { n: rows.length, trades: JSON.parse(JSON.stringify(rows)) };
    }
    if (path.startsWith("/api/my_trades/") && opts && opts.method === "DELETE") {
      const id = decodeURIComponent(path.split("/").pop());
      const i = trades.findIndex((t) => t.id === id);
      if (i < 0) { const e = new Error("record not found"); e.status = 404; throw e; }
      trades.splice(i, 1);
      return { ok: true, n: trades.length };
    }
    if (path.startsWith("/api/strategy_signals")) {
      return JSON.parse(JSON.stringify(signals));
    }
    if (path.startsWith("/api/stocks/search")) {
      const q = decodeURIComponent((path.split("q=")[1] || "").trim());
      const SUG = {
        zgsh: [{ ts_code: "sh.601088", name: "中国神华", industry: "B06煤炭开采和洗选业" }],
        "600900": [{ ts_code: "sh.600900", name: "长江电力", industry: "D44电力、热力生产和供应业" }],
        电力: [{ ts_code: "sh.600900", name: "长江电力", industry: "D44电力、热力生产和供应业" }],
      };
      return { q, results: SUG[q] || [] };
    }
    if (path.startsWith("/api/lake/")) {
      // v8.2.5e：信号行点击弹窗（StockPanoramaCard）取 /api/lake/stock +
      // /api/lake/kline——测试返回 404 → 卡片走「数据湖无此股（T1 未灌入）」
      // 占位分支（不挂 ECharts/canvas）。
      const e = new Error("not found in lake");
      e.status = 404;
      throw e;
    }
    throw new Error("unmocked: " + path);
  });
}

let appPinia = null;
// 测试卫生：记录所有挂载的 wrapper；afterEach 统一 unmount。
// 若某用例断言失败（提前 throw 跳过行内 unmount），泄漏的订阅会 refcount>0，
// 使下一个 beforeEach 的 invalidate 触发"对已重置 mock 的刷新"→ registry 缓存
// 被 undefined 污染且 TTL 新鲜 → 后续挂载直接命中空数据。统一 unmount 杜绝污染。
const mounted = new Set();
const mk = () => {
  const pinia = createPinia();
  appPinia = pinia;
  const w = mount(MyPositionsTab, { attachTo: document.body, global: { plugins: [pinia] } });
  mounted.add(w);
  return w;
};

beforeEach(() => {
  setActivePinia(createPinia());
  apiMock.mockReset();
  // registry 是模块级单例（会话缓存语义）→ 测试间必须清缓存，否则首个测试的
  // 数据（或空数据）会被后续测试的挂载直接命中。v7.0.30 时段门会在
  // 「非时段 + 有缓存」时直接返回旧缓存不发请求，故必须 __reset 清数据（仅
  // invalidate 只打过期标记不清数据，会让下一个测试拿到上一个测试的旧值）。
  __reset();
  invalidate("my_positions");
  invalidate("strategy_signals");
  localStorage.clear(); // 策略选择持久化 → 测试间隔离
  vi.stubGlobal("confirm", vi.fn(() => true));
});

afterEach(() => {
  for (const w of [...mounted]) { try { w.unmount(); } catch { /* 已 unmount */ } }
  mounted.clear();
});

const puts = () => apiMock.mock.calls.filter((c) => c[1] && c[1].method === "PUT");

describe("MyPositionsTab · 渲染", () => {
  it("空账本：标题 + 空态提示，不发 PUT", async () => {
    mockApi(MY_EMPTY);
    const w = mk();
    await flushPromises();
    expect(w.text()).toContain("我的持仓");
    expect(w.text()).toContain("尚未录入持仓");
    expect(puts()).toHaveLength(0);
    w.unmount();
  });

  it("有账本：表格行 + 汇总条（万为单位）+ 数据截至 + 价源标注", async () => {
    mockApi(MY);
    const w = mk();
    await flushPromises();
    expect(w.text()).toContain("中国神华");
    expect(w.text()).toContain("sh.601088");
    expect(w.text()).toContain("¥4.1万"); // 40680 市值
    expect(w.text()).toContain("数据截至 2026-10-21");
    expect(w.text()).toContain("最新收盘（T+1）"); // 无 price_source → 湖价口径（v7.0.40）
    w.unmount();
  });

  it("v8.2.0 补→v8.2.3：触发线首仓/加仓标签 + 再武装价无条件显示", async () => {
    const FIX = {
      ...JSON.parse(JSON.stringify(MY)),
      n: 2,
      positions: [
        { ts_code: "sh.600737", name: "中粮糖业", shares: 400, cost_price: 11.0,
          last_price: 11.0, market_value: 4400, pnl: 0, pnl_pct: 0,
          updated_at: "2026-10-23T01:00:00Z", since_date: "2026-10-23",
          buys: [], in_pos: true, strat_shares: 55968.96,
          strat_shares_since: 0, theory_lots: 0, actual_lots: 4, gap_lots: 0,
          k_level: 0, line_kind: "first",
          next_add_price: 172.7193, next_rearm_price: null,
          next_add_price_raw: 13.5913, next_rearm_price_raw: null,
          end_date: "2026-11-18" },
        { ts_code: "sh.601006", name: "大秦铁路", shares: 100, cost_price: 5.0,
          last_price: 5.0, market_value: 500, pnl: 0, pnl_pct: 0,
          updated_at: "2026-11-01T01:00:00Z", since_date: "2026-11-01",
          buys: [], in_pos: true, strat_shares: 12000,
          strat_shares_since: 1000, theory_lots: 10, actual_lots: 1, gap_lots: 9,
          k_level: 5, line_kind: "add",
          next_add_price: 12.8782, next_rearm_price: 15.7489,
          next_add_price_raw: 4.6549, next_rearm_price_raw: 5.6925,
          end_date: "2026-11-18" },
      ],
      signals: [],
      totals: { market_value: 4900, cost: 4900, pnl: 0, pnl_pct: 0 },
    };
    mockApi(FIX);
    const w = mk();
    await flushPromises();
    // 首仓线标「首仓」、加仓线标「加仓」（价格 2 位小数，全局格式统一）
    expect(w.text()).toContain("13.59 首仓");
    expect(w.text()).toContain("4.65 加仓");
    // v8.2.3：再武装价恢复无条件显示（v8.2.0「策略零买入仓隐藏」规则退役）——
    // 湖中该字段本就为 null → 「—」占位（无 13.0755 系数据本身缺失）
    expect(w.text()).not.toContain("13.0755");
    // 有再武装价的股照显（价格 2 位小数，全局格式统一）
    expect(w.text()).toContain("5.69");
    w.unmount();
  });

  it("v8.2.0 补：首仓线双条件——列显两条件线较低者，弹层展开组成线", async () => {
    const FIX = {
      ...JSON.parse(JSON.stringify(MY)),
      n: 1,
      positions: [{
        ts_code: "sh.600737", name: "中粮糖业", shares: 400, cost_price: 11.0,
        last_price: 11.0, market_value: 4400, pnl: 0, pnl_pct: 0,
        updated_at: "2026-10-23T01:00:00Z", since_date: "2026-10-23",
        buys: [], in_pos: true, strat_shares: 55968.96,
        strat_shares_since: 0, theory_lots: 0, actual_lots: 4, gap_lots: 0,
        k_level: 0, line_kind: "first",
        // 双条件：① 价线 13.5913 ∧ ② 时机线 8.6142 → 有效线 = 较低 8.6142
        // （后端已 min 覆写 next_add_price_raw）
        first_price_line_raw: 13.5913, timing_line_raw: 8.6142,
        next_add_price: 113.05, next_rearm_price: null,
        next_add_price_raw: 8.6142, next_rearm_price_raw: null,
        end_date: "2026-11-18",
      }],
      signals: [],
      totals: { market_value: 4400, cost: 4400, pnl: 0, pnl_pct: 0 },
    };
    mockApi(FIX);
    const w = mk();
    await flushPromises();
    // 列显双条件有效线（较低者）+「首仓」标签
    // v8.2.0 补（全局格式统一）：价格 2 位小数
    expect(w.text()).toContain("8.61 首仓");
    // 单条件价线 13.5913 不再作为触发价展示（仅在弹层组成线出现）
    const row = w.findAll("tbody tr").find((r) => r.text().includes("sh.600737"));
    expect(row.text()).not.toContain("13.5913");
    // 弹层：点击行 → 展开两条组成线 + 双条件标签（价格 2 位）
    await row.trigger("click");
    await flushPromises();
    expect(w.text()).toContain("① 价线（本轮高点×(1−回撤)）");
    expect(w.text()).toContain("13.59");
    expect(w.text()).toContain("② 时机线（5 年≤40% 分位翻线价）");
    expect(w.text()).toContain("下一触发价（双条件·取较低）");
    w.unmount();
  });

  it("lake_locked（灌数持锁降级）→ 琥珀提示条 + 账本仍列出", async () => {
    const locked = {
      ...JSON.parse(JSON.stringify(MY)),
      lake_locked: true,
      as_of: null,
      positions: [{ ...MY.positions[0], last_price: null, market_value: null, pnl: null, pnl_pct: null,
        in_pos: null, strat_shares: null, k_level: null, next_add_price: null, next_rearm_price: null, end_date: null }],
      totals: { market_value: null, cost: 30000, pnl: null, pnl_pct: null },
    };
    mockApi(locked);
    const w = mk();
    await flushPromises();
    expect(w.text()).toContain("灌数");
    expect(w.text()).toContain("中国神华"); // 账本仍可见
    expect(w.text()).not.toContain("¥4.1万"); // 无价格 → 无市值汇总
    w.unmount();
  });
});

describe("MyPositionsTab · 录入条", () => {
  it("非法代码不发 PUT；合法代码 PUT 正确 body（大写小写化）", async () => {
    mockApi(MY_EMPTY);
    const w = mk();
    await flushPromises();
    const inputs = w.findAll(".add-bar input");
    expect(inputs).toHaveLength(4); // 代码/股数/成本价/建仓日（v8.2.0）
    await inputs[0].setValue("601088"); // 缺 sh. 前缀
    await inputs[1].setValue("100");
    await inputs[2].setValue("30");
    await w.find(".add-bar button").trigger("click");
    await flushPromises();
    expect(w.text()).toContain("代码应为");
    expect(puts()).toHaveLength(0);

    await inputs[0].setValue("SH.601088"); // 大写 → 组件内小写化
    await w.find(".add-bar button").trigger("click");
    await flushPromises();
    expect(puts()).toHaveLength(1);
    expect(JSON.parse(puts()[0][1].body)).toEqual(
      { ts_code: "sh.601088", shares: 100, cost_price: 30, since_date: "" });
    expect(w.text()).toContain("已保存 sh.601088");
    w.unmount();
  });
});

describe("MyPositionsTab · 策略应用 + 加仓信号（v7.0.4）", () => {
  it("有信号 → 红色提示条（名称/触发价/档位/建议手数）+ 表格信号列 🔔", async () => {
    mockApi(MY_SIGNAL);
    const w = mk();
    await flushPromises();
    expect(w.find(".signal-banner").exists()).toBe(true);
    expect(w.text()).toContain("信号提示");
    expect(w.text()).toContain("1 只达到触发价位");
    expect(w.text()).toContain("加仓"); // 信号 tag
    expect(w.text()).toContain("中国神华");
    expect(w.text()).toContain("现价 40"); // 现价（实时价，数字渲染为 40）
    expect(w.text()).toContain("42.07"); // 触发价（原始口径）
    expect(w.text()).toContain("实时行情"); // 价源标注
    // v7.0.40：持仓卡价源标注（实时行情 + 时间戳）
    expect(w.text()).toContain("价源：实时行情 20261022150500");
    expect(w.text()).toContain("第 0 档 → 第 1 档");
    expect(w.text()).toContain("1 手"); // v7.0.28 建议手数（k=0 加仓 = 1 手）
    expect(w.text()).toContain("¥0.4万"); // 1 手×100 股×现价 40 → 0.4 万
    // 表格信号列
    expect(w.text()).toContain("🔔 加仓");
    w.unmount();
  });

  it("买入（首仓）信号 → 提示条「买入·首仓」+ 表格信号列 🔔 买入", async () => {
    mockApi(MY_BUY);
    const w = mk();
    await flushPromises();
    expect(w.find(".signal-banner").exists()).toBe(true);
    expect(w.text()).toContain("买入·首仓");
    expect(w.text()).toContain("中国移动");
    expect(w.text()).toContain("现价 70");
    expect(w.text()).toContain("首仓（第 1 档）");
    expect(w.text()).toContain("1 手"); // v7.0.28 首仓 = 1 手
    expect(w.text()).toContain("¥0.7万"); // 1 手×100 股×现价 70 → 0.7 万
    // 表格信号列：只有触发行有标记
    expect(w.text()).toContain("🔔 买入");
    expect(w.text()).not.toContain("🔔 加仓");
    w.unmount();
  });

  it("无信号 → 无提示条，信号列空", async () => {
    mockApi(MY);
    const w = mk();
    await flushPromises();
    expect(w.find(".signal-banner").exists()).toBe(false);
    expect(w.text()).not.toContain("🔔 加仓");
    w.unmount();
  });

  it("v8.2.3 轮次口径：信号条/策略档列轮次前缀 + 多轮理论文案", async () => {
    // round_no=1（用户首轮）+ 多轮理论 20 手 / 现持 12 手（仅展示）
    const pos = JSON.parse(JSON.stringify(MY_SIGNAL.positions[0]));
    pos.round_no = 1; pos.theory_lots = 20; pos.actual_lots = 12; pos.gap_lots = 8;
    const sig = JSON.parse(JSON.stringify(MY_SIGNAL.signals[0]));
    sig.round_no = 1; sig.theory_lots = 20; sig.actual_lots = 12;
    mockApi({ ...MY_SIGNAL, positions: [pos], signals: [sig] });
    const w = mk();
    await flushPromises();
    // 信号条：档位带「首轮」前缀 + 建议补至首轮第 1 档 + 多轮理论文案
    expect(w.text()).toContain("首轮第 0 档 → 第 1 档");
    expect(w.text()).toContain("补至首轮第 1 档");
    expect(w.text()).toContain("多轮理论 20 手 / 现持 12 手");
    // 表格策略档列：首轮第0档（无 round_no 的股仍裸档位）
    expect(w.text()).toContain("首轮第0档");
    w.unmount();
  });

  it("v7.0.29 策略固定当前：无下拉/不发 ?strategy=/不读 ss.mypos.strategy", async () => {
    mockApi(MY);
    localStorage.setItem("ss.mypos.strategy", "保守_低档"); // 旧版残留选择
    const w = mk();
    await flushPromises();
    expect(w.find(".strat-sel select").exists()).toBe(false);
    expect(w.text()).toContain("策略应用：当前策略（strategy.yaml）");
    expect(apiMock.mock.calls.some(([pth]) => pth.startsWith("/api/my_positions?"))).toBe(false);
    expect(apiMock.mock.calls.some(([pth]) => pth.startsWith("/api/strategy_signals?"))).toBe(false);
    expect(apiMock.mock.calls.some(([pth]) => pth === "/api/strategies")).toBe(false);
    expect(localStorage.getItem("ss.mypos.strategy")).toBe("保守_低档"); // 组件不碰该键，原样保留
    w.unmount();
  });
});

describe("MyPositionsTab · 代码模糊搜索（v7.0.3）", () => {
  it("输拼音首字母 → 候选下拉，点击选中回填完整代码", async () => {
    mockApi(MY_EMPTY);
    const w = mk();
    await flushPromises();
    const codeInput = w.find(".code-sug-wrap input");
    await codeInput.setValue("zgsh");
    await vi.waitFor(() => expect(w.find(".code-sug-list").exists()).toBe(true));
    expect(w.text()).toContain("中国神华");
    await w.find(".code-sug-list li").trigger("mousedown");
    expect(codeInput.element.value).toBe("sh.601088");
    expect(w.find(".code-sug-list").exists()).toBe(false);
    expect(puts()).toHaveLength(0); // 选中 ≠ 提交
    w.unmount();
  });

  it("输代码数字 → 候选出现；Enter 确认第一条候选（不发 PUT）", async () => {
    mockApi(MY_EMPTY);
    const w = mk();
    await flushPromises();
    const codeInput = w.find(".code-sug-wrap input");
    await codeInput.setValue("600900");
    await vi.waitFor(() => expect(w.find(".code-sug-list").exists()).toBe(true));
    await codeInput.trigger("keydown", { key: "Enter" });
    expect(codeInput.element.value).toBe("sh.600900");
    expect(w.find(".code-sug-list").exists()).toBe(false);
    expect(puts()).toHaveLength(0);
    w.unmount();
  });

  it("无候选（空结果）→ 不下拉；搜索失败（503）→ 静默降级仍可手动输入", async () => {
    apiMock.mockImplementation(async (path, opts = {}) => {
      if (path === "/api/my_positions" && (!opts || !opts.method || opts.method === "GET")) {
        return JSON.parse(JSON.stringify(MY_EMPTY));
      }
      if (path.startsWith("/api/stocks/search")) {
        const e = new Error("lake locked");
        e.status = 503;
        throw e;
      }
      throw new Error("unmocked: " + path);
    });
    const w = mk();
    await flushPromises();
    const codeInput = w.find(".code-sug-wrap input");
    await codeInput.setValue("电力");
    await new Promise((r) => setTimeout(r, 450)); // debounce + 失败返回
    expect(w.find(".code-sug-list").exists()).toBe(false);
    // 手动输入完整代码不受影响
    await codeInput.setValue("sh.600036");
    expect(codeInput.element.value).toBe("sh.600036");
    w.unmount();
  });

  it("Esc 关闭下拉", async () => {
    mockApi(MY_EMPTY);
    const w = mk();
    await flushPromises();
    const codeInput = w.find(".code-sug-wrap input");
    await codeInput.setValue("zgsh");
    await vi.waitFor(() => expect(w.find(".code-sug-list").exists()).toBe(true));
    await codeInput.trigger("keydown", { key: "Escape" });
    expect(w.find(".code-sug-list").exists()).toBe(false);
    w.unmount();
  });
});

describe("MyPositionsTab · 行点击弹层（策略参考 + 交易登记 + 删除）", () => {
  it("行点击 → 策略参考字段 + 建仓时间线 + 删除 PUT shares=0（弹层无编辑区）", async () => {
    mockApi(MY);
    const w = mk();
    await flushPromises();
    const row = w.findAll("tbody tr").find((r) => r.text().includes("sh.601088"));
    expect(row).toBeTruthy();
    await row.trigger("click");
    await flushPromises();
    expect(w.text()).toContain("下一触发价（首仓/加仓）");
    expect(w.text()).toContain("42.07"); // 弹层展示原始口径（与最新价同单位）
    expect(w.find(".d-row").exists()).toBe(true);
    expect(w.text()).toContain("再武装触发价");
    // v8.2.0：弹层编辑区已移除（更新走顶部录入条），只留提示 + 删除
    expect(w.findAll(".edit-bar input")).toHaveLength(0);
    expect(w.text()).toContain("添加/更新持仓");
    // v8.2.0 建仓时间线（手动加仓自动记录）展示
    expect(w.text()).toContain("建仓时间线");
    expect(w.text()).toContain("2026-10-20");
    // 删除持仓（confirm stub=true）→ PUT shares=0
    const editBtns = w.findAll(".edit-bar button");
    await editBtns.find((b) => b.text() === "删除持仓").trigger("click");
    await flushPromises();
    expect(puts()).toHaveLength(1);
    expect(JSON.parse(puts()[0][1].body)).toEqual(
      { ts_code: "sh.601088", shares: 0, cost_price: 25, since_date: "" });
    w.unmount();
  });

  it("有回测结果（taskStore.lastResults）→ 弹层显示该股逐笔买入历史", async () => {
    mockApi(MY);
    const w = mk();
    const store = useTaskStore(appPinia);
    store.lastResults = {
      taskId: "bt_t9",
      data: { buys: [
        { ts_code: "sh.601088", seq: 1, date: "2018-03-01", price: 20.5, amount: 100000 },
        { ts_code: "sz.000651", seq: 1, date: "2019-01-15", price: 42.1, amount: 100000 },
      ] },
    };
    await flushPromises();
    const row = w.findAll("tbody tr").find((r) => r.text().includes("sh.601088"));
    await row.trigger("click");
    await flushPromises();
    expect(w.text()).toContain("bt_t9");
    expect(w.text()).toContain("2018-03-01"); // 该股的历史买入（另一股 2019-01-15 不显示）
    expect(w.text()).not.toContain("2019-01-15");
    w.unmount();
  });

  it("无回测结果 → 弹层提示去回测页", async () => {
    mockApi(MY);
    const w = mk();
    await flushPromises();
    const row = w.findAll("tbody tr").find((r) => r.text().includes("sh.601088"));
    await row.trigger("click");
    await flushPromises();
    expect(w.text()).toContain("尚无回测结果");
    w.unmount();
  });

  it("关闭：右上角 ✕ 与 Esc 都能关（存量 bug 回归：曾漏绑 @close）", async () => {
    mockApi(MY);
    const w = mk();
    await flushPromises();
    const row = w.findAll("tbody tr").find((r) => r.text().includes("sh.601088"));
    await row.trigger("click");
    await flushPromises();
    expect(w.find(".modal").exists()).toBe(true);
    // ✕ 按钮
    await w.find(".modal-head button").trigger("click");
    await flushPromises();
    expect(w.find(".modal").exists()).toBe(false);
    // 重开 → Esc
    await row.trigger("click");
    await flushPromises();
    expect(w.find(".modal").exists()).toBe(true);
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    await flushPromises();
    expect(w.find(".modal").exists()).toBe(false);
    w.unmount();
  });
});

describe("MyPositionsTab · 弹层交易登记（v7.0.17，买 + 卖）", () => {
  async function openRow(w, code = "sh.601088") {
    const row = w.findAll("tbody tr").find((r) => r.text().includes(code));
    await row.trigger("click");
    await flushPromises();
    return w;
  }
  const buyInputs = (w) => w.findAll(".buy-inputs input");
  const buyBtn = (w) => w.findAll(".buy-bar button").find((b) => b.text() === "登记");
  const posts = () => apiMock.mock.calls.filter((c) => c[1] && c[1].method === "POST");

  it("有信号 → 股数预填建议值（建议手数×100）+ 价格预填最新价", async () => {
    mockApi(MY_SIGNAL); // sh.601088：suggested_lots 1 → 100 股；last_price 33.9
    const w = mk();
    await flushPromises();
    await openRow(w);
    const ins = buyInputs(w);
    expect(ins).toHaveLength(3); // 股数/价格/交易日期（v8.2.0 日期留空=今天）
    expect(ins[0].element.value).toBe("100"); // 1 手 × 100 股
    expect(ins[1].element.value).toBe("33.9"); // 最新价预填
    expect(ins[2].element.value).toBe(""); // 日期默认空（=今天）
    expect(w.find(".buy-preview").exists()).toBe(true);
    w.unmount();
  });

  it("无信号 → 股数不预填，价格仍预填最新价", async () => {
    mockApi(MY);
    const w = mk();
    await flushPromises();
    await openRow(w);
    const ins = buyInputs(w);
    expect(ins[0].element.value).toBe("");
    expect(ins[1].element.value).toBe("33.9");
    w.unmount();
  });

  it("正数登记（买入）→ POST /api/my_trades 加权更新 + 成功文案 + 账本快照同步", async () => {
    mockApi(MY); // 1200 股 @ 25
    const w = mk();
    await flushPromises();
    await openRow(w);
    const ins = buyInputs(w);
    await ins[0].setValue("200");
    await ins[1].setValue("30");
    // 预览：(1200×25 + 200×30) / 1400 = 25.714… → 价格 2 位（全局格式统一）
    expect(w.find(".buy-preview").text()).toContain("1400 股 @ 25.71");
    await buyBtn(w).trigger("click");
    await flushPromises();
    expect(posts()).toHaveLength(1);
    expect(JSON.parse(posts()[0][1].body)).toEqual(
      { ts_code: "sh.601088", shares: 200, price: 30, date: "", name: "中国神华" });
    expect(w.text()).toContain("已登记买入 200 股 @ 30.00 → 1400 股 @ 25.71");
    // 交易历史即时出现新记录（登记后 loadTrades 刷新）
    expect(w.find(".trade-hist").exists()).toBe(true);
    expect(w.text()).toContain("已登记买入");
    w.unmount();
  });

  it("负数登记（卖出）→ POST 负股数 + 预览含已实现盈亏 + 成功文案", async () => {
    mockApi(MY); // 1200 股 @ 25
    const w = mk();
    await flushPromises();
    await openRow(w);
    const ins = buyInputs(w);
    await ins[0].setValue("-200");
    await ins[1].setValue("30");
    // 卖出预览：减股数 + 均价不变 + 已实现盈亏 (30−25)×200 = +1000
    expect(w.find(".buy-preview").text()).toContain("→ 1000 股 @ 25");
    expect(w.find(".buy-preview").text()).toContain("已实现盈亏 +1000.00");
    await buyBtn(w).trigger("click");
    await flushPromises();
    expect(posts()).toHaveLength(1);
    expect(JSON.parse(posts()[0][1].body).shares).toBe(-200);
    expect(w.text()).toContain("已登记卖出 200 股 @ 30.00 → 1000 股 @ 25.00");
    w.unmount();
  });

  it("卖光（负数 = −当前持仓）→ 清仓移除提示", async () => {
    mockApi(MY); // 1200 股
    const w = mk();
    await flushPromises();
    await openRow(w);
    const ins = buyInputs(w);
    await ins[0].setValue("-1200");
    await ins[1].setValue("30");
    await buyBtn(w).trigger("click");
    await flushPromises();
    expect(posts()).toHaveLength(1);
    expect(w.text()).toContain("清仓移除");
    w.unmount();
  });

  it("交易日期：手填历史日期回填（body date）；留空=今天（body date 空串）", async () => {
    mockApi(MY);
    const w = mk();
    await flushPromises();
    await openRow(w);
    const ins = buyInputs(w);
    // 手填历史日期 → 回填
    await ins[0].setValue("200");
    await ins[1].setValue("30");
    await ins[2].setValue("2026-11-01");
    await buyBtn(w).trigger("click");
    await flushPromises();
    expect(posts()).toHaveLength(1);
    expect(JSON.parse(posts()[0][1].body).date).toBe("2026-11-01");
    // 日期成功后清空 → 再登记一笔留空 = 今天
    await ins[0].setValue("100");
    await ins[1].setValue("31");
    await buyBtn(w).trigger("click");
    await flushPromises();
    expect(posts()).toHaveLength(2);
    expect(JSON.parse(posts()[1][1].body).date).toBe("");
    w.unmount();
  });

  it("校验：非整手 / 非整数 / 价格非正 / 卖出超持仓 → 报错不发 POST", async () => {
    mockApi(MY);
    const w = mk();
    await flushPromises();
    await openRow(w);
    const ins = buyInputs(w);

    await ins[0].setValue("150");
    await buyBtn(w).trigger("click");
    await flushPromises();
    expect(w.text()).toContain("整手");
    expect(posts()).toHaveLength(0);

    await ins[0].setValue("abc");
    await buyBtn(w).trigger("click");
    await flushPromises();
    expect(w.text()).toContain("非零整数");
    expect(posts()).toHaveLength(0);

    await ins[0].setValue("0");
    await buyBtn(w).trigger("click");
    await flushPromises();
    expect(w.text()).toContain("非零整数");
    expect(posts()).toHaveLength(0);

    await ins[0].setValue("-2000"); // 超持仓 1200
    await buyBtn(w).trigger("click");
    await flushPromises();
    expect(w.text()).toContain("卖出不能超过持仓");
    expect(posts()).toHaveLength(0);

    await ins[0].setValue("200");
    await ins[1].setValue("0");
    await buyBtn(w).trigger("click");
    await flushPromises();
    expect(w.text()).toContain("本次价格应为正数");
    expect(posts()).toHaveLength(0);
    w.unmount();
  });
});

describe("MyPositionsTab · 弹层交易历史（v7.0.17）", () => {
  const HISTORY = [
    { id: "t200", ts_code: "sh.601088", name: "中国神华", direction: "buy", shares: 200,
      price: 30, amount: 6000, date: "2026-09-10", realized_pnl: null,
      created_at: "2026-09-10T01:00:00Z" },
    { id: "t100", ts_code: "sh.601088", name: "中国神华", direction: "sell", shares: 100,
      price: 20, amount: 2000, date: "2026-08-01", realized_pnl: -500,
      created_at: "2026-08-01T01:00:00Z" },
    { id: "t300", ts_code: "sz.000651", name: "格力电器", direction: "buy", shares: 100,
      price: 40, amount: 4000, date: "2026-09-01", realized_pnl: null,
      created_at: "2026-09-01T01:00:00Z" },
  ];

  it("弹层打开 → 按当前股加载交易历史（买/卖标签 + 已实现盈亏 + 仅本股条目）", async () => {
    mockApi(MY, JSON.parse(JSON.stringify(HISTORY)));
    const w = mk();
    await flushPromises();
    const row = w.findAll("tbody tr").find((r) => r.text().includes("sh.601088"));
    await row.trigger("click");
    await flushPromises();
    expect(w.find(".trade-hist").exists()).toBe(true);
    const rows = w.findAll(".trade-hist tbody tr");
    expect(rows).toHaveLength(2); // sz.000651 的条目不显示
    expect(rows[0].text()).toContain("2026-09-10");
    expect(rows[0].text()).toContain("买");
    expect(rows[1].text()).toContain("2026-08-01");
    expect(rows[1].text()).toContain("卖");
    expect(rows[1].text()).toContain("-500"); // 已实现盈亏（负）
    // 删除按钮每行一个
    expect(w.findAll(".trade-hist tbody button")).toHaveLength(2);
    w.unmount();
  });

  it("删除（confirm）→ DELETE /api/my_trades/{id} + 列表刷新", async () => {
    mockApi(MY, JSON.parse(JSON.stringify(HISTORY)));
    const w = mk();
    await flushPromises();
    const row = w.findAll("tbody tr").find((r) => r.text().includes("sh.601088"));
    await row.trigger("click");
    await flushPromises();
    const delBtns = w.findAll(".trade-hist tbody button");
    await delBtns[0].trigger("click"); // 最新一条 t200（confirm stub=true）
    await flushPromises();
    const dels = apiMock.mock.calls.filter((c) => c[1] && c[1].method === "DELETE");
    expect(dels).toHaveLength(1);
    expect(dels[0][0]).toBe("/api/my_trades/t200");
    expect(w.findAll(".trade-hist tbody tr")).toHaveLength(1); // 剩 t100
    w.unmount();
  });

  it("无交易记录 → 空态提示", async () => {
    mockApi(MY);
    const w = mk();
    await flushPromises();
    const row = w.findAll("tbody tr").find((r) => r.text().includes("sh.601088"));
    await row.trigger("click");
    await flushPromises();
    expect(w.text()).toContain("暂无交易记录");
    expect(w.find(".trade-hist").exists()).toBe(false);
    w.unmount();
  });
});

describe("MyPositionsTab · 策略信号中心 (v7.0.19)", () => {

  it("渲染卡片 + 三档分段计数 + 默认当前信号（持仓加仓/非持仓首仓 + 时机门 + 回测 + 价源）", async () => {
    mockApi(MY, [], SIG);
    const w = mk();
    await flushPromises();
    // 卡片标题 + 覆盖说明 + 价源标注
    expect(w.text()).toContain("📡 策略信号");
    expect(w.text()).toContain("股票池 ∪ 我的持仓");
    expect(w.text()).toContain("距触发线 ≤ 5%"); // approach_pct（v7.0.30 3→5）
    expect(w.text()).toContain("微信推送信号"); // v8.2.5：当前 = 要下单/要推的信号
    expect(w.text()).toContain("其余用最新收盘"); // spot_preselect_pct 实时取价覆盖说明
    expect(w.text()).toContain("近 90 天"); // history_window_days
    expect(w.text()).toContain("实时行情"); // price_source=spot
    // 分段计数（v8.2.5 + v8.2.5b/c）：当前 = action 非空 5 只（神华/核电加仓
    // + 移动/大秦/中信特钢首仓——中信特钢 approaching 未到线，classify
    // 预告 action，行着色浅灰）→ 计数 4/5（4 = 真正触发浅红行：神华/移动
    // active + 核电 touched + 大秦 active；5 = 总行数）；潜力 = ①距线 ≤5%
    // 或 ②分位≤50%：云南铜业/紫金矿业/长江电力/交通银行/中国海油/山煤国际/
    // 国电电力（①）+ 中国石化（②，45%）= 8 只；江苏银行（-5.9% 带外 +
    // 62.3%>50）/ 中粮糖业（-12.2% + 76.8%）不入
    expect(w.text()).toContain("当前信号（4/5）");
    expect(w.text()).toContain("潜力信号（8）");
    expect(w.text()).toContain("历史信号（1）"); // v8.2.5d：仅微信推送过的记录
    // 默认 active：持仓加仓（2 手 → 参考 0.8 万）+ 非持仓首仓（1 手 → 0.7 万）
    expect(w.text()).toContain("● 持仓");
    expect(w.text()).toContain("○ 非持仓");
    expect(w.text()).toContain("加仓");
    expect(w.text()).toContain("买入·首仓");
    expect(w.text()).toContain("2 手");
    expect(w.text()).toContain("1 手");
    expect(w.text()).toContain("0.8");
    expect(w.text()).toContain("0.7");
    // 盘中触碰：已触回标注 + 日内低列（核电行；v8.2.5 后仍属当前段——
    // 它的触线是真信号 action=add）
    expect(w.text()).toContain("· 已触回");
    expect(w.text()).toContain("日内低");
    expect(w.text()).toContain("9.96");
    // v8.2.5：touched 但时机未过（云南铜业 action=null）不再入当前段——
    // 只在潜力信号段展示（见「潜力信号」用例）
    expect(w.text()).not.toContain("云南铜业");
    // v7.0.23 首仓双条件：非持仓 + 时机✓ + 价破首仓线（大秦：in_pos=true
    // 模拟已建仓也走首仓线）→ 入当前信号段；时机 ✗✗ 的（江苏银行/紫金矿业）
    // 价格态 active 也不入段（首仓缺时机门不成信号）
    expect(w.text()).toContain("大秦铁路");
    expect(w.text()).toContain("✓ 5年分位 19.1%（需≤40%） · ✓ 股息率 4.58%（需≥4.5%）");
    expect(w.text()).not.toContain("江苏银行");
    expect(w.text()).not.toContain("紫金矿业");
    // v8.2.0 补（Joel 2026-11-13）：无信号（action=null）不入当前信号——
    // 中粮糖业（持仓二次建仓位·时机未过 76.8%）/ 中国石化（临近时机线 45%）
    // 价格态 active 也不入段（中国石化移入「即将进入」段展示）
    expect(w.text()).not.toContain("中粮糖业");
    expect(w.text()).not.toContain("中国石化");
    // 时机门：逐条件 ✓/✓ 明细（无总判定前缀——全 ✓ 即过、任一 ✗ 即未过）
    expect(w.text()).toContain("✓ 5年分位 21.9%（需≤40%） · ✓ 股息率 4.59%（需≥4.5%）");
    // v8.2.5b 行着色（Joel 2026-11-24）：真正触发行浅红 / 即将触发行浅灰，
    // 位置不变仅底色区分（限定信号卡片内——「我的持仓」表同名单行无此 class）
    const sigCard = w.findAll(".card").find((c) => c.text().includes("策略信号"));
    const actRows = sigCard.findAll("tbody tr");
    const rowOf = (nm) => actRows.find((tr) => tr.text().includes(nm));
    expect(rowOf("中国神华").classes()).toContain("sig-row-hit"); // active 价已破线
    expect(rowOf("中国移动").classes()).toContain("sig-row-hit"); // active
    expect(rowOf("中国核电").classes()).toContain("sig-row-hit"); // touched 盘中已触回弹
    expect(rowOf("大秦铁路").classes()).toContain("sig-row-hit"); // active
    expect(rowOf("中信特钢").classes()).toContain("sig-row-near"); // approaching 未到线
    expect(rowOf("中信特钢").classes()).not.toContain("sig-row-hit");
    expect(rowOf("中信特钢").text()).toContain("买入·首仓");
    expect(rowOf("中信特钢").text()).toContain("2 手");
    // 十年回测列：+88.5% · 6档（持仓神华）/ 无回测 → —
    expect(w.text()).toContain("+88.5% · 6档");
    expect(w.text()).toContain("+45.2% · 4档");
    expect(w.text()).toContain("+120.4% · 7档"); // 大秦
    w.unmount();
  });

  it("切「潜力信号」显示距触发%（带符号）；切「历史信号」按日期降序 + 档位标签", async () => {
    mockApi(MY, [], SIG);
    const w = mk();
    await flushPromises();
    const segBtns = () => w.findAll(".sig-seg button");
    await segBtns()[1].trigger("click"); // 潜力信号（v8.2.5 原「即将进入」）
    await flushPromises();
    expect(w.text()).toContain("+2.0%"); // 距触发带符号：线上方（长江/交通）
    expect(w.text()).toContain("长江电力"); // 持仓 approaching 股在表内（①+②）
    // v8.2.5 ②路：非持仓 + 分位 33.5% ≤50%（含已过门）+ ①距线 2.0% → 入段
    expect(w.text()).toContain("交通银行");
    // ①②双路：山煤国际（+2.1% & 45%≤50）
    expect(w.text()).toContain("山煤国际");
    expect(w.text()).toContain("✗ 5年分位 45.0%（需≤40%）");
    // v8.2.5（Joel 2026-11-24 海油实案）：时机再远（85.7%），价格距线
    // 2.5% ≤5% 也入潜力段——时机明细在「时机」列可见
    expect(w.text()).toContain("中国海油");
    const hyRow = w.findAll("tbody tr").find((tr) => tr.text().includes("中国海油"));
    expect(hyRow).toBeTruthy();
    expect(hyRow.text()).toContain("+2.5%");
    expect(hyRow.text()).toContain("✗ 5年分位 85.7%（需≤40%）");
    expect(hyRow.text()).toContain("首仓线（第1档）"); // v8.2.4 线说法
    // ②路单入（价 -5.6% 带外）：中国石化 45% ≤50；档位 = 再武装·建仓线（第1档）
    // （v8.2.4：无动作行档位用「线」说法；持仓 in_pos k=0 = 新一轮首仓）
    expect(w.text()).toContain("中国石化");
    expect(w.text()).toContain("-5.6%");
    expect(w.text()).toContain("再武装·建仓线（第1档）");
    expect(w.text()).not.toContain("中粮糖业"); // -12.2% 带外 + 76.8%>50
    // v8.2.5 ①路：国电电力 +2.1% 入段（旧三分类下 76.8% 远离时机线被滤出）
    expect(w.text()).toContain("国电电力");
    // v8.2.5（11-23 touched 保留定案作废）：云南铜业 触线+时机未过 → 从当前
    // 段移入潜力段（①+1.1%），已触回标保留
    expect(w.text()).toContain("云南铜业");
    const ynRow = w.findAll("tbody tr").find((tr) => tr.text().includes("云南铜业"));
    expect(ynRow).toBeTruthy();
    expect(ynRow.text()).toContain("已触回");
    expect(ynRow.text()).toContain("+1.1%");
    expect(ynRow.text()).toContain("再武装·建仓线（第1档）");
    expect(ynRow.text()).toContain("✗ 5年分位 62.3%（需≤40%）");
    // v8.2.5b：潜力段同口径着色——云南铜业 touched（真触过线）浅红；
    // 长江电力 approaching（未到线）浅灰（限定信号卡片内）
    const potCard = w.findAll(".card").find((c) => c.text().includes("策略信号"));
    const potRows = potCard.findAll("tbody tr");
    const potRowOf = (nm) => potRows.find((tr) => tr.text().includes(nm));
    expect(potRowOf("云南铜业").classes()).toContain("sig-row-hit");
    expect(potRowOf("长江电力").classes()).toContain("sig-row-near");
    expect(potRowOf("中国海油").classes()).toContain("sig-row-near");
    // 边界：江苏银行 -5.9%（超 5% 带）+ 62.3%>50 → 不入段
    expect(w.text()).not.toContain("江苏银行");
    // 「下一档手数」列——next_lots 单点原值（时机未过的山煤/石化也显示首仓
    // 手数；云南铜业 4 手；交通银行 next_lots=null → —）
    expect(w.text()).toContain("下一档手数");
    expect(w.text()).toContain("1 手");
    expect(w.text()).toContain("4 手");
    await segBtns()[2].trigger("click"); // 历史信号
    await flushPromises();
    // v8.2.5d（Joel 2026-11-24）：历史只显示微信推送过的真实触发信号
    // （pushed=true）——fixture 4 条中仅神华 2026-11-02 已推；未推送的
    // 核电触碰事件 / 神华 10-20 首仓 / 长江 10-11 首仓 不显示
    expect(w.text()).toContain("历史信号（1）");
    // 档位标签：第2档（level=1，first_of_round=false）
    expect(w.text()).toContain("第2档");
    // 未推送的触碰事件行（中国核电 source=touch, pushed=false）被滤出
    expect(w.text()).not.toContain("中国核电");
    expect(w.text()).not.toContain("2026-10-11");
    w.unmount();
  });

  it("lake_locked 降级提示条（仅列账本）", async () => {
    const locked = { ...JSON.parse(JSON.stringify(SIG)), lake_locked: true };
    mockApi(MY, [], locked);
    const w = mk();
    await flushPromises();
    expect(w.text()).toContain("数据湖正被灌数进程占用");
    w.unmount();
  });

  it("v8.2.5e：信号行点击 → 个股全景弹窗（股票池同件；关闭即卸载）", async () => {
    mockApi(MY, [], SIG);
    const w = mk();
    await flushPromises();
    // 当前信号段点「中国神华」行（限定策略信号卡片内，避开持仓表同名行）
    const card = w.findAll(".card").find((c) => c.text().includes("策略信号"));
    const row = card.findAll("tbody tr").find((tr) => tr.text().includes("中国神华"));
    await row.trigger("click");
    await flushPromises();
    // 弹窗标题 = name + ts_code + 「· 个股全景」（与股票池同一文案）
    expect(w.text()).toContain("中国神华 sh.601088 · 个股全景");
    // 全景卡已挂载：测试里 /api/lake/stock 404 → 占位分支（不挂 ECharts）
    expect(w.text()).toContain("数据湖无此股（T1 未灌入）");
    // 关闭（✕）→ 弹窗与全景卡卸载
    await w.find(".modal .mini-btn").trigger("click");
    await flushPromises();
    expect(w.text()).not.toContain("· 个股全景");
    w.unmount();
  });
});

// ---------------------------------------------------------------------------
// v7.0.30 实时刷新轮询门：交易时段内 30s 轮询；非时段保留缓存不发网络请求；
// 首载恒取一次；手动「刷新」按钮经 forceKeys 强制走网络（非时段也可）。
// 时段判定用 fake timers 固定系统时钟（2026-11-09 周一 20:00 北京 = 非时段）。
// ---------------------------------------------------------------------------
describe("MyPositionsTab · v7.0.30 实时刷新轮询门", () => {
  const getCalls = () => apiMock.mock.calls.filter(
    (c) => !c[1] || !c[1].method || c[1].method === "GET");

  it("非时段：首载取一次、30s 轮询空转零网络、手动刷新强制走网络", async () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(new Date("2026-11-09T20:00:00+08:00")); // 周一 20:00 北京
      mockApi(MY, [], SIG);
      const w = mk();
      await flushPromises();
      // 首载：两个 key 各取一次（非时段也取——首载恒取，保证有最近数据）
      expect(getCalls().length).toBe(2);

      // 30s 轮询 tick ×2：非时段 + 有缓存 → 返回缓存，零网络
      vi.advanceTimersByTime(30000);
      await flushPromises();
      vi.advanceTimersByTime(30000);
      await flushPromises();
      expect(getCalls().length).toBe(2); // 仍只有首载 2 次

      // 手动「刷新」→ forceKeys 强制走网络（非时段同样生效）
      const btn = w.findAll("button").find((b) => b.text().includes("刷新"));
      expect(btn).toBeTruthy();
      await btn.trigger("click");
      await flushPromises();
      expect(getCalls().length).toBe(4); // 两 key 各补一次
      w.unmount();
    } finally {
      vi.useRealTimers();
    }
  });
});
