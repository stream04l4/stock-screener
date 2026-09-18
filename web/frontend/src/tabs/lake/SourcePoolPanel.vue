<script setup>
// SourcePoolPanel —— v6.1 多源资源池面板（brief D3-B；数据 = /status.source_pool）。
//
// 四区块（报告 §3 T-数据湖）：
//   1) 采用源分布：每表 by_source **横向堆叠条**（固定色板 sina蓝/tencent橙/baostock紫/
//      tdx青/adata_f10粉/local灰；legacy 未知 source → 灰）+ 数字；
//   2) 数据源状态（v6.1.3，原"资源池健康度"升级）：每源一张卡（5 色 SOURCE_COLORS
//      左边框/标题着色），卡内四行——连通性（✓可达/✗不可达/—未探测 + latency；
//      enabled=false → "已禁用"灰 badge 替代 ✓/✗）/ 数据类型（provides chips，role
//      后缀小字 muted）/ 配额（baostock 今日 used/budget；其余 rate_limit 文案或
//      "无官方配额"）/ 探测时间（stale=true → "探测数据过期"角标）。
//   3) conflict_src：total + 非零表逐行（T8/T9 无该列，后端不出现在对象里）；
//   4) BaoStock 恢复探测：alive badge + at/elapsed/detail。
// **backfill_in_progress 时整块"灌数中暂不可用"**（库被独占写锁持有期间 source_pool
// 只在 ready 态出现——locked 态响应无此键，本组件按 props.pool==null 降级）。
import { computed } from "vue";

const props = defineProps({
  pool: { type: Object, default: null },   // status.source_pool（ready 态才有）
  backfill: { type: Boolean, default: false }, // backfill_in_progress（琥珀块同条件）
});

// 固定色板（brief 逐字：sina蓝/tencent橙/baostock紫/tdx青/adata_f10粉/local灰）；
// legacy 历史 source 值（如 't'/'em_local_static'）→ 灰（未知源不猜颜色）。
const SOURCE_COLORS = {
  sina: "#2563eb", tencent: "#f97316", baostock: "#8b5cf6",
  tdx: "#06b6d4", adata_f10: "#ec4899", local: "#94a3b8",
};
const UNKNOWN_COLOR = "#cbd5e1";

// 表展示名（与 /status.tables[].name_cn 对齐；by_source 键=表 key）
const TABLE_LABELS = {
  stock_master: "T1 股票主档", kline_daily: "T2 日K线", valuation_daily: "T3 估值日线",
  dividend_events: "T4 分红事件", fundamentals_quarterly: "T5 季度基本面",
  holders_snapshot: "T6 前十大股东", index_daily: "T7 指数日线",
  factor_snapshot: "T8 因子快照", macro_rf: "T9 无风险利率",
};

// 区块1：by_source → 每表 {label, total, segs:[{src,count,pct,color}]}（固定 9 表顺序）
const rows = computed(() => {
  const by = (props.pool && props.pool.by_source) || {};
  return Object.entries(by).map(([key, m]) => {
    const total = Object.values(m).reduce((a, b) => a + b, 0);
    const segs = Object.entries(m)
      .sort((a, b) => b[1] - a[1])   // 数字大的段在前（视觉主次）
      .map(([src, count]) => ({
        src, count, total,
        pct: total > 0 ? (count / total) * 100 : 0,
        color: SOURCE_COLORS[src] || UNKNOWN_COLOR,
      }));
    return { key, label: TABLE_LABELS[key] || key, total, segs };
  });
});

// 区块2（v6.1.3）：数据源状态卡。优先用 pool.sources（后端 v6.1.3 数组，固定 5 源
// 顺序 + provides/quota/rate_limit/enabled）；旧形态（无 sources 键）回退 adapters
// 网格语义（provides/quota/rate_limit 空 → 前端显示占位文案），保证向前兼容。
const sources = computed(() => {
  const pool = props.pool || {};
  if (Array.isArray(pool.sources) && pool.sources.length) return pool.sources;
  // 回退：adapters → 最小形态（available/probed_at/latency_ms 复用，enabled=true）
  const ad = pool.adapters || {};
  return Object.entries(ad).map(([name, a]) => ({
    name, enabled: true,
    available: (a && a.available) ?? null,
    probed_at: (a && a.probed_at) ?? null,
    latency_ms: (a && a.latency_ms) ?? null,
    authority: null, provides: [], quota: null, rate_limit: null,
  }));
});

// 连通性三态（✓可达/✗不可达/—未探测；复用 availMark 口径）
function availMark(a) {
  if (a === null || a === undefined) return "—";
  return a ? "✓" : "✗";
}
function connWord(a) {
  if (a === null || a === undefined) return "未探测";
  return a ? "可达" : "不可达";
}
// 字段组 → chip 短名（brief："T2 日K线·OHLCV+amount(主源)" 形式；模板内联使用）
const FIELD_GROUP_CN = {
  ohlcv_amount: "OHLCV+amount", adj_factor: "复权因子", master: "主档",
  valuation: "估值", f10: "F10基本面", ohlcv: "OHLCV", amount: "amount",
};
// 配额行：quota 非 null → 今日 used/budget；否则 rate_limit 文案或"无官方配额"
function quotaText(s) {
  if (s.quota && typeof s.quota === "object") {
    const u = s.quota.used_today == null ? "—" : s.quota.used_today;
    return `今日 ${u}/${s.quota.budget ?? "—"}`;
  }
  return s.rate_limit || "无官方配额";
}

// 区块3：conflict_rows total + 非零表逐行（T8/T9 不在对象里；total 单独取）
const conflicts = computed(() => {
  const cr = (props.pool && props.pool.conflict_rows) || {};
  const total = cr.total ?? 0;
  const nonzero = Object.entries(cr).filter(([k, v]) => k !== "total" && v > 0);
  return { total, nonzero };
});

// 区块4：baostock_probe（progress 既有键透出；null → 未探测占位）
const bsProbe = computed(() => (props.pool && props.pool.baostock_probe) || null);

const stale = computed(() => !!(props.pool && props.pool.stale));

function fmtLatency(ms) {
  if (ms === null || ms === undefined) return "—";
  return ms >= 1000 ? (ms / 1000).toFixed(1) + "s" : ms + "ms";
}
</script>

<template>
  <div class="lake-spp lake-section">
    <!-- backfill_in_progress（或 locked 态无 pool）→ 整块降级占位 -->
    <p v-if="backfill || !pool" class="placeholder lake-spp-off">
      ⏳ 灌数中暂不可用（库被独占写锁持有，完成后自动恢复）
    </p>
    <template v-else>
      <!-- 区块1：采用源分布（by_source 横向堆叠条 + 数字） -->
      <div class="lake-spp-block">
        <div class="lake-spp-title">
          采用源分布（by_source · 每表行数按 source 拆分）
          <span v-if="stale" class="badge lake-st-lagging lake-spp-stale">探测数据过期</span>
        </div>
        <div v-for="r in rows" :key="r.key" class="lake-spp-row">
          <span class="lake-spp-row-label">{{ r.label }}</span>
          <span class="lake-spp-bar" :title="r.segs.map(s => s.src + ':' + s.count).join(' ')">
            <i v-for="(s, i) in r.segs" :key="i" :style="{ width: s.pct + '%', background: s.color }"></i>
          </span>
          <span class="lake-spp-row-total">{{ r.total.toLocaleString("en-US") }}</span>
        </div>
      </div>

      <!-- 区块2（v6.1.3）：数据源状态（每源一张卡；SOURCE_COLORS 左边框/标题着色）。
           四行：连通性（✓可达/✗不可达/—未探测 + latency；enabled=false→"已禁用"灰 badge
           替代 ✓/✗）/ 数据类型（provides chips，role 后缀小字 muted）/ 配额 / 探测时间 -->
      <div class="lake-spp-block">
        <div class="lake-spp-title">
          数据源状态（连通性·数据类型·配额 · 灌数启动时探测）
          <span v-if="stale" class="badge lake-st-lagging lake-spp-stale">探测数据过期</span>
        </div>
        <div class="lake-spp-adapters">
          <div v-for="s in sources" :key="s.name" class="lake-spp-adapter"
               :style="{ borderLeft: '3px solid ' + (SOURCE_COLORS[s.name] || UNKNOWN_COLOR) }">
            <div class="lake-spp-adapter-head">
              <b :style="{ color: SOURCE_COLORS[s.name] || '#475569' }">{{ s.name }}</b>
              <span v-if="s.enabled === false" class="badge lake-st-empty lake-spp-disabled">已禁用</span>
            </div>
            <!-- 行1 连通性（enabled=false → 上方 badge 替代 ✓/✗） -->
            <div v-if="s.enabled !== false" class="lake-spp-conn">
              <span class="lake-spp-avail" :class="{ ok: s.available === true, bad: s.available === false }">
                {{ availMark(s.available) }} {{ connWord(s.available) }}
              </span>
              <span class="muted small mono"> · latency {{ fmtLatency(s.latency_ms) }}</span>
            </div>
            <!-- 行2 数据类型（provides chips；role 后缀小字 muted） -->
            <div class="lake-spp-chips">
              <template v-if="(s.provides || []).length">
                <span v-for="(p, i) in s.provides" :key="i" class="chip lake-spp-chip">
                  {{ p.table_cn }}·{{ FIELD_GROUP_CN[p.field_group] || p.field_group }}<span class="muted small"> {{ p.role }}</span>
                </span>
              </template>
              <span v-else class="muted small">—</span>
            </div>
            <!-- 行3 配额（baostock 今日 used/budget；其余 rate_limit 或"无官方配额"） -->
            <div class="muted small mono">配额：{{ quotaText(s) }}</div>
            <!-- 行4 探测时间 -->
            <div class="muted small mono">探测：{{ s.probed_at || "未探测" }}</div>
          </div>
        </div>
      </div>

      <!-- 区块3：conflict_src（total + 非零表逐行） -->
      <div class="lake-spp-block">
        <div class="lake-spp-title">跨源分歧（conflict_src · T1-T7）</div>
        <div class="lake-spp-conflict">
          <span class="badge" :class="conflicts.total > 0 ? 'lake-st-lagging' : 'lake-st-fresh'">
            total {{ conflicts.total }}
          </span>
          <template v-if="conflicts.nonzero.length">
            <div v-for="[t, n] in conflicts.nonzero" :key="t" class="muted small mono">
              {{ t }}: {{ n }} 行
            </div>
          </template>
          <span v-else class="muted small">无分歧（全部单源一致）</span>
        </div>
      </div>

      <!-- 区块4：BaoStock 恢复探测（Q6） -->
      <div class="lake-spp-block">
        <div class="lake-spp-title">BaoStock 恢复探测（Q6 · 灌数启动探一次）</div>
        <div v-if="bsProbe" class="lake-spp-bs">
          <span class="badge" :class="bsProbe.alive ? 'lake-st-fresh' : 'lake-st-empty'">
            {{ bsProbe.alive ? "✓ alive" : "✗ dead" }}
          </span>
          <span class="muted small mono">{{ bsProbe.at || "—" }}</span>
          <span v-if="bsProbe.elapsed_s != null" class="muted small mono">{{ Number(bsProbe.elapsed_s).toFixed(1) }}s</span>
          <span v-if="bsProbe.detail" class="muted small">{{ bsProbe.detail }}</span>
        </div>
        <span v-else class="muted small">未探测（最近一次灌数未记录 Q6 结果）</span>
      </div>
    </template>
  </div>
</template>
