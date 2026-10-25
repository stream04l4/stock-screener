# -*- coding: utf-8 -*-
"""test_web_auth —— v7.0.14 登录门禁（TestClient，零网络/零真实湖）。

契约：
- 白名单 /api/health、/api/login、/api/logout 免登录；其余 /api/* 无 cookie → 401；
- 登录成功 → HttpOnly 签名 cookie（7 天）；错误凭据 → 401「用户名或密码错误」；
- 连续 5 次失败锁 120s → 429（正确密码也拒）；成功后计数清零；
- token 篡改/过期 → 401；logout 清 cookie；凭据未配置 → 500 显式失败（不静默放行）。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import web.app as wapp  # noqa: E402
import web.auth as wa   # noqa: E402

SECRET = "test-secret-0123456789abcdef0123456789abcdef"


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("WEB_ADMIN_USER", "admin")
    monkeypatch.setenv("WEB_ADMIN_PASS", "unit-test-only-pass")
    monkeypatch.setenv("WEB_SESSION_SECRET", SECRET)
    wa._attempts.clear()
    with TestClient(wapp.app) as c:
        yield c
    wa._attempts.clear()


def test_public_and_protected(client):
    assert client.get("/api/health").status_code == 200          # 白名单
    r = client.get("/api/strategy")                              # 受保护，无 cookie
    assert r.status_code == 401 and "未登录" in r.json()["detail"]
    assert client.get("/api/timing").status_code == 401
    assert client.get("/api/my_positions").status_code == 401


def test_login_success_sets_httponly_cookie(client):
    r = client.post("/api/login", json={"username": "admin", "password": "unit-test-only-pass"})
    assert r.status_code == 200 and r.json() == {"ok": True, "user": "admin"}
    setc = r.headers.get("set-cookie", "")
    assert "ssc_session=" in setc
    assert "httponly" in setc.lower() and "samesite=lax" in setc.lower()
    # cookie 生效：me + 受保护端点放行
    assert client.get("/api/auth/me").json() == {"user": "admin"}
    assert client.get("/api/strategy").status_code == 200


def test_login_wrong_password_and_lockout(client):
    r = client.post("/api/login", json={"username": "admin", "password": "bad"})
    assert r.status_code == 401 and r.json()["detail"] == "用户名或密码错误"
    # 连续失败 → 第 5 次进入锁定，第 6 次起 429（正确密码也拒）
    for _ in range(4):
        assert client.post("/api/login", json={"username": "admin", "password": "bad"}).status_code == 401
    r = client.post("/api/login", json={"username": "admin", "password": "unit-test-only-pass"})
    assert r.status_code == 429
    # 锁定窗口内正确密码同样 429
    assert client.post("/api/login", json={"username": "admin", "password": "unit-test-only-pass"}).status_code == 429
    # 解锁后（时间推进 > LOCK_S）恢复正常：直接清零锁定时钟
    ip = "testclient"
    now = time.time()
    for k, rec in wa._attempts.items():
        if rec[1] > now:
            rec[1] = 0.0
    r = client.post("/api/login", json={"username": "admin", "password": "unit-test-only-pass"})
    assert r.status_code == 200
    # 成功清零：再失败一次只累计 1
    assert client.post("/api/login", json={"username": "admin", "password": "bad"}).status_code == 401
    assert wa._attempts.get(ip, [1, 0.0])[0] == 1


def test_tampered_and_expired_token(client, monkeypatch):
    from web.auth import make_token
    # 篡改 sig
    good = make_token("admin", SECRET)
    tampered = good[:-4] + ("AAAA" if good[-4:] != "AAAA" else "BBBB")
    client.cookies.set("ssc_session", tampered)
    assert client.get("/api/auth/me").status_code == 401
    # 过期 token（exp 已过去）
    expired = make_token("admin", SECRET, now=time.time() - 10 * 86400)
    client.cookies.set("ssc_session", expired)
    assert client.get("/api/auth/me").status_code == 401
    # 正常 token
    client.cookies.set("ssc_session", make_token("admin", SECRET))
    assert client.get("/api/auth/me").json() == {"user": "admin"}


def test_logout_clears_session(client):
    client.post("/api/login", json={"username": "admin", "password": "unit-test-only-pass"})
    assert client.get("/api/auth/me").status_code == 200
    r = client.post("/api/logout")
    assert r.status_code == 200 and "ssc_session" in r.headers.get("set-cookie", "")
    assert client.get("/api/auth/me").status_code == 401


def test_missing_credentials_500(client, monkeypatch):
    monkeypatch.delenv("WEB_ADMIN_USER", raising=False)
    monkeypatch.delenv("WEB_ADMIN_PASS", raising=False)
    monkeypatch.setattr(wa, "_read_env", lambda p: {})  # .env 也视为不存在
    r = client.post("/api/login", json={"username": "admin", "password": "unit-test-only-pass"})
    assert r.status_code == 500 and "未配置" in r.json()["detail"]


# ---------------------------------------------------------------------------
# token 单元
# ---------------------------------------------------------------------------
def test_token_roundtrip_and_reject():
    from web.auth import make_token, parse_token
    t = make_token("admin", SECRET)
    assert parse_token(t, SECRET) == "admin"
    assert parse_token(t, "other-secret" * 4) is None       # 错密钥
    assert parse_token(t.split(".")[0] + ".x.y.z", SECRET) is None  # 垃圾串
    assert parse_token("", SECRET) is None
    assert parse_token(None, SECRET) is None


def test_check_credentials_missing():
    assert wa.check_credentials("a", "b", None, "x") is False
    assert wa.check_credentials("a", "b", "u", None) is False
    assert wa.check_credentials("a", "b", "a", "b") is True


def test_auth_disabled_hook_default_off():
    # WEB_TEST_NO_AUTH 钩子仅供 lake real-server 测试子进程用（见各测试 code_lines）；
    # 主测试进程/生产环境恒关，门禁必须生效。
    assert wapp._AUTH_DISABLED is False
