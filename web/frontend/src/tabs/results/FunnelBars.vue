// 漏斗条形图（移植 app.js renderFunnel；纯 div 非图表库）。
// 比例基准 = 最大正数（L0）；pct = max(2, |c|/base*100)；负值条加 .neg（琥珀渐变）。
<script setup>
import { computed } from "vue";

const props = defineProps({ funnel: { type: Array, default: () => [] } });

// 移植 renderFunnel 的比例计算（含 count==null → 0 与 minNeg 跟踪，保持逐字一致）
const items = computed(() => {
  let max = 0, minNeg = 0;
  for (const f of props.funnel) {
    const c = f.count == null ? 0 : f.count;
    if (c > max) max = c;
    if (c < minNeg) minNeg = c;
  }
  const base = Math.max(max, 1);
  return props.funnel.map((f) => {
    const c = f.count == null ? 0 : f.count;
    return {
      label: f.label,
      desc: f.desc || "",
      count: f.count,
      pct: Math.max(2, Math.abs(c) / base * 100),
      neg: c < 0,
    };
  });
});
</script>

<template>
  <div class="funnel-box">
    <div v-for="(f, i) in items" :key="i">
      <div class="funnel-row">
        <div class="funnel-label" :title="f.desc || f.label">{{ f.label }}</div>
        <div class="funnel-bar-track">
          <div class="funnel-bar" :class="{ neg: f.neg }" :style="{ width: f.pct + '%' }"></div>
        </div>
        <div class="funnel-val">{{ f.count == null ? "—" : String(f.count) }}</div>
      </div>
      <div v-if="f.desc" class="funnel-desc">{{ f.desc }}</div>
    </div>
  </div>
</template>
