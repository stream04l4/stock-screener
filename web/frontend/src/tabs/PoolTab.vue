// 候选池 tab（v7.0.12）：低波红利央国企候选股票池（基础 5 条件 + 选股门槛 ∪ 手动添加）。
// - 候选池 ≠ 买入信号：入选只代表进入策略候选；买不买看价格是否到策略触发线
//   （「我的持仓」页信号 / 微信提醒，同口径）——页面文案明确这一点。
// - 漏斗：全市场→白名单+存活→央国企→市值→上市→连续分红→十年回测为正→股息率→价格区间→候选池。
// - 手动添加：代码录入（搜索下拉复用 /api/stocks/search）→ POST /api/pool/manual；
//   手动行点行 → 弹层移除（DELETE /api/pool/manual）。手动股绕过回测/股息率/价格门槛。
// 数据按需加载（useLiveQuery）→ 手动刷新按钮 invalidate。
<script setup>
import { computed, reactive, ref } from "vue";
import { api } from "../api/client.js";
import { useLiveQuery } from "../live/useLiveQuery.js";
import { invalidate } from "../live/queryRegistry.js";
import SimpleTable from "../components/SimpleTable.vue";
import Modal from "../components/Modal.vue";

const { data: poolData, loading: poolLoading, error: poolError } =
  useLiveQuery("pool", () => api("/api/pool"), {});

function refresh() {
  invalidate("pool");
}

// ---------------------------------------------------------------------------
// 漏斗展示
// ---------------------------------------------------------------------------
const FUNNEL_STEPS = computed(() => {
  const f = poolData.value && poolData.value.funnel;
  if (!f) return [];
  return [
    ["全市场", f.total],
    ["白名单+存活+非ST", f.whitelist],
    ["央国企", f.soe],
    ["市值达标", f.mv],
    ["上市满期", f.listing],
    ["连续分红", f.div],
    ["十年回测为正", f.backtest],
    ["股息率达标", f.yield],
    ["价格区间内", f.price],
    ["候选池", f.final],
  ];
});

// ---------------------------------------------------------------------------
// 手动添加录入条（搜索下拉，复用 /api/stocks/search）
// ---------------------------------------------------------------------------
const add = reactive({ code: "" });
const addMsg = ref("");
const addErr = ref("");
const sug = ref([]);
const sugOpen = ref(false);
const sugHi = ref(-1);
let sugTimer = null;
let sugSeq = 0;

function codeSearchable(v) {
  const s = (v || "").trim();
  if (!s) return false;
  if (/^(?:sh|sz|bj)?\.?\d+$/i.test(s)) {
    return s.replace(/^[a-z]+\.?/i, "").length >= 3;
  }
  return true;
}

async function onCodeInput() {
  if (sugTimer) clearTimeout(sugTimer);
  const q = add.code.trim();
  if (!codeSearchable(q)) { closeSug(); return; }
  sugTimer = setTimeout(async () => {
    const seq = ++sugSeq;
    try {
      const d = await api("/api/stocks/search?q=" + encodeURIComponent(q));
      if (seq !== sugSeq) return;
      sug.value = d.results || [];
      sugHi.value = -1;
      sugOpen.value = sug.value.length > 0;
    } catch {
      if (seq === sugSeq) { sug.value = []; sugOpen.value = false; }
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
    sugHi.value = e.key === "ArrowDown"
      ? (sugHi.value + 1) % n : (sugHi.value - 1 + n) % n;
    return;
  }
  if (e.key === "Enter" && sugOpen.value) {
    e.preventDefault();
    pickSug(sug.value[Math.max(0, sugHi.value)]);
  }
}

async function doAdd() {
  addMsg.value = "";
  addErr.value = "";
  const code = add.code.trim();
  if (!code) { addErr.value = "请输入股票代码（6 位数字或 sh.600000 形式）"; return; }
  try {
    await api("/api/pool/manual", {
      method: "POST",
      body: JSON.stringify({ ts_code: code }),
    });
    addMsg.value = `已加入候选池：${code}`;
    add.code = "";
    closeSug();
    refresh();
  } catch (e) {
    addErr.value = e.message;
  }
}

// ---------------------------------------------------------------------------
// 行点击：手动股 → 移除确认弹层；自动股 → 详情弹层
// ---------------------------------------------------------------------------
const modalRow = ref(null);

function onRow(r) {
  modalRow.value = r;
}

async function doRemove() {
  if (!modalRow.value) return;
  try {
    await api("/api/pool/manual", {
      method: "DELETE",
      body: JSON.stringify({ ts_code: modalRow.value.ts_code }),
    });
    modalRow.value = null;
    refresh();
  } catch (e) {
    addErr.value = e.message;
    modalRow.value = null;
  }
}

// ---------------------------------------------------------------------------
// 表格列
// ---------------------------------------------------------------------------
const COLS = [
  ["ts_code", "代码"],
  ["name", "名称"],
  ["bt_pnl_pct", "10年回测%", true],
  ["bt_n_buys", "买入次数", true],
  ["ttm_yield_pct", "股息率%", true],
  ["price_latest", "最新价", true],
  ["total_mv", "总市值(亿)", true],
  ["src", "来源"],
];

const rows = computed(() => {
  const ms = (poolData.value && poolData.value.members) || [];
  return ms.map((m) => ({ ...m, src: m.is_manual ? "手动" : "自动" }));
});
</script>

<template>
  <div class="tab">
    <div class="card">
      <div class="card-head">
        <h2>候选股票池</h2>
        <button class="btn" :disabled="poolLoading" @click="refresh">
          {{ poolLoading ? "加载中…" : "刷新" }}
        </button>
      </div>
      <p class="hint">
        候选池 ≠ 买入信号：入选只代表进入策略候选名单；是否买入看价格是否到策略触发线
        （「我的持仓」页信号 / 微信提醒，同一口径）。
        手动添加的股票绕过「十年回测/股息率/价格区间」门槛，恒在候选池。
      </p>
      <p v-if="poolError" class="err">{{ poolError }}</p>

      <!-- 漏斗 -->
      <div v-if="poolData" class="funnel">
        <span v-for="[label, n] in FUNNEL_STEPS" :key="label" class="fstep">
          <em>{{ label }}</em><b>{{ n }}</b>
        </span>
      </div>

      <!-- 手动添加录入条 -->
      <div class="addbar">
        <div class="addwrap">
          <input
            v-model="add.code"
            class="addinput"
            placeholder="手动添加：代码或名称，如 600900 / 长江电力"
            @input="onCodeInput"
            @keydown="onCodeKeydown"
            @blur="() => setTimeout(closeSug, 150)"
          />
          <ul v-if="sugOpen" class="sug">
            <li
              v-for="(r, i) in sug"
              :key="r.ts_code"
              :class="{ hi: i === sugHi }"
              @mousedown.prevent="pickSug(r)"
            >
              {{ r.ts_code }} {{ r.name }}<span class="ind">{{ r.industry }}</span>
            </li>
          </ul>
        </div>
        <button class="btn" @click="doAdd">加入候选池</button>
        <span v-if="addMsg" class="ok">{{ addMsg }}</span>
        <span v-if="addErr" class="err">{{ addErr }}</span>
      </div>
    </div>

    <div class="card">
      <div class="card-head">
        <h3>成员（{{ poolData ? poolData.n_members : 0 }}）</h3>
        <span class="hint2">点行：手动股可移除；自动股看回测详情</span>
      </div>
      <SimpleTable
        :rows="rows"
        :cols="COLS"
        empty="（候选池为空——等待每日选股预计算，或上方手动添加）"
        @row-click="onRow"
      />
    </div>

    <!-- 行弹层 -->
    <Modal
      v-if="modalRow"
      open
      :title="modalRow.name + '（' + modalRow.ts_code + '）'"
      @close="modalRow = null"
    >
      <dl class="kv">
        <dt>来源</dt><dd>{{ modalRow.src === "手动" ? "手动添加" : "自动入选" }}</dd>
        <dt>十年回测净收益</dt>
        <dd>{{ modalRow.bt_pnl_pct === null || modalRow.bt_pnl_pct === undefined ? "未触发（策略无仓位）" : modalRow.bt_pnl_pct + "%" }}</dd>
        <dt>十年买入次数</dt><dd>{{ modalRow.bt_n_buys ?? "—" }}</dd>
        <dt>股息率 TTM</dt><dd>{{ modalRow.ttm_yield_pct ?? "—" }} %</dd>
        <dt>最新价</dt><dd>{{ modalRow.price_latest ?? "—" }} 元</dd>
        <dt>总市值</dt><dd>{{ modalRow.total_mv ?? "—" }} 亿</dd>
      </dl>
      <p class="hint">
        买入时机由策略状态机决定：价格到首仓线/加仓线时，「我的持仓」页出信号、微信推送提醒。
      </p>
      <div class="modal-actions">
        <button class="btn" @click="modalRow = null">关闭</button>
        <button v-if="modalRow.src === '手动'" class="btn danger" @click="doRemove">
          从候选池移除
        </button>
      </div>
    </Modal>
  </div>
</template>

<style scoped>
.funnel { display: flex; flex-wrap: wrap; gap: 8px; margin: 10px 0; }
.fstep {
  display: inline-flex; align-items: baseline; gap: 6px;
  padding: 3px 10px; border: 1px solid var(--line, #d8d8e0); border-radius: 999px;
  font-size: 12px; background: var(--bg-soft, #f6f6fa);
}
.fstep em { font-style: normal; color: #666; }
.fstep b { font-size: 13px; }
.addbar { display: flex; align-items: center; gap: 10px; margin-top: 8px; }
.addwrap { position: relative; }
.addinput { width: 340px; }
.sug {
  position: absolute; top: 100%; left: 0; right: 0; z-index: 30;
  margin: 2px 0 0; padding: 4px 0; list-style: none;
  background: var(--bg, #fff); border: 1px solid var(--line, #d8d8e0);
  border-radius: 8px; box-shadow: 0 6px 18px rgba(20, 20, 40, .12); max-height: 260px; overflow: auto;
}
.sug li { padding: 6px 12px; font-size: 13px; cursor: pointer; }
.sug li.hi, .sug li:hover { background: var(--bg-soft, #eef0f8); }
.sug .ind { color: #888; margin-left: 8px; font-size: 12px; }
.addbar .ok { color: #1a7f37; font-size: 13px; }
.addbar .err { color: #b42318; font-size: 13px; }
.hint { color: #666; font-size: 13px; line-height: 1.7; margin: 6px 0 10px; }
.hint2 { color: #888; font-size: 12px; }
.kv { display: grid; grid-template-columns: 110px 1fr; gap: 4px 12px; margin: 8px 0; }
.kv dt { color: #666; }
.kv dd { margin: 0; }
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 14px; }
.btn.danger { border-color: #b42318; color: #b42318; }
</style>
