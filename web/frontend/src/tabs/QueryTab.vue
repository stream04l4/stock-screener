// 查询 tab（v7.0）：当前持仓状态（/api/positions，同步全池 ~2s）+ 股票池（/api/pool，同步）。
// 行点击 → Modal 弹该股「状态 + 历史」：
// - 状态 = positions 条目全字段（含 next_add_price / next_rearm_price 触发价位）；
// - 历史 = 最近一次完成回测的 buys（taskStore.lastResults，跨页签共享）；无回测结果时提示。
// 两个端点都是同步查询（非日跑）→ useLiveQuery 按需 + 事件触发（手动刷新按钮 invalidate）。
<script setup>
import { computed, ref } from "vue";
import { api } from "../api/client.js";
import { useLiveQuery } from "../live/useLiveQuery.js";
import { invalidate } from "../live/queryRegistry.js";
import { useTaskStore } from "../stores/taskStore.js";
import SimpleTable from "../components/SimpleTable.vue";
import Modal from "../components/Modal.vue";

const store = useTaskStore();

// ---------------------------------------------------------------------------
// 数据（按需 + 手动刷新）
// ---------------------------------------------------------------------------
const { data: posData, loading: posLoading, error: posError } =
  useLiveQuery("positions", () => api("/api/positions"), {});
const { data: poolData, loading: poolLoading, error: poolError } =
  useLiveQuery("pool", () => api("/api/pool"), {});

function refresh() {
  invalidate("positions");
  invalidate("pool");
}

// ---------------------------------------------------------------------------
// 持仓表列
// ---------------------------------------------------------------------------
const POS_COLS = [
  ["ts_code", "代码"], ["name", "名称"], ["in_pos", "持仓"],
  ["shares", "股数", true], ["avg_cost", "摊薄成本", true], ["last_price", "最新价", true],
  ["market_value", "持仓市值", true], ["cash_div", "累计分红", true],
  ["unreal_pct", "盈亏%(cash)", true], ["k_level", "当前档", true],
  ["next_add_price", "下次加仓价", true], ["next_rearm_price", "再武装价", true],
  ["end_date", "数据截至"],
];

const POOL_COLS = [
  ["ts_code", "代码"], ["name", "名称"], ["industry_csric2", "行业"],
  ["soe_basis", "SOE依据"], ["total_mv", "总市值(亿)", true],
  ["ttm_yield_pct", "TTM息率%", true], ["n_bars", "bar数", true], ["list_date", "上市日"],
];

// ---------------------------------------------------------------------------
// 行点击弹层（状态 + 历史）
// ---------------------------------------------------------------------------
const modalPos = ref(null); // positions 条目
const modalPool = ref(null); // pool 成员（pool 行点击：仅成员信息 + 该股在 positions 中的状态）

function onPosRow(r) { modalPos.value = r; }
function onPoolRow(r) {
  modalPool.value = r;
  // 若 positions 已加载 → 附带该股状态
  const p = (posData.value?.positions || []).find((x) => x.ts_code === r.ts_code) || null;
  modalPos.value = p; // 复用同一弹层（positions 优先展示；null 时弹层只显示 pool 成员）
}

// 该股在最近一次回测中的买入历史（taskStore 缓存；无 → 提示去回测页跑一次）
const modalBuys = computed(() => {
  const code = (modalPos.value || modalPool.value)?.ts_code;
  if (!code || !store.lastResults) return null;
  const rows = (store.lastResults.data.buys || []).filter((b) => b.ts_code === code);
  return { taskId: store.lastResults.taskId, rows };
});

const BUYS_COLS = [
  ["ts_code", "代码"], ["seq", "序", true], ["date", "日期"],
  ["price", "成交价", true], ["amount", "金额", true],
];

const funnelText = computed(() => {
  const f = poolData.value?.funnel;
  if (!f) return "";
  return `全市场 ${f.total} → 行业白名单 ${f.whitelist} → SOE ${f.soe} → 市值≥200亿 ${f.mv} → 上市≥250bar ${f.listing} → 连续分红 ${f.div} = 池 ${f.final} 只`;
});
</script>

<template>
  <section class="tab-panel active">
    <div class="query-toolbar">
      <h3>🔍 查询（同步，全池 ~2s）</h3>
      <button class="ghost-btn" @click="refresh">🔄 刷新</button>
    </div>

    <!-- 持仓状态 -->
    <div class="card">
      <h3>当前持仓状态（逐股状态机投影）</h3>
      <p v-if="posLoading && !posData" class="placeholder">加载中…</p>
      <p v-else-if="posError && !posData" class="msg-err">加载失败: {{ posError.message }}</p>
      <template v-else-if="posData">
        <p class="muted small">截至 {{ posData.as_of }} · {{ posData.n }} 只 · 行点击看状态/历史</p>
        <SimpleTable :rows="posData.positions" :cols="POS_COLS" @row-click="onPosRow" />
      </template>
    </div>

    <!-- 股票池 -->
    <div class="card">
      <h3>股票池（5 条件全链漏斗）</h3>
      <p v-if="poolLoading && !poolData" class="placeholder">加载中…</p>
      <p v-else-if="poolError && !poolData" class="msg-err">加载失败: {{ poolError.message }}</p>
      <template v-else-if="poolData">
        <p class="muted small">{{ funnelText }}</p>
        <SimpleTable :rows="poolData.members" :cols="POOL_COLS" @row-click="onPoolRow" />
      </template>
    </div>

    <!-- 行点击弹层：状态 + 历史 -->
    <Modal :open="!!(modalPos || modalPool)"
           :title="(modalPos && modalPos.name) || (modalPool && modalPool.name) || '个股详情'">
      <div v-if="modalPos" class="detail-grid">
        <div v-for="[k, label] in [
          ['ts_code', '代码'], ['in_pos', '持仓中'], ['shares', '股数'], ['avg_cost', '摊薄成本'],
          ['last_price', '最新价'], ['market_value', '持仓市值'], ['cash_div', '累计分红(cash)'],
          ['unreal_pct', '盈亏%（cash口径）'], ['k_level', '当前档位 k'],
          ['entry_high', '本轮高点 H'], ['low_since_high', '本周期低点 L'],
          ['next_add_price', '下次加仓触发价'], ['next_rearm_price', '再武装触发价'],
          ['end_date', '数据截至'],
        ]" :key="k" class="d-row">
          <span class="d-key">{{ label }}</span>
          <span class="d-val">{{ modalPos[k] ?? "—" }}</span>
        </div>
      </div>
      <div v-if="modalPool && !modalPos" class="detail-grid">
        <div v-for="[k, label] in [
          ['ts_code', '代码'], ['industry_csric2', '行业'], ['soe_basis', 'SOE依据'],
          ['total_mv', '总市值(亿)'], ['ttm_yield_pct', 'TTM息率%'],
          ['n_bars', 'bar数'], ['list_date', '上市日'],
        ]" :key="k" class="d-row">
          <span class="d-key">{{ label }}</span>
          <span class="d-val">{{ modalPool[k] ?? "—" }}</span>
        </div>
      </div>

      <div class="detail-buys">
        <h4>买入历史（最近回测：{{ modalBuys ? modalBuys.taskId : "无" }}）</h4>
        <template v-if="modalBuys">
          <SimpleTable v-if="modalBuys.rows.length" :rows="modalBuys.rows" :cols="BUYS_COLS" />
          <p v-else class="muted small">该标的在最近一次回测中无买入。</p>
        </template>
        <p v-else class="muted small">尚无回测结果——到「回测」页触发一次（含该标的）即可看逐笔历史。</p>
      </div>
    </Modal>
  </section>
</template>
