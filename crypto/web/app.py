# -*- coding: utf-8 -*-
"""crypto.web.app —— 加密货币系统 Web（v7.0.43 框架）。

独立进程 / 独立端口 :9091 / 独立 SPA（crypto/web/frontend/dist）。
与股票系统（web/app.py，:9090）共享登录：直接 import web.auth（同一
WEB_SESSION_SECRET，ssc_session cookie 按 hostname 跨端口生效）。

框架阶段端点：
- 登录三件套 + health（与股票同款）
- /api/lake/status：数据湖状态（表/行数/每币范围/最近同步）——真实
- /api/coins、/api/kline：行情查询（湖内 1h K 线）——真实，供后续 tab 用
- /api/positions：骨架占位（ready=false，P2 手动登记）
- /api/strategy：策略参数读/存/重置（v7.0.48；覆盖文件 data/crypto/strategy_params.json）
- /api/backtest：全史回测（v7.0.48；湖 1h → 引擎 → 逐币结果 + 净值曲线）
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

import duckdb
import yaml
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# 仓库根（crypto/web/app.py → 上三级）；stock 侧 web.auth 直接复用
PROJECT_ROOT = Path(__file__).resolve().parents[2]
# vite base:'/crypto/' → 产物 assets 前缀 /crypto/assets/，落 crypto/web/dist
DIST_DIR = Path(__file__).resolve().parent / "dist"

CFG_PATH = PROJECT_ROOT / "crypto" / "config" / "crypto.yaml"
with open(CFG_PATH, "r", encoding="utf-8") as _f:
    CFG = yaml.safe_load(_f)

STATE_PATH = PROJECT_ROOT / CFG["lake"]["state_file"]
DB_PATH = PROJECT_ROOT / CFG["lake"]["path"]
GATEWAY_STATE_PATH = PROJECT_ROOT / "data" / "crypto" / "gateway_state.json"

# 核心应用：全部路由保持 /api/* + 静态根（子应用内视角）。
# 对外统一挂 /crypto 前缀（见文件底部 app）——浏览器走
# https://stream04l4.duckdns.org/crypto/...，nginx 按前缀透传；
# 直连 :9091/crypto/... 同样可用（单一 URL 结构，无环境分支）。
core_app = FastAPI(title="crypto-screener web", version="0.1.0")


def _db() -> duckdb.DuckDBPyConnection:
    if not DB_PATH.exists():
        raise HTTPException(status_code=503, detail="数据湖未初始化（先跑 python -m crypto.sync）")
    # 锁语义：read_only 与写锁互斥，cron 离线同步运行中可能短暂持锁 → 短重试
    import time
    for _ in range(3):
        try:
            return duckdb.connect(str(DB_PATH), read_only=True)
        except Exception:  # noqa: BLE001 - 写锁竞争
            time.sleep(0.5)
    raise HTTPException(status_code=503, detail="数据湖写入占用中，稍后重试")


# ---------------------------------------------------------------------------
# 登录（复用股票 web.auth：同 secret、同 cookie 名、同防爆破语义）
# ---------------------------------------------------------------------------
from web.auth import (COOKIE_NAME, TOKEN_TTL_S, check_credentials,  # noqa: E402
                      load_auth_config, lock_status, make_token,
                      parse_token, record_fail, record_ok)

_AUTH_PUBLIC_PATHS = {"/api/login", "/api/logout", "/api/health"}
_AUTH_DISABLED = os.environ.get("WEB_TEST_NO_AUTH") == "1"


@core_app.middleware("http")
async def _auth_gate(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and not _AUTH_DISABLED and path not in _AUTH_PUBLIC_PATHS:
        _u, _p, secret = load_auth_config()
        if parse_token(request.cookies.get(COOKIE_NAME), secret) is None:
            return JSONResponse({"detail": "未登录或会话已过期，请先登录"}, status_code=401)
    return await call_next(request)


class _LoginBody(BaseModel):
    username: str = ""
    password: str = ""


@core_app.post("/api/login")
def login(body: _LoginBody, request: Request, response: Response) -> Dict[str, Any]:
    """与股票系统同一账号；cookie 按 hostname 跨端口 → 股票 :9090 同步免登。"""
    admin_user, admin_pass, secret = load_auth_config()
    if not admin_user or not admin_pass:
        raise HTTPException(status_code=500,
                            detail="登录凭据未配置（.env WEB_ADMIN_USER/WEB_ADMIN_PASS）")
    ip = request.client.host if request.client else "unknown"
    if lock_status(ip):
        raise HTTPException(status_code=429, detail="失败次数过多，请 2 分钟后再试")
    if not check_credentials(body.username, body.password, admin_user, admin_pass):
        record_fail(ip)
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    record_ok(ip)
    response.set_cookie(COOKIE_NAME, make_token(admin_user, secret), max_age=TOKEN_TTL_S,
                        httponly=True, samesite="lax", path="/")
    return {"ok": True, "user": admin_user}


@core_app.post("/api/logout")
def logout(response: Response) -> Dict[str, Any]:
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@core_app.get("/api/auth/me")
def auth_me(request: Request) -> Dict[str, Any]:
    _u, _p, secret = load_auth_config()
    user = parse_token(request.cookies.get(COOKIE_NAME), secret)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return {"user": user}


@core_app.get("/api/health")
def health() -> Dict[str, Any]:
    return {"ok": True, "system": "crypto-screener", "version": "0.1.0",
            "port": CFG["web"]["port"]}


# ---------------------------------------------------------------------------
# 数据湖（真实）
# ---------------------------------------------------------------------------
@core_app.get("/api/lake/status")
def lake_status() -> Dict[str, Any]:
    from crypto import models
    st = models.stats(str(DB_PATH))
    try:
        import json
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            st["last_sync"] = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        st["last_sync"] = None
    return st


@core_app.get("/api/gateway")
def gateway_status() -> Dict[str, Any]:
    """实时 K 线网关状态（crypto.gateway 进程写的状态文件；进程停 → down）。"""
    import json
    import time

    if not GATEWAY_STATE_PATH.exists():
        return {"ok": False, "ws": "down",
                "detail": "网关状态文件不存在（crypto-gateway.service 未启动？）"}
    try:
        with open(GATEWAY_STATE_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        d["stale_s"] = int(time.time()) - int(d.get("updated", 0))
        d["stale"] = d["stale_s"] > 180  # 状态文件 3 分钟未更新视为网关掉线
        return d
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "ws": "down", "detail": f"状态文件读取失败: {e}"}


# ---------------------------------------------------------------------------
# 行情查询（湖内 1h K 线；真实，供后续 tab 扩展）
# ---------------------------------------------------------------------------
_INTERVAL_AGG = {
    # 1h 基础粒度；4h/1d 本地聚合（GROUP BY 小时整除）
    "1h": None, "4h": 4, "1d": 24,
}


@core_app.get("/api/coins")
def coins() -> Dict[str, Any]:
    """币列表 + 湖内最新 1h 收盘（实时 24h ticker 后续版本接源缓存）。"""
    con = _db()
    try:
        rows = con.execute("""
            SELECT m.symbol, m.is_active, k.ts, k.close
            FROM coin_master m
            LEFT JOIN (
                SELECT symbol, ts, close FROM kline_1h a
                WHERE exchange='binance' AND ts = (
                    SELECT MAX(ts) FROM kline_1h b
                    WHERE b.exchange='binance' AND b.symbol = a.symbol)
            ) k USING (symbol)
            ORDER BY m.symbol
        """).fetchall()
        import datetime as dt
        out = [{"symbol": s, "active": bool(act),
                "last_close": c,
                "last_ts": dt.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M")
                if t else None}
               for s, act, t, c in rows]
        return {"coins": out}
    finally:
        con.close()


@core_app.get("/api/kline")
def kline(symbol: str = Query(..., description="Binance 格式，如 BTCUSDT"),
          interval: str = "1h",
          from_ts: Optional[int] = Query(None),
          to_ts: Optional[int] = Query(None),
          limit: int = Query(720, le=20000)) -> Dict[str, Any]:
    """湖内 K 线（1h 原始；4h/1d 本地聚合）。升序。"""
    if interval not in _INTERVAL_AGG:
        raise HTTPException(status_code=400, detail="interval 仅支持 1h/4h/1d")
    agg = _INTERVAL_AGG[interval]
    con = _db()
    try:
        where = "exchange='binance' AND symbol=?"
        params: list = [symbol]
        if from_ts is not None:
            where += " AND ts>=?"; params.append(int(from_ts))
        if to_ts is not None:
            where += " AND ts<=?"; params.append(int(to_ts))
        if agg:
            bucket = agg * 3600
            sql = f"""
                SELECT ts/{bucket}*{bucket} AS ts,
                       MIN(open) AS open, MAX(high) AS high, MIN(low) AS low,
                       MAX_BY(close, ts) AS close,
                       SUM(volume) AS volume, SUM(quote_volume) AS quote_volume
                FROM kline_1h WHERE {where}
                GROUP BY 1 ORDER BY 1 DESC LIMIT ?
            """
            params.append(int(limit))
        else:
            sql = f"""
                SELECT ts, open, high, low, close, volume, quote_volume
                FROM kline_1h WHERE {where}
                ORDER BY ts DESC LIMIT ?
            """
            params.append(int(limit))
        rows = con.execute(sql, params).fetchall()
        rows.reverse()  # → 升序
        return {"symbol": symbol, "interval": interval,
                "bars": [{"ts": int(r[0]), "open": r[1], "high": r[2], "low": r[3],
                          "close": r[4], "volume": r[5], "quote_volume": r[6]}
                         for r in rows]}
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 策略（P3，v7.0.48 实体化）：参数单一来源 = crypto.yaml strategy 段
# + 运行时覆盖 data/crypto/strategy_params.json（页面调整，gitignore 运行时目录）
# ---------------------------------------------------------------------------
import json  # noqa: E402
import time  # noqa: E402

from crypto.strategy.engine import StrategyParams  # noqa: E402

STRATEGY_OVERRIDE_PATH = PROJECT_ROOT / "data" / "crypto" / "strategy_params.json"

# 字段名 → (类型, 下限, 上限)：页面/接口的合理性护栏（防手滑写崩引擎）
_STRATEGY_FIELDS = {
    "first_drop_pct": (float, 1.0, 95.0),        # 建仓跌幅 %（1=跌1% 即建仓）
    "add_grid_pct": (float, 0.1, 50.0),          # 加仓级距 %
    "base_amount_usdt": (float, 0.1, 1_000_000.0),
    "growth": (float, 1.0, 3.0),                 # 每次加仓倍数
    "take_profit_mult": (float, 1.01, 1000.0),   # 止盈倍数
    "fee_bps": (float, 0.0, 1000.0),
    "lookback_h": (int, 168, 87600),             # 一年高点窗口（小时）
}


def _validate_strategy_dict(d: Dict[str, Any], partial: bool = True) -> Dict[str, Any]:
    """校验/清洗参数字典 → 合法部分；越界或类型错 → 400。partial=True 允许子集。"""
    clean: Dict[str, Any] = {}
    for k, v in (d or {}).items():
        if k not in _STRATEGY_FIELDS:
            raise HTTPException(status_code=400, detail=f"未知策略参数: {k}")
        typ, lo, hi = _STRATEGY_FIELDS[k]
        try:
            x = typ(v)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"参数 {k} 需为数字")
        if not lo <= x <= hi:
            raise HTTPException(status_code=400,
                                detail=f"参数 {k}={v} 超出范围 [{lo}, {hi}]")
        clean[k] = x
    if not partial and set(clean) != set(_STRATEGY_FIELDS):
        raise HTTPException(status_code=400,
                            detail="缺少策略参数: "
                                   + ", ".join(set(_STRATEGY_FIELDS) - set(clean)))
    return clean


def _load_override() -> Dict[str, Any]:
    if STRATEGY_OVERRIDE_PATH.exists():
        try:
            with open(STRATEGY_OVERRIDE_PATH, encoding="utf-8") as f:
                d = json.load(f)
            return _validate_strategy_dict(d)
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001 - 损坏覆盖文件 → 忽略（回退 yaml）
            return {}
    return {}


def _effective_params(extra: Optional[Dict[str, Any]] = None):
    """yaml 基线 ← 覆盖文件 ← 请求内临时参数 → (StrategyParams, 生效 dict)。"""
    merged = dict(CFG.get("strategy") or {})
    merged.update(_load_override())
    if extra:
        merged.update(_validate_strategy_dict(extra))
    p = StrategyParams.from_cfg({"strategy": merged})
    return p, p.__dict__.copy()  # type: ignore[attr-defined]


def _write_override(d: Dict[str, Any]) -> None:
    """覆盖文件原子写（tmp + rename）。d = 合并后的完整有效字段子集。"""
    STRATEGY_OVERRIDE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STRATEGY_OVERRIDE_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STRATEGY_OVERRIDE_PATH)


@core_app.get("/api/strategy")
def strategy() -> Dict[str, Any]:
    """当前生效策略参数（yaml 基线 + 页面覆盖）+ 默认值 + 覆盖标记。"""
    p, eff = _effective_params()
    has_override = STRATEGY_OVERRIDE_PATH.exists()
    defaults = StrategyParams().__dict__.copy()  # type: ignore[attr-defined]
    return {
        "ready": True,
        "params": eff,
        "defaults": defaults,
        "has_override": has_override,
        "rule": {
            "first": "建仓：价 ≤ 一年高点 × (1 − first_drop_pct%)，首仓 base_amount_usdt USDT",
            "add": "加仓：固定网格 建仓价 × (1 − add_grid_pct% × k)，第 k 级 base×growth^k USDT，不设上限",
            "hold": "上涨不加仓，新高不刷新建仓价",
            "exit": "止盈：价 ≥ 建仓价 × take_profit_mult → 全卖，entry 重置进入下一轮",
        },
    }


class _StrategyBody(BaseModel):
    params: Dict[str, Any] = {}


@core_app.post("/api/strategy")
def strategy_save(body: _StrategyBody) -> Dict[str, Any]:
    """保存页面参数 → 覆盖文件（与既有覆盖合并，子集可存）。"""
    new = _validate_strategy_dict(body.params)
    if not new:
        raise HTTPException(status_code=400, detail="params 为空")
    merged = _load_override()
    merged.update(new)
    _write_override(merged)
    p, eff = _effective_params()
    return {"ok": True, "params": eff, "has_override": True}


@core_app.post("/api/strategy/reset")
def strategy_reset() -> Dict[str, Any]:
    """删除覆盖文件 → 回退 crypto.yaml 默认。"""
    if STRATEGY_OVERRIDE_PATH.exists():
        STRATEGY_OVERRIDE_PATH.unlink()
    p, eff = _effective_params()
    return {"ok": True, "params": eff, "has_override": False}


class _BacktestBody(BaseModel):
    params: Dict[str, Any] = {}     # 可选：本次回测临时覆盖（不落盘）
    coins: list = []                # 可选：缺省 = crypto.yaml coins 段


def _ui_result(r: Dict[str, Any]) -> Dict[str, Any]:
    """引擎结果 → 页面契约（键名归一；净值曲线降采样 ≤400 点）。"""
    eq = r.pop("equity")
    dates = r.pop("dates")
    if len(eq):
        step = max(1, len(eq) // 400)
        curve = [[str(dates[i])[:10], round(float(eq[i]), 2)]
                 for i in range(0, len(eq), step)]
        if curve and curve[-1][0] != str(dates[-1])[:10]:
            curve.append([str(dates[-1])[:10], round(float(eq[-1]), 2)])
    else:
        curve = []
    rounds = [{
        "entry_date": x["entry_date"], "entry": x["entry"],
        "exit_date": x["exit_date"], "exit": x["exit_price"],
        "k": x["max_level"], "invested": x["spent_usdt"],
        "proceeds": x["proceeds_usdt"], "pnl_pct": x["pnl_pct"],
    } for x in r["rounds"]]
    o = r["open_round"]
    open_round = None
    if o:
        open_round = {
            "entry_date": o["entry_date"], "entry": o["entry"],
            "k": o["k_level"], "spent": o["spent_usdt"],
            "value": o["value_usdt"], "unrealized_pct": o["unrealized_pct"],
            "tp_line": o["tp_price"],
        }
    spent_total = sum(x["spent_usdt"] for x in r["rounds"])
    # realized_cash = 净盈亏累计（proceeds − spent）→ 收益率 = 净/投入
    rpct = (round(r["realized_cash"] / spent_total * 100.0, 2)
            if spent_total else 0.0)
    return {
        "symbol": r["symbol"], "n_bars": r["n_bars"],
        "start": r["start"], "end": r["end"],
        "rounds": rounds, "open_round": open_round,
        "events": r.get("events", []),
        "realized_cash": r["realized_cash"], "realized_pnl_pct": rpct,
        "final_equity": r["final_equity"], "mdd_pct": r["mdd_pct"],
        "equity_curve": curve,
    }


@core_app.post("/api/backtest")
def backtest(body: _BacktestBody) -> Dict[str, Any]:
    """全史回测（湖 1h K 线 → 引擎 → 逐币结果 + 降采样净值曲线）。

    同步执行（3 币全史 ~1s）。params 优先级：请求 > 覆盖文件 > yaml。
    """
    from crypto.backtest.runner import run_backtest
    p, eff = _effective_params(body.params or None)
    coins = [c for c in (body.coins or []) if c] or CFG["coins"]
    t0 = time.time()
    results = run_backtest(coins, p, lake_path=str(DB_PATH))
    return {"ok": True, "params": eff, "elapsed_s": round(time.time() - t0, 2),
            "results": [_ui_result(r) for r in results]}


# 我的持仓（P2）骨架占位——启动前与 Joel 确认账本归属（crypto_design §9）
@core_app.get("/api/positions")
def positions() -> Dict[str, Any]:
    return {"ready": False, "detail": "我的持仓：框架已搭，功能待完善（P2 手动登记）"}


# ---------------------------------------------------------------------------
# 静态前端（最后挂载，避免吞掉 /api/*）
# ---------------------------------------------------------------------------
if DIST_DIR.exists():
    assets_dir = DIST_DIR / "assets"
    if assets_dir.exists():
        core_app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    @core_app.get("/")
    async def index() -> FileResponse:
        return FileResponse(DIST_DIR / "index.html")


# ── 对外入口：统一 /crypto 前缀 ────────────────────────────────────
# nginx 443 单域名分流（见 docs/architecture.md）：
#   https://stream04l4.duckdns.org/        → :9090 股票
#   https://stream04l4.duckdns.org/crypto/ → 此处（nginx 按前缀透传，不剥前缀）
app = FastAPI(title="crypto-screener web", version="0.1.0")
app.mount("/crypto", core_app)


# 外层 auth 门（纵深防御，必须）：经验证，经 Mount 进入子应用时内部
# _auth_gate 完全不执行（本版本 FastAPI 挂载子应用不套用其 HTTP 中间件，
# 探针确认 parse_token 零调用）→ 未登录请求直通。此处对外层全部
# /crypto/api/* 兜底同一套鉴权（公开路径与内部一致：login/logout/health）。
# 内层门保留：core_app 直用（测试/直连）时仍然生效。
@app.middleware("http")
async def _auth_gate_outer(request: Request, call_next):
    path = request.url.path
    if path.startswith("/crypto/api/") and not _AUTH_DISABLED:
        inner_path = path[len("/crypto"):]  # → /api/...
        if inner_path not in _AUTH_PUBLIC_PATHS:
            _u, _p, secret = load_auth_config()
            if parse_token(request.cookies.get(COOKIE_NAME), secret) is None:
                return JSONResponse({"detail": "未登录或会话已过期，请先登录"}, status_code=401)
    return await call_next(request)


@app.get("/")
async def root_redirect():
    # 直连 :9091 根路径 → 引导到带前缀入口
    return RedirectResponse(url="/crypto/")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=CFG["web"]["host"], port=int(CFG["web"]["port"]))
