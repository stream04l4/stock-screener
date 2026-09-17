<script setup>
// SyncControl —— v6.0.9/v6.0.10 同步控制按钮（**状态机原样移植** app.js L1776-1982）。
//
// 按钮态优先级（vanilla lakeRenderSyncControl 逐字对齐）：
//   锁定中（starting/stopping）> running → [⏹ 停止同步 (pid)] > idle → [▶ 启动同步]。
// - **锁定态不被轮询覆盖**：syncState=stopping 时 status=null（网络抖动/5xx）也不解锁——
//   保持 disabled+"停止中…"，靠 5min 硬超时兜底（store._checkStopCompletion 仅在成功拉取
//   时判定完成/超时；vanilla 同语义：d=null 不进确认分支）。
// - stopping 1s 快轮询 / >90s 文案升级"当前任务收尾中，最长约几分钟" / 5min 硬超时
//   toast+释放——判定归 lakeStore（_tick 驱动本组件每轮重算 elapsed，对齐 vanilla
//   "每轮 loadLakeStatus 完成都重渲染按钮"的节奏）。
// - lastPid 记忆 / runningSince 已耗时口径：全在 store（跨页签保活）。
import { computed } from "vue";
import { useLakeStore, fmtElapsed, STOP_SOFT_MS } from "../../stores/lakeStore.js";

const lake = useLakeStore();

// 运行中 meta 拼接（移植 vanilla running 分支）：stopping=true → 友好文案；
// 今日配额 x/budget（tasks max 防御滞后视图）+ 已耗时/进度更新时间。
const runningMeta = computed(() => {
  const d = lake.status;
  if (!d || !d.installed) return "";
  const stopping = d.stopping === true;
  let qUsed = null, qBudget = null;
  for (const t of d.tasks || []) {
    if (!t) continue;
    if (typeof t.quota_used_today === "number") qUsed = Math.max(qUsed ?? 0, t.quota_used_today);
    if (typeof t.quota_budget === "number") qBudget = Math.max(qBudget ?? 0, t.quota_budget);
  }
  const parts = [stopping ? "⏹ 停止收尾中（当前任务完成后退出）" : "PID " + (d.lock_holder_pid != null ? d.lock_holder_pid : "未知")];
  if (qUsed != null) parts.push(`今日配额 ${qUsed}/${qBudget ?? "—"}`);
  if (lake.runningSince) parts.push("已耗时 " + fmtElapsed(Date.now() - lake.runningSince));
  else if (d.updated_at) parts.push("进度更新于 " + String(d.updated_at).slice(5, 16));
  return parts.join(" · ");
});

const stoppingMeta = computed(() => {
  const pid = lake.stoppingPid != null ? lake.stoppingPid : "未知";
  return `PID ${pid} · 已等待 ${fmtElapsed(Date.now() - (lake.stoppingSince || Date.now()))}`;
});

// 按钮渲染（优先级：锁定 > running > idle；错误态/未安装 → 禁用占位）。
// ⚠️ 读 lake._tick：每轮 fetchStatus（成功/失败）自增 → 强制本 computed 重算，
// 即使 status 引用未变（连续失败）elapsed/90s 文案也按 tick 节奏刷新——对齐 vanilla
// "每轮 loadLakeStatus 完成都重渲染按钮"。
const btn = computed(() => {
  void lake._tick;   // 依赖锚点（见上注释）
  if (lake.syncState === "starting") {
    return { label: "▶ 启动中…", disabled: true, busy: true };
  }
  if (lake.syncState === "stopping") {
    const pid = lake.stoppingPid != null ? lake.stoppingPid : "未知";
    const elapsed = Date.now() - (lake.stoppingSince || Date.now());
    // >90s 未停：文案升级（当前任务收尾中，最长约几分钟）；继续轮询直到成功或硬超时
    const label = elapsed > STOP_SOFT_MS
      ? "⏹ 停止中…（当前任务收尾中，最长约几分钟）"
      : `⏹ 停止中… (${pid})`;
    return { label, disabled: true, busy: true };
  }
  // 错误态（5xx/网络）：禁用按钮不白屏（保留占位文案）
  if (!lake.status) {
    return { label: "状态不可用", disabled: true, busy: false, muted: true };
  }
  if (!lake.status.installed) {
    return { label: "数据湖未安装", disabled: true, busy: false, muted: true };
  }
  if (lake.backfillRunning) {
    const pid = lake.status.lock_holder_pid != null ? lake.status.lock_holder_pid : "未知";
    return { label: `⏹ 停止同步 (${pid})`, disabled: false, busy: false, action: "stop" };
  }
  return { label: "▶ 启动同步", disabled: false, busy: false, action: "start" };
});

const meta = computed(() => {
  void lake._tick;   // 依赖锚点（与 btn 同节奏）
  if (lake.syncState === "starting") return "正在启动（确认进程拉起中）";
  if (lake.syncState === "stopping") return stoppingMeta.value;
  if (!lake.status) return "";
  if (!lake.status.installed) return "";
  return lake.backfillRunning ? runningMeta.value : "";
});

function onClick() {
  if (btn.value.action === "start") lake.startSync();
  else if (btn.value.action === "stop") lake.stopSync();
}
</script>

<template>
  <div class="lake-sync-control">
    <span class="lake-sync-label">数据同步</span>
    <button id="btn-lake-sync-toggle" class="primary-btn" :class="{ 'sync-busy': btn.busy }"
            :disabled="btn.disabled" @click="onClick">
      <span v-if="btn.muted" class="muted">{{ btn.label }}</span>
      <template v-else>{{ btn.label }}</template>
    </button>
    <span id="lake-sync-meta" class="muted small">{{ meta }}</span>
  </div>
</template>
