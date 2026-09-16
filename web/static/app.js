/* A股四维选股 Web 控制台 —— vanilla JS + fetch，无框架（v2：KPI卡/badge/个股弹框ECharts/SSE实时日志） */
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

function fmtNum(v, nd = 2) {
  if (v === null || v === undefined || v === "" || (typeof v === "number" && isNaN(v))) return "—";
  const n = Number(v);
  if (isNaN(n)) return String(v);
  return n.toFixed(nd);
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
  if (name === "backtest") loadBacktest();
  if (name === "lake") onLakeTab();
  // v6.0.9：同步控制按钮态 3s 轮询——仅数据湖 tab 激活期间运行（离开即停，不空转）
  if (name === "lake") startLakeSyncPoll(); else stopLakeSyncPoll();
}

// ---------------------------------------------------------------------------
// 回测页（v3：/api/backtest → ECharts 净值曲线 + 基准叠加；不展开 UI，能看图即可）
// ---------------------------------------------------------------------------
let btChart = null;
async function loadBacktest() {
  const box = $("#bt-chart");
  const meta = $("#bt-metrics");
  if (!box) return;
  let d;
  try {
    d = await api("/api/backtest");
  } catch (e) {
    if (btChart) { try { btChart.dispose(); } catch { /* ignore */ } btChart = null; }
    box.innerHTML = "";
    meta.textContent = "回测产物不存在：" + e.message;
    return;
  }
  const m = d.metrics || {};
  const f = m.full || {};
  const s1 = (m.slices || {})["1y"] || {};
  meta.textContent =
    `窗口 ${m.window ? m.window.start + " ~ " + m.window.end : "—"}（${m.window ? m.window.n_days : 0} 交易日）· ` +
    `全期年化 ${fmtNum(f.annual_return_pct, 2)}% / 夏普 ${fmtNum(f.sharpe, 2)} / 最大回撤 ${fmtNum(f.max_drawdown_pct, 2)}% · ` +
    `近1y 年化 ${fmtNum(s1.annual_return_pct, 2)}%`;

  try {
    const echarts = await loadEcharts();
    if (!btChart) btChart = echarts.init(box);
    const dates = d.equity_curve.map((r) => r.date);
    const series = [{ name: "策略", type: "line", showSymbol: false, data: d.equity_curve.map((r) => r.strategy), lineStyle: { width: 2 } }];
    for (const b of d.benchmarks || []) {
      series.push({ name: b, type: "line", showSymbol: false, data: d.equity_curve.map((r) => r[b]), lineStyle: { width: 1, type: "dashed" } });
    }
    btChart.setOption({
      tooltip: { trigger: "axis" },
      legend: { data: series.map((s) => s.name) },
      grid: { left: 50, right: 20, top: 40, bottom: 60 },
      xAxis: { type: "category", data: dates },
      yAxis: { type: "value", scale: true },
      dataZoom: [{ type: "inside" }, { type: "slider", height: 18 }],
      series,
    }, true);
    btChart.resize();
  } catch (e) {
    meta.textContent = "图表渲染失败：" + e.message;
  }
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
      const isFailed = r.status === "failed";
      // 失败运行：红色"失败"badge + 错误摘要（绝不与"入选 0"的正常空结果混同）
      let meta;
      if (isFailed) {
        meta = `✗ 运行失败`;
      } else {
        meta = `入选 ${r.selected_count} · ${r.total_candidates} 候选`;
      }
      const kpiMeta = !isFailed && (r.avg_ttm_yield_pct != null || r.avg_roe_pct != null)
        ? ` · TTM息率 ${fmtNum(r.avg_ttm_yield_pct, 2)}% · ROE ${fmtNum(r.avg_roe_pct, 1)}%` : "";
      const children = [
        el("span", { class: "r-date" }, r.date),
      ];
      if (isFailed) {
        children.push(el("span", { class: "run-badge run-badge-failed" }, "失败"));
      }
      children.push(el("span", { class: "r-meta" + (isFailed ? " r-meta-failed" : "") }, meta + kpiMeta));
      const li = el("li", { "data-date": r.date, onclick: () => selectRun(r.date, li) }, ...children);
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

// ---- v2 KPI 卡片（数据来自 parse_report 的「一、KPI 概览」）----
function renderKpiCards(kpi) {
  const wrap = el("div", { class: "kpi-cards" });
  if (!kpi) return wrap;
  const items = [
    ["入选数（Top N）", kpi.selected == null ? "—" : String(kpi.selected), "入选股票数量"],
    ["平均TTM股息率", kpi.avg_ttm_yield_pct == null ? "—" : fmtNum(kpi.avg_ttm_yield_pct, 2) + "%", "入选股 TTM 滚动股息率均值"],
    ["平均ROE", kpi.avg_roe_pct == null ? "—" : fmtNum(kpi.avg_roe_pct, 1) + "%", "入选股最近年报 ROE 均值"],
    ["行业集中度", kpi.industry_concentration || "—", "Top1 行业占比 / HHI×100"],
  ];
  for (const [title, val, desc] of items) {
    wrap.append(el("div", { class: "kpi-card" },
      el("div", { class: "kpi-val" }, val),
      el("div", { class: "kpi-title" }, title),
      el("div", { class: "kpi-desc" }, desc)));
  }
  return wrap;
}

function renderRunDetail(d, box) {
  box.innerHTML = "";

  // 失败运行（生产路径失败守卫）：顶部红色横幅 + 错误摘要，不渲染 KPI/漏斗/榜单。
  // 与合法"0 只入选"严格区分——那种 status=ok，正常走下面的空结果渲染。
  if (d.status === "failed") {
    box.append(
      el("div", { class: "card run-failed-card" },
        el("h3", {}, el("span", { class: "run-badge run-badge-failed" }, "运行失败"),
          ` · ${d.date}`),
        el("p", { class: "muted small" }, `该日筛选因数据源级失败中止，未产出结果（不是"0 只入选"）。`),
        el("pre", { class: "msg-err" }, (d.error_type ? d.error_type + ": " : "") + (d.error || "未知错误")),
        el("p", { class: "muted small" }, `失败时间: ${d.generated_at || "—"} · 主数据源 BaoStock（封禁/降级/空股票池等）`)
      )
    );
    return;
  }

  // v5.2 Phase 1：数据源健康度 badge（Q5 双通道之 Web 侧）——**仅异常时显示**；
  // 正常日/旧运行 data_health=null → 不渲染（零视觉回归）。
  if (d.data_health && d.data_health.has_anomaly) {
    const dh = d.data_health;
    box.append(
      el("div", { class: "card health-anomaly-card" },
        el("h3", {}, el("span", { class: "run-badge run-badge-failed" }, "数据源异常"),
          ` · ${d.date}`),
        ...dh.anomalies.map((a) => el("p", { class: "muted small" }, "⚠️ " + a)),
        el("p", { class: "muted small" }, "详见报告「数据源健康度」段（report_*.md）")
      )
    );
  }

  // KPI 卡片（v2 报告才有；旧运行 kpi 全空 → 不显示）
  if (d.kpi && (d.kpi.selected != null || d.kpi.avg_ttm_yield_pct != null)) {
    box.append(renderKpiCards(d.kpi));
  }

  box.append(
    el("div", { class: "card" },
      el("h3", {}, `运行 ${d.date}`,
        el("span", { class: "muted small" }, ` · 生成于 ${d.generated_at}`)),
      renderFunnel(d.funnel)
    )
  );

  // 入选股票表（v2：top_n_selected=1；legacy：pass_all=是）
  const selRows = (d.selected && d.selected.length) ? d.selected
    : d.survivors.filter((r) => String(r.top_n_selected ?? "").trim() === "1");
  box.append(
    el("div", { class: "card" },
      el("h3", {}, `最终入选（${selRows.length} 只，按综合得分/CSV 顺序）`),
      renderDataTable(selRows, "sel-table", d)
    )
  );

  // 全量打分候选表
  box.append(
    el("div", { class: "card" },
      el("h3", {}, `全部候选（${d.survivors.length} 行，可搜索 / 点表头排序；点行查看个股雷达图+K线）`),
      renderDataTable(d.survivors, "surv-table", d)
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

// ---- v2 badge（阈值全部来自 /api/runs/{day} 的 badges，源自 config/strategy.yaml）----
function rowBadges(r, badges) {
  const out = [];
  if (!badges) return out;
  const n = (v) => { const x = parseFloat(v); return isNaN(x) ? null : x; };
  if (n(r.ttm_dividend_yield_pct) != null &&
      n(r.ttm_dividend_yield_pct) >= badges.high_dividend_pct) {
    out.push(el("span", { class: "bdg bdg-div" }, "高股息"));
  }
  if (n(r.industry_roe_rank_pct) != null &&
      n(r.industry_roe_rank_pct) <= badges.industry_top_pct) {
    out.push(el("span", { class: "bdg bdg-ind" }, `行业Top${Math.round(badges.industry_top_pct)}%`));
  }
  if (String(r.ma_bullish ?? "").trim() === "1") {
    out.push(el("span", { class: "bdg bdg-ma" }, "MA多头"));
  }
  const fs = n(r.piotroski_fscore);
  if (fs != null && fs >= badges.fscore_min) {
    out.push(el("span", { class: "bdg bdg-fs" }, `F≥${badges.fscore_min}`));
  }
  return out;
}

// ---- 通用数据表（搜索 + 排序 + 分页 + v2 badge + 行点击弹框）----
const SURV_COLS = [
  ["code", "代码"], ["name", "名称"], ["industry", "行业"],
  ["close", "收盘", true],
  ["ma_bullish", "MA多头", true], ["window_return_pct", "250日收益%", true],
  ["annual_vol_pct", "年化波动%", true], ["rsi14", "RSI14", true],
  ["macd_golden_cross", "MACD金叉", true],
  ["ttm_dividend_yield_pct", "TTM息率%", true], ["payout_ratio_pct", "支付率%", true],
  ["industry_roe_rank_pct", "行业ROE分位", true], ["industry_yoy_pni_rank_pct", "行业YOY分位", true],
  ["roe_pct", "ROE%", true], ["roe_3y_mean_pct", "ROE3年均值%", true],
  ["liability_pct", "负债率%", true], ["gross_margin_pct", "毛利率%", true],
  ["piotroski_fscore", "F-Score", true],
  ["z_technical", "z技术", true], ["z_dividend", "z股息", true],
  ["z_industry", "z行业", true], ["z_fundamental", "z基本面", true],
  ["score_technical", "分技术", true], ["score_dividend", "分股息", true],
  ["score_industry", "分行业", true], ["score_fundamental", "分基本面", true],
  ["total_score", "综合得分", true], ["rank", "排名", true],
  ["top_n_selected", "入选", true],
  // legacy 兼容列（v1 运行可见；v2 行中为空）
  ["dividend_yield_pct", "息率%(旧)", true], ["yoy_net_profit_pct", "净利同比%", true],
  ["industry_percentile", "行业百分位(旧)", true], ["pass_all", "全过"],
];

function renderDataTable(rows, tableId, runDetail) {
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
    if (key === "top_n_selected") return String(r[key] ?? "").trim() === "1" ? "✓" : "—";
    if (key === "ma_bullish" || key === "macd_golden_cross") {
      const v = String(r[key] ?? "").trim();
      return v === "" ? "—" : (v === "1" ? "✓" : "✗");
    }
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
    // badge 列（仅表头，无排序）
    hrow.append(el("th", {}, "Badge"));
    thead.append(hrow);
    const tbody = el("tbody");
    for (const r of slice) {
      const tr = el("tr", { class: "clickable" });
      for (const [key, , isNum] of SURV_COLS) {
        let v = cellText(r, key);
        const td = el("td", { class: isNum ? "num" : "" });
        if (key.startsWith("pass_")) {
          td.className += r[key] === "是" ? " tag-pass" : " tag-fail";
        }
        td.textContent = v;
        tr.append(td);
      }
      const bdTd = el("td", { class: "bdg-cell" });
      for (const b of rowBadges(r, runDetail && runDetail.badges)) bdTd.append(b);
      tr.append(bdTd);
      // 行点击 → 个股弹框（雷达图 + K线）
      const code = r.code;
      if (code) {
        tr.addEventListener("click", () => openStockModal(code, runDetail && runDetail.date));
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
// 个股弹框（ECharts 懒加载：首次打开才注入 /static/vendor/echarts.min.js）
// ---------------------------------------------------------------------------
let echartsReady = null;
function loadEcharts() {
  if (!echartsReady) {
    echartsReady = new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = "/static/vendor/echarts.min.js";
      s.onload = () => resolve(window.echarts);
      s.onerror = () => { echartsReady = null; reject(new Error("ECharts 加载失败（本地 vendor）")); };
      document.head.append(s);
    });
  }
  return echartsReady;
}

let modalCharts = []; // 打开中的 ECharts 实例（关闭时 dispose，防内存泄漏）

function closeStockModal() {
  $("#stock-modal").classList.add("hidden");
  for (const c of modalCharts) { try { c.dispose(); } catch { /* ignore */ } }
  modalCharts = [];
}

async function openStockModal(code, runDay) {
  const modal = $("#stock-modal");
  const body = $("#stock-modal-body");
  modal.classList.remove("hidden");
  $("#stock-modal-title").textContent = `${code} · 个股明细`;
  body.innerHTML = '<p class="placeholder">加载本地缓存数据…</p>';
  for (const c of modalCharts) { try { c.dispose(); } catch { /* ignore */ } }
  modalCharts = [];

  if (!runDay) {
    body.innerHTML = "";
    body.append(el("p", { class: "msg-err" }, "未选择运行日期，无法定位结果行。"));
    return;
  }

  let d;
  try {
    d = await api(`/api/stocks/${code}/detail?run_day=${encodeURIComponent(runDay)}`);
  } catch (e) {
    body.innerHTML = "";
    body.append(el("p", { class: "msg-err" }, "加载失败: " + e.message));
    return;
  }

  body.innerHTML = "";
  if (d.name || d.industry) {
    body.append(el("p", { class: "small muted" },
      `${d.name || ""} · ${d.industry || "无行业"} · 运行日 ${runDay}`));
  }

  // ---- 因子/得分明细表 ----
  const fKeys = Object.keys(d.factors || {});
  if (fKeys.length) {
    body.append(el("h4", {}, "四维原始因子值"));
    const rows = [];
    for (const k of fKeys) rows.push([k, d.factors[k] == null ? "—" : fmtNum(d.factors[k], 3)]);
    const sKeys = Object.keys(d.scores || {});
    for (const k of sKeys) rows.push([k, d.scores[k] == null ? "—" : fmtNum(d.scores[k], 4)]);
    body.append(el("div", { class: "tbl-wrap" }, renderTableSimple(["字段", "值"], rows)));
  }

  // ---- ECharts：雷达图 + K线趋势图（懒加载 vendor）----
  try {
    const echarts = await loadEcharts();

    // 雷达图：四维得分（score_*；缺失维度 → 0）
    const dims = [
      ["technical", "技术面"], ["dividend", "股息"],
      ["industry", "行业"], ["fundamental", "基本面"],
    ];
    const radarEl = el("div", { class: "chart-box" });
    body.append(el("h4", {}, "四维得分雷达图"), radarEl);
    const radarChart = echarts.init(radarEl);
    radarChart.setOption({
      tooltip: {},
      radar: {
        indicator: dims.map(([, label]) => ({ name: label, max: 3 })),
        radius: "65%",
      },
      series: [{
        type: "radar",
        data: [{
          value: dims.map(([k]) => {
            const v = d.scores && d.scores["score_" + k];
            return v == null ? 0 : Math.max(-3, Math.min(3, Number(v)));
          }),
          name: code,
        }],
      }],
    });
    modalCharts.push(radarChart);

    // K线趋势图：close_af1 + MA20/MA60（本地缓存重建，离线可用）
    const kl = d.kline || {};
    if (kl.dates && kl.dates.length) {
      const kEl = el("div", { class: "chart-box chart-kline" });
      body.append(el("h4", {}, `K线趋势（后复权重建，${kl.dates.length} 根，截至 ${runDay}）`), kEl);
      const kChart = echarts.init(kEl);
      kChart.setOption({
        tooltip: { trigger: "axis" },
        legend: { data: ["收盘(af1)", "MA20", "MA60"] },
        grid: { left: 50, right: 20, top: 30, bottom: 60 },
        xAxis: { type: "category", data: kl.dates },
        yAxis: { type: "value", scale: true },
        dataZoom: [{ type: "inside" }, { type: "slider", height: 18, bottom: 12 }],
        series: [
          { name: "收盘(af1)", type: "line", data: kl.close_af1, showSymbol: false, connectNulls: false, lineStyle: { width: 1.5 } },
          { name: "MA20", type: "line", data: kl.ma20, showSymbol: false, connectNulls: false },
          { name: "MA60", type: "line", data: kl.ma60, showSymbol: false, connectNulls: false },
        ],
      });
      modalCharts.push(kChart);
    } else if (kl.error) {
      body.append(el("p", { class: "msg-err" }, kl.error));
    } else {
      body.append(el("p", { class: "muted small" }, "本地缓存无该股票K线（未迁移/新股）。"));
    }
  } catch (e) {
    body.append(el("p", { class: "msg-err" }, "图表加载失败: " + e.message));
  }
}

function initStockModal() {
  $("#stock-modal-close").addEventListener("click", closeStockModal);
  $(".modal-mask", $("#stock-modal")).addEventListener("click", closeStockModal);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("#stock-modal").classList.contains("hidden")) closeStockModal();
  });
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
  scoring: { title: "打分模型（v2）", icon: "🧮" },
  badges: { title: "Badge 阈值（v2 Web）", icon: "🏷️" },
  hard_filter: { title: "硬性剔除（v2）", icon: "🚫" },
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
  "scoring.mode": "打分模式：zscore（截面Z-Score多因子，v2 默认）/ legacy（旧四维AND硬过滤，可回退）",
  "scoring.top_n": "榜单输出前 N 名",
  "scoring.missing_policy": "缺失因子处理：neutral_renorm（z=0+按可用权重归一化）/ neutral（z=0不重归一化）/ drop（维度缺失不参与合成）",
  "scoring.weights": "四维权重（和必须为1；滑块 0–1，实时显示归一化值）",
  "scoring.sub_weights": "维度内子因子权重（每维度和必须为1；格式 key:值,key:值）",
  "badges.industry_top_pct": "行业TopN% badge：industry_roe_rank_pct ≤ 该值",
  "badges.fscore_min": "F-Score badge：piotroski_fscore ≥ 该值",
  "hard_filter.st_enabled": "ST 剔除（日K isST=1）",
  "hard_filter.listing_min_trading_days": "上市未满 N 个交易日剔除（全历史K线行数判断）",
  "data.kline_calendar_days_back": "后复权窗口K线回溯日历天（需覆盖均线/收益窗口交易日+节假日余量）",
  "data.retry_max_attempts": "BaoStock 单次查询失败重试次数（指数退避）",
  "data.cache_dir": "本地缓存目录（相对路径基于项目根）",
  "crosscheck.enabled": "启用腾讯 qt.gtimg.cn 交叉验证（不进主计算路径）",
  "crosscheck.sample_size": "从最终入选股中抽样 N 只验证收盘价",
  "crosscheck.price_tolerance_pct": "收盘价偏差容忍度（%）",
  "crosscheck.batch_size": "腾讯接口单批请求股票数",
};
// 字段类型（结构性元数据，非业务阈值）：int/float/bool/str/list/enum2/weights_dict/sub_weights_dict
const FIELD_TYPE = {
  ma_period: "int", return_window_days: "int", min_return_pct: "float",
  max_return_pct: "float", max_annual_volatility_pct: "float",
  window_days: "int", min_yield_pct: "float",
  rank_by: "enum2", top_pct: "float", min_group_size: "int",
  roe_min_pct: "float", net_profit_yoy_field: "enum2", liability_max_pct: "float",
  gross_margin_min_pct: "float", probe_quarters_back: "int",
  a_share_prefixes: "list", listing_min_trading_days: "int", st_name_keyword: "str",
  mode: "enum2", top_n: "int", missing_policy: "enum2",
  weights: "weights_dict", sub_weights: "sub_weights_dict",
  industry_top_pct: "float", fscore_min: "int",
  st_enabled: "bool",
  kline_calendar_days_back: "int", retry_max_attempts: "int", cache_dir: "str",
  enabled: "bool", sample_size: "int", price_tolerance_pct: "float", batch_size: "int",
};
// enum2 选项（按 section.field；缺省回退通用）
const ENUM_OPTIONS = {
  "industry.rank_by": [["roeAvg", "roeAvg（最近披露报告期 ROE）"]],
  "fundamental.net_profit_yoy_field": [
    ["YOYPNI", "YOYPNI（归母净利同比）"], ["YOYNI", "YOYNI（净利润同比）"]],
  "scoring.mode": [
    ["zscore", "zscore（截面Z-Score多因子，v2 默认）"],
    ["legacy", "legacy（旧四维AND硬过滤，可回退）"]],
  "scoring.missing_policy": [
    ["neutral_renorm", "neutral_renorm（缺失 z=0 + 按可用权重归一化）"],
    ["neutral", "neutral（缺失 z=0，不重归一化）"],
    ["drop", "drop（维度缺失 → 不参与合成）"]],
};

const WEIGHT_DIMS = [
  ["technical", "技术面"], ["dividend", "股息"],
  ["industry", "行业"], ["fundamental", "基本面"],
];

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
          editing ? null : el("span", { class: "s-val" }, displayValue(section, field, value))));
      if (editing) f.append(editControl(section, field, value));
      else if (desc) f.append(el("div", { class: "s-desc" }, desc));
      card.append(f);
    }
    cards.append(card);
  }
}

function displayValue(section, field, v) {
  const t = FIELD_TYPE[field];
  if (t === "list") return Array.isArray(v) ? v.join(", ") : String(v);
  if (t === "bool") return v ? "true" : "false";
  if (t === "weights_dict") {
    return WEIGHT_DIMS.filter(([k]) => k in v).map(([k, label]) => `${label} ${v[k]}`).join(" / ");
  }
  if (t === "sub_weights_dict") {
    return Object.entries(v || {}).map(([dim, m]) =>
      dim + "{" + Object.entries(m).map(([k, x]) => `${k}:${x}`).join(",") + "}").join("  ");
  }
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
  } else if (t === "enum2") {
    const opts = ENUM_OPTIONS[`${section}.${field}`] || [[String(value), String(value)]];
    input = el("select", {}, ...opts.map(([val, label]) =>
      el("option", { value: val, selected: value === val ? "selected" : null }, label)));
  } else if (t === "list") {
    input = el("input", { type: "text", value: Array.isArray(value) ? value.join(", ") : String(value), placeholder: "sh.60, sh.68, sz.00, sz.30" });
  } else if (t === "weights_dict") {
    // 四维权重滑块（0–1，实时显示归一化值）
    input = el("div", { class: "weight-sliders" });
    for (const [dim, label] of WEIGHT_DIMS) {
      const raw = el("span", { class: "w-raw" }, String(value[dim]));
      const norm = el("span", { class: "w-norm" }, "");
      const slider = el("input", { type: "range", min: "0", max: "1", step: "0.01", value: String(value[dim]) });
      slider.dataset.section = section;
      slider.dataset.field = field;
      slider.dataset.dim = dim;
      const refresh = () => {
        raw.textContent = Number(slider.value).toFixed(2);
        let sum = 0;
        for (const s of $$(".weight-sliders input[type=range]")) sum += Number(s.value) || 0;
        const txt = sum > 0 ? (Number(slider.value) / sum).toFixed(3) : "—";
        norm.textContent = `归一化 ${txt}` + (Math.abs(sum - 1) > 1e-9 ? `（和=${sum.toFixed(2)}≠1，保存将被拒绝）` : "");
      };
      slider.addEventListener("input", refresh);
      input.append(el("div", { class: "w-row" },
        el("span", { class: "w-label" }, label), slider, raw, norm));
      setTimeout(refresh, 0);
    }
  } else if (t === "sub_weights_dict") {
    // 每维度一行 key:值,key:值 文本编辑
    input = el("div", { class: "subweight-edits" });
    for (const [dim] of WEIGHT_DIMS) {
      const m = value[dim] || {};
      const txt = Object.entries(m).map(([k, x]) => `${k}:${x}`).join(",");
      const ti = el("input", { type: "text", value: txt, placeholder: "key:值,key:值" });
      ti.dataset.section = section;
      ti.dataset.field = field;
      ti.dataset.dim = dim;
      input.append(el("div", { class: "w-row" },
        el("span", { class: "w-label" }, dim), ti));
    }
  } else {
    input = el("input", { type: "text", value: String(value) });
  }
  if (t !== "weights_dict" && t !== "sub_weights_dict") {
    input.dataset.section = section;
    input.dataset.field = field;
  }
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

function parseSubWeightsText(txt) {
  const out = {};
  for (const part of String(txt).split(",")) {
    const s = part.trim();
    if (!s) continue;
    const i = s.lastIndexOf(":");
    if (i <= 0) return null;
    const k = s.slice(0, i).trim();
    const v = parseFloat(s.slice(i + 1));
    if (!k || isNaN(v) || v < 0) return null;
    out[k] = v;
  }
  return Object.keys(out).length ? out : null;
}

async function saveStrategy() {
  // 以当前 API 返回的 JSON 为底，覆盖编辑值 → 保证所有 key 齐全
  const payload = JSON.parse(JSON.stringify(strategyJson));
  let bad = false;
  for (const input of $$(".s-field input, .s-field select")) {
    const sec = input.dataset.section, field = input.dataset.field;
    if (!sec || !field) continue;
    const t = FIELD_TYPE[field] || "str";
    let v;
    if (t === "weights_dict") {
      // 滑块：按 data-dim 聚合到 payload[sec].weights[dim]
      const dim = input.dataset.dim;
      v = parseFloat(input.value);
      if (isNaN(v) || v < 0 || v > 1) { setStrategyMsg(`✗ ${sec}.${field}.${dim} 必须在 0–1`, "err"); bad = true; continue; }
      payload[sec][field] = payload[sec][field] || {};
      payload[sec][field][dim] = v;
    } else if (t === "sub_weights_dict") {
      const dim = input.dataset.dim;
      const parsed = parseSubWeightsText(input.value);
      if (!parsed) { setStrategyMsg(`✗ ${sec}.${field}.${dim} 格式应为 key:值,key:值`, "err"); bad = true; continue; }
      payload[sec][field] = payload[sec][field] || {};
      payload[sec][field][dim] = parsed;
    } else if (t === "int") {
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
    // weights_dict / sub_weights_dict 已在分支内按 data-dim 聚合写入，不能再用标量覆盖
    if (t !== "weights_dict" && t !== "sub_weights_dict") {
      payload[sec][field] = v;
    }
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
// 运行页（v2：SSE 实时日志优先，失败回退 3s 轮询；进度条绑定 progress 事件）
// ---------------------------------------------------------------------------
let pollTimer = null;
let sseSource = null;
let sseFailCount = 0;
let runFinished = false;
const logBuffer = [];

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
    logBuffer.length = 0;
    $("#log-tail").textContent = "";
    startRunMonitor(res.task_id);
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

function appendLogLine(line) {
  logBuffer.push(line);
  if (logBuffer.length > 400) logBuffer.shift();
  const lt = $("#log-tail");
  lt.textContent = logBuffer.join("\n");
  lt.scrollTop = lt.scrollHeight;
}

function updateProgressBar(done, total, stage) {
  const pct = total > 0 ? Math.min(100, done / total * 100) : 0;
  const bar = $("#task-progress-bar");
  bar.style.width = pct.toFixed(1) + "%";
  bar.parentElement.title = `${stage}: ${done}/${total}`;
  $("#task-progress-label").textContent =
    `进度 [${stage}] ${done}/${total}（${pct.toFixed(1)}%）`;
}

function finishRun(state) {
  if (runFinished) return;
  runFinished = true;
  stopSse();
  clearInterval(pollTimer);
  showProgress("", state, "");
  $("#btn-run-start").disabled = false;
  if (state === "done") {
    toast("筛选完成！结果页已刷新");
    switchTab("results");
  } else {
    toast("运行失败，请查看日志", false);
  }
}

function stopSse() {
  if (sseSource) { try { sseSource.close(); } catch { /* ignore */ } sseSource = null; }
}

// SSE 优先；连续失败（代理不支持/老浏览器）→ 回退现有 3s 轮询
function startRunMonitor(taskId) {
  runFinished = false;
  sseFailCount = 0;
  try {
    const es = new EventSource(`/api/runs/${taskId}/events`);
    sseSource = es;
    es.onmessage = (ev) => {
      let data;
      try { data = JSON.parse(ev.data); } catch { return; }
      if (data.type === "heartbeat") return;
      if (data.type === "log" && data.text != null) appendLogLine(data.text);
      else if (data.type === "progress") updateProgressBar(data.done, data.total, data.stage);
      else if (data.type === "done") finishRun("done");
      else if (data.type === "error") { appendLogLine("[error 事件]"); finishRun("failed"); }
    };
    es.onerror = () => {
      // 终态后服务端主动关流也会触发 onerror → 不重试
      if (runFinished) { stopSse(); return; }
      sseFailCount += 1;
      if (sseFailCount >= 3) {
        // EventSource 自带重连；若持续失败说明通道不可用 → 回退轮询兜底
        stopSse();
        toast("SSE 不可用，已回退到 3s 轮询", false);
        pollStatus(taskId);
      }
    };
  } catch (e) {
    // EventSource 构造失败（极少数环境）→ 直接轮询
    stopSse();
    pollStatus(taskId);
  }
}

// 兜底轮询（保留 v1 行为）
function pollStatus(taskId) {
  clearInterval(pollTimer);
  const tick = async () => {
    try {
      const s = await api(`/api/runs/${taskId}/status`);
      showProgress(taskId, s.status,
        `任务 ${taskId}${s.date ? " · 日期 " + s.date : ""} · ${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`);
      const lt = $("#log-tail");
      lt.textContent = (s.log_tail || []).join("\n") || "（暂无日志）";
      lt.scrollTop = lt.scrollHeight;
      if (s.status === "done") finishRun("done");
      else if (s.status === "failed") finishRun("failed");
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
// v6 数据湖页签（/api/lake/*；独立分析层，错误态降级不阻塞其他 tab）
// ---------------------------------------------------------------------------
let lakeLoaded = false;      // 首次进 tab 才拉 status/market（懒加载）
let lakeInstalled = null;    // /status.installed（null=未知）
let lakeMarketPage = 1;

function lakeSetError(msg) {
  const box = $("#lake-error");
  if (!msg) { box.classList.add("hidden"); box.textContent = ""; return; }
  box.textContent = "数据湖不可用：" + msg;
  box.classList.remove("hidden");
}

// v6.0.4：数据端点 409 lake_backfill_in_progress（灌数进程持 DuckDB 独占写锁）→
// 中性占位，**不**弹红色错误横幅——顶部琥珀色"⏳ 数据灌入中"块已说明原因。
function lakeIsBackfillErr(e) {
  return !!(e && e.status === 409 && e.body && e.body.error === "lake_backfill_in_progress");
}

// v6.0.4：灌数中状态块（琥珀色，与红色错误横幅 / "未初始化"空态三态互不串味）。
// d = /status 响应；d.backfill_in_progress !== true → 隐藏块（防灌数结束后残留）。
function lakeSetBackfill(d) {
  const box = $("#lake-backfill");
  if (!d || !d.backfill_in_progress) { box.classList.add("hidden"); return; }
  $("#lake-backfill-meta").textContent =
    "持锁 PID " + (d.lock_holder_pid ?? "未知") + " · 进度更新于 " + (d.updated_at || "—");
  lakeRenderTasksTable($("#lake-backfill-tasks"), d.tasks || []);
  box.classList.remove("hidden");
}

// tasks 摘要表渲染（区块C 正常态与 v6.0.4 灌数中块共用同一份，视觉一致）：
// 表 / 层级 / 状态 / 进度(done/total+条) / 配额(今日 used/budget) / ETA(min)。
function lakeRenderTasksTable(table, tasks) {
  let th = "<thead><tr><th>表</th><th>层级</th><th>状态</th><th class='num'>进度</th><th class='num'>配额(今日)</th><th class='num'>ETA(min)</th></tr></thead><tbody>";
  if (!tasks.length) th += '<tr><td colspan="6" class="placeholder">暂无后台补齐任务</td></tr>';
  for (const t of tasks) {
    const pct = t.total ? Math.round((t.done / t.total) * 100) : 0;
    th += `<tr><td>${esc(t.table)}</td><td>${esc(t.tier)}</td>` +
      `<td><span class="badge ${esc(t.state || "idle")}">${esc(t.state || "idle")}</span></td>` +
      `<td class="num lake-task-progress"><div class="progress-track" style="margin:0"><div class="progress-bar" style="width:${pct}%"></div></div>${t.done ?? 0}/${t.total ?? 0}</td>` +
      `<td class="num">${t.quota_used_today ?? 0}/${t.quota_budget ?? "—"}</td>` +
      `<td class="num">${t.eta_min ?? "—"}</td></tr>`;
  }
  th += "</tbody>";
  table.innerHTML = th;
}

// 加载态：骨架屏（切股/搜索时）
function lakeSkeleton(n = 4) {
  let h = '<div class="lake-loading-label">加载中…</div>';
  for (let i = 0; i < n; i++) h += '<div class="lake-skeleton"></div>';
  return h;
}

// NULL → "—"；百分比带 %；市值带"亿"（正常态格式约定，报告 §5）
function lakeFmt(v, kind) {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  if (isNaN(n)) return String(v);
  if (kind === "pct") return n.toFixed(2) + "%";
  if (kind === "yi") return n.toFixed(1) + "亿";
  if (kind === "num") return n.toFixed(2);
  return String(v);
}

async function initLakePage() {
  const input = $("#lake-search-input");
  const resultsBox = $("#lake-search-results");

  // 搜索（防抖 + 下拉）
  let debounce = null;
  async function doSearch() {
    const q = input.value.trim();
    try {
      const d = await api("/api/lake/search?q=" + encodeURIComponent(q));
      resultsBox.innerHTML = "";
      if (!d.results || !d.results.length) {
        // 空态：搜索无结果
        resultsBox.append(el("div", { class: "sr-empty" }, "未找到匹配股票"));
      } else {
        for (const r of d.results) {
          const item = el("div", { class: "sr-item" },
            el("span", { class: "sr-code" }, r.ts_code),
            el("span", { class: "sr-name" }, r.name || ""),
            el("span", { class: "sr-ind" }, r.industry_name || ""));
          item.addEventListener("click", () => { selectLakeStock(r.ts_code); });
          resultsBox.append(item);
        }
      }
      resultsBox.classList.remove("hidden");
    } catch (e) {
      // v6.0.4：灌数持锁（409 lake_backfill_in_progress）→ 中性处理，不弹红横幅
      // （顶部琥珀色"⏳ 数据灌入中"块已说明原因）；其余错误保持红横幅。
      if (!lakeIsBackfillErr(e)) lakeSetError(e.message);
      resultsBox.classList.add("hidden");
    }
  }
  input.addEventListener("input", () => {
    clearTimeout(debounce); debounce = setTimeout(doSearch, 250);
  });
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") doSearch(); });
  $("#btn-lake-search").addEventListener("click", doSearch);
  // 点空白收起下拉
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".lake-toolbar")) resultsBox.classList.add("hidden");
  });

  // 区块B：筛选/排序/分页
  $("#lake-industry-filter").addEventListener("change", () => { lakeMarketPage = 1; loadLakeMarket(); });
  $("#lake-soe-filter").addEventListener("change", () => { lakeMarketPage = 1; loadLakeMarket(); });
  $("#lake-sort-select").addEventListener("change", () => { lakeMarketPage = 1; loadLakeMarket(); });
  // ⟳ 刷新：进度 + 行业下拉一并刷新（backfill 灌入新行业后无需整页 reload）
  $("#btn-lake-refresh-status").addEventListener("click", () => { loadLakeStatus(); loadLakeIndustries(); });
}

async function selectLakeStock(ts_code) {
  $("#lake-search-results").classList.add("hidden");
  const body = $("#lake-stock-body");
  $("#lake-stock-title").textContent = "";
  body.innerHTML = lakeSkeleton(5);   // 加载态：骨架屏
  // v6.0.6：日K线区同步进加载态（切股后重取；区间选择保持当前值）
  lakeKlineTs = ts_code;
  const kwrap = $("#lake-kline-chart");
  if (kwrap) kwrap.innerHTML = '<div class="lake-kline-empty">加载中…</div>';
  try {
    const d = await api("/api/lake/stock/" + ts_code);
    renderLakeStock(d);
    loadLakeKline(ts_code, lakeKlineRange);   // v6.0.6：全景卡片顶部日K线蜡烛图
  } catch (e) {
    if (e.status === 404) body.innerHTML = '<p class="placeholder">数据湖无此股（T1 未灌入）</p>';
    else if (lakeIsBackfillErr(e)) body.innerHTML = '<p class="placeholder">⏳ 数据灌入中，个股查询暂不可用——稍后刷新</p>';
    else { lakeSetError(e.message); body.innerHTML = '<p class="placeholder">加载失败</p>'; }
    // v6.0.6：以上分支整体替换卡片体（K线区随之消失，不残留"加载中…"）；
    // /stock 成功而 /kline 失败的降级在 loadLakeKline 内处理（空态/409 占位）。
  }
}

function renderLakeStock(d) {
  const b = d.base || {};
  $("#lake-stock-title").textContent = ` ${d.ts_code} · ${b.name || ""}`;
  const body = $("#lake-stock-body");
  let h = "";
  // v6.0.6：卡片顶部 = 日K线蜡烛图（区间切换 + SVG 手绘；数据由 loadLakeKline 异步填充）
  h += '<div class="lake-section-title">日K线（T2 · 原始价）</div>';
  h += '<div class="lake-kline-bar" id="lake-kline-bar"></div>';
  h += '<div class="lake-kline-wrap" id="lake-kline-chart"><div class="lake-kline-empty">加载中…</div></div>';
  // 基础卡（名称/行业/板块/is_st/soe_flag+soe_basis）
  h += '<div class="lake-base-grid">';
  h += lakeKv("名称", b.name || "—");
  h += lakeKv("行业", [b.industry_csric2, b.industry_name].filter(Boolean).join(" ") || "—");
  h += lakeKv("板块", b.board || "—");
  h += lakeKv("ST", b.is_st ? "是" : "否");
  h += lakeKv("央国企", (b.soe_flag || "—") + (b.soe_basis ? "（" + b.soe_basis + "）" : ""));
  h += "</div>";
  // 估值行（total_mv/float_mv/pe/pb/turnover/ttm_yield）
  h += '<div class="lake-section-title">估值</div><div class="lake-base-grid">';
  h += lakeKv("总市值", lakeFmt(b.total_mv, "yi"));
  h += lakeKv("流通市值", lakeFmt(b.float_mv, "yi"));
  h += lakeKv("PE(TTM)", lakeFmt(b.pe_ttm, "num"));
  h += lakeKv("PB", lakeFmt(b.pb, "num"));
  h += lakeKv("换手率", lakeFmt(b.turnover_pct, "pct"));
  h += lakeKv("TTM股息率", lakeFmt(b.ttm_yield_pct, "pct"));
  h += "</div>";
  // 因子网格（T8）
  h += '<div class="lake-section-title">因子（T8' + (d.factors_as_of ? " · " + d.factors_as_of : "") + "）</div>";
  const fkeys = Object.keys(d.factors || {});
  if (!fkeys.length) {
    h += '<p class="muted small">暂无数据（后台补齐中）</p>';
  } else {
    h += '<div class="lake-factor-grid">';
    for (const k of fkeys) h += `<div class="lake-factor"><div class="k">${esc(k)}</div><div class="v">${lakeFmt(d.factors[k], "num")}</div></div>`;
    h += "</div>";
  }
  // 最近分红
  h += '<div class="lake-section-title">最近分红</div><div class="lake-base-grid">';
  h += lakeKv("除权日", b.last_ex_date || "—");
  h += lakeKv("每股分红(元)", lakeFmt(b.last_cash_dps, "num"));
  h += "</div>";
  // T5 最近季
  const f = d.fundamental_latest;
  h += '<div class="lake-section-title">最近季报（T5）</div>';
  if (!f) {
    h += '<p class="muted small">暂无数据（后台补齐中）</p>';
  } else {
    h += '<div class="lake-base-grid">';
    h += lakeKv("报告期", f.period || "—");
    h += lakeKv("披露日", f.pub_date || "—");
    h += lakeKv("ROE(平均)", lakeFmt(f.roe_avg, "pct"));
    h += lakeKv("ROE(加权)", lakeFmt(f.roe_weighted, "pct"));
    h += lakeKv("净利同比", lakeFmt(f.yoy_pni, "pct"));
    h += lakeKv("毛利率", lakeFmt(f.gross_margin, "pct"));
    h += lakeKv("资产负债率", lakeFmt(f.liability_pct, "pct"));
    h += "</div>";
  }
  // 前十大股东表（T6）
  h += '<div class="lake-section-title">前十大股东（T6 · 流通股口径）</div>';
  if (!d.holders_top10 || !d.holders_top10.length) {
    h += '<p class="muted small">暂无数据（后台补齐中）</p>';
  } else {
    h += '<div class="tbl-wrap"><table class="data"><thead><tr>' +
      "<th>#</th><th>股东名称</th><th class='num'>占流通股%</th><th>股本性质</th>" +
      "</tr></thead><tbody>";
    for (const r of d.holders_top10) {
      h += `<tr><td>${esc(r.holder_rank)}</td><td>${esc(r.holder_name)}</td>` +
        `<td class="num">${lakeFmt(r.hold_ratio, "pct")}</td><td>${esc(r.share_nature || "—")}</td></tr>`;
    }
    h += "</tbody></table></div>";
  }
  body.innerHTML = h;
  // v6.0.6：填充区间切换按钮组（DOM 就绪后；数据请求由 selectLakeStock 发起）
  lakeKlineRenderBar(lakeKlineRange);
}

function lakeKv(k, v) {
  return `<div class="lake-kv"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div></div>`;
}

// ---------------------------------------------------------------------------
// v6.0.6：个股全景·日K线蜡烛图（手绘 SVG，无图表库/CDN）
//
// 数据源：GET /api/lake/kline/{ts_code}?days=N（raw kline_daily，date 升序）。
// 布局（viewBox 1000×480 坐标系，preserveAspectRatio 等比缩放 → 图宽自适应卡片）：
//   - K线区上 ~72%（y: 26..350），成交量柱下 ~28%（y: 368..448），底部日期刻度；
//   - x = 日期等距（第 i 根 → x = padL + slot*(i+0.5)）；y = 价格自适应
//     （min/max 含 4% 上下留白，防最高/最低点贴边）。
// A股配色：红涨绿跌——当日 close≥open → --lake-up（红），否则 --lake-down（绿）
//   （.lk-up/.lk-down 走 style.css CSS 变量，与主题体系协调）。
// hover：十字线 + tooltip（日期/开高低收/成交量）跟随**最近 K线点**（按 x 吸附）。
// 刻度抽稀：价格 4~6 档、日期 4~6 档（步长取 ceil(n/target)，不挤叠）。
// ---------------------------------------------------------------------------
const LAKE_KLINE_RANGES = [
  { key: "60", label: "60" },
  { key: "120", label: "120" },
  { key: "250", label: "250" },
  { key: "all", label: "全部" },
];
let lakeKlineTs = null;      // 当前图所属 ts_code（切股时作废旧请求）
let lakeKlineRange = "250";  // 默认 250（brief）

// 409 灌数中占位：与页面既有降级文案一致（"⏳ 数据灌入中，…暂不可用——稍后刷新"）
function lakeKlineStateHtml() {
  return '<p class="lake-kline-empty">⏳ 数据灌入中，K线暂不可用——稍后刷新</p>';
}

// 空态/错误态占位（brief 逐字文案："该股暂无K线数据"；不白屏）
function lakeKlinePlaceholder(msg) {
  return `<div class="lake-kline-empty">${esc(msg)}</div>`;
}

// 刻度抽稀步长：ceil(n/target)，保证档数 ≤target（4~6 档区间内）、≥1（不挤叠）
function lakeKlineTickStep(n, target = 5) {
  return Math.max(1, Math.ceil(n / target));
}

// 成交量格式化（tooltip 用）：≥1亿 → x.xx亿、≥1万 → x.x万、其余整数
function lakeKlineFmtVol(v) {
  if (v === null || v === undefined || isNaN(Number(v))) return "—";
  const n = Number(v);
  if (n >= 1e8) return (n / 1e8).toFixed(2) + "亿";
  if (n >= 1e4) return (n / 1e4).toFixed(1) + "万";
  return String(Math.round(n));
}

function lakeKlineRenderChart(wrap, data, ts_code) {
  // 每次重绘先清空（区间切换/切股都会重进本函数，防残留）
  wrap.innerHTML = "";
  const rows = (data && data.rows) || [];
  if (!rows.length) {
    wrap.innerHTML = lakeKlinePlaceholder("该股暂无K线数据");
    return;
  }
  // ---- viewBox 坐标系常量（1000×480；K线区上72% / 量区下28%）----
  const W = 1000, H = 480, padL = 64, padR = 14;
  const pTop = 26, pBot = 350;      // K线价格区（高 324 ≈ 72%）
  const vTop = 368, vBot = 448;     // 成交量区（高 80 ≈ 28%）
  const plotW = W - padL - padR;
  const n = rows.length;
  const slot = plotW / n;           // x=日期等距
  const xi = (i) => padL + slot * (i + 0.5);

  // ---- y 比例尺：价格自适应（min/max 含 4% 留白）；成交量 0..max ----
  let pMin = Infinity, pMax = -Infinity, vMax = 0;
  for (const r of rows) {
    const lo = Math.min(Number(r.low), Number(r.open), Number(r.close));
    const hi = Math.max(Number(r.high), Number(r.open), Number(r.close));
    if (lo < pMin) pMin = lo;
    if (hi > pMax) pMax = hi;
    if (Number(r.volume) > vMax) vMax = Number(r.volume);
  }
  if (!isFinite(pMin) || !isFinite(pMax)) {
    wrap.innerHTML = lakeKlinePlaceholder("该股暂无K线数据");
    return;
  }
  const span = (pMax - pMin) || Math.abs(pMax) || 1;
  pMin -= span * 0.04; pMax += span * 0.04;   // 上下留白（防最高/最低贴边）
  const py = (p) => pBot - ((p - pMin) / (pMax - pMin)) * (pBot - pTop);
  const vy = (v) => vBot - (vMax ? (Number(v) / vMax) * (vBot - vTop - 4) : 0);

  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
  svg.setAttribute("class", "lake-kline-svg");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `${ts_code} 日K线蜡烛图（${n} 根）`);
  const add = (tag, attrs, parent) => {
    const e = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    (parent || svg).appendChild(e);
    return e;
  };

  // ---- 价格刻度（4~6 档）+ 水平网格线 ----
  {
    const total = Math.max(1, Math.round((pMax - pMin) / span * 5));   // ≈5 档目标
    for (let k = 0; k <= total; k++) {
      const t = pMin + ((pMax - pMin) * k) / total;
      const y = py(t);
      add("line", { class: "lk-grid", x1: padL, y1: y, x2: W - padR, y2: y });
      const tx = add("text", { class: "lk-tick", x: padL - 8, y: y + 4, "text-anchor": "end" });
      tx.textContent = t.toFixed(2);
    }
  }

  // ---- 日期刻度（4~6 档，自适应抽稀）----
  const dStep = lakeKlineTickStep(n, 5);
  for (let i = 0; i < n; i += dStep) {
    const x = xi(i);
    add("line", { class: "lk-axis", x1: x, y1: vBot, x2: x, y2: vBot + 5 });
    const tx = add("text", { class: "lk-tick", x, y: vBot + 20, "text-anchor": "middle" });
    tx.textContent = String(rows[i].date).slice(5);   // MM-DD（完整日期见 tooltip）
  }

  // ---- 蜡烛 + 成交量柱（A股配色：红涨绿跌，close≥open → lk-up 否则 lk-down）----
  const bodyW = Math.max(1.5, Math.min(slot * 0.62, 14));   // 根数多/窄屏时自动收窄
  for (let i = 0; i < n; i++) {
    const r = rows[i], x = xi(i);
    const up = Number(r.close) >= Number(r.open);           // A股：红涨绿跌
    const cls = up ? "lk-up" : "lk-down";
    add("line", { class: cls, x1: x, y1: py(r.high), x2: x, y2: py(r.low),
                  "stroke-width": Math.max(1, Math.min(slot * 0.12, 2)) });   // 影线
    const yO = py(r.open), yC = py(r.close);
    add("rect", { class: cls, x: x - bodyW / 2, y: Math.min(yO, yC),
                  width: bodyW, height: Math.max(1, Math.abs(yC - yO)) });    // 实体
    const vv = Number(r.volume) || 0;
    add("rect", { class: cls, x: x - bodyW / 2, y: vy(vv), width: bodyW,
                  height: Math.max(1, vBot - vy(vv)), opacity: "0.75" });     // 量柱（同色弱化）
  }

  // ---- hover 层：十字线 + 高亮框 + 透明捕获层（事件挂捕获层，不挡渲染）----
  const crossV = add("line", { class: "lk-cross", x1: 0, y1: pTop, x2: 0, y2: vBot, visibility: "hidden" });
  const crossH = add("line", { class: "lk-cross", x1: padL, y1: 0, x2: W - padR, y2: 0, visibility: "hidden" });
  const hoverBox = add("rect", { class: "lk-hoverdot", x: 0, y: 0, width: 0, height: 0, rx: 2, visibility: "hidden" });
  const tip = document.createElement("div");
  tip.className = "lake-kline-tip";

  let lastClientY = 0;   // 闭包存最近一次事件坐标（tooltip 垂直跟随）
  const capture = add("rect", { x: padL, y: pTop, width: plotW, height: vBot - pTop + 40,
                                fill: "transparent" });
  const showAt = (clientX) => {
    // 屏幕坐标 → viewBox 坐标（getBoundingClientRect 反推，随缩放自适应）
    const rect = svg.getBoundingClientRect();
    if (!rect.width) return;
    const vx = ((clientX - rect.left) / rect.width) * W;
    let i = Math.round((vx - padL) / slot - 0.5);
    i = Math.max(0, Math.min(n - 1, i));            // 吸附最近 K线点
    const r = rows[i], x = xi(i);
    crossV.setAttribute("x1", x); crossV.setAttribute("x2", x);
    crossH.setAttribute("y1", py(r.close)); crossH.setAttribute("y2", py(r.close));
    hoverBox.setAttribute("x", x - bodyW / 2 - 3);
    hoverBox.setAttribute("y", py(r.high) - 3);
    hoverBox.setAttribute("width", bodyW + 6);
    hoverBox.setAttribute("height", Math.max(py(r.low), vy(Number(r.volume) || 0)) - py(r.high) + 6);
    crossV.setAttribute("visibility", "visible");
    crossH.setAttribute("visibility", "visible");
    hoverBox.setAttribute("visibility", "visible");
    tip.innerHTML =
      `<div class="tt-date">${esc(r.date)}</div>` +
      `<div class="tt-row"><span class="tt-k">开</span><span class="tt-v">${Number(r.open).toFixed(2)}</span></div>` +
      `<div class="tt-row"><span class="tt-k">高</span><span class="tt-v">${Number(r.high).toFixed(2)}</span></div>` +
      `<div class="tt-row"><span class="tt-k">低</span><span class="tt-v">${Number(r.low).toFixed(2)}</span></div>` +
      `<div class="tt-row"><span class="tt-k">收</span><span class="tt-v" style="color:${Number(r.close) >= Number(r.open) ? "#fca5a5" : "#86efac"}">${Number(r.close).toFixed(2)}</span></div>` +
      `<div class="tt-row"><span class="tt-k">量</span><span class="tt-v">${lakeKlineFmtVol(r.volume)}</span></div>`;
    tip.style.display = "block";
    // tooltip 跟随鼠标（容器内坐标），右缘溢出时翻到左侧、上下夹在容器内
    const wrapRect = wrap.getBoundingClientRect();
    let left = clientX - wrapRect.left + 14;
    if (left + tip.offsetWidth > wrapRect.width - 4) left = clientX - wrapRect.left - tip.offsetWidth - 14;
    const top = Math.max(4, Math.min(lastClientY - wrapRect.top, wrapRect.height - tip.offsetHeight - 4));
    tip.style.left = left + "px";
    tip.style.top = top + "px";
  };
  capture.addEventListener("mousemove", (e) => { lastClientY = e.clientY; showAt(e.clientX); });
  capture.addEventListener("mouseleave", () => {
    tip.style.display = "none";
    crossV.setAttribute("visibility", "hidden");
    crossH.setAttribute("visibility", "hidden");
    hoverBox.setAttribute("visibility", "hidden");
  });

  wrap.append(svg, tip);
}

async function loadLakeKline(ts_code, range) {
  const wrap = $("#lake-kline-chart");
  if (!wrap || ts_code !== lakeKlineTs) return;   // 已切股 → 丢弃过期请求
  wrap.innerHTML = '<div class="lake-kline-empty">加载中…</div>';
  try {
    const d = await api(`/api/lake/kline/${ts_code}?days=${encodeURIComponent(range)}`);
    if (ts_code !== lakeKlineTs) return;          // 响应回来时已切股 → 丢弃
    lakeKlineRenderChart(wrap, d, ts_code);
  } catch (e) {
    if (ts_code !== lakeKlineTs) return;
    if (lakeIsBackfillErr(e)) wrap.innerHTML = lakeKlineStateHtml();   // 409 灌数中：与页面既有降级一致
    else if (e.status === 409) wrap.innerHTML = lakeKlinePlaceholder("数据湖未初始化，K线暂不可用");
    else { wrap.innerHTML = lakeKlinePlaceholder("K线加载失败——稍后刷新"); }
  }
}

// 区间切换按钮组（默认 250；"全部"= days=all）：切换即重取数据。
function lakeKlineRenderBar(active) {
  const bar = $("#lake-kline-bar");
  if (!bar) return;
  bar.innerHTML = "";
  for (const r of LAKE_KLINE_RANGES) {
    const b = el("button", { class: "lake-kline-range" + (r.key === active ? " cur" : "") }, r.label);
    b.addEventListener("click", () => {
      if (r.key === lakeKlineRange) return;
      lakeKlineRange = r.key;
      lakeKlineRenderBar(lakeKlineRange);
      loadLakeKline(lakeKlineTs, lakeKlineRange);   // 切换即重取
    });
    bar.append(b);
  }
}

async function loadLakeIndustries() {
  // v6.0.1 D-3：区块B 行业下拉动态填充（stock_master distinct industry_csric2 + 名称）。
  // 三态：加载（select 禁用）/ 正常（选项 = code + 名称）/ 空库（保留"全部行业"占位 + 空态文案）。
  const sel = $("#lake-industry-filter");
  const hint = $("#lake-industry-hint");
  const prev = sel.value;
  sel.disabled = true;
  try {
    const d = await api("/api/lake/industries");
    const list = d.industries || [];
    if (!list.length) {
      // 空态：库未灌入 / 无行业数据 → 保持占位 + 提示（不误导用户以为有筛选）
      sel.innerHTML = '<option value="">全部行业</option>';
      hint.textContent = "（暂无行业数据）";
      hint.classList.remove("hidden");
    } else {
      let h = '<option value="">全部行业</option>';
      for (const it of list) {
        const label = it.name ? `${it.code} ${it.name}` : it.code;
        h += `<option value="${esc(it.code)}">${esc(label)}</option>`;
      }
      sel.innerHTML = h;
      hint.classList.add("hidden");
    }
    if (list.some((it) => it.code === prev)) sel.value = prev;  // 保留已有选择（若仍存在）
  } catch (e) {
    // 错误态：红横幅（与其他 lake 端点一致），下拉回退占位不阻塞浏览；
    // v6.0.4：灌数持锁 → 中性处理，不弹红横幅（琥珀色块已说明原因）
    if (!lakeIsBackfillErr(e)) lakeSetError(e.message);
    sel.innerHTML = '<option value="">全部行业</option>';
  } finally {
    sel.disabled = false;
  }
}

async function loadLakeMarket() {
  const table = $("#lake-market-table");
  const ind = $("#lake-industry-filter").value;
  const soe = $("#lake-soe-filter").value;
  const sort = $("#lake-sort-select").value;
  let qs = `page=${lakeMarketPage}&sort=${sort}`;
  if (ind) qs += "&industry=" + encodeURIComponent(ind);
  if (soe) qs += "&soe=" + soe;
  table.innerHTML = '<tr><td colspan="8" class="lake-loading-label">加载中…</td></tr>'; // 表格首行 spinner
  try {
    const d = await api("/api/lake/market?" + qs);
    renderLakeMarket(d);
  } catch (e) {
    // v6.0.4：灌数持锁 → 中性占位（不弹红横幅，顶部琥珀色块已说明原因）
    if (lakeIsBackfillErr(e)) {
      table.innerHTML = '<tr><td colspan="8" class="placeholder">⏳ 数据灌入中，全市场查询暂不可用——稍后刷新</td></tr>';
      $("#lake-market-count").textContent = "";
    } else {
      lakeSetError(e.message);
      table.innerHTML = '<tr><td colspan="8" class="placeholder">加载失败</td></tr>';
    }
  }
}

function renderLakeMarket(d) {
  const table = $("#lake-market-table");
  let h = "<thead><tr>" +
    "<th>代码</th><th>名称</th><th>行业</th><th class='num'>总市值(亿)</th>" +
    "<th class='num'>PE</th><th class='num'>PB</th><th class='num'>股息率%</th><th>央国企</th>" +
    "</tr></thead><tbody>";
  if (!d.rows || !d.rows.length) {
    // 空态：全市场 0 行
    h += '<tr><td colspan="8" class="placeholder">数据湖尚未灌入数据，请先运行 backfill</td></tr>';
  } else {
    for (const r of d.rows) {
      h += `<tr class="clickable" data-ts="${esc(r.ts_code)}">` +
        `<td><span class="sr-code">${esc(r.ts_code)}</span></td>` +
        `<td>${esc(r.name || "—")}</td><td>${esc(r.industry_name || "—")}</td>` +
        `<td class="num">${lakeFmt(r.total_mv, "num")}</td>` +
        `<td class="num">${lakeFmt(r.pe_ttm, "num")}</td>` +
        `<td class="num">${lakeFmt(r.pb, "num")}</td>` +
        `<td class="num">${lakeFmt(r.ttm_yield_pct, "pct")}</td>` +
        `<td>${r.soe_flag === "央国企" ? '<span class="bdg bdg-ind">央国企</span>' : "—"}</td></tr>`;
    }
  }
  h += "</tbody>";
  table.innerHTML = h;
  $("#lake-market-count").textContent = `共 ${d.total} 只 · 第 ${d.page}/${Math.max(1, d.pages)} 页`;
  // 行点击 → 个股全景
  table.querySelectorAll("tr.clickable").forEach((tr) =>
    tr.addEventListener("click", () => {
      selectLakeStock(tr.dataset.ts);
      $("#lake-stock-card").scrollIntoView({ behavior: "smooth", block: "start" });
    }));
  // 分页
  const pager = $("#lake-market-pager");
  pager.innerHTML = "";
  const mkBtn = (label, page, disabled, cur) => {
    const b = el("button", {}, label);
    if (disabled) b.disabled = true;
    if (cur) b.classList.add("cur");
    if (!disabled && !cur) b.addEventListener("click", () => { lakeMarketPage = page; loadLakeMarket(); });
    return b;
  };
  pager.append(mkBtn("«", 1, d.page <= 1), mkBtn("‹", d.page - 1, d.page <= 1));
  pager.append(document.createTextNode(` ${d.page} / ${Math.max(1, d.pages)} `));
  pager.append(mkBtn("›", d.page + 1, d.page >= d.pages), mkBtn("»", d.pages, d.page >= d.pages));
}

// ---------------------------------------------------------------------------
// v6.0.5：区块 C「数据库状态 / 补齐进度」渲染（汇总条 + 9表清单 + 视图区）
// ---------------------------------------------------------------------------
// state → 徽章 class/文案（绿 fresh / 黄 lagging / 蓝 pending / 灰 empty）。
// 复用现有 .badge 基类 + CSS 变量体系（style.css v6.0.5 段新增 lake-st-* 颜色）。
const LAKE_STATE_BADGE = {
  fresh: ["lake-st-fresh", "✅ 最新"],
  lagging: ["lake-st-lagging", "🕓 滞后"],
  pending: ["lake-st-pending", "⏳ 待补"],
  empty: ["lake-st-empty", "— 空"],
};

// 状态徽章 + 副文案（state_detail）。detail 与徽章词完全相同 → 不重复显示；
// detail 以"徽章词 + 空格"开头（如"滞后 3 日" vs 徽章"🕓 滞后"）→ 只留剩余部分。
function lakeStateCell(t) {
  const [cls, label] = LAKE_STATE_BADGE[t.state] || ["lake-st-empty", String(t.state ?? "—")];
  let detail = t.state_detail || "";
  const word = label.replace(/^[^\s]+\s*/, "");   // 去掉 emoji，留状态词（最新/滞后/待补/空）
  if (detail === word) detail = "";
  else if (word && detail.startsWith(word + " ")) detail = detail.slice(word.length).trim();
  return `<span class="badge ${cls}">${esc(label)}</span>` +
    (detail ? ` <span class="muted small">${esc(detail)}</span>` : "");
}

// 汇总条（brief §2.1）：N 表 + N 视图 · 库 X MB · DuckDB x.y.z · 最后同步 MM-DD HH:MM · [总体徽章]
function lakeRenderSummary(d) {
  const box = $("#lake-summary");
  // 总体徽章：灌数中→⏳琥珀 / 任一 lagging→🕓琥珀 / 全 fresh+pending→✅绿
  let badge;
  if (d.backfill_in_progress) badge = '<span class="badge lake-st-lagging">⏳ 灌数中</span>';
  else if ((d.tables || []).some((t) => t.state === "lagging")) badge = '<span class="badge lake-st-lagging">🕓 有滞后</span>';
  else badge = '<span class="badge lake-st-fresh">✅ 正常</span>';

  const parts = [];
  if (d.initialized === false) {
    // 未初始化空态（v6.0.3 语义保留）：不显示"正常/滞后"徽章，明确提示 init
    box.innerHTML = `<span class="muted">库未初始化 —— 请先运行 <code>python scripts/lake_backfill.py init</code></span>`;
    return;
  }
  const nTables = (d.tables || []).length, nViews = (d.views || []).length;
  if (nTables) parts.push(`${nTables} 表` + (nViews ? ` + ${nViews} 视图` : ""));
  if (d.db && d.db.size_mb != null) parts.push(`库 ${d.db.size_mb} MB`);
  parts.push(`DuckDB ${esc(d.duckdb_version || "?")}`);
  const ts = (d.sync && d.sync.last_updated_at) || d.updated_at;
  parts.push(`最后同步 ${ts ? esc(String(ts).slice(5, 16)) : "—"}`);
  box.innerHTML = parts.map((p) => `<span>${p}</span>`).join('<span class="lake-summary-sep">·</span>') + ` ${badge}`;
}

// 表清单（brief §2.2）：9 行固定顺序；零数据 P2/P3 表 muted 弱化（"计划内未做" ≠ 错误）。
function lakeRenderTables(d) {
  const table = $("#lake-tables");
  const th = "<thead><tr><th>表</th><th>说明</th><th class='num'>行数</th><th class='num'>股票数</th>" +
    "<th>数据区间</th><th>状态</th><th>最后同步</th></tr></thead>";
  const tables = d.tables || [];
  if (!tables.length) {
    // locked（灌数中，tables 缺省）或降级：占位行（不白屏、不显示成错误）
    table.innerHTML = th + '<tbody><tr><td colspan="7" class="placeholder">' +
      (d.backfill_in_progress ? "灌数进行中 —— 表级状态完成后可见" : "暂无表级状态") +
      "</td></tr></tbody>";
    return;
  }
  let h = th + "<tbody>";
  for (const t of tables) {
    const muted = t.rows <= 0 ? " lake-row-muted" : "";   // 零数据表视觉弱化（P2/P3）
    const range = (t.date_min && t.date_max) ? `${esc(t.date_min)} ~ ${esc(t.date_max)}` : "—";
    h += `<tr class="lake-table-row${muted}">` +
      `<td><div>${esc(t.name_cn)}</div><div class="muted small mono">${esc(t.tier || "")} · ${esc(t.key)}</div></td>` +
      `<td class="lake-t-desc"><span class="muted small">${esc(t.desc || "—")}</span></td>` +
      `<td class="num">${t.rows != null ? t.rows.toLocaleString("en-US") : "—"}</td>` +
      `<td class="num">${t.codes != null ? t.codes.toLocaleString("en-US") : "—"}</td>` +
      `<td class="mono small">${range}</td>` +
      `<td class="lake-t-state">${lakeStateCell(t)}</td>` +
      `<td class="muted small mono">${t.last_sync_at ? esc(String(t.last_sync_at).slice(5, 16)) : "—"}</td></tr>`;
  }
  table.innerHTML = h + "</tbody>";
}

// 视图区（brief §2.3）：3 view 小字行 + 复权因子覆盖进度条（<5% 加提示）。
function lakeRenderViews(d) {
  const box = $("#lake-views");
  const views = d.views || [];
  if (!views.length) { box.innerHTML = ""; return; }   // locked/降级态：不显示视图区
  let h = '<div class="lake-views-title muted small">派生视图</div>';
  for (const v of views) {
    h += `<div class="lake-view-row"><span class="mono small">${esc(v.key)}</span> ` +
      `<b>${esc(v.name_cn)}</b> <span class="muted small">—— ${esc(v.desc || "")}</span></div>`;
  }
  const pct = d.adj_factor_coverage_pct;
  if (pct != null) {
    const w = Math.max(0, Math.min(100, Number(pct)));
    h += `<div class="lake-af-line"><span class="muted small">复权因子覆盖 ${w.toFixed(1)}%</span>` +
      `<span class="progress-track lake-af-track"><span class="progress-bar" style="width:${w}%"></span></span>` +
      (w < 5 ? '<span class="lake-af-hint muted small">history 补齐后 hfq/qfq 全量可用</span>' : "") +
      `</div>`;
  }
  box.innerHTML = h;
}

// ---------------------------------------------------------------------------
// v6.0.9 同步控制区（启动/停止全史补库）：按钮二态由 /status 的
// backfill_in_progress / lock_holder_pid 驱动（v6.0.4 三态机制，不新增 GET 端点）。
// 3s 轮询复用 loadLakeStatus（数据湖 tab 激活期间持续；离开 tab 停止）。
// v6.0.10：按钮点击即**锁定**（disabled + 置灰），直到成功/失败才释放——
//   停止：POST /sync/stop 异步返回 waiting_task=true 后进入"收尾轮询"（1s），
//     等 /status backfill_in_progress=false → toast"已停止，进度已保存"+ 释放；
//     >90s 未停 → 文案变"当前任务收尾中，最长约几分钟"；5min 硬超时 → toast + 释放。
//   启动：POST /sync/start 返回前锁定（200→成功态 / 409→toast+释放）。
//   任一进行中另一个也禁用（防 start/stop 竞态）。零新库，复用 toast/轮询/CSS 变量。
// ---------------------------------------------------------------------------
let lakeSyncPollTimer = null;        // status 轮询定时器（tab 激活期间；停止期 1s）
let lakeSyncPollIv = null;           // v6.0.10：当前定时器间隔（状态切换时按需重建）
let lakeSyncRunningSince = null;     // 本会话首次观察到 running 的时间戳（已耗时口径）
let lakeSyncStarting = false;        // v6.0.10：启动进行中（POST /start 在途）
let lakeSyncStopping = null;         // v6.0.10：{since, pid} | null（停止收尾中）
let lakeSyncLastPid = null;          // v6.0.10：最近一次 /status 的 lock_holder_pid
                                     // （stopping=true 时 meta 不显示 PID——点击侧从这取，
                                     //  避免按钮显示"⏹ 停止中… (未知)"）

const LAKE_STOP_SOFT_MS = 90 * 1000;    // >90s 未停 → 文案升级"当前任务收尾中"
const LAKE_STOP_HARD_MS = 5 * 60 * 1000; // 5min 硬超时 → toast + 释放（不无限锁死）

function lakeFmtElapsed(ms) {
  const m = Math.floor(ms / 60000);
  if (m < 1) return "<1分钟";
  if (m < 60) return m + "分钟";
  return Math.floor(m / 60) + "小时" + (m % 60) + "分";
}

function _lakeSyncPollInterval() {
  // v6.0.10：停止收尾期间提到 1s（更快感知 backfill_in_progress=false），其余 3s
  return lakeSyncStopping ? 1000 : 3000;
}

function startLakeSyncPoll() {
  // v6.0.10：间隔按状态取（停止期 1s，其余 3s）。已有定时器且间隔不同 → 重建
  // （停止开始/结束时切换快慢轮询）；同间隔不重建（避免重复触发漂移）。
  const iv = _lakeSyncPollInterval();
  if (lakeSyncPollTimer) {
    if (lakeSyncPollIv !== iv) {
      clearInterval(lakeSyncPollTimer);
      lakeSyncPollTimer = setInterval(loadLakeStatus, iv);
      lakeSyncPollIv = iv;
    }
    return;
  }
  lakeSyncPollIv = iv;
  lakeSyncPollTimer = setInterval(loadLakeStatus, iv);   // 复用现有 status 拉取
}

function stopLakeSyncPoll() {
  if (lakeSyncPollTimer) { clearInterval(lakeSyncPollTimer); lakeSyncPollTimer = null; }
  lakeSyncPollIv = null;
}

// d = /status 响应（null=错误态）。按钮态优先级：
//   锁定中（starting/stopping）> running → [⏹ 停止同步 (pid)] > idle → [▶ 启动同步]。
function lakeRenderSyncControl(d) {
  const btn = $("#btn-lake-sync-toggle");
  const meta = $("#lake-sync-meta");
  if (!btn) return;

  // v6.0.10：锁定态（启动/停止进行中）——**不被轮询覆盖**（按钮保持禁用 + 置灰，
  // 直到成功或失败才释放；错误态 d=null 也不解锁，靠硬超时兜底）。两按钮是同一个
  // toggle，"任一进行中另一个也禁用"= 锁定期间本按钮恒 disabled。
  if (lakeSyncStarting) {
    btn.disabled = true;
    btn.classList.add("sync-busy");
    btn.innerHTML = "▶ 启动中…";
    meta.textContent = "正在启动（确认进程拉起中）";
    return;
  }
  if (lakeSyncStopping) {
    const pid = lakeSyncStopping.pid != null ? lakeSyncStopping.pid : "未知";
    const elapsed = Date.now() - lakeSyncStopping.since;
    // v6.0.10：停止成功确认——backfill_in_progress=false（进程已退出、锁释放）→
    // toast"已停止，进度已保存"+ 立即释放回 [▶ 启动同步]（**同步完成**，不依赖
    // 异步 loadLakeStatus 的下一轮渲染；错误态 d=null 时不进此分支，靠硬超时兜底）。
    if (d && d.installed && d.backfill_in_progress !== true) {
      lakeSyncStopping = null;
      btn.disabled = false;
      btn.classList.remove("sync-busy");
      toast("已停止，进度已保存");
      lakeRenderSyncControl(d);   // 按真实状态重渲染（此刻必为 idle → 启动按钮）
      return;
    }
    btn.disabled = true;
    btn.classList.add("sync-busy");
    // >90s 未停：文案升级（当前任务收尾中，最长约几分钟）；继续轮询直到成功或硬超时
    if (elapsed > LAKE_STOP_SOFT_MS) {
      btn.innerHTML = "⏹ 停止中…（当前任务收尾中，最长约几分钟）";
    } else {
      btn.innerHTML = `⏹ 停止中… (${pid})`;
    }
    meta.textContent = `PID ${pid} · 已等待 ${lakeFmtElapsed(elapsed)}`;
    if (elapsed > LAKE_STOP_HARD_MS && d && d.installed) {
      // 5min 硬超时：释放按钮 + toast（进程可能仍在收尾，刷新可查真实状态）——
      // 同步按真实状态重渲染（running→恢复停止入口 / idle→启动按钮），不依赖异步轮询。
      lakeSyncStopping = null;
      btn.classList.remove("sync-busy");
      toast("停止超时，进程可能仍在收尾，请刷新查看", false);
      lakeRenderSyncControl(d);
    }
    return;
  }

  // 错误态（5xx/duckdb 未装）：禁用按钮不白屏（保留上次文案的占位）。
  // v6.0.10：**锁定期间 d=null 不得覆盖锁定态**（网络抖动 ≠ 停止失败——保持
  // disabled + "停止中…"，靠 5min 硬超时兜底释放；否则一次轮询失败就解锁，
  // 用户会连点多次 stop，正是本次要修的体验问题）。
  if (!d) {
    if (lakeSyncStarting || lakeSyncStopping) return;   // 锁定态保持（不覆盖文案）
    btn.disabled = true; btn.innerHTML = '<span class="muted">状态不可用</span>'; meta.textContent = ""; return;
  }
  if (!d.installed) { btn.disabled = true; btn.innerHTML = '<span class="muted">数据湖未安装</span>'; meta.textContent = ""; return; }

  const running = d.backfill_in_progress === true;

  // 会话内观察到 false→true 跃迁才记"已耗时"起点（中途进页面不知真实启动时刻，
  // 改显示进度更新时间——不猜、不误导）
  if (running && !lakeSyncRunningSince) lakeSyncRunningSince = Date.now();
  else if (!running) { lakeSyncRunningSince = null; lakeSyncLastPid = null; }

  btn.disabled = false;
  if (running) {
    const pid = d.lock_holder_pid != null ? d.lock_holder_pid : "未知";
    // v6.0.10：记住最近一次 holder pid（stopping=true 时 meta 不显示 PID，点击侧
    // 停止按钮文案需要它——避免"⏹ 停止中… (未知)"）
    if (d.lock_holder_pid != null) lakeSyncLastPid = d.lock_holder_pid;
    // v6.0.10：stopping=true（SIGTERM 已发、当前任务收尾中）→ meta 提示友好文案，
    // 按钮仍是停止入口（再点一次=重复发信号，runner 幂等忽略；锁定由点击侧负责）
    const stopping = d.stopping === true;
    // 运行中提示：今日配额 x/5000（progress tasks 视图 quota，max 防御滞后）+ 耗时
    let qUsed = null, qBudget = null;
    for (const t of (d.tasks || [])) {
      if (!t) continue;
      if (typeof t.quota_used_today === "number") qUsed = Math.max(qUsed ?? 0, t.quota_used_today);
      if (typeof t.quota_budget === "number") qBudget = Math.max(qBudget ?? 0, t.quota_budget);
    }
    const parts = [stopping ? "⏹ 停止收尾中（当前任务完成后退出）" : "PID " + pid];
    if (qUsed != null) parts.push(`今日配额 ${qUsed}/${qBudget ?? "—"}`);
    if (lakeSyncRunningSince) parts.push("已耗时 " + lakeFmtElapsed(Date.now() - lakeSyncRunningSince));
    else if (d.updated_at) parts.push("进度更新于 " + String(d.updated_at).slice(5, 16));
    meta.textContent = parts.join(" · ");
    btn.innerHTML = `⏹ 停止同步 (${pid})`;
    btn.onclick = lakeSyncStop;
  } else {
    meta.textContent = "";
    btn.innerHTML = "▶ 启动同步";
    btn.onclick = lakeSyncStart;
  }
}

async function lakeSyncStart() {
  const btn = $("#btn-lake-sync-toggle");
  if (!confirm("将启动全史数据补库（后台长跑，每日配额 5000 到顶自停）。确认启动？")) return;
  // v6.0.10：点击即锁定——POST /start 返回前按钮禁用 + "▶ 启动中…"（防连点/竞态）
  lakeSyncStarting = true;
  btn.disabled = true;
  btn.classList.add("sync-busy");
  btn.innerHTML = "▶ 启动中…";
  try {
    const d = await api("/api/lake/sync/start", { method: "POST" });
    if (d.started) toast(`同步已启动（PID ${d.pid ?? "?"}）`);
    else toast("同步启动失败：" + (d.reason || "未知原因"), false);
  } catch (e) {
    // 409（已有灌数在跑，状态可能刚变化）→ toast 提示不白屏
    if (e.status === 409 && e.body && e.body.hint) toast(e.body.hint, false);
    else toast("同步启动失败：" + e.message, false);
  } finally {
    // v6.0.10：POST 返回即释放（200→成功态 / 409→toast+释放）——下一轮渲染按真实
    // /status 恢复二态（started→running 显示停止按钮；失败→启动按钮）
    lakeSyncStarting = false;
    btn.classList.remove("sync-busy");
    loadLakeStatus();   // 立即刷新按钮态（不等下一轮 3s）
  }
}

async function lakeSyncStop() {
  const btn = $("#btn-lake-sync-toggle");
  if (!confirm("停止后进度已保存，下次启动自动续传。确认停止？")) return;
  // v6.0.10：点击瞬间锁定——disabled + "⏹ 停止中… (pid)" + 置灰（.sync-busy），
  // 直到成功或失败才释放（防连点多次 stop、给即时反馈）。pid 优先从 meta 解析
  // （正常运行态 meta="PID x · …"）；stopping=true 时 meta 不显示 PID → 回退
  // 轮询记住的最近 lock_holder_pid（避免按钮显示"(未知)"）。
  const pid = $("#lake-sync-meta")?.textContent.match(/PID\s+(\d+)/)?.[1]
    ?? (lakeSyncLastPid != null ? String(lakeSyncLastPid) : null);
  lakeSyncStopping = { since: Date.now(), pid: pid ? Number(pid) : null };
  btn.disabled = true;
  btn.classList.add("sync-busy");
  btn.innerHTML = `⏹ 停止中… (${pid ?? "未知"})`;
  startLakeSyncPoll();   // v6.0.10：收尾期间提到 1s 轮询（复用 loadLakeStatus）
  try {
    const d = await api("/api/lake/sync/stop", { method: "POST" });
    if (d.waiting_task) {
      // 后端异步语义：信号已发、正在等当前任务收尾 → 保持锁定，轮询 /status 判完成
      toast("停止信号已发送，等待当前任务收尾…");
    } else if (d.reason) {
      // 信号未发出（holder pid 未知/发送失败）→ 释放 + toast
      lakeSyncStopping = null;
      btn.classList.remove("sync-busy");
      toast("同步停止失败：" + d.reason, false);
    }
  } catch (e) {
    if (e.status === 409 && e.body && e.body.hint) {
      // 409 sync_not_running（状态刚变化）→ 释放 + toast
      lakeSyncStopping = null;
      btn.classList.remove("sync-busy");
      toast(e.body.hint, false);
    } else {
      toast("同步停止失败：" + e.message, false);
      // 网络错误：保持锁定继续轮询（后端可能已收到信号；硬超时兜底释放）
    }
  } finally {
    loadLakeStatus();   // 立即按真实状态渲染（锁定态下不覆盖按钮，见 lakeRenderSyncControl）
  }
}

async function loadLakeStatus() {
  const table = $("#lake-tasks-table");
  try {
    const d = await api("/api/lake/status");
    lakeInstalled = !!d.installed;
    if (!d.installed) {
      lakeSetError("duckdb 未安装（uv sync --extra lake）");
      $("#lake-summary").innerHTML = '<span class="muted">数据湖未安装</span>';
      table.innerHTML = '<tr><td colspan="6" class="placeholder">数据湖不可用</td></tr>';
      return;
    }
    lakeSetError("");   // 正常态：清错误横幅
    // v6.0.4 三态渲染（互不串味）：
    //   ① backfill_in_progress=true → 琥珀色"⏳ 数据灌入中"块 + tasks 摘要；
    //      coverage 来自 progress 文件降级，区块C 汇总条显示"灌数中"徽章。
    //   ② initialized=false（真未初始化）→ 空态文案（"请先运行 backfill init"）。
    //   ③ 正常 → v6.0.5 汇总条 + 9表清单 + 视图区 + tasks 表。
    lakeSetBackfill(d);
    lakeRenderSyncControl(d);   // v6.0.9：同步控制按钮二态（3s 轮询驱动）
    lakeRenderSummary(d);
    lakeRenderTables(d);
    lakeRenderViews(d);
    // tasks 表（灌数中态与正常态共用同一渲染；灌数中时顶部琥珀块另有摘要副本）
    lakeRenderTasksTable(table, d.tasks || []);
  } catch (e) {
    // 错误态：5xx / duckdb 未装 → 红色横幅 + 降级占位（不白屏）
    lakeSetError(e.message);
    lakeRenderSyncControl(null);   // v6.0.9：按钮禁用占位（不白屏）
    $("#lake-summary").innerHTML = '<span class="muted">—</span>';
    $("#lake-tables").innerHTML =
      '<tbody><tr><td colspan="7" class="placeholder">数据湖不可用</td></tr></tbody>';
    $("#lake-views").innerHTML = "";
    table.innerHTML = '<tr><td colspan="6" class="placeholder">数据湖不可用</td></tr>';
  }
}

function onLakeTab() {
  if (!lakeLoaded) {
    lakeLoaded = true;
    loadLakeStatus();
    loadLakeIndustries();   // v6.0.1 D-3：区块B 行业下拉动态填充
    loadLakeMarket();
  }
}

// ---------------------------------------------------------------------------
// 启动
// ---------------------------------------------------------------------------
document.addEventListener("DOMContentLoaded", () => {
  initTabs();
  initStrategyButtons();
  initRunPage();
  initStockModal();
  initLakePage();
  $("#btn-refresh-runs").addEventListener("click", loadRuns);
  loadRuns();
});
