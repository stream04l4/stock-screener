// 通用小表格（v7.0）：列定义驱动（[key, label, isNum?]）+ 搜索 + 点表头排序 + 行点击。
// v7 新表的通用件（per_stock / buys / positions / pool）——替代 v6 SURV_COLS 硬编码的 DataTable。
// 空值（无数据的数值列）排序统一排到最后（v6 NOTE-1 语义保留）。
<script setup>
import { computed, reactive } from "vue";

const props = defineProps({
  rows: { type: Array, default: () => [] },
  cols: { type: Array, required: true }, // [[key, label, isNum?], ...]
  empty: { type: String, default: "（无数据）" },
});
const emit = defineEmits(["row-click"]);

const state = reactive({ q: "", sortKey: null, sortDir: 1 });

function num(v) {
  if (v === null || v === undefined || v === "") return null;
  const n = typeof v === "number" ? v : parseFloat(v);
  return isNaN(n) ? null : n;
}

const filtered = computed(() => {
  let out = props.rows;
  if (state.q) {
    const q = state.q.toLowerCase();
    out = out.filter((r) =>
      Object.values(r).some((v) => String(v ?? "").toLowerCase().includes(q)));
  }
  if (state.sortKey) {
    const k = state.sortKey, dir = state.sortDir;
    out = [...out].sort((a, b) => {
      const na = num(a[k]), nb = num(b[k]);
      if (na !== null && nb !== null) return (na - nb) * dir;
      if (na === null && nb === null) return String(a[k] ?? "").localeCompare(String(b[k] ?? ""), "zh") * dir;
      return na === null ? 1 : -1; // 空值排最后
    });
  }
  return out;
});

function onSearch(e) {
  state.q = e.target.value.trim();
}

function onSort(key) {
  if (state.sortKey === key) state.sortDir *= -1;
  else { state.sortKey = key; state.sortDir = 1; }
}

function cell(r, key) {
  const v = r[key];
  if (v === null || v === undefined || v === "") return "—";
  return String(v);
}

function onRowClick(r) {
  emit("row-click", r);
}
</script>

<template>
  <div class="simple-table">
    <div class="tbl-toolbar">
      <input type="search" placeholder="搜索…" :value="state.q" @input="onSearch">
      <span class="count">共 {{ filtered.length }} 行</span>
    </div>
    <div class="tbl-wrap">
      <table class="data">
        <thead>
          <tr>
            <th v-for="[key, label, isNum] in cols" :key="key" :class="{ num: !!isNum }" @click="onSort(key)">
              {{ label }}<span v-if="state.sortKey === key" class="sort-arrow">{{ state.sortDir > 0 ? "▲" : "▼" }}</span>
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="!filtered.length">
            <td :colspan="cols.length" class="muted">{{ empty }}</td>
          </tr>
          <tr v-for="(r, i) in filtered" :key="i" class="clickable" @click="onRowClick(r)">
            <td v-for="[key, , isNum] in cols" :key="key" :class="{ num: !!isNum }">{{ cell(r, key) }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
