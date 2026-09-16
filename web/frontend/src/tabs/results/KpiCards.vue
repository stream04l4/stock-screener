// KPI 卡片（移植 app.js renderKpiCards；数据来自 parse_report 的「一、KPI 概览」）。
<script setup>
import { fmtNum } from "../../utils/fmt.js";

const props = defineProps({ kpi: { type: Object, default: null } });
</script>

<template>
  <div v-if="kpi" class="kpi-cards">
    <div class="kpi-card">
      <div class="kpi-val">{{ kpi.selected == null ? "—" : String(kpi.selected) }}</div>
      <div class="kpi-title">入选数（Top N）</div>
      <div class="kpi-desc">入选股票数量</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-val">{{ kpi.avg_ttm_yield_pct == null ? "—" : fmtNum(kpi.avg_ttm_yield_pct, 2) + "%" }}</div>
      <div class="kpi-title">平均TTM股息率</div>
      <div class="kpi-desc">入选股 TTM 滚动股息率均值</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-val">{{ kpi.avg_roe_pct == null ? "—" : fmtNum(kpi.avg_roe_pct, 1) + "%" }}</div>
      <div class="kpi-title">平均ROE</div>
      <div class="kpi-desc">入选股最近年报 ROE 均值</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-val">{{ kpi.industry_concentration || "—" }}</div>
      <div class="kpi-title">行业集中度</div>
      <div class="kpi-desc">Top1 行业占比 / HHI×100</div>
    </div>
  </div>
</template>
