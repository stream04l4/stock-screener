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
// v6.2.1 S1 — 策略库（本机命名保存 + 下拉加载 + 删除，data/strategies/*.yaml）：
// - 【💾 另存为】→ 小弹窗（名称输入框默认 策略_YYYYMMDD_HHMM 可改）+【保存】→ POST /api/strategies；
//   成功 → toast"已保存到策略库：{name}" + 刷新下拉（loadLib()）；409 strategy_exists → toast 提示重名。
// - 【📂 加载】= 下拉选择框（选项 = GET /api/strategies 全部 + 占位"— 选择策略 —"）；
//   选中即 confirm"将用 {name} 覆盖当前策略配置，确认？" → POST /api/strategies/{name}/load
//   （服务端复用 PUT 同一套校验 → .bak 备份 + 写盘）→ 成功 toast"已加载 {name}"（含 backup 文件名）
//   + invalidate("strategy") 重拉；400 → msg-box 显示 errors（与 PUT 400 同 UI）。
// - 【🗑️ 删除】= 下拉旁按钮删当前选中项（confirm 二次确认）→ DELETE → 刷新下拉。
// - 编辑态下三者禁用（防丢未保存修改，与 Toolbar disabled 一致）。
// - **保留现有【编辑】【保存】【取消】**：编辑→保存 = 应用到当前活动策略（PUT strategy.yaml），
//   语义不变；"另存为"是额外的命名快照，两者并存。
// v6.2.1 S2 — 页面归属调整：本 tab 只渲染 SECTION_GROUPS（8 核心段 + ⚙️高级配置折叠组）；
// backtest/reinvest 移到 BacktestTab、lake 段不再渲染（yaml-only）。
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
const libLoading = ref(false);  // S1：GET /api/strategies 在途（另存为/加载/删除禁用）
const strategies = ref([]);     // S1：策略库列表 [{name, saved_at, size_bytes}]（saved_at 降序）
const selectedName = ref("");   // S1：加载下拉当前选中值（"" = 占位）
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
// S1 — 策略库：另存为（弹窗命名保存）/ 下拉加载 / 删除
//   存储 = 服务端 data/strategies/*.yaml（config/strategy.yaml 原文快照，保留注释排版）。
//   编辑态下三者禁用（防丢未保存修改，与 Toolbar disabled 一致）。
// ---------------------------------------------------------------------------

// ---- 策略库列表（下拉数据源；进 tab 拉一次 + 每次增删改后刷新）----
async function loadLib() {
  libLoading.value = true;
  try {
    strategies.value = (await api("/api/strategies")) || [];
  } catch (e) {
    // 列表拉取失败不阻断页面（下拉空态 + toast；保存/加载仍可用——后端各自报错）
    strategies.value = [];
    toast.toast("策略库列表加载失败：" + e.message, false);
  } finally {
    libLoading.value = false;
  }
}

// ---- 【💾 另存为】：小弹窗（名称输入框默认 策略_YYYYMMDD_HHMM，可改）+【保存】----
const saveAsOpen = ref(false);
const saveAsName = ref("");
const saveAsBusy = ref(false); // POST /api/strategies 在途

function defaultSaveAsName() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return `策略_${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}_${p(d.getHours())}${p(d.getMinutes())}`;
}

function openSaveAs() {
  if (editing.value || libLoading.value) return; // 与按钮 disabled 一致
  saveAsName.value = defaultSaveAsName(); // 默认名（当前时间），可改
  saveAsOpen.value = true;
}

async function confirmSaveAs() {
  if (saveAsBusy.value) return;
  saveAsBusy.value = true;
  try {
    const res = await api("/api/strategies", { method: "POST", body: JSON.stringify({ name: saveAsName.value }) });
    toast.toast(`已保存到策略库：${res.name}`);
    setMsg(`✓ 已保存到策略库：${res.name}（${res.path.split("/").pop()}）`, "ok");
    saveAsOpen.value = false;
    selectedName.value = res.name; // 下拉直接定位到新保存项（便于紧接着加载/删除）
    await loadLib(); // 刷新下拉
  } catch (e) {
    if (e.status === 409 && e.body && e.body.error === "strategy_exists") {
      toast.toast(`策略库中已存在「${saveAsName.value}」，请换一个名称`, false);
    } else {
      toast.toast("另存为失败：" + e.message, false);
    }
  } finally {
    saveAsBusy.value = false;
  }
}

// ---- 【📂 加载】：下拉选中即触发（confirm → POST /api/strategies/{name}/load）----
async function onLoadSelect(name) {
  if (editing.value || libLoading.value) return; // 与按钮 disabled 一致
  if (!name) return; // 占位项（"— 选择策略 —"）：不触发加载
  selectedName.value = name;
  const ok = window.confirm(`将用 ${name} 覆盖当前策略配置，确认？`);
  if (!ok) {
    selectedName.value = ""; // 取消 → 下拉复位占位（避免误以为已加载）
    return;
  }
  libLoading.value = true; // load 在途也锁工具栏（防连点）
  try {
    const res = await api(`/api/strategies/${encodeURIComponent(name)}/load`, { method: "POST" });
    toast.toast(`已加载 ${name}`);
    setMsg(`✓ 已加载 ${name}（备份: ${String(res.backup).split("/").pop()}）`, "ok");
    invalidate("strategy"); // 事件触发重拉 GET（用 API 返回 JSON 为底重渲染只读视图）
  } catch (e) {
    // 400 → 保持现状（不改 strategyJson、不重拉），msg-box 展示 errors（与 PUT 400 同 UI）
    const errs = e.body && e.body.detail && e.body.detail.errors ? e.body.detail.errors.join("\n") : e.message;
    setMsg("✗ 加载被拒绝（400）：\n" + errs, "err");
    toast.toast(`策略 ${name} 加载失败`, false);
  } finally {
    libLoading.value = false;
  }
}

// ---- 【🗑️ 删除】：删当前选中项（confirm 二次确认）→ DELETE → 刷新下拉 ----
async function onDeleteSelected() {
  const name = selectedName.value;
  if (!name || editing.value || libLoading.value) return; // 与按钮 disabled 一致
  const ok = window.confirm(`确认删除策略库中的「${name}」？\n（仅删本机快照文件，不影响当前活动配置）`);
  if (!ok) return;
  libLoading.value = true;
  try {
    await api(`/api/strategies/${encodeURIComponent(name)}`, { method: "DELETE" });
    toast.toast(`已删除策略 ${name}`);
    selectedName.value = ""; // 下拉复位占位
    await loadLib(); // 刷新下拉
  } catch (e) {
    toast.toast("删除失败：" + e.message, false);
  } finally {
    libLoading.value = false;
  }
}

function setMsg(text, kind) { msg.value = { text, kind }; }

onMounted(() => {
  /* useLiveQuery 已订阅；首帧由 watch(data) 驱动 */
  loadLib(); // S1：策略库下拉数据源（与 strategy 拉取并行，互不阻塞）
});
</script>

<template>
  <section class="tab-panel active">
    <Toolbar :editing="editing" :meta="metaText" :saving="saving" :lib-loading="libLoading"
             :strategies="strategies" :selected="selectedName"
             @edit="onEdit" @save="onSave" @cancel="onCancel"
             @save-as="openSaveAs" @load-select="onLoadSelect" @delete-selected="onDeleteSelected" />
    <p v-if="error && !strategyJson" class="msg-err">加载策略失败: {{ error.message }}</p>
    <p v-else-if="loading && !strategyJson" class="placeholder">加载中…</p>
    <StrategyCards v-else :json="strategyJson" :draft="draft" :editing="editing"
                   :exclude="['backtest', 'reinvest', 'lake']" @update="onDraftUpdate" />
    <!-- S1：另存为小弹窗（名称输入框默认 策略_YYYYMMDD_HHMM +【保存】；Esc/遮罩关闭） -->
    <div v-if="saveAsOpen" class="modal">
      <div class="modal-mask" @click="saveAsOpen = false"></div>
      <div class="modal-box saveas-box">
        <div class="modal-head">
          <h3>💾 另存为（保存到本机策略库）</h3>
          <button class="mini-btn" title="关闭" @click="saveAsOpen = false">✕</button>
        </div>
        <div class="modal-body">
          <label for="input-saveas-name" class="muted small">策略名称（支持中文；留空自动用默认名）</label>
          <input id="input-saveas-name" type="text" v-model="saveAsName" autofocus
                 placeholder="策略_YYYYMMDD_HHMM" @keydown.enter.prevent="confirmSaveAs" />
          <div class="saveas-actions">
            <button class="ghost-btn" :disabled="saveAsBusy" @click="saveAsOpen = false">取消</button>
            <button id="btn-saveas-confirm" class="primary-btn" :disabled="saveAsBusy" @click="confirmSaveAs">保存</button>
          </div>
        </div>
      </div>
    </div>
    <div id="strategy-msg" class="msg-box">
      <div v-if="msg.text" :class="msg.kind === 'err' ? 'msg-err' : 'msg-ok'">{{ msg.text }}</div>
    </div>
  </section>
</template>
