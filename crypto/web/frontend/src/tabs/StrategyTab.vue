// 策略（v7.0.48 实体化，v7.0.50 编辑模式与 A 股 tab 对齐）：
// 默认只读展示生效参数 → 点【编辑模式】解锁 → 【保存策略】后锁死（回只读）。
// 与股票 StrategyTab 同语义（D-W02）：editing 是唯一状态，setEditing() 唯一入口；
// 保存成功 → 退出编辑态 + 重拉；400 被拒 → 保持编辑态可改正重试；取消 → 退出 + 重拉。
// 参数优先级：页面保存（data/crypto/strategy_params.json）> crypto.yaml 默认。
<script setup>
import { onMounted, ref } from "vue";
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

const info = ref(null);   // GET 响应（只读态数据源 + 保存后重拉）
const err = ref("");      // 加载失败
const msg = ref("");      // 操作反馈（ok/err 文本）
const editing = ref(false); // 编辑态唯一标志
const draft = ref(null);     // 编辑态草稿（进入编辑态时深拷贝 info.params）
const saving = ref(false);
const resetting = ref(false);

// 唯一入口：所有进入/退出编辑态的路径都经过它（与股票 tab 的 setEditing 同义）
function setEditing(on) {
  editing.value = !!on;
  draft.value = on
    ? { ...info.value.params }
    : null;
  if (!on) msg.value = "";
}

async function load() {
  try {
    info.value = await api("/api/strategy");
  } catch (e) {
    err.value = String((e && e.message) || e);
  }
}

onMounted(load);

function startEdit() {
  if (info.value) setEditing(true);
}

async function save() {
  saving.value = true;
  msg.value = "";
  try {
    const params = {};
    for (const f of FIELDS) {
      const v = Number(draft.value[f.key]);
      if (!Number.isFinite(v)) throw new Error(`${f.label} 需为数字`);
      params[f.key] = v;
    }
    await api("/api/strategy", {
      method: "POST",
      body: JSON.stringify({ params }),
    });
    setEditing(false); // 锁死：退出编辑态（注意：退出会清 msg，提示在之后设）
    await load();      // 重拉只读视图
    msg.value = "✓ 策略已保存（参数文件 data/crypto/strategy_params.json）";
  } catch (e) {
    // 400 拒绝 → 保持编辑态，用户可改正后重试（与股票 tab 同语义）
    msg.value = "✗ 保存被拒绝：" + String((e && e.message) || e);
  } finally {
    saving.value = false;
  }
}

async function cancel() {
  setEditing(false);
  await load();
}

async function reset() {
  resetting.value = true;
  msg.value = "";
  try {
    await api("/api/strategy/reset", { method: "POST" });
    msg.value = "✓ 已恢复 crypto.yaml 默认参数";
    await load();
  } catch (e) {
    msg.value = "✗ 恢复默认失败：" + String((e && e.message) || e);
  } finally {
    resetting.value = false;
  }
}
</script>

<template>
  <div class="card">
    <h2>策略（马丁格尔 · 深回撤网格 + 倍率止盈）</h2>
    <p v-if="err" class="err">{{ err }}</p>
    <template v-else-if="info">
      <div class="toolbar">
        <span class="muted">
          生效参数：{{ info.has_override ? "页面自定义" : "crypto.yaml 默认" }}
        </span>
        <button v-if="!editing" class="btn primary" @click="startEdit">✏️ 编辑模式</button>
        <template v-else>
          <button class="btn primary" :disabled="saving" @click="save">💾 保存策略</button>
          <button class="btn ghost" :disabled="saving" @click="cancel">取消</button>
        </template>
        <button v-if="!editing && info.has_override" class="btn ghost"
                :disabled="resetting" @click="reset">↺ 恢复默认</button>
      </div>
      <p v-if="msg" :class="msg.startsWith('✗') ? 'err' : 'ok'">{{ msg }}</p>

      <h3>规则</h3>
      <ul class="rules">
        <li>{{ info.rule.first }}</li>
        <li>{{ info.rule.add }}</li>
        <li>{{ info.rule.hold }}</li>
        <li>{{ info.rule.exit }}</li>
      </ul>

      <h3>策略参数</h3>
      <div class="grid">
        <div v-for="f in FIELDS" :key="f.key" class="field">
          <label :for="f.key">{{ f.label }}
            <em>{{ info.defaults[f.key] }}（默认）</em>
          </label>
          <div v-if="editing" class="inp">
            <input :id="f.key" v-model.number="draft[f.key]" type="number"
                   :step="f.step" />
            <span class="unit">{{ f.unit }}</span>
          </div>
          <div v-else class="val">
            {{ info.params[f.key] }} <span class="unit">{{ f.unit }}</span>
          </div>
          <p class="hint">{{ f.hint }}</p>
        </div>
      </div>

      <p v-if="!editing" class="hint">
        参数只读，点【编辑模式】解锁修改；保存后锁死，回测 tab 立即按新参数执行。
        参数存 data/crypto/strategy_params.json（运行时覆盖，不改仓库配置）。
      </p>
    </template>
  </div>
</template>

<style scoped>
.card { background: #fff; border: 1px solid #d8d8e0; border-radius: 12px; padding: 20px; }
h2 { margin: 0 0 12px; font-size: 16px; }
h3 { margin: 18px 0 8px; font-size: 13px; color: #555; }
.err { color: #b42318; font-size: 13px; }
.ok { color: #188038; font-size: 13px; }
.muted { color: #888; font-size: 12px; }
.toolbar { display: flex; gap: 10px; align-items: center; margin-bottom: 6px; flex-wrap: wrap; }
.rules { margin: 0; padding-left: 18px; font-size: 13px; color: #444; line-height: 1.7; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 12px; }
.field label { font-size: 12px; color: #555; display: block; margin-bottom: 4px; }
.field label em { font-style: normal; color: #aaa; font-size: 11px; }
.inp { display: flex; align-items: center; gap: 6px; }
.inp input {
  flex: 1; width: 100%; padding: 6px 8px; border: 1px solid #d0d0d8;
  border-radius: 8px; font-size: 13px;
}
.val { font-size: 14px; font-weight: 600; color: #222; padding: 4px 0; }
.unit { font-size: 12px; color: #888; min-width: 34px; font-weight: 400; }
.hint { color: #999; font-size: 11px; margin: 4px 0 0; }
.btn {
  padding: 7px 16px; border-radius: 8px; border: 1px solid #d0d0d8;
  background: #fff; font-size: 13px; cursor: pointer;
}
.btn.primary { background: #188038; border-color: #188038; color: #fff; }
.btn.ghost { background: #fff; color: #555; }
.btn:disabled { opacity: 0.5; cursor: default; }
</style>
