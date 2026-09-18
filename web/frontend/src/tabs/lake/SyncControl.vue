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
// 已耗时/进度更新时间。v6.1.3：头部配额片段（今日 used/budget）移除——配额是
// BaoStock 的，归 SourcePoolPanel 数据源状态卡展示；此处只留 PID/stopping + 耗时口径。
const runningMeta = computed(() => {
  const d = lake.status;
  if (!d || !d.installed) return "";
  const stopping = d.stopping === true;
  const parts = [stopping ? "⏹ 停止收尾中（当前任务完成后退出）" : "PID " + (d.lock_holder_pid != null ? d.lock_holder_pid : "未知")];
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
  // v6.1.4 O2：idle → 主按钮=全史补库（原"▶ 启动同步"改名，语义不变）
  return { label: "▶ 启动全史补库", disabled: false, busy: false, action: "start" };
});

// v6.1.4 O2：增量同步按钮（P3 每日增量）。仅 idle+installed+未 running 时可点；
// running/starting/stopping/错误态 → 禁用（brief：running 时两按钮都禁）。
const incBtn = computed(() => {
  void lake._tick;   // 依赖锚点（与 btn 同节奏）
  const enabled = (lake.syncState === "idle" && !!lake.status
    && lake.status.installed && !lake.backfillRunning);
  return { label: "▶ 启动增量同步", disabled: !enabled, busy: false };
});

// v6.1.5 F4：T5 基本面一键启动按钮（第三个）。状态机复用 incBtn——仅 idle+installed+
// 未 running 时可点；running/starting/stopping/错误态 → 禁用（brief：running 时三按钮全禁）。
const t5Btn = computed(() => {
  void lake._tick;   // 依赖锚点（与 btn/incBtn 同节奏）
  const enabled = (lake.syncState === "idle" && !!lake.status
    && lake.status.installed && !lake.backfillRunning);
  return { label: "▶ 启动基本面(T5)", disabled: !enabled, busy: false };
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
  if (btn.value.action === "start") lake.startSync("history");
  else if (btn.value.action === "stop") lake.stopSync();
}
function onIncClick() {
  // v6.1.4 O2：增量同步（P3）——mode=incremental 透传后端 ?mode=
  lake.startSync("incremental");
}
function onT5Click() {
  // v6.1.5 F4：T5 基本面一键启动——mode=t5 透传后端 ?mode=t5（→ driver history --t5）
  lake.startSync("t5");
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
    <!-- v6.1.4 O2：增量同步（P3）按钮——idle 时可点；running/starting/stopping/错误态禁用 -->
    <button id="btn-lake-sync-incremental" class="primary-btn"
            :disabled="incBtn.disabled" @click="onIncClick">
      {{ incBtn.label }}
    </button>
    <!-- v6.1.5 F4：T5 基本面一键启动（第三个按钮）——状态机复用增量按钮；running 时三按钮全禁 -->
    <button id="btn-lake-sync-t5" class="primary-btn"
            :disabled="t5Btn.disabled" @click="onT5Click">
      {{ t5Btn.label }}
    </button>
    <span id="lake-sync-meta" class="muted small">{{ meta }}</span>
  </div>
</template>
