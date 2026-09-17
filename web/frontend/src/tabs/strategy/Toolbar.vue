// 策略工具栏（移植 index.html .strategy-toolbar + app.js initStrategyButtons）。
// D-W02 纪律原样：三个按钮的 hidden 状态由 editing prop 唯一驱动——所有进入/退出编辑态
// 的路径（点编辑、保存成功、保存失败保持、取消、加载失败回退）都经过 StrategyTab 的
// setEditing() 唯一入口翻转 editing，本组件只读绑定，保证按钮与标志始终一致。
<script setup>
defineProps({
  editing: Boolean, // strategyEditing 标志（StrategyTab 持有，setEditing 唯一入口）
  meta: { type: String, default: "" }, // "已加载 config/strategy.yaml · N 个配置段"
  saving: Boolean, // PUT 在途 → 保存按钮禁用（旧版 btn.disabled=true，finally 恢复）
});
const emit = defineEmits(["edit", "save", "cancel"]);
</script>

<template>
  <div class="strategy-toolbar">
    <span id="strategy-meta" class="muted">{{ meta }}</span>
    <button id="btn-strategy-edit" class="primary-btn" :class="{ hidden: editing }" @click="emit('edit')">✏️ 编辑模式</button>
    <button id="btn-strategy-save" class="primary-btn" :class="{ hidden: !editing }" :disabled="saving" @click="emit('save')">💾 保存策略</button>
    <button id="btn-strategy-cancel" class="ghost-btn" :class="{ hidden: !editing }" @click="emit('cancel')">取消</button>
  </div>
</template>
