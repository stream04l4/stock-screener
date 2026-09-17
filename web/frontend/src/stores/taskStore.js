// taskStore —— 运行任务全局状态 + SSE 生命周期归属（报告 §2.3 / brief D2）。
//
// 形状：activeTaskId, state(running/done/failed), logTail[](≤400), progress{done,total,stage}, sseAlive。
//
// **SSE 生命周期归本 store，不归运行页组件**（跨页签核心改动）：
// - 用户在结果页时筛选完成 → toast + invalidate('runs')（RunList 订阅中 → 立即重拉，列表刷新）；
// - 切走再切回运行 tab，任务状态/日志仍在（keep-alive 组件销毁不影响 store）。
//
// SSE 契约复用现有端点 GET /api/runs/{id}/events（EventSource + Last-Event-ID 续传 + 15s 心跳，
// 事件 log/progress/done/error；语义原样移植 app.js L1068-1099 startRunMonitor）：
// - heartbeat → 忽略；log → appendLogLine（≤400 截断）；progress → 更新进度条三元组；
//   done → finish("done")；error → 追加 "[error 事件]" 行 + finish("failed")。
// - 连续 3 次 onerror → useSSE 关闭连接并回调 onDowngrade → **降级 3s 轮询 /api/runs/{id}/status**
//   （保留 v1 兜底语义原样）；404（服务重启且任务未知）→ 停止轮询。
import { defineStore } from "pinia";
import { api } from "../api/client.js";
import { invalidate } from "../live/queryRegistry.js";
import { useSSE as realUseSSE } from "../live/useSSE.js";
import { useToastStore } from "./toastStore.js";

// 测试注入点：单测替换为 fake（控制 open/message/error）；生产 = D1 已验收的 useSSE。
let _useSSEImpl = realUseSSE;
export function __setUseSSEForTest(impl) { _useSSEImpl = impl || realUseSSE; }

const LOG_TAIL_MAX = 400;   // 与旧版 appendLogLine 的 400 行截断一致
const POLL_MS = 3000;       // v1 兜底轮询间隔（保留原样）

export const useTaskStore = defineStore("task", {
  state: () => ({
    activeTaskId: null,
    state: null, // running / done / failed（null=无任务）
    logTail: [], // ≤400 行
    progress: { done: 0, total: 0, stage: "" },
    sseAlive: false,
    taskMeta: "", // "任务 xxx · PID n · 日期 d"（旧版 #task-meta 文案）
    runFinished: false, // 本会话内是否已到终态（旧版 finishRun：按钮恢复在终态而非 running 期间）
  }),
  getters: {
    hasTask: (s) => s.activeTaskId != null,
    pct(s) {
      const { done, total } = s.progress;
      return total > 0 ? Math.min(100, (done / total) * 100) : 0;
    },
    progressLabel(s) {
      const p = this.pct;
      return `进度 [${s.progress.stage}] ${s.progress.done}/${s.progress.total}（${p.toFixed(1)}%）`;
    },
  },
  actions: {
    // ------------------------------------------------------------------
    // 触发新筛选（移植 app.js startRun：POST /api/runs；409 → toast 已有任务）
    // 成功/失败都向调用方抛出原样语义：成功返回 res；失败 rethrow（RunForm 恢复按钮态）。
    // ------------------------------------------------------------------
    async startRun(date) {
      const dateVal = date || new Date().toISOString().slice(0, 10);
      try {
        const res = await api("/api/runs", { method: "POST", body: JSON.stringify({ date: dateVal }) });
        // 新任务：清旧状态再开监控（SSE 优先）
        this.stopMonitors();
        this.activeTaskId = res.task_id;
        this.state = "running";
        this.logTail = [];
        this.progress = { done: 0, total: 0, stage: "" };
        this._finished = false;
        this.runFinished = false; // 新任务 → 开始按钮禁用直到终态（旧版 btn.disabled=true）
        this.taskMeta = `任务 ${res.task_id} · PID ${res.pid} · 日期 ${res.date}`;
        this.startSSE(res.task_id);
        return res;
      } catch (e) {
        if (e.status === 409) useToastStore().toast("已有运行任务在进行中，请稍候（可下方查看状态）", false);
        else useToastStore().toast("触发失败: " + e.message, false);
        throw e; // RunForm 需要感知失败以恢复按钮态（旧版 catch 后 btn.disabled=false）
      }
    },

    // ------------------------------------------------------------------
    // SSE 监控（全局生命周期；组件销毁不影响——跨页签核心）
    // ------------------------------------------------------------------
    startSSE(taskId) {
      this.stopMonitors();
      this._sse = _useSSEImpl(
        `/api/runs/${taskId}/events`,
        (ev) => this.handleSSEEvent(ev),
        { onDowngrade: () => this.downgradeToPolling(taskId) }
      );
    },

    // SSE 事件分发（语义原样移植 app.js startRunMonitor.onmessage）
    handleSSEEvent(ev) {
      let data;
      try { data = JSON.parse(ev.data); } catch { return; } // 非法 JSON → 忽略（旧版一致）
      if (data.type === "heartbeat") return;
      if (data.type === "log" && data.text != null) this.appendLogLine(data.text);
      else if (data.type === "progress") this.updateProgress(data.done, data.total, data.stage);
      else if (data.type === "done") this.finish("done");
      else if (data.type === "error") { this.appendLogLine("[error 事件]"); this.finish("failed"); }
    },

    // 连续 3 次 onerror（useSSE 已关闭连接）→ 降级 3s 轮询兜底（保留 v1 行为）。
    // 护栏：任务已终态时（done/error 后服务端关流，EventSource 自动重连必然再失败）
    // 静默收尾即可——不切轮询、不弹"SSE 不可用"toast（旧版同场景 finishRun 后不再提示）。
    downgradeToPolling(taskId) {
      if (this.state === "done" || this.state === "failed") {
        this.stopMonitors();
        return;
      }
      useToastStore().toast("SSE 不可用，已回退到 3s 轮询", false);
      this.startPolling(taskId);
    },

    // ------------------------------------------------------------------
    // 兜底轮询（保留 v1 行为：/api/runs/{id}/status；404 → 停止）
    // ------------------------------------------------------------------
    startPolling(taskId) {
      if (this._pollTimer) clearInterval(this._pollTimer);
      this.sseAlive = false;
      const tick = async () => {
        try {
          const s = await api(`/api/runs/${taskId}/status`);
          // 状态徽章/meta（移植 pollStatus.showProgress 语义）
          this.state = s.status;
          this.taskMeta = `任务 ${taskId}${s.date ? " · 日期 " + s.date : ""} · ${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`;
          // 日志整段替换（旧版 pollStatus：log_tail 全量覆盖，不做去重）
          this.logTail = (s.log_tail || []).slice(-LOG_TAIL_MAX);
          if (s.status === "done") this.finish("done");
          else if (s.status === "failed") this.finish("failed");
        } catch (e) {
          // 404（服务重启且任务未知）→ 停止轮询（旧版语义原样）
          if (e.status === 404) this.stopMonitors();
        }
      };
      tick();
      this._pollTimer = setInterval(tick, POLL_MS);
    },

    // ------------------------------------------------------------------
    // 终态（移植 app.js finishRun：幂等、停 SSE+轮询、toast；
    // done → toast + invalidate('runs')——跨页签核心验收点）
    // ------------------------------------------------------------------
    finish(state) {
      if (this._finished && this.state === state) return;
      this._finished = true;
      this.stopMonitors();
      this.state = state;
      this.runFinished = true; // 旧版 finishRun：终态恢复 #btn-run-start.disabled=false
      const toast = useToastStore();
      if (state === "done") {
        toast.toast("筛选完成！结果页已刷新");
        // 跨页签核心：用户此刻可能在结果页 → runs 列表立即重拉（RunList 订阅中则即时生效）
        invalidate("runs");
      } else {
        toast.toast("运行失败，请查看日志", false);
      }
    },

    stopMonitors() {
      if (this._sse) { try { this._sse.close(); } catch { /* ignore */ } this._sse = null; }
      if (this._pollTimer) { clearInterval(this._pollTimer); this._pollTimer = null; }
      this.sseAlive = false;
    },

    // ------------------------------------------------------------------
    // 日志/进度（移植 appendLogLine / updateProgressBar 语义）
    // ------------------------------------------------------------------
    appendLogLine(line) {
      this.logTail.push(line);
      if (this.logTail.length > LOG_TAIL_MAX) this.logTail.shift(); // ≤400 截断（旧版一致）
    },

    updateProgress(done, total, stage) {
      this.progress = { done: done || 0, total: total || 0, stage: stage || "" };
    },

    // ------------------------------------------------------------------
    // 会话恢复：页面刷新后若仍有活动任务（锁文件在）→ 重建监控。
    // running → SSE 续传（Last-Event-ID 从头补发历史日志，契约原样）；
    // 已终态 → 同步最终状态 + toast（与"在线完成"路径一致）。
    // ------------------------------------------------------------------
    async restore() {
      if (this.activeTaskId) return; // 已有任务在监控
      let s;
      try {
        const h = await api("/api/health");
        const tid = (h && h.active_task) || "";
        if (!tid) return; // 无活动任务 → 静默
        s = await api(`/api/runs/${tid}/status`);
      } catch { return; } // 404/其他 → 静默
      this.activeTaskId = s.task_id;
      this.logTail = (s.log_tail || []).slice(-LOG_TAIL_MAX);
      if (s.status === "running") {
        this.state = "running";
        this._finished = false;
        this.taskMeta = `任务 ${s.task_id}${s.date ? " · 日期 " + s.date : ""}（页面刷新后续接监控）`;
        this.startSSE(s.task_id);
      } else {
        // 恢复时已终态：静默同步状态 + 刷新 runs 列表（不弹 toast——任务早已结束，
        // 避免页面加载即出现误导性"筛选完成"提示；与旧版"刷新后无监控"相比是增强）
        this.state = s.status;
        this.taskMeta = `任务 ${s.task_id}`;
        this.runFinished = true;
        this._finished = true;
        invalidate("runs");
      }
    },
  },
});
