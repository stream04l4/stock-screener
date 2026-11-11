// 策略 tab（报告 §3 T-策略）：Toolbar + StrategyCards + 编辑/保存状态机。
//
// **D-W02 纪律原样**：editing ref 是 strategyEditing 标志的 Pinia 化；setEditing(on) 是唯一入口——
// 所有进入/退出编辑态的路径（点编辑、保存成功、保存失败保持、取消、加载失败回退）都经过它，
// Toolbar 三按钮 hidden 状态只读绑定 editing，保证按钮与标志始终一致（旧版注释原样）。
//
// 保存语义原样（app.js saveStrategy L928-988）：
// - payload = 当前 API JSON 深拷贝为底 + 编辑值覆盖（保证所有 key 齐全）；
// - 逐字段校验（int/float/bool/list/weights 0–1/sub_weights 格式），任一非法 → msg-box 报错、不提交；
// - PUT 成功 → **用 API 返回 JSON 为底重渲染**（旧版 setTimeout(loadStrategy,600)：重拉 GET）
//   + toast"策略已更新"+ msg"✓ 策略已保存（备份: <文件名>）"，经 setEditing(false) 退出编辑态；
// - 400 → **保持编辑态**显示 detail.errors（用户可改正后重试），toast"策略保存失败"；
// - 取消 → setEditing(false) + 重拉 GET。
// v7.0.29 — 策略库退役（Joel 定案：后续策略不太会大变）：另存为/下拉加载/删除全删，
// 本 tab 只保留【编辑】【保存】【取消】= 编辑→保存应用到当前活动策略（PUT strategy.yaml）。
// v6.2.1 S2 — 页面归属调整：本 tab 只渲染 SECTION_GROUPS（8 核心段 + ⚙️高级配置折叠组）；
// backtest/reinvest 移到 BacktestTab、lake 段不再渲染（yaml-only）。
// 报告 §2.1：GET /api/strategy → 按需+事件触发（保存/取消后 invalidate）。
<script setup>
import { computed, onMounted, ref, watch } from "vue";
import { api } from "../api/client.js";
import { useLiveQuery } from "../live/useLiveQuery.js";
import { invalidate } from "../live/queryRegistry.js";
import { useToastStore } from "../stores/toastStore.js";
import { FIELD_TYPE, FIELD_LABELS, EXCLUDED_SECTIONS } from "../data/strategyMeta.js";
import Toolbar from "./strategy/Toolbar.vue";
import StrategyCards from "./strategy/StrategyCards.vue";

const toast = useToastStore();

// 按需拉取（首次进 tab 才发请求——懒加载验收点；TTL 0 = 事件触发刷新）
const { data, loading, error } = useLiveQuery("strategy", () => api("/api/strategy"), {});

const strategyJson = ref(null); // GET 成功后的 d.json（只读态数据源 + 保存 payload 底）
const editing = ref(false);     // D-W02：strategyEditing 标志
const draft = ref(null);        // 编辑态草稿（JSON 深拷贝 + 编辑值覆盖）
const saving = ref(false);      // PUT 在途（保存按钮禁用，finally 恢复）
const msg = ref({ text: "", kind: null }); // msg-box：ok/err

// D-W02：唯一入口。所有进入/退出编辑态的路径必须经过它。
function setEditing(on) {
  editing.value = !!on;
}

// GET 数据到达 → 更新只读底（加载成功 → 统一回到只读态，幂等）
watch(
  () => data.value,
  (d) => {
    if (!d || !d.json) return;
    strategyJson.value = d.json;
    setEditing(false); // D-W02：加载成功后统一回到只读态（幂等）
  },
  { immediate: true }
);

// D-W02：加载失败回退也必须经唯一入口（旧版 loadStrategy catch → setStrategyEditing(false)，
// 不让工具栏残留幽灵编辑态）。SWR 语义下后台刷新失败保留旧值、editing 不受影响。
watch(
  () => error.value,
  (e) => { if (e && !strategyJson.value) setEditing(false); },
  { immediate: true }
);

const metaText = computed(() =>
  strategyJson.value ? `已加载 config/strategy.yaml · ${Object.keys(strategyJson.value).length} 个配置段` : ""
);

// ---------------------------------------------------------------------------
// 编辑态进入（旧版 btn-strategy-edit handler）
// ---------------------------------------------------------------------------
function onEdit() {
  if (!strategyJson.value) return;
  draft.value = JSON.parse(JSON.stringify(strategyJson.value)); // 深拷贝为底
  setEditing(true); // D-W02：唯一入口，按钮 class 与标志同步翻转
  msg.value = { text: "", kind: null };
}

// FieldEditor 变更 → 合并进 draft（weights_dict/sub_weights_dict 整体替换该字段对象）
function onDraftUpdate(u) {
  if (!draft.value) return;
  draft.value[u.section] = draft.value[u.section] || {};
  draft.value[u.section][u.field] = u.value;
}

// ---------------------------------------------------------------------------
// 保存（移植 saveStrategy：校验 → PUT → 成功重拉 / 400 保持编辑态）
// ---------------------------------------------------------------------------
async function onSave() {
  if (!draft.value) return;
  const payload = JSON.parse(JSON.stringify(draft.value)); // 深拷贝提交
  let bad = false;

  for (const [section, fields] of Object.entries(payload)) {
  if (!fields || typeof fields !== "object") continue;
  for (const [field, value] of Object.entries(fields)) {
    const t = FIELD_TYPE[field] || "str";
    const label = FIELD_LABELS[`${section}.${field}`] || field; // 报错用通俗名（原始名排查时仍看 yaml）
    let v;
    if (t === "int") {
      v = Number.isInteger(Number(value)) ? parseInt(value, 10) : NaN;
      if (isNaN(v)) { setMsg(`✗ ${label} 必须是整数`, "err"); bad = true; continue; }
    } else if (t === "float") {
      v = parseFloat(value);
      if (isNaN(v)) { setMsg(`✗ ${label} 必须是数值`, "err"); bad = true; continue; }
    } else if (t === "bool") {
      v = value === "true" || value === true;
    } else if (t === "list") {
      v = String(value).split(",").map((s) => s.trim()).filter(Boolean);
      if (!v.length) { setMsg(`✗ ${label} 不能为空`, "err"); bad = true; continue; }
    } else {
      v = value; // str / enum2 / readonly：原样（enum 值域由后端校验，400 回显）
    }
    payload[section][field] = v;
  }
  if (bad) break;
  }
  if (bad) return; // 本地校验失败 → 不提交（旧版语义原样）

  saving.value = true;
  try {
    const res = await api("/api/strategy", { method: "PUT", body: JSON.stringify(payload) });
    // v7.0.41：保存后后端依次重算十年回测表（~2s）+ 选股时机表（~1s）；
    // 湖被锁时各自降级 = 下次 11:30 同步生效
    const sr = res.selection_rescan;
    const selNote = sr
      ? (sr.refreshed ? `；十年回测表已重算（${sr.n_written} 只）`
                      : `；十年回测表稍后重算（${sr.reason}）`)
      : "";
    const tr = res.timing_rescan;
    const rescanNote = tr
      ? (tr.refreshed ? `；选股时机已重算（今日选股 ${tr.n_watchlist} 只）`
                      : `；选股时机稍后重算（${tr.reason}）`)
      : "";
    setMsg(`✓ 策略已保存（备份: ${String(res.backup).split("/").pop()}）${selNote}${rescanNote}`, "ok");
    // D-W02：保存成功 → 经唯一入口退出编辑态，再刷新只读视图（旧版 setTimeout(loadStrategy,600)）
    setEditing(false);
    toast.toast("策略已更新");
    invalidate("strategy"); // 事件触发重拉 GET（报告 §2.1；替代旧版 setTimeout 600ms）
  } catch (e) {
    // 400 拒绝：保持编辑态，用户可改正后重试（saving 由 finally 恢复）
    const errs = e.body && e.body.detail && e.body.detail.errors ? e.body.detail.errors.join("\n") : e.message;
    setMsg("✗ 保存被拒绝（400）：\n" + errs, "err");
    toast.toast("策略保存失败", false);
  } finally {
    saving.value = false;
  }
}

// 取消（旧版 btn-strategy-cancel handler：先恢复工具栏，再重新拉取只读视图）
function onCancel() {
  setEditing(false); // D-W02：唯一入口
  invalidate("strategy"); // 重拉 GET（报告 §2.1 事件触发）
}

function setMsg(text, kind) { msg.value = { text, kind }; }

onMounted(() => {
  /* useLiveQuery 已订阅；首帧由 watch(data) 驱动 */
});
</script>

<template>
  <section class="tab-panel active">
    <Toolbar :editing="editing" :meta="metaText" :saving="saving"
             @edit="onEdit" @save="onSave" @cancel="onCancel" />
    <p v-if="error && !strategyJson" class="msg-err">加载策略失败: {{ error.message }}</p>
    <p v-else-if="loading && !strategyJson" class="placeholder">加载中…</p>
    <StrategyCards v-else :json="strategyJson" :draft="draft" :editing="editing"
                   :exclude="EXCLUDED_SECTIONS" @update="onDraftUpdate" />
    <div id="strategy-msg" class="msg-box">
      <div v-if="msg.text" :class="msg.kind === 'err' ? 'msg-err' : 'msg-ok'">{{ msg.text }}</div>
    </div>
  </section>
</template>
