// 数据湖：crypto.duckdb 表/行数/每币 K 线范围 + 最近同步状态
// + 实时网关（/api/gateway，WS 状态 + 最新价，15s 自动刷新）。
<script setup>
import { onMounted, onUnmounted, ref } from "vue";
import { api } from "../api/client.js";

const st = ref(null);
const err = ref("");
const gw = ref(null);

async function load() {
  try {
    st.value = await api("/api/lake/status");
  } catch (e) {
    err.value = String((e && e.message) || e);
  }
}
async function loadGw() {
  try {
    gw.value = await api("/api/gateway");
  } catch {
    gw.value = null; // 网关端点不可用不阻断数据湖展示
  }
}
let timer = null;
onMounted(async () => {
  await load();
  await loadGw();
  timer = setInterval(loadGw, 15000);
});
onUnmounted(() => {
  if (timer) clearInterval(timer);
});
</script>

<template>
  <div class="card">
    <h2>数据湖（crypto.duckdb，独立于 A 股湖）</h2>
    <p v-if="err" class="err">{{ err }}</p>
    <template v-else-if="st">
      <div class="meta">
        <span class="chip">库大小 {{ st.size_mb ?? 0 }} MB</span>
        <span v-for="t in st.tables" :key="t.name" class="chip">
          {{ t.name }}：{{ t.rows }} 行
        </span>
        <span class="chip">同步间隔：每小时 :05</span>
      </div>

      <h3>实时 K 线网关（WebSocket）</h3>
      <div v-if="gw" class="sync">
        <span :class="['gwdot', gw.ok && !gw.stale ? 'on' : 'off']"></span>
        <span>WS {{ gw.ws }}</span>
        <span v-if="gw.last_error" class="hint">（{{ gw.last_error }}）</span>
        <span v-if="gw.reconnects">重连 {{ gw.reconnects }} 次</span>
        <span v-for="(c, s) in gw.coins" :key="s" class="chip">
          {{ s.replace("USDT", "") }} {{ c.price }}
        </span>
      </div>
      <p v-else class="hint">网关未启动（systemctl --user start crypto-gateway）</p>

      <h3>最近同步</h3>
      <div v-if="st.last_sync" class="sync">
        <span>时间 {{ st.last_sync.time }}</span>
        <span>源 {{ st.last_sync.exchange }}</span>
        <span>{{ st.last_sync.coins }} 币 / {{ st.last_sync.rows_total }} 行</span>
        <span>{{ st.last_sync.duration_s }}s</span>
        <span v-if="st.last_sync.cross_check?.length">
          交叉校验 {{ st.last_sync.cross_check.filter(c => c.ok).length }}/{{ st.last_sync.cross_check.length }} 通过
        </span>
      </div>
      <p v-else class="hint">尚未同步——跑 <code>python -m crypto.sync</code></p>

      <h3>每币 K 线（1h，Binance）</h3>
      <table v-if="st.per_symbol?.length">
        <thead>
          <tr><th>币种</th><th>行数</th><th>起</th><th>止</th><th>最新收盘</th></tr>
        </thead>
        <tbody>
          <tr v-for="s in st.per_symbol" :key="s.symbol">
            <td>{{ s.symbol }}</td><td>{{ s.rows }}</td>
            <td>{{ s.from }}</td><td>{{ s.to }}</td><td>{{ s.last_close }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="hint">湖内暂无 K 线。</p>
    </template>
  </div>
</template>

<style scoped>
.card { background: #fff; border: 1px solid #d8d8e0; border-radius: 12px; padding: 20px; }
h2 { margin: 0 0 12px; font-size: 16px; }
h3 { margin: 18px 0 8px; font-size: 13px; color: #555; }
.err { color: #b42318; font-size: 13px; }
.meta { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
.chip {
  font-size: 12px; padding: 3px 10px; border-radius: 999px;
  background: #f0f1f5; color: #444;
}
.sync { display: flex; gap: 14px; flex-wrap: wrap; font-size: 13px; color: #555; align-items: center; }
.hint { color: #999; font-size: 12px; }
.gwdot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.gwdot.on { background: #188038; }
.gwdot.off { background: #d92d20; }
table { width: 100%; font-size: 13px; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #ececf2; }
th { color: #888; font-weight: 500; font-size: 12px; }
</style>
