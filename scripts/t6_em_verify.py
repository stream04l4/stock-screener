#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""t6_em_verify —— T6 前十大股东 EM 备源验证（2026-11-27，Joel 拍板③：实测一次）。

**目的**：source_pool T6 条目东财挂「备源(待验证)」——本脚本实测 EM F10 股东
研究端点（emweb.securities.eastmoney.com/PC_HSF10/ShareholderResearch/PageAjax）
与湖内 sina_f10 行的对标质量，产出「收为备源 / 不收」的判据数据。

**口径**：
- 样本：湖内 holders_snapshot 随机 20 股（固定 seed，可复现）× 每股湖内
  **最新 as_of_date**（= 新浪 F10 页最新报告期；对 EM 同 END_DATE 期）。
- 端点：PageAjax 返回 ``sdgd``（前十大股东，与新浪 vCI 页同口径——实测
  sh.600519 rank1 茅台集团 54.5% 与湖内 54.499% 一致）；字段
  HOLDER_RANK/HOLDER_NAME/HOLD_NUM/HOLD_NUM_RATIO(%)。
- 判定（每 rank 行）：名称归一化后精确匹配（全角/半角括号、空白统一）+
  hold_ratio 相对差 ≤0.05pp → ok；名称不匹配或 ratio 超阈 → diff；
  湖有 EM 无该期 → em_no_data。
- **只读**：不写湖、不触发 backfill；仅 1 次 EM 请求/股（限速 0.5s）。

**用法**：``.venv/bin/python scripts/t6_em_verify.py [--n 20] [--out /tmp/...]``
**结果**：JSON 落盘 + 控制台汇总（ok/diff/em_no_data 计数 + 样例）。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
import urllib.request
from typing import Any, Dict, List, Optional

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(REPO, "data", "lake", "lake.duckdb")

EM_URL = ("https://emweb.securities.eastmoney.com/PC_HSF10/ShareholderResearch/"
          "PageAjax?code={code6}")
EM_HEADERS = {"Referer": "https://emweb.securities.eastmoney.com/",
              "User-Agent": "Mozilla/5.0"}

RATIO_TOL_PP = 0.05    # 比例容差（百分点；同分母口径下的舍入差 <0.05pp）
REL_TOL_PCT = 3.5      # 相对容差（%）：湖内 sina 比例分母=流通股本（页面时点）、
                       # EM 分母=报告期总股本 → 系统性 +3% 左右分母差归 basis_diff


def _norm_name(s: str) -> str:
    """名称归一化：全角括号/破折号→半角、去空白（同一实体两源写法差）。"""
    s = (s or "").strip()
    s = s.replace("（", "(").replace("）", ")").replace("－", "-")
    s = re.sub(r"\s+", "", s)
    return s


def _fetch_em_sdgd(code6: str, retries: int = 3) -> Optional[List[Dict[str, Any]]]:
    """抓单股 PageAjax → sdgd 行列表（失败 None；限速 0.5s 在调用方）。"""
    for i in range(retries):
        try:
            req = urllib.request.Request(EM_URL.format(code6=code6),
                                         headers=EM_HEADERS)
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
            rows = d.get("sdgd") or []
            return rows if isinstance(rows, list) else None
        except Exception as exc:  # noqa: BLE001 - 网络/解析失败 → 重试后 None
            if i == retries - 1:
                print(f"  ! EM 请求失败 {code6}: {exc}", file=sys.stderr)
                return None
            time.sleep(2 * (i + 1))
    return None


def _em_period_map(rows: List[Dict[str, Any]], end_date: str) -> Dict[int, Dict[str, Any]]:
    """sdgd 行按 END_DATE 过滤 → {rank: row}。"""
    out: Dict[int, Dict[str, Any]] = {}
    for r in rows:
        if str(r.get("END_DATE", ""))[:10] == end_date:
            try:
                out[int(r["HOLDER_RANK"])] = r
            except (KeyError, ValueError, TypeError):
                continue
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20, help="样本股数（默认 20）")
    ap.add_argument("--seed", type=int, default=20261127)
    ap.add_argument("--out", default="/tmp/t6_em_verify.json")
    args = ap.parse_args()

    import duckdb
    con = duckdb.connect(DB, read_only=True)

    # 样本：湖内有最新期数据的股票（as_of_date 非空），随机 n 只
    pool = [r[0] for r in con.execute(
        "SELECT DISTINCT ts_code FROM holders_snapshot "
        "WHERE as_of_date IS NOT NULL").fetchall()]
    random.seed(args.seed)
    sample = random.sample(pool, min(args.n, len(pool)))

    results: List[Dict[str, Any]] = []
    for ts_code in sample:
        # EM PageAjax 需带交易所前缀（sh600519/sz000001；纯 6 位→"股票代码不合法"）
        em_code = ts_code.replace(".", "")
        row = con.execute(
            "SELECT as_of_date, COUNT(*) FROM holders_snapshot "
            "WHERE ts_code=? GROUP BY 1 ORDER BY 1 DESC LIMIT 1",
            [ts_code]).fetchone()
        if not row:
            continue
        end_date = str(row[0])[:10]
        lake_rows = con.execute(
            "SELECT holder_rank, holder_name, hold_ratio FROM holders_snapshot "
            "WHERE ts_code=? AND as_of_date=? ORDER BY holder_rank",
            [ts_code, end_date]).fetchall()
        em_rows = _fetch_em_sdgd(em_code)
        time.sleep(0.5)
        em_map = _em_period_map(em_rows or [], end_date)
        period_mismatch = False
        if not em_map and em_rows and any(r.get("END_DATE") for r in em_rows):
            # 湖内 as_of_date 非标准季度期日（如 2026-11-04 披露日口径）→
            # 回退比 EM 最新期（内容可比，仅期日标注口径差）→ 打标不误判失败
            latest = max(str(r.get("END_DATE", ""))[:10] for r in em_rows
                         if r.get("END_DATE"))
            em_map = _em_period_map(em_rows, latest)
            period_mismatch = True

        per_rank: List[Dict[str, Any]] = []
        for rank, name, ratio in lake_rows:
            er = em_map.get(rank)
            if er is None:
                status = "em_no_data"
            else:
                ename = str(er.get("HOLDER_NAME") or "")
                eratio = er.get("HOLD_NUM_RATIO")
                name_ok = _norm_name(name) == _norm_name(ename)
                if not name_ok:
                    status = "list_diff"   # 名单不一致（两源真分歧；EM 侧未必错——
                else:                      # 湖内 sina 页陈旧时恰是备源存在的理由）
                    if ratio is None or eratio is None:
                        status = "ratio_diff"
                    else:
                        dr = abs(float(ratio) - float(eratio))
                        rel = dr / max(1e-9, float(eratio)) * 100.0
                        if dr <= RATIO_TOL_PP:
                            status = "ok"
                        elif rel <= REL_TOL_PCT:
                            status = "basis_diff"   # 分母口径差（流通 vs 总股本）
                        else:
                            status = "ratio_diff"
            per_rank.append({
                "rank": rank, "lake_name": name, "lake_ratio": ratio,
                "em_name": (er or {}).get("HOLDER_NAME"),
                "em_ratio": (er or {}).get("HOLD_NUM_RATIO"),
                "status": status,
            })
        cnt = {s: sum(1 for p in per_rank if p["status"] == s)
               for s in ("ok", "basis_diff", "ratio_diff", "list_diff", "em_no_data")}
        results.append({
            "ts_code": ts_code, "end_date": end_date,
            "period_mismatch": period_mismatch,
            "lake_rows": len(per_rank), **cnt,
            "periods_in_em": sorted({str(r.get("END_DATE", ""))[:10]
                                     for r in (em_rows or []) if r.get("END_DATE")}),
            "diffs": [p for p in per_rank if p["status"] in
                      ("ratio_diff", "list_diff")][:5],
        })
        print(f"{ts_code} {end_date}: ok={cnt['ok']} basis={cnt['basis_diff']} "
              f"ratio={cnt['ratio_diff']} list={cnt['list_diff']} "
              f"nodata={cnt['em_no_data']} (em期数={len(results[-1]['periods_in_em'])})")

    con.close()

    tot = {k: sum(r[k] for r in results)
           for k in ("lake_rows", "ok", "basis_diff", "ratio_diff",
                     "list_diff", "em_no_data")}
    tot["stocks"] = len(results)
    tot["em_missing"] = sum(1 for r in results if not r["periods_in_em"])
    tot["agreement_pct"] = round(
        100.0 * (tot["ok"] + tot["basis_diff"]) / max(1, tot["lake_rows"]), 1)
    # 判据：EM 端点全覆盖 + 名称&比例(含分母容差)一致 ≥80% → 收为备源。
    # list_diff=两源名单真分歧（湖内 sina 页陈旧时恰是备源存在的理由，不计 EM 缺陷）；
    # ratio_diff=名单同但比例超容差（EM 侧质量疑点，单独跟踪）。
    verdict = (
        "PASS（EM 全覆盖且一致率 ≥80%，收为备源）"
        if tot["em_missing"] == 0 and tot["agreement_pct"] >= 80.0
        else "FAIL（端点缺失或一致率 <80%，不收/需复核）")
    out = {"as_of": time.strftime("%Y-%m-%d %H:%M:%S"),
           "n_stocks": args.n, "ratio_tol_pp": RATIO_TOL_PP,
           "rel_tol_pct": REL_TOL_PCT,
           "totals": tot, "verdict": verdict, "per_stock": results}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n==== 汇总: {tot} → {verdict}")
    print(f"结果 JSON: {args.out}")
    return 0 if ("PASS" in verdict) else 1


if __name__ == "__main__":
    sys.exit(main())
