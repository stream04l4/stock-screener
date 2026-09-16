// 缺失与异常（移植 app.js renderRunDetail 的 notes 卡：missing_fundamental + skipped_groups）。
<script setup>
import { computed } from "vue";

const props = defineProps({ d: { type: Object, required: true } });

const missing = computed(() => props.d.missing_fundamental || []);
// skipped_groups：按数量降序（移植 .sort((a,b) => b[1]-a[1])）
const skipped = computed(() =>
  Object.entries(props.d.skipped_groups || {})
    .sort((a, b) => b[1] - a[1])
    .map(([k, v]) => `${k}（${v}只）`));
</script>

<template>
  <div class="card">
    <h3>缺失与异常</h3>
    <template v-if="missing.length">
      <p class="small muted">基本面数据缺失 {{ missing.length }} 只（维度判不通过）：</p>
      <div class="tbl-wrap">
        <table class="data">
          <thead><tr><th>代码</th><th>名称</th><th>缺失字段</th></tr></thead>
          <tbody>
            <tr v-for="(m, i) in missing" :key="i">
              <td>{{ m.code }}</td><td>{{ m.name }}</td><td>{{ m.missing }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
    <p v-else class="small muted">无基本面数据缺失记录。</p>
    <p v-if="skipped.length" class="small muted">
      行业组不足最小规模、跳过排名约束（{{ skipped.length }} 个组）：
      <span class="small">{{ skipped.join("、") }}</span>
    </p>
  </div>
</template>
