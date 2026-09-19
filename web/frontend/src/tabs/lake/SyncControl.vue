<script setup>
// SyncControl —— v6.1.6 单按钮同步控制（三按钮合并：全史补库/增量同步/T5 → 一个
// 【▶ 启动数据补齐 / ■ 停止数据补齐】toggle；状态机原样保留 app.js L1776-1982 移植版）。
//
// v6.1.6 brief §B（Joel 拍板：三个按钮没必要，一个按钮负责启动停止——只要启动了就是
// 需要把所有历史及现状数据全部都补上）：
//   idle → 【▶ 启动数据补齐】（confirm："将启动全量数据补齐（历史+增量+基本面，
//          后台长跑，幂等可中断续传）。确认启动？"→ store.startSync() 无参=full）
//   running → 【■ 停止数据补齐】（红色危险样式 sync-danger；点击走现有 /sync/stop——
//          v6.0.10 状态机 + v6.1.5 hang1 R4 ≤20s 快退，语义零改动）
//   按钮下方小字显示当前 phase（来自 /status.phase，仅 backfill_in_progress=true 时
//   后端追加）："正在灌：T2 全史 / T3 估值增量 / T5 基本面 / T6 股东"。非 full 模式进程无段标
//   → 无 phase 键 → 小字不渲染（三态契约其余字段不动）。
//
// 按钮态优先级（与 v6.0.10 状态机逐字对齐）：
//   锁定中（starting/stopping）> running > idle；错误态/未安装 → 禁用占位。
// - **锁定态不被轮询覆盖**：syncState=stopping 时 status=null（网络抖动/5xx）也不解锁——
//   保持 disabled+"停止中…"，靠 5min 硬超时兜底（store._checkStopCompletion 仅在成功
//   拉取时判定完成/超时；vanilla 同语义：d=null 不进确认分支）。
// - stopping 1s 快轮询 / >90s 文案升级"当前任务收尾中，最长约几分钟" / 5min 硬超时
//   toast+释放——判定归 lakeStore（_tick 驱动本组件每轮重算 elapsed）。
// - lastPid 记忆 / runningSince 已耗时口径：全在 store（跨页签保活）。
import { computed } from "vue";
import { useLakeStore, fmtElapsed, STOP_SOFT_MS } from "../../stores/lakeStore.js";

const lake = useLakeStore();

// v6.1.6：phase 小字文案映射（brief §A："T2 全史 / T3 估值增量 / T5 基本面"）。
// v6.1.7：+t6 股东阶段（full 第 4 阶段——holders_snapshot 接入 full）。
// v6.1.8 F1：+t8 因子/t9 利率轻量阶段（full 顺序 history→p3→t8→t9→t5→t6）。
const PHASE_LABELS = { history: "T2 全史", p3: "T3 估值增量", t8: "T8 因子重算", t9: "T9 利率更新", t5: "T5 基本面", t6: "T6 股东" };

// 运行中 meta 拼接（v6.1.4 口径保留）：stopping=true → 友好文案；已耗时/进度更新时间。
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

// v6.1.6：按钮下方小字——当前 phase（仅 running + status.phase 存在时渲染）。
const phaseLine = computed(() => {
  void lake._tick;   // 依赖锚点（与 btn 同节奏）
  const d = lake.status;
  if (!d || !d.backfill_in_progress) return "";
  const label = PHASE_LABELS[d.phase];
  return label ? "正在灌：" + label : "";
});

// 按钮渲染（优先级：锁定 > running > idle；错误态/未安装 → 禁用占位）。
// ⚠️ 读 lake._tick：每轮 fetchStatus（成功/失败）自增 → 强制本 computed 重算，
// 即使 status 引用未变（连续失败）elapsed/90s 文案也按 tick 节奏刷新。
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
    // v6.1.6：running → 【■ 停止数据补齐】（红色危险样式；走现有 /sync/stop）
    return { label: "■ 停止数据补齐", disabled: false, busy: false, action: "stop", danger: true };
  }
  // v6.1.6：idle → 单按钮【▶ 启动数据补齐】（无参 startSync = full 全量）
  return { label: "▶ 启动数据补齐", disabled: false, busy: false, action: "start" };
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
  if (btn.value.action === "start") lake.startSync();   // v6.1.6：无参 = full（全量补齐）
  else if (btn.value.action === "stop") lake.stopSync();
}
</script>

<template>
  <div class="lake-sync-control">
    <span class="lake-sync-label">数据同步</span>
    <!-- v6.1.6：三按钮合并为单 toggle——idle【▶ 启动数据补齐】/ running【■ 停止数据补齐】(danger) -->
    <button id="btn-lake-sync-toggle" class="primary-btn"
            :class="{ 'sync-busy': btn.busy, 'sync-danger': btn.danger }"
            :disabled="btn.disabled" @click="onClick">
      <span v-if="btn.muted" class="muted">{{ btn.label }}</span>
      <template v-else>{{ btn.label }}</template>
    </button>
    <span id="lake-sync-meta" class="muted small">{{ meta }}</span>
    <!-- v6.1.6：当前 phase 小字（来自 /status.phase，仅 running 时后端追加；非 full 进程无该键） -->
    <span id="lake-sync-phase" class="muted small" v-if="phaseLine">{{ phaseLine }}</span>
  </div>
</template>
