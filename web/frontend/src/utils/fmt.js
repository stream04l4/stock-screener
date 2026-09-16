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
  if (kind === "pct") return n.toFixed(2) + "%";
  if (kind === "yi") return n.toFixed(1) + "亿";
  if (kind === "num") return n.toFixed(2);
  return String(v);
}
