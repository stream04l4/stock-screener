# -*- coding: utf-8 -*-
"""lake.config —— 数据湖配置读取（config/strategy.yaml 的 ``lake`` 段）。

**边界**：lake 只读 strategy.yaml（yaml.safe_load），**不 import screener.config**
（避免反向耦合主路径；screener/config.py 不得为 lake 加 accessor——那是主路径改动）。

配置项（缺省值在此，可被 strategy.yaml ``lake:`` 段覆盖）：
- ``baostock_daily_budget``: BaoStock 日预算上限（默认 5000，Q2；到顶当日停、次日续）。

v6.1（多源资源池，Joel Q1-Q6 拍板）新增：
- ``source_priority``: 每表字段组的"主源→fallback"序。**权威性排序（Q1）**：
  新浪(0) > 腾讯(1) > BaoStock(2) > tdx/adata(3) > 本地推导(4)。数值越小越权威，
  冲突裁决取 authority 更小者——**不用时间戳新者优先**（历史回灌场景低权威源
  重跑覆盖高权威源方向反了，见 research_report_multisource §4.1）。
- ``crosscheck_thresholds``: 跨源交叉校验阈值（报告 §4.3）——相对差超阈 → 写
  conflict_src + log warning，**不阻断**（取主源值）。
- ``sina.enabled / tdx.enabled / adata_f10.enabled / baostock_probe.enabled``:
  各新源开关（默认全开；关=该源不进池，worker 直接跳过——零回归兜底）。
"""
from __future__ import annotations

import os
from typing import Any, Dict

# ---------------------------------------------------------------------------
# 缺省值（strategy.yaml 未配 lake 段时用）
# ---------------------------------------------------------------------------
_DEFAULTS: Dict[str, Any] = {
    "baostock_daily_budget": 5000,   # Q2：BaoStock 日预算默认 5,000 次/日

    # v6.1 源开关（默认全开；False=该源不进池，worker 跳过）
    "sina_enabled": True,            # 新浪 akshare stock_zh_a_daily（T2 OHLCV/amount 主源）
    "tdx_enabled": True,             # easy-tdx（T2/T7 fallback + T7 amount）
    "adata_f10_enabled": True,       # adata get_core_index（T5 主源，仅 F10）
    "baostock_probe_enabled": True,  # Q6：灌数启动探一次 BaoStock 判活
    # v6.1.4 O4：tencent 原无开关键（腾讯是 legacy 直连兜底+T3/T7 主源）——补齐开关
    # （默认 True=现状不变；False=T2/T7 池跳过 tencent、T3 快照仍走腾讯直连——
    # T3 是"当前时点"数据唯一源，不受本开关门控，见 web_api /sources/toggle 文档）。
    "tencent_enabled": True,

    # v6.1 字段组→源优先级（Q1 拍板序）。key=表名，value={field_group: [src,...]}。
    # resolve_source 按此返回 adapter 序列；worker 取第一个 available() 且成功的源。
    "source_priority": {
        # T2 kline_daily：OHLCV/amount 主源=新浪（全史一次拉全、含 amount、volume=股），
        # fallback=腾讯(分页 n≤2000)→tdx；adj_factor 主源=新浪 hfq÷raw 推导，
        # fallback=tdx hfq÷raw → BaoStock(探测存活时)。
        "kline_daily": {
            "ohlcv_amount": ["sina", "tencent", "tdx"],
            "adj_factor": ["sina", "tdx", "baostock"],
        },
        # T5 fundamentals_quarterly：adata F10 主源；BaoStock 探测存活时交叉校验。
        "fundamentals_quarterly": {
            "f10": ["adata_f10", "baostock"],
        },
        # T7 index_daily：OHLCV 腾讯主源（现状稳定）+ tdx fallback；amount 补 tdx。
        "index_daily": {
            "ohlcv": ["tencent", "tdx"],
            "amount": ["tdx"],
        },
    },

    # v6.1 跨源交叉校验阈值（报告 §4.3；相对差，超阈写 conflict_src+告警、不阻断）
    "crosscheck_thresholds": {
        "t2_close_pct": 0.5,          # T2 close：腾讯 vs tdx vs 新浪(≥2源) >0.5%
        "t2_amount_pct": 2.0,         # T2 amount：tdx vs 新浪 >2%（金额单位/浮点容忍略宽）
        "t2_adj_factor_pct": 0.5,     # T2 adj_factor：末因子 hfq/raw 推导对比 >0.5%（从严，af 误差累积进 view）
        "t7_close_pct": 0.3,          # T7 close：腾讯 vs tdx >0.3%（指数点位精度要求高，更严）
        "t5_pp": 1.0,                 # T5 roe/gross_margin/liability_pct：adata vs BaoStock >1pp（绝对百分点）
    },

    # v6.1 限速纪律（秒/股；worker 内 sleep，brief 红线）
    "sina_min_interval_s": 1.0,       # 新浪 ≥1s/股（2 次调用=raw+hfq，留余量）
    "tdx_min_interval_s": 0.5,        # tdx ≥0.5s/股
    "adata_f10_min_interval_s": 1.0,  # adata F10 ≥1s/股

    # DEFECT-HANG-1（R3）：进度停滞看门狗阈值（分钟）。progress 文件 mtime 连续无推进
    # 超此值 → 主线程 SIGINT abort + 干净退出（下次续传）。缺省 10min（brief R3"建议
    # 10min，config 可调"）；正常最坏任务间隔 = 单取数墙钟上限(30s)×源数 + 限速 ≈ 分钟级，
    # 10min 对健康运行零误杀。<=0 → 看门狗禁用（不启动）。
    "hang_stall_minutes": 10.0,
}


def _project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def strategy_yaml_path() -> str:
    """strategy.yaml 绝对路径（**独立函数**：O4 toggle 写盘 + 测试 monkeypatch 注入点）。

    lake_cfg() 读与 set_lake_source_switch() 写都经此函数——测试 patch 本函数指向
    tmp 副本即可全离线验证写盘，绝不触碰生产 config/strategy.yaml（brief 红线）。
    """
    return os.path.join(_project_root(), "config", "strategy.yaml")


def set_lake_source_switch(name: str, enabled: bool) -> Dict[str, Any]:
    """v6.1.4 O4：写 strategy.yaml ``lake`` 段源开关（ruamel roundtrip 保注释/格式）。

    :param name: 源名（sina/tencent/baostock/tdx/adata_f10）→ 开关键
        ``{name}_enabled``（baostock → ``baostock_probe_enabled``，与 _DEFAULTS 对齐）。
    :param enabled: True/False。

    **写盘纪律**（brief 红线）：
    - **ruamel roundtrip**：注释/键序/缩进逐字保留，只改目标布尔值；文件缺失 →
      新建最小 ``lake:`` 段（不伪造其余内容）。
    - **写前备份 .bak**：``strategy.yaml.bak``（覆盖式——最近一次写入前的状态；
      原子性=先写 tmp 再 rename，.bak 与最终文件同目录）。
    - **立即生效**：lake_cfg() 每次调用现读文件（无进程级缓存，见 lake_cfg docstring）
      → 下次 resolve_source/灌数启动读到新值；**运行中灌数不受影响**（adapter 池在
      启动时构建，文档注明"下次启动生效"）。

    :return: ``{"name", "key", "enabled", "path", "backup"}``（调用方回显/日志）。
    :raises ValueError: name 不在 5 源白名单（不猜键名——写错键=配置静默失效）。
    """
    key = {"sina": "sina_enabled", "tencent": "tencent_enabled",
           "baostock": "baostock_probe_enabled", "tdx": "tdx_enabled",
           "adata_f10": "adata_f10_enabled"}.get(name)
    if key is None:
        raise ValueError(f"未知源名: {name!r}（白名单 sina/tencent/baostock/tdx/adata_f10）")

    from ruamel.yaml import YAML   # 项目依赖（screener 配置工具链已用；roundtrip 保注释）

    yaml_rt = YAML()
    yaml_rt.preserve_quotes = True          # 引号风格保留（'true' vs "true" 不漂移）
    path = strategy_yaml_path()
    backup = path + ".bak"

    doc = None
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            doc = yaml_rt.load(f) or {}
        # 写前备份（.bak=写入前的完整文件；覆盖式，保留最近一份）
        import shutil
        shutil.copyfile(path, backup)
    else:
        doc = {}   # 缺失 → 新建最小文档（只含 lake 段目标键；不伪造其余内容）

    if not isinstance(doc, dict):
        raise ValueError(f"strategy.yaml 顶层非 mapping（{type(doc).__name__}）——拒绝写入")
    lake = doc.get("lake")
    if not isinstance(lake, dict):
        lake = {}
        doc["lake"] = lake
    # ruamel CommentedMap：直接赋值保留既有注释；新键追加到段尾（roundtrip 保序）
    lake[key] = bool(enabled)

    # 原子写：tmp + rename（同目录，防半截文件被灌数进程读到）
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        yaml_rt.dump(doc, f)
    os.replace(tmp, path)
    return {"name": name, "key": key, "enabled": bool(enabled),
            "path": path, "backup": backup if os.path.exists(backup) else None}


def lake_cfg() -> Dict[str, Any]:
    """读 strategy.yaml 的 ``lake`` 段，叠加缺省值。文件缺失/无 lake 段 → 纯缺省。

    v6.1：嵌套 dict（source_priority/crosscheck_thresholds）做**浅合并**——yaml 只写
    部分字段组时不整块覆盖缺省（防 yaml 半配置导致某表优先级丢失）。
    """
    cfg = _deep_default()
    path = strategy_yaml_path()   # v6.1.4 O4：与写盘同一路径函数（测试 monkeypatch 注入点）
    try:
        import yaml

        with open(path, "r", encoding="utf-8") as f:
            doc = yaml.safe_load(f) or {}
        lake = doc.get("lake")
        if isinstance(lake, dict):
            for k in _DEFAULTS:
                if k in lake and lake[k] is not None:
                    v = lake[k]
                    if isinstance(_DEFAULTS[k], dict) and isinstance(v, dict):
                        merged = dict(_DEFAULTS[k])
                        merged.update(v)  # 浅合并：yaml 显式值优先，缺的字段组保留缺省
                        cfg[k] = merged
                    else:
                        cfg[k] = v
    except (OSError, ValueError):
        pass  # 配置不可用 → 纯缺省（不阻断）
    return cfg


def _deep_default() -> Dict[str, Any]:
    """缺省值深拷贝一层（嵌套 dict 单独复制，防调用方改缺省污染）。"""
    out: Dict[str, Any] = {}
    for k, v in _DEFAULTS.items():
        out[k] = dict(v) if isinstance(v, dict) else v
    return out


def source_priority(table: str, field_group: str) -> list:
    """取 (表, 字段组) 的源优先级序列（缺省兜底：未知表/组 → 空列表=单源旧行为）。"""
    sp = lake_cfg().get("source_priority", {}) or {}
    groups = sp.get(table, {}) or {}
    order = groups.get(field_group, []) or []
    return list(order)


def crosscheck_threshold(name: str, default: float) -> float:
    """取交叉校验阈值（缺省兜底）。"""
    th = lake_cfg().get("crosscheck_thresholds", {}) or {}
    v = th.get(name, default)
    try:
        return float(v)
    except (TypeError, ValueError):
        return default
