// 字段编辑器（移植 app.js editControl L832-890：按 FIELD_TYPE 分支）。
// int/float → number input；bool → select(true/false)；enum2 → select(ENUM_OPTIONS，缺省回退当前值)；
// list → text（逗号分隔）；str → text；
// weights_dict → 4 滑块（0–1 step .01，实时显示原始值 + 归一化值，和≠1 警告"保存将被拒绝"）；
// sub_weights_dict → 每维度一行 key:值,key:值 文本（parseSubWeightsText 语义同旧版）。
// 所有变更经 emit("update", {section, field, value}) 上报父组件 draft（保存时统一校验/合并）。
<script setup>
import { computed, ref } from "vue";
import { FIELD_TYPE, ENUM_OPTIONS, WEIGHT_DIMS, parseSubWeightsText } from "../../data/strategyMeta.js";

const props = defineProps({ section: String, field: String, value: {} });
const emit = defineEmits(["update"]);

const t = FIELD_TYPE[props.field] || "str"; // 旧版：未知类型回退 str

// ---- weights_dict：4 滑块（本地 model 镜像 draft；input → 更新 + emit）----
const wModel = ref({});
if (t === "weights_dict") {
  for (const [dim] of WEIGHT_DIMS) wModel.value[dim] = Number(props.value?.[dim]) || 0;
}
const wSum = computed(() => WEIGHT_DIMS.reduce((a, [d]) => a + (Number(wModel.value[d]) || 0), 0));
function onWInput(dim, el) {
  wModel.value[dim] = Number(el.value);
  emit("update", { section: props.section, field: props.field, value: { ...wModel.value } });
}
// 旧版 refresh()：归一化值 +（和≠1 → "保存将被拒绝"警告）；sum=0 → "—"
function wNormText(dim) {
  const sum = wSum.value;
  if (sum <= 0) return "归一化 —";
  const txt = (Number(wModel.value[dim]) / sum).toFixed(3);
  return `归一化 ${txt}` + (Math.abs(sum - 1) > 1e-9 ? `（和=${sum.toFixed(2)}≠1，保存将被拒绝）` : "");
}

// ---- sub_weights_dict：每维度 key:值 文本（本地 model 镜像 draft，含全部 4 维——
//      emit 始终带完整 {dim: parsed} 对象，保证 draft[sec].sub_weights 形状稳定）----
const swModel = ref({});
if (t === "sub_weights_dict") {
  for (const [dim] of WEIGHT_DIMS) {
    const m = props.value?.[dim] || {};
    swModel.value[dim] = Object.entries(m).map(([k, x]) => `${k}:${x}`).join(",");
  }
}
function onSWInput(dim, el) {
  swModel.value[dim] = el.value;
  // 实时解析上报（合法 → 对象；非法 → null，保存时统一报错）
  const full = {};
  for (const [d2] of WEIGHT_DIMS) full[d2] = parseSubWeightsText(swModel.value[d2]);
  emit("update", { section: props.section, field: props.field, value: full });
}

// ---- 标量字段：input 事件直报原始字符串（校验/转换在父组件保存时，语义同旧版）----
function onScalarInput(ev) {
  emit("update", { section: props.section, field: props.field, value: ev.target.value });
}

const enumOpts = computed(() => ENUM_OPTIONS[`${props.section}.${props.field}`] || [[String(props.value), String(props.value)]]);
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
         placeholder="sh.60, sh.68, sz.00, sz.30" @input="onScalarInput">
  <!-- weights_dict：4 滑块 -->
  <div v-else-if="t === 'weights_dict'" class="weight-sliders">
    <div v-for="[dim, label] in WEIGHT_DIMS" :key="dim" class="w-row">
      <span class="w-label">{{ label }}</span>
      <input type="range" min="0" max="1" step="0.01" :value="wModel[dim]" @input="onWInput(dim, $event.target)">
      <span class="w-raw">{{ Number(wModel[dim]).toFixed(2) }}</span>
      <span class="w-norm">{{ wNormText(dim) }}</span>
    </div>
  </div>
  <!-- sub_weights_dict：每维度 key:值 文本 -->
  <div v-else-if="t === 'sub_weights_dict'" class="subweight-edits">
    <div v-for="[dim] in WEIGHT_DIMS" :key="dim" class="w-row">
      <span class="w-label">{{ dim }}</span>
      <input type="text" :value="swModel[dim]" placeholder="key:值,key:值" @input="onSWInput(dim, $event.target)">
    </div>
  </div>
  <!-- str / 未知类型 -->
  <input v-else type="text" :value="String(value)" @input="onScalarInput">
</template>
