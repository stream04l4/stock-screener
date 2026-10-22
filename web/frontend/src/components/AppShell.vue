// 应用壳（v7.0 四页签）：回测 / 我的持仓 / 策略 / 数据湖。
// - 不引入 vue-router：页签 = 应用内状态（active ref）。
// - 非当前 tab 内容懒挂载：v-if="active === x" → 首次激活才渲染并发请求。
//   已挂载过的 tab 用 keep-alive 保活（切回状态仍在）。
// v7.0：v6"运行"页签（日跑+多因子回测）随策略核心移除；回测成为主工作流页签。
<script setup>
import { ref } from "vue";
import ToastBar from "./ToastBar.vue";
import BacktestTab from "../tabs/BacktestTab.vue";
import MyPositionsTab from "../tabs/MyPositionsTab.vue";
import StrategyTab from "../tabs/StrategyTab.vue";
import LakeTab from "../tabs/LakeTab.vue";

const TABS = [
  ["backtest", "回测"],
  ["my_positions", "我的持仓"],
  ["strategy", "策略"],
  ["lake", "数据湖"],
];

// 默认激活"回测"（v7 主工作流：按需回测 + 进度 + 结果）
const active = ref("backtest");
</script>

<template>
  <header class="topbar">
    <div class="brand">📉 深回撤金字塔加仓 <span class="sub">A股深回撤加仓单策略 · 高点回撤首仓 · 阶梯加仓不封顶（v7.0）</span></div>
    <nav class="tabs">
      <button v-for="[key, label] in TABS" :key="key" class="tab" :class="{ active: active === key }" @click="active = key">{{ label }}</button>
    </nav>
    <ToastBar />
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
    数据源：本机 DuckDB 数据湖（data/lake/，lake-sync 定时灌数，BaoStock T+1）；页面数据读自湖 + output/backtest_martingale/ 回测产物 + config/strategy.yaml。
    v7.0：单一策略（深回撤金字塔加仓）——逐股独立状态机，高点回撤首仓、每再跌一档加仓一档（金额×growth 递增，上不封顶）、不卖出、无熔断；
    信号用后复权收盘、成交按原始收盘、现金分红单列（cash 口径）。湖内无退市股（幸存者偏差），回测产物已带声明。
  </footer>
</template>
