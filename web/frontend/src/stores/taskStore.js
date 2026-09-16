// taskStore —— D1 接口占位（D2 填充：运行 tab + SSE 全局生命周期）。
//
// 报告 §2.3 目标形状（D2 实现）：
//   activeTaskId, state(running/done/failed), logTail[](≤400), progress{done,total,stage}, sseAlive
//   // SSE 生命周期归它（全局而非运行页组件）→ 用户在结果页时筛选完成：toast + invalidate('runs')
//
// D1 不实现 SSE 全局生命周期与自适应轮询器；此处仅占位，保证 AppShell/后续 tab 可按契约引用。
import { defineStore } from "pinia";

export const useTaskStore = defineStore("task", {
  state: () => ({
    activeTaskId: null,
    state: null, // running / done / failed（D2）
    logTail: [], // ≤400 行（D2）
    progress: { done: 0, total: 0, stage: "" }, // D2
    sseAlive: false, // D2
  }),
});
