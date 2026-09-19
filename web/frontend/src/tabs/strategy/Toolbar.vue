// 策略工具栏（v6.2.1 S1：策略库——【💾 另存为】弹窗命名保存 + 【📂 加载】下拉选择 + 🗑️ 删除）。
// D-W02 纪律原样：编辑/保存/取消三按钮的 hidden 状态由 editing prop 唯一驱动——所有进入/退出编辑态
// 的路径（点编辑、保存成功、保存失败保持、取消、加载失败回退）都经过 StrategyTab 的
// setEditing() 唯一入口翻转 editing，本组件只读绑定，保证按钮与标志始终一致。
// S1：另存为/加载下拉/删除在**编辑态下禁用**（防丢未保存修改，置灰 + title 提示）；
//     libLoading=true（策略库列表拉取中）时同样禁用。
// 交互最简原则（brief S1"二选一"选此项）：删除 = 下拉旁的单个 🗑️ 按钮，删**当前选中项**
// （confirm 二次确认由父组件执行），不做每行图标。
<script setup>
const props = defineProps({
  editing: Boolean, // strategyEditing 标志（StrategyTab 持有，setEditing 唯一入口）
  meta: { type: String, default: "" }, // "已加载 config/strategy.yaml · N 个配置段"
  saving: Boolean, // PUT 在途 → 保存按钮禁用（finally 恢复）
  libLoading: Boolean, // GET /api/strategies 在途 → 另存为/加载/删除禁用
  strategies: { type: Array, default: () => [] }, // 策略库列表 [{name, saved_at, size_bytes}]（saved_at 降序）
  selected: { type: String, default: "" }, // 下拉当前选中值（"" = 占位"— 选择策略 —"）
});
const emit = defineEmits([
  "edit", "save", "cancel",
  "save-as",        // 【💾 另存为】→ 父组件弹命名弹窗
  "load-select",    // 下拉 change（值为策略名；占位 "" 不触发——@change 只在用户改选时 fire）
  "delete-selected", // 🗑️ 删除当前选中项（父组件 confirm 二次确认后 DELETE）
]);

// 下拉 change：emit 给父组件（confirm 判定是**同步**的——window.confirm 阻塞）。
// emit 返回后若 selected prop 未变（父组件拒绝，如 confirm 取消）→ 手动复位 DOM，
// 防"用户选中的选项仍显示在下拉里但状态是占位"的漂移（Vue 视角 prop 无变化不会碰 DOM）。
function onSelChange(e) {
  const v = e.target.value;
  emit("load-select", v);
  if (props.selected !== v) e.target.value = "";
}
</script>

<template>
  <div class="strategy-toolbar">
    <span id="strategy-meta" class="muted">{{ meta }}</span>
    <button id="btn-strategy-edit" class="primary-btn" :class="{ hidden: editing }" @click="emit('edit')">✏️ 编辑模式</button>
    <button id="btn-strategy-save" class="primary-btn" :class="{ hidden: !editing }" :disabled="saving" @click="emit('save')">💾 保存策略</button>
    <button id="btn-strategy-cancel" class="ghost-btn" :class="{ hidden: !editing }" @click="emit('cancel')">取消</button>
    <span class="toolbar-sep" aria-hidden="true"></span>
    <button id="btn-strategy-save-as" class="ghost-btn"
            :disabled="editing || libLoading"
            :title="editing ? '编辑态下禁用（防丢未保存修改）' : '把当前策略配置命名保存到本机策略库'"
            @click="emit('save-as')">💾 另存为</button>
    <span class="toolbar-sep" aria-hidden="true"></span>
    <label id="strategy-load-label" class="lib-label" for="select-strategy-load">📂 加载</label>
    <select id="select-strategy-load" class="lib-select" :value="selected"
            :disabled="editing || libLoading"
            :title="editing ? '编辑态下禁用（防丢未保存修改）' : '从本机策略库加载（选中即覆盖当前配置，写前自动备份）'"
            @change="onSelChange">
      <option value="">— 选择策略 —</option>
      <option v-for="s in strategies" :key="s.name" :value="s.name">
        {{ s.name }}（{{ s.saved_at }}）
      </option>
    </select>
    <button id="btn-strategy-delete" class="ghost-btn lib-del"
            :disabled="editing || libLoading || !selected"
            :title="editing ? '编辑态下禁用（防丢未保存修改）' : (selected ? `删除策略库中的「${selected}」` : '先在左侧下拉选择要删除的策略')"
            @click="emit('delete-selected')">🗑️ 删除</button>
  </div>
</template>
