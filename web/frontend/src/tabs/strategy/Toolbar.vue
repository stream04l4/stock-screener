// 策略工具栏（v6.2 O3：+【💾 另存为】【📂 加载】按钮）。
// D-W02 纪律原样：编辑/保存/取消三按钮的 hidden 状态由 editing prop 唯一驱动——所有进入/退出编辑态
// 的路径（点编辑、保存成功、保存失败保持、取消、加载失败回退）都经过 StrategyTab 的
// setEditing() 唯一入口翻转 editing，本组件只读绑定，保证按钮与标志始终一致。
// O3：另存为/加载在**编辑态下禁用**（防丢未保存修改，按钮置灰 + title 提示）；
//     importing=true 时同样禁用（import 在途）。
<script setup>
defineProps({
  editing: Boolean, // strategyEditing 标志（StrategyTab 持有，setEditing 唯一入口）
  meta: { type: String, default: "" }, // "已加载 config/strategy.yaml · N 个配置段"
  saving: Boolean, // PUT 在途 → 保存按钮禁用（旧版 btn.disabled=true，finally 恢复）
  importing: Boolean, // POST /api/strategy/import 在途 → 另存为/加载禁用
});
const emit = defineEmits(["edit", "save", "cancel", "export", "import"]);
</script>

<template>
  <div class="strategy-toolbar">
    <span id="strategy-meta" class="muted">{{ meta }}</span>
    <button id="btn-strategy-edit" class="primary-btn" :class="{ hidden: editing }" @click="emit('edit')">✏️ 编辑模式</button>
    <button id="btn-strategy-save" class="primary-btn" :class="{ hidden: !editing }" :disabled="saving" @click="emit('save')">💾 保存策略</button>
    <button id="btn-strategy-cancel" class="ghost-btn" :class="{ hidden: !editing }" @click="emit('cancel')">取消</button>
    <span class="toolbar-sep" aria-hidden="true"></span>
    <button id="btn-strategy-export" class="ghost-btn"
            :disabled="editing || importing"
            :title="editing ? '编辑态下禁用（防丢未保存修改）' : '导出完整策略配置快照为 .yaml 文件'"
            @click="emit('export')">💾 另存为</button>
    <button id="btn-strategy-import" class="ghost-btn"
            :disabled="editing || importing"
            :title="editing ? '编辑态下禁用（防丢未保存修改）' : '加载 .yaml 策略文件（整份替换，写前自动备份）'"
            @click="emit('import')">📂 加载</button>
  </div>
</template>
