// 今日选股（v7.0.13，策略页签「选股策略」卡片内）。
// - 数据：GET /api/timing（候选池 × 已启用时机条件的 OR 命中；读 selection_timing
//   预计算表 O(1)，服务端排序 = 会提醒的优先 → 股息率降序 → 代码）。
// - 双口径（与后端单一定义点 martingale/timing.py 一致）：
//   本表 = 命中任意已启用条件（观察名单）；「买入区」徽标 = ①价格低位 且 ②高股息率
//   （③年线启用时叠加）= 微信"新进入买入区"提醒口径（仅新触发推送，每日湖更新后自动检查）。
// - 一键加入我的持仓：行内展开 股数/成本价（成本默认当前价）→ PUT /api/my_positions
//   upsert（与「我的持仓」录入条同一端点/口径）→ 成功后 invalidate("my_positions")。
// - 防御：as_of=null（timing 表未生成）→ 空表 + 等待提示；加载失败 → 一行错误。
<script setup>
import { reactive, ref } from "vue";
import { api } from "../../api/client.js";
import { useLiveQuery } from "../../live/useLiveQuery.js";
import { invalidate } from "../../live/queryRegistry.js";

const { data, loading, error } = useLiveQuery("timing", () => api("/api/timing"), {});

// 行内加持仓：{ ts_code: { shares, cost } }；msg 暂存保存结果反馈。
const open = reactive({});
const busy = ref("");
const msg = reactive({});
function openAdd(r) {
  open[r.ts_code] = { shares: "", cost: r.close != null ? String(r.close) : "" };
}
async function saveAdd(r) {
  const o = open[r.ts_code];
  if (!o) return;
  const shares = Number(o.shares);
  const cost = Number(o.cost);
  if (!Number.isFinite(shares) || shares <= 0) { msg[r.ts_code] = { ok: false, text: "股数应为正数" }; return; }
  if (!Number.isFinite(cost) || cost < 0) { msg[r.ts_code] = { ok: false, text: "成本价应为非负数" }; return; }
  busy.value = r.ts_code;
  try {
    await api("/api/my_positions", {
      method: "PUT",
      body: JSON.stringify({ ts_code: r.ts_code, shares, cost_price: cost }),
    });
    msg[r.ts_code] = { ok: true, text: `已加入「我的持仓」（${r.name} ${shares} 股 @${cost}），持仓策略接管后续档位` };
    delete open[r.ts_code];
    invalidate("my_positions");
  } catch (e) {
    msg[r.ts_code] = { ok: false, text: String((e && e.message) || e) };
  } finally {
    busy.value = "";
  }
}

function fmt(v, suffix = "") {
  return v === null || v === undefined ? "—" : `${v}${suffix}`;
}
</script>

<template>
  <div class="ts">
    <div class="ts-head">
      <span class="ts-title">今日选股（{{ data ? data.n_watchlist : "…" }} / 候选池 {{ data ? data.n_pool : "…" }}）</span>
      <span v-if="data && data.as_of" class="ts-muted">数据截至 {{ data.as_of }}</span>
      <span v-else-if="data" class="ts-muted">等待每日湖更新（timing 表尚未生成）</span>
      <span v-if="loading" class="ts-muted">加载中…</span>
    </div>
    <p class="ts-note">
      列表 = 命中任意已启用条件（观察）；<b>买入区</b> = 价格低位且高股息率同时命中（微信只推<b>新触发</b>）。
      确认后「加持仓」，持仓策略自动接管加仓档位。
    </p>
    <p v-if="error" class="ts-err">今日选股加载失败：{{ error }}</p>
    <div v-else-if="data" class="tbl-wrap">
      <table class="data">
        <thead>
          <tr>
            <th>代码</th><th>名称</th>
            <th class="num">现价</th>
            <th class="num">{{ data.params.price_window_years }}年分位%</th>
            <th class="num">{{ data.params.price_window_years }}年回撤%</th>
            <th class="num">股息率%</th>
            <th>触发条件</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="!data.rows.length">
            <td colspan="8" class="muted">
              （当前无股票命中已启用的选股条件{{ data.as_of ? "" : "——等待每日湖更新" }}）
            </td>
          </tr>
          <template v-for="r in data.rows" :key="r.ts_code">
            <tr :class="{ 'ts-push': r.push_eligible }">
              <td>{{ r.ts_code }}</td>
              <td>
                {{ r.name }}
                <span v-if="r.push_eligible" class="ts-badge">买入区</span>
              </td>
              <td class="num">{{ fmt(r.close) }}</td>
              <td class="num">{{ fmt(r.pctile_window) }}</td>
              <td class="num">{{ fmt(r.dd_window) }}</td>
              <td class="num">{{ fmt(r.ttm_yield_pct) }}</td>
              <td class="ts-reasons">{{ r.reasons || "—" }}</td>
              <td class="ts-op">
                <button v-if="!open[r.ts_code]" type="button" class="ts-btn"
                        @click="openAdd(r)">加持仓</button>
                <!-- 保存反馈挂在主行（成功收起录入行后仍可见；错误时录入行保持展开） -->
                <span v-if="msg[r.ts_code]" class="ts-msg"
                      :class="msg[r.ts_code].ok ? 'ts-msg-ok' : 'ts-msg-err'">{{ msg[r.ts_code].text }}</span>
              </td>
            </tr>
            <tr v-if="open[r.ts_code]" class="ts-add-row">
              <td :colspan="8">
                <span class="ts-add-label">加入我的持仓 · {{ r.name }}</span>
                <input v-model="open[r.ts_code].shares" class="inp num ts-inp"
                       placeholder="股数（如 1000）" @keyup.enter="saveAdd(r)" />
                <input v-model="open[r.ts_code].cost" class="inp num ts-inp"
                       placeholder="成本价" @keyup.enter="saveAdd(r)" />
                <button type="button" class="ts-btn" :disabled="busy === r.ts_code"
                        @click="saveAdd(r)">{{ busy === r.ts_code ? "保存中…" : "保存" }}</button>
              </td>
            </tr>
          </template>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
.ts { margin-top: 12px; padding-top: 10px; border-top: 1px dashed var(--line, #d8d8e0); }
.ts-head { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
.ts-title { font-size: 14px; font-weight: 600; }
.ts-muted { color: #999; font-size: 12px; }
.ts-note { color: #888; font-size: 12px; margin: 4px 0 8px; }
.ts-err { color: #b42318; font-size: 13px; margin: 6px 0; }
.ts-push { background: rgba(20, 120, 60, 0.07); }
.ts-badge {
  display: inline-block; margin-left: 6px; padding: 0 6px; font-size: 11px;
  color: #fff; background: #14783c; border-radius: 8px; vertical-align: 1px;
}
.ts-reasons { font-size: 12px; color: #555; min-width: 0; overflow-wrap: anywhere; }
.ts-op { min-width: 0; overflow-wrap: anywhere; }
.ts-btn {
  font-size: 12px; padding: 2px 10px; border: 1px solid var(--line, #d8d8e0);
  border-radius: 6px; background: #fff; cursor: pointer; white-space: nowrap;
}
.ts-btn:hover:not(:disabled) { border-color: #14783c; color: #14783c; }
.ts-btn:disabled { opacity: 0.6; cursor: default; }
.ts-add-row td { background: #fafafa; }
.ts-add-label { font-size: 12px; color: #555; margin-right: 8px; }
.ts-inp { width: 110px; margin-right: 6px; }
.ts-msg { font-size: 12px; margin-left: 8px; }
.ts-msg-ok { color: #14783c; }
.ts-msg-err { color: #b42318; }
</style>
