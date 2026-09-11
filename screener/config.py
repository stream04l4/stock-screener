# -*- coding: utf-8 -*-
"""策略配置加载与校验。

所有筛选阈值都来自 config/strategy.yaml —— 代码里不得出现任何硬编码阈值。
本模块只做"读取 + 结构校验"，不做业务判断。
"""
from __future__ import annotations

import os
from typing import Any, Dict, List

import yaml


class ConfigError(ValueError):
    """配置缺失或非法。"""


def _require(cfg: Dict[str, Any], dotted: str) -> Any:
    cur: Any = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise ConfigError(f"strategy.yaml 缺少配置项: {dotted}")
        cur = cur[part]
    return cur


def load_config(path: str) -> Dict[str, Any]:
    """读取 strategy.yaml，返回原始 dict（已做结构校验）。"""
    if not os.path.exists(path):
        raise ConfigError(f"配置文件不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if not isinstance(cfg, dict):
        raise ConfigError("strategy.yaml 顶层必须是映射(mapping)")

    # --- 结构校验：缺项直接报错，避免运行到一半才发现 ---
    for key in (
        "technical.ma_period",
        "technical.return_window_days",
        "technical.min_return_pct",
        "technical.max_return_pct",
        "technical.max_annual_volatility_pct",
        "dividend.window_days",
        "dividend.min_yield_pct",
        "fundamental.roe_min_pct",
        "fundamental.net_profit_yoy_field",
        "fundamental.liability_max_pct",
        "fundamental.gross_margin_min_pct",
        "fundamental.probe_quarters_back",
        "industry.rank_by",
        "industry.top_pct",
        "industry.min_group_size",
        "universe.a_share_prefixes",
        "universe.listing_min_trading_days",
        "data.kline_calendar_days_back",
        # v2 打分模型（scoring）与 badge / 硬剔除开关
        "scoring.mode",
        "scoring.top_n",
        "scoring.missing_policy",
        "scoring.weights.technical",
        "scoring.weights.dividend",
        "scoring.weights.industry",
        "scoring.weights.fundamental",
        "scoring.sub_weights.technical",
        "scoring.sub_weights.dividend",
        "scoring.sub_weights.industry",
        "scoring.sub_weights.fundamental",
        "badges.industry_top_pct",
        "badges.fscore_min",
        "hard_filter.st_enabled",
        "hard_filter.listing_min_trading_days",
    ):
        _require(cfg, key)

    # --- 语义校验 ---
    tech = cfg["technical"]
    if not (0 <= float(tech["min_return_pct"]) <= float(tech["max_return_pct"])):
        raise ConfigError("technical.min_return_pct 必须 <= max_return_pct")
    if int(tech["ma_period"]) < 1 or int(tech["return_window_days"]) < 2:
        raise ConfigError("technical.ma_period / return_window_days 非法")

    fund = cfg["fundamental"]
    if fund["net_profit_yoy_field"] not in ("YOYPNI", "YOYNI"):
        raise ConfigError("fundamental.net_profit_yoy_field 只能是 YOYPNI 或 YOYNI")

    ind = cfg["industry"]
    if ind["rank_by"] != "roeAvg":
        raise ConfigError("industry.rank_by 目前只支持 roeAvg")
    if not (0 < float(ind["top_pct"]) <= 100):
        raise ConfigError("industry.top_pct 必须在 (0, 100]")

    uni = cfg["universe"]
    for p in uni["a_share_prefixes"]:
        if not isinstance(p, str) or "." not in p:
            raise ConfigError(f"universe.a_share_prefixes 项非法: {p!r}（应形如 sh.60）")

    # --- v2 scoring 段语义校验 ---
    sc = cfg["scoring"]
    if sc["mode"] not in ("zscore", "legacy"):
        raise ConfigError("scoring.mode 只能是 zscore 或 legacy")
    if int(sc["top_n"]) < 1:
        raise ConfigError("scoring.top_n 必须 >= 1")
    if sc["missing_policy"] not in ("neutral_renorm", "neutral", "drop"):
        raise ConfigError("scoring.missing_policy 只能是 neutral_renorm/neutral/drop")
    w = {k: float(v) for k, v in sc["weights"].items()}
    if len(w) != 4 or any(v < 0 for v in w.values()):
        raise ConfigError("scoring.weights 必须恰好含 technical/dividend/industry/fundamental 且 >= 0")
    # 权重和必须 ≈1（归一化基准；容差 1e-6）
    if abs(sum(w.values()) - 1.0) > 1e-6:
        raise ConfigError(f"scoring.weights 之和必须为 1（当前 {sum(w.values()):.6f}）")
    # sub_weights：每维度的子因子权重和 ≈1
    for dim, subs in sc["sub_weights"].items():
        if not isinstance(subs, dict) or not subs:
            raise ConfigError(f"scoring.sub_weights.{dim} 必须是非空映射")
        s = sum(float(v) for v in subs.values())
        if abs(s - 1.0) > 1e-6:
            raise ConfigError(f"scoring.sub_weights.{dim} 之和必须为 1（当前 {s:.6f}）")

    # --- v2 badge / 硬剔除开关 ---
    bd = cfg["badges"]
    if not (0 < float(bd["industry_top_pct"]) <= 100):
        raise ConfigError("badges.industry_top_pct 必须在 (0,100]")
    if int(bd["fscore_min"]) < 0:
        raise ConfigError("badges.fscore_min 必须 >= 0")
    hf = cfg["hard_filter"]
    if not isinstance(hf["st_enabled"], bool):
        raise ConfigError("hard_filter.st_enabled 必须是布尔值")
    if int(hf["listing_min_trading_days"]) < 1:
        raise ConfigError("hard_filter.listing_min_trading_days 必须 >= 1")

    # --- v3 回测段（可选：主筛选流程不依赖；存在则严格校验）---
    if "backtest" in cfg:
        _validate_backtest(cfg["backtest"])

    return cfg


def _validate_backtest(b: Dict[str, Any]) -> None:
    """backtest 段结构/语义校验（报告 R5 schema）。缺项直接报错。"""
    from datetime import date as _date

    for key in ("start", "end", "rebalance", "top_n", "weights_ref",
                "execution", "costs", "suspension", "benchmarks", "risk_free_pct"):
        if key not in b:
            raise ConfigError(f"backtest.{key} 缺失")

    try:
        start = _date.fromisoformat(str(b["start"]))
        end = _date.fromisoformat(str(b["end"]))
    except ValueError as exc:
        raise ConfigError(f"backtest.start/end 必须是 YYYY-MM-DD: {exc}")
    if not (start < end):
        raise ConfigError("backtest.start 必须早于 end")

    if b["rebalance"] not in ("monthly", "quarterly"):
        raise ConfigError("backtest.rebalance 只能是 monthly 或 quarterly")
    if int(b["top_n"]) < 1:
        raise ConfigError("backtest.top_n 必须 >= 1")
    if b["weights_ref"] != "scoring.weights":
        # 单一事实来源：回测权重只允许引用 scoring.weights（TL 拍板）
        raise ConfigError("backtest.weights_ref 目前只支持 'scoring.weights'")
    if b["execution"] not in ("t1_open", "t1_close", "t_close"):
        raise ConfigError("backtest.execution 只能是 t1_open/t1_close/t_close")

    c = b["costs"]
    for key in ("commission_bp", "min_commission_cny", "transfer_fee_bp",
                "slippage_bp", "delisting_haircut_pct"):
        if key not in c:
            raise ConfigError(f"backtest.costs.{key} 缺失")
        v = float(c[key])
        if v < 0:
            raise ConfigError(f"backtest.costs.{key} 必须 >= 0")
    if not (0 <= float(c["delisting_haircut_pct"]) <= 100):
        raise ConfigError("backtest.costs.delisting_haircut_pct 必须在 [0,100]")
    segs = c.get("stamp_tax_sell", [])
    if not isinstance(segs, list):
        raise ConfigError("backtest.costs.stamp_tax_sell 必须是列表（日期分段）")
    for i, seg in enumerate(segs):
        for k in ("from", "to", "bp"):
            if k not in seg:
                raise ConfigError(f"backtest.costs.stamp_tax_sell[{i}].{k} 缺失")
        try:
            d1 = _date.fromisoformat(str(seg["from"]))
            d2 = _date.fromisoformat(str(seg["to"]))
        except ValueError as exc:
            raise ConfigError(f"stamp_tax_sell[{i}] 日期非法: {exc}")
        if not (d1 <= d2):
            raise ConfigError(f"stamp_tax_sell[{i}].from 必须 <= to")
        if float(seg["bp"]) < 0:
            raise ConfigError(f"stamp_tax_sell[{i}].bp 必须 >= 0")

    s = b["suspension"]
    for key in ("max_defer_days", "on_timeout"):
        if key not in s:
            raise ConfigError(f"backtest.suspension.{key} 缺失")
    if int(s["max_defer_days"]) < 0:
        raise ConfigError("backtest.suspension.max_defer_days 必须 >= 0")
    if s["on_timeout"] not in ("drop_to_cash", "hold"):
        raise ConfigError("backtest.suspension.on_timeout 只能是 drop_to_cash/hold")

    bm = b["benchmarks"]
    if not isinstance(bm, list) or any(not isinstance(x, str) for x in bm):
        raise ConfigError("backtest.benchmarks 必须是代码字符串列表")
    try:
        rf = float(b["risk_free_pct"])
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"backtest.risk_free_pct 必须是数值（年化%）: {exc}")
    if not (-100.0 <= rf <= 100.0):
        raise ConfigError("backtest.risk_free_pct 超出合理范围 [-100,100]")


def tech(cfg: Dict[str, Any]) -> Dict[str, float]:
    """技术面阈值（小数/百分数口径与原配置一致，见各字段名）。"""
    t = cfg["technical"]
    return {
        "ma_period": int(t["ma_period"]),
        "return_window_days": int(t["return_window_days"]),
        # 配置里是百分数(如 100 表示 100%)，统一转成小数供比较
        "min_return": float(t["min_return_pct"]) / 100.0,
        "max_return": float(t["max_return_pct"]) / 100.0,
        "max_vol": float(t["max_annual_volatility_pct"]) / 100.0,
    }


def dividend_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    d = cfg["dividend"]
    return {
        "window_days": int(d["window_days"]),
        # 百分数 → 小数
        "min_yield": float(d["min_yield_pct"]) / 100.0,
    }


def fundamental_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    f = cfg["fundamental"]
    return {
        # 基本面字段在 BaoStock 里是小数（0.10 = 10%），阈值统一转小数比较
        "roe_min": float(f["roe_min_pct"]) / 100.0,
        "yoy_field": str(f["net_profit_yoy_field"]),
        "liability_max": float(f["liability_max_pct"]) / 100.0,
        "gross_margin_min": float(f["gross_margin_min_pct"]) / 100.0,
        "probe_quarters_back": int(f["probe_quarters_back"]),
    }


def industry_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    i = cfg["industry"]
    return {
        "rank_by": str(i["rank_by"]),
        "top_pct": float(i["top_pct"]) / 100.0,
        "min_group_size": int(i["min_group_size"]),
    }


def universe_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    u = cfg["universe"]
    return {
        "prefixes": [str(p) for p in u["a_share_prefixes"]],
        "listing_min_trading_days": int(u["listing_min_trading_days"]),
        "st_name_keyword": str(u.get("st_name_keyword", "ST")),
        # ---- v5 硬过滤（TL D2/D3）：键缺失 → 关闭（v4 配置零回归）----
        # soe_required: 央国企过滤开关（前十大股东关键词/IS_SJKZR，em.py + metrics.soe_flag）
        "soe_required": bool(u.get("soe_required", False)),
        # industry_whitelist_csric2: 证监会二级行业白名单（代码清单显式枚举、可审计）。
        # 空列表 = 不过滤（v4 行为）。
        "industry_whitelist_csric2": [str(x) for x in (u.get("industry_whitelist_csric2") or [])],
        # min_total_mv_yi: 总市值下限（亿元，腾讯快照 idx45）；None = 不启用
        "min_total_mv_yi": (float(u["min_total_mv_yi"]) if u.get("min_total_mv_yi") is not None else None),
    }


def data_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    d = cfg["data"]
    return {
        # 后复权窗口K线往前回溯的日历天数（需覆盖 ma_period/return_window_days 个交易日，
        # 250 个交易日 ≈ 375 个日历天；取 420 留足节假日余量）
        "kline_calendar_days_back": int(d["kline_calendar_days_back"]),
        "retry_max_attempts": int(d.get("retry_max_attempts", 5)),
        "cache_dir": str(d.get("cache_dir", "cache")),
    }


def crosscheck_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    c = cfg.get("crosscheck", {}) or {}
    return {
        "enabled": bool(c.get("enabled", True)),
        "sample_size": int(c.get("sample_size", 20)),
        "price_tolerance_pct": float(c.get("price_tolerance_pct", 0.5)) / 100.0,
        "batch_size": int(c.get("batch_size", 50)),
    }


def datasource_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """v4 数据源架构配置（报告 R3 + TL 修正）。

    ``datasource`` 段**可选**：缺失时返回 primary=baostock 的默认值（现有行为，
    217 测试的 _base_cfg 无此段 → 走原 BaoStock 路径不变）。存在则严格校验结构。
    所有阈值/批量参数都来自 strategy.yaml（零硬编码纪律）；本函数只做读取 + 校验。
    """
    d = cfg.get("datasource")
    if not isinstance(d, dict) or not d:
        # 默认：baostock 主源（回退旧行为），fail_fast 兜底——与 v4 上线前一致
        return {
            "primary": "baostock",
            "fallback": "fail_fast",
            "tencent": {"snapshot_batch_size": 200, "snapshot_interval_s": 0.5,
                        "timeout_s": 15, "max_attempts": 3, "kline_bars": 40},
            "exdate_detector": {"preclose_dev_threshold_pct": 0.5,
                                "factor_sanity_cap_pct": 30,
                                "max_candidates_per_day": 200,
                                "cutover_max_candidates": 300},
            "factor_reconcile": {"weekly_baostock_scan": False, "scan_days_spread": 2,
                                 "skip_during_disclosure": True},
            "contract": {"pct_sample_size": 20, "pct_tolerance_pct": 0.5},
        }
    _validate_datasource(d)
    return d


def _validate_datasource(d: Dict[str, Any]) -> None:
    """datasource 段结构/语义校验（缺项直接报错，避免运行到一半才发现）。"""
    if d.get("primary") not in ("tencent", "baostock"):
        raise ConfigError("datasource.primary 只能是 tencent 或 baostock")
    if d.get("fallback") not in ("fail_fast", "baostock"):
        raise ConfigError("datasource.fallback 只能是 fail_fast 或 baostock")
    t = d.get("tencent", {}) or {}
    for key, lo, hi in (("snapshot_batch_size", 1, 500), ("timeout_s", 1, 300)):
        if int(t.get(key, 200)) < lo or int(t.get(key, 200)) > hi:
            raise ConfigError(f"datasource.tencent.{key} 非法")
    if float(t.get("snapshot_interval_s", 0.5)) < 0:
        raise ConfigError("datasource.tencent.snapshot_interval_s 必须 >= 0")
    if int(t.get("max_attempts", 3)) < 1:
        raise ConfigError("datasource.tencent.max_attempts 必须 >= 1")
    ed = d.get("exdate_detector", {}) or {}
    if float(ed.get("preclose_dev_threshold_pct", 0.5)) <= 0:
        raise ConfigError("datasource.exdate_detector.preclose_dev_threshold_pct 必须 > 0")
    if int(ed.get("max_candidates_per_day", 200)) < 1:
        raise ConfigError("datasource.exdate_detector.max_candidates_per_day 必须 >= 1")
    if int(ed.get("cutover_max_candidates", 300)) < 1:
        raise ConfigError("datasource.exdate_detector.cutover_max_candidates 必须 >= 1")


def scoring_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """v2 打分模型配置（权重/子权重全部来自 strategy.yaml，代码零硬编码）。"""
    s = cfg["scoring"]
    return {
        "mode": str(s["mode"]),
        "top_n": int(s["top_n"]),
        "missing_policy": str(s["missing_policy"]),
        "normalize": str(s.get("normalize", "cross_section")),
        "weights": {k: float(v) for k, v in s["weights"].items()},
        "sub_weights": {d: {k: float(v) for k, v in subs.items()}
                        for d, subs in s["sub_weights"].items()},
    }


def hard_filter_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """硬性剔除开关（ST / 上市天数；v5 追加连续分红年数，键缺失 → None=v4 行为）。"""
    h = cfg["hard_filter"]
    return {
        "st_enabled": bool(h["st_enabled"]),
        "listing_min_trading_days": int(h["listing_min_trading_days"]),
        # v5（TL D1）：连续分红年数下限；新股规则见 metrics.consecutive_div_years 调用方。
        # None = 不启用（v4 配置零回归）。
        "min_consecutive_div_years": (
            int(h["min_consecutive_div_years"]) if h.get("min_consecutive_div_years") is not None else None
        ),
    }


def em_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """v5 东财 datacenter-web 客户端参数（TL D4；键缺失 → 默认值，与 brief §1 一致）。

    全部来自 strategy.yaml datasource.em 段（零硬编码纪律）：
    page_size=500 / interval_s>=0.5（限速）/ timeout_s=15 / max_attempts<=3。
    """
    e = (cfg.get("datasource") or {}).get("em") or {}
    out = {
        # Round-2（TL D-EM）：东财整体开关。false = 禁止任何 datacenter-web 请求；
        # 引擎侧守卫 + 单测断言 enabled=false 时零东财调用。本地静态缓存读取不受影响。
        "enabled": bool(e.get("enabled", False)),
        "page_size": int(e.get("page_size", 500)),
        "interval_s": float(e.get("interval_s", 0.5)),
        "timeout_s": float(e.get("timeout_s", 15)),
        "max_attempts": int(e.get("max_attempts", 3)),
        # 周期性长冷却（防 EM 滑动窗口限流；实测 ~100 页连续请求后"服务器繁忙"）
        "cooldown_every_pages": int(e.get("cooldown_every_pages", 25)),
        "cooldown_seconds": float(e.get("cooldown_seconds", 6.0)),
        # 两次全表级取数之间的静默期（分红→股东；日常增量路径不经过，无成本）
        "full_table_gap_seconds": float(e.get("full_table_gap_seconds", 600)),
        # 10Y 国债 sanity 区间（百分数；越界告警不静默，TL D4）
        "cgb10y_sanity_pct": [float(x) for x in e.get("cgb10y_sanity_pct", [0.5, 4.0])],
    }
    if out["page_size"] < 1 or out["page_size"] > 500:
        raise ConfigError("datasource.em.page_size 必须在 [1,500]")
    if out["interval_s"] < 0.5:
        # brief §1 硬约束：东财非官方接口限速 >=0.5s/页，防触发风控
        raise ConfigError("datasource.em.interval_s 必须 >= 0.5（东财限速纪律）")
    if out["max_attempts"] < 1 or out["max_attempts"] > 3:
        raise ConfigError("datasource.em.max_attempts 必须在 [1,3]（brief §1：重试 <=3 次）")
    lo, hi = out["cgb10y_sanity_pct"]
    if not (0 < lo < hi):
        raise ConfigError("datasource.em.cgb10y_sanity_pct 必须为 0<lo<hi 的百分数区间")
    return out


def soe_keywords_cfg(cfg: Dict[str, Any]) -> List[str]:
    """v5 央国企识别关键词（TL D3，config 驱动；键缺失 → 空=不启用）。"""
    u = cfg.get("universe") or {}
    return [str(k) for k in (u.get("soe_keywords") or [])]


def sina_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """v5 Round-2（TL D3'/D9'）：新浪非官方接口客户端参数。

    全部来自 strategy.yaml datasource.sina 段（零硬编码纪律）：
    interval_s>=1.0（限速）/ timeout_s=15 / max_attempts<=2（重试 <=2 次）。
    键缺失 → 与 D9' 纪律一致的默认值。
    """
    s = (cfg.get("datasource") or {}).get("sina") or {}
    out = {
        "interval_s": float(s.get("interval_s", 1.0)),
        "timeout_s": float(s.get("timeout_s", 15)),
        "max_attempts": int(s.get("max_attempts", 2)),
        # 连续失败熔断阈值（D9'：>=N 只 → 该类因子整体降级 None + 告警，不得死磕）
        "consecutive_fail_breaker": int(s.get("consecutive_fail_breaker", 5)),
        # 财务 JSON 单次取回报告期数（~5 年季频；足够定位最近已披露年报）
        "cf_reports_num": int(s.get("cf_reports_num", 20)),
        # WAF 滑动窗口限速（Round-2 实测：F10 持续 ~10-15 次请求后触发 HTTP 456，
        # 与东财"服务器繁忙"同类）——周期性长冷却 + 456 专用退避，防单跑自触发封禁。
        "cooldown_every_n": int(s.get("cooldown_every_n", 15)),
        "cooldown_s": float(s.get("cooldown_s", 20.0)),
        "waf_backoff_s": float(s.get("waf_backoff_s", 30.0)),
    }
    if out["interval_s"] < 1.0:
        raise ConfigError("datasource.sina.interval_s 必须 >= 1.0（D9' 新浪限速纪律）")
    if out["max_attempts"] < 1 or out["max_attempts"] > 2:
        raise ConfigError("datasource.sina.max_attempts 必须在 [1,2]（D9'：重试 <=2 次）")
    return out


def rf_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """v5 Round-2（TL D4'）：10Y 国债收益率源参数（TradingEconomics）。

    - url：TE 中国 10Y 页面（HTML；解析 JSON-LD Dataset + 正文 "eased/rose to X%"）。
    - fallback_pct：解析失败回退值（%）+ 告警不静默。
    - sanity_pct：[lo, hi] 百分数区间，越界告警不静默（防页面改版漂移）。
    """
    r = (cfg.get("datasource") or {}).get("rf") or {}
    out = {
        "url": str(r.get("url", "https://tradingeconomics.com/china/government-bond-yield")),
        "fallback_pct": float(r.get("fallback_pct", 2.0)),
        "sanity_pct": [float(x) for x in r.get("sanity_pct", [0.5, 4.0])],
    }
    lo, hi = out["sanity_pct"]
    if not (0 < lo < hi):
        raise ConfigError("datasource.rf.sanity_pct 必须为 0<lo<hi 的百分数区间")
    return out


def reinvest_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """v5 再投资参考输出参数（TL D8：按 Joel 目标定档 target_ttm_yield_pct=4.0）。

    报告列 = DPS / (target/100) 参考价 + 当前 TTM 股息率历史分位展示。
    键缺失 → 默认值（不改变主计算路径，纯输出层）。
    """
    r = cfg.get("reinvest") or {}
    return {
        "target_ttm_yield_pct": float(r.get("target_ttm_yield_pct", 4.0)),
        # 历史分位回看年数（dividend_yield_percentile lookback）
        "yield_pctile_lookback_years": int(r.get("yield_pctile_lookback_years", 10)),
    }


def backtest_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """v3 回测配置（strategy.yaml backtest 段；零硬编码，全部来自 config）。

    :raises ConfigError: backtest 段缺失或非法（_validate_backtest 已兜底结构）。
    """
    b = cfg.get("backtest")
    if not isinstance(b, dict):
        raise ConfigError("strategy.yaml 缺少 backtest 段（回测引擎需要）")
    c = b["costs"]
    return {
        "start": str(b["start"]),
        "end": str(b["end"]),
        "rebalance": str(b["rebalance"]),
        "top_n": int(b["top_n"]),
        "weights_ref": str(b["weights_ref"]),
        "execution": str(b["execution"]),
        "costs": {
            "commission_bp": float(c["commission_bp"]),
            "min_commission_cny": float(c["min_commission_cny"]),
            # 初始资金（元）：佣金下限(元)折算归一化净值用；缺省 100 万（兼容旧 config）
            "initial_capital_cny": float(c.get("initial_capital_cny", 1_000_000)),
            "stamp_tax_sell": [
                {"from": str(s["from"]), "to": str(s["to"]), "bp": float(s["bp"])}
                for s in c.get("stamp_tax_sell", [])
            ],
            "transfer_fee_bp": float(c["transfer_fee_bp"]),
            "slippage_bp": float(c["slippage_bp"]),
            "delisting_haircut_pct": float(c["delisting_haircut_pct"]),
        },
        "suspension": {
            "max_defer_days": int(b["suspension"]["max_defer_days"]),
            "on_timeout": str(b["suspension"]["on_timeout"]),
        },
        "benchmarks": [str(x) for x in b["benchmarks"]],
        "risk_free_pct": float(b["risk_free_pct"]),
    }
