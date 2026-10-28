// 应用壳（v7.0.2 四页签）：我的持仓 / 策略 / 回测 / 数据湖。
// - 不引入 vue-router：页签 = 应用内状态（active ref）。
// - 非当前 tab 内容懒挂载：v-if="active === x" → 首次激活才渲染并发请求。
//   已挂载过的 tab 用 keep-alive 保活（切回状态仍在）。
// v7.0.2：页签重排——回测移到第三位（主入口=我的持仓）；回测内新增策略选择（策略库快照）。
// v7.0.12：候选池不设独立页签——池即策略页签「股票池」卡片（含选股门槛 + 手动池录入条）。
<script setup>
import { ref } from "vue";
import ToastBar from "./ToastBar.vue";
import BacktestTab from "../tabs/BacktestTab.vue";
import MyPositionsTab from "../tabs/MyPositionsTab.vue";
import StrategyTab from "../tabs/StrategyTab.vue";
import LakeTab from "../tabs/LakeTab.vue";
import { useAuthStore } from "../stores/authStore.js";

const TABS = [
  ["my_positions", "我的持仓"],
  ["strategy", "策略"],
  ["backtest", "回测"],
  ["lake", "数据湖"],
];

// 默认激活第一个页签（v7.0.2：我的持仓 = 日常主入口）
const active = ref("my_positions");

// v7.0.14：顶栏显示当前用户 + 退出（回登录页，cookie 清除）
const auth = useAuthStore();
async function doLogout() {
  await auth.logout();
}

// 共享登录跳转：Quant iTransformer 指挥塔（9091，同 hostname，cookie 跨端口生效）
const quantUrl = "http://" + window.location.hostname + ":9091/";
</script>

<template>
  <header class="topbar">
    <div class="brand">📉 深回撤金字塔加仓 <span class="sub">A股深回撤加仓单策略 · 高点回撤首仓 · 阶梯加仓不封顶（v7.0）</span></div>
    <nav class="tabs">
      <button v-for="[key, label] in TABS" :key="key" class="tab" :class="{ active: active === key }" @click="active = key">{{ label }}</button>
    </nav>
    <ToastBar />
    <div class="topbar-auth">
      <a class="topbar-quant" title="进入 Quant iTransformer 指挥塔（9091，共享登录）" :href="quantUrl">⚡Quant</a>
      <span class="topbar-user">👤 {{ auth.user }}</span>
      <button type="button" class="topbar-logout" title="退出登录（清除本机会话）" @click="doLogout">退出</button>
    </div>
  </header>

  <main>
    <keep-alive>
      <BacktestTab v-if="active === 'backtest'" />
      <MyPositionsTab v-else-if="active === 'my_positions'" />
      <StrategyTab v-else-if="active === 'strategy'" />
      <LakeTab v-else-if="active === 'lake'" />
    </keep-alive>
  </main>

  <footer class="footnote">
    数据源：本机 DuckDB 数据湖（data/lake/，lake-sync 定时灌数，T+1）；页面数据读自湖 + output/backtest_martingale/ 回测产物 + config/strategy.yaml。
    v7.0：单一策略（深回撤金字塔加仓）——逐股独立状态机，高点回撤首仓、每再跌一档加仓一档（金额×growth 递增，上不封顶）、不卖出、无熔断；
    信号用后复权收盘、成交按原始收盘、现金分红单列（cash 口径）。湖内无退市股（幸存者偏差），回测产物已带声明。
  </footer>
</template>

<style scoped>
.topbar-auth { display: flex; align-items: center; gap: 8px; white-space: nowrap; }
.topbar-quant {
  font-size: 12px; padding: 2px 10px; border: 1px solid var(--line, #d8d8e0);
  border-radius: 6px; background: #fff; color: #1a56db; text-decoration: none;
}
.topbar-quant:hover { border-color: #1a56db; background: #f2f6ff; }
.topbar-user { font-size: 12px; color: #666; }
.topbar-logout {
  font-size: 12px; padding: 2px 10px; border: 1px solid var(--line, #d8d8e0);
  border-radius: 6px; background: #fff; color: #555; cursor: pointer;
}
.topbar-logout:hover { border-color: #b42318; color: #b42318; }
</style>
