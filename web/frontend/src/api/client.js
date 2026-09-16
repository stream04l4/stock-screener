// API client —— 逐字移植 web/static/app.js 的 api() 语义（行为一致性基准）。
// - 非 2xx：抛 Error，err.status = HTTP 状态码，err.body = 解析后的 JSON body（非 JSON → null）；
//   message = body.detail（字符串原样 / 对象 JSON.stringify），无 detail 时回退 res.statusText。
// - isBackfillErr：v6.0.4 契约——409 + body.error === "lake_backfill_in_progress"
//   （灌数进程持 DuckDB 独占写锁）→ 调用方中性处理，不弹红色错误横幅。全局复用。

export async function api(path, opts = {}) {
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

export function isBackfillErr(e) {
  return !!(e && e.status === 409 && e.body && e.body.error === "lake_backfill_in_progress");
}
