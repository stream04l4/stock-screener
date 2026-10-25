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

# 进程内：IP -> [fail_count, lock_until_epoch]
_attempts: Dict[str, list] = {}


def _env_path() -> str:
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")


def _read_env(path: str) -> Dict[str, str]:
    """手动解析 .env（与 martingale.pushplus 同风格；KEY=VALUE，忽略注释/空行）。"""
    out: Dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


def load_auth_config() -> Tuple[Optional[str], Optional[str], str]:
    """返回 (user, pass, secret)；user/pass 允许 None（→ 登录端点 500）；secret 恒有。

    每次调用重读 .env（测试可 monkeypatch 环境变量或文件；生产 .env 稳定）。
    环境变量优先于 .env 文件（部署可覆盖）。
    """
    env_file = _read_env(_env_path())
    user = os.environ.get("WEB_ADMIN_USER") or env_file.get("WEB_ADMIN_USER")
    pw = os.environ.get("WEB_ADMIN_PASS") or env_file.get("WEB_ADMIN_PASS")
    secret = os.environ.get("WEB_SESSION_SECRET") or env_file.get("WEB_SESSION_SECRET")
    if not secret:
        secret = secrets.token_hex(32)
    return user, pw, secret


def _b64e(s: bytes) -> str:
    return base64.urlsafe_b64encode(s).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_token(user: str, secret: str, now: Optional[float] = None) -> str:
    exp = int((now or time.time()) + TOKEN_TTL_S)
    payload = f"v1.{user}.{exp}"
    sig = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
    return f"{payload}.{_b64e(sig)}"


def parse_token(token: Optional[str], secret: str, now: Optional[float] = None) -> Optional[str]:
    """有效 → 用户名；无效/过期 → None（任何异常都归一为 None，不炸请求）。"""
    if not token:
        return None
    parts = token.split(".")
    if len(parts) != 4 or parts[0] != "v1":
        return None
    user, exp_s, sig_b64 = parts[1], parts[2], parts[3]
    if not user or not exp_s.isdigit():
        return None
    if int(exp_s) < (now or time.time()):
        return None
    payload = f"v1.{user}.{exp_s}"
    expect = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
    try:
        given = _b64d(sig_b64)
    except Exception:  # noqa: BLE001 - 非法 base64
        return None
    if not hmac.compare_digest(expect, given):
        return None
    return user


def check_credentials(user: str, pw: str, admin_user: Optional[str],
                      admin_pass: Optional[str]) -> bool:
    """hmac.compare_digest 双字段时序安全比较；凭据缺失 → False（端点先 500 拦截）。"""
    if not admin_user or not admin_pass:
        return False
    return (hmac.compare_digest(str(user or ""), admin_user)
            and hmac.compare_digest(str(pw or ""), admin_pass))


# ---------------------------------------------------------------------------
# 防爆破计数（进程内）
# ---------------------------------------------------------------------------
def lock_status(ip: str, now: Optional[float] = None) -> bool:
    """True = 当前锁定中。"""
    rec = _attempts.get(ip)
    return bool(rec and rec[1] > (now or time.time()))


def record_fail(ip: str, now: Optional[float] = None) -> Tuple[int, bool]:
    """记一次失败 → (累计失败数, 是否新进入锁定)。"""
    now = now or time.time()
    cnt, lock_until = _attempts.get(ip, [0, 0.0])
    cnt += 1
    if cnt >= _MAX_FAILS:
        lock_until = now + _LOCK_S
        cnt = 0  # 锁定期内不再累加，解锁后重新计数
    _attempts[ip] = [cnt, lock_until]
    return cnt, lock_until > now


def record_ok(ip: str) -> None:
    _attempts.pop(ip, None)
