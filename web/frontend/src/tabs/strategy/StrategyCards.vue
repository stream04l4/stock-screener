// 策略卡片（移植 app.js renderStrategyCards L799-816：section 卡 + field 行）。
// - 只读态：字段值 displayValue() 格式化展示 + FIELD_DESC 说明；
// - 编辑态：FieldEditor 控件，draft 值下行、变更上行（emit update → StrategyTab 合并进 draft）。
// SECTION_META/FIELD_DESC/FIELD_TYPE/ENUM_OPTIONS 来自 src/data/strategyMeta.js。
<script setup>
import { SECTION_META, FIELD_DESC, displayValue } from "../../data/strategyMeta.js";
import FieldEditor from "./FieldEditor.vue";

defineProps({
  json: Object, // GET /api/strategy 返回的 d.json（只读态数据源）
  draft: Object, // 编辑态草稿（StrategyTab 持有；= JSON 深拷贝 + 编辑值覆盖）
  editing: Boolean,
});
const emit = defineEmits(["update"]);
</script>

<template>
  <div id="strategy-cards" class="cards">
    <template v-for="[section, fields] in Object.entries(json || {})" :key="section">
      <div v-if="fields && typeof fields === 'object'" class="s-card">
        <h4><span class="s-icon">{{ (SECTION_META[section] || { icon: "🔧" }).icon }}</span>{{ (SECTION_META[section] || { title: section }).title }}</h4>
        <div v-for="[field, value] in Object.entries(fields)" :key="field" class="s-field">
          <div class="s-field-head">
            <span class="s-key">{{ field }}</span>
            <span v-if="!editing" class="s-val">{{ displayValue(section, field, value) }}</span>
          </div>
          <FieldEditor v-if="editing" :section="section" :field="field" :value="(draft?.[section] || {})[field]" @update="(u) => emit('update', u)" />
          <div v-else-if="FIELD_DESC[section + '.' + field]" class="s-desc">{{ FIELD_DESC[section + '.' + field] }}</div>
        </div>
      </div>
    </template>
  </div>
</template>
