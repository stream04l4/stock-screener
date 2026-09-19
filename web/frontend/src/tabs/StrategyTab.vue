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
// 报告 §2.1：GET /api/strategy → 按需+事件触发（保存/取消后 invalidate）。
<script setup>
import { computed, onMounted, ref, watch } from "vue";
import { api } from "../api/client.js";
import { useLiveQuery } from "../live/useLiveQuery.js";
import { invalidate } from "../live/queryRegistry.js";
import { useToastStore } from "../stores/toastStore.js";
import { FIELD_TYPE } from "../data/strategyMeta.js";
import Toolbar from "./strategy/Toolbar.vue";
import StrategyCards from "./strategy/StrategyCards.vue";

const toast = useToastStore();

// 按需拉取（首次进 tab 才发请求——懒加载验收点；TTL 0 = 事件触发刷新）
const { data, loading, error } = useLiveQuery("strategy", () => api("/api/strategy"), {});

const strategyJson = ref(null); // GET 成功后的 d.json（只读态数据源 + 保存 payload 底）
const editing = ref(false);     // D-W02：strategyEditing 标志
const draft = ref(null);        // 编辑态草稿（JSON 深拷贝 + 编辑值覆盖）
const saving = ref(false);      // PUT 在途（保存按钮禁用，finally 恢复）
const importing = ref(false);   // O3：POST /api/strategy/import 在途（另存为/加载按钮禁用）
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
      let v;
      if (t === "weights_dict") {
        // 滑块值已在 FieldEditor 内解析为 number；0–1 校验（旧版语义原样）
        for (const [dim, wv] of Object.entries(value || {})) {
          const n = Number(wv);
          if (isNaN(n) || n < 0 || n > 1) { setMsg(`✗ ${section}.${field}.${dim} 必须在 0–1`, "err"); bad = true; break; }
        }
      } else if (t === "sub_weights_dict") {
        // FieldEditor 已解析（非法 → null）；null → 格式错误（旧版语义原样）
        for (const [dim, parsed] of Object.entries(value || {})) {
          if (!parsed) { setMsg(`✗ ${section}.${field}.${dim} 格式应为 key:值,key:值`, "err"); bad = true; break; }
        }
      } else if (t === "int") {
        v = Number.isInteger(Number(value)) ? parseInt(value, 10) : NaN;
        if (isNaN(v)) { setMsg(`✗ ${section}.${field} 必须是整数`, "err"); bad = true; continue; }
      } else if (t === "float") {
        v = parseFloat(value);
        if (isNaN(v)) { setMsg(`✗ ${section}.${field} 必须是数值`, "err"); bad = true; continue; }
      } else if (t === "bool") {
        v = value === "true" || value === true;
      } else if (t === "list") {
        v = String(value).split(",").map((s) => s.trim()).filter(Boolean);
        if (!v.length) { setMsg(`✗ ${section}.${field} 不能为空`, "err"); bad = true; continue; }
      } else {
        v = value; // str / enum2：原样（enum 值域由后端校验，400 回显）
      }
      if (t !== "weights_dict" && t !== "sub_weights_dict") payload[section][field] = v;
    }
    if (bad) break;
  }
  if (bad) return; // 本地校验失败 → 不提交（旧版语义原样）

  saving.value = true;
  try {
    const res = await api("/api/strategy", { method: "PUT", body: JSON.stringify(payload) });
    setMsg(`✓ 策略已保存（备份: ${String(res.backup).split("/").pop()}）`, "ok");
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

// ---------------------------------------------------------------------------
// O3 — 另存为 / 加载策略文件
//   另存为 = 完整配置快照（含基础设施段），Blob 下载；文件名可输入、默认带时间戳。
//   加载 = file input(.yaml/.yml) → POST /api/strategy/import {raw} → 服务端复用 PUT 校验。
//   编辑态下两者禁用（防丢未保存修改，与 Toolbar disabled 一致）。
// ---------------------------------------------------------------------------
const fileInput = ref(null); // O3：隐藏 file input（accept=.yaml,.yml）

function defaultExportName() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return `strategy_${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}_${p(d.getHours())}${p(d.getMinutes())}.yaml`;
}

// 另存为：GET /api/strategy 取 raw（新鲜读盘，保证导出=当前磁盘内容）→ Blob 下载
async function onExport() {
  if (editing.value || importing.value) return; // 与按钮 disabled 一致
  try {
    const d = await api("/api/strategy");
    const raw = d && typeof d.raw === "string" ? d.raw : "";
    if (!raw) { toast.toast("无可导出的策略内容", false); return; }
    const name = window.prompt("另存为文件名（默认带时间戳，可改）：", defaultExportName());
    if (name === null) return; // 用户取消 prompt
    const fname = (name.trim() || "strategy.yaml");
    const blob = new Blob([raw], { type: "text/yaml;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = fname;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast.toast("已导出完整策略配置（含数据源/健康检查等基础设施配置）");
  } catch (e) {
    toast.toast("另存为失败：" + e.message, false);
  }
}

// 加载：点【📂 加载】→ 触发隐藏 file input
function onImport() {
  if (editing.value || importing.value) return; // 与按钮 disabled 一致
  if (fileInput.value) fileInput.value.click();
}

// file input change → 读文本 → POST /api/strategy/import
async function onFileChosen(ev) {
  const f = ev.target.files && ev.target.files[0];
  ev.target.value = ""; // 复位，允许重复选同一文件
  if (!f) return;
  importing.value = true;
  try {
    const raw = await f.text();
    const res = await api("/api/strategy/import", { method: "POST", body: JSON.stringify({ raw }) });
    toast.toast(`已加载策略文件 ${f.name}`);
    setMsg(`✓ 已加载策略文件（备份: ${String(res.backup).split("/").pop()}）`, "ok");
    invalidate("strategy"); // 事件触发重拉 GET（用 API 返回 JSON 为底重渲染只读视图）
  } catch (e) {
    // 400 → 保持现状（不改 strategyJson、不重拉），msg-box 展示 errors（与 PUT 400 同 UI）
    const errs = e.body && e.body.detail && e.body.detail.errors ? e.body.detail.errors.join("\n") : e.message;
    setMsg("✗ 加载被拒绝（400）：\n" + errs, "err");
    toast.toast("策略文件加载失败", false);
  } finally {
    importing.value = false;
  }
}

function setMsg(text, kind) { msg.value = { text, kind }; }

onMounted(() => { /* useLiveQuery 已订阅；首帧由 watch(data) 驱动 */ });
</script>

<template>
  <section class="tab-panel active">
    <Toolbar :editing="editing" :meta="metaText" :saving="saving" :importing="importing"
             @edit="onEdit" @save="onSave" @cancel="onCancel" @export="onExport" @import="onImport" />
    <!-- O3：隐藏 file input（accept=.yaml,.yml）；【📂 加载】触发 click() -->
    <input ref="fileInput" type="file" accept=".yaml,.yml" style="display:none" @change="onFileChosen" />
    <p v-if="error && !strategyJson" class="msg-err">加载策略失败: {{ error.message }}</p>
    <p v-else-if="loading && !strategyJson" class="placeholder">加载中…</p>
    <StrategyCards v-else :json="strategyJson" :draft="draft" :editing="editing" @update="onDraftUpdate" />
    <div id="strategy-msg" class="msg-box">
      <div v-if="msg.text" :class="msg.kind === 'err' ? 'msg-err' : 'msg-ok'">{{ msg.text }}</div>
    </div>
  </section>
</template>
