<script setup>
// StockPanoramaCard —— 个股全景（/api/lake/stock/{code}；移植 renderLakeStock L1264-1342）。
// 区块：日K线区（LakeKlineChart ECharts candlestick）+ 基础卡 + 估值行 + 因子T8 +
//       最近分红 + T5 最近季报 + T6 前十大股东表。
// - useLiveQuery(`lake:stock:${code}`) TTL60s（报告 §2.1：/stock/{code} 按需+会话缓存）；
//   切股 → key 变化 → 新请求，旧响应自然丢弃（registry 按 key 隔离）。
// - K线区间切换：range ref → `lake:kline:${code}:${range}` 重取（60/120/250/all）。
// - 404 → "数据湖无此股（T1 未灌入）"；409 backfill → 灌数中占位（vanilla 同文案）。
import { computed, ref, watch } from "vue";
import { api, isBackfillErr } from "../../api/client.js";
import { useLiveQuery } from "../../live/useLiveQuery.js";
import { lakeFmt } from "../../utils/fmt.js";
import LakeKlineChart from "./LakeKlineChart.vue";

const props = defineProps({ code: { type: String, default: "" } });
const emit = defineEmits(["error"]);   // 非 404/非 backfill 错误 → 父组件红横幅（vanilla lakeSetError）

// 全景卡（TTL60s；code 为空 → 不订阅不发请求）
const stockQ = useLiveQuery(
  () => (props.code ? `lake:stock:${props.code}` : null),
  () => api(`/api/lake/stock/${props.code}`),
  { ttl: 60000 }
);

// K线（缓存键 ts_code+range+adjust——v6.1.2 P1-A 复权态入 key；切股/切区间/切复权 → key 变 → 重取）
const range = ref("250");   // 默认 250（brief/vanilla lakeKlineRange）
// v6.1.2 P1-A：复权三态切换，**默认前复权 qfq**（A股惯例、最新价=真实价）。
const adjust = ref("qfq");   // none | qfq | hfq
const klineQ = useLiveQuery(
  () => (props.code ? `lake:kline:${props.code}:${range.value}:${adjust.value}` : null),
  () => api(`/api/lake/kline/${props.code}?days=${encodeURIComponent(range.value)}`
            + `&adjust=${encodeURIComponent(adjust.value)}`),
  { ttl: 60000 }
);

const d = computed(() => stockQ.data.value || null);
const base = computed(() => (d.value && d.value.base) || {});
const kRows = computed(() => (klineQ.data.value && klineQ.data.value.rows) || []);
// v6.1.2 P1-A：复权降级提示（adj_factor 全/部分 NULL → 后端带 adjust_note，图表上方小字）
const adjustNote = computed(() => (klineQ.data.value && klineQ.data.value.adjust_note) || "");
// v6.1.2 P1-A：标题复权态文案（原始 / 前复权 / 后复权）
const ADJUST_LABEL = { none: "原始价", qfq: "前复权", hfq: "后复权" };
const adjustLabel = computed(() => ADJUST_LABEL[adjust.value] || "原始价");

// 404/灌数中占位（vanilla selectLakeStock catch 分支文案原样）
const stockState = computed(() => {
  const e = stockQ.error.value;
  if (!props.code) return null;
  if (e && !d.value) {
    if (e.status === 404) return "数据湖无此股（T1 未灌入）";
    if (isBackfillErr(e)) return "⏳ 数据灌入中，个股查询暂不可用——稍后刷新";
    return "加载失败";
  }
  return null;
});

const factors = computed(() => Object.entries((d.value && d.value.factors) || {}));
const f = computed(() => (d.value && d.value.fundamental_latest) || null);
const holders = computed(() => (d.value && d.value.holders_top10) || []);
// v6.1.2 P2-B：近 5 年分红小表（/stock dividends_recent；仅 ready 态出现，空 → []）
const dividends = computed(() => (d.value && d.value.dividends_recent) || []);

// 错误分流（vanilla selectLakeStock catch）：404/backfill → 卡片内占位；其余 → 红横幅
watch(stockQ.error, (e) => {
  if (!e) return;
  if (e.status === 404 || isBackfillErr(e)) return;   // 卡片内已渲染占位，不弹横幅
  emit("error", e.message);
});
</script>

<template>
  <div class="card" id="lake-stock-card">
    <h3>个股全景 <span v-if="code" class="muted small">{{ code }} · {{ base.name || "" }}</span></h3>

    <!-- 未选股占位（vanilla 初始态：lake-stock-body placeholder） -->
    <p v-if="!code" class="placeholder">← 搜索并选择一只股票查看全景（基础 / 估值 / 因子 / 分红 / 前十大股东）</p>

    <!-- 加载态：骨架屏（vanilla lakeSkeleton(5)） -->
    <div v-else-if="!d && !stockState" id="lake-stock-body">
      <div class="lake-loading-label">加载中…</div>
      <div v-for="i in 5" :key="i" class="lake-skeleton"></div>
    </div>

    <!-- 404 / 灌数中 / 加载失败占位 -->
    <p v-else-if="stockState" class="placeholder">{{ stockState }}</p>

    <template v-else>
      <!-- 日K线区（ECharts candlestick；区间按钮 60/120/250/all → days 重取；
           v6.1.2 P1-A：复权三态切换 原始/前复权/后复权，默认前复权） -->
      <div class="lake-section-title">日K线（T2 · {{ adjustLabel }}）</div>
      <LakeKlineChart :rows="kRows" :ts-code="code" :range="range"
                      :adjust="adjust" :adjust-note="adjustNote"
                      :loading="klineQ.loading.value" :error="klineQ.error.value"
                      @range-change="(r) => (range = r)"
                      @adjust-change="(a) => (adjust = a)" />

      <!-- 基础卡（名称/行业/板块/is_st/soe_flag+soe_basis） -->
      <div class="lake-base-grid">
        <div class="lake-kv"><div class="k">名称</div><div class="v">{{ base.name || "—" }}</div></div>
        <div class="lake-kv"><div class="k">行业</div>
          <!-- v6.1.2 P0：只显示 industry_name（已含 CSRC 代码前缀，如"C39计算机…"）。
               v6.1.1 回填后 industry_csric2 有值且 industry_name 本身带代码前缀 →
               旧写法 join 出 "C39 C39计算机…" 重复。MarketTable 行业列(L110)与 /search
               下拉(sr-ind)均只用 industry_name，无此问题（TL 复查确认）。 -->
          <div class="v">{{ base.industry_name || "—" }}</div></div>
        <div class="lake-kv"><div class="k">板块</div><div class="v">{{ base.board || "—" }}</div></div>
        <div class="lake-kv"><div class="k">ST</div><div class="v">{{ base.is_st ? "是" : "否" }}</div></div>
        <div class="lake-kv"><div class="k">央国企</div>
          <div class="v">{{ (base.soe_flag || "—") + (base.soe_basis ? "（" + base.soe_basis + "）" : "") }}</div></div>
      </div>

      <!-- 估值行 -->
      <div class="lake-section-title">估值</div>
      <div class="lake-base-grid">
        <div class="lake-kv"><div class="k">总市值</div><div class="v">{{ lakeFmt(base.total_mv, "yi") }}</div></div>
        <div class="lake-kv"><div class="k">流通市值</div><div class="v">{{ lakeFmt(base.float_mv, "yi") }}</div></div>
        <div class="lake-kv"><div class="k">PE(TTM)</div><div class="v">{{ lakeFmt(base.pe_ttm, "num") }}</div></div>
        <div class="lake-kv"><div class="k">PB</div><div class="v">{{ lakeFmt(base.pb, "num") }}</div></div>
        <div class="lake-kv"><div class="k">换手率</div><div class="v">{{ lakeFmt(base.turnover_pct, "pct") }}</div></div>
        <div class="lake-kv"><div class="k">TTM股息率</div><div class="v">{{ lakeFmt(base.ttm_yield_pct, "pct") }}</div></div>
      </div>

      <!-- 因子网格（T8） -->
      <div class="lake-section-title">因子（T8{{ d.factors_as_of ? " · " + d.factors_as_of : "" }}）</div>
      <p v-if="!factors.length" class="muted small">暂无数据（后台补齐中）</p>
      <div v-else class="lake-factor-grid">
        <div v-for="[k, v] in factors" :key="k" class="lake-factor">
          <div class="k">{{ k }}</div><div class="v">{{ lakeFmt(v, "num") }}</div>
        </div>
      </div>

      <!-- 最近分红（最新一次 kv + v6.1.2 P2-B 近5年小表） -->
      <div class="lake-section-title">最近分红</div>
      <div class="lake-base-grid">
        <div class="lake-kv"><div class="k">除权日</div><div class="v">{{ base.last_ex_date || "—" }}</div></div>
        <div class="lake-kv"><div class="k">每股分红(元)</div><div class="v">{{ lakeFmt(base.last_cash_dps, "num") }}</div></div>
      </div>
      <!-- v6.1.2 P2-B：近 5 年分红小表（除权日/每股分红/股息率；空态"近 5 年无分红记录"） -->
      <p v-if="!dividends.length" class="muted small">近 5 年无分红记录</p>
      <div v-else class="tbl-wrap">
        <table class="data" id="lake-dividends-table">
          <thead><tr><th>除权日</th><th class="num">每股分红(元)</th><th class="num">股息率%</th></tr></thead>
          <tbody>
            <tr v-for="(r, i) in dividends" :key="i">
              <td class="mono small">{{ r.ex_date || "—" }}</td>
              <td class="num">{{ lakeFmt(r.cash_dps, "num") }}</td>
              <td class="num">{{ lakeFmt(r.dividend_yield_pct, "pct") }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- T5 最近季 -->
      <div class="lake-section-title">最近季报（T5）</div>
      <p v-if="!f" class="muted small">暂无数据（后台补齐中）</p>
      <div v-else class="lake-base-grid">
        <div class="lake-kv"><div class="k">报告期</div><div class="v">{{ f.period || "—" }}</div></div>
        <div class="lake-kv"><div class="k">披露日</div><div class="v">{{ f.pub_date || "—" }}</div></div>
        <div class="lake-kv"><div class="k">ROE(平均)</div><div class="v">{{ lakeFmt(f.roe_avg, "pct") }}</div></div>
        <div class="lake-kv"><div class="k">ROE(加权)</div><div class="v">{{ lakeFmt(f.roe_weighted, "pct") }}</div></div>
        <div class="lake-kv"><div class="k">净利同比</div><div class="v">{{ lakeFmt(f.yoy_pni, "pct") }}</div></div>
        <div class="lake-kv"><div class="k">毛利率</div><div class="v">{{ lakeFmt(f.gross_margin, "pct") }}</div></div>
        <div class="lake-kv"><div class="k">资产负债率</div><div class="v">{{ lakeFmt(f.liability_pct, "pct") }}</div></div>
      </div>

      <!-- 前十大股东表（T6） -->
      <div class="lake-section-title">前十大股东（T6 · 流通股口径）</div>
      <p v-if="!holders.length" class="muted small">暂无数据（后台补齐中）</p>
      <div v-else class="tbl-wrap">
        <table class="data">
          <thead><tr><th>#</th><th>股东名称</th><th class="num">占流通股%</th><th>股本性质</th></tr></thead>
          <tbody>
            <tr v-for="r in holders" :key="r.holder_rank">
              <td>{{ r.holder_rank }}</td><td>{{ r.holder_name }}</td>
              <td class="num">{{ lakeFmt(r.hold_ratio, "pct") }}</td>
              <td>{{ r.share_nature || "—" }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
  </div>
</template>
