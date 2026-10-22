// taskStore —— 回测任务全局状态 + SSE 生命周期归属（v7.0：/api/bt/runs 族）。
//
// 形状：activeTaskId, state(running/done/failed), logTail[](≤400), progress{done,total,stage}, sseAlive。
// 另：lastResults{taskId, data} —— 最近一次完成回测的产物缓存（回测页结果区 + 查询页
//   行点击弹层"历史"共用；不持久化，会话级）。
//
// **SSE 生命周期归本 store，不归页面组件**（跨页签核心）：
// - 用户在查询/策略页时回测完成 → toast；切走再切回回测 tab，任务状态/日志仍在。
//
// SSE 契约 GET /api/bt/runs/{id}/events（EventSource + Last-Event-ID 续传 + 15s 心跳，
// 事件 log/progress/done/error）：
// - heartbeat → 忽略；log → appendLogLine（≤400 截断）；progress → 更新进度条三元组；
//   done → finish("done")；error → 追加 "[error 事件]" 行 + finish("failed")。
// - 连续 3 次 onerror → useSSE 关闭连接并回调 onDowngrade → **降级 3s 轮询
//   /api/bt/runs/{id}/status**；404（服务重启且任务未知）→ 停止轮询。
import { defineStore } from "pinia";
import { api } from "../api/client.js";
import { useSSE as realUseSSE } from "../live/useSSE.js";
import { useToastStore } from "./toastStore.js";

// 测试注入点：单测替换为 fake（控制 open/message/error）；生产 = useSSE。
let _useSSEImpl = realUseSSE;
export function __setUseSSEForTest(impl) { _useSSEImpl = impl || realUseSSE; }

const LOG_TAIL_MAX = 400;   // 与旧版 appendLogLine 的 400 行截断一致
const POLL_MS = 3000;       // 兜底轮询间隔

export const useTaskStore = defineStore("task", {
  state: () => ({
    activeTaskId: null,
    state: null, // running / done / failed（null=无任务）
    logTail: [], // ≤400 行
    progress: { done: 0, total: 0, stage: "" },
    sseAlive: false,
    taskMeta: "", // "任务 xxx · PID n"
    runFinished: false, // 本会话内是否已到终态（按钮在终态恢复可用）
    lastResults: null, // {taskId, data}：最近完成回测的产物（查询页"历史"共用）
  }),
  getters: {
    hasTask: (s) => s.activeTaskId != null,
    pct(s) {
      const { done, total } = s.progress;
      return total > 0 ? Math.min(100, (done / total) * 100) : 0;
    },
    progressLabel(s) {
      const p = this.pct;
      return `进度 [${s.progress.stage || "—"}] ${s.progress.done}/${s.progress.total || "—"}（${p.toFixed(1)}%）`;
    },
  },
  actions: {
    // ------------------------------------------------------------------
    // 触发回测（POST /api/bt/runs；409 → toast 已有任务）。
    // 成功返回 res；失败 rethrow（BacktestTab 恢复按钮态）。
    // ------------------------------------------------------------------
    async startBtRun(params) {
      try {
        const res = await api("/api/bt/runs", { method: "POST", body: JSON.stringify(params || {}) });
        this.stopMonitors();
        this.activeTaskId = res.task_id;
        this.state = "running";
        this.logTail = [];
        this.progress = { done: 0, total: 0, stage: "" };
        this._finished = false;
        this.runFinished = false;
        this.lastResults = null; // 新任务 → 旧结果作废（防串味）
        const codes = res.meta && res.meta.codes ? res.meta.codes.join(", ") : "股票池";
        this.taskMeta = `任务 ${res.task_id} · PID ${res.pid} · 标的 ${codes}`;
        this.startSSE(res.task_id);
        return res;
      } catch (e) {
        if (e.status === 409) useToastStore().toast("已有回测任务在进行中，请稍候（可下方查看状态）", false);
        else useToastStore().toast("触发失败: " + e.message, false);
        throw e;
      }
    },

    // 回测完成后拉产物并缓存（BacktestTab done 后调用；查询页行点击"历史"复用）。
    async fetchResults(taskId) {
      const data = await api(`/api/bt/results/${taskId}`);
      this.lastResults = { taskId, data };
      return data;
    },

    // ------------------------------------------------------------------
    // SSE 监控（全局生命周期；组件销毁不影响——跨页签核心）
    // ------------------------------------------------------------------
    startSSE(taskId) {
      this.stopMonitors();
      this._sse = _useSSEImpl(
        `/api/bt/runs/${taskId}/events`,
        (ev) => this.handleSSEEvent(ev),
        { onDowngrade: () => this.downgradeToPolling(taskId) }
      );
    },

    // SSE 事件分发
    handleSSEEvent(ev) {
      let data;
      try { data = JSON.parse(ev.data); } catch { return; } // 非法 JSON → 忽略
      if (data.type === "heartbeat") return;
      if (data.type === "log" && data.text != null) this.appendLogLine(data.text);
      else if (data.type === "progress") this.updateProgress(data.done, data.total, data.stage);
      else if (data.type === "done") this.finish("done");
      else if (data.type === "error") { this.appendLogLine("[error 事件]"); this.finish("failed"); }
    },

    // 连续 3 次 onerror → 降级 3s 轮询兜底。
    // 护栏：任务已终态时静默收尾（不切轮询、不弹 toast）。
    downgradeToPolling(taskId) {
      if (this.state === "done" || this.state === "failed") {
        this.stopMonitors();
        return;
      }
      useToastStore().toast("SSE 不可用，已回退到 3s 轮询", false);
      this.startPolling(taskId);
    },

    // ------------------------------------------------------------------
    // 兜底轮询（/api/bt/runs/{id}/status；404 → 停止）
    // ------------------------------------------------------------------
    startPolling(taskId) {
      if (this._pollTimer) clearInterval(this._pollTimer);
      this.sseAlive = false;
      const tick = async () => {
        try {
          const s = await api(`/api/bt/runs/${taskId}/status`);
          this.state = s.status;
          this.taskMeta = `任务 ${taskId} · ${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`;
          this.logTail = (s.log_tail || []).slice(-LOG_TAIL_MAX);
          if (s.status === "done") this.finish("done");
          else if (s.status === "failed") this.finish("failed");
        } catch (e) {
          if (e.status === 404) this.stopMonitors(); // 服务重启且任务未知 → 停止
        }
      };
      tick();
      this._pollTimer = setInterval(tick, POLL_MS);
    },

    // ------------------------------------------------------------------
    // 终态（幂等、停 SSE+轮询、toast）
    // ------------------------------------------------------------------
    finish(state) {
      if (this._finished && this.state === state) return;
      this._finished = true;
      this.stopMonitors();
      this.state = state;
      this.runFinished = true;
      const toast = useToastStore();
      if (state === "done") toast.toast("回测完成！结果区已刷新");
      else toast.toast("回测失败，请查看日志", false);
    },

    stopMonitors() {
      if (this._sse) { try { this._sse.close(); } catch { /* ignore */ } this._sse = null; }
      if (this._pollTimer) { clearInterval(this._pollTimer); this._pollTimer = null; }
      this.sseAlive = false;
    },

    // ------------------------------------------------------------------
    // 日志/进度
    // ------------------------------------------------------------------
    appendLogLine(line) {
      this.logTail.push(line);
      if (this.logTail.length > LOG_TAIL_MAX) this.logTail.shift(); // ≤400 截断
    },

    updateProgress(done, total, stage) {
      this.progress = { done: done || 0, total: total || 0, stage: stage || "" };
    },

    // ------------------------------------------------------------------
    // 会话恢复：页面刷新后若仍有活动任务（锁文件在）→ 重建监控。
    // running → SSE 续传（Last-Event-ID 从头补发历史日志）；已终态 → 同步状态。
    // ------------------------------------------------------------------
    async restore() {
      if (this.activeTaskId) return; // 已有任务在监控
      let s;
      try {
        const h = await api("/api/health");
        const tid = (h && h.active_task) || "";
        if (!tid) return; // 无活动任务 → 静默
        s = await api(`/api/bt/runs/${tid}/status`);
      } catch { return; } // 404/其他 → 静默
      this.activeTaskId = s.task_id;
      this.logTail = (s.log_tail || []).slice(-LOG_TAIL_MAX);
      if (s.status === "running") {
        this.state = "running";
        this._finished = false;
        this.taskMeta = `任务 ${s.task_id}（页面刷新后续接监控）`;
        this.startSSE(s.task_id);
      } else {
        // 恢复时已终态：静默同步（不弹 toast——任务早已结束，避免误导）
        this.state = s.status;
        this.taskMeta = `任务 ${s.task_id}`;
        this.runFinished = true;
        this._finished = true;
      }
    },
  },
});
