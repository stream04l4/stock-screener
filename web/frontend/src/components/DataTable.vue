// 通用数据表（移植 app.js renderDataTable：搜索 + 点表头排序 + 分页 + v2 badge + 行点击）。
// 行为一致性基准 = app.js:376-485；NOTE-1：空值（无数据的数值列）统一排到最后，升/降序都不插中间。
<script setup>
import { computed, reactive } from "vue";
import { SURV_COLS } from "../data/survCols.js";

const props = defineProps({
  rows: { type: Array, default: () => [] },
  // badge 阈值（来自 /api/runs/{day} 的 badges，源自 config/strategy.yaml；前端不得硬编码）
  badges: { type: Object, default: null },
});
const emit = defineEmits(["row-click"]);

const state = reactive({ q: "", sortKey: null, sortDir: 1, page: 0, pageSize: 25 });

// ---- cellText（逐字移植）----
function cellText(r, key) {
  if (key.startsWith("pass_")) return r[key] === "是" ? "✓" : "—";
  if (key === "top_n_selected") return String(r[key] ?? "").trim() === "1" ? "✓" : "—";
  if (key === "ma_bullish" || key === "macd_golden_cross") {
    const v = String(r[key] ?? "").trim();
    return v === "" ? "—" : (v === "1" ? "✓" : "✗");
  }
  return r[key] ?? "";
}

function num(v) { const n = parseFloat(v); return isNaN(n) ? null : n; }

// ---- filtered：搜索（全字段 includes，trim+小写）→ 排序（NOTE-1 空值排最后）----
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
      // NOTE-1：空值（无数据的数值列）统一排到最后，升序/降序都不插中间
      return na === null ? 1 : -1;
    });
  }
  return out;
});

const pages = computed(() => Math.max(1, Math.ceil(filtered.value.length / state.pageSize)));
// 搜索缩小结果集时 page 可能越界 → 钳位（移植 render() 内的 `if (state.page >= pages) state.page = pages - 1`）
const safePage = computed(() => Math.min(state.page, pages.value - 1));
const slice = computed(() =>
  filtered.value.slice(safePage.value * state.pageSize, (safePage.value + 1) * state.pageSize));

function onSearch(e) {
  state.q = e.target.value.trim();
  state.page = 0;
}

// 点表头排序：同列反向；新列升序（移植 th click handler）
function onSort(key) {
  if (state.sortKey === key) state.sortDir *= -1;
  else { state.sortKey = key; state.sortDir = 1; }
}

// ---- v2 badge（阈值全部来自 props.badges；逐字移植 rowBadges）----
function rowBadges(r) {
  const out = [];
  if (!props.badges) return out;
  const n = (v) => { const x = parseFloat(v); return isNaN(x) ? null : x; };
  if (n(r.ttm_dividend_yield_pct) != null &&
      n(r.ttm_dividend_yield_pct) >= props.badges.high_dividend_pct) {
    out.push({ cls: "bdg-div", text: "高股息" });
  }
  if (n(r.industry_roe_rank_pct) != null &&
      n(r.industry_roe_rank_pct) <= props.badges.industry_top_pct) {
    out.push({ cls: "bdg-ind", text: `行业Top${Math.round(props.badges.industry_top_pct)}%` });
  }
  if (String(r.ma_bullish ?? "").trim() === "1") {
    out.push({ cls: "bdg-ma", text: "MA多头" });
  }
  const fs = n(r.piotroski_fscore);
  if (fs != null && fs >= props.badges.fscore_min) {
    out.push({ cls: "bdg-fs", text: `F≥${props.badges.fscore_min}` });
  }
  return out;
}

function onRowClick(r) {
  const code = r.code;
  if (code) emit("row-click", code);
}
</script>

<template>
  <div>
    <div class="tbl-toolbar">
      <input type="search" placeholder="搜索代码 / 名称 / 行业…" :value="state.q" @input="onSearch">
      <span class="count">共 {{ filtered.length }} 行（原始 {{ rows.length }}）</span>
    </div>
    <div class="tbl-wrap">
      <table class="data">
        <thead>
          <tr>
            <th v-for="[key, label, isNum] in SURV_COLS" :key="key" :class="{ num: !!isNum }" @click="onSort(key)">
              {{ label }}<span v-if="state.sortKey === key" class="sort-arrow">{{ state.sortDir > 0 ? "▲" : "▼" }}</span>
            </th>
            <th>Badge</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(r, i) in slice" :key="i" class="clickable" @click="onRowClick(r)">
            <template v-for="[key, , isNum] in SURV_COLS" :key="key">
              <td :class="{ num: !!isNum, 'tag-pass': key.startsWith('pass_') && r[key] === '是', 'tag-fail': key.startsWith('pass_') && r[key] !== '是' }">{{ cellText(r, key) }}</td>
            </template>
            <td class="bdg-cell">
              <span v-for="(b, bi) in rowBadges(r)" :key="bi" class="bdg" :class="b.cls">{{ b.text }}</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <div class="pager">
      <button :disabled="safePage === 0" @click="state.page--">‹ 上一页</button>
      <span>第 {{ safePage + 1 }} / {{ pages }} 页</span>
      <button :disabled="safePage >= pages - 1" @click="state.page++">下一页 ›</button>
    </div>
  </div>
</template>
