// 应用壳（4 页签，lake-source brief E 节：运行/结果合并为单一"运行"tab）：
// 移植 index.html topbar/main/footnote 结构 + app.js switchTab 行为。
// - 不引入 vue-router：页签 = 应用内状态（active ref）。
// - 非当前 tab 内容懒挂载：v-if="active === x" → 首次激活才渲染并发请求（报告 §3 / brief B0）。
//   已挂载过的 tab 用 keep-alive 保活（旧版 .tab-panel 隐藏不销毁，切回状态仍在——行为一致）。
<script setup>
import { ref } from "vue";
import ToastBar from "./ToastBar.vue";
import StrategyTab from "../tabs/StrategyTab.vue";
import BacktestTab from "../tabs/BacktestTab.vue";
import RunTab from "../tabs/RunTab.vue";
import LakeTab from "../tabs/LakeTab.vue";

const emit = defineEmits(["open-stock"]);

const TABS = [
  ["run", "运行"],
  ["strategy", "策略"],
  ["backtest", "回测"],
  ["lake", "数据湖"],
];

// lake-source brief E 节：默认激活"运行"（原结果页内容已并入其下半区）
const active = ref("run");
</script>

<template>
  <header class="topbar">
    <div class="brand">📈 A股四维选股 <span class="sub">技术面 · 股息率 · 行业排名 · 基本面（v2 多因子打分）</span></div>
    <nav class="tabs">
      <button v-for="[key, label] in TABS" :key="key" class="tab" :class="{ active: active === key }" @click="active = key">{{ label }}</button>
    </nav>
    <ToastBar />
  </header>

  <main>
    <keep-alive>
      <RunTab v-if="active === 'run'" @open-stock="(code, day) => emit('open-stock', code, day)" />
      <StrategyTab v-else-if="active === 'strategy'" />
      <BacktestTab v-else-if="active === 'backtest'" />
      <LakeTab v-else-if="active === 'lake'" />
    </keep-alive>
  </main>

  <footer class="footnote">
    数据源：BaoStock（日K / 分红 / 季报 / 行业 / 股票列表）；A股日K为 T+1 更新，筛选运行日 = 请求日期回退到的最近交易日。
    v2：稳定键增量缓存（kline_af3 + adjfactor 本地重建后复权价）+ 截面 Z-Score 多因子打分；行业 = 证监会二级分类；Piotroski S2/S3/S5/S9 为比率代理口径。
    页面数据均读自本机 output/ · config/ · logs/ · cache/ 文件系统，无数据库。
  </footer>
</template>
