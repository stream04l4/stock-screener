// 格式化工具 —— 逐字移植 web/static/app.js（行为一致性基准）。

// fmtNum：NULL/空/NaN → "—"；非数值原样字符串化；数值 toFixed(nd)。
export function fmtNum(v, nd = 2) {
  if (v === null || v === undefined || v === "" || (typeof v === "number" && isNaN(v))) return "—";
  const n = Number(v);
  if (isNaN(n)) return String(v);
  return n.toFixed(nd);
}

// lakeFmt：数据湖页格式约定（v6，报告 §5）：NULL → "—"；pct 带 %；yi 带"亿"；num 两位小数。
export function lakeFmt(v, kind) {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  if (isNaN(n)) return String(v);
  // v8.2.0 补（Joel 2026-11-20 全局格式统一）：百分比统一 1 位小数
  if (kind === "pct") return n.toFixed(1) + "%";
  if (kind === "yi") return n.toFixed(1) + "亿";
  if (kind === "num") return n.toFixed(2);
  return String(v);
}

// ---------------------------------------------------------------------------
// v8.2.0 补（Joel 2026-11-20 全局数据格式统一）——语义格式化单点定义：
//   价格（现价/成本/单价/触发线）= 2 位小数；百分比 = 1 位小数 + %；
//   手数/股数 = 整数无小数。金额（¥/万）不属三规则，走 fmtMoney（千分位 2 位）。
// 全前端数值展示一律经本文件函数（禁裸 toFixed 散落）。
// ---------------------------------------------------------------------------
export function fmtPrice(v) {
  return fmtNum(v, 2);
}

export function fmtPct(v) {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  if (isNaN(n)) return String(v);
  return n.toFixed(1) + "%";
}

export function fmtInt(v) {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  if (isNaN(n)) return String(v);
  return String(Math.round(n));
}

export function fmtMoney(v) {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  if (isNaN(n)) return String(v);
  return n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
