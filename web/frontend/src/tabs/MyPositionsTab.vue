// 我的持仓 tab（v7.0.1，原「查询」改名重定位）：维护用户自己的实际持仓状态。
// - 账本 = data/user_positions.json（后端 /api/my_positions 读写，原子落盘）；
// - 展示 = 账本 × 展示价（v7.0.40：实时行情优先，行情失败按股回退湖最新价）
//   + 策略状态机参考（当前档/下次加仓价/再武装价，池外股同样有参考——状态机不挑池）；
// - 添加/更新/删除：顶部录入条 + 行点击弹层编辑，PUT 单条 upsert（shares=0 删除）。
// 数据按需加载（useLiveQuery，非日跑）→ 手动刷新按钮 invalidate。
<script setup>
import { computed, reactive, ref } from "vue";
import { api } from "../api/client.js";
import { useLiveQuery } from "../live/useLiveQuery.js";
import { invalidate, peek } from "../live/queryRegistry.js";
import { inTradingWindow } from "../live/tradingWindow.js";
import { useTaskStore } from "../stores/taskStore.js";
import SimpleTable from "../components/SimpleTable.vue";
import Modal from "../components/Modal.vue";

const store = useTaskStore();

// ---------------------------------------------------------------------------
// 数据（v7.0.30 实时刷新：交易时段内 30s 自动轮询——后端对「股票池 ∪ 持仓」
// 取腾讯实时价，前端零维护；非交易时段不发请求、保留最近数据。首载恒取一次。
// 策略固定 = 当前策略 config/strategy.yaml，v7.0.29 策略库退役。）
// ---------------------------------------------------------------------------
const POLL_MS = 30000;
// 时段门取数：交易时段 或 尚无缓存（首载）→ 走网络；非时段有缓存 → 直接返回
// 缓存（registry 记 at=now，轮询空转零网络）。手动刷新按钮经 forceKeys 强制
// 走网络（invalidate 不清缓存，只打过期标记——不强制的话非时段手刷会拿到旧缓存）。
const forceKeys = new Set();
function gatedFetch(key, url) {
  return () => {
    if (forceKeys.delete(key)) return api(url);
    if (inTradingWindow() || peek(key) === undefined) return api(url);
    return Promise.resolve(peek(key));
  };
}

const { data: myData, loading: myLoading, error: myError } =
  useLiveQuery("my_positions", gatedFetch("my_positions", "/api/my_positions"),
               { interval: POLL_MS });

function refresh() {
  forceKeys.add("my_positions");
  forceKeys.add("strategy_signals");
  invalidate("my_positions");
  invalidate("strategy_signals");
}

// ---------------------------------------------------------------------------
// 策略信号中心（v7.0.19）：当前信号 / 即将进入信号区 / 历史信号 + 持仓·非持仓
// 数据 = /api/strategy_signals（股票池 ∪ 我的持仓，口径与 /api/my_positions 同一
// 定义点）；策略固定 = 当前策略（v7.0.29）；分段切换（active/approaching/history）。
// ---------------------------------------------------------------------------
const sigSel = ref("active"); // active | approaching | history
const { data: sigData, loading: sigLoading, error: sigError } =
  useLiveQuery("strategy_signals",
               gatedFetch("strategy_signals", "/api/strategy_signals"),
               { interval: POLL_MS });

const SIG_HISTORY_CAP = 200; // 历史信号展示上限（全量仍在 API 里，前端截断）
const sigStocks = computed(() => sigData.value?.stocks || []);
// 当前信号 = active（现价在区内）+ touched（v7.0.20：今日已触线、现价回出——
// 盘中达成过信号就保留到当日，次日行情日滚动后自动移出、移入历史段）
// 且（v7.0.23 Joel 定案「非持仓首仓 = 时机成熟 且 价跌破首仓线，双条件」）：
// 持仓股（加仓信号，不受时机限制）或非持仓股**时机达标**（timing_ok=true——
// 非持仓信号 = 买入·首仓，时机未过/无时机行 fail-closed 不入段，与「即将进入」
// 同一门）。旧 v7.0.20 的 in_pos 排除（非持仓模拟加仓档无意义）随口径废除：
// 非持仓触发线已恒为首仓线，active 即真实首仓信号。
const sigActive = computed(() => sigStocks.value.filter(
  (s) => (s.status === "active" || s.status === "touched")
         && (s.held || s.timing_ok === true)));
// 即将进入（v7.0.22 定案；v7.0.31 Joel 定案 C：非持仓股分位放宽到 ≤50%
// 展示——40% 时机门未过但价格已临区的股票 = 最有提前量价值的信号，标
// 「时机未过」明细；原「timing_ok 才展示」会让这类股票在时机翻转前完全
// 不可见，翻转当晚才突然出信号（山煤国际 2026-11-10 实案））：价格带
// （5% 内临线）——持仓股照展示（加仓不受时机限制）；非持仓股须
// timing_pctile ≤ 50（无分位数据 fail-closed 不展示）。
const sigApproach = computed(() => sigStocks.value.filter(
  (s) => s.status === "approaching"
         && (s.held || (typeof s.timing_pctile === "number"
                        && s.timing_pctile <= 50))));
const sigHistoryAll = computed(() => sigData.value?.history || []);
const sigHistory = computed(() => sigHistoryAll.value.slice(0, SIG_HISTORY_CAP));

// 档位标签：策略首笔（k=0 且本轮首买）=「首仓」，其余 =「第 N 档」（N=k+1）
function levelLabel(level, firstOfRound) {
  if (firstOfRound && level === 0) return "首仓";
  return "第" + ((level ?? 0) + 1) + "档";
}
function fmtWan(v) {
  if (v === null || v === undefined) return "—";
  return (v / 10000).toFixed(1);
}
// 分段 → 展示行（预渲染标签列；SimpleTable 单元格为纯文本）
function btText(s) {
  if (s.bt_pnl_pct === null || s.bt_pnl_pct === undefined) return "—";
  const sign = s.bt_pnl_pct >= 0 ? "+" : "";
  return sign + s.bt_pnl_pct + "% · " + (s.bt_n_buys ?? "—") + "档";
}
function timingText(s) {
  if (s.held) return "—";
  // timing_reasons 已是逐条件 ✓/✗ 明细（每条带数值+阈值）——不再叠加
  // 总判定前缀（全 ✓ 即过、任一 ✗ 即未过，前缀属冗余且易误读为两个条件）
  return s.timing_reasons || "—";
}
function actionText(s) {
  if (!s.action) return "时机未过";
  if (s.held) return s.action === "buy" ? "买入·首仓" : "加仓";
  // 非持仓统一「买入·首仓」（v7.0.23：触发线恒为首仓线，无「第N档加入」口径）
  return "买入·首仓";
}
const sigShown = computed(() => {
  if (sigSel.value === "active") {
    return sigActive.value.map((s) => ({ ...s,
      status_tag: (s.held ? "● 持仓" : "○ 非持仓") + (s.touched ? " · 已触回" : ""),
      action_label: actionText(s),
      level_label: s.held && s.in_pos ? `第${s.k_level ?? 0}档→第${(s.k_level ?? 0) + 1}档` : "首仓（第1档）",
      lots_s: s.suggested_lots == null ? "—" : `${s.suggested_lots} 手`,
      amount_w: s.suggested_lots != null && s.price
        ? fmtWan(s.suggested_lots * 100 * s.price) : "—",
      day_low_s: s.day_low == null ? "—" : String(s.day_low),
      bt_s: btText(s), timing_s: timingText(s) }));
  }
  if (sigSel.value === "approaching") {
    return sigApproach.value.map((s) => ({ ...s,
      status_tag: s.held ? "● 持仓" : "○ 非持仓",
      distance_s: (s.distance_pct ?? 0).toFixed(1) + "%",
      level_label: s.held && s.in_pos ? "第" + ((s.k_level ?? 0) + 1) + "档" : "首仓（第1档）",
      bt_s: btText(s), timing_s: timingText(s) }));
  }
  return sigHistory.value.map((h) => ({ ...h,
    status_tag: h.held ? "● 持仓" : "○ 非持仓",
    level_label: levelLabel(h.level, h.first_of_round),
    lots_s: h.lots == null ? "—" : `${h.lots} 手`,
    amount_w: fmtWan(h.amount),
    pushed_s: h.pushed ? "✓ 已推" : "—"}));
});
const SIG_ACTIVE_COLS = [
  ["ts_code", "代码"], ["name", "名称"], ["status_tag", "状态"],
  ["price", "现价", true], ["next_add_price_raw", "触发价", true],
  ["day_low_s", "日内低", true],
  ["action_label", "动作"], ["level_label", "档位"], ["lots_s", "建议手数"],
  ["amount_w", "参考金额(万)", true],
  ["bt_s", "10年回测"], ["timing_s", "时机(非持仓)"],
];
const SIG_APPROACH_COLS = [
  ["ts_code", "代码"], ["name", "名称"], ["status_tag", "状态"],
  ["price", "现价", true], ["next_add_price_raw", "触发价", true],
  ["distance_s", "距触发", true], ["level_label", "档位"],
  ["bt_s", "10年回测"], ["timing_s", "时机(非持仓)"],
];
const SIG_HISTORY_COLS = [
  ["date", "日期"], ["ts_code", "代码"], ["name", "名称"], ["status_tag", "状态"],
  ["level_label", "档位"], ["price", "触发价", true],
  ["lots_s", "手数"], ["amount_w", "金额(万)", true],
  ["pushed_s", "微信推送"],
];

// ---------------------------------------------------------------------------
// 表格列
// ---------------------------------------------------------------------------
const MY_COLS = [
  ["ts_code", "代码"], ["name", "名称"],
  ["shares", "股数", true], ["cost_price", "成本价", true], ["last_price", "最新价", true],
  // v8.2.0 累计差值口径（Joel 2026-11-13）：理论=状态机累计手数、实际=账本手数、
  // 差值=max(0,理论−实际)；湖中无数据的股为 "—"。实时新档触发的补差见顶部信号条。
  ["theory_lots", "策略理论(手)", true], ["actual_lots", "实际(手)", true],
  ["gap_lots", "差值(手)", true],
  ["market_value", "持仓市值", true], ["pnl", "浮动盈亏", true], ["pnl_pct", "盈亏%", true],
  ["k_level", "策略档", true],
  ["next_add_price_raw", "下次加仓价", true], ["next_rearm_price_raw", "再武装价", true],
  ["signal", "信号"],
  ["end_date", "数据截至"],
];

// 买卖信号 → 行标记（signal 列）+ 顶部提示条
const signalSet = computed(() => new Map((myData.value?.signals || []).map((s) => [s.ts_code, s])));
const displayRows = computed(() => (myData.value?.positions || []).map((p) => {
  const s = signalSet.value.get(p.ts_code);
  return { ...p, signal: s ? (s.action === "buy" ? "🔔 买入" : "🔔 加仓") : "" };
}));

// ---------------------------------------------------------------------------
// 顶部录入条（添加/更新）
// ---------------------------------------------------------------------------
const add = reactive({ code: "", shares: "", cost: "", since: "" });
const addMsg = ref("");
const addErr = ref("");
const CODE_RE = /^[a-z]{2}\.\d{6}$/;

// ---- 代码模糊搜索（下拉候选：代码数字 / 中文名 / 拼音首字母 / 全拼）----
const sug = ref([]);        // [{ts_code, name, industry}]
const sugOpen = ref(false);
const sugHi = ref(-1);      // 高亮候选（-1 = 无）
let sugTimer = null;
let sugSeq = 0;             // 竞态保护：只接受最后一次请求的结果

function codeSearchable(v) {
  const s = (v || "").trim();
  if (!s) return false;
  if (/^(?:sh|sz|bj)?\.?\d+$/i.test(s)) {   // 代码/纯数字：≥3 位才查（服务端同规则）
    return s.replace(/^[a-z]+\.?/i, "").length >= 3;
  }
  return true; // 中文 / 拼音：1 位起查
}

async function onCodeInput() {
  if (sugTimer) clearTimeout(sugTimer);
  const q = add.code.trim();
  if (!codeSearchable(q)) { closeSug(); return; }
  sugTimer = setTimeout(async () => {
    const seq = ++sugSeq;
    try {
      const d = await api("/api/stocks/search?q=" + encodeURIComponent(q));
      if (seq !== sugSeq) return; // 过期响应丢弃
      sug.value = d.results || [];
      sugHi.value = -1;
      sugOpen.value = sug.value.length > 0;
    } catch (e) {
      if (seq === sugSeq) { sug.value = []; sugOpen.value = false; }
      // 503 lake_locked / 网络错误 → 静默降级：仍可手动输入完整代码
    }
  }, 250);
}

function closeSug() {
  if (sugTimer) clearTimeout(sugTimer);
  sugSeq++;
  sug.value = []; sugOpen.value = false; sugHi.value = -1;
}

function pickSug(r) {
  add.code = r.ts_code;
  closeSug();
}

function onCodeKeydown(e) {
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    if (!sugOpen.value || !sug.value.length) return;
    e.preventDefault();
    const n = sug.value.length;
    sugHi.value = e.key === "ArrowDown" ? (sugHi.value + 1) % n : (sugHi.value - 1 + n) % n;
    return;
  }
  if (e.key === "Enter" && sugOpen.value) {
    // 下拉打开时 Enter = 确认高亮候选（无高亮取第一条），不触发提交
    e.preventDefault();
    pickSug(sugHi.value >= 0 ? sug.value[sugHi.value] : sug.value[0]);
    return;
  }
  if (e.key === "Escape") closeSug();
}

function onCodeKeyup(e) {
  if (e.key === "Enter" && sugOpen.value) return; // 已被 keydown 消费
  if (e.key === "Enter") addNew();
}

async function putPos(code, shares, cost, since) {
  return api("/api/my_positions", {
    method: "PUT",
    body: JSON.stringify({ ts_code: code, shares: Number(shares), cost_price: Number(cost),
                           since_date: since || "" }),
  });
}

const SINCE_RE = /^\d{4}-\d{2}-\d{2}$/;

async function addNew() {
  addErr.value = "";
  addMsg.value = "";
  const code = add.code.trim().toLowerCase();
  const shares = Number(add.shares);
  const cost = Number(add.cost);
  const since = (add.since || "").trim();
  if (!CODE_RE.test(code)) { addErr.value = "代码应为 sh.600000 / sz.000001 形式"; return; }
  if (!Number.isFinite(shares) || shares <= 0) { addErr.value = "股数应为正数"; return; }
  if (!Number.isFinite(cost) || cost < 0) { addErr.value = "成本价应为非负数"; return; }
  if (since && !SINCE_RE.test(since)) { addErr.value = "建仓日应为 YYYY-MM-DD"; return; }
  try {
    await putPos(code, shares, cost, since);
    addMsg.value = `已保存 ${code}`;
    add.code = ""; add.shares = ""; add.cost = ""; add.since = "";
    refresh();
  } catch (e) {
    addErr.value = e.message;
  }
}

// ---------------------------------------------------------------------------
// 行点击弹层（策略参考 + 编辑股数/成本）
// ---------------------------------------------------------------------------
const modalRow = ref(null);
const edit = reactive({ shares: "", cost: "" });
const saveMsg = ref("");
const saveErr = ref("");

function onRow(r) {
  modalRow.value = r;
  edit.shares = String(r.shares ?? "");
  edit.cost = String(r.cost_price ?? "");
  saveMsg.value = "";
  saveErr.value = "";
  // 买入登记预填：该股有活跃信号 → 建议股数（建议手数×100，v7.0.28 手数口径）；价格默认最新价
  const s = activeSignal(r.ts_code);
  buy.shares = s ? suggestShares(s) : "";
  buy.price = r.last_price != null ? String(r.last_price) : "";
  buyMsg.value = "";
  buyErr.value = "";
  tradeErr.value = "";
  loadTrades(); // 实盘交易历史（v7.0.17）
}

async function saveEdit() {
  saveErr.value = "";
  saveMsg.value = "";
  const code = modalRow.value.ts_code;
  const shares = Number(edit.shares);
  const cost = Number(edit.cost);
  if (!Number.isFinite(shares) || shares < 0) { saveErr.value = "股数应为非负数"; return; }
  if (!Number.isFinite(cost) || cost < 0) { saveErr.value = "成本价应为非负数"; return; }
  try {
    // 建仓日只在顶部「添加/更新持仓」录入条首次建仓时确认；弹层保存不再带
    await putPos(code, shares, cost, "");
    saveMsg.value = shares === 0 ? "已删除" : "已保存";
    if (shares === 0) modalRow.value = null;
    refresh();
  } catch (e) {
    saveErr.value = e.message;
  }
}

async function deleteRow() {
  if (!modalRow.value) return;
  if (!window.confirm(`删除持仓 ${modalRow.value.ts_code}？`)) return;
  saveErr.value = "";
  saveMsg.value = "";
  try {
    await putPos(modalRow.value.ts_code, 0, modalRow.value.cost_price);
    modalRow.value = null;
    refresh();
  } catch (e) {
    saveErr.value = e.message;
  }
}

// ---------------------------------------------------------------------------
// 交易登记（v7.0.17，原「买入登记」v7.0.15）：实盘买入/卖出后在此登记，
// 正数 = 买入（整手）、负数 = 卖出。后端单次请求原子完成两件事：
// ① 更新当前持仓（买：加权成本 3 位小数，券商口径不含手续费；卖：减股数、
//    剩余均价不变、清仓自动移除）② 追加实盘交易历史（data/my_trades.json）。
// 刷新后策略档位（状态机按账本重播种）自动按新持仓重算。
// ---------------------------------------------------------------------------
const buy = reactive({ shares: "", price: "" });
const buyMsg = ref("");
const buyErr = ref("");
// 实盘交易历史（v7.0.17）：弹层内按当前行股票加载；删除仅移除记录不回改持仓
const tradeHist = ref([]);
const tradeErr = ref("");

function activeSignal(code) {
  return (myData.value?.signals || []).find((s) => s.ts_code === code) || null;
}

// 建议股数：信号建议手数 × lot_size（v7.0.28 手数口径，A 股整手）
function suggestShares(s) {
  if (!s || s.suggested_lots == null) return "";
  return String(s.suggested_lots * 100);
}

// 实时预览：买（正数）= 加权成本；卖（负数）= 减股数 + 已实现盈亏
const buyPreview = computed(() => {
  const r = modalRow.value;
  const ns = Number(buy.shares);
  const np = Number(buy.price);
  if (!r || !Number.isInteger(ns) || ns === 0 || !Number.isFinite(np) || np <= 0) return "";
  const oldShares = r.shares || 0;
  const oldCost = r.cost_price || 0;
  if (ns > 0) {
    const newShares = oldShares + ns;
    const newCost = (oldShares * oldCost + ns * np) / newShares;
    return `当前 ${oldShares} 股 @ ${oldCost} ＋ 本次 ${ns} 股 @ ${np}` +
      ` → ${newShares} 股 @ ${newCost.toFixed(3)}（本次金额 ¥${(ns * np).toLocaleString()}）`;
  }
  const nSell = -ns;
  if (nSell > oldShares) return `⚠ 卖出不能超过持仓：当前 ${oldShares} 股`;
  const newShares = oldShares - nSell;
  const pnl = (np - oldCost) * nSell;
  return `当前 ${oldShares} 股 @ ${oldCost} − 本次 ${nSell} 股 @ ${np}` +
    ` → ${newShares} 股 @ ${oldCost}（已实现盈亏 ${pnl >= 0 ? "+" : ""}${pnl.toFixed(2)}）`;
});

async function postTrade(code, shares, price) {
  return api("/api/my_trades", {
    method: "POST",
    body: JSON.stringify({
      ts_code: code, shares, price: Number(price),
      name: (modalRow.value && modalRow.value.name) || "",
    }),
  });
}

async function recordTrade() {
  buyErr.value = "";
  buyMsg.value = "";
  const r = modalRow.value;
  if (!r) return;
  const ns = Number(buy.shares);
  const np = Number(buy.price);
  if (!Number.isInteger(ns) || ns === 0) { buyErr.value = "本次股数应为非零整数（正=买入，负=卖出）"; return; }
  if (ns > 0 && ns % 100 !== 0) { buyErr.value = "A 股买入按整手（100 股倍数）"; return; }
  if (ns < 0 && -ns > (r.shares || 0)) { buyErr.value = `卖出不能超过持仓：当前 ${r.shares || 0} 股`; return; }
  if (!Number.isFinite(np) || np <= 0) { buyErr.value = "本次价格应为正数"; return; }
  try {
    const res = await postTrade(r.ts_code, ns, np);
    const pos = res.position;
    if (pos.removed) {
      buyMsg.value = `已登记卖出 ${-ns} 股 @ ${np}，清仓移除`;
    } else {
      buyMsg.value = ns > 0
        ? `已登记买入 ${ns} 股 @ ${np} → ${pos.shares} 股 @ ${Number(pos.cost_price).toFixed(3)}`
        : `已登记卖出 ${-ns} 股 @ ${np} → ${pos.shares} 股 @ ${Number(pos.cost_price).toFixed(3)}`;
      // 弹层内同步：编辑框 + 账本快照字段（策略档位列刷新后重开弹层可见新值）
      edit.shares = String(pos.shares);
      edit.cost = String(pos.cost_price);
      Object.assign(r, { shares: pos.shares, cost_price: pos.cost_price });
    }
    buy.shares = "";
    buy.price = "";
    saveMsg.value = "";
    refresh();
    loadTrades(); // 交易历史即时刷新（新记录排最前）
  } catch (e) {
    buyErr.value = e.message;
  }
}

// 实盘交易历史加载（弹层内，按当前行股票过滤；服务端最新在前）
async function loadTrades() {
  const code = modalRow.value?.ts_code;
  if (!code) { tradeHist.value = []; return; }
  try {
    const d = await api("/api/my_trades?ts_code=" + encodeURIComponent(code));
    tradeHist.value = d.trades || [];
  } catch { tradeHist.value = []; } // 加载失败不阻塞弹层（历史区显示空）
}

// 删除一条历史记录：仅移除日志，不回改当前持仓（持仓账本是唯一状态源）
async function deleteTrade(id) {
  if (!window.confirm("删除这条交易记录？（仅移除记录，不回改当前持仓）")) return;
  tradeErr.value = "";
  try {
    await api("/api/my_trades/" + encodeURIComponent(id), { method: "DELETE" });
    await loadTrades();
  } catch (e) { tradeErr.value = e.message; }
}

// 该股在最近一次回测中的买入历史（taskStore 缓存；无 → 提示去回测页跑一次）
const modalBuys = computed(() => {
  const code = modalRow.value?.ts_code;
  if (!code || !store.lastResults) return null;
  const rows = (store.lastResults.data.buys || []).filter((b) => b.ts_code === code);
  return { taskId: store.lastResults.taskId, rows };
});

const BUYS_COLS = [
  ["ts_code", "代码"], ["seq", "序", true], ["date", "日期"],
  ["price", "成交价", true], ["amount", "金额", true],
];

// ---------------------------------------------------------------------------
// 汇总 + 格式
// ---------------------------------------------------------------------------
const t = computed(() => myData.value?.totals || null);

function fmtW(v) {
  if (v === null || v === undefined) return "—";
  return "¥" + (v / 10000).toFixed(1) + "万";
}
function fmtPct(v) {
  if (v === null || v === undefined) return "—";
  return v.toFixed(2) + "%";
}
function pnlColor(v) {
  if (v === null || v === undefined) return "inherit";
  return v > 0 ? "#c62828" : v < 0 ? "#2e7d32" : "inherit";
}
</script>

<template>
  <section class="tab-panel active">
    <div class="my-toolbar">
      <h3>💼 我的持仓</h3>
      <span class="muted small">策略应用：当前策略（strategy.yaml）</span>
      <button class="ghost-btn" @click="refresh">🔄 刷新</button>
    </div>

    <!-- 录入条：添加/更新 -->
    <div class="card">
      <h3>添加 / 更新持仓</h3>
      <p class="muted small">
        录入你实际持有的股票（代码 + 股数 + 成本价）；<b>首次建仓时填建仓日</b>
        （之后自动锚定，无需再动），同一代码再次保存即更新持仓。
        策略参考列来自状态机模拟——「下次加仓价 / 再武装价」即该跌到的触发价位。
      </p>
      <div class="add-bar">
        <div class="code-sug-wrap">
          <input v-model="add.code" class="inp" placeholder="代码/拼音/中文，如 600036、zgsh、长江电力"
                 @input="onCodeInput" @keydown="onCodeKeydown" @keyup="onCodeKeyup" @blur="closeSug" />
          <ul v-if="sugOpen" class="code-sug-list">
            <li v-for="(r, i) in sug" :key="r.ts_code" :class="{ hi: i === sugHi }"
                @mousedown.prevent="pickSug(r)">
              <span class="sug-code">{{ r.ts_code }}</span>
              <span class="sug-name">{{ r.name }}</span>
              <span class="muted small sug-ind">{{ r.industry }}</span>
            </li>
          </ul>
        </div>
        <input v-model="add.shares" class="inp num" placeholder="股数" @keyup.enter="addNew" />
        <input v-model="add.cost" class="inp num" placeholder="成本价" @keyup.enter="addNew" />
        <input v-model="add.since" class="inp num" type="date" title="建仓日（可选；理论持仓锚点，默认今天）"
               @keyup.enter="addNew" />
        <button class="primary-btn" @click="addNew">保存</button>
        <span v-if="addMsg" class="msg-ok">{{ addMsg }}</span>
        <span v-if="addErr" class="msg-err">{{ addErr }}</span>
      </div>
    </div>

    <!-- 买卖信号提示条（所选策略：现价 ≤ 下一触发价位；实时价判定，价源标注） -->
    <div v-if="myData && myData.signals && myData.signals.length" class="signal-banner">
      <div class="signal-title">
        🔔 信号提示（当前策略）：{{ myData.signals.length }} 只达到触发价位
        <span class="muted small">
          · 价源：{{ myData.price_source === "spot" ? `实时行情${myData.spot_time ? " " + myData.spot_time : ""}` : "最新收盘（T+1）" }}
        </span>
      </div>
      <div v-for="s in myData.signals" :key="s.ts_code" class="signal-row">
        <span class="signal-tag" :class="s.action === 'buy' ? 'tag-buy' : 'tag-add'">
          {{ s.action === "buy" ? "买入·首仓" : "加仓" }}
        </span>
        <b>{{ s.name }} <span class="mono">{{ s.ts_code }}</span></b>
        <span>现价 {{ s.price }} ≤ 触发价 {{ s.next_add_price }}</span>
        <span>{{ s.action === "buy" ? "首仓（第 1 档）" : `第 ${s.k_level} 档 → 第 ${s.next_level} 档` }}</span>
        <span v-if="s.suggested_lots != null">
          建议{{ s.action === "buy" ? "买入" : "补" }}
          <b>{{ s.suggested_lots }} 手</b>
          <span v-if="s.theory_lots != null" class="muted small">
            （理论累计 {{ s.theory_lots }} 手 / 现持 {{ s.actual_lots }} 手）
          </span>
          <span v-if="s.price" class="muted small">≈ ¥{{ ((s.suggested_lots * 100 * s.price) / 10000).toFixed(1) }}万</span>
        </span>
      </div>
    </div>

    <!-- 我的持仓表 -->
    <div class="card">
      <h3>当前持仓（{{ myData?.n ?? 0 }} 只）</h3>
      <p v-if="myLoading && !myData" class="placeholder">加载中…</p>
      <p v-else-if="myError && !myData" class="msg-err">加载失败: {{ myError.message }}</p>
      <template v-else-if="myData">
        <div v-if="myData.lake_locked" class="lake-backfill">
          ⏳ 数据湖正被灌数进程占用——最新价/市值/策略参考暂不可用，当前只显示你的账本；灌数结束后刷新。
        </div>
        <p class="muted small" v-if="myData.n">
          数据截至 {{ myData.as_of || "—" }}（状态机） · 价源：{{ myData.price_source === "spot"
            ? `实时行情${myData.spot_time ? " " + myData.spot_time : ""}` : "最新收盘（T+1）" }}
          · 行点击可编辑股数/成本或删除
        </p>
        <div v-if="t && myData.n" class="totals-bar">
          <span>总市值 <b>{{ fmtW(t.market_value) }}</b></span>
          <span>总成本 <b>{{ fmtW(t.cost) }}</b></span>
          <span>浮动盈亏 <b :style="{ color: pnlColor(t.pnl) }">
            {{ fmtW(t.pnl) }}（{{ fmtPct(t.pnl_pct) }}）</b></span>
        </div>
        <SimpleTable :rows="displayRows" :cols="MY_COLS"
                     empty="（尚未录入持仓——上方输入代码/股数/成本添加）" @row-click="onRow" />
      </template>
    </div>

    <!-- 策略信号中心（v7.0.19）：当前信号 / 即将进入 / 历史信号 + 持仓·非持仓 -->
    <div class="card">
      <h3>📡 策略信号（当前策略）</h3>
      <p class="muted small">
        股票池 ∪ 我的持仓（实时价覆盖 = 持仓 ∪ 湖最后收盘距触发线 ≤
        {{ sigData?.spot_preselect_pct ?? 10 }}%，其余用最新收盘）· 信号区 = 下一档触发价 ·
        「即将进入」= 现价在触发价上方 {{ sigData?.approach_pct ?? 5 }}% 以内
        （持仓股照列；非持仓股须 5 年分位 ≤50%——40% 时机门未过但价格已临区，标
        「未过（分位 X%）」，见「时机」列）
        · 当前 = 现价在区内 或 今日已触线（「已触回」= 盘中摸到后反弹，保留到当日、次日移入历史）；
        仅持仓加仓信号 + 非持仓买入信号（首仓线），非持仓模拟已建仓的加仓档不入当前信号（触碰记录见历史信号）
        · 非持仓股买入信号须过选股时机（5年分位≤40%，见「时机」列）
        · 历史 = 近 {{ sigData?.history_window_days ?? 90 }} 天（「微信推送」= 该档已推过点位提醒）
        · 10年回测 = 策略十年回放
        <span v-if="sigData?.price_source === 'spot'">
          · 价源：实时行情{{ sigData.spot_time ? " " + sigData.spot_time : "" }}</span>
        <span v-else-if="sigData">· 价源：最新收盘（T+1）</span>
        <span>· 交易时段 30s 自动刷新（非时段停轮询，手动刷新随时可用）</span>
      </p>
      <div v-if="sigData?.lake_locked" class="lake-backfill">
        ⏳ 数据湖正被灌数进程占用——策略参考暂不可用（仅列账本代码）；灌数结束后刷新。
      </div>
      <div class="sig-seg">
        <button :class="{ on: sigSel === 'active' }" @click="sigSel = 'active'">当前信号（{{ sigActive.length }}）</button>
        <button :class="{ on: sigSel === 'approaching' }" @click="sigSel = 'approaching'">即将进入（{{ sigApproach.length }}）</button>
        <button :class="{ on: sigSel === 'history' }" @click="sigSel = 'history'">历史信号（{{ sigHistoryAll.length }}）</button>
      </div>
      <p v-if="sigLoading && !sigData" class="placeholder">加载中…</p>
      <p v-else-if="sigError && !sigData" class="msg-err">加载失败: {{ sigError.message }}</p>
      <template v-else-if="sigData">
        <SimpleTable v-if="sigSel !== 'history'" :rows="sigShown"
                     :cols="sigSel === 'active' ? SIG_ACTIVE_COLS : SIG_APPROACH_COLS"
                     empty="（无）" />
        <template v-else>
          <SimpleTable :rows="sigShown" :cols="SIG_HISTORY_COLS" empty="（历史无触发）" />
          <p v-if="sigHistoryAll.length > SIG_HISTORY_CAP" class="muted small">
            仅显示最近 {{ SIG_HISTORY_CAP }} 条
          </p>
        </template>
      </template>
    </div>

    <!-- 行点击弹层：策略参考 + 编辑 + 交易登记 + 交易历史 + 回测逐笔 -->
    <Modal :open="!!modalRow" :title="(modalRow && modalRow.name) || '持仓详情'"
           @close="modalRow = null">
      <div v-if="modalRow">
        <div class="detail-grid">
          <div v-for="[k, label] in [
            ['ts_code', '代码'], ['in_pos', '策略持仓中'], ['strat_shares', '策略模拟股数'],
            ['since_date', '建仓日（顶部录入条填）'], ['strat_shares_since', '建仓日起策略股数'],
            ['theory_lots', '策略理论(手)'], ['actual_lots', '实际(手)'], ['gap_lots', '差值(手)'],
            ['k_level', '当前档位 k'],
            ['next_add_price_raw', '下一加仓触发价'], ['next_rearm_price_raw', '再武装触发价'],
            ['last_price', '最新价'], ['end_date', '数据截至'],
          ]" :key="k" class="d-row">
            <span class="d-key">{{ label }}</span>
            <span class="d-val">{{ modalRow[k] ?? "—" }}</span>
          </div>
        </div>
        <!-- v8.2.0 手动加仓时间线（自动记录，时间锚定计算依据） -->
        <div v-if="(modalRow.buys || []).length" class="buys-timeline">
          <h4>🕒 建仓时间线 <span class="muted small">（手动加仓自动记录 · 锚点 = 最早一笔 {{ modalRow.since_date }}）</span></h4>
          <table class="buys-tbl">
            <thead><tr><th>日期</th><th>加仓(股)</th><th>价格</th></tr></thead>
            <tbody>
              <tr v-for="(b, i) in modalRow.buys" :key="i">
                <td>{{ b.date }}</td>
                <td class="num">{{ b.shares }}</td>
                <td class="num">{{ b.price }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div class="edit-bar">
          <label class="edit-field">
            <span>股数</span>
            <input v-model="edit.shares" class="inp num" />
          </label>
          <label class="edit-field">
            <span>成本价</span>
            <input v-model="edit.cost" class="inp num" />
          </label>
          <button class="primary-btn" @click="saveEdit">保存</button>
          <button class="ghost-btn" @click="deleteRow">删除</button>
          <span v-if="saveMsg" class="msg-ok">{{ saveMsg }}</span>
          <span v-if="saveErr" class="msg-err">{{ saveErr }}</span>
        </div>

        <!-- 交易登记（v7.0.17）：实盘买入/卖出后登记 → 当前持仓自动更新 + 写入交易历史 -->
        <div class="buy-bar">
          <h4>📝 交易登记 <span class="muted small">（正数=买入 / 负数=卖出；登记后当前持仓自动更新并写入交易历史）</span></h4>
          <div class="buy-inputs">
            <label class="edit-field">
              <span>本次股数</span>
              <input v-model="buy.shares" class="inp num" placeholder="正=买（整手），负=卖" />
            </label>
            <label class="edit-field">
              <span>本次价格</span>
              <input v-model="buy.price" class="inp num" placeholder="元" />
            </label>
            <button class="primary-btn" @click="recordTrade">登记</button>
            <span v-if="buyMsg" class="msg-ok">{{ buyMsg }}</span>
            <span v-if="buyErr" class="msg-err">{{ buyErr }}</span>
          </div>
          <p v-if="buyPreview" class="muted small buy-preview">{{ buyPreview }}</p>
        </div>
        <!-- 交易历史（v7.0.17）：实盘登记产生的买卖记录，可删除（仅移除记录，不回改持仓） -->
        <div class="detail-buys">
          <h4>📜 交易历史 <span class="muted small">（{{ tradeHist.length }} 条 · 删除仅移除记录，不回改当前持仓）</span></h4>
          <table v-if="tradeHist.length" class="trade-hist">
            <thead>
              <tr><th>日期</th><th>方向</th><th>股数</th><th>价格</th><th>金额</th><th>已实现盈亏</th><th></th></tr>
            </thead>
            <tbody>
              <tr v-for="t in tradeHist" :key="t.id">
                <td>{{ t.date }}</td>
                <td><span :class="t.direction === 'buy' ? 'tag-buy' : 'tag-sell'">{{ t.direction === 'buy' ? '买' : '卖' }}</span></td>
                <td class="num">{{ t.shares }}</td>
                <td class="num">{{ t.price }}</td>
                <td class="num">{{ t.amount }}</td>
                <td class="num" :style="t.realized_pnl == null ? '' : { color: pnlColor(t.realized_pnl) }">
                  {{ t.realized_pnl == null ? "—" : (t.realized_pnl >= 0 ? "+" : "") + t.realized_pnl }}
                </td>
                <td class="num"><button class="ghost-btn tiny" title="删除这条记录" @click="deleteTrade(t.id)">🗑</button></td>
              </tr>
            </tbody>
          </table>
          <p v-else class="muted small">暂无交易记录——在上方「交易登记」登记买入/卖出后出现。</p>
          <span v-if="tradeErr" class="msg-err">{{ tradeErr }}</span>
        </div>
        <!-- 回测逐笔（最近一次回测的模拟买入，与实盘交易历史分离） -->
        <div class="detail-buys">
          <h4>📊 回测逐笔 <span class="muted small">（最近回测：{{ modalBuys ? modalBuys.taskId : "无" }}）</span></h4>
          <template v-if="modalBuys">
            <SimpleTable v-if="modalBuys.rows.length" :rows="modalBuys.rows" :cols="BUYS_COLS" />
            <p v-else class="muted small">该标的在最近一次回测中无买入。</p>
          </template>
          <p v-else class="muted small">尚无回测结果——到「回测」页触发一次（含该标的）即可看逐笔历史。</p>
        </div>
      </div>
    </Modal>
  </section>
</template>
