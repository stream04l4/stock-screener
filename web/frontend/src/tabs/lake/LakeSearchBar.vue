<script setup>
// LakeSearchBar —— 搜索（防抖 250ms + 下拉；点空白收起；移植 app.js initLakePage 搜索段）。
// - 409 lake_backfill_in_progress → **中性处理不弹红横幅**（顶部琥珀色"⏳ 数据灌入中"块
//   已说明原因——vanilla doSearch catch 分支同语义，isBackfillErr 全局复用）。
// - 其余错误 → emit error（父组件 ErrorBanner 红横幅）。
import { onBeforeUnmount, ref } from "vue";
import { api, isBackfillErr } from "../../api/client.js";

const emit = defineEmits(["select", "error"]);

const q = ref("");
const results = ref([]);
const open = ref(false);
const loading = ref(false);
let debounce = null;

async function doSearch() {
  const query = q.value.trim();
  try {
    const d = await api("/api/lake/search?q=" + encodeURIComponent(query));
    results.value = (d.results || []).slice();
    open.value = true;
  } catch (e) {
    // v6.0.4：灌数持锁（409 lake_backfill_in_progress）→ 中性处理，不弹红横幅
    if (!isBackfillErr(e)) emit("error", e.message);
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
           placeholder="搜索代码 / 名称（如 601398 或 工行）" autocomplete="off"
           @input="onInput" @keydown.enter="doSearch()" />
    <button id="btn-lake-search" class="primary-btn" @click="doSearch()">🔍 搜索</button>
    <div v-if="open" id="lake-search-results" class="lake-search-results">
      <p v-if="loading && !results.length" class="sr-empty">加载中…</p>
      <div v-else-if="!results.length" class="sr-empty">未找到匹配股票</div>
      <div v-for="r in results" :key="r.ts_code" class="sr-item" @click="pick(r)">
        <span class="sr-code">{{ r.ts_code }}</span>
        <span class="sr-name">{{ r.name || "" }}</span>
        <span class="sr-ind">{{ r.industry_name || "" }}</span>
      </div>
    </div>
  </div>
</template>
