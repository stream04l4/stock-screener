# -*- coding: utf-8 -*-
"""web.auth —— 登录/会话（v7.0.14，纯标准库，零新依赖）。

机制：
- 凭据来自项目根 .env（gitignore，禁入 git/日志）：
  WEB_ADMIN_USER / WEB_ADMIN_PASS（缺失 → 登录端点 500 显式失败，不静默放行）；
  WEB_SESSION_SECRET 缺失时用进程内随机值（重启后旧 cookie 失效，属预期安全行为）。
- 会话 token = `v1.{user}.{exp}.{b64url(hmac_sha256(secret, "v1.{user}.{exp}"))}`，
  无状态签名 cookie（HttpOnly / SameSite=Lax / Max-Age=7 天）；重启不失效。
- 防爆破：按 IP 计数，5 次失败锁 120s（进程内，重启清零——单用户内网场景足够）。

接口：make_token / parse_token / check_credentials / 失败计数（login_attempts）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

COOKIE_NAME = "ssc_session"
TOKEN_TTL_S = 7 * 24 * 3600          # 7 天
_MAX_FAILS = 5
_LOCK_S = 120
_ATTEMPTS_MAX_KEYS = 2048

# 进程内：IP -> [fail_count, lock_until_epoch, last_seen_epoch]
_attempts: Dict[str, list] = {}
# 进程内惰性随机密钥兜底（仅当 .env 与环境变量均未配 WEB_SESSION_SECRET 时使用，
# 进程内保持恒定，避免每次请求重新生成导致登录恒 401）
_FALLBACK_SECRET: Optional[str] = None
# .env 解析缓存：(path, mtime_ns, size) -> dict，避免 async 中间件每请求同步读盘
_ENV_CACHE: Dict[str, Tuple[Optional[Tuple[int, int]], Dict[str, str]]] = {}


def _env_path() -> str:
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")


def _read_env(path: str) -> Dict[str, str]:
    """解析 .env（带 mtime+size 缓存；测试 monkeypatch 文件或路径即时感知）。"""
    try:
        st = os.stat(path)
        sig: Optional[Tuple[int, int]] = (st.st_mtime_ns, st.st_size)
    except OSError:
        sig = None
    cached = _ENV_CACHE.get(path)
    if cached is not None and cached[0] == sig:
        return cached[1]
    out: Dict[str, str] = {}
    if sig is not None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    out[k.strip()] = v.strip().strip('"').strip("'")
        except OSError:
            pass
    _ENV_CACHE[path] = (sig, out)
    return out


def load_auth_config() -> Tuple[Optional[str], Optional[str], str]:
    """返回 (user, pass, secret)；user/pass 允许 None（→ 登录端点 500）；secret 恒有且进程内稳定。"""
    global _FALLBACK_SECRET
    env_file = _read_env(_env_path())
    user = os.environ.get("WEB_ADMIN_USER") or env_file.get("WEB_ADMIN_USER")
    pw = os.environ.get("WEB_ADMIN_PASS") or env_file.get("WEB_ADMIN_PASS")
    secret = os.environ.get("WEB_SESSION_SECRET") or env_file.get("WEB_SESSION_SECRET")
    if not secret:
        if _FALLBACK_SECRET is None:
            _FALLBACK_SECRET = secrets.token_hex(32)
        secret = _FALLBACK_SECRET
    return user, pw, secret


def _b64e(s: bytes) -> str:
    return base64.urlsafe_b64encode(s).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_token(user: str, secret: str, now: Optional[float] = None) -> str:
    exp = int((now or time.time()) + TOKEN_TTL_S)
    # 含 '.' 的用户名做 base64url 编码，防止 split('.') 误拆分；普通用户名保持原样兼容
    enc_user = f"b64u:{_b64e(user.encode('utf-8'))}" if "." in user else user
    payload = f"v1.{enc_user}.{exp}"
    sig = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
    return f"{payload}.{_b64e(sig)}"


def parse_token(token: Optional[str], secret: str, now: Optional[float] = None) -> Optional[str]:
    """有效 → 用户名；无效/过期 → None（任何异常都归一为 None，不炸请求）。"""
    if not token:
        return None
    parts = token.split(".")
    if len(parts) != 4 or parts[0] != "v1":
        return None
    raw_user, exp_s, sig_b64 = parts[1], parts[2], parts[3]
    if not raw_user or not exp_s.isdigit():
        return None
    if int(exp_s) < (now or time.time()):
        return None
    payload = f"v1.{raw_user}.{exp_s}"
    expect = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
    try:
        given = _b64d(sig_b64)
    except Exception:  # noqa: BLE001 - 非法 base64
        return None
    if not hmac.compare_digest(expect, given):
        return None
    if raw_user.startswith("b64u:"):
        try:
            return _b64d(raw_user[5:]).decode("utf-8")
        except Exception:  # noqa: BLE001
            return None
    return raw_user


def check_credentials(user: str, pw: str, admin_user: Optional[str],
                      admin_pass: Optional[str]) -> bool:
    """UTF-8 字节级无短路时序安全比较（避免 Python 3.10 非 ASCII str 抛 TypeError 500）。"""
    if not admin_user or not admin_pass:
        return False
    u_ok = hmac.compare_digest(str(user or "").encode("utf-8"), admin_user.encode("utf-8"))
    p_ok = hmac.compare_digest(str(pw or "").encode("utf-8"), admin_pass.encode("utf-8"))
    return u_ok and p_ok


# ---------------------------------------------------------------------------
# 防爆破计数（进程内，带过期清理与容量上限防内存泄漏）
# ---------------------------------------------------------------------------
def _prune_attempts(now: float) -> None:
    if len(_attempts) <= _ATTEMPTS_MAX_KEYS // 2:
        return
    cutoff = now - _LOCK_S * 4
    for k in list(_attempts):
        rec = _attempts[k]
        last_seen = rec[2] if len(rec) > 2 else rec[1]
        if rec[1] <= now and last_seen < cutoff:
            _attempts.pop(k, None)
    if len(_attempts) > _ATTEMPTS_MAX_KEYS:
        for k in list(_attempts)[: len(_attempts) - _ATTEMPTS_MAX_KEYS]:
            _attempts.pop(k, None)


def lock_status(ip: str, now: Optional[float] = None) -> bool:
    """True = 当前锁定中。"""
    rec = _attempts.get(ip)
    return bool(rec and rec[1] > (now or time.time()))


def record_fail(ip: str, now: Optional[float] = None) -> Tuple[int, bool]:
    """记一次失败 → (累计失败数, 是否新进入锁定)。"""
    now = now or time.time()
    _prune_attempts(now)
    rec = _attempts.get(ip)
    cnt = rec[0] if rec else 0
    lock_until = rec[1] if rec else 0.0
    cnt += 1
    if cnt >= _MAX_FAILS:
        lock_until = now + _LOCK_S
        cnt = 0  # 锁定期内不再累加，解锁后重新计数
    _attempts[ip] = [cnt, lock_until, now]
    return cnt, lock_until > now


def record_ok(ip: str) -> None:
    _attempts.pop(ip, None)
