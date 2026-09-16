// 顶栏全局状态条（现 #global-status 的组件化；数据源 = toastStore）。
<script setup>
import { computed } from "vue";
import { useToastStore } from "../stores/toastStore.js";

const t = useToastStore();
// 与旧页一致：成功绿 #4ade80 / 失败红 #f87171；无消息时回退 CSS 默认色（文字为空，视觉等价）。
const color = computed(() => (t.msg ? (t.ok ? "#4ade80" : "#f87171") : undefined));
</script>

<template>
  <div id="global-status" class="global-status" :style="{ color }">{{ t.msg }}</div>
</template>
