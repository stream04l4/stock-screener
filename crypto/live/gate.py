"""Gate.io 模拟盘（testnet）API v4 最小客户端 —— 纯 stdlib。

模拟盘专用（v8.0.7）：
- base 固定 ``https://api-testnet.gateapi.io/api/v4``（Gate 官方模拟盘入口），
  代码层拒绝任何其他 base，防止误下实盘单；
- Key 从仓库根 ``.env`` 读（``GATE_API_KEY`` / ``GATE_API_SECRET``），
  只读用、不回显、不写盘、不进日志；
- 验签 = HexEncode(HMAC-SHA512(secret, sign_string))，
  sign_string = "METHOD\\n/api/v4/path\\nquery\\nsha512(body)\\n" + 秒级时间戳。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

TESTNET_BASE = "https://api-testnet.gateapi.io/api/v4"
_TIMEOUT_S = 15


class GateError(Exception):
    """Gate API 错误（label/message 来自响应体，status = HTTP 状态码）。"""

    def __init__(self, label: str, message: str, status: int | None = None):
        super().__init__(f"{label}: {message}")
        self.label = label
        self.message = message
        self.status = status


def _load_keys() -> tuple[str, str]:
    """从仓库根 .env 读 GATE_API_KEY / GATE_API_SECRET（缺失返回空串）。"""
    env: dict[str, str] = {}
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env.get("GATE_API_KEY", ""), env.get("GATE_API_SECRET", "")


def _request(method: str, path: str, body: dict | None = None,
             params: dict | None = None) -> Any:
    """带签名的 REST 调用（base 锁定 testnet；path 形如 /spot/accounts）。

    params → URL query（按 key 排序 urlencode）；签名字符串 =
    METHOD\\n/api/v4+path\\nquery_string\\nsha512(body)\\n秒级时间戳。
    """
    from urllib.parse import urlencode
    key, secret = _load_keys()
    if not key or not secret:
        raise GateError("NO_KEY", ".env 未配置 GATE_API_KEY/GATE_API_SECRET")
    if not path.startswith("/"):
        path = "/" + path
    qs = urlencode(sorted(params.items())) if params else ""
    full_path = path + (f"?{qs}" if qs else "")
    payload = json.dumps(body).encode("utf-8") if body is not None else b""
    ts = str(int(time.time()))
    hashed_payload = hashlib.sha512(payload).hexdigest()
    # 验签用完整 URL path（含 /api/v4 前缀）+ 排序后的 query_string
    sign_str = f"{method}\n/api/v4{path}\n{qs}\n{hashed_payload}\n{ts}"
    sign = hmac.new(secret.encode(), sign_str.encode(),
                    hashlib.sha512).hexdigest()
    req = urllib.request.Request(
        TESTNET_BASE + full_path,
        data=payload if payload else None,
        method=method,
        headers={
            "KEY": key,
            "Timestamp": ts,
            "SIGN": sign,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        label, msg = "HTTP", ""
        try:
            d = json.loads(e.read().decode("utf-8")[:500])
            label, msg = d.get("label", "HTTP"), d.get("message", "")
        except Exception:  # noqa: BLE001
            msg = e.read().decode("utf-8", "replace")[:200]
        raise GateError(label, msg, e.code) from None


def spot_accounts():
    """现货账户各币余额（list[currency/available/locked]）。"""
    return _request("GET", "/spot/accounts")


def market_buy(pair: str, amount: str) -> dict:
    """市价买入（amount = 基币数量，字符串保精度）。返回订单对象。"""
    return _request("POST", "/spot/orders", {
        "currency_pair": pair,
        "side": "buy",
        "type": "market",
        "amount": str(amount),
    })


def status() -> dict:
    """轻量连通性探测 → {connected, detail, btc_avail, usdt_avail}。"""
    try:
        accts = spot_accounts() or []
        avail = {c["currency"]: float(c.get("available") or 0)
                 for c in accts if c.get("currency") in ("BTC", "USDT")}
        return {"connected": True, "detail": "模拟盘 API 已连通",
                "btc_avail": avail.get("BTC"), "usdt_avail": avail.get("USDT")}
    except GateError as e:
        return {"connected": False, "detail": f"{e.label}: {e.message}",
                "btc_avail": None, "usdt_avail": None}
    except Exception as e:  # noqa: BLE001 网络异常等
        return {"connected": False, "detail": f"连接失败: {e}",
                "btc_avail": None, "usdt_avail": None}
