#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""持仓策略点位微信提醒（v7.0.11，Joel 2026-10-24："持仓到策略点位要提醒"）。

职责：交易时段每 10 分钟（systemd timer 驱动，见 stock-screener-alerts.timer）
检查**用户持仓账本**里每只股票是否到达策略状态机的下一触发价位，触发则通过
**推送加（PushPlus，微信公众号 pushplus 推送加）** 推到 Joel 的微信。

口径与 Web「我的持仓」页面信号**逐字一致**（共享 martingale.signals.build_signals
+ web.app._strategy_state_rows 单一定义点）：
- 现价 = 腾讯实时行情（批量一次调用）；行情失败回退湖最后价（只漏报不误报）；
- 触发 = 现价 ≤ 下一档触发价（原始口径 raw，分红复权已除）；
- in_pos（策略持仓第 k 档）→ "add"（加到 k+1 档，base×growth^k）；否则 "buy"
  （首仓 initial_amount）。

推送加接口：POST http://www.pushplus.plus/send
  {"token": ..., "title": ..., "content": <html>, "template": "html"}
  返回 {"code": 200, ...} 即成功（token 在 .env：PUSHPLUS_TOKEN）。

去重（data/alerts/pushplus_alert_state.json，原子写）：
- 键 = "code|action|k|触发价"——**同一点位同日最多推 1 条**；点位变化（加仓后
  状态机推进）= 新键 → 可再推；推送失败不落状态 → 下次运行自动重试。

退出码：0=正常（含无持仓/无触发/已去重）；2=配置错（PUSHPLUS_TOKEN 缺失）；
3=数据湖不可用（持锁/缺失——下轮自动重试）；4=推送加推送失败（下轮重试）。

用法：
    python scripts/position_alert.py            # 定时任务入口（含交易时段门）
    python scripts/position_alert.py --force    # 跳过时段门（手动/测试）
    python scripts/position_alert.py --dry-run  # 算信号+打印，不推送不落状态
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

PUSHPLUS_SEND_URL = "http://www.pushplus.plus/send"
STATE_PATH = os.path.join(PROJECT_ROOT, "data", "alerts", "pushplus_alert_state.json")

log = logging.getLogger("position_alert")


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _load_env_key(name: str) -> Optional[str]:
    """从 .env 读配置键（同 tushare_ingest 手动解析，不依赖 python-dotenv）。"""
    env_path = os.path.join(PROJECT_ROOT, ".env")
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    except OSError:
        pass
    return os.environ.get(name) or None


def _beijing_now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Shanghai"))


def in_trading_window(now: Optional[datetime] = None) -> bool:
    """交易时段门（留缓冲）：周一至周五 09:15-11:35 / 12:55-15:05 北京时间。

    15:05 末窗覆盖收盘判定（收盘价破线 = 当日有效信号）；周末/节假日由
    「无新触发+同日去重」自然消化（不推或只推一条，无误报）。
    """
    now = now or _beijing_now()
    if now.weekday() > 4:
        return False
    hm = now.hour * 60 + now.minute
    return (9 * 60 + 15) <= hm <= (11 * 60 + 35) or (12 * 60 + 55) <= hm <= (15 * 60 + 5)


# ---------------------------------------------------------------------------
# 状态文件（去重）
# ---------------------------------------------------------------------------
def _load_state() -> Dict[str, Dict[str, Any]]:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        al = d.get("alerts")
        return al if isinstance(al, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state: Dict[str, Dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"updated_at": _beijing_now().isoformat(timespec="seconds"),
                   "alerts": state}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_PATH)  # 原子替换，防半截状态


def _alert_key(sig: Dict[str, Any]) -> str:
    """去重键：同一 (股票, 动作, 档, 触发价) 视为同一点位。

    加仓成交后状态机推进 → 触发价/k 变化 → 新键可再推；同日同点位不重推。
    """
    return f"{sig['ts_code']}|{sig['action']}|{sig['k_level']}|{sig['next_add_price']}"


# ---------------------------------------------------------------------------
# 消息与推送
# ---------------------------------------------------------------------------
def render_message(signals: List[Dict[str, Any]], sp_name: str,
                   price_source: str, spot_time: Optional[str]) -> tuple:
    """推送加消息（title 纯文本 + content HTML，微信服务号原生渲染）。"""
    names = []
    blocks: List[str] = []
    for s in signals:
        code, name = s["ts_code"], (s.get("name") or s["ts_code"])
        names.append(name)
        if s["action"] == "add":
            line1 = (f"<b>{name}（{code}）</b> 到达 <b>加仓线 第 {s['k_level']} 档</b>")
            tail = (f"建议加仓至第 {s['next_level']} 档，约 "
                    f"{s['suggested_amount']:,.0f} 元"
                    if s.get("suggested_amount") else "建议加仓一档")
        else:
            line1 = f"<b>{name}（{code}）</b> 到达 <b>首仓线</b>"
            tail = (f"首仓触发，约 {s['suggested_amount']:,.0f} 元"
                    if s.get("suggested_amount") else "首仓触发")
        blocks.append(
            f"<p>{line1}</p>"
            f"<p>现价: <b>{s['price']:.3f}</b> &nbsp; 触发价: {s['next_add_price']:.3f}</p>"
            f"<p>{tail}</p><hr/>")
    title = ("持仓点位提醒：" + "、".join(names[:3]) +
             (" 等" if len(names) > 3 else "") +
             f" 到达策略点位（{len(signals)} 只）")
    src = "腾讯实时行情" if price_source == "spot" else "湖最后收盘（实时行情缺失）"
    ts = f" {spot_time}" if spot_time else ""
    footer = (f"<p style='color:#888;font-size:12px'>行情: {src}{ts} ｜ "
              f"策略: {sp_name} ｜ 同一点位每日仅提醒一次</p>")
    return title, "".join(blocks) + footer


def push_pushplus(token: str, title: str, content: str) -> bool:
    """推推送加（微信）。返回 {"code": 200} → True；失败抛错由调用方记痕。"""
    import requests

    resp = requests.post(
        PUSHPLUS_SEND_URL,
        json={"token": token, "title": title, "content": content,
              "template": "html"},
        timeout=15)
    resp.raise_for_status()
    body = resp.json()
    if body.get("code") != 200:
        # 常见：token 无效/过期、账号被封、额度用尽——msg 里带原因，明确报错
        raise RuntimeError(f"pushplus 推送失败: {body.get('msg')} (code={body.get('code')})")
    return True


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run(force: bool = False, dry_run: bool = False,
        state_path: Optional[str] = None) -> int:
    """跑一轮检查。返回退出码（0/2/3/4，见模块 docstring）。"""
    global STATE_PATH
    if state_path:
        STATE_PATH = state_path

    key = _load_env_key("PUSHPLUS_TOKEN")
    if key is None and not dry_run:
        log.error("配置缺失: .env 无 PUSHPLUS_TOKEN（无法推送）")
        return 2
    if not in_trading_window() and not force:
        log.info("非交易时段（%s），跳过", _beijing_now().strftime("%Y-%m-%d %H:%M %A"))
        return 0

    # 1) 用户持仓账本（空 → 无事可做）
    from web.app import _up_read, _strategy_state_rows

    user = _up_read()
    if not user:
        log.info("持仓账本为空，跳过")
        return 0
    codes = sorted(user.keys())

    # 2) 策略状态机行（湖只读；持锁/缺失 → 3，下轮重试）
    try:
        from web.app import _load_pool_ctx

        _sp, _up, con, pool = _load_pool_ctx()
        try:
            rows = _strategy_state_rows(_sp, con, set(pool.codes) | set(codes))
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001 - 湖锁定/缺失/配置错（含 HTTPException）
        log.warning("数据湖不可用（下轮重试）: %s", exc)
        return 3

    # 3) 实时行情（批量一次；失败回退湖最后价——只漏报不误报）
    spot: Dict[str, Dict[str, Any]] = {}
    try:
        from screener.data.tencent import TencentClient

        spot = TencentClient(timeout=5.0, max_attempts=1).fetch(codes) or {}
    except Exception as exc:  # noqa: BLE001 - 行情失败不阻断（回退湖价）
        log.warning("腾讯实时行情失败（回退湖最后价）: %s", exc)
    price_source = ("spot" if any(v.get("price") is not None for v in spot.values())
                    else "lake")
    spot_time = next((v.get("timestamp") for v in spot.values() if v.get("timestamp")), None)

    # 4) 信号（与 Web 页面同一口径）
    positions = []
    for c in codes:
        u = user[c] if isinstance(user[c], dict) else {}
        s = rows.get(c, {})
        last = s.get("last_price")
        positions.append({
            "ts_code": c,
            "name": s.get("name") or c,
            "last_price": last,
            "in_pos": s.get("in_pos"),
            "k_level": s.get("k_level"),
            "next_add_price_raw": s.get("next_add_price_raw"),
            "spot_price": (spot.get(c) or {}).get("price"),
            "shares": float(u.get("shares") or 0),
        })
    from martingale.signals import build_signals

    signals = build_signals(positions, _sp)
    if not signals:
        log.info("无触发（%d 只持仓检查完毕，price_source=%s）", len(codes), price_source)
        return 0

    # 5) 去重（同日同点位不重推）
    state = _load_state()
    today = _beijing_now().strftime("%Y-%m-%d")
    to_push = []
    for sig in signals:
        k = _alert_key(sig)
        prev = state.get(k)
        if prev and prev.get("date") == today:
            log.info("去重: %s 今早已推（%s）", k, prev.get("at"))
            continue
        to_push.append((k, sig))
    if not to_push:
        log.info("全部触发今早已推过，跳过")
        return 0

    # 6) 推送（聚合一条）
    title, content = render_message([s for _, s in to_push], getattr(_sp, "name", "策略"),
                                    price_source, spot_time)
    log.info("触发 %d 条（%s），标题: %s", len(to_push), price_source, title)
    if dry_run:
        log.info("dry-run：不推送不落状态")
        return 0
    try:
        assert key is not None   # dry_run 时 key 可缺（上方已拦截非 dry_run 缺 key）
        push_pushplus(key, title, content)
    except Exception as exc:  # noqa: BLE001 - 推送失败不落状态 → 下轮重试
        log.error("推送加推送失败（不落状态，下轮重试）: %s", exc)
        return 4

    # 7) 落状态（原子写）
    at = _beijing_now().isoformat(timespec="seconds")
    for k, sig in to_push:
        state[k] = {"date": today, "at": at, "price": sig["price"],
                    "name": sig.get("name")}
    try:
        _save_state(state)
    except OSError as exc:
        log.error("状态落盘失败（已推送，下轮可能重推一次，可接受）: %s", exc)
    log.info("推送成功，状态已落盘（%d 条）", len(to_push))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(description="持仓策略点位微信提醒（systemd timer 驱动）")
    p.add_argument("--force", action="store_true", help="跳过交易时段门（手动/测试）")
    p.add_argument("--dry-run", action="store_true", help="只算信号+打印，不推送不落状态")
    p.add_argument("--state-path", default=None, help="状态文件路径（测试用）")
    args = p.parse_args(argv)
    t0 = time.monotonic()
    rc = run(force=args.force, dry_run=args.dry_run, state_path=args.state_path)
    log.info("exit=%d (%.1fs)", rc, time.monotonic() - t0)
    return rc


if __name__ == "__main__":
    sys.exit(main())
