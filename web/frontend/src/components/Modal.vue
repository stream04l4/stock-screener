// 通用弹框壳（移植 index.html #stock-modal 结构 + app.js initStockModal 行为）。
// - Esc / 遮罩点击 → emit('close')；open=false 时不渲染（内部图表由调用方负责 dispose）。
<script setup>
import { onBeforeUnmount, watch } from "vue";

const props = defineProps({
  open: { type: Boolean, default: false },
  title: { type: String, default: "" },
});
const emit = defineEmits(["close"]);

function onKeydown(e) {
  if (e.key === "Escape") emit("close");
}
watch(
  () => props.open,
  (v) => {
    if (v) document.addEventListener("keydown", onKeydown);
    else document.removeEventListener("keydown", onKeydown);
  },
  { immediate: true }
);
onBeforeUnmount(() => document.removeEventListener("keydown", onKeydown));
</script>

<template>
  <div v-if="open" class="modal">
    <div class="modal-mask" @click="emit('close')"></div>
    <div class="modal-box">
      <div class="modal-head">
        <h3>{{ title }}</h3>
        <button class="mini-btn" title="关闭" @click="emit('close')">✕</button>
      </div>
      <div class="modal-body"><slot /></div>
    </div>
  </div>
</template>
