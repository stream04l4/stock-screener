#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""策略点位微信提醒（v7.0.11 持仓通道；v7.0.23 增加非持仓首仓通道；
v7.0.31 实时时机 + 补推兜底）。

两条策略线（v7.0.23 Joel 定案：建仓/加仓为两个独立策略），交易时段每 10 分钟
（systemd timer 驱动，见 stock-screener-alerts.timer）通过**推送加（PushPlus，
微信公众号 pushplus 推送加）** 推到 Joel 的微信：

① 持仓股·加仓（越跌越买）：检查**用户持仓账本**里每只股票是否到达策略状态机
   的下一触发价位（in_pos → 第 k+1 档，(first_lots+k×add_lots_step) 手；
   未 in_pos → 首仓线）；
② 非持仓股·首仓（双条件）：检查**候选池非持仓股**是否同时满足
   a. 时机成熟：**实时分位** ≤ price_pctile_max（v7.0.31 Joel 定案 A：以
      现价替换 5 年前复权窗口末值重算——前夜收盘快照会漏「价格破线 + 分位
      破线同日达成」：快照当晚才刷、推送窗口 15:05 已关；山煤国际
      2026-11-10 实案：前夜 42.00% → 当日 37.95%，价全天在首仓线下）；
      无数据 fail-closed 不推；
   b. 价格到位：现价 ≤ 首仓线（本轮高点×(1−first_drawdown_pct)；该参数
      v7.0.26 起居 timing 段 = 选股策略）。
   两者都过 = 首仓买入信号（first_lots 手，v7.0.28 手数口径）。建仓后录入
   「我的持仓」→ 自动转通道①。

口径与 Web「策略信号中心」页面**同一单点**（martingale.touches.trigger_price
+ martingale.signals.build_signals + web.app._strategy_state_rows）：
- 现价 = 腾讯实时行情（批量一次调用，池+持仓一次拉取）；行情失败回退湖最后价
  （与持仓通道同策，只漏报不误报）；
- 触发 = 现价 ≤ 触发线（原始口径 raw，分红复权已除）。

推送加接口：POST http://www.pushplus.plus/send
  {"token": ..., "title": ..., "content": <html>, "template": "html"}
  返回 {"code": 200, ...} 即成功（token 在 .env：PUSHPLUS_TOKEN）。

去重 + 补推兜底（data/alerts/pushplus_alert_state.json，原子写，v7.0.31
Joel 定案：「所有信号都要标注，未成功推送的重复推送」）：
- 键 = "code|action|k|触发价"。**推成功过的点位永不重推**（点位变化——加仓
  后状态机推进 / 新一轮首仓线——= 新键 → 可再推）；
- alerts = 已推点位（Web 历史信号 pushed 标注读它的键）；pending = 应推未
  推成功的信号（first_seen = 首次成为当前信号之日）——信号仍当前时每轮
  自动重推，信号日非当日 → 消息标注「（补推，信号日 …）」；pending 条目
  PENDING_TTL_DAYS 天未推即老化移除（信号已过期，Web 标注仍显示未推）；
- 推送失败不落 alerts（留 pending）→ 下轮自动重试。

退出码：0=正常（含无触发/已去重）；2=配置错（PUSHPLUS_TOKEN 缺失/策略配置）；
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
from datetime import date, datetime, timedelta, timezone

from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from martingale import timing  # noqa: E402  (须在 sys.path 注入后)
from martingale import touches  # noqa: E402

PUSHPLUS_SEND_URL = "http://www.pushplus.plus/send"
STATE_PATH = os.path.join(PROJECT_ROOT, "data", "alerts", "pushplus_alert_state.json")
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "strategy.yaml")
PENDING_TTL_DAYS = 30  # 未推信号 pending 老化（信号过期；Web 标注仍显示未推）

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
    """读推送状态 → {"alerts": {key: 已推点位}, "pending": {key: 应推未推}}。

    v7.0.31：alerts = 已推成功（Web 历史信号 pushed 标注读它的键）；pending =
    应推未推成功的信号（first_seen = 首次成为当前信号之日，补推标注 + TTL）。
    旧文件无 pending 键 → {}。
    """
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {"alerts": {}, "pending": {}}
    al = d.get("alerts")
    pd = d.get("pending")
    return {"alerts": al if isinstance(al, dict) else {},
            "pending": pd if isinstance(pd, dict) else {}}


def _save_state(state: Dict[str, Dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"updated_at": _beijing_now().isoformat(timespec="seconds"),
                   "alerts": state.get("alerts", {}),
                   "pending": state.get("pending", {})},
                  f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_PATH)  # 原子替换，防半截状态


def _alert_key(sig: Dict[str, Any]) -> str:
    """去重键：同一 (股票, 动作, 档, 触发价) 视为同一点位。

    加仓成交后状态机推进 → 触发价/k 变化 → 新键可再推；同日同点位不重推。
    """
    return f"{sig['ts_code']}|{sig['action']}|{sig['k_level']}|{sig['next_add_price']}"


def _prune_pending(pending: Dict[str, Dict[str, Any]],
                   alerts: Dict[str, Dict[str, Any]],
                   today: str) -> bool:
    """pending 卫生（v7.0.31）：已推键移除；first_seen 超 TTL → 信号过期移除。

    返回是否有变更（调用方按需落盘）。
    """
    changed = False
    for k in list(pending):
        if k in alerts:
            del pending[k]
            changed = True
            continue
        fs = (pending[k] or {}).get("first_seen")
        if fs and (date.fromisoformat(today)
                   - date.fromisoformat(fs)).days > PENDING_TTL_DAYS:
            del pending[k]
            changed = True
    return changed


# ---------------------------------------------------------------------------
# 消息与推送
# ---------------------------------------------------------------------------
def render_message(signals: List[Dict[str, Any]], sp_name: str,
                   price_source: str, spot_time: Optional[str]) -> tuple:
    """推送加消息（title 纯文本 + content HTML，微信服务号原生渲染）。

    v7.0.23：双通道聚合（持仓加仓 + 非持仓首仓）。首仓信号带 note（区分口径）
    与 timing 明细（分位；v7.0.26 起股息率参考项删除）；加仓信号无 note（持仓策略）。
    """
    names = []
    blocks: List[str] = []
    for s in signals:
        code, name = s["ts_code"], (s.get("name") or s["ts_code"])
        names.append(name)
        # v8.2.0 累计差值口径：建议量 = 理论累计 vs 现持的差值（build_signals
        # 已算）；持仓够了的信号根本不会到这里（不产信号）。
        gap_note = (f"（策略理论累计 {s['theory_lots']} 手 / 现持 {s['actual_lots']} 手）"
                    if s.get("theory_lots") is not None and s.get("actual_lots") is not None
                    else "")
        if s["action"] == "add":
            line1 = (f"<b>{name}（{code}）</b> 到达 <b>加仓线 第 {s['k_level']} 档</b>")
            tail = (f"建议补至第 {s['next_level']} 档，{s['suggested_lots']} 手{gap_note}"
                    if s.get("suggested_lots") else "建议加仓一档")
        else:
            line1 = (f"<b>{name}（{code}）</b> 到达 <b>首仓线</b>"
                     + (f"（{s['note']}）" if s.get("note") else ""))
            tail = (f"首仓触发，{s['suggested_lots']} 手{gap_note}"
                    if s.get("suggested_lots") else "首仓触发")
        if s.get("delayed"):
            # v7.0.31 补推兜底：信号日非当日（前几轮应推未推成功/时机当晚才
            # 达成）→ 显式标注，避免当成新信号误读
            line1 += f" <span style='color:#c00'>(补推，信号日 {s['first_seen']})</span>"
        bits = [f"现价: <b>{s['price']:.3f}</b>",
                f"触发价: {s['next_add_price']:.3f}"]
        if s.get("pctile") is not None:
            bits.append(f"时机: 分位 {s['pctile']:.1f}%（低位·实时）")
        blocks.append(
            f"<p>{line1}</p>"
            f"<p>{' &nbsp; '.join(bits)}</p>"
            f"<p>{tail}</p><hr/>")
    title = ("策略点位提醒：" + "、".join(names[:3]) +
             (" 等" if len(names) > 3 else "") +
             f" 到达策略点位（{len(signals)} 只）")
    src = "腾讯实时行情" if price_source == "spot" else "湖最后收盘（实时行情缺失）"
    ts = f" {spot_time}" if spot_time else ""
    footer = (f"<p style='color:#888;font-size:12px'>行情: {src}{ts} ｜ "
              f"策略: {sp_name} ｜ 持仓=越跌越买加仓 ｜ 非持仓首仓=实时分位≤40%且破首仓线 ｜ "
              f"同一点位仅推一次，未推成功自动补推（标注信号日）</p>")
    return title, "".join(blocks) + footer


def push_pushplus(token: str, title: str, content: str) -> bool:
    """推推送加（微信）。委托 martingale.pushplus.send（v7.0.13 单一实现点）。"""
    from martingale.pushplus import send as _send

    _send(token, title, content)  # 失败抛 RuntimeError，由调用方记痕
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
    today = _beijing_now().strftime("%Y-%m-%d")

    # 0) 策略配置预检（与 signal_touch 同口径：配置错 = 退出码 2）→ 时机参数
    from martingale.config import load_config
    try:
        _sp_cfg, _up_cfg, tp = load_config(CONFIG_PATH)
    except Exception as exc:  # noqa: BLE001 - ConfigError
        log.error("策略配置错误: %s", exc)
        return 2

    # 1) 用户持仓账本（v7.0.23：账本空不再早退——候选池非持仓股仍有首仓通道）
    from web.app import _up_read, _strategy_state_rows

    user = _up_read()
    held_codes = sorted(user.keys())

    # 2) 策略状态机行（湖只读；持锁/缺失 → 3，下轮重试）
    try:
        from web.app import _load_pool_ctx

        _sp, _up, con, pool = _load_pool_ctx()
    except Exception as exc:  # noqa: BLE001 - 湖锁定/缺失/配置错（含 HTTPException）
        log.warning("数据湖不可用（下轮重试）: %s", exc)
        return 3
    # v7.0.32 除权日修正参照日（北京日期——A 股 ex_date 是北京时间日期）
    _bj_today = datetime.now(timezone(timedelta(hours=8))).date()
    # v8.2.0：用户建仓日锚定（理论持仓从建仓时算；since_date 缺失 → 今天）
    _bj_today_s = str(_bj_today)[:10]
    since_map: Dict[str, str] = {}
    for c in held_codes:
        u = user[c] if isinstance(user[c], dict) else {}
        since_map[c] = str(u.get("since_date") or "")[:10] or _bj_today_s
    try:
        universe = set(pool.codes) | set(held_codes)
        # include_buys=True 必须与 Web 同口径（v7.0.31 山煤国际实案）：
        # first_level_raw 只在 include_buys=True 时附行——缺了它非持仓股
        # 首仓触发线恒 None，首仓推送通道结构性全关（Web 显示首仓线、
        # 推送侧却拿不到线 = 口径漂移）
        rows = (_strategy_state_rows(_sp, con, sorted(universe),
                                     include_buys=True, now=_bj_today,
                                     since_map=since_map)
                if universe else {})
    except Exception as exc:  # noqa: BLE001
        con.close()
        log.warning("数据湖不可用（下轮重试）: %s", exc)
        return 3
    if not universe:
        con.close()
        log.info("股票池与持仓账本均为空，跳过")
        return 0

    # 3) 实时行情（池+持仓批量一次；失败回退湖最后价——只漏报不误报）
    # v1.1（Gate 3，设计 §5）：一主一备统一入口——腾讯主 → 东财 push2delay 备
    #（整批切换，不逐只混用）；两源皆败 → spot={} 走下方湖价回退
    spot: Dict[str, Dict[str, Any]] = {}
    spot_source = ""   # tencent / eastmoney_push2delay / ""（皆败）
    try:
        from screener.data.realtime import fetch_spot

        spot, spot_source = fetch_spot(sorted(universe), timeout=5.0, max_attempts=1)
    except Exception as exc:  # noqa: BLE001 - 行情失败不阻断（回退湖价）
        log.warning("实时行情主备皆败（回退湖最后价）: %s", exc)
    # 3b) 实时分位（v7.0.31 A 案，Joel 定案）：现价（spot，无行情回退湖最后
    #     收盘）替换 5 年前复权窗口末值重算——修「破线 + 分位破线同日达成」
    #     被前夜快照漏推的结构性盲区。算完即关连接。
    price_map: Dict[str, Optional[float]] = {}
    for c in universe:
        p = (spot.get(c) or {}).get("price")
        if p is None:
            p = (rows.get(c) or {}).get("last_price")
        price_map[c] = p
    try:
        live_pct: Dict[str, Optional[float]] = timing.live_pctile_map(
            con, sorted(universe), price_map, tp, now=_bj_today)
    except Exception as exc:  # noqa: BLE001 - 分位失败 → 首仓通道 fail-closed
        log.warning("实时分位计算失败（首仓通道 fail-closed）: %s", exc)
        live_pct = {}
    finally:
        con.close()
    price_source = ("spot" if any(v.get("price") is not None for v in spot.values())
                    else "lake")
    spot_time = next((v.get("timestamp") for v in spot.values() if v.get("timestamp")), None)

    # 4a) 持仓通道：持仓股 → 下一档触发价（与 Web 页面同一口径）
    positions = []
    for c in held_codes:
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
            # v8.2.0 累计差值口径：建仓日起策略买入累计（build_signals 据此算
            # 理论 vs 实际差值——持仓够了不提醒）；strat_shares 全历史累计仅参考
            "strat_shares_since": s.get("shares_since"),
            "strat_shares": s.get("shares"),
        })
    from martingale.signals import build_signals

    signals = build_signals(positions, _sp)
    for sig in signals:
        sig["sig_date"] = today  # 加仓信号日 = 当日（档位当前已到达）

    # 4b) 首仓通道（v7.0.23）：候选池非持仓股，双条件 = 实时时机成熟 且
    # 现价 ≤ 首仓线（触发线恒为 first_level_raw，与页面 touches.trigger_price
    # 非持仓分支同一单点）。v7.0.31：时机 = 实时分位（非前夜快照）；
    # fail-closed：无分位数据/未达标 → 不推。
    touch_state = touches.load_state()  # 补推兜底：信号日 = 触碰落盘日（如有）
    for c in sorted(set(pool.codes) - set(held_codes)):
        r = rows.get(c) or {}
        trig = touches.trigger_price(r, held=False)
        if not trig:
            continue
        pct = live_pct.get(c)
        if pct is None or pct > tp.price_pctile_max:
            continue
        price_now = (spot.get(c) or {}).get("price") or r.get("last_price")
        if price_now is None or price_now > trig:
            continue
        ev = touch_state.get(c) or {}
        ev_date = ev.get("date")
        signals.append({
            "ts_code": c,
            "name": r.get("name") or c,
            "price": price_now,
            "next_add_price": trig,
            "k_level": 0,
            "next_level": 1,
            "suggested_lots": getattr(_sp, "first_lots", None),
            "action": "buy",
            "note": "非持仓·首仓信号（实时时机✓）",
            "pctile": round(pct, 1),
            "sig_date": ev_date if ev_date and ev_date <= today else today,
        })
    signals.sort(key=lambda x: x.get("ts_code") or "")

    # 5) 去重 + 补推兜底（v7.0.31）：已推（alerts）永不重推；未推 → 重推
    state = _load_state()
    alerts, pending = state["alerts"], state["pending"]
    dirty = _prune_pending(pending, alerts, today)
    if not signals:
        if dirty:
            _save_state(state)
        log.info("无触发（持仓 %d 只 + 池内非持仓 %d 只检查完毕，price_source=%s）",
                 len(held_codes), len(set(pool.codes) - set(held_codes)), price_source)
        return 0
    to_push = []
    for sig in signals:
        k = _alert_key(sig)
        if k in alerts:
            log.info("去重: %s 已推送过（%s），不重推", k, (alerts[k] or {}).get("at"))
            continue
        pend = pending.get(k) or {}
        sig_date = sig.get("sig_date") or today
        first_seen = min(pend.get("first_seen") or sig_date, sig_date)
        sig["first_seen"] = first_seen
        sig["delayed"] = first_seen < today
        to_push.append((k, sig))
    if not to_push:
        if dirty:
            _save_state(state)
        log.info("全部触发均已推送过，跳过")
        return 0
    # 6) 渲染 + dry-run 出口（不落任何状态）
    title, content = render_message([s for _, s in to_push], getattr(_sp, "name", "策略"),
                                    price_source, spot_time)
    n_delayed = sum(1 for _, s in to_push if s.get("delayed"))
    log.info("触发 %d 条（%s%s），标题: %s", len(to_push), price_source,
             f"，补推 {n_delayed}" if n_delayed else "", title)
    if dry_run:
        log.info("dry-run：不推送不落状态")
        return 0
    # 6b) 应推未推信号先记账并落盘（推送前——推送中途崩溃也保住 first_seen；
    # 成功后移入 alerts 并从 pending 移除）
    for k, sig in to_push:
        p = dict(pending.get(k) or {})
        p["first_seen"] = sig["first_seen"]
        p["name"] = sig.get("name")
        pending[k] = p
    try:
        _save_state(state)
    except OSError as exc:
        log.warning("pending 预落盘失败（不影响推送）: %s", exc)
    try:
        assert key is not None   # dry_run 时 key 可缺（上方已拦截非 dry_run 缺 key）
        push_pushplus(key, title, content)
    except Exception as exc:  # noqa: BLE001 - 推送失败不落 alerts（留 pending 重试）
        log.error("推送加推送失败（不落状态，下轮重试）: %s", exc)
        return 4

    # 7) 落状态（原子写：alerts += 已推；pending -= 已推）
    at = _beijing_now().isoformat(timespec="seconds")
    for k, sig in to_push:
        alerts[k] = {"date": today, "at": at, "price": sig["price"],
                     "name": sig.get("name"),
                     "signal_date": sig.get("first_seen")}
        pending.pop(k, None)
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
