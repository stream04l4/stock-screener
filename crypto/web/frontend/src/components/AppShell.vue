// 应用壳（加密货币系统，v7.0.43 框架）：四页签 我的持仓 / 策略 / 回测 / 数据湖
// （与股票系统同构，内容后续慢慢完善）。
// - 不引入 vue-router：页签 = 应用内状态（active ref）；v-if 懒挂载 + keep-alive。
// - 顶栏：📉 股票 跳转（:9090，cookie 跨端口免登）+ 当前用户 + 退出。
<script setup>
import { ref } from "vue";
import PositionsTab from "../tabs/PositionsTab.vue";
import StrategyTab from "../tabs/StrategyTab.vue";
import BacktestTab from "../tabs/BacktestTab.vue";
import LakeTab from "../tabs/LakeTab.vue";
import { useAuthStore } from "../stores/authStore.js";

const TABS = [
  ["my_positions", "我的持仓"],
  ["strategy", "策略"],
  ["backtest", "回测"],
  ["lake", "数据湖"],
];

const active = ref("my_positions");

// 股票系统跳转（同 hostname，cookie 按 host 不按端口 → 已登录则免登）
const stockUrl = "http://" + window.location.hostname + ":9090/";

const auth = useAuthStore();
async function doLogout() {
  await auth.logout();
}
</script>

<template>
  <header class="topbar">
    <div class="brand">🪙 加密货币 <span class="sub">独立系统 · 与股票系统同仓库 · 框架 v7.0.43</span></div>
    <nav class="tabs">
      <button v-for="[key, label] in TABS" :key="key" class="tab" :class="{ active: active === key }" @click="active = key">{{ label }}</button>
    </nav>
    <a class="topbar-jump" :href="stockUrl" title="跳转股票系统（:9090，已登录免登）">📉 股票</a>
    <div class="topbar-auth">
      <span class="topbar-user">👤 {{ auth.user }}</span>
      <button type="button" class="topbar-logout" title="退出登录（清除本机会话）" @click="doLogout">退出</button>
    </div>
  </header>

  <main>
    <keep-alive>
      <BacktestTab v-if="active === 'backtest'" />
      <PositionsTab v-else-if="active === 'my_positions'" />
      <StrategyTab v-else-if="active === 'strategy'" />
      <LakeTab v-else-if="active === 'lake'" />
    </keep-alive>
  </main>

  <footer class="footnote">
    数据源：本机 DuckDB 独立湖（data/lake/crypto.duckdb，crypto-sync 每小时灌数，Binance 主源 + OKX 交叉校验）。
    框架阶段（v7.0.43）：四页签骨架 + 数据底座；持仓/策略/回测内容后续慢慢完善。
  </footer>
</template>

<style scoped>
.topbar {
  display: flex; align-items: center; gap: 18px; flex-wrap: wrap;
  padding: 10px 16px; background: #fff; border-bottom: 1px solid #d8d8e0;
  position: sticky; top: 0; z-index: 10;
}
.brand { font-weight: 600; font-size: 15px; white-space: nowrap; }
.brand .sub { font-weight: 400; font-size: 11px; color: #999; margin-left: 8px; }
.tabs { display: flex; gap: 4px; flex-wrap: wrap; }
.tab {
  border: 1px solid transparent; background: none; padding: 6px 14px;
  font-size: 13px; color: #555; border-radius: 8px; cursor: pointer;
}
.tab:hover { background: #f0f1f5; }
.tab.active { background: #f7efd9; border-color: #e0cf9a; color: #7a5500; font-weight: 600; }
.topbar-jump {
  margin-left: auto; font-size: 13px; color: #14783c; text-decoration: none;
  padding: 4px 12px; border: 1px solid #cfe3d6; border-radius: 8px; background: #fff;
}
.topbar-jump:hover { background: #f0f7f3; }
.topbar-auth { display: flex; align-items: center; gap: 8px; white-space: nowrap; }
.topbar-user { font-size: 12px; color: #666; }
.topbar-logout {
  font-size: 12px; padding: 2px 10px; border: 1px solid #d8d8e0;
  border-radius: 6px; background: #fff; color: #555; cursor: pointer;
}
.topbar-logout:hover { border-color: #b42318; color: #b42318; }
main { padding: 16px; max-width: 1200px; margin: 0 auto; }
.footnote {
  max-width: 1200px; margin: 24px auto 32px; padding: 0 16px;
  color: #999; font-size: 12px; line-height: 1.7;
}
</style>
