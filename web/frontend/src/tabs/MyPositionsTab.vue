// 我的持仓 tab（v7.0.1，原「查询」改名重定位）：维护用户自己的实际持仓状态。
// - 账本 = data/user_positions.json（后端 /api/my_positions 读写，原子落盘）；
// - 展示 = 账本 × 湖最新价（市值/浮盈）+ 策略状态机参考（当前档/下次加仓价/再武装价，
//   池外股同样有参考——状态机不挑池）；
// - 添加/更新/删除：顶部录入条 + 行点击弹层编辑，PUT 单条 upsert（shares=0 删除）。
// 数据按需加载（useLiveQuery，非日跑）→ 手动刷新按钮 invalidate。
<script setup>
import { computed, onMounted, reactive, ref, watch } from "vue";
import { api } from "../api/client.js";
import { useLiveQuery } from "../live/useLiveQuery.js";
import { invalidate } from "../live/queryRegistry.js";
import { useTaskStore } from "../stores/taskStore.js";
import SimpleTable from "../components/SimpleTable.vue";
import Modal from "../components/Modal.vue";

const store = useTaskStore();

// ---------------------------------------------------------------------------
// 策略选择（策略应用）：状态机参考/加仓信号按所选策略参数计算
// ---------------------------------------------------------------------------
const stratSel = ref("current"); // "current"（活动策略 strategy.yaml）| 策略库名
const lib = ref([]);             // 策略库条目（策略页"另存为"的快照）
// 策略选择持久化（localStorage）：刷新/重开页面保留上次选择；
// 先按 current 挂载（不发无效请求），lib 加载后校验再切换 → 触发重拉。
const STRAT_LS_KEY = "ss.mypos.strategy";
onMounted(async () => {
  let saved = "";
  try { saved = localStorage.getItem(STRAT_LS_KEY) || ""; } catch { saved = ""; }
  try { lib.value = await api("/api/strategies"); } catch { lib.value = []; }
  if (saved && saved !== "current" && lib.value.some((s) => s.name === saved)) {
    stratSel.value = saved; // 库条目已删除 → 保持 current，静默回退
  }
});
const stratLabel = computed(() => stratSel.value === "current" ? "当前策略" : stratSel.value);
// 切换策略 → 失效缓存重拉（fetcher 闭包读 stratSel，带 ?strategy=）+ 记住选择
watch(stratSel, (v) => {
  invalidate("my_positions");
  try { localStorage.setItem(STRAT_LS_KEY, v); } catch { /* 隐私模式等：忽略 */ }
});

// ---------------------------------------------------------------------------
// 数据（按需 + 手动刷新；?strategy= 策略选择）
// ---------------------------------------------------------------------------
const { data: myData, loading: myLoading, error: myError } =
  useLiveQuery("my_positions",
    () => api("/api/my_positions" + (stratSel.value !== "current"
      ? "?strategy=" + encodeURIComponent(stratSel.value) : "")), {});

function refresh() {
  invalidate("my_positions");
}

// ---------------------------------------------------------------------------
// 表格列
// ---------------------------------------------------------------------------
const MY_COLS = [
  ["ts_code", "代码"], ["name", "名称"],
  ["shares", "股数", true], ["cost_price", "成本价", true], ["last_price", "最新价", true],
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
const add = reactive({ code: "", shares: "", cost: "" });
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

async function putPos(code, shares, cost) {
  return api("/api/my_positions", {
    method: "PUT",
    body: JSON.stringify({ ts_code: code, shares: Number(shares), cost_price: Number(cost) }),
  });
}

async function addNew() {
  addErr.value = "";
  addMsg.value = "";
  const code = add.code.trim().toLowerCase();
  const shares = Number(add.shares);
  const cost = Number(add.cost);
  if (!CODE_RE.test(code)) { addErr.value = "代码应为 sh.600000 / sz.000001 形式"; return; }
  if (!Number.isFinite(shares) || shares <= 0) { addErr.value = "股数应为正数"; return; }
  if (!Number.isFinite(cost) || cost < 0) { addErr.value = "成本价应为非负数"; return; }
  try {
    await putPos(code, shares, cost);
    addMsg.value = `已保存 ${code}`;
    add.code = ""; add.shares = ""; add.cost = "";
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
  // 买入登记预填：该股有活跃信号 → 建议股数（建议金额÷现价取整手）；价格默认最新价
  const s = activeSignal(r.ts_code);
  buy.shares = s ? suggestShares(s) : "";
  buy.price = r.last_price != null ? String(r.last_price) : "";
  buyMsg.value = "";
  buyErr.value = "";
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
    await putPos(code, shares, cost);
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
// 买入登记（v7.0.15）：加仓/首仓信号触发 → 券商实盘买入 → 在此登记本次股数+价格，
// 直接更新当前持仓：新总股数 = 旧股数 + 本次；新成本价 = 新旧加权平均（3 位小数，
// 券商口径，不含手续费）。复用「添加/更新」同一 PUT upsert 端点，刷新后策略档位
// （状态机按账本重播种）自动按新持仓重算。
// ---------------------------------------------------------------------------
const buy = reactive({ shares: "", price: "" });
const buyMsg = ref("");
const buyErr = ref("");

function activeSignal(code) {
  return (myData.value?.signals || []).find((s) => s.ts_code === code) || null;
}

// 建议股数：信号建议金额 ÷ 现价，向下取整到整手（100 股）
function suggestShares(s) {
  if (!s || s.suggested_amount == null) return "";
  const p = s.price || (modalRow.value && modalRow.value.last_price);
  if (!p) return "";
  const n = Math.floor(s.suggested_amount / 100 / p) * 100;
  return n >= 100 ? String(n) : "";
}

// 实时预览：旧持仓 + 本次 → 新总股数/加权成本（基于已保存账本值，非编辑框）
const buyPreview = computed(() => {
  const r = modalRow.value;
  const ns = Number(buy.shares);
  const np = Number(buy.price);
  if (!r || !Number.isInteger(ns) || ns <= 0 || !Number.isFinite(np) || np <= 0) return "";
  const oldShares = r.shares || 0;
  const oldCost = r.cost_price || 0;
  const newShares = oldShares + ns;
  const newCost = (oldShares * oldCost + ns * np) / newShares;
  return `当前 ${oldShares} 股 @ ${oldCost} ＋ 本次 ${ns} 股 @ ${np}` +
    ` → ${newShares} 股 @ ${newCost.toFixed(3)}（本次金额 ¥${(ns * np).toLocaleString()}）`;
});

async function recordBuy() {
  buyErr.value = "";
  buyMsg.value = "";
  const r = modalRow.value;
  if (!r) return;
  const ns = Number(buy.shares);
  const np = Number(buy.price);
  if (!Number.isInteger(ns) || ns <= 0) { buyErr.value = "本次股数应为正整数"; return; }
  if (ns % 100 !== 0) { buyErr.value = "A 股买入按整手（100 股倍数）"; return; }
  if (!Number.isFinite(np) || np <= 0) { buyErr.value = "本次价格应为正数"; return; }
  const oldShares = r.shares || 0;
  const oldCost = r.cost_price || 0;
  const newShares = oldShares + ns;
  const newCost = Number(((oldShares * oldCost + ns * np) / newShares).toFixed(3));
  try {
    await putPos(r.ts_code, newShares, newCost);
    buyMsg.value = `已登记 ${ns} 股 @ ${np} → ${newShares} 股 @ ${newCost.toFixed(3)}`;
    // 弹层内同步：编辑框 + 账本快照字段（策略档位列刷新后重开弹层可见新值）
    edit.shares = String(newShares);
    edit.cost = String(newCost);
    Object.assign(r, { shares: newShares, cost_price: newCost });
    buy.shares = "";
    buy.price = "";
    saveMsg.value = "";
    refresh();
  } catch (e) {
    buyErr.value = e.message;
  }
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
      <label class="strat-sel">
        <span>策略应用</span>
        <select v-model="stratSel">
          <option value="current">当前策略</option>
          <option v-for="s in lib" :key="s.name" :value="s.name">{{ s.name }}（{{ s.saved_at }}）</option>
        </select>
      </label>
      <button class="ghost-btn" @click="refresh">🔄 刷新</button>
    </div>

    <!-- 录入条：添加/更新 -->
    <div class="card">
      <h3>添加 / 更新持仓</h3>
      <p class="muted small">
        录入你实际持有的股票（代码 + 股数 + 成本价）；同一代码再次保存即更新。
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
        <button class="primary-btn" @click="addNew">保存</button>
        <span v-if="addMsg" class="msg-ok">{{ addMsg }}</span>
        <span v-if="addErr" class="msg-err">{{ addErr }}</span>
      </div>
    </div>

    <!-- 买卖信号提示条（所选策略：现价 ≤ 下一触发价位；实时价判定，价源标注） -->
    <div v-if="myData && myData.signals && myData.signals.length" class="signal-banner">
      <div class="signal-title">
        🔔 信号提示（{{ stratLabel }}）：{{ myData.signals.length }} 只达到触发价位
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
        <span v-if="s.suggested_amount != null">
          建议{{ s.action === "buy" ? "买入" : "加仓" }} ≈ ¥{{ (s.suggested_amount / 10000).toFixed(1) }}万
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
          数据截至 {{ myData.as_of || "—" }} · 行点击可编辑股数/成本或删除
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

    <!-- 行点击弹层：策略参考 + 编辑 + 买入历史 -->
    <Modal :open="!!modalRow" :title="(modalRow && modalRow.name) || '持仓详情'">
      <div v-if="modalRow">
        <div class="detail-grid">
          <div v-for="[k, label] in [
            ['ts_code', '代码'], ['in_pos', '策略持仓中'], ['strat_shares', '策略模拟股数'],
            ['k_level', '当前档位 k'],
            ['next_add_price_raw', '下一加仓触发价'], ['next_rearm_price_raw', '再武装触发价'],
            ['last_price', '最新价'], ['end_date', '数据截至'],
          ]" :key="k" class="d-row">
            <span class="d-key">{{ label }}</span>
            <span class="d-val">{{ modalRow[k] ?? "—" }}</span>
          </div>
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

        <!-- 买入登记（v7.0.15）：信号触发 → 券商买入 → 登记后当前持仓自动更新（加权成本） -->
        <div class="buy-bar">
          <h4>📝 买入登记 <span class="muted small">（券商买入后在此登记，当前持仓自动更新）</span></h4>
          <div class="buy-inputs">
            <label class="edit-field">
              <span>本次股数</span>
              <input v-model="buy.shares" class="inp num" placeholder="整手 100 股倍数" />
            </label>
            <label class="edit-field">
              <span>本次价格</span>
              <input v-model="buy.price" class="inp num" placeholder="元" />
            </label>
            <button class="primary-btn" @click="recordBuy">登记</button>
            <span v-if="buyMsg" class="msg-ok">{{ buyMsg }}</span>
            <span v-if="buyErr" class="msg-err">{{ buyErr }}</span>
          </div>
          <p v-if="buyPreview" class="muted small buy-preview">{{ buyPreview }}</p>
        </div>
        <div class="detail-buys">
          <h4>买入历史（最近回测：{{ modalBuys ? modalBuys.taskId : "无" }}）</h4>
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
