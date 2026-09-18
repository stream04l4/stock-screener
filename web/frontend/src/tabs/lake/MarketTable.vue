<script setup>
// MarketTable —— 全市场浏览（/api/lake/market 服务端分页 + industry/soe/sort 筛选；
// 移植 loadLakeMarket/renderLakeMarket L1598-1665）。
// - **筛选/排序/分页变化即拉**：useLiveQuery key = `lake:market:${page}:${sort}:${industry}:${soe}`
//   ——任一参数变 → 新 key → 立即请求（报告 §2.1）；backfill 完成事件
//   invalidate('lake:*') → 当前 key at=null + refcount>0 → 立即重拉（跨页签感知）。
// - 行业下拉 /industries **动态填充三态**（vanilla loadLakeIndustries L1563-1596）：
//   加载（select 禁用）/ 正常（选项=code+名称，保留已有选择若仍存在）/ 空库（占位+"暂无行业数据"）。
// - 行点击 → emit select(ts_code)（父组件切 StockPanoramaCard + scrollIntoView）。
// - 409 backfill → 中性占位不弹红横幅；其余错误 → 红横幅（emit error）+ "加载失败"。
import { computed, ref, watch } from "vue";
import { api, isBackfillErr } from "../../api/client.js";
import { useLiveQuery } from "../../live/useLiveQuery.js";
import { lakeFmt } from "../../utils/fmt.js";

const emit = defineEmits(["select", "error"]);

// ---- 筛选/分页本地 state（变化即拉）----
const page = ref(1);
// v6.1.4 O1：排序 = 表头点击（列名白名单 code/name/industry_name/close/volume/
// amount/pe_ttm/pb/date + asc|desc；后端非法→400）。sort/order 进 useLiveQuery key
// → 变化即重取（与分页同机制）。缺省 sort=code&order=desc（后端默认列）。
const sort = ref("code");
const order = ref("desc");
const industry = ref("");          // industry_csric2 精确过滤（""=全部行业）
const soe = ref("");               // all | soe | other（""=all，不传参）

// v6.1.4 O1：表头可点排序列（现有表格全部数值列 + 代码/名称/行业）。key=后端白名单列名，
// label=表头文案，num=右对齐。再点同列 → 切换 asc/desc；点新列 → desc（brief：▲▼指示）。
const SORT_COLS = [
  { key: "code", label: "代码" },
  { key: "name", label: "名称" },
  { key: "industry_name", label: "行业" },
  { key: "pe_ttm", label: "PE", num: true },
  { key: "pb", label: "PB", num: true },
];
function onSortCol(key) {
  if (sort.value === key) order.value = order.value === "desc" ? "asc" : "desc";
  else { sort.value = key; order.value = "desc"; }
  page.value = 1;   // 排序变化 → 回第 1 页（与筛选同语义）
}
function sortMark(key) {
  if (sort.value !== key) return "";
  return order.value === "desc" ? " ▼" : " ▲";
}

// ---- 市场数据（key 含全部参数 → 变化即拉；TTL0：纯按需+事件失效）----
const marketQ = useLiveQuery(
  () => `lake:market:${page.value}:${sort.value}:${order.value}:${industry.value}:${soe.value}`,
  () => {
    let qs = `page=${page.value}&sort=${encodeURIComponent(sort.value)}&order=${encodeURIComponent(order.value)}`;
    if (industry.value) qs += "&industry=" + encodeURIComponent(industry.value);
    if (soe.value && soe.value !== "all") qs += "&soe=" + encodeURIComponent(soe.value);
    return api("/api/lake/market?" + qs);
  },
  { ttl: 0 }
);

// ---- 行业下拉（/industries 动态填充三态；TTL 长：事件失效驱动刷新）----
const indQ = useLiveQuery("lake:industries", () => api("/api/lake/industries"), { ttl: 3600000 });
const industries = computed(() => (indQ.data.value && indQ.data.value.industries) || []);
// 三态：loading（禁用）/ 正常 / 空库（占位+提示）
const indLoading = computed(() => indQ.loading.value);
const indEmpty = computed(() => !indLoading.value && industries.value.length === 0);

// 行业下拉重填后保留已有选择（若仍存在）——vanilla loadLakeIndustries 语义
function onIndustries() {
  if (industries.value.some((it) => it.code === industry.value)) return; // 仍在 → 不动
  industry.value = "";   // 已不存在 → 回退全部行业（并触发重拉）
}

const d = computed(() => marketQ.data.value || null);
const rows = computed(() => (d.value && d.value.rows) || []);
const loading = computed(() => marketQ.loading.value && !d.value);
// v6.1.2 P3：跳页输入框（数字 + 回车/Go；clamp [1, pages]，与 page ref 同 key 机制走 useLiveQuery 重取）
const jumpVal = ref("");
function doJump() {
  const n = parseInt(jumpVal.value, 10);
  if (isNaN(n)) return;   // 非数字 → 不动（不猜、不清空已有页）
  const pages = d.value ? Math.max(1, d.value.pages) : 1;
  page.value = Math.min(pages, Math.max(1, n));   // clamp [1, pages]
}

// ---- 错误态（409 backfill 中性 / 其余红横幅）----
// 表格内占位文案：backfill → 灌数中；其余错误且有数据 → null（旧值照常展示，SWR 语义）
const marketErr = computed(() => {
  const e = marketQ.error.value;
  if (!e) return null;
  if (isBackfillErr(e)) return "⏳ 数据灌入中，全市场查询暂不可用——稍后刷新";
  return d.value ? null : "加载失败";
});

// 错误分流：backfill → 表格中性占位（watch 不 emit）；其余 → 红横幅（emit error）
watch(marketQ.error, (e) => {
  if (!e) return;
  if (!isBackfillErr(e)) emit("error", e.message);
});

function onFilter() { page.value = 1; }   // 筛选/排序变化 → 回第 1 页（vanilla lakeMarketPage=1）
function go(p) { if (p >= 1) page.value = p; }
</script>

<template>
  <div class="card" id="lake-market-card">
    <h3>全市场浏览（T1⋈T3 · 服务端分页）</h3>
    <div class="lake-toolbar">
      <select id="lake-industry-filter" :value="industry" :disabled="indLoading" @change="onFilter(); industry = $event.target.value">
        <option v-if="!industries.length" value="">全部行业</option>
        <option v-for="it in industries" :key="it.code" :value="it.code">
          {{ it.name ? it.code + " " + it.name : it.code }}
        </option>
      </select>
      <span v-if="indEmpty" id="lake-industry-hint" class="muted small">（暂无行业数据）</span>
      <select id="lake-soe-filter" :value="soe" @change="onFilter(); soe = $event.target.value">
        <option value="">全部</option>
        <option value="soe">央国企</option>
        <option value="other">非央国企</option>
      </select>
      <!-- v6.1.4 O1：排序改表头点击（▲▼指示，再点切换方向；旧"按总市值/股息率"下拉移除——
           白名单不含 total_mv/ttm_yield_pct） -->
      <span id="lake-market-count" class="count">
        {{ d ? `共 ${d.total} 只 · 第 ${d.page}/${Math.max(1, d.pages)} 页` : "" }}
      </span>
    </div>

    <div class="tbl-wrap">
      <table class="data" id="lake-market-table">
        <thead><tr>
          <!-- v6.1.4 O1：可点排序表头（代码/名称/行业/PE/PB；点击=desc 起，再点切 asc）。
               总市值(亿) 保留展示列但**不可排**（白名单不含 total_mv——brief 逐字） -->
          <th v-for="c in SORT_COLS" :key="c.key" class="lake-sort-th clickable"
              :class="{ num: c.num, 'lake-sort-active': sort === c.key }"
              :title="'点击按' + c.label + '排序'" @click="onSortCol(c.key)">
            {{ c.label }}<span class="lake-sort-mark">{{ sortMark(c.key) }}</span>
          </th>
          <th class="num">总市值(亿)</th><th class="num">股息率%</th><th>央国企</th>
        </tr></thead>
        <tbody>
          <tr v-if="loading"><td colspan="8" class="lake-loading-label">加载中…</td></tr>
          <tr v-else-if="marketErr"><td colspan="8" class="placeholder">{{ marketErr }}</td></tr>
          <tr v-else-if="!rows.length"><td colspan="8" class="placeholder">数据湖尚未灌入数据，请先运行 backfill</td></tr>
          <tr v-for="r in rows" :key="r.ts_code" class="clickable" @click="emit('select', r.ts_code)">
            <td><span class="sr-code">{{ r.ts_code }}</span></td>
            <td>{{ r.name || "—" }}</td>
            <td>{{ r.industry_name || "—" }}</td>
            <td class="num">{{ lakeFmt(r.total_mv, "num") }}</td>
            <td class="num">{{ lakeFmt(r.pe_ttm, "num") }}</td>
            <td class="num">{{ lakeFmt(r.pb, "num") }}</td>
            <td class="num">{{ lakeFmt(r.ttm_yield_pct, "pct") }}</td>
            <td><span v-if="r.soe_flag === '央国企'" class="bdg bdg-ind">央国企</span><template v-else>—</template></td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 分页（vanilla renderLakeMarket pager：« ‹ N/M › »；v6.1.2 P3 +跳页输入框） -->
    <div class="pager" id="lake-market-pager">
      <button :disabled="!d || d.page <= 1" @click="go(1)">«</button>
      <button :disabled="!d || d.page <= 1" @click="go((d ? d.page : 1) - 1)">‹</button>
      <span>{{ d ? `${d.page} / ${Math.max(1, d.pages)}` : "—" }}</span>
      <button :disabled="!d || d.page >= d.pages" @click="go((d ? d.page : 1) + 1)">›</button>
      <button :disabled="!d || d.page >= d.pages" @click="go(d ? Math.max(1, d.pages) : 1)">»</button>
      <!-- v6.1.2 P3：跳页（数字 input + 回车/Go；clamp [1,pages]，同 page ref key 机制重取） -->
      <input type="number" id="lake-market-jump" class="lake-jump-input" min="1"
             :max="d ? Math.max(1, d.pages) : undefined" v-model="jumpVal"
             placeholder="跳页" @keydown.enter="doJump()" />
      <button id="btn-lake-market-jump" class="mini-btn" @click="doJump()">Go</button>
    </div>
  </div>
</template>
