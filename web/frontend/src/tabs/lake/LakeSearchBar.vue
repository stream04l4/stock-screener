<script setup>
// LakeSearchBar —— 搜索（防抖 250ms + 下拉；点空白收起；移植 app.js initLakePage 搜索段）。
// - 409 lake_backfill_in_progress → **中性处理不弹红横幅**（顶部琥珀色"⏳ 数据灌入中"块
//   已说明原因——vanilla doSearch catch 分支同语义，isBackfillErr 全局复用）。
// - v8.0.9：端点换 /api/stocks/search（与「我的持仓」录入条同口径模糊匹配：
//   代码数字 / 中文名 / 拼音首字母 / 全拼），include_delisted=1 对齐旧 /api/lake/search
//   的 stock_master 全表语义（含退市/ST）；其 503 lake_locked 同样中性处理。
// - 其余错误 → emit error（父组件 ErrorBanner 红横幅）。
import { onBeforeUnmount, ref } from "vue";
import { api, isBackfillErr } from "../../api/client.js";

const emit = defineEmits(["select", "error"]);

const q = ref("");
const results = ref([]);
const open = ref(false);
const loading = ref(false);
let debounce = null;

// 503 lake_locked（灌数持锁）→ 中性（同 409 语义，不弹红横幅）
function isLakeLockedErr(e) {
  const d = e && e.body && e.body.detail;
  return !!(d && typeof d === "object" && d.error === "lake_locked");
}

async function doSearch() {
  const query = q.value.trim();
  // 空查询 / 纯数字 <3 位 → 服务端 400，不请求（与「我的持仓」同规则）
  if (!query || (/^(?:sh|sz|bj)?\.?\d+$/i.test(query)
      && query.replace(/^[a-z]+\.?/i, "").length < 3)) {
    results.value = [];
    open.value = false;
    return;
  }
  try {
    const d = await api("/api/stocks/search?q=" + encodeURIComponent(query)
      + "&include_delisted=1");
    results.value = (d.results || []).slice();
    open.value = true;
  } catch (e) {
    // v6.0.4：灌数持锁（409）→ 中性处理，不弹红横幅；v8.0.9：503 lake_locked 同理
    if (!isBackfillErr(e) && !isLakeLockedErr(e)) emit("error", e.message);
    open.value = false;
  } finally {
    loading.value = false;
  }
}

function onInput() {
  clearTimeout(debounce);
  debounce = setTimeout(doSearch, 250);   // 防抖 250ms（brief）
}

// 点空白收起下拉（vanilla document click → !closest('.lake-toolbar') → hidden）
function onDocClick(e) {
  if (!open.value) return;   // 下拉未开：无操作（keep-alive 期间监听器常驻，幂等）
  if (e.target && e.target.closest && !e.target.closest(".lake-toolbar")) open.value = false;
}

onBeforeUnmount(() => {
  clearTimeout(debounce);
  document.removeEventListener("click", onDocClick);
});
document.addEventListener("click", onDocClick);

function pick(r) {
  open.value = false;
  emit("select", r.ts_code);
}
</script>

<template>
  <div class="lake-toolbar">
    <input type="search" id="lake-search-input" v-model="q"
           placeholder="搜索代码 / 名称 / 拼音（如 601398 / 银行 / zgsh）" autocomplete="off"
           @input="onInput" @keydown.enter="doSearch()" />
    <button id="btn-lake-search" class="primary-btn" @click="doSearch()">🔍 搜索</button>
    <div v-if="open" id="lake-search-results" class="lake-search-results">
      <p v-if="loading && !results.length" class="sr-empty">加载中…</p>
      <div v-else-if="!results.length" class="sr-empty">未找到匹配股票</div>
      <div v-for="r in results" :key="r.ts_code" class="sr-item" @click="pick(r)">
        <span class="sr-code">{{ r.ts_code }}</span>
        <span class="sr-name">{{ r.name || "" }}</span>
        <span class="sr-ind">{{ r.industry || "" }}</span>
        <span v-if="r.delisted" class="sr-ind">（退市）</span>
      </div>
    </div>
  </div>
</template>
