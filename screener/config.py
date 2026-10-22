# -*- coding: utf-8 -*-
"""screener.config —— v7 数据客户端库的最小配置入口。

v7.0 定位：screener 已退化为**数据客户端库**（screener/data/{baostock_client,
tencent,sina,rf}.py，供 lake/ingest 与 scripts/lake_backfill 只读复用）。策略参数
的权威校验在 ``martingale.config``（新 schema）；Web 的 strategy 读写亦用
``martingale.config.load_config`` 做严格校验。

本模块只保留 lake_backfill 的两个消费点（lake_backfill:1229/1929）：
- ``load_config(path)``：读 strategy.yaml → 原始 dict（轻量结构校验：文件存在、
  顶层 mapping、v7 必备顶层段 strategy/universe/lake 存在；datasource 缺失不报错
  —— sina_cfg/rf_cfg 自带缺省值）；
- ``sina_cfg(cfg)`` / ``rf_cfg(cfg)``：datasource.sina / datasource.rf 参数提取
  （含 D9'/D4' 限速纪律校验，语义不变）。
"""
from __future__ import annotations

import os
from typing import Any, Dict

import yaml


class ConfigError(ValueError):
    """配置缺失或非法。"""


def load_config(path: str) -> Dict[str, Any]:
    """读取 strategy.yaml，返回原始 dict（轻量结构校验）。

    v7 slim schema：顶层 strategy / universe / lake 必备（缺 → ConfigError，
    避免 lake-sync 在坏配置上静默跑偏）；datasource 段可选（sina_cfg/rf_cfg
    键缺失时回退各自默认值）。
    """
    if not os.path.exists(path):
        raise ConfigError(f"配置文件不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if not isinstance(cfg, dict):
        raise ConfigError("strategy.yaml 顶层必须是映射(mapping)")
    for key in ("strategy", "universe", "lake"):
        if not isinstance(cfg.get(key), dict):
            raise ConfigError(f"strategy.yaml 缺少 {key} 段（v7 slim schema 必备）")
    return cfg


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
