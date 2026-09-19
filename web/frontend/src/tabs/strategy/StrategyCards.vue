// 策略卡片（v6.2 O1：三段分组 + section 级说明 + 折叠/展开）。
// - 只读态：字段值 displayValue() 格式化展示 + FIELD_DESC 说明；
// - 编辑态：FieldEditor 控件，draft 值下行、变更上行（emit update → StrategyTab 合并进 draft）；
//   **desc 两种态都显示**（O2：编辑态控件下方 muted small，只读态位置保留）。
// - 分组（SECTION_GROUPS，有序）：📌策略核心 / 🧪回测与再投资 / ⚙️数据与基础设施。
//   折叠/展开状态存 localStorage（strategy_group_collapsed_v1）；**编辑态强制全部展开防漏改**。
// SECTION_META/SECTION_DESC/SECTION_GROUPS/FIELD_DESC/FIELD_TYPE 来自 src/data/strategyMeta.js。
<script setup>
import { computed, ref } from "vue";
import { SECTION_META, SECTION_DESC, SECTION_GROUPS, FIELD_DESC, displayValue } from "../../data/strategyMeta.js";
import FieldEditor from "./FieldEditor.vue";

const props = defineProps({
  json: Object, // GET /api/strategy 返回的 d.json（只读态数据源）
  draft: Object, // 编辑态草稿（StrategyTab 持有；= JSON 深拷贝 + 编辑值覆盖）
  editing: Boolean,
});
const emit = defineEmits(["update"]);

// ---------------------------------------------------------------------------
// 分组折叠状态（O1：localStorage 记忆；编辑态强制全部展开防漏改）
// ---------------------------------------------------------------------------
const LS_KEY = "strategy_group_collapsed_v1";
function readCollapsed() {
  try { return JSON.parse(localStorage.getItem(LS_KEY) || "{}"); } catch { return {}; }
}
// 首次访问：defaultOpen=false 的组默认折叠；用户操作后以 localStorage 为准。
const collapsed = ref(readCollapsed());
function isCollapsed(g) {
  if (props.editing) return false; // 编辑态强制展开（防漏改）
  const c = collapsed.value[g.id];
  return c === undefined ? !g.defaultOpen : !!c;
}
function toggleGroup(g) {
  if (props.editing) return; // 编辑态不可折叠
  collapsed.value = { ...collapsed.value, [g.id]: !isCollapsed(g) };
  try { localStorage.setItem(LS_KEY, JSON.stringify(collapsed.value)); } catch { /* 隐私模式等 → 忽略 */ }
}

// json 中未归入任何分组的 section（防御：yaml 新增段但 SECTION_GROUPS 未更新时仍可见，
// 追加到末组末尾；正常 16 段全在分组内）。
const ungrouped = computed(() => {
  const grouped = new Set(SECTION_GROUPS.flatMap((g) => g.sections));
  return Object.keys(props.json || {}).filter((s) => !grouped.has(s));
});
</script>

<template>
  <div id="strategy-cards" class="cards-groups">
    <section v-for="g in SECTION_GROUPS" :key="g.id" class="s-group">
      <header class="s-group-head" :class="{ open: !isCollapsed(g) }">
        <button type="button" class="s-group-toggle" :disabled="editing"
                :title="editing ? '编辑态强制展开（防漏改）' : (isCollapsed(g) ? '展开本组' : '折叠本组')"
                @click="toggleGroup(g)">
          <span class="s-group-caret">{{ isCollapsed(g) ? "▸" : "▾" }}</span>
          <span class="s-group-icon">{{ g.icon }}</span>
          <span class="s-group-title">{{ g.title }}</span>
          <span class="s-group-count muted">（{{ (g.sections.length + (g === SECTION_GROUPS[SECTION_GROUPS.length - 1] ? ungrouped.length : 0)) }} 段）</span>
        </button>
      </header>
      <div v-show="!isCollapsed(g)" class="cards">
        <template v-for="section in [...g.sections, ...(g === SECTION_GROUPS[SECTION_GROUPS.length - 1] ? ungrouped : [])]" :key="section">
          <div v-if="(json || {})[section] && typeof (json || {})[section] === 'object'" class="s-card">
            <h4><span class="s-icon">{{ (SECTION_META[section] || { icon: "🔧" }).icon }}</span>{{ (SECTION_META[section] || { title: section }).title }}</h4>
            <p v-if="SECTION_DESC[section]" class="s-sec-desc">{{ SECTION_DESC[section] }}</p>
            <div v-for="[field, value] in Object.entries((json || {})[section])" :key="field" class="s-field">
              <div class="s-field-head">
                <span class="s-key">{{ field }}</span>
                <span v-if="!editing" class="s-val">{{ displayValue(section, field, value) }}</span>
              </div>
              <FieldEditor v-if="editing" :section="section" :field="field" :value="(draft?.[section] || {})[field]" @update="(u) => emit('update', u)" />
              <!-- O2：desc 只读态与编辑态都显示（编辑态 = 控件下方 muted small） -->
              <div v-if="FIELD_DESC[section + '.' + field]" class="s-desc">{{ FIELD_DESC[section + '.' + field] }}</div>
            </div>
          </div>
        </template>
      </div>
    </section>
  </div>
</template>
