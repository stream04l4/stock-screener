// API client（加密货币系统）—— 与股票前端 api/client.js 同语义。
// 443 单域名架构下 API 一律走 /crypto 前缀（nginx 按前缀透传到 :9091，
// 后端原生带前缀，直连 :9091 与经 nginx 行为一致）。
const API_BASE = "/crypto";

export async function api(path, opts = {}) {
  const res = await fetch(API_BASE + path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  let body = null;
  try { body = await res.json(); } catch { /* 非 JSON */ }
  if (!res.ok) {
    if (res.status === 401 && !path.startsWith("/api/login")
        && !path.startsWith("/api/auth/me")) {
      window.dispatchEvent(new CustomEvent("auth:401"));
    }
    const detail = body && body.detail ? body.detail : res.statusText;
    const err = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    err.status = res.status;
    err.body = body;
    throw err;
  }
  return body;
}
