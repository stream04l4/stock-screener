// lakeStore —— 数据湖全局状态 + /api/lake/status 自适应轮询器（报告 §2.3 / brief D3）。
//
// 形状（报告 §2.3）：status(快照), backfillRunning, syncState(idle/starting/stopping{since,pid})。
// **自适应轮询器归本 store**（跨页签核心）：
//   - 湖页激活 → 3s；stopping 期 → 1s（v6.0.10 快轮询，更快感知 backfill_in_progress=false）；
//   - 非激活但 backfill running → 10s（跨页签感知：用户在结果页也能看到灌数收尾）；
//   - idle 且非激活 → 停（不空转）。切到湖页先立即拉一次（activate()）。
// **running→idle 跃迁** → toast"数据灌入完成" + invalidate('lake:*')（market/industries/kline/
// stock 缓存全部失效，订阅中的立即重拉）→ 切到湖页必是新鲜快照（Joel 核心诉求）。
//
// **v6.0.10 同步控制状态机原样移植**（app.js L1776-1982 lakeRenderSyncControl/lakeSyncStart/
// lakeSyncStop）：
//   - 锁定态（starting/stopping）**不被轮询覆盖**——fetchStatus 失败（d=null 等价）时保持
//     disabled + "停止中…"，靠 5min 硬超时兜底（网络抖动 ≠ 停止失败，防用户连点 stop）；
//   - stopping 1s 快轮询 / >90s 文案升级"当前任务收尾中，最长约几分钟" / 5min 硬超时 toast+释放;
//   - lastPid 记忆：running 时记住 lock_holder_pid（stopping=true 时 meta 不显示 PID，
//     点击侧停止按钮文案需要它——避免"⏹ 停止中… (未知)"）；
//   - runningSince：会话内观察到 false→true 跃迁才记"已耗时"起点（中途进页面不知真实
//     启动时刻 → 改显示进度更新时间，不猜、不误导）。
import { defineStore } from "pinia";
import { api } from "../api/client.js";
import { invalidate } from "../live/queryRegistry.js";
import { useToastStore } from "./toastStore.js";

export const STOP_SOFT_MS = 90 * 1000;     // >90s 未停 → 文案升级（v6.0.10）
export const STOP_HARD_MS = 5 * 60 * 1000; // 5min 硬超时 → toast + 释放（不无限锁死）

// 已耗时格式化（移植 app.js lakeFmtElapsed）：<1分钟 / N分钟 / N小时M分
export function fmtElapsed(ms) {
  const m = Math.floor(ms / 60000);
  if (m < 1) return "<1分钟";
  if (m < 60) return m + "分钟";
  return Math.floor(m / 60) + "小时" + (m % 60) + "分";
}

export const useLakeStore = defineStore("lake", {
  state: () => ({
    status: null,        // /api/lake/status 快照（null=错误态/从未成功——红横幅+降级占位）
    lastError: "",       // 最近一次失败消息（ErrorBanner 数据源；成功拉取即清空）
    backfillView: null,  // v6.0.4 琥珀块视图：仅成功拉取时更新（d.backfill_in_progress ? d : null）。
                         //   错误态不触碰——与 vanilla loadLakeStatus catch 行为一致（横幅出现但
                         //   灌数中块不残留也不闪没，下轮成功再收敛）。
    activated: false,    // 湖页激活（keep-alive onActivated/onDeactivated）
    syncState: "idle",   // idle / starting / stopping（v6.0.10 锁定状态机）
    stoppingSince: null, // stopping 起点时间戳（90s/5min 判定）
    stoppingPid: null,   // stopping 目标 PID（点击侧解析，lastPid 记忆兜底）
    lastPid: null,       // 最近一次 /status 的 lock_holder_pid（v6.0.10）
    runningSince: null,  // 本会话首次观察到 running 的时间戳（已耗时口径）
    _prevBf: null,       // 上一次成功拉取的 backfill_in_progress（跃迁判定；null=从未成功）
    _timer: null,        // 轮询定时器句柄
    _timerIv: 0,         // 当前定时器间隔（状态切换时按需重建；同间隔不重建防漂移）
    _tick: 0,            // 每次 fetchStatus（成功/失败都计）自增——SyncControl 读取它以在
                         //   每轮 tick 重算 elapsed（90s 文案升级）：vanilla 每轮 loadLakeStatus
                         //   完成（含失败）都会重渲染按钮，Vue 侧靠此计数器对齐同一节奏。
  }),
  getters: {
    // ⚠️ installed/backfill_in_progress 都在 status 快照内（非顶层 state）——从 s.status 取。
    backfillRunning: (s) => !!(s.status && s.status.installed && s.status.backfill_in_progress === true),
  },
  actions: {
    // ------------------------------------------------------------------
    // 生命周期（LakeTab onActivated/onDeactivated；keep-alive 切页即停/恢复）
    // ------------------------------------------------------------------
    activate() {
      if (this.activated) return;
      this.activated = true;
      this.fetchStatus();     // 报告 §2.1：切到湖页先立即拉一次（不等下一轮 interval）
      this._recomputeTimer();
    },
    deactivate() {
      this.activated = false;
      this._recomputeTimer(); // 非激活但 running → 10s 跨页签感知；idle → 停
    },

    // ------------------------------------------------------------------
    // /status 拉取（轮询 tick 与手动 ⟳ 共用）
    // ------------------------------------------------------------------
    async fetchStatus() {
      this._tick++;   // 每轮 tick 自增（成功/失败）——SyncControl elapsed 重算锚点
      try {
        const d = await api("/api/lake/status");
        this.status = d;
        // vanilla loadLakeStatus：!installed → lakeSetError("duckdb 未安装（uv sync --extra lake）")
        // （200 响应但 duckdb 缺失 → 红横幅 + 各区块"数据湖未安装/不可用"占位）
        this.lastError = d.installed ? "" : "duckdb 未安装（uv sync --extra lake）";
        this.backfillView = d.backfill_in_progress ? d : null;
        // v6.0.10：running 会话观察 + lastPid 记忆
        const nowBf = !!(d.installed && d.backfill_in_progress === true);
        if (nowBf && !this.runningSince) this.runningSince = Date.now();
        else if (!nowBf) { this.runningSince = null; this.lastPid = null; }
        if (nowBf && d.lock_holder_pid != null) this.lastPid = d.lock_holder_pid;
        // **running→idle 跃迁（Joel 核心诉求）**：toast + 全湖缓存失效——用户在结果页
        // 也能收到"数据灌入完成"，切到湖页必是新鲜快照（market/industries/kline 重拉）。
        if (this._prevBf === true && nowBf === false) {
          useToastStore().toast("数据灌入完成");
          invalidate("lake:");
        }
        this._prevBf = nowBf;
        this._checkStopCompletion(d);   // v6.0.10：停止成功确认 / 硬超时（仅成功拉取时判定）
        // **DEFECT-D3-2**：本轮读到的新 backfillRunning 立即生效——非激活且 running→idle
        // 跃迁被观察到后把 10s interval 降为 0（brief 契约「idle 且非激活 → 停，不空转」）。
        // _recomputeTimer 有同间隔幂等保护（_timerIv===iv 直接 return），重复调用安全；
        // 既有节奏不变：stopping 1s / 激活 3s / 非激活 running 10s。跃迁判定在 _prevBf，
        // 与定时器无关（B9.2 toast+invalidate 各恰一次不受影响）。
        this._recomputeTimer();
      } catch (e) {
        // 错误态（5xx/网络）：status=null → 红横幅 + 各区块降级占位。
        // ⚠️ v6.0.10：**锁定期间 d=null 不得覆盖锁定态**——syncState/stoppingSince 不动，
        // SyncControl 按锁定分支渲染（保持 disabled+"停止中…"），靠 5min 硬超时兜底释放。
        this.status = null;
        this.lastError = e.message;
      }
    },

    // v6.0.10：stopping 收尾判定（移植 lakeRenderSyncControl stopping 分支的确认逻辑，
    // 从"渲染时判定"改为"成功拉取后判定"——语义等价：完成/超时都要求 /status 可读）。
    _checkStopCompletion(d) {
      if (this.syncState !== "stopping" || !d || !d.installed) return;
      const elapsed = Date.now() - this.stoppingSince;
      if (d.backfill_in_progress !== true) {
        // 停止成功确认（进程已退出、锁释放）→ toast + 立即释放回 [▶ 启动同步]
        this._releaseStop("已停止，进度已保存", true);
      } else if (elapsed > STOP_HARD_MS) {
        // 5min 硬超时：释放按钮 + toast（进程可能仍在收尾，刷新可查真实状态）
        this._releaseStop("停止超时，进程可能仍在收尾，请刷新查看", false);
      }
    },

    _releaseStop(msg, ok) {
      this.syncState = "idle";
      this.stoppingSince = null;
      this.stoppingPid = null;
      useToastStore().toast(msg, ok);
      this._recomputeTimer();   // 1s 快轮询 → 按新状态恢复（激活 3s / 非激活 running 10s）
    },

    // ------------------------------------------------------------------
    // 同步控制动作（v6.0.9/v6.0.10；移植 lakeSyncStart/lakeSyncStop）
    // ------------------------------------------------------------------
    async startSync() {
      if (!window.confirm("将启动全史数据补库（后台长跑，每日配额 5000 到顶自停）。确认启动？")) return;
      // v6.0.10：点击即锁定——POST /start 返回前按钮禁用+"▶ 启动中…"（防连点/竞态）
      this.syncState = "starting";
      try {
        const d = await api("/api/lake/sync/start", { method: "POST" });
        if (d.started) useToastStore().toast(`同步已启动（PID ${d.pid ?? "?"}）`);
        else useToastStore().toast("同步启动失败：" + (d.reason || "未知原因"), false);
      } catch (e) {
        // 409（已有灌数在跑，状态可能刚变化）→ toast 提示不白屏
        if (e.status === 409 && e.body && e.body.hint) useToastStore().toast(e.body.hint, false);
        else useToastStore().toast("同步启动失败：" + e.message, false);
      } finally {
        // POST 返回即释放（200→成功态 / 409→toast+释放）——立即按真实 /status 恢复二态
        this.syncState = "idle";
        this.fetchStatus();
      }
    },

    async stopSync() {
      if (!window.confirm("停止后进度已保存，下次启动自动续传。确认停止？")) return;
      // v6.0.10：点击瞬间锁定。pid 优先取当前 /status 的 lock_holder_pid；stopping=true 时
      // meta 不显示 PID → 回退轮询记住的 lastPid（避免按钮显示"(未知)"）。
      const pid = this.status && this.status.lock_holder_pid != null
        ? this.status.lock_holder_pid : this.lastPid;
      this.syncState = "stopping";
      this.stoppingSince = Date.now();
      this.stoppingPid = pid != null ? Number(pid) : null;
      this._recomputeTimer();   // v6.0.10：收尾期间提到 1s 快轮询
      try {
        const d = await api("/api/lake/sync/stop", { method: "POST" });
        if (d.waiting_task) {
          // 后端异步语义：信号已发、正在等当前任务收尾 → 保持锁定，轮询 /status 判完成
          useToastStore().toast("停止信号已发送，等待当前任务收尾…");
        } else if (d.reason) {
          // 信号未发出（holder pid 未知/发送失败）→ 释放 + toast
          this._releaseStop("同步停止失败：" + d.reason, false);
        }
      } catch (e) {
        if (e.status === 409 && e.body && e.body.hint) {
          // 409 sync_not_running（状态刚变化）→ 释放 + toast
          this._releaseStop(e.body.hint, false);
        }
        // 网络错误：保持锁定继续轮询（后端可能已收到信号；硬超时兜底释放）
      } finally {
        this.fetchStatus();   // 立即按真实状态渲染（锁定态下不覆盖按钮，见 SyncControl）
      }
    },

    // ⟳ 手动刷新（vanilla #btn-lake-refresh-status：status + 行业下拉/全湖缓存一并失效）
    refreshAll() {
      this.fetchStatus();
      invalidate("lake:");
    },

    // ------------------------------------------------------------------
    // 自适应轮询定时器（interval=stopping?1s : 激活?3s : (running?10s:off)）
    // ------------------------------------------------------------------
    _recomputeTimer() {
      let iv = 0;
      if (this.syncState === "stopping") iv = 1000;        // v6.0.10：停止收尾期 1s
      else if (this.activated) iv = 3000;                  // 湖页激活 3s（v6.0.9 既有）
      else if (this.backfillRunning) iv = 10000;           // 非激活但 running：跨页签感知
      if (this._timerIv === iv) return;                    // 同间隔不重建（避免重复触发漂移）
      if (this._timer) { clearInterval(this._timer); this._timer = null; }
      this._timerIv = iv;
      if (iv > 0) this._timer = setInterval(() => { this.fetchStatus(); }, iv);
    },

    // 测试/页面卸载兜底：停定时器（组件不直接碰——生命周期归 store）
    _stopTimerForTest() {
      if (this._timer) { clearInterval(this._timer); this._timer = null; }
      this._timerIv = 0;
    },
  },
});
