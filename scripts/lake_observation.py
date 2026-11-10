#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gate 1 观察期只读交叉验证（重设计 §6 D1–D5；设计稿
stages/01_research/table_source_redesign_20261110.md，Gate 0 已批）。

**零写入红线**：对湖只开 read_only 连接；唯一落盘 = data/observation/
（每日 JSON + summary.jsonl 追加）。失败不阻断（挂 lake-sync cron 尾部，
§8「失败不阻断增量」）。

两种模式：
  --daily    lake-sync 尾部跑（11:30 UTC 增量后）：
    T1  东财全市场列表 12 页（push2delay clist，f14 名称/f26 上市日/f100 行业）
          → 字段齐备率 + 与湖 T1 行数对比
    T2  池+持仓逐股当日日K（push2his fqt=1 raw，lmt=1）vs 湖 kline_daily
          当日行 close/amount 分歧率（晋主判据：<0.1% / <0.5%）
    T3  datacenter-web RPT_VALUEANALYSIS_DET 12 页当日行 vs 湖 valuation_daily
          （pe_ttm/pb/total_mv；换手率=NULL 显式降级不对账）
    T7  上证指数/沪深300 当日指数K（push2his 含 amount）vs 湖 index_daily
    T4  东财分红报表端点探活（reportName 待验证项；未找到→如实记录）
    Fy  Fuyao 财报 PIT 验证 1 次（income-statements quarterly limit=1，
          report_date_ms 披露日字段在场即 PIT 合格）
  --latency  盘中 3 时点跑（cron 01:35/04:05/06:35 UTC）：
    push2delay ulist f124 vs 腾讯 ts 的 tick 级延时（§5 备源定位判据：
    >3 分钟 → 备源降「取价失败兜底」）

成本（§6 成本列修订，2026-11-10 review）：T2 交叉校验=**池+持仓样本**
（生产信号系统同口径 universe 的超集，非全市场 5500 逐股——设计稿原「逐股」
与「≈30 调用/日」成本列矛盾，工程裁定样本制；全市场逐股留 Gate 2 影子运行）。
全日 ≈30( T1/T3/T7/T4/Fy) + ~110(T2 逐股, 0.4s 间隔串行) ≈ 140 调用/日，
东财无硬限（11-10 实测 20 并发 12 req/s），远在内侧。

东财 9701/2003/302 空 = 中止条件信号（设计稿 §6）：端点级重试 3 次（3s 退避）
仍失败 → 记该表 available=false + detail；**单表失败不算全日失败**，全日失败
= 所有东财端点全挂（由消费者/TL 按日汇总判）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OBS_DIR = os.path.join(REPO, "data", "observation")
LAKE_DB = os.path.join(REPO, "data", "lake", "lake.duckdb")
ENV_PATH = os.path.join(REPO, ".env")
USER_POS = os.path.join(REPO, "data", "user_positions.json")

# 东财端点（11-10 实测可用形态）
EM_CLIST = ("https://push2delay.eastmoney.com/api/qt/clist/get"
            "?pn={pn}&pz=500&po=1&np=1&fltt=2&invt=2&fid=f12"
            "&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23&fields=f12,f14,f26,f100")
EM_KLINE = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
            "?secid={secid}&klt=101&fqt=1&lmt=1&end=20500101"
            "&fields1=f1,f2,f3&fields2=f51,f53,f56,f57")
EM_DC = ("https://datacenter-web.eastmoney.com/api/data/v1/get"
         "?reportName=RPT_VALUEANALYSIS_DET&columns=ALL&pageSize=500&pageNumber={pn}")
EM_DIVIDEND = ("https://datacenter-web.eastmoney.com/api/data/v1/get"
               "?reportName=RPT_SHAREBONUS_DET&columns=ALL&pageSize=5&pageNumber=1")
FU_YAO_FIN = ("https://fuyao.aicubes.cn/api/a-share/financials/income-statements"
              "?thscode=600519.SH&period=quarterly&limit=1")

CST = dt.timezone(dt.timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
_RETRY = 3        # 东财端点级重试（9701 服务器繁忙等软失败）
_BACKOFF = 3.0


def _http(url: str, headers: Optional[Dict[str, str]] = None,
          timeout: float = 15.0) -> Dict[str, Any]:
    """GET → JSON。抛 OSError/ValueError 由调用方重试。"""
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _get_retry(url: str, headers: Optional[Dict[str, str]] = None,
               timeout: float = 15.0) -> Tuple[Any, str]:
    """带 3 次退避重试的 GET。返回 (payload, detail)；全败 detail=最后错误。"""
    last = ""
    for i in range(_RETRY):
        try:
            d = _http(url, headers, timeout)
            return d, ""
        except (OSError, ValueError) as exc:
            last = f"网络/解析失败: {exc}"
        if i < _RETRY - 1:
            time.sleep(_BACKOFF * (i + 1))
    return None, last


def _em_detail(code: Any, message: Any) -> str:
    return f"rc={code} {message or ''}"[:120]


def _load_env_key(key: str) -> Optional[str]:
    try:
        for line in open(ENV_PATH, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if k.strip() == key:
                    return v.strip()
    except OSError:
        pass
    return None


def _beijing_today() -> str:
    return dt.datetime.now(CST).strftime("%Y-%m-%d")


def _compact_date(s: str) -> str:
    return s.replace("-", "")


def _bs_to_secid(code: str) -> Optional[str]:
    prefix, _, num = code.partition(".")
    if not num.isdigit() or len(num) != 6:
        return None
    if num[0] == "6":
        return f"1.{num}"
    if num[0] in ("0", "3"):
        return f"0.{num}"
    return None


def _universe(con) -> List[str]:
    """池（selection_backtest_10y 最新 as_of）∪ 持仓账本。

    生产信号系统 universe = 过滤后池 ∪ 持仓；本脚本取**过滤前池超集**
    （selection_backtest_10y 全量 distinct）——交叉校验样本覆盖生产全集，
    多校验的 ~40 只不影响判据（分歧率按校验集统计）。
    """
    try:
        held = {c for c in json.load(open(USER_POS, encoding="utf-8"))}
    except (OSError, ValueError):
        held = set()
    pool = {r[0] for r in con.execute(
        "SELECT DISTINCT ts_code FROM selection_backtest_10y "
        "WHERE as_of_date=(SELECT MAX(as_of_date) FROM selection_backtest_10y)"
    ).fetchall()}
    return sorted(pool | held)


# ---------------------------------------------------------------------------
# --daily 各表
# ---------------------------------------------------------------------------
def obs_t1(today: str) -> Dict[str, Any]:
    """T1 东财全市场列表 12 页：字段齐备率 + 湖行数对比。"""
    out: Dict[str, Any] = {"pages": 0, "total": 0,
                           "name_ok": 0, "list_date_ok": 0, "industry_ok": 0,
                           "detail": ""}
    for pn in range(1, 13):
        d, err = _get_retry(EM_CLIST.format(pn=pn))
        if d is None:
            out["detail"] = f"p{pn} {err}"
            return out
        res = d.get("data") or {}
        diff = res.get("diff") or []
        if not diff:
            out["pages"] = pn - 1
            break
        out["pages"] = pn
        out["total"] += len(diff)
        for it in diff:
            if it.get("f14"):
                out["name_ok"] += 1
            if str(it.get("f26", "")) and str(it.get("f26")) != "-":
                out["list_date_ok"] += 1
            if it.get("f100"):
                out["industry_ok"] += 1
        if len(diff) < 500:
            break
    import duckdb
    with duckdb.connect(LAKE_DB, read_only=True) as con:
        out["lake_t1_rows"] = con.execute("SELECT COUNT(*) FROM stock_master").fetchone()[0]
    return out


def obs_t2(today: str) -> Dict[str, Any]:
    """T2 池+持仓逐股当日日K vs 湖（close 0.1% / amount 0.5% 判据线）。"""
    import duckdb
    t2: Dict[str, Any] = {"date": today, "universe": 0, "checked": 0, "missing_lake": 0,
                          "close_div": 0, "amount_div": 0, "em_fail": 0,
                          "max_close_div_pct": 0.0, "max_amount_div_pct": 0.0,
                          "detail": ""}
    with duckdb.connect(LAKE_DB, read_only=True) as con:
        universe = _universe(con)
        t2["universe"] = len(universe)
        lake = {}
        for c, cl, am in con.execute(
            "SELECT ts_code, close, amount FROM kline_daily WHERE date=? "
            "AND close IS NOT NULL", (dt.date.fromisoformat(today),)
        ).fetchall():
            lake[c] = (cl, am)
    for c in universe:
        secid = _bs_to_secid(c)
        if not secid:
            continue
        d, err = _get_retry(EM_KLINE.format(secid=secid), timeout=10.0)
        if d is None or (d.get("data") or {}).get("klines") is None:
            t2["em_fail"] += 1
            t2["detail"] = f"{c}: {err or _em_detail(d.get('rc') if d else '?', '')}"
            continue
        klines = d["data"]["klines"]
        if not klines:
            continue
        parts = klines[-1].split(",")   # f51 date, f53 close, f56 volume, f57 amount
        k_date, k_close, k_amount = parts[0], float(parts[1]), float(parts[3])
        if k_date != today:
            continue                     # 东财当日 bar 未到（停牌/非交易日）→ 不比对
        if c not in lake:
            t2["missing_lake"] += 1
            continue
        l_close, l_amount = lake[c]
        t2["checked"] += 1
        cd = abs(k_close - l_close) / l_close * 100 if l_close else 0.0
        ad = (abs(k_amount - l_amount) / l_amount * 100
              if l_amount else (0.0 if k_amount == l_amount else 999.0))
        if cd > 0.1:
            t2["close_div"] += 1
        if ad > 0.5:
            t2["amount_div"] += 1
        t2["max_close_div_pct"] = max(t2["max_close_div_pct"], round(cd, 4))
        t2["max_amount_div_pct"] = max(t2["max_amount_div_pct"], round(ad, 4))
        time.sleep(0.4)   # 限速：串行 0.4s/只（~110 只 ≈ 45s）
    if t2["checked"]:
        t2["close_div_pct"] = round(t2["close_div"] / t2["checked"] * 100, 3)
        t2["amount_div_pct"] = round(t2["amount_div"] / t2["checked"] * 100, 3)
        t2["pass"] = (t2["close_div_pct"] < 0.1 and t2["amount_div_pct"] < 0.5)
    else:
        t2["pass"] = False
    return t2


def _pick(it: Dict[str, Any], *names: str) -> Optional[float]:
    """候选字段名防御提取（东财报表列名以首次成功返回为准；D1 落 fields_seen 复核）。"""
    for n in names:
        v = it.get(n)
        if isinstance(v, (int, float)):
            return float(v)
    return None


def obs_t3(today: str) -> Dict[str, Any]:
    """T3 datacenter-web 12 页当日行 vs 湖（pe_ttm/pb/total_mv；单趟 12 调用）。

    字段名候选（11-10 探测确认 4/5 字段可用、换手率缺）：PE_TTM / PB_MRQ /
    TOTAL_MARKET_CAP（东财 unit=元；湖 total_mv 单位=亿元）。首跑 fields_seen
    落实际列名供 TL 复核映射。
    """
    import duckdb
    out: Dict[str, Any] = {"date": today, "rows": 0, "checked": 0,
                           "pe_div": 0, "pb_div": 0, "mv_div": 0,
                           "max_pe_div_pct": 0.0, "max_pb_div_pct": 0.0,
                           "max_mv_div_pct": 0.0, "fields_seen": [], "detail": ""}
    samples: Dict[str, Dict[str, Any]] = {}
    for pn in range(1, 13):
        d, err = _get_retry(EM_DC.format(pn=pn))
        if d is None:
            out["detail"] = f"p{pn} {err}"
            out["pass"] = False
            return out
        if not d.get("success"):
            out["detail"] = f"p{pn} {_em_detail(d.get('code'), d.get('message'))}"
            out["pass"] = False
            return out
        data = ((d.get("result") or {}).get("data")) or []
        if not data:
            break
        if not out["fields_seen"]:
            out["fields_seen"] = sorted(data[0].keys())
        for it in data[:50]:   # 每页 50 只样本（~600 只样本/日，够判据统计）
            if str(it.get("TRADE_DATE", ""))[:10] != today:
                continue       # 非当日行跳过（分页漂移/停牌）
            out["rows"] += 1
            code6 = str(it.get("SECURITY_CODE", ""))
            if code6.startswith("6"):
                bs = f"sh.{code6}"
            elif code6.startswith(("0", "3")):
                bs = f"sz.{code6}"
            else:
                continue
            samples[bs] = it
        if len(data) < 500:
            break
    with duckdb.connect(LAKE_DB, read_only=True) as con:
        rows = con.execute(
            "SELECT ts_code, pe_ttm, pb, total_mv FROM valuation_daily WHERE date=?",
            (dt.date.fromisoformat(today),)).fetchall()
    lake = {c: (pe, pb, mv) for c, pe, pb, mv in rows if pe is not None or pb is not None}
    for c, it in samples.items():
        if c not in lake:
            continue
        l_pe, l_pb, l_mv = lake[c]
        e_pe = _pick(it, "PE_TTM")
        e_pb = _pick(it, "PB_MRQ", "PB")
        e_mv = _pick(it, "TOTAL_MARKET_CAP", "TOTAL_MV")
        out["checked"] += 1
        if e_pe and l_pe and e_pe > 0 and l_pe > 0:
            dp = abs(e_pe - l_pe) / l_pe * 100
            out["max_pe_div_pct"] = max(out["max_pe_div_pct"], round(dp, 3))
            if dp > 1.0:
                out["pe_div"] += 1
        if e_pb and l_pb and e_pb > 0 and l_pb > 0:
            dpb = abs(e_pb - l_pb) / l_pb * 100
            out["max_pb_div_pct"] = max(out["max_pb_div_pct"], round(dpb, 3))
            if dpb > 1.0:
                out["pb_div"] += 1
        if e_mv and l_mv:
            dm = abs(e_mv / 1e8 - l_mv) / l_mv * 100   # 东财元 → 亿元 对齐湖口径
            out["max_mv_div_pct"] = max(out["max_mv_div_pct"], round(dm, 3))
            if dm > 0.5:
                out["mv_div"] += 1
    # checked<100（字段名映射错/当日行不足）→ 判不通过，TL 看 detail 处置
    out["pass"] = (out["checked"] >= 100
                   and out["pe_div"] == 0 and out["pb_div"] == 0 and out["mv_div"] == 0)
    return out


def obs_t7(today: str) -> Dict[str, Any]:
    """T7 上证/沪深300 当日指数K vs 湖（close 0.3% / amount 0.5%）。"""
    import duckdb
    out: Dict[str, Any] = {"date": today, "checked": 0, "close_div": 0,
                           "amount_div": 0, "per_index": {}, "detail": ""}
    for idx, secid in (("sh.000001", "1.000001"), ("sh.000300", "1.000300")):
        d, err = _get_retry(EM_KLINE.format(secid=secid), timeout=10.0)
        if d is None or (d.get("data") or {}).get("klines") is None:
            out["detail"] = f"{idx}: {err or '无 klines'}"
            continue
        parts = d["data"]["klines"][-1].split(",")
        e_date, e_close, e_amount = parts[0], float(parts[1]), float(parts[3])
        if e_date != today:
            out["per_index"][idx] = {"em_date": e_date, "note": "东财当日 bar 未到"}
            continue
        with duckdb.connect(LAKE_DB, read_only=True) as con:
            row = con.execute(
                "SELECT close, amount FROM index_daily WHERE index_code=? AND date=?",
                (idx, dt.date.fromisoformat(today))).fetchone()
        if not row:
            out["per_index"][idx] = {"note": "湖无当日行"}
            continue
        l_close, l_amount = row
        cd = abs(e_close - l_close) / l_close * 100 if l_close else 0.0
        ad = abs(e_amount - l_amount) / l_amount * 100 if l_amount else 0.0
        out["checked"] += 1
        if cd > 0.3:
            out["close_div"] += 1
        if ad > 0.5:
            out["amount_div"] += 1
        out["per_index"][idx] = {"close_div_pct": round(cd, 4),
                                 "amount_div_pct": round(ad, 4)}
    out["pass"] = out["checked"] > 0 and out["close_div"] == 0 and out["amount_div"] == 0
    return out


def obs_t4() -> Dict[str, Any]:
    """T4 东财分红报表端点探活（reportName 待验证项——首次跑定生死）。"""
    d, err = _get_retry(EM_DIVIDEND)
    if d is None:
        return {"ok": False, "detail": err}
    if not d.get("success"):
        return {"ok": False,
                "detail": _em_detail(d.get("code"), d.get("message"))}
    data = ((d.get("result") or {}).get("data")) or []
    if not data:
        return {"ok": False, "detail": "success 但空数据"}
    keys = sorted(data[0].keys())
    # PIT/对账关键字段在场性（列名以实际返回为准）
    need = [k for k in ("EX_DIVIDEND_DATE", "IMPL_PLAN_PROFILE", "SECURITY_CODE")
            if k in keys]
    return {"ok": True, "fields": keys, "pit_fields_seen": need, "n": len(data)}


def obs_fuyao() -> Dict[str, Any]:
    """Fuyao 财报 PIT 验证（1 次；report_date_ms 披露日在场=合格）。"""
    key = _load_env_key("HITHINK_FINANCE_API_KEY")
    if not key:
        return {"ok": False, "detail": "缺 HITHINK_FINANCE_API_KEY"}
    d, err = _get_retry(FU_YAO_FIN, headers={"X-api-key": key})
    if d is None:
        return {"ok": False, "detail": err}
    if d.get("code") != 0:
        return {"ok": False, "detail": f"code={d.get('code')}"}
    data = (d.get("data") or {})
    items = data.get("items") or data.get("data") or []
    if not items:
        return {"ok": False, "detail": "空 items"}
    it = items[0]
    rdm = it.get("report_date_ms")
    ok = isinstance(rdm, (int, float)) and rdm > 0
    return {"ok": ok, "period_end_ms": it.get("period_end_ms"),
            "report_date_ms": rdm,
            "disclosure_lag_days": (round((rdm - it.get("period_end_ms", 0)) / 86400000, 1)
                                    if ok and it.get("period_end_ms") else None)}


# ---------------------------------------------------------------------------
# --latency
# ---------------------------------------------------------------------------
def obs_latency(sample_n: int = 10) -> Dict[str, Any]:
    """push2delay f124 vs 腾讯 ts 的 tick 延时（秒；正=东财滞后腾讯）。"""
    import duckdb
    out: Dict[str, Any] = {"ts_utc": dt.datetime.utcnow().isoformat(timespec="seconds"),
                           "delays_s": [], "detail": ""}
    try:
        from screener.data.tencent import TencentClient
    except ImportError:
        out["detail"] = "TencentClient import 失败"
        return out
    with duckdb.connect(LAKE_DB, read_only=True) as con:
        codes = [r[0] for r in con.execute(
            "SELECT ts_code FROM kline_daily WHERE date="
            "(SELECT MAX(date) FROM kline_daily) GROUP BY ts_code "
            "ORDER BY ts_code LIMIT ?", (sample_n,)).fetchall()]
    if not codes:
        out["detail"] = "湖无代码"
        return out
    try:
        tx = TencentClient(timeout=6.0, max_attempts=1).fetch(codes) or {}
    except Exception as exc:  # noqa: BLE001
        out["detail"] = f"腾讯失败: {exc}"
        return out
    from screener.data.realtime import _em_fetch
    try:
        em = _em_fetch(codes) or {}
    except Exception as exc:  # noqa: BLE001
        out["detail"] = f"东财失败: {exc}"
        return out
    for c in codes:
        t_ts = (tx.get(c) or {}).get("timestamp")
        e_ts = (em.get(c) or {}).get("timestamp")
        if not t_ts or not e_ts or len(str(t_ts)) != 14 or len(str(e_ts)) != 14:
            continue
        t_s = dt.datetime.strptime(str(t_ts), "%Y%m%d%H%M%S").replace(tzinfo=CST)
        e_s = dt.datetime.strptime(str(e_ts), "%Y%m%d%H%M%S").replace(tzinfo=CST)
        out["delays_s"].append(round((t_s - e_s).total_seconds(), 1))
    if out["delays_s"]:
        ds = sorted(out["delays_s"])
        out["median_s"] = ds[len(ds) // 2]
        out["max_s"] = ds[-1]
        out["n"] = len(ds)
        # §5 判据：延时 >3 分钟 → 备源降「取价失败兜底」
        out["below_3min"] = out["max_s"] <= 180
    return out


# ---------------------------------------------------------------------------
def _write_day(result: Dict[str, Any]) -> str:
    os.makedirs(OBS_DIR, exist_ok=True)
    day = result["date"]
    path = os.path.join(OBS_DIR, f"{day}.json")
    # 同日多次跑（daily + 3 次 latency）→ 合并写（latency 追加时点数组）
    prev: Dict[str, Any] = {}
    if os.path.exists(path):
        try:
            prev = json.load(open(path, encoding="utf-8"))
        except ValueError:
            prev = {}
    if result.get("run") == "latency":
        prev.setdefault("latency_samples", []).append(result.get("latency"))
        merged = prev
    else:
        merged = {**prev, **result}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=1)
    # summary.jsonl 追加一行（Gate 2 判据汇总用）
    line = {
        "date": day, "run": result.get("run"),
        "ts_utc": result.get("ts_utc"),
        "t2_pass": (result.get("t2") or {}).get("pass"),
        "t2_close_div_pct": (result.get("t2") or {}).get("close_div_pct"),
        "t2_amount_div_pct": (result.get("t2") or {}).get("amount_div_pct"),
        "t3_pass": (result.get("t3") or {}).get("pass"),
        "t7_pass": (result.get("t7") or {}).get("pass"),
        "t1_total": (result.get("t1") or {}).get("total"),
        "t4_ok": (result.get("t4") or {}).get("ok"),
        "fuyao_pit_ok": (result.get("fuyao") or {}).get("ok"),
        "latency_max_s": ((result.get("latency") or {}).get("max_s")
                          if result.get("run") == "latency"
                          else (prev.get("latency_samples") or [{}])[-1].get("max_s")
                          if prev.get("latency_samples") else None),
    }
    with open(os.path.join(OBS_DIR, "summary.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--daily", action="store_true")
    g.add_argument("--latency", action="store_true")
    args = ap.parse_args()
    today = _beijing_today()
    result: Dict[str, Any] = {
        "date": today,
        "ts_utc": dt.datetime.utcnow().isoformat(timespec="seconds"),
        "run": "daily" if args.daily else "latency",
        "gate": "Gate 1 观察期（重设计 §6；零写入只读）",
    }
    t0 = time.monotonic()
    if args.daily:
        result["t1"] = obs_t1(today)
        result["t2"] = obs_t2(today)
        result["t3"] = obs_t3(today)
        result["t7"] = obs_t7(today)
        result["t4"] = obs_t4()
        result["fuyao"] = obs_fuyao()
    else:
        result["latency"] = obs_latency()
    result["elapsed_s"] = round(time.monotonic() - t0, 1)
    path = _write_day(result)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    print(f"[observation] 落盘 {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
