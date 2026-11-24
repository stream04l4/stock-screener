// 我的持仓 tab（v7.0.1，原「查询」改名重定位）：维护用户自己的实际持仓状态。
// - 账本 = data/user_positions.json（后端 /api/my_positions 读写，原子落盘）；
// - 展示 = 账本 × 展示价（v7.0.40：实时行情优先，行情失败按股回退湖最新价）
//   + 策略状态机参考（当前档/下次加仓价/再武装价，池外股同样有参考——状态机不挑池）；
// - 添加/更新/删除：顶部录入条 + 行点击弹层编辑，PUT 单条 upsert（shares=0 删除）。
// 数据按需加载（useLiveQuery，非日跑）→ 手动刷新按钮 invalidate。
<script setup>
import { computed, reactive, ref } from "vue";
import { api } from "../api/client.js";
import { useLiveQuery } from "../live/useLiveQuery.js";
import { invalidate, peek } from "../live/queryRegistry.js";
import { inTradingWindow } from "../live/tradingWindow.js";
import { useTaskStore } from "../stores/taskStore.js";
import SimpleTable from "../components/SimpleTable.vue";
import Modal from "../components/Modal.vue";
import { fmtPrice, fmtPct, fmtInt, fmtMoney } from "../utils/fmt.js";

const store = useTaskStore();

// ---------------------------------------------------------------------------
// 数据（v7.0.30 实时刷新：交易时段内 30s 自动轮询——后端对「股票池 ∪ 持仓」
// 取腾讯实时价，前端零维护；非交易时段不发请求、保留最近数据。首载恒取一次。
// 策略固定 = 当前策略 config/strategy.yaml，v7.0.29 策略库退役。）
// ---------------------------------------------------------------------------
const POLL_MS = 30000;
// 时段门取数：交易时段 或 尚无缓存（首载）→ 走网络；非时段有缓存 → 直接返回
// 缓存（registry 记 at=now，轮询空转零网络）。手动刷新按钮经 forceKeys 强制
// 走网络（invalidate 不清缓存，只打过期标记——不强制的话非时段手刷会拿到旧缓存）。
const forceKeys = new Set();
function gatedFetch(key, url) {
  return () => {
    if (forceKeys.delete(key)) return api(url);
    if (inTradingWindow() || peek(key) === undefined) return api(url);
    return Promise.resolve(peek(key));
  };
}

const { data: myData, loading: myLoading, error: myError } =
  useLiveQuery("my_positions", gatedFetch("my_positions", "/api/my_positions"),
               { interval: POLL_MS });

function refresh() {
  forceKeys.add("my_positions");
  forceKeys.add("strategy_signals");
  invalidate("my_positions");
  invalidate("strategy_signals");
}

// ---------------------------------------------------------------------------
// 策略信号中心（v7.0.19；v8.2.5：当前=微信推送信号区 / 潜力信号区 / 历史）
// 数据 = /api/strategy_signals（股票池 ∪ 我的持仓，口径与 /api/my_positions 同一
// 定义点）；策略固定 = 当前策略（v7.0.29）；分段切换（active/approaching/history）。
// ---------------------------------------------------------------------------
const sigSel = ref("active"); // active | approaching | history
const { data: sigData, loading: sigLoading, error: sigError } =
  useLiveQuery("strategy_signals",
               gatedFetch("strategy_signals", "/api/strategy_signals"),
               { interval: POLL_MS });

const SIG_HISTORY_CAP = 200; // 历史信号展示上限（全量仍在 API 里，前端截断）
const sigStocks = computed(() => sigData.value?.stocks || []);
// **v8.2.5（Joel 2026-11-24 定案）+ v8.2.5b 行着色：当前信号区 = 信号区**——
// 入区条件 action 非空。注意 classify_signal 对 approaching（距线 ≤5% 未到
// 线）也返回 action（「到线将执行的动作」预告），故本段含「已触发」与
// 「即将触发」两类行——位置不变、以行着色区分（sigRowClass）：浅红 = 真正
// 触发（active 价已破线 / touched 盘中已触回弹），浅灰 = 即将触发
// （approaching 价在线上方 5% 内）。微信推送信号 = 浅红行（价格到线 且
// 时机门全过）。旧「touched 保留到当日」（11-23 定案）作废——触线但时机
// 未过（action 空）不入本段（去潜力信号段）。
const sigActive = computed(() => sigStocks.value.filter((s) => s.action != null));
// **潜力信号区**（v8.2.5 改名「即将进入」，Joel 2026-11-24 定案）——满足
// 任一即入段：
// ① 价格距触发线 ≤ approach_pct%（绝对距离；非持仓/持仓 k=0 = 首仓线，
//    持仓 k≥1 = 下一加仓线；含「已破线但时机未过」的 touched 行）；
// ② 5年分位 ≤ 50%（price_pctile_max + 10，**无下界**——含已过门的 ≤40%，
//    「门过了等价格」的潜力股也显示；如分位 20% 距线再远也入段）。
// 与当前信号段**互斥**：action 非空（微信推送信号）只展示在「当前」。
// 分位缺失（无时机数据 fail-closed）→ 仅 ① 路可入。
const sigApproach = computed(() => {
  const band = Number(sigData.value?.approach_pct ?? 5);
  const pctCap = Number(sigData.value?.price_pctile_max ?? 40) + 10; // 50%
  return sigStocks.value.filter((s) => {
    if (s.action != null) return false; // 当前信号段优先
    const dist = s.distance_pct;
    const nearLine = dist != null && Math.abs(dist) <= band;
    const pct = s.timing_pctile;
    const nearPct = typeof pct === "number" && pct <= pctCap;
    return nearLine || nearPct;
  });
});
// **v8.2.5b 行着色（Joel 2026-11-24 定案）**：「当前」「潜力信号」段位置
// 不变，以行底色区分触发状态——浅红（sig-row-hit）= 真正触发（价已破线
// active / 盘中已触回弹 touched）；浅灰（sig-row-near）= 即将触发（价在
// 线上方 5% 内 approaching）。classify_signal 对 approaching 也返回
// action 预告，不靠入区条件区分，故须着色。
function sigRowClass(row) {
  if (row.status === "active" || row.status === "touched") return "sig-row-hit";
  if (row.status === "approaching") return "sig-row-near";
  return "";
}
// v8.2.5c（Joel 2026-11-24）：分段计数「当前信号（N/M）」——N = 真正触发
// （浅红行：active 价已破线 / touched 盘中已触回弹）个数，M = 当前段总行数。
const sigHitCount = computed(() => sigActive.value.filter(
  (s) => s.status === "active" || s.status === "touched").length);
// v8.2.5d（Joel 2026-11-24）：历史信号只显示**微信推送过的真实触发信号**
// （pushed=true = 该档点位提醒已推微信）；状态机回放中未推送的记录（含
// 未推送的触碰事件）不展示。
const sigHistoryAll = computed(() =>
  (sigData.value?.history || []).filter((h) => h.pushed));
const sigHistory = computed(() => sigHistoryAll.value.slice(0, SIG_HISTORY_CAP));

// 档位标签：策略首笔（k=0 且本轮首买）=「首仓」，其余 =「第 N 档」（N=k+1）
function levelLabel(level, firstOfRound) {
  if (firstOfRound && level === 0) return "首仓";
  return "第" + ((level ?? 0) + 1) + "档";
}
// v8.2.3 轮次口径（Joel 2026-11-23）：用户相对轮次号 → 前缀（「首轮/第二
// 轮/…/第N轮」；null/无数据 → 空串不加前缀）
// v8.2.4（Joel 2026-11-24）：无动作行（时机未过/价未到位，action=null）档位
// 列用「线」说法（首仓线/建仓线）——避免被读成「提示首仓/建仓」的信号；
// 有真实动作的行保留动作词（首仓/建仓/第N档）
function lineNotation(label) {
  return label.replace("首仓", "首仓线").replace("建仓", "建仓线");
}
function roundNoLabel(n) {
  if (!n || n <= 0) return "";
  const c = ["一", "二", "三", "四", "五", "六", "七", "八", "九"];
  return n <= 9 ? (n === 1 ? "首轮" : c[n - 1] + "轮") : "第" + n + "轮";
}
function fmtWan(v) {
  if (v === null || v === undefined) return "—";
  return (v / 10000).toFixed(1);
}
// 分段 → 展示行（预渲染标签列；SimpleTable 单元格为纯文本）
function btText(s) {
  if (s.bt_pnl_pct === null || s.bt_pnl_pct === undefined) return "—";
  const sign = s.bt_pnl_pct >= 0 ? "+" : "";
  return sign + s.bt_pnl_pct + "% · " + (s.bt_n_buys ?? "—") + "档";
}
function timingText(s) {
  // 持仓股轮内加仓档不受时机限制 → 「—」；首档（二次建仓）须过时机门 →
  // timing_ok 有 true/false（v8.2.0 补）→ 展示分位明细；非持仓照旧
  if (s.held && s.timing_ok === null && s.timing_ok !== false) return "—";
  return s.timing_reasons || "—";
}
function actionText(s) {
  // v8.2.3：「持仓已够」抑制已退役（v8.2.0 差值口径）——持仓触线必提醒，
  // action 为空只剩「时机未过」（k=0 二次建仓位时机门 / 非持仓时机门）
  if (!s.action) return "时机未过";
  if (s.held) {
    // v8.2.0 补：首档触线 = 建仓（再武装后新一轮 = 二次建仓），不是「加仓」
    if (s.new_round) return s.in_pos ? "二次建仓" : "建仓";
    return s.action === "buy" ? "买入·首仓" : "加仓";
  }
  // 非持仓统一「买入·首仓」（v7.0.23：触发线恒为首仓线，无「第N档加入」口径）
  return "买入·首仓";
}
const sigShown = computed(() => {
  if (sigSel.value === "active") {
    return sigActive.value.map((s) => ({ ...s,
      status_tag: (s.held ? "● 持仓" : "○ 非持仓") + (s.touched ? " · 已触回" : ""),
      action_label: actionText(s),
      // v8.2.3：持仓行档位带用户相对轮次前缀（round_no null → 无前缀）
      // v8.2.4：无动作行（时机未过）档位用「线」说法，不读成信号
      level_label: (() => {
        const lbl = s.new_round ? (s.in_pos
          ? `再武装·建仓（${roundNoLabel(s.round_no) || ""}第1档）` : "建仓（第1档）")
          : (s.held && s.in_pos
             ? `${roundNoLabel(s.round_no)}第${s.k_level ?? 0}档→第${(s.k_level ?? 0) + 1}档`
             : "首仓（第1档）");
        return s.action == null ? lineNotation(lbl) : lbl;
      })(),
      lots_s: s.suggested_lots == null ? "—" : `${s.suggested_lots} 手`,
      amount_w: s.suggested_lots != null && s.price
        ? fmtWan(s.suggested_lots * 100 * s.price) : "—",
      day_low_s: s.day_low == null ? "—" : String(s.day_low),
      bt_s: btText(s), timing_s: timingText(s) }));
  }
  if (sigSel.value === "approaching") {
    return sigApproach.value.map((s) => ({ ...s,
      // v8.2.5：入段条件（距线≤5% / 分位≤50%）由「距触发」「时机」列直接
      // 可见，状态列只标 持仓/非持仓/已触回
      status_tag: (s.held ? "● 持仓" : "○ 非持仓")
        + (s.touched ? " · 已触回" : ""),
      // 带符号：+ = 线上方（临近），− = 已破线（含时机未过）
      distance_s: (s.distance_pct != null
        ? (s.distance_pct >= 0 ? "+" : "") + s.distance_pct.toFixed(1) : "—") + "%",
      // v8.2.0 补（Joel 2026-11-21）：下一档手数 = API next_lots（单点原值，
      // 不受时机门/持仓已够门控——临近时机线的 k=0 行也显示首仓手数）
      lots_s: s.next_lots == null ? "—" : `${s.next_lots} 手`,
      // 持仓 in_pos 的 k=0 = 再武装后新一轮首仓（二次建仓位），非「第 1 档」加仓；
      // v8.2.3：持仓行档位带用户相对轮次前缀（round_no null → 无前缀）
      // v8.2.4：该段行 action 恒 null → 档位一律「线」说法
      level_label: lineNotation((s.held && s.in_pos)
        ? (s.k_level === 0
           ? `再武装·建仓（${roundNoLabel(s.round_no) || ""}第1档）`
           : `${roundNoLabel(s.round_no)}第` + ((s.k_level ?? 0) + 1) + "档")
        : "首仓（第1档）"),
      bt_s: btText(s), timing_s: timingText(s) }));
  }
  return sigHistory.value.map((h) => ({ ...h,
    status_tag: h.held ? "● 持仓" : "○ 非持仓",
    level_label: levelLabel(h.level, h.first_of_round),
    lots_s: h.lots == null ? "—" : `${h.lots} 手`,
    amount_w: fmtWan(h.amount)}));
});
const SIG_ACTIVE_COLS = [
  ["ts_code", "代码"], ["name", "名称"], ["status_tag", "状态"],
  ["price", "现价", true, fmtPrice], ["next_add_price_raw", "触发价", true, fmtPrice],
  ["day_low_s", "日内低", true],
  ["action_label", "动作"], ["level_label", "档位"], ["lots_s", "建议手数"],
  ["amount_w", "参考金额(万)", true],
  ["bt_s", "10年回测"], ["timing_s", "时机(非持仓)"],
];
const SIG_APPROACH_COLS = [
  ["ts_code", "代码"], ["name", "名称"], ["status_tag", "状态"],
  ["price", "现价", true, fmtPrice], ["next_add_price_raw", "触发价", true, fmtPrice],
  ["distance_s", "距触发", true], ["level_label", "档位"],
  // v8.2.0 补（Joel 2026-11-21）：下一档到线将买的手数（整数，格式三规则）
  ["lots_s", "下一档手数"],
  ["bt_s", "10年回测"], ["timing_s", "时机(非持仓)"],
];
const SIG_HISTORY_COLS = [
  ["date", "日期"], ["ts_code", "代码"], ["name", "名称"], ["status_tag", "状态"],
  ["level_label", "档位"], ["price", "触发价", true, fmtPrice],
  ["lots_s", "手数"], ["amount_w", "金额(万)", true],
];

// ---------------------------------------------------------------------------
// 表格列
// ---------------------------------------------------------------------------
const MY_COLS = [
  ["ts_code", "代码"], ["name", "名称"],
  // v8.2.0 补（Joel 2026-11-20 全局格式统一）：价格 2 位 / 百分比 1 位 / 股数手数整数
  ["shares", "股数", true, fmtInt], ["cost_price", "成本价", true, fmtPrice], ["last_price", "最新价", true, fmtPrice],
  // v8.2.3 轮次口径（Joel 2026-11-23）：理论 = 用户首轮窗口起点起策略累计
  // （多轮总理论值，仅展示）；实际 = 账本手数；差值 = max(0,理论−实际)；
  // 湖中无数据的股为 "—"。建议量恒为本档手数（顶部信号条），不随差值补差。
  ["theory_lots", "多轮理论(手)", true, fmtInt], ["actual_lots", "实际(手)", true, fmtInt],
  ["gap_lots", "差值(手)", true, fmtInt],
  ["market_value", "持仓市值", true, fmtMoney], ["pnl", "浮动盈亏", true, fmtMoney], ["pnl_pct", "盈亏%", true, fmtPct],
  // v8.2.3：策略档 = 用户相对轮次前缀 + 当前档位（首轮第4档；无数据 → 裸档位）
  ["k_level", "策略档", true,
   (v, r) => (v == null ? "—" : roundNoLabel(r.round_no) + "第" + v + "档")],
  // v8.2.0 补（Joel 2026-11-19）：触发线语义标签——k≥1 加仓线标「加仓」、
  // k=0 首仓线标「首仓」（策略下一次建仓价）。v8.2.3：再武装价恢复无条件
  // 显示（v8.2.0「策略零买入仓隐藏」规则随旧差值口径退役）。
  ["next_add_price_raw", "下次触发价", true,
   (v, r) => (v == null ? "—" : fmtPrice(v) + (r.line_kind === "add" ? " 加仓" : " 首仓"))],
  ["next_rearm_price_raw", "再武装价", true, fmtPrice],
  ["signal", "信号"],
  ["end_date", "数据截至"],
];

// 买卖信号 → 行标记（signal 列）+ 顶部提示条
const signalSet = computed(() => new Map((myData.value?.signals || []).map((s) => [s.ts_code, s])));
const displayRows = computed(() => (myData.value?.positions || []).map((p) => {
  const s = signalSet.value.get(p.ts_code);
  // v8.2.0 补：首档触线（再武装后新一轮 = 二次建仓）→ 标「建仓」而非「加仓」
  const tag = s ? (s.new_round ? (s.in_pos ? "🔔 二次建仓" : "🔔 建仓")
    : (s.action === "buy" ? "🔔 买入" : "🔔 加仓")) : "";
  return { ...p, signal: tag };
}));

// ---------------------------------------------------------------------------
// 顶部录入条（添加/更新）
// ---------------------------------------------------------------------------
const add = reactive({ code: "", shares: "", cost: "", since: "" });
const addMsg = ref("");
const addErr = ref("");
const CODE_RE = /^[a-z]{2}\.\d{6}$/;

// ---- 代码模糊搜索（下拉候选：代码数字 / 中文名 / 拼音首字母 / 全拼）----
const sug = ref([]);        // [{ts_code, name, industry}]
const sugOpen = ref(false);
const sugHi = ref(-1);      // 高亮候选（-1 = 无）
let sugTimer = null;
let sugSeq = 0;             // 竞态保护：只接受最后一次请求的结果

function codeSearchable(v) {
  const s = (v || "").trim();
  if (!s) return false;
  if (/^(?:sh|sz|bj)?\.?\d+$/i.test(s)) {   // 代码/纯数字：≥3 位才查（服务端同规则）
    return s.replace(/^[a-z]+\.?/i, "").length >= 3;
  }
  return true; // 中文 / 拼音：1 位起查
}

async function onCodeInput() {
  if (sugTimer) clearTimeout(sugTimer);
  const q = add.code.trim();
  if (!codeSearchable(q)) { closeSug(); return; }
  sugTimer = setTimeout(async () => {
    const seq = ++sugSeq;
    try {
      const d = await api("/api/stocks/search?q=" + encodeURIComponent(q));
      if (seq !== sugSeq) return; // 过期响应丢弃
      sug.value = d.results || [];
      sugHi.value = -1;
      sugOpen.value = sug.value.length > 0;
    } catch (e) {
      if (seq === sugSeq) { sug.value = []; sugOpen.value = false; }
      // 503 lake_locked / 网络错误 → 静默降级：仍可手动输入完整代码
    }
  }, 250);
}

function closeSug() {
  if (sugTimer) clearTimeout(sugTimer);
  sugSeq++;
  sug.value = []; sugOpen.value = false; sugHi.value = -1;
}

function pickSug(r) {
  add.code = r.ts_code;
  closeSug();
}

function onCodeKeydown(e) {
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    if (!sugOpen.value || !sug.value.length) return;
    e.preventDefault();
    const n = sug.value.length;
    sugHi.value = e.key === "ArrowDown" ? (sugHi.value + 1) % n : (sugHi.value - 1 + n) % n;
    return;
  }
  if (e.key === "Enter" && sugOpen.value) {
    // 下拉打开时 Enter = 确认高亮候选（无高亮取第一条），不触发提交
    e.preventDefault();
    pickSug(sugHi.value >= 0 ? sug.value[sugHi.value] : sug.value[0]);
    return;
  }
  if (e.key === "Escape") closeSug();
}

function onCodeKeyup(e) {
  if (e.key === "Enter" && sugOpen.value) return; // 已被 keydown 消费
  if (e.key === "Enter") addNew();
}

async function putPos(code, shares, cost, since) {
  return api("/api/my_positions", {
    method: "PUT",
    body: JSON.stringify({ ts_code: code, shares: Number(shares), cost_price: Number(cost),
                           since_date: since || "" }),
  });
}

const SINCE_RE = /^\d{4}-\d{2}-\d{2}$/;

async function addNew() {
  addErr.value = "";
  addMsg.value = "";
  const code = add.code.trim().toLowerCase();
  const shares = Number(add.shares);
  const cost = Number(add.cost);
  const since = (add.since || "").trim();
  if (!CODE_RE.test(code)) { addErr.value = "代码应为 sh.600000 / sz.000001 形式"; return; }
  if (!Number.isFinite(shares) || shares <= 0) { addErr.value = "股数应为正数"; return; }
  if (!Number.isFinite(cost) || cost < 0) { addErr.value = "成本价应为非负数"; return; }
  if (since && !SINCE_RE.test(since)) { addErr.value = "建仓日应为 YYYY-MM-DD"; return; }
  try {
    await putPos(code, shares, cost, since);
    addMsg.value = `已保存 ${code}`;
    add.code = ""; add.shares = ""; add.cost = ""; add.since = "";
    refresh();
  } catch (e) {
    addErr.value = e.message;
  }
}

// ---------------------------------------------------------------------------
// 行点击弹层（策略参考 + 建仓时间线 + 交易登记 + 交易历史 + 回测逐笔）
// 更新股数/成本统一走顶部「添加/更新持仓」录入条（Joel 2026-11-13：弹层不重复编辑）
// ---------------------------------------------------------------------------
const modalRow = ref(null);
// v8.2.0 补（Joel 2026-11-20 拍板：首仓线按双条件）：「first」行弹层展开
// 组成线——下一触发价 = min(① 价线=本轮高点×(1−回撤), ② 时机线=5 年
// ≤40% 分位翻线价)，后端已取较低覆写 next_add_price_raw；时机线可得时
// 两条组成线照显，「add」行（k≥1 无时机门）/ 时机线缺失 → 原标签。
const modalFieldRows = computed(() => {
  const r = modalRow.value;
  if (!r) return [];
  const rows = [
    ["ts_code", "代码"], ["in_pos", "策略持仓中"], ["strat_shares", "策略模拟股数"],
    ["since_date", "建仓日（顶部录入条填）"], ["strat_shares_since", "建仓日起策略股数"],
    ["round_no", "用户轮次"],
    ["theory_lots", "多轮理论(手)"], ["actual_lots", "实际(手)"], ["gap_lots", "差值(手)"],
    ["k_level", "当前档位 k"],
  ];
  if (r.line_kind === "first" && r.timing_line_raw != null) {
    rows.push(["first_price_line_raw", "① 价线（本轮高点×(1−回撤)）"]);
    rows.push(["timing_line_raw", "② 时机线（5 年≤40% 分位翻线价）"]);
    rows.push(["next_add_price_raw", "下一触发价（双条件·取较低）"]);
  } else {
    rows.push(["next_add_price_raw", "下一触发价（首仓/加仓）"]);
  }
  rows.push(["next_rearm_price_raw", "再武装触发价"]);
  rows.push(["last_price", "最新价"], ["end_date", "数据截至"]);
  return rows;
});
const saveMsg = ref("");
const saveErr = ref("");

// v8.2.0 补（Joel 2026-11-20 全局格式统一）：弹层字段按类型格式化
// （价格 2 位 / 股数手数整数；文本字段原样）
const D_PRICE_KEYS = new Set(["first_price_line_raw", "timing_line_raw", "next_add_price_raw",
  "next_rearm_price_raw", "last_price"]);
const D_INT_KEYS = new Set(["strat_shares", "strat_shares_since", "theory_lots",
  "actual_lots", "gap_lots", "k_level", "round_no"]);
function fmtField(k, v) {
  if (v === null || v === undefined || v === "") return "—";
  if (D_PRICE_KEYS.has(k)) return fmtPrice(v);
  if (D_INT_KEYS.has(k)) return fmtInt(v);
  return v;
}

function onRow(r) {
  modalRow.value = r;
  saveMsg.value = "";
  saveErr.value = "";
  // 买入登记预填：该股有活跃信号 → 建议股数（建议手数×100，v7.0.28 手数口径）；价格默认最新价
  const s = activeSignal(r.ts_code);
  buy.shares = s ? suggestShares(s) : "";
  buy.price = r.last_price != null ? String(r.last_price) : "";
  buy.date = "";
  buyMsg.value = "";
  buyErr.value = "";
  tradeErr.value = "";
  loadTrades(); // 实盘交易历史（v7.0.17）
}

async function deleteRow() {
  if (!modalRow.value) return;
  if (!window.confirm(`删除持仓 ${modalRow.value.ts_code}？`)) return;
  saveErr.value = "";
  saveMsg.value = "";
  try {
    await putPos(modalRow.value.ts_code, 0, modalRow.value.cost_price);
    modalRow.value = null;
    refresh();
  } catch (e) {
    saveErr.value = e.message;
  }
}

// ---------------------------------------------------------------------------
// 交易登记（v7.0.17，原「买入登记」v7.0.15）：实盘买入/卖出后在此登记，
// 正数 = 买入（整手）、负数 = 卖出。后端单次请求原子完成两件事：
// ① 更新当前持仓（买：加权成本 3 位小数，券商口径不含手续费；卖：减股数、
//    剩余均价不变、清仓自动移除）② 追加实盘交易历史（data/my_trades.json）。
// 刷新后策略档位（状态机按账本重播种）自动按新持仓重算。
// ---------------------------------------------------------------------------
const buy = reactive({ shares: "", price: "", date: "" });
const buyMsg = ref("");
const buyErr = ref("");
// 实盘交易历史（v7.0.17）：弹层内按当前行股票加载；删除仅移除记录不回改持仓
const tradeHist = ref([]);
const tradeErr = ref("");

function activeSignal(code) {
  return (myData.value?.signals || []).find((s) => s.ts_code === code) || null;
}

// 建议股数：信号建议手数 × lot_size（v7.0.28 手数口径，A 股整手）
function suggestShares(s) {
  if (!s || s.suggested_lots == null) return "";
  return String(s.suggested_lots * 100);
}

// 实时预览：买（正数）= 加权成本；卖（负数）= 减股数 + 已实现盈亏
const buyPreview = computed(() => {
  const r = modalRow.value;
  const ns = Number(buy.shares);
  const np = Number(buy.price);
  if (!r || !Number.isInteger(ns) || ns === 0 || !Number.isFinite(np) || np <= 0) return "";
  const oldShares = r.shares || 0;
  const oldCost = r.cost_price || 0;
  if (ns > 0) {
    const newShares = oldShares + ns;
    const newCost = (oldShares * oldCost + ns * np) / newShares;
    return `当前 ${fmtInt(oldShares)} 股 @ ${fmtPrice(oldCost)} ＋ 本次 ${fmtInt(ns)} 股 @ ${fmtPrice(np)}` +
      ` → ${fmtInt(newShares)} 股 @ ${fmtPrice(newCost)}（本次金额 ¥${fmtMoney(ns * np)}）`;
  }
  const nSell = -ns;
  if (nSell > oldShares) return `⚠ 卖出不能超过持仓：当前 ${oldShares} 股`;
  const newShares = oldShares - nSell;
  const pnl = (np - oldCost) * nSell;
  return `当前 ${oldShares} 股 @ ${oldCost} − 本次 ${nSell} 股 @ ${np}` +
    ` → ${newShares} 股 @ ${oldCost}（已实现盈亏 ${pnl >= 0 ? "+" : ""}${pnl.toFixed(2)}）`;
});

async function postTrade(code, shares, price, date) {
  return api("/api/my_trades", {
    method: "POST",
    body: JSON.stringify({
      ts_code: code, shares, price: Number(price), date: date || "",
      name: (modalRow.value && modalRow.value.name) || "",
    }),
  });
}

async function recordTrade() {
  buyErr.value = "";
  buyMsg.value = "";
  const r = modalRow.value;
  if (!r) return;
  const ns = Number(buy.shares);
  const np = Number(buy.price);
  const bd = (buy.date || "").trim();
  if (!Number.isInteger(ns) || ns === 0) { buyErr.value = "本次股数应为非零整数（正=买入，负=卖出）"; return; }
  if (ns > 0 && ns % 100 !== 0) { buyErr.value = "A 股买入按整手（100 股倍数）"; return; }
  if (ns < 0 && -ns > (r.shares || 0)) { buyErr.value = `卖出不能超过持仓：当前 ${r.shares || 0} 股`; return; }
  if (!Number.isFinite(np) || np <= 0) { buyErr.value = "本次价格应为正数"; return; }
  if (bd && !SINCE_RE.test(bd)) { buyErr.value = "交易日期应为 YYYY-MM-DD（留空=今天）"; return; }
  try {
    const res = await postTrade(r.ts_code, ns, np, bd);
    const pos = res.position;
    if (pos.removed) {
      buyMsg.value = `已登记卖出 ${-ns} 股 @ ${np}，清仓移除`;
    } else {
      buyMsg.value = ns > 0
        ? `已登记买入 ${fmtInt(ns)} 股 @ ${fmtPrice(np)} → ${fmtInt(pos.shares)} 股 @ ${fmtPrice(pos.cost_price)}`
        : `已登记卖出 ${fmtInt(-ns)} 股 @ ${fmtPrice(np)} → ${fmtInt(pos.shares)} 股 @ ${fmtPrice(pos.cost_price)}`;
      // 弹层内同步：账本快照字段（策略档位列刷新后重开弹层可见新值）
      Object.assign(r, { shares: pos.shares, cost_price: pos.cost_price });
    }
    buy.shares = "";
    buy.price = "";
    buy.date = "";
    saveMsg.value = "";
    refresh();
    loadTrades(); // 交易历史即时刷新（新记录排最前）
  } catch (e) {
    buyErr.value = e.message;
  }
}

// 实盘交易历史加载（弹层内，按当前行股票过滤；服务端最新在前）
async function loadTrades() {
  const code = modalRow.value?.ts_code;
  if (!code) { tradeHist.value = []; return; }
  try {
    const d = await api("/api/my_trades?ts_code=" + encodeURIComponent(code));
    tradeHist.value = d.trades || [];
  } catch { tradeHist.value = []; } // 加载失败不阻塞弹层（历史区显示空）
}

// 删除一条历史记录：仅移除日志，不回改当前持仓（持仓账本是唯一状态源）
async function deleteTrade(id) {
  if (!window.confirm("删除这条交易记录？（仅移除记录，不回改当前持仓）")) return;
  tradeErr.value = "";
  try {
    await api("/api/my_trades/" + encodeURIComponent(id), { method: "DELETE" });
    await loadTrades();
  } catch (e) { tradeErr.value = e.message; }
}

// 该股在最近一次回测中的买入历史（taskStore 缓存；无 → 提示去回测页跑一次）
const modalBuys = computed(() => {
  const code = modalRow.value?.ts_code;
  if (!code || !store.lastResults) return null;
  const rows = (store.lastResults.data.buys || []).filter((b) => b.ts_code === code);
  return { taskId: store.lastResults.taskId, rows };
});

const BUYS_COLS = [
  ["ts_code", "代码"], ["seq", "序", true], ["date", "日期"],
  ["price", "成交价", true, fmtPrice], ["amount", "金额", true, fmtMoney],
];

// ---------------------------------------------------------------------------
// 汇总 + 格式
// ---------------------------------------------------------------------------
const t = computed(() => myData.value?.totals || null);

function fmtW(v) {
  if (v === null || v === undefined) return "—";
  return "¥" + (v / 10000).toFixed(1) + "万";
}
// v8.2.0 补：本地 fmtPct 已并入 utils/fmt.js 单点（百分比 1 位小数，import 同名）
function pnlColor(v) {
  if (v === null || v === undefined) return "inherit";
  return v > 0 ? "var(--lake-up)" : v < 0 ? "var(--lake-down)" : "inherit";
}
</script>

<template>
  <section class="tab-panel active">
    <div class="my-toolbar">
      <h3>💼 我的持仓</h3>
      <span class="muted small">策略应用：当前策略（strategy.yaml）</span>
      <button class="ghost-btn" @click="refresh">🔄 刷新</button>
    </div>

    <!-- 录入条：添加/更新 -->
    <div class="card">
      <h3>添加 / 更新持仓</h3>
      <p class="muted small">
        录入你实际持有的股票（代码 + 股数 + 成本价）；<b>首次建仓时填建仓日</b>
        （之后自动锚定，无需再动），同一代码再次保存即更新持仓。
        策略参考列来自状态机模拟——「下次触发价」即该跌到的触发价位：
        首仓线按<b>双条件</b>判定（① 价破价线 且 ② 5 年分位≤40%），
        显示两条件线的较低者（标「首仓」，点行看两条组成线）；
        加仓线（标「加仓」）= 纯价格条件；
        v8.2.3：再武装价恢复无条件显示（旧「策略零买入仓隐藏」规则退役）。
      </p>
      <div class="add-bar">
        <div class="code-sug-wrap">
          <input v-model="add.code" class="inp" placeholder="代码/拼音/中文，如 600036、zgsh、长江电力"
                 @input="onCodeInput" @keydown="onCodeKeydown" @keyup="onCodeKeyup" @blur="closeSug" />
          <ul v-if="sugOpen" class="code-sug-list">
            <li v-for="(r, i) in sug" :key="r.ts_code" :class="{ hi: i === sugHi }"
                @mousedown.prevent="pickSug(r)">
              <span class="sug-code">{{ r.ts_code }}</span>
              <span class="sug-name">{{ r.name }}</span>
              <span class="muted small sug-ind">{{ r.industry }}</span>
            </li>
          </ul>
        </div>
        <input v-model="add.shares" class="inp num" placeholder="股数" @keyup.enter="addNew" />
        <input v-model="add.cost" class="inp num" placeholder="成本价" @keyup.enter="addNew" />
        <input v-model="add.since" class="inp num" type="date" title="建仓日（可选；首轮/轮次判定锚点，默认今天）"
               @keyup.enter="addNew" />
        <button class="primary-btn" @click="addNew">保存</button>
        <span v-if="addMsg" class="msg-ok">{{ addMsg }}</span>
        <span v-if="addErr" class="msg-err">{{ addErr }}</span>
      </div>
    </div>

    <!-- 买卖信号提示条（所选策略：现价 ≤ 下一触发价位；实时价判定，价源标注） -->
    <div v-if="myData && myData.signals && myData.signals.length" class="signal-banner">
      <div class="signal-title">
        🔔 信号提示（当前策略）：{{ myData.signals.length }} 只达到触发价位
        <span class="muted small">
          · 价源：{{ myData.price_source === "spot" ? `实时行情${myData.spot_time ? " " + myData.spot_time : ""}` : "最新收盘（T+1）" }}
        </span>
      </div>
      <div v-for="s in myData.signals" :key="s.ts_code" class="signal-row">
        <span class="signal-tag" :class="s.action === 'buy' ? 'tag-buy' : 'tag-add'">
          {{ s.new_round ? (s.in_pos ? "二次建仓" : "建仓") : (s.action === "buy" ? "买入·首仓" : "加仓") }}
        </span>
        <b>{{ s.name }} <span class="mono">{{ s.ts_code }}</span></b>
        <span>现价 {{ fmtPrice(s.price) }} ≤ 触发价 {{ fmtPrice(s.next_add_price) }}</span>
        <!-- v8.2.3：持仓行档位带用户相对轮次前缀（round_no null → 无前缀） -->
        <span>{{ s.new_round ? (s.in_pos
          ? `再武装·首仓（${roundNoLabel(s.round_no)}第 1 档）` : "首仓（第 1 档）")
          : (s.action === "buy" ? "首仓（第 1 档）"
             : `${roundNoLabel(s.round_no)}第 ${s.k_level} 档 → 第 ${s.next_level} 档`) }}</span>
        <span v-if="s.suggested_lots != null">
          建议{{ s.new_round ? (s.in_pos ? roundNoLabel(s.round_no) + "建仓" : "建仓")
            : (s.action === "buy" ? "买入" : "补至" + roundNoLabel(s.round_no) + "第 " + s.next_level + " 档") }}
          <b>{{ fmtInt(s.suggested_lots) }} 手</b>
          <span v-if="s.theory_lots != null" class="muted small">
            （多轮理论 {{ fmtInt(s.theory_lots) }} 手 / 现持 {{ fmtInt(s.actual_lots) }} 手）
          </span>
          <span v-if="s.price" class="muted small">≈ ¥{{ ((s.suggested_lots * 100 * s.price) / 10000).toFixed(1) }}万</span>
        </span>
      </div>
    </div>

    <!-- 我的持仓表 -->
    <div class="card">
      <h3>当前持仓（{{ myData?.n ?? 0 }} 只）</h3>
      <p v-if="myLoading && !myData" class="placeholder">加载中…</p>
      <p v-else-if="myError && !myData" class="msg-err">加载失败: {{ myError.message }}</p>
      <template v-else-if="myData">
        <div v-if="myData.lake_locked" class="lake-backfill">
          ⏳ 数据湖正被灌数进程占用——最新价/市值/策略参考暂不可用，当前只显示你的账本；灌数结束后刷新。
        </div>
        <p class="muted small" v-if="myData.n">
          数据截至 {{ myData.as_of || "—" }}（状态机） · 价源：{{ myData.price_source === "spot"
            ? `实时行情${myData.spot_time ? " " + myData.spot_time : ""}` : "最新收盘（T+1）" }}
          · 行点击看详情 / 建仓时间线 / 交易登记
        </p>
        <div v-if="t && myData.n" class="totals-bar">
          <span>总市值 <b>{{ fmtW(t.market_value) }}</b></span>
          <span>总成本 <b>{{ fmtW(t.cost) }}</b></span>
          <span>浮动盈亏 <b :style="{ color: pnlColor(t.pnl) }">
            {{ fmtW(t.pnl) }}（{{ fmtPct(t.pnl_pct) }}）</b></span>
        </div>
        <SimpleTable :rows="displayRows" :cols="MY_COLS"
                     empty="（尚未录入持仓——上方输入代码/股数/成本添加）" @row-click="onRow" />
      </template>
    </div>

    <!-- 策略信号中心（v7.0.19；v8.2.5 改名：当前=微信推送信号 / 潜力信号 / 历史） -->
    <div class="card">
      <h3>📡 策略信号（当前策略）</h3>
      <p class="muted small">
        股票池 ∪ 我的持仓（实时价覆盖 = 持仓 ∪ 湖最后收盘距触发线 ≤
        {{ sigData?.spot_preselect_pct ?? 10 }}%，其余用最新收盘）· 信号线 = 下一档触发价 ·
        「当前」= 信号区（action 非空），计数 **N/M**：N = 真正触发的个数
        （浅红行），M = 总行数：**浅红行 = 真正触发**（价格已破线 /
        盘中已触线回弹，微信推送信号 = 其中时机门全过的行）；**浅灰行 =
        即将触发**（价格距触发线 ≤5%，到线即推）·
        「潜力信号」（v8.2.5）满足任一即入段：① 价格距触发线 ≤
        {{ sigData?.approach_pct ?? 5 }}%（绝对距离，含已破线但时机未过——「距触发」列
        带符号：+ 线上方 / − 已破线）② 5年分位 ≤50%（含已过门的 ≤40%，
        「门过了等价格」也显示；见「时机」列）；与「当前」互斥（达推送条件
        的只展示在「当前」）·
        「下一档手数」= 触发价到线将买的手数（首仓/二次建仓 =
        首仓手数，加仓档 = 首仓 + 档×步长；不受时机门影响）
        · 非持仓股买入信号须过选股时机（5年分位≤40%，见「时机」列）
        · 历史 = 近 {{ sigData?.history_window_days ?? 90 }} 天**微信推送过的真实触发信号**
        （未推送的状态机回放记录不显示）
        · 10年回测 = 策略十年回放
        <span v-if="sigData?.price_source === 'spot'">
          · 价源：实时行情{{ sigData.spot_time ? " " + sigData.spot_time : "" }}</span>
        <span v-else-if="sigData">· 价源：最新收盘（T+1）</span>
        <span>· 交易时段 30s 自动刷新（非时段停轮询，手动刷新随时可用）</span>
      </p>
      <div v-if="sigData?.lake_locked" class="lake-backfill">
        ⏳ 数据湖正被灌数进程占用——策略参考暂不可用（仅列账本代码）；灌数结束后刷新。
      </div>
      <div class="sig-seg">
        <button :class="{ on: sigSel === 'active' }" @click="sigSel = 'active'">当前信号（{{ sigHitCount }}/{{ sigActive.length }}）</button>
        <button :class="{ on: sigSel === 'approaching' }" @click="sigSel = 'approaching'">潜力信号（{{ sigApproach.length }}）</button>
        <button :class="{ on: sigSel === 'history' }" @click="sigSel = 'history'">历史信号（{{ sigHistoryAll.length }}）</button>
      </div>
      <p v-if="sigLoading && !sigData" class="placeholder">加载中…</p>
      <p v-else-if="sigError && !sigData" class="msg-err">加载失败: {{ sigError.message }}</p>
      <template v-else-if="sigData">
        <SimpleTable v-if="sigSel !== 'history'" :rows="sigShown"
                     :cols="sigSel === 'active' ? SIG_ACTIVE_COLS : SIG_APPROACH_COLS"
                     :row-class="sigRowClass"
                     empty="（无）" />
        <template v-else>
          <SimpleTable :rows="sigShown" :cols="SIG_HISTORY_COLS" empty="（历史无触发）" />
          <p v-if="sigHistoryAll.length > SIG_HISTORY_CAP" class="muted small">
            仅显示最近 {{ SIG_HISTORY_CAP }} 条
          </p>
        </template>
      </template>
    </div>

    <!-- 行点击弹层：策略参考 + 编辑 + 交易登记 + 交易历史 + 回测逐笔 -->
    <Modal :open="!!modalRow" :title="(modalRow && modalRow.name) || '持仓详情'"
           @close="modalRow = null">
      <div v-if="modalRow">
        <div class="detail-grid">
          <div v-for="[k, label] in modalFieldRows" :key="k" class="d-row">
            <span class="d-key">{{ label }}</span>
            <span class="d-val">{{ fmtField(k, modalRow[k]) }}</span>
          </div>
        </div>
        <!-- v8.2.0 手动加仓时间线（自动记录，时间锚定计算依据） -->
        <div v-if="(modalRow.buys || []).length" class="buys-timeline">
          <h4>🕒 建仓时间线 <span class="muted small">（手动加仓自动记录 · 锚点 = 最早一笔 {{ modalRow.since_date }}）</span></h4>
          <table class="buys-tbl">
            <thead><tr><th>日期</th><th class="num">加仓(股)</th><th class="num">价格</th></tr></thead>
            <tbody>
              <tr v-for="(b, i) in modalRow.buys" :key="i">
                <td>{{ b.date }}</td>
                <td class="num">{{ fmtInt(b.shares) }}</td>
                <td class="num">{{ fmtPrice(b.price) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div class="edit-bar">
          <span class="muted small">更新股数/成本用顶部「添加/更新持仓」录入条（同一代码再次保存即更新）</span>
          <button class="ghost-btn" @click="deleteRow">删除持仓</button>
          <span v-if="saveMsg" class="msg-ok">{{ saveMsg }}</span>
          <span v-if="saveErr" class="msg-err">{{ saveErr }}</span>
        </div>

        <!-- 交易登记（v7.0.17）：实盘买入/卖出后登记 → 当前持仓自动更新 + 写入交易历史 -->
        <div class="buy-bar">
          <h4>📝 交易登记 <span class="muted small">（正数=买入 / 负数=卖出；登记后当前持仓自动更新并写入交易历史）</span></h4>
          <div class="buy-inputs">
            <label class="edit-field">
              <span>本次股数</span>
              <input v-model="buy.shares" class="inp num" placeholder="正=买（整手），负=卖" />
            </label>
            <label class="edit-field">
              <span>本次价格</span>
              <input v-model="buy.price" class="inp num" placeholder="元" />
            </label>
            <label class="edit-field">
              <span>交易日期</span>
              <input v-model="buy.date" class="inp num" type="date"
                     title="留空=默认今天；可手动填历史日期回填" />
            </label>
            <button class="primary-btn" @click="recordTrade">登记</button>
            <span v-if="buyMsg" class="msg-ok">{{ buyMsg }}</span>
            <span v-if="buyErr" class="msg-err">{{ buyErr }}</span>
          </div>
          <p v-if="buyPreview" class="muted small buy-preview">{{ buyPreview }}</p>
        </div>
        <!-- 交易历史（v7.0.17）：实盘登记产生的买卖记录，可删除（仅移除记录，不回改持仓） -->
        <div class="detail-buys">
          <h4>📜 交易历史 <span class="muted small">（{{ tradeHist.length }} 条 · 删除仅移除记录，不回改当前持仓）</span></h4>
          <table v-if="tradeHist.length" class="trade-hist">
            <thead>
              <tr><th>日期</th><th>方向</th><th class="num">股数</th><th class="num">价格</th><th class="num">金额</th><th class="num">已实现盈亏</th><th></th></tr>
            </thead>
            <tbody>
              <tr v-for="t in tradeHist" :key="t.id">
                <td>{{ t.date }}</td>
                <td><span :class="t.direction === 'buy' ? 'tag-buy' : 'tag-sell'">{{ t.direction === 'buy' ? '买' : '卖' }}</span></td>
                <td class="num">{{ fmtInt(t.shares) }}</td>
                <td class="num">{{ fmtPrice(t.price) }}</td>
                <td class="num">{{ fmtMoney(t.amount) }}</td>
                <td class="num" :style="t.realized_pnl == null ? '' : { color: pnlColor(t.realized_pnl) }">
                  {{ t.realized_pnl == null ? "—" : (t.realized_pnl >= 0 ? "+" : "") + fmtMoney(t.realized_pnl) }}
                </td>
                <td class="num"><button class="ghost-btn tiny" title="删除这条记录" @click="deleteTrade(t.id)">🗑</button></td>
              </tr>
            </tbody>
          </table>
          <p v-else class="muted small">暂无交易记录——在上方「交易登记」登记买入/卖出后出现。</p>
          <span v-if="tradeErr" class="msg-err">{{ tradeErr }}</span>
        </div>
        <!-- 回测逐笔（最近一次回测的模拟买入，与实盘交易历史分离） -->
        <div class="detail-buys">
          <h4>📊 回测逐笔 <span class="muted small">（最近回测：{{ modalBuys ? modalBuys.taskId : "无" }}）</span></h4>
          <template v-if="modalBuys">
            <SimpleTable v-if="modalBuys.rows.length" :rows="modalBuys.rows" :cols="BUYS_COLS" />
            <p v-else class="muted small">该标的在最近一次回测中无买入。</p>
          </template>
          <p v-else class="muted small">尚无回测结果——到「回测」页触发一次（含该标的）即可看逐笔历史。</p>
        </div>
      </div>
    </Modal>
  </section>
</template>
