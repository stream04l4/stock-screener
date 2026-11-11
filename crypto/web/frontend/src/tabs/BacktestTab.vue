// 回测（v7.0.48 实体化）：POST /api/backtest 全史回测（按当前生效参数）→
// 逐币：了结轮次表 / 持仓中轮次 / 净值统计 / 净值曲线（内联 SVG，零图表依赖）。
<script setup>
import { computed, ref } from "vue";
import { api } from "../api/client.js";

const busy = ref(false);
const err = ref("");
const done = ref(false);
const res = ref(null); // { params, elapsed_s, results: [...] }

async function run() {
  busy.value = true; err.value = "";
  try {
    res.value = await api("/api/backtest", {
      method: "POST",
      body: JSON.stringify({}),
    });
    done.value = true;
  } catch (e) {
    err.value = String((e && e.message) || e);
  } finally {
    busy.value = false;
  }
}

const paramChips = computed(() => {
  const p = (res.value && res.value.params) || {};
  if (!p.first_drop_pct) return [];
  return [
    `建仓 −${p.first_drop_pct}%`,
    `级距 ${p.add_grid_pct}%`,
    `首仓 ${p.base_amount_usdt}U`,
    `×${p.growth}/级`,
    `${p.take_profit_mult}x 止盈`,
    `fee ${p.fee_bps}bps`,
  ];
});

// 净值曲线 → SVG polyline points（viewBox 760×200，y 基线 0）
function curvePoints(curve) {
  if (!curve || curve.length < 2) return "";
  const W = 760, H = 200, pad = 6;
  const max = Math.max(...curve.map((c) => c[1]), 1);
  return curve
    .map((c, i) => {
      const x = pad + (i / (curve.length - 1)) * (W - 2 * pad);
      const y = H - pad - (c[1] / max) * (H - 2 * pad);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
}

function fmt(v) {
  if (v == null) return "—";
  return Number(v).toLocaleString("en-US", { maximumFractionDigits: 2 });
}
function pct(v) {
  return v == null ? "—" : `${v > 0 ? "+" : ""}${fmt(v)}%`;
}
</script>

<template>
  <div class="card">
    <h2>回测（全史 1h K 线 · 当前生效参数）</h2>
    <p v-if="err" class="err">{{ err }}</p>
    <div class="toolbar">
      <button class="btn primary" :disabled="busy" @click="run">
        {{ busy ? "回测运行中…" : done ? "重新回测" : "运行回测" }}
      </button>
      <span v-if="res" class="hint">
        耗时 {{ res.elapsed_s }}s · 参数：
        <span v-for="c in paramChips" :key="c" class="chip">{{ c }}</span>
      </span>
      <span v-else-if="!busy" class="hint">
        用湖内全部 1h 数据回测当前策略（策略 tab 改参数后点"重新回测"即可）
      </span>
    </div>

    <template v-if="done && res">
      <section v-for="r in res.results" :key="r.symbol" class="coin">
        <div class="coin-head">
          <h3>{{ r.symbol }}</h3>
          <span class="hint">{{ r.start }} → {{ r.end }} · {{ r.n_bars }} 根 1h</span>
        </div>

        <div class="stats">
          <div class="stat"><em>已了结轮次</em><b>{{ r.rounds.length }}</b></div>
          <div class="stat"><em>已了结净盈亏</em>
            <b :class="{ pos: r.realized_cash >= 0, neg: r.realized_cash < 0 }">
              {{ pct(r.realized_pnl_pct) }}（{{ fmt(r.realized_cash) }} USDT）
            </b>
          </div>
          <div class="stat"><em>当前净值</em><b>{{ fmt(r.final_equity) }} USDT</b></div>
          <div class="stat"><em>净值最大回撤</em><b>{{ fmt(r.mdd_pct) }}%</b></div>
        </div>

        <svg v-if="r.equity_curve && r.equity_curve.length > 1"
             viewBox="0 0 760 200" class="chart" role="img" aria-label="净值曲线">
          <line x1="6" x2="754" y1="194" y2="194" stroke="#ddd" stroke-width="1" />
          <polyline :points="curvePoints(r.equity_curve)" fill="none"
                    stroke="#188038" stroke-width="1.6" />
        </svg>
        <p v-if="r.equity_curve && r.equity_curve.length > 1" class="hint chart-x">
          {{ r.equity_curve[0][0] }} ———— {{ r.equity_curve[r.equity_curve.length - 1][0] }}
          （峰值 {{ fmt(Math.max(...r.equity_curve.map(c => c[1]))) }} USDT）
        </p>

        <h4>了结轮次（{{ r.take_profit_mult }}x 止盈触发）</h4>
        <table v-if="r.rounds.length" class="tbl">
          <tr><th>建仓日</th><th>建仓价</th><th>了结日</th><th>了结价</th>
              <th>级数</th><th>投入</th><th>所得</th><th>收益率</th></tr>
          <tr v-for="(rd, i) in r.rounds" :key="i">
            <td>{{ rd.entry_date }}</td>
            <td>{{ fmt(rd.entry) }}</td>
            <td>{{ rd.exit_date }}</td>
            <td>{{ fmt(rd.exit) }}</td>
            <td>{{ rd.k }}</td>
            <td>{{ fmt(rd.invested) }} U</td>
            <td>{{ fmt(rd.proceeds) }} U</td>
            <td :class="{ pos: rd.pnl_pct >= 0, neg: rd.pnl_pct < 0 }">
              {{ pct(rd.pnl_pct) }}
            </td>
          </tr>
        </table>
        <p v-else class="hint">无（历史未触发 {{ r.take_profit_mult }}x 止盈）</p>

        <div v-if="r.open_round" class="open">
          <b>持仓中（等待 {{ r.take_profit_mult }}x 止盈）</b>
          <span>
            {{ r.open_round.entry_date }} @ {{ fmt(r.open_round.entry) }} 建仓 ·
            已加仓 {{ r.open_round.k }} 级 ·
            投入 {{ fmt(r.open_round.spent) }} USDT ·
            现值 {{ fmt(r.open_round.value) }}（{{ pct(r.open_round.unrealized_pct) }}）·
            止盈线 <b>{{ fmt(r.open_round.tp_line) }}</b>
          </span>
        </div>
      </section>
    </template>
  </div>
</template>

<style scoped>
.card { background: #fff; border: 1px solid #d8d8e0; border-radius: 12px; padding: 20px; }
h2 { margin: 0 0 12px; font-size: 16px; }
h3 { margin: 0; font-size: 15px; }
h4 { margin: 14px 0 6px; font-size: 12px; color: #555; }
.err { color: #b42318; font-size: 13px; }
.toolbar { display: flex; gap: 10px; align-items: center; margin-bottom: 10px; flex-wrap: wrap; }
.btn {
  padding: 7px 16px; border-radius: 8px; border: 1px solid #d0d0d8;
  background: #fff; font-size: 13px; cursor: pointer;
}
.btn.primary { background: #188038; border-color: #188038; color: #fff; }
.btn:disabled { opacity: 0.5; cursor: default; }
.hint { color: #999; font-size: 11px; }
.chip {
  display: inline-block; background: #f0f1f5; border-radius: 999px;
  padding: 1px 8px; margin-right: 4px; color: #555;
}
.coin { border: 1px solid #e5e5ee; border-radius: 10px; padding: 14px 16px; margin-top: 14px; }
.coin-head { display: flex; justify-content: space-between; align-items: baseline; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin: 10px 0; }
.stat { background: #f7f8fb; border-radius: 8px; padding: 8px 10px; }
.stat em { display: block; font-style: normal; color: #999; font-size: 11px; }
.stat b { font-size: 13px; }
.pos { color: #188038; }
.neg { color: #c9302c; }
.chart { width: 100%; height: auto; background: #fcfcfe; border: 1px solid #eee; border-radius: 8px; }
.chart-x { margin-top: 2px; }
.tbl { width: 100%; border-collapse: collapse; font-size: 12px; }
.tbl th, .tbl td { text-align: right; padding: 4px 8px; border-bottom: 1px solid #eee; }
.tbl th { color: #888; font-weight: normal; }
.tbl th:first-child, .tbl td:first-child { text-align: left; }
.open {
  margin-top: 10px; background: #fff8e6; border: 1px solid #f0d890;
  border-radius: 8px; padding: 8px 12px; font-size: 12px; color: #6b5900;
  display: flex; flex-direction: column; gap: 4px;
}
</style>
