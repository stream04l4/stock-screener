// 我的持仓（v8.0.7 实体化）：
// ① 策略当前轮（纸面持仓）——引擎按生效参数全史回测出的 open_round，
//    即 Gate 模拟盘要镜像的仓位；
// ② Gate 模拟盘（testnet）状态卡——连通性 + BTC/USDT 可用余额。
<script setup>
import { onMounted, ref } from "vue";
import { api } from "../api/client.js";

const info = ref(null);
const err = ref("");
onMounted(async () => {
  try {
    info.value = await api("/api/positions");
  } catch (e) {
    err.value = String((e && e.message) || e);
  }
});

function usd(v, digits = 2) {
  if (v === null || v === undefined) return "—";
  return Number(v).toLocaleString("en-US",
    { minimumFractionDigits: digits, maximumFractionDigits: digits });
}
</script>

<template>
  <div class="card">
    <h2>我的持仓</h2>
    <p v-if="err" class="err">{{ err }}</p>
    <template v-else-if="info">
      <h3>策略当前轮（纸面持仓 = 模拟盘镜像基准）</h3>
      <table v-if="info.positions.length" class="tbl">
        <thead>
          <tr>
            <th>币种</th><th>建仓日</th><th>建仓价</th><th>加仓级</th>
            <th>持仓量</th><th>均价</th><th>现价</th>
            <th>市值 (USDT)</th><th>浮盈 (USDT)</th><th>止盈线</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="p in info.positions" :key="p.symbol">
            <td class="sym">{{ p.symbol }}</td>
            <td>{{ p.entry_date }}</td>
            <td>{{ usd(p.entry) }}</td>
            <td>L{{ p.k_level }}</td>
            <td>{{ p.shares }}</td>
            <td>{{ usd(p.avg_cost) }}</td>
            <td>{{ usd(p.last_price) }}</td>
            <td>{{ usd(p.value_usdt) }}</td>
            <td :class="p.unrealized_usdt >= 0 ? 'pos' : 'neg'">
              {{ p.unrealized_usdt >= 0 ? "+" : "" }}{{ usd(p.unrealized_usdt) }}
              （{{ p.unrealized_pct }}%）
            </td>
            <td>{{ p.tp_price ? usd(p.tp_price) : "无（追踪）" }}</td>
          </tr>
        </tbody>
      </table>
      <div v-else class="empty">
        <p class="empty-title">当前无开仓轮</p>
        <p class="empty-sub">策略未触发建仓（价格未跌到触发位），或上一轮已了结等待下一轮。</p>
      </div>

      <h3>Gate 模拟盘（testnet）</h3>
      <div class="gate" :class="info.gate.connected ? 'ok' : 'bad'">
        <span class="dot"></span>
        <span v-if="info.gate.connected">
          已连通 · BTC 可用 {{ usd(info.gate.btc_avail, 6) }} ·
          USDT 可用 {{ usd(info.gate.usdt_avail) }}
        </span>
        <span v-else>未连通：{{ info.gate.detail }}</span>
      </div>
      <p class="hint">
        纸面持仓是策略引擎按当前生效参数算出的「应当持有」仓位；
        模拟盘仓位由建仓操作镜像（市价买入等量 BTC），两者偏差即执行误差。
      </p>
    </template>
  </div>
</template>

<style scoped>
.card { background: #fff; border: 1px solid #d8d8e0; border-radius: 12px; padding: 20px; }
h2 { margin: 0 0 12px; font-size: 16px; }
h3 { margin: 18px 0 8px; font-size: 13px; color: #555; }
.err { color: #b42318; font-size: 13px; }
.empty { text-align: center; padding: 24px 0; color: #999; }
.empty-title { font-size: 14px; color: #666; margin: 0 0 4px; }
.empty-sub { font-size: 12px; margin: 0; }
.tbl { width: 100%; border-collapse: collapse; font-size: 13px; }
.tbl th, .tbl td { padding: 8px 10px; border-bottom: 1px solid #ececf2; text-align: right; }
.tbl th:first-child, .tbl td:first-child { text-align: left; }
.tbl th { color: #888; font-weight: 500; font-size: 12px; }
.sym { font-weight: 600; }
.pos { color: #188038; }
.neg { color: #b42318; }
.gate { display: flex; align-items: center; gap: 8px; font-size: 13px;
        padding: 10px 12px; border-radius: 8px; border: 1px solid #ececf2; }
.gate .dot { width: 8px; height: 8px; border-radius: 50%; background: #999; }
.gate.ok { color: #188038; }
.gate.ok .dot { background: #188038; }
.gate.bad { color: #8a6d00; }
.gate.bad .dot { background: #f5a623; }
.hint { color: #999; font-size: 11px; margin: 8px 0 0; }
</style>
