// 顶栏全局状态条（现 #global-status 的组件化；数据源 = toastStore）。
<script setup>
import { computed } from "vue";
import { useToastStore } from "../stores/toastStore.js";

const t = useToastStore();
// 与旧页一致：成功绿 var(--good-bright) / 失败红 var(--bad-bright)；无消息时回退 CSS 默认色（文字为空，视觉等价）。
const color = computed(() => (t.msg ? (t.ok ? "var(--good-bright)" : "var(--bad-bright)") : undefined));
</script>

<template>
  <div id="global-status" class="global-status" :style="{ color }">{{ t.msg }}</div>
</template>
