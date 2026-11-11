// API client（加密货币系统）—— 与股票前端 api/client.js 同语义。
export async function api(path, opts = {}) {
  const res = await fetch(path, {
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
