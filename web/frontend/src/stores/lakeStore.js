// lakeStore —— D1 接口占位（D3 填充：数据湖 tab + /api/lake/status 自适应轮询器）。
//
// 报告 §2.3 目标形状（D3 实现）：
//   status(快照), backfillRunning, syncState(idle/starting/stopping{since,pid})
//   // 自适应轮询器归它：interval=激活?3s:(running?10s:off)，stopping 期 1s；
//   // running→idle 跃迁 → toast"数据灌入完成"+invalidate('lake:*')（Joel 核心诉求）
//
// D1 不实现自适应轮询器；此处仅占位。
import { defineStore } from "pinia";

export const useLakeStore = defineStore("lake", {
  state: () => ({
    status: null, // /api/lake/status 快照（D3）
    backfillRunning: false, // D3
    syncState: "idle", // idle/starting/stopping{since,pid}（D3）
  }),
});
