/* A股四维选股 Web 控制台 —— vanilla JS + fetch，无框架 */
"use strict";

// ---------------------------------------------------------------------------
// 工具
// ---------------------------------------------------------------------------
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function el(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k === "html") n.innerHTML = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) n.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined) continue;
    n.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return n;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  let body = null;
  try { body = await res.json(); } catch { /* 非 JSON */ }
  if (!res.ok) {
    const detail = body && body.detail ? body.detail : res.statusText;
    const err = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    err.status = res.status;
    err.body = body;
    throw err;
  }
  return body;
}

function toast(msg, ok = true) {
  const gs = $("#global-status");
  gs.textContent = (ok ? "✓ " : "✗ ") + msg;
  gs.style.color = ok ? "#4ade80" : "#f87171";
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { gs.textContent = ""; }, 6000);
}

// ---------------------------------------------------------------------------
// Tab 切换
// ---------------------------------------------------------------------------
function initTabs() {
  $$(".tab").forEach((btn) =>
    btn.addEventListener("click", () => switchTab(btn.dataset.tab))
  );
}
function switchTab(name) {
  $$(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === "tab-" + name));
  if (name === "results") loadRuns();
  if (name === "strategy") loadStrategy();
}

// ---------------------------------------------------------------------------
// 结果页
// ---------------------------------------------------------------------------
let runsData = [];

async function loadRuns() {
  const list = $("#run-list");
  try {
    const data = await api("/api/runs");
    runsData = data.runs;
    list.innerHTML = "";
    if (!runsData.length) {
      list.append(el("li", { class: "muted" }, "暂无运行结果"));
      return;
    }
    for (const r of runsData) {
      const li = el(
        "li",
        { "data-date": r.date, onclick: () => selectRun(r.date, li) },
        el("span", { class: "r-date" }, r.date),
        el("span", { class: "r-meta" }, `入选 ${r.selected_count} · ${r.total_candidates} 候选`)
      );
      list.append(li);
    }
    // 默认选中最新
    if (!list.querySelector("li.active")) selectRun(runsData[0].date, list.firstChild);
  } catch (e) {
    list.innerHTML = "";
    list.append(el("li", { class: "msg-err" }, "加载失败: " + e.message));
  }
}

let currentRunDate = null;

async function selectRun(day, liEl) {
  $$("#run-list li").forEach((l) => l.classList.toggle("active", l === liEl));
  const box = $("#run-detail");
  box.innerHTML = '<p class="placeholder">加载中…</p>';
  currentRunDate = day;
  try {
    // 列表与详情端点统一用 ISO（YYYY-MM-DD）
    const d = await api("/api/runs/" + day);
    renderRunDetail(d, box);
  } catch (e) {
    box.innerHTML = "";
    box.append(el("p", { class: "msg-err" }, "加载失败: " + e.message));
  }
}

function renderRunDetail(d, box) {
  box.innerHTML = "";
  box.append(
    el("div", { class: "card" },
      el("h3", {}, `运行 ${d.date}`,
        el("span", { class: "muted small" }, ` · 生成于 ${d.generated_at}`)),
      renderFunnel(d.funnel)
    )
  );

  // 入选股票表
  box.append(
    el("div", { class: "card" },
      el("h3", {}, `最终入选（${d.selected.length} 只，按 CSV 顺序）`),
      renderDataTable(d.selected, "sel-table")
    )
  );

  // 全部技术面幸存者
  box.append(
    el("div", { class: "card" },
      el("h3", {}, `技术面幸存者全量数据（${d.survivors.length} 行，可搜索 / 点表头排序）`),
      renderDataTable(d.survivors, "surv-table")
    )
  );

  // 缺失名单 + 跳过行业组
  const notes = el("div", { class: "card" }, el("h3", {}, "缺失与异常"));
  if (d.missing_fundamental.length) {
    notes.append(el("p", { class: "small muted" }, `基本面数据缺失 ${d.missing_fundamental.length} 只（维度判不通过）：`));
    const tbl = el("div", { class: "tbl-wrap" });
    tbl.append(renderTableSimple([["代码", "名称", "缺失字段"]],
      d.missing_fundamental.map((m) => [m.code, m.name, m.missing])));
    notes.append(tbl);
  } else {
    notes.append(el("p", { class: "small muted" }, "无基本面数据缺失记录。"));
  }
  if (Object.keys(d.skipped_groups).length) {
    const items = Object.entries(d.skipped_groups)
      .sort((a, b) => b[1] - a[1])
      .map(([k, v]) => `${k}（${v}只）`);
    notes.append(el("p", { class: "small muted" },
      `行业组不足最小规模、跳过排名约束（${items.length} 个组）：`,
      el("span", { class: "small" }, items.join("、"))));
  }
  box.append(notes);

  // 报告原文
  const mdBox = el("div", { class: "card" },
    el("h3", {}, "报告 Markdown 原文"),
    d.report_md ? renderMarkdown(d.report_md) : el("p", { class: "muted" }, "无报告文件"));
  box.append(mdBox);
}

// ---- 漏斗条形图 ----
function renderFunnel(funnel) {
  const wrap = el("div", { class: "funnel-box" });
  if (!funnel.length) return wrap;
  // 找最大正数作为比例基准（L0）
  let max = 0, minNeg = 0;
  for (const f of funnel) {
    const c = f.count == null ? 0 : f.count;
    if (c > max) max = c;
    if (c < minNeg) minNeg = c;
  }
  const base = Math.max(max, 1);
  for (const f of funnel) {
    const c = f.count == null ? 0 : f.count;
    const pct = Math.max(2, Math.abs(c) / base * 100);
    wrap.append(
      el("div", {},
        el("div", { class: "funnel-row" },
          el("div", { class: "funnel-label", title: f.desc || f.label }, f.label),
          el("div", { class: "funnel-bar-track" },
            el("div", { class: "funnel-bar" + (c < 0 ? " neg" : ""), style: `width:${pct}%` })),
          el("div", { class: "funnel-val" }, f.count == null ? "—" : String(c))),
        f.desc ? el("div", { class: "funnel-desc" }, f.desc) : null
      )
    );
  }
  return wrap;
}

// ---- 通用数据表（搜索 + 排序 + 分页）----
const SURV_COLS = [
  ["code", "代码"], ["name", "名称"], ["industry", "行业"],
  ["close", "收盘价", true], ["dividend_yield_pct", "股息率%", true],
  ["roe_pct", "ROE%", true], ["industry_percentile", "行业百分位", true],
  ["window_return_pct", "250日收益%", true], ["annual_vol_pct", "年化波动%", true],
  ["yoy_net_profit_pct", "净利同比%", true], ["liability_pct", "负债率%", true],
  ["gross_margin_pct", "毛利率%", true],
  ["pass_technical", "技术"], ["pass_dividend", "股息"],
  ["pass_industry", "行业"], ["pass_fundamental", "基本面"], ["pass_all", "全过"],
];

function renderDataTable(rows, tableId) {
  const state = { q: "", sortKey: null, sortDir: 1, page: 0, pageSize: 25 };
  const wrap = el("div");

  const searchInput = el("input", { type: "search", placeholder: "搜索代码 / 名称 / 行业…" });
  const countSpan = el("span", { class: "count" });
  const toolbar = el("div", { class: "tbl-toolbar" }, searchInput, countSpan);
  const tblWrap = el("div", { class: "tbl-wrap" });
  const pager = el("div", { class: "pager" });
  wrap.append(toolbar, tblWrap, pager);

  function cellText(r, key) {
    if (key.startsWith("pass_")) return r[key] === "是" ? "✓" : "—";
    return r[key] ?? "";
  }
  function num(v) { const n = parseFloat(v); return isNaN(n) ? null : n; }

  function filtered() {
    let out = rows;
    if (state.q) {
      const q = state.q.toLowerCase();
      out = out.filter((r) =>
        Object.values(r).some((v) => String(v ?? "").toLowerCase().includes(q)));
    }
    if (state.sortKey) {
      const k = state.sortKey, dir = state.sortDir;
      out = [...out].sort((a, b) => {
        const na = num(a[k]), nb = num(b[k]);
        if (na !== null && nb !== null) return (na - nb) * dir;
        if (na === null && nb === null) return String(a[k] ?? "").localeCompare(String(b[k] ?? ""), "zh") * dir;
        // NOTE-1：空值（无数据的数值列）统一排到最后，升序/降序都不插中间
        return na === null ? 1 : -1;
      });
    }
    return out;
  }

  function render() {
    const data = filtered();
    const pages = Math.max(1, Math.ceil(data.length / state.pageSize));
    if (state.page >= pages) state.page = pages - 1;
    const slice = data.slice(state.page * state.pageSize, (state.page + 1) * state.pageSize);

    countSpan.textContent = `共 ${data.length} 行（原始 ${rows.length}）`;

    tblWrap.innerHTML = "";
    const t = el("table", { class: "data" });
    const thead = el("thead");
    const hrow = el("tr");
    for (const [key, label, isNum] of SURV_COLS) {
      const th = el("th", { class: isNum ? "num" : "" }, label);
      if (state.sortKey === key) {
        th.append(el("span", { class: "sort-arrow" }, state.sortDir > 0 ? "▲" : "▼"));
      }
      th.addEventListener("click", () => {
        if (state.sortKey === key) state.sortDir *= -1;
        else { state.sortKey = key; state.sortDir = 1; }
        render();
      });
      hrow.append(th);
    }
    thead.append(hrow);
    const tbody = el("tbody");
    for (const r of slice) {
      const tr = el("tr");
      for (const [key, , isNum] of SURV_COLS) {
        let v = cellText(r, key);
        const td = el("td", { class: isNum ? "num" : "" });
        if (key.startsWith("pass_")) {
          td.className += r[key] === "是" ? " tag-pass" : " tag-fail";
        }
        td.textContent = v;
        tr.append(td);
      }
      tbody.append(tr);
    }
    t.append(thead, tbody);
    tblWrap.append(t);

    pager.innerHTML = "";
    pager.append(
      el("button", { onclick: () => { state.page--; render(); }, disabled: state.page === 0 ? "disabled" : null }, "‹ 上一页"),
      el("span", {}, `第 ${state.page + 1} / ${pages} 页`),
      el("button", { onclick: () => { state.page++; render(); }, disabled: state.page >= pages - 1 ? "disabled" : null }, "下一页 ›")
    );
  }

  searchInput.addEventListener("input", (e) => {
    state.q = e.target.value.trim();
    state.page = 0;
    render();
  });
  render();
  return wrap;
}

function renderTableSimple(header, rows) {
  const t = el("table", { class: "data" });
  const tr = el("tr");
  header.forEach((h) => tr.append(el("th", {}, h)));
  t.append(el("thead", {}, tr));
  const tb = el("tbody");
  rows.forEach((r) => {
    const row = el("tr");
    r.forEach((c) => row.append(el("td", {}, c)));
    tb.append(row);
  });
  t.append(tb);
  return t;
}

// ---- 极简 markdown 渲染（先转义，再处理表格/标题/列表/加粗）----
function renderMarkdown(md) {
  const box = el("div", { class: "md-view" });
  const lines = md.split("\n");
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (/^\s*\|.*\|\s*$/.test(line)) {
      const tblLines = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) { tblLines.push(lines[i]); i++; }
      const rows = tblLines
        .filter((l) => !/^\s*\|[\s:|-]+\|\s*$/.test(l))
        .map((l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => inlineMd(c.trim())));
      if (rows.length) {
        const t = el("table");
        rows.forEach((r, ri) => {
          const tr = el("tr");
          r.forEach((c) => tr.append(ri === 0 ? el("th", { html: c }) : el("td", { html: c })));
          t.append(tr);
        });
        box.append(t);
      }
      continue;
    }
    if (/^###\s/.test(line)) { box.append(el("h4", { html: inlineMd(line.replace(/^###\s*/, "")) })); i++; continue; }
    if (/^##\s/.test(line)) { box.append(el("h3", { html: inlineMd(line.replace(/^##\s*/, "")) })); i++; continue; }
    if (/^#\s/.test(line)) { box.append(el("h2", { html: inlineMd(line.replace(/^#\s*/, "")) })); i++; continue; }
    if (/^\s*[-*]\s/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*[-*]\s/.test(lines[i])) { items.push(inlineMd(lines[i].replace(/^\s*[-*]\s*/, ""))); i++; }
      box.append(el("ul", {}, ...items.map((x) => el("li", { html: x }))));
      continue;
    }
    if (/^\s*>/.test(line)) { box.append(el("blockquote", { html: inlineMd(line.replace(/^\s*>\s?/, "")), style: "color:#6b7487;border-left:3px solid #e3e8f0;margin:6px 0;padding-left:10px" })); i++; continue; }
    if (line.trim() === "") { i++; continue; }
    box.append(el("p", { html: inlineMd(line) }));
    i++;
  }
  return box;
}
function inlineMd(s) {
  let h = esc(s);
  h = h.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
  h = h.replace(/`(.+?)`/g, "<code>$1</code>");
  return h;
}

// ---------------------------------------------------------------------------
// 策略页
// ---------------------------------------------------------------------------
const SECTION_META = {
  technical: { title: "技术面", icon: "📉" },
  dividend: { title: "股息率", icon: "💰" },
  industry: { title: "行业排名", icon: "🏭" },
  fundamental: { title: "基本面", icon: "📊" },
  universe: { title: "股票池", icon: "🗂️" },
  data: { title: "数据获取", icon: "⚙️" },
  crosscheck: { title: "交叉验证", icon: "🔍" },
};
// 字段说明（只描述含义，不含阈值；数值一律来自 /api/strategy）
const FIELD_DESC = {
  "technical.ma_period": "收盘价须高于 MA{n}（后复权日K），均线周期（交易日）",
  "technical.return_window_days": "区间收益率回看窗口（交易日）",
  "technical.min_return_pct": "近 N 日区间收益率下限（%，含边界）",
  "technical.max_return_pct": "近 N 日区间收益率上限（%，含边界）",
  "technical.max_annual_volatility_pct": "年化波动率上限（%，不含边界）",
  "dividend.window_days": "股息率统计窗口（日历天）：[运行日-N, 运行日] 内已除权分红",
  "dividend.min_yield_pct": "股息率下限（%，含边界）= 窗口分红合计 ÷ 不复权收盘价",
  "industry.rank_by": "组内排名依据（目前仅支持最近披露报告期 ROE：roeAvg）",
  "industry.top_pct": "保留行业内前 N%（按 ROE 降序）",
  "industry.min_group_size": "行业组不足该数量 → 跳过排名约束并在报告注明",
  "fundamental.roe_min_pct": "ROE 下限（%，报告期累计口径、未年化，含边界）",
  "fundamental.net_profit_yoy_field": "净利同比字段：YOYPNI(归母) / YOYNI(净利润)，须 > 0",
  "fundamental.liability_max_pct": "资产负债率上限（%，含边界）",
  "fundamental.gross_margin_min_pct": "毛利率下限（%，不含边界）；金融业该字段为空 → 落缺失名单",
  "fundamental.probe_quarters_back": "「最近披露报告期」从当前季度最多回退探测的季数",
  "universe.a_share_prefixes": "沪深A股代码前缀（排除指数/ETF/B股），逗号分隔如 sh.60,sh.68,sz.00,sz.30",
  "universe.listing_min_trading_days": "上市满 N 个交易日（窗口内K线行数判断）",
  "universe.st_name_keyword": "名称辅助标记（剔除以日K isST=1 为准）",
  "data.kline_calendar_days_back": "后复权窗口K线回溯日历天（需覆盖均线/收益窗口交易日+节假日余量）",
  "data.retry_max_attempts": "BaoStock 单次查询失败重试次数（指数退避）",
  "data.cache_dir": "本地缓存目录（相对路径基于项目根）",
  "crosscheck.enabled": "启用腾讯 qt.gtimg.cn 交叉验证（不进主计算路径）",
  "crosscheck.sample_size": "从最终入选股中抽样 N 只验证收盘价",
  "crosscheck.price_tolerance_pct": "收盘价偏差容忍度（%）",
  "crosscheck.batch_size": "腾讯接口单批请求股票数",
};
// 字段类型（结构性元数据，非业务阈值）：int/float/bool/str/list/enum
const FIELD_TYPE = {
  ma_period: "int", return_window_days: "int", min_return_pct: "float",
  max_return_pct: "float", max_annual_volatility_pct: "float",
  window_days: "int", min_yield_pct: "float",
  rank_by: "enum", top_pct: "float", min_group_size: "int",
  roe_min_pct: "float", net_profit_yoy_field: "enum2", liability_max_pct: "float",
  gross_margin_min_pct: "float", probe_quarters_back: "int",
  a_share_prefixes: "list", listing_min_trading_days: "int", st_name_keyword: "str",
  kline_calendar_days_back: "int", retry_max_attempts: "int", cache_dir: "str",
  enabled: "bool", sample_size: "int", price_tolerance_pct: "float", batch_size: "int",
};

let strategyJson = null;
let strategyEditing = false;

// D-W02：工具栏按钮状态机的唯一入口。所有进入/退出编辑态的路径（点编辑、保存成功、
// 取消、加载失败回退）都必须经过它，保证三个按钮的 hidden class 与 strategyEditing
// 标志始终一致——此前 save/cancel 分支各自直接改标志、漏了按钮 class，导致编辑态卡死。
function setStrategyEditing(on) {
  strategyEditing = !!on;
  $("#btn-strategy-edit").classList.toggle("hidden", on);
  $("#btn-strategy-save").classList.toggle("hidden", !on);
  $("#btn-strategy-cancel").classList.toggle("hidden", !on);
}

async function loadStrategy() {
  const cards = $("#strategy-cards");
  cards.innerHTML = "";
  try {
    const d = await api("/api/strategy");
    strategyJson = d.json;
    renderStrategyCards(cards, strategyJson, false);
    setStrategyEditing(false); // D-W02：加载成功后统一回到只读态（幂等）
    $("#strategy-meta").textContent = `已加载 config/strategy.yaml · ${Object.keys(strategyJson).length} 个配置段`;
  } catch (e) {
    cards.append(el("p", { class: "msg-err" }, "加载策略失败: " + e.message));
    setStrategyEditing(false); // D-W02：加载失败也不让工具栏残留幽灵编辑态
  }
}

function renderStrategyCards(cards, json, editing) {
  for (const [section, fields] of Object.entries(json)) {
    if (!fields || typeof fields !== "object") continue;
    const meta = SECTION_META[section] || { title: section, icon: "🔧" };
    const card = el("div", { class: "s-card" }, el("h4", {}, el("span", { class: "s-icon" }, meta.icon), meta.title));
    for (const [field, value] of Object.entries(fields)) {
      const desc = FIELD_DESC[`${section}.${field}`] || "";
      const f = el("div", { class: "s-field" },
        el("div", { class: "s-field-head" },
          el("span", { class: "s-key" }, field),
          editing ? null : el("span", { class: "s-val" }, displayValue(field, value))));
      if (editing) f.append(editControl(section, field, value));
      else if (desc) f.append(el("div", { class: "s-desc" }, desc));
      card.append(f);
    }
    cards.append(card);
  }
}

function displayValue(field, v) {
  const t = FIELD_TYPE[field];
  if (t === "list") return v.join(", ");
  if (t === "bool") return v ? "true" : "false";
  return String(v);
}

function editControl(section, field, value) {
  const t = FIELD_TYPE[field] || "str";
  let input;
  if (t === "int" || t === "float") {
    input = el("input", { type: "number", step: t === "int" ? "1" : "any", value: String(value) });
  } else if (t === "bool") {
    input = el("select", {},
      el("option", { value: "true", selected: value ? "selected" : null }, "true"),
      el("option", { value: "false", selected: value ? null : "selected" }, "false"));
  } else if (t === "enum") {
    input = el("select", {}, el("option", { value: "roeAvg", selected: "selected" }, "roeAvg（最近披露报告期 ROE）"));
  } else if (t === "enum2") {
    input = el("select", {},
      el("option", { value: "YOYPNI", selected: value === "YOYPNI" ? "selected" : null }, "YOYPNI（归母净利同比）"),
      el("option", { value: "YOYNI", selected: value === "YOYNI" ? "selected" : null }, "YOYNI（净利润同比）"));
  } else if (t === "list") {
    input = el("input", { type: "text", value: Array.isArray(value) ? value.join(", ") : String(value), placeholder: "sh.60, sh.68, sz.00, sz.30" });
  } else {
    input = el("input", { type: "text", value: String(value) });
  }
  input.dataset.section = section;
  input.dataset.field = field;
  return input;
}

function initStrategyButtons() {
  $("#btn-strategy-edit").addEventListener("click", () => {
    if (!strategyJson) return;
    renderStrategyCards($("#strategy-cards"), strategyJson, true);
    setStrategyEditing(true); // D-W02：唯一入口，按钮 class 与标志同步翻转
    setStrategyMsg("", null);
  });
  $("#btn-strategy-cancel").addEventListener("click", () => {
    setStrategyEditing(false); // D-W02：先恢复工具栏，再重新拉取只读视图
    loadStrategy();
  });
  $("#btn-strategy-save").addEventListener("click", saveStrategy);
}

function setStrategyMsg(text, kind) {
  const box = $("#strategy-msg");
  box.innerHTML = "";
  if (!text) return;
  box.append(el("div", { class: kind === "err" ? "msg-err" : "msg-ok" }, text));
}

async function saveStrategy() {
  // 以当前 API 返回的 JSON 为底，覆盖编辑值 → 保证所有 key 齐全
  const payload = JSON.parse(JSON.stringify(strategyJson));
  let bad = false;
  for (const input of $$(".s-field input, .s-field select")) {
    const sec = input.dataset.section, field = input.dataset.field;
    const t = FIELD_TYPE[field] || "str";
    let v;
    if (t === "int") {
      v = Number.isInteger(Number(input.value)) ? parseInt(input.value, 10) : NaN;
      if (isNaN(v)) { setStrategyMsg(`✗ ${sec}.${field} 必须是整数`, "err"); bad = true; continue; }
    } else if (t === "float") {
      v = parseFloat(input.value);
      if (isNaN(v)) { setStrategyMsg(`✗ ${sec}.${field} 必须是数值`, "err"); bad = true; continue; }
    } else if (t === "bool") {
      v = input.value === "true";
    } else if (t === "list") {
      v = input.value.split(",").map((s) => s.trim()).filter(Boolean);
      if (!v.length) { setStrategyMsg(`✗ ${sec}.${field} 不能为空`, "err"); bad = true; continue; }
    } else {
      v = input.value;
    }
    payload[sec][field] = v;
  }
  if (bad) return;

  const btn = $("#btn-strategy-save");
  btn.disabled = true;
  try {
    const res = await api("/api/strategy", { method: "PUT", body: JSON.stringify(payload) });
    setStrategyMsg(`✓ 策略已保存（备份: ${res.backup.split("/").pop()}）`, "ok");
    // D-W02：保存成功 → 经唯一入口退出编辑态（按钮 class 同步恢复），再刷新只读视图
    setStrategyEditing(false);
    toast("策略已更新");
    setTimeout(loadStrategy, 600);
  } catch (e) {
    // 400 拒绝：保持编辑态，用户可改正后重试（save.disabled 由 finally 恢复）
    const errs = e.body && e.body.detail && e.body.detail.errors ? e.body.detail.errors.join("\n") : e.message;
    setStrategyMsg("✗ 保存被拒绝（400）：\n" + errs, "err");
    toast("策略保存失败", false);
  } finally {
    btn.disabled = false;
  }
}

// ---------------------------------------------------------------------------
// 运行页
// ---------------------------------------------------------------------------
let pollTimer = null;

function initRunPage() {
  const d = new Date();
  $("#run-date").value = d.toISOString().slice(0, 10);
  $("#btn-run-start").addEventListener("click", startRun);
}

async function startRun() {
  const dateVal = $("#run-date").value || new Date().toISOString().slice(0, 10);
  const btn = $("#btn-run-start");
  btn.disabled = true;
  try {
    const res = await api("/api/runs", { method: "POST", body: JSON.stringify({ date: dateVal }) });
    showProgress(res.task_id, "running", `任务 ${res.task_id} · PID ${res.pid} · 日期 ${res.date}`);
    pollStatus(res.task_id);
  } catch (e) {
    if (e.status === 409) toast("已有运行任务在进行中，请稍候（可下方查看状态）", false);
    else toast("触发失败: " + e.message, false);
    btn.disabled = false;
  }
}

function showProgress(taskId, state, meta) {
  const box = $("#run-progress");
  box.classList.remove("hidden");
  const badge = $("#task-state-badge");
  badge.textContent = state === "running" ? "运行中…" : state === "done" ? "已完成" : "失败";
  badge.className = "badge " + state;
  if (meta) $("#task-meta").textContent = meta;
}

function pollStatus(taskId) {
  clearInterval(pollTimer);
  const tick = async () => {
    try {
      const s = await api(`/api/runs/${taskId}/status`);
      showProgress(taskId, s.status,
        `任务 ${taskId}${s.date ? " · 日期 " + s.date : ""} · ${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`);
      $("#log-tail").textContent = (s.log_tail || []).join("\n") || "（暂无日志）";
      const lt = $("#log-tail");
      lt.scrollTop = lt.scrollHeight;
      if (s.status === "done") {
        clearInterval(pollTimer);
        $("#btn-run-start").disabled = false;
        toast("筛选完成！结果页已刷新");
        switchTab("results");
      } else if (s.status === "failed") {
        clearInterval(pollTimer);
        $("#btn-run-start").disabled = false;
        toast("运行失败，请查看日志", false);
      }
    } catch (e) {
      // 404（服务重启且任务未知）→ 停止轮询
      if (e.status === 404) {
        clearInterval(pollTimer);
        $("#btn-run-start").disabled = false;
      }
    }
  };
  tick();
  pollTimer = setInterval(tick, 3000);
}

// ---------------------------------------------------------------------------
// 启动
// ---------------------------------------------------------------------------
document.addEventListener("DOMContentLoaded", () => {
  initTabs();
  initStrategyButtons();
  initRunPage();
  $("#btn-refresh-runs").addEventListener("click", loadRuns);
  loadRuns();
});
