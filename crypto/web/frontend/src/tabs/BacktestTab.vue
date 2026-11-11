// 回测（v7.0.48 实体化，v7.0.51 图表增强，v7.0.53 价格图 + 外部投入）：
// POST /api/backtest 全史回测 → 逐币：净值曲线 + 价格曲线（对数坐标）
// （时间轴刻度 + 建仓▼/加仓●/平仓▲ 事件标记，悬停看明细）
// + 了结轮次表 / 持仓中轮次 / 净值与投入统计。内联 SVG，零图表库依赖。
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

// ---- 图表几何（viewBox 760×210：绘图区 y∈[12,178]，时间轴标签 y=196）----
const CHART = { W: 760, top: 12, bottom: 178, padX: 10, axisY: 196 };

const parseD = (s) => Date.parse(s + "T00:00:00Z");

// 时间 → x（线性时间轴；曲线与事件共用，保证对齐）
function timeX(curve, dateStr) {
  const s = parseD(curve[0][0]);
  const e = parseD(curve[curve.length - 1][0]);
  const t = (parseD(dateStr) - s) / (e - s || 1);
  return CHART.padX + Math.min(1, Math.max(0, t)) * (CHART.W - 2 * CHART.padX);
}
function yOf(v, max) {
  return CHART.bottom - (v / (max || 1)) * (CHART.bottom - CHART.top);
}
// 事件日期 → 曲线上 y（curve 按日期升序，二分找最近点）
function yAt(curve, max, dateStr) {
  let lo = 0, hi = curve.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (curve[mid][0] <= dateStr) lo = mid; else hi = mid;
  }
  const v = (dateStr - curve[lo][0]) <= (curve[hi][0] - dateStr)
    ? curve[lo][1] : curve[hi][1];
  return yOf(v, max);
}

// 每币图表模型：折线点 / 时间刻度 / 事件标记（x,y 已算好）
const charts = computed(() =>
  ((res.value && res.value.results) || []).map((r) => {
    const curve = r.equity_curve || [];
    if (curve.length < 2) return { r, ready: false };
    const max = Math.max(...curve.map((c) => c[1]), 1);
    const pts = curve
      .map((c) => `${timeX(curve, c[0]).toFixed(1)},${yOf(c[1], max).toFixed(1)}`)
      .join(" ");
    // 6 个等距时间刻度（去重首尾）
    const idxs = [];
    for (let i = 0; i < 6; i++) {
      const j = Math.round((i / 5) * (curve.length - 1));
      if (!idxs.includes(j)) idxs.push(j);
    }
    const spanDays =
      (parseD(curve[curve.length - 1][0]) - parseD(curve[0][0])) / 86400000;
    const fmtTick = (d) => (spanDays > 366 ? d.slice(0, 7) : d);
    const ticks = idxs.map((j) => ({
      x: timeX(curve, curve[j][0]), label: fmtTick(curve[j][0]),
    }));
    const marks = (r.events || []).map((ev) => ({
      x: timeX(curve, ev.date),
      y: yAt(curve, max, ev.date),
      ev,
      label: evLabel(ev, r),
    }));
    // ---- 价格图（对数坐标；事件标记落在精确价格上）----
    const pc = r.price_curve || [];
    let pchart = null;
    if (pc.length >= 2) {
      const prices = pc.map((p) => p[1]);
      const lmin = Math.log10(Math.min(...prices));
      const lmax = Math.log10(Math.max(...prices));
      const lspan = lmax - lmin || 1;
      const pyOf = (v) =>
        CHART.bottom - ((Math.log10(v) - lmin) / lspan) * (CHART.bottom - CHART.top);
      const ppts = pc
        .map((p) => `${timeX(pc, p[0]).toFixed(1)},${pyOf(p[1]).toFixed(1)}`)
        .join(" ");
      // y 轴刻度：1/2/5 × 10^e，范围内最多 7 个
      const yt = [];
      for (let e = Math.floor(lmin) - 1; e <= Math.ceil(lmax) + 1 && yt.length < 7; e++)
        for (const m of [1, 2, 5]) {
          const v = m * 10 ** e;
          const lv = Math.log10(v);
          if (lv >= lmin - 1e-9 && lv <= lmax + 1e-9) yt.push(v);
        }
      const pmarks = (r.events || []).map((ev) => ({
        x: timeX(pc, ev.date),
        y: pyOf(ev.price),
        ev,
        label: evLabel(ev, r),
      }));
      pchart = {
        ppts, pmax: Math.max(...prices), pmin: Math.min(...prices),
        yt: yt.map((v) => ({ y: pyOf(v), label: fmtPrice(v) })),
        pmarks,
      };
    }
    return { r, ready: true, max, pts, ticks, marks, pchart };
  }));

function evLabel(ev, r) {
  if (ev.type === "entry") return `建仓 ${ev.date} @${ev.price}`;
  if (ev.type === "add") return `加仓 第${ev.level}级 ${ev.date} @${ev.price}`;
  return `平仓 ${ev.date} @${ev.price}（${r.take_profit_mult}x 止盈）`;
}
// 三角标记顶点（建仓=下三角 ▼，平仓=上三角 ▲）
function triPoints(m) {
  const { x, y } = m;
  return m.ev.type === "entry"
    ? `${x},${y + 4.5} ${x - 4.5},${y - 3} ${x + 4.5},${y - 3}`
    : `${x},${y - 4.5} ${x - 4.5},${y + 3} ${x + 4.5},${y + 3}`;
}
function tickAnchor(i, n) {
  if (i === 0) return "start";
  if (i === n - 1) return "end";
  return "middle";
}

function fmt(v) {
  if (v == null) return "—";
  return Number(v).toLocaleString("en-US", { maximumFractionDigits: 2 });
}
// 价格轴刻度：10^4 以上紧凑化（59448 → 59.4k），小数价保留 4 位
function fmtPrice(v) {
  if (v >= 10000) return (v / 1000).toFixed(1) + "k";
  if (v >= 100) return String(Math.round(v));
  return String(Number(v.toPrecision(3)));
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
      <section v-for="ch in charts" :key="ch.r.symbol" class="coin">
        <div class="coin-head">
          <h3>{{ ch.r.symbol }}</h3>
          <span class="hint">{{ ch.r.start }} → {{ ch.r.end }} · {{ ch.r.n_bars }} 根 1h</span>
        </div>

        <div class="stats">
          <div class="stat"><em>已了结轮次</em><b>{{ ch.r.rounds.length }}</b></div>
          <div class="stat"><em>已了结净盈亏</em>
            <b :class="{ pos: ch.r.realized_cash >= 0, neg: ch.r.realized_cash < 0 }">
              {{ pct(ch.r.realized_pnl_pct) }}（{{ fmt(ch.r.realized_cash) }} USDT）
            </b>
          </div>
          <div class="stat" title="首次建仓全为外部投入；止盈了结后余额滚入下一轮，余额耗尽才追加">
            <em>总外部投入</em><b>{{ fmt(ch.r.external_invested) }} USDT</b>
          </div>
          <div class="stat" title="持仓市值 + 期末余额（现金）">
            <em>当前净值</em><b>{{ fmt(ch.r.final_equity) }} USDT</b>
          </div>
          <div class="stat"><em>净值最大回撤</em><b>{{ fmt(ch.r.mdd_pct) }}%</b></div>
        </div>

        <h4>净值曲线（持仓市值 + 余额）</h4>
        <svg v-if="ch.ready" :viewBox="`0 0 ${CHART.W} 210`"
             class="chart" role="img" :aria-label="`${ch.r.symbol} 净值曲线`">
          <!-- 时间刻度（竖网格线 + 日期标签） -->
          <g v-for="(t, i) in ch.ticks" :key="'t' + i">
            <line :x1="t.x" :x2="t.x" :y1="CHART.top" :y2="CHART.bottom" class="gridline" />
            <text :x="t.x" :y="CHART.axisY" class="axlbl"
                  :text-anchor="tickAnchor(i, ch.ticks.length)">{{ t.label }}</text>
          </g>
          <line :x1="CHART.padX" :x2="CHART.W - CHART.padX"
                :y1="CHART.bottom" :y2="CHART.bottom" class="baseline" />
          <!-- 净值曲线 -->
          <polyline :points="ch.pts" fill="none" class="eq-line" />
          <!-- 事件标记：建仓 ▼ / 加仓 ● / 平仓 ▲（悬停看明细） -->
          <g v-for="(m, i) in ch.marks" :key="'m' + i"
             :class="['mk', 'mk-' + m.ev.type]">
            <polygon v-if="m.ev.type !== 'add'" :points="triPoints(m)" />
            <circle v-else :cx="m.x" :cy="m.y" r="1.7" />
            <title>{{ m.label }}</title>
          </g>
          <text :x="CHART.W - CHART.padX" :y="CHART.top - 1" class="axlbl"
                text-anchor="end">峰值 {{ fmt(ch.max) }} USDT</text>
        </svg>

        <template v-if="ch.ready && ch.pchart">
          <h4>价格曲线（对数坐标 · 标记 = 成交价）</h4>
          <svg :viewBox="`0 0 ${CHART.W} 210`"
               class="chart" role="img" :aria-label="`${ch.r.symbol} 价格曲线`">
            <!-- 时间刻度（与净值图共用刻度值，保证上下对齐） -->
            <g v-for="(t, i) in ch.ticks" :key="'pt' + i">
              <line :x1="t.x" :x2="t.x" :y1="CHART.top" :y2="CHART.bottom" class="gridline" />
              <text :x="t.x" :y="CHART.axisY" class="axlbl"
                    :text-anchor="tickAnchor(i, ch.ticks.length)">{{ t.label }}</text>
            </g>
            <!-- 价格 y 轴刻度（1/2/5 对数网格） -->
            <g v-for="(yt, i) in ch.pchart.yt" :key="'py' + i">
              <line :x1="CHART.padX" :x2="CHART.W - CHART.padX"
                    :y1="yt.y" :y2="yt.y" class="gridline" />
              <text :x="CHART.padX + 2" :y="yt.y - 2" class="axlbl">{{ yt.label }}</text>
            </g>
            <line :x1="CHART.padX" :x2="CHART.W - CHART.padX"
                  :y1="CHART.bottom" :y2="CHART.bottom" class="baseline" />
            <!-- 价格曲线 -->
            <polyline :points="ch.pchart.ppts" fill="none" class="price-line" />
            <!-- 事件标记：建仓 ▼ / 加仓 ● / 平仓 ▲（落在精确成交价上） -->
            <g v-for="(m, i) in ch.pchart.pmarks" :key="'pm' + i"
               :class="['mk', 'mk-' + m.ev.type]">
              <polygon v-if="m.ev.type !== 'add'" :points="triPoints(m)" />
              <circle v-else :cx="m.x" :cy="m.y" r="1.7" />
              <title>{{ m.label }}</title>
            </g>
            <text :x="CHART.W - CHART.padX" :y="CHART.top - 1" class="axlbl"
                  text-anchor="end">峰值 {{ fmtPrice(ch.pchart.pmax) }} · 最低 {{ fmtPrice(ch.pchart.pmin) }}</text>
          </svg>
        </template>

        <div v-if="ch.ready" class="legend">
          <span><i class="mk mk-entry"><svg viewBox="0 0 10 10" width="10" height="10"><polygon points="5,9 0.5,2.5 9.5,2.5" /></svg></i> 建仓</span>
          <span><i class="mk mk-add"><svg viewBox="0 0 10 10" width="10" height="10"><circle cx="5" cy="5" r="3" /></svg></i> 加仓（每级）</span>
          <span><i class="mk mk-exit"><svg viewBox="0 0 10 10" width="10" height="10"><polygon points="5,1 0.5,7.5 9.5,7.5" /></svg></i> 平仓（止盈）</span>
          <span class="hint">悬停标记看明细</span>
        </div>

        <h4>了结轮次（{{ ch.r.take_profit_mult }}x 止盈触发）</h4>
        <table v-if="ch.r.rounds.length" class="tbl">
          <tr><th>建仓日</th><th>建仓价</th><th>了结日</th><th>了结价</th>
              <th>级数</th><th>投入</th><th>所得</th><th>收益率</th></tr>
          <tr v-for="(rd, i) in ch.r.rounds" :key="i">
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
        <p v-else class="hint">无（历史未触发 {{ ch.r.take_profit_mult }}x 止盈）</p>

        <div v-if="ch.r.open_round" class="open">
          <b>持仓中（等待 {{ ch.r.take_profit_mult }}x 止盈）</b>
          <span>
            {{ ch.r.open_round.entry_date }} @ {{ fmt(ch.r.open_round.entry) }} 建仓 ·
            已加仓 {{ ch.r.open_round.k }} 级 ·
            投入 {{ fmt(ch.r.open_round.spent) }} USDT ·
            现值 {{ fmt(ch.r.open_round.value) }}（{{ pct(ch.r.open_round.unrealized_pct) }}）·
            止盈线 <b>{{ fmt(ch.r.open_round.tp_line) }}</b>
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
.gridline { stroke: #ececf2; stroke-width: 1; }
.baseline { stroke: #ddd; stroke-width: 1; }
.axlbl { font-size: 10px; fill: #888; }
.eq-line { stroke: #188038; stroke-width: 1.6; }
.price-line { stroke: #1a73e8; stroke-width: 1.6; }
/* 事件标记：建仓=蓝 ▼，加仓=灰 ●，平仓=红 ▲ */
.mk-entry polygon { fill: #1a73e8; }
.mk-add circle { fill: #777; }
.mk-exit polygon { fill: #d93025; }
.mk { cursor: help; }
.legend { display: flex; gap: 14px; align-items: center; font-size: 11px; color: #555; margin-top: 4px; flex-wrap: wrap; }
.legend i { display: inline-flex; margin-right: 3px; }
.legend .mk-entry polygon { fill: #1a73e8; }
.legend .mk-add circle { fill: #777; }
.legend .mk-exit polygon { fill: #d93025; }
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
