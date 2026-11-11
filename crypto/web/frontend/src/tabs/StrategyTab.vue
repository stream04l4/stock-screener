// 策略（v7.0.48 实体化）：当前生效参数 + 可调表单（覆盖文件）+ 规则说明。
// 参数优先级：页面保存（data/crypto/strategy_params.json）> crypto.yaml 默认。
<script setup>
import { onMounted, reactive, ref } from "vue";
import { api } from "../api/client.js";

const FIELDS = [
  { key: "first_drop_pct", label: "建仓跌幅", unit: "%", step: 1,
    hint: "价格从一年高点下跌该百分比（至高点×(1−该值)）即建仓" },
  { key: "add_grid_pct", label: "加仓级距", unit: "%", step: 0.5,
    hint: "自建仓价每下跌该百分比加一级（固定网格，不设上限）" },
  { key: "base_amount_usdt", label: "首仓金额", unit: "USDT", step: 1,
    hint: "首次建仓投入" },
  { key: "growth", label: "加仓倍数", unit: "×", step: 0.01,
    hint: "第 k 级加仓 = 首仓金额 × 该倍数的 k 次方" },
  { key: "take_profit_mult", label: "止盈倍数", unit: "×", step: 0.5,
    hint: "价格涨至建仓价该倍数 → 全部卖出，重置进入下一轮" },
  { key: "fee_bps", label: "手续费", unit: "bps", step: 5,
    hint: "taker 费率（买卖双向计；0 = 不计费，10 = 0.1%）" },
  { key: "lookback_h", label: "一年高点窗口", unit: "h", step: 168,
    hint: "滚动窗口小时数（8760 = 365 天）" },
];

const info = ref(null);
const err = ref("");
const saved = ref(false);
const busy = ref(false);
const form = reactive({});

onMounted(async () => {
  try {
    info.value = await api("/api/strategy");
    for (const f of FIELDS) form[f.key] = info.value.params[f.key];
  } catch (e) {
    err.value = String((e && e.message) || e);
  }
});

async function save() {
  busy.value = true; saved.value = false;
  try {
    const params = {};
    for (const f of FIELDS) {
      const v = Number(form[f.key]);
      if (!Number.isFinite(v)) throw new Error(`${f.label} 需为数字`);
      params[f.key] = v;
    }
    const r = await api("/api/strategy", {
      method: "POST",
      body: JSON.stringify({ params }),
    });
    info.value = { ...info.value, params: r.params, has_override: r.has_override };
    saved.value = true;
  } catch (e) {
    err.value = String((e && e.message) || e);
  } finally {
    busy.value = false;
  }
}

async function reset() {
  busy.value = true; saved.value = false;
  try {
    const r = await api("/api/strategy/reset", { method: "POST" });
    info.value = { ...info.value, params: r.params, has_override: r.has_override };
    for (const f of FIELDS) form[f.key] = r.params[f.key];
    saved.value = true;
  } catch (e) {
    err.value = String((e && e.message) || e);
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div class="card">
    <h2>策略（马丁格尔 · 深回撤网格 + 倍率止盈）</h2>
    <p v-if="err" class="err">{{ err }}</p>
    <template v-else-if="info">
      <div class="badge-row">
        <span :class="['badge', info.has_override ? 'on' : 'off']">
          {{ info.has_override ? '页面自定义参数生效' : 'crypto.yaml 默认参数' }}
        </span>
        <span v-if="saved" class="ok">已保存 ✓</span>
      </div>

      <h3>规则</h3>
      <ul class="rules">
        <li>{{ info.rule.first }}</li>
        <li>{{ info.rule.add }}</li>
        <li>{{ info.rule.hold }}</li>
        <li>{{ info.rule.exit }}</li>
      </ul>

      <h3>参数调整</h3>
      <div class="grid">
        <div v-for="f in FIELDS" :key="f.key" class="field">
          <label :for="f.key">{{ f.label }}
            <em>{{ info.defaults[f.key] }}（默认）</em>
          </label>
          <div class="inp">
            <input :id="f.key" v-model.number="form[f.key]" type="number"
                   :step="f.step" />
            <span class="unit">{{ f.unit }}</span>
          </div>
          <p class="hint">{{ f.hint }}</p>
        </div>
      </div>

      <div class="actions">
        <button class="btn primary" :disabled="busy" @click="save">保存参数</button>
        <button v-if="info.has_override" class="btn" :disabled="busy"
                @click="reset">恢复默认</button>
        <span class="hint">保存后回测 tab 立即按新参数执行；参数存
          data/crypto/strategy_params.json（运行时覆盖，不改仓库配置）</span>
      </div>
    </template>
  </div>
</template>

<style scoped>
.card { background: #fff; border: 1px solid #d8d8e0; border-radius: 12px; padding: 20px; }
h2 { margin: 0 0 12px; font-size: 16px; }
h3 { margin: 18px 0 8px; font-size: 13px; color: #555; }
.err { color: #b42318; font-size: 13px; }
.badge-row { display: flex; gap: 10px; align-items: center; margin-bottom: 6px; }
.badge { font-size: 12px; padding: 3px 10px; border-radius: 999px; }
.badge.on { background: #e6f4ea; color: #188038; }
.badge.off { background: #f0f1f5; color: #666; }
.ok { color: #188038; font-size: 12px; }
.rules { margin: 0; padding-left: 18px; font-size: 13px; color: #444; line-height: 1.7; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 12px; }
.field label { font-size: 12px; color: #555; display: block; margin-bottom: 4px; }
.field label em { font-style: normal; color: #aaa; font-size: 11px; }
.inp { display: flex; align-items: center; gap: 6px; }
.inp input {
  flex: 1; width: 100%; padding: 6px 8px; border: 1px solid #d0d0d8;
  border-radius: 8px; font-size: 13px;
}
.unit { font-size: 12px; color: #888; min-width: 34px; }
.hint { color: #999; font-size: 11px; margin: 4px 0 0; }
.actions { display: flex; gap: 10px; align-items: center; margin-top: 16px; flex-wrap: wrap; }
.btn {
  padding: 7px 16px; border-radius: 8px; border: 1px solid #d0d0d8;
  background: #fff; font-size: 13px; cursor: pointer;
}
.btn.primary { background: #188038; border-color: #188038; color: #fff; }
.btn:disabled { opacity: 0.5; cursor: default; }
</style>
