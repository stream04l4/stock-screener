// 字段编辑器（v7.0：按 FIELD_TYPE 分支）。
// int/float → number input；bool → select(true/false)；enum2 → select(ENUM_OPTIONS，缺省回退当前值)；
// list → text（逗号分隔）；str → text；readonly → 固定值只读展示。
// 所有变更经 emit("update", {section, field, value}) 上报父组件 draft（保存时统一校验/合并）。
// v7.0：weights_dict/sub_weights_dict 分支已删（多因子权重随策略核心移除）。
<script setup>
import { computed, ref } from "vue";
import { FIELD_TYPE, ENUM_OPTIONS, readableValue } from "../../data/strategyMeta.js";

const props = defineProps({ section: String, field: String, value: {} });
const emit = defineEmits(["update"]);

const t = FIELD_TYPE[props.field] || "str"; // 旧版：未知类型回退 str

// ---- 标量字段：input 事件直报原始字符串（校验/转换在父组件保存时，语义同旧版）----
function onScalarInput(ev) {
  emit("update", { section: props.section, field: props.field, value: ev.target.value });
}

const enumOpts = computed(
  () => ENUM_OPTIONS[`${props.section}.${props.field}`] || [[String(props.value), String(props.value)]]
);
</script>

<template>
  <!-- int / float -->
  <input v-if="t === 'int' || t === 'float'" type="number" :step="t === 'int' ? '1' : 'any'"
         :value="String(value)" @input="onScalarInput">
  <!-- bool -->
  <select v-else-if="t === 'bool'" :value="value ? 'true' : 'false'" @change="onScalarInput">
    <option value="true">true</option>
    <option value="false">false</option>
  </select>
  <!-- enum2 -->
  <select v-else-if="t === 'enum2'" :value="String(value)" @change="onScalarInput">
    <option v-for="[val, label] in enumOpts" :key="val" :value="val">{{ label }}</option>
  </select>
  <!-- list -->
  <input v-else-if="t === 'list'" type="text"
         :value="Array.isArray(value) ? value.join(', ') : String(value)"
         placeholder="B06, B07, C25, D44, …" @input="onScalarInput">
  <!-- readonly：固定值（accounting=cash 等）——只读展示 -->
  <div v-else-if="t === 'readonly'" class="s-readonly" :title="'固定值（只读）'">
    {{ readableValue(value) }}
  </div>
  <!-- str / 未知类型 -->
  <input v-else type="text" :value="String(value)" @input="onScalarInput">
</template>
