// 系统选择页（登录成功后出现）：同一个登录页 → 选择进入哪个系统。
// - A股4维选股 → 关闭选择，渲染 AppShell（9090 本站）；
// - Quant iTransformer → 跳转 9091（共享 ssc_session cookie，自动免登）。
// 刷新/重进（cookie 仍有效）不经过本页，直达 A股控制台。
<script setup>
import { useAuthStore } from "../stores/authStore.js";

const auth = useAuthStore();

function goStock() {
  auth.dismissChoice();
}

function goQuant() {
  // 用当前 hostname（支持外网 IP/域名访问）+ 9091 端口
  window.location.href = "http://" + window.location.hostname + ":9091/";
}
</script>

<template>
  <div class="choice-wrap">
    <div class="choice-card">
      <h1 class="choice-title">🔓 登录成功，选择进入系统</h1>
      <p class="choice-sub">两个系统共用同一套登录，任意一侧登录后另一侧免登</p>
      <div class="choice-grid">
        <button type="button" class="choice-btn choice-stock" @click="goStock">
          <span class="choice-emoji">📉</span>
          <span class="choice-name">A股4维选股</span>
          <span class="choice-desc">深回撤金字塔加仓策略 · 数据湖驱动</span>
        </button>
        <button type="button" class="choice-btn choice-quant" @click="goQuant">
          <span class="choice-emoji">⚡</span>
          <span class="choice-name">Quant iTransformer</span>
          <span class="choice-desc">BTCUSDT 永续合约 · Gate 沙盒</span>
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.choice-wrap {
  min-height: 100vh; display: flex; align-items: center; justify-content: center;
  padding: 16px; background: #f5f6fa;
}
.choice-card {
  width: 100%; max-width: 640px; box-sizing: border-box;
  background: #fff; border: 1px solid var(--line, #d8d8e0); border-radius: 12px;
  padding: 32px 28px; box-shadow: 0 4px 16px rgba(0, 0, 0, 0.06);
}
.choice-title { font-size: 18px; margin: 0 0 4px; }
.choice-sub { color: #888; font-size: 12px; margin: 0 0 22px; }
.choice-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.choice-btn {
  display: flex; flex-direction: column; align-items: center; gap: 6px;
  padding: 22px 12px; border: 1px solid var(--line, #d8d8e0); border-radius: 10px;
  background: #fafbfc; cursor: pointer; text-align: center;
  transition: border-color 0.15s, background 0.15s, transform 0.1s;
}
.choice-btn:hover { transform: translateY(-2px); }
.choice-stock:hover { border-color: #14783c; background: #f2faf5; }
.choice-quant:hover { border-color: #1a56db; background: #f2f6ff; }
.choice-emoji { font-size: 30px; }
.choice-name { font-size: 15px; font-weight: 600; color: #222; }
.choice-desc { font-size: 12px; color: #888; }
@media (max-width: 520px) {
  .choice-grid { grid-template-columns: 1fr; }
}
</style>
