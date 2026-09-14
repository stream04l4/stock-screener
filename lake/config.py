# -*- coding: utf-8 -*-
"""lake.config —— 数据湖配置读取（config/strategy.yaml 的 ``lake`` 段）。

**边界**：lake 只读 strategy.yaml（yaml.safe_load），**不 import screener.config**
（避免反向耦合主路径；screener/config.py 不得为 lake 加 accessor——那是主路径改动）。

配置项（缺省值在此，可被 strategy.yaml ``lake:`` 段覆盖）：
- ``baostock_daily_budget``: BaoStock 日预算上限（默认 5000，Q2；到顶当日停、次日续）。
"""
from __future__ import annotations

import os
from typing import Any, Dict

# 缺省值（strategy.yaml 未配 lake 段时用）
_DEFAULTS: Dict[str, Any] = {
    "baostock_daily_budget": 5000,   # Q2：BaoStock 日预算默认 5,000 次/日
}


def _project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def lake_cfg() -> Dict[str, Any]:
    """读 strategy.yaml 的 ``lake`` 段，叠加缺省值。文件缺失/无 lake 段 → 纯缺省。"""
    cfg = dict(_DEFAULTS)
    path = os.path.join(_project_root(), "config", "strategy.yaml")
    try:
        import yaml

        with open(path, "r", encoding="utf-8") as f:
            doc = yaml.safe_load(f) or {}
        lake = doc.get("lake")
        if isinstance(lake, dict):
            for k in _DEFAULTS:
                if k in lake and lake[k] is not None:
                    cfg[k] = lake[k]
    except (OSError, ValueError):
        pass  # 配置不可用 → 纯缺省（不阻断）
    return cfg
