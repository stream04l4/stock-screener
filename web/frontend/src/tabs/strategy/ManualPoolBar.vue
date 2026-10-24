// 手动池录入条（v7.0.12 移至策略页签「股票池」卡片内；原候选池页签已删）。
// - 候选池 = 基础 5 条件 + 选股门槛（十年回测为正/股息率/价格区间）∪ 手动池。
// - 手动添加的股票绕过三道选股门槛，恒在候选池；退市自动剔除。
// - 数据：GET /api/pool/manual（读 data/manual_pool.json，零湖访问）；
//   POST/DELETE /api/pool/manual 增删；搜索下拉复用 /api/stocks/search。
// - 防御：加载失败 → 隐藏本条（策略页主体不受影响）。
<script setup>
import { computed, reactive, ref } from "vue";
import { api } from "../../api/client.js";
import { useLiveQuery } from "../../live/useLiveQuery.js";
import { invalidate } from "../../live/queryRegistry.js";
import { useToastStore } from "../../stores/toastStore.js";

const toast = useToastStore();
const { data, loading, error } = useLiveQuery(
  "manual_pool", () => api("/api/pool/manual"), {});

const visible = computed(() =>
  !error.value && data.value && typeof data.value === "object");

function refresh() {
  invalidate("manual_pool");
  invalidate("pool"); // 候选列表同步刷新（手动股进出）
}

// ---------------------------------------------------------------------------
// 搜索下拉（/api/stocks/search；与我的持仓录入条同口径）
// ---------------------------------------------------------------------------
const add = reactive({ code: "" });
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

const busy = ref(false);

async function doAdd() {
  if (busy.value) return;
  const code = add.code.trim();
  if (!code) {
    toast.toast("请输入股票代码（6 位数字或 sh.600000 形式）", false);
    return;
  }
  busy.value = true;
  try {
    const d = await api("/api/pool/manual", {
      method: "POST", body: JSON.stringify({ ts_code: code }),
    });
    toast.toast(`已加入候选池：${d.entry.name}（${code}）`);
    add.code = "";
    closeSug();
    refresh();
  } catch (e) {
    toast.toast("加入失败：" + e.message, false);
  } finally {
    busy.value = false;
  }
}

async function doRemove(tsCode, name) {
  if (busy.value) return;
  busy.value = true;
  try {
    await api("/api/pool/manual", {
      method: "DELETE", body: JSON.stringify({ ts_code: tsCode }),
    });
    toast.toast(`已从候选池移除：${name}（${tsCode}）`);
    refresh();
  } catch (e) {
    toast.toast("移除失败：" + e.message, false);
  } finally {
    busy.value = false;
  }
}

const entries = computed(() => {
  const stocks = (data.value && data.value.stocks) || {};
  return Object.entries(stocks).sort(([a], [b]) => a.localeCompare(b));
});
</script>

<template>
  <div v-if="visible" class="mpb">
    <div class="mpb-head">
      <span class="mpb-title">手动添加（绕过回测/股息率/价格门槛，恒在候选池）</span>
      <span v-if="loading" class="mpb-muted">加载中…</span>
    </div>
    <div class="mpb-row">
      <div class="mpb-wrap">
        <input
          v-model="add.code"
          class="mpb-input"
          placeholder="代码或名称，如 600900 / 长江电力"
          @input="onCodeInput"
          @keydown="onCodeKeydown"
          @blur="() => setTimeout(closeSug, 150)"
        />
        <ul v-if="sugOpen" class="mpb-sug">
          <li
            v-for="(r, i) in sug"
            :key="r.ts_code"
            :class="{ hi: i === sugHi }"
            @mousedown.prevent="pickSug(r)"
          >
            {{ r.ts_code }} {{ r.name }}<span class="mpb-ind">{{ r.industry }}</span>
          </li>
        </ul>
      </div>
      <button class="mpb-btn" :disabled="busy" @click="doAdd">加入</button>
    </div>
    <ul v-if="entries.length" class="mpb-list">
      <li v-for="[code, e] in entries" :key="code">
        <span class="mpb-code">{{ code }}</span>
        <span class="mpb-name">{{ e.name }}</span>
        <button class="mpb-x" title="从候选池移除" :disabled="busy"
                @click="doRemove(code, e.name)">×</button>
      </li>
    </ul>
    <p v-else class="mpb-muted">（暂无手动添加）</p>
  </div>
</template>

<style scoped>
.mpb {
  margin-top: 12px; padding: 10px 12px;
  border-top: 1px dashed var(--line, #d8d8e0);
}
.mpb-head { font-size: 13px; color: #444; margin-bottom: 8px; }
.mpb-muted { color: #999; font-size: 12px; }
.mpb-row { display: flex; align-items: center; gap: 8px; }
.mpb-wrap { position: relative; }
.mpb-input { width: 300px; }
.mpb-sug {
  position: absolute; top: 100%; left: 0; right: 0; z-index: 30;
  margin: 2px 0 0; padding: 4px 0; list-style: none;
  background: var(--bg, #fff); border: 1px solid var(--line, #d8d8e0);
  border-radius: 8px; box-shadow: 0 6px 18px rgba(20, 20, 40, .12);
  max-height: 240px; overflow: auto;
}
.mpb-sug li { padding: 6px 12px; font-size: 13px; cursor: pointer; }
.mpb-sug li.hi, .mpb-sug li:hover { background: var(--bg-soft, #eef0f8); }
.mpb-ind { color: #888; margin-left: 8px; font-size: 12px; }
.mpb-btn { padding: 4px 14px; }
.mpb-list { list-style: none; margin: 8px 0 0; padding: 0; display: flex; flex-wrap: wrap; gap: 6px; }
.mpb-list li {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 2px 8px; border: 1px solid var(--line, #d8d8e0); border-radius: 999px;
  font-size: 12px; background: var(--bg-soft, #f6f6fa);
}
.mpb-code { font-family: ui-monospace, monospace; }
.mpb-name { color: #444; }
.mpb-x {
  border: none; background: none; cursor: pointer; color: #b42318;
  font-size: 14px; line-height: 1; padding: 0 2px;
}
</style>
