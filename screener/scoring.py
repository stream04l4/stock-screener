# -*- coding: utf-8 -*-
"""v2 多因子打分引擎（Z-Score 截面标准化，调研报告 R4）。

设计（TL 拍板 + 报告 R4）：
- **截面集合** = 硬性剔除（ST/上市未满 N 日）后的全体候选。每个子因子在该集合上
  独立算 z=(x-mean)/std（样本标准差 n-1；std=0 → 该因子所有 z=0）。
- **缺失处理**（missing_policy）：
  - neutral_renorm（默认）：缺失因子 z=0，且维度分/综合分按"可用子因子权重之和"
    归一化 → 既给中性贡献又保持跨股可比；
  - neutral：缺失 z=0，不重归一化（数据不全的股票得分被稀释）；
  - drop：任一子因子缺失 → 该维度记 None、不参与合成（其权重质量直接丢失，
    综合分 = Σ 可用维度 weight×z_dim，**不**再按可用维度权重归一化——比
    neutral_renorm 更严格，数据不全的股票被降权）。
- **维度合成**：维度内子因子按 sub_weights 加权平均 → 维度 z；
  total_score = Σ weight × z_dim。
- 输出 Top N（total_score 降序）。

本模块为纯函数层（不联网），全部输入是"每只股票的原始因子值 dict"，便于离线单测。
权重/子权重/top_n/missing_policy 全部由调用方从 config 传入——代码零硬编码。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

# 四个维度（顺序固定，与 config scoring.weights 的键一致）
DIMENSIONS = ("technical", "dividend", "industry", "fundamental")


def _mean_std(values: Sequence[float]) -> tuple[float, float]:
    """样本均值 + 样本标准差（n-1）。n<2 → std=0。"""
    n = len(values)
    if n == 0:
        return 0.0, 0.0
    mean = sum(values) / n
    if n < 2:
        return mean, 0.0
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return mean, math.sqrt(var)


def zscore_series(values: Sequence[Optional[float]]) -> List[float]:
    """对一列因子值做截面 Z-Score。

    - 缺失（None）位置 → 0.0（中性；是否重归一化由调用方按 missing_policy 处理）。
    - std=0（全体相同或仅 1 个有效值）→ 全部 z=0（避免除零，且"无区分度"因子
      不应贡献任何得分）。
    """
    valid = [v for v in values if v is not None]
    mean, std = _mean_std(valid)
    out: List[float] = []
    for v in values:
        if v is None or std == 0.0:
            out.append(0.0)
        else:
            out.append((v - mean) / std)
    return out


@dataclass
class ScoredStock:
    """单只股票的打分结果。"""

    code: str
    # 子因子原始值 {dim: {factor: value|None}}
    raw: Dict[str, Dict[str, Optional[float]]] = field(default_factory=dict)
    # 子因子 z 值（缺失→0）{dim: {factor: z}}
    z_factors: Dict[str, Dict[str, float]] = field(default_factory=dict)
    # 维度 z（合成后）{dim: z|None}
    z_dims: Dict[str, Optional[float]] = field(default_factory=dict)
    # 维度得分 = weight × z_dim {dim: score|None}
    scores: Dict[str, Optional[float]] = field(default_factory=dict)
    total_score: float = 0.0
    rank: int = 0
    top_n_selected: bool = False
    na_factors: List[str] = field(default_factory=list)  # 缺失因子名（逗号分隔存 CSV）


def score_cross_section(
    stocks: Sequence[Dict[str, Any]],
    weights: Dict[str, float],
    sub_weights: Dict[str, Dict[str, float]],
    top_n: int,
    missing_policy: str = "neutral_renorm",
) -> List[ScoredStock]:
    """对全体候选做截面 Z-Score 打分，返回按 total_score 降序的 ScoredStock 列表。

    :param stocks: [{"code":..., "factors": {dim: {factor_name: value|None}}}...]
        —— 硬剔除后的全体候选（每个维度下是子因子原始值映射）。
    :param weights: {dim: weight}，和=1（config scoring.weights）。
    :param sub_weights: {dim: {factor: w}}，每维度和=1（config scoring.sub_weights）。
        某维度缺省 → 该维度内等权。
    :param top_n: 榜单前 N 名。
    :param missing_policy: neutral_renorm | neutral | drop。
    """
    codes = [s["code"] for s in stocks]
    # 收集每个 (dim, factor) 的因子列（按 stocks 顺序对齐）
    dim_factors: Dict[str, List[str]] = {}
    for dim in DIMENSIONS:
        seen: List[str] = []
        for s in stocks:
            f = (s.get("factors") or {}).get(dim) or {}
            for k in f:
                if k not in seen:
                    seen.append(k)
        # 以 sub_weights 的键为基准（保证顺序稳定、覆盖全部配置因子）
        base = list((sub_weights.get(dim) or {}).keys())
        ordered = [k for k in base if k in seen] + [k for k in seen if k not in base]
        dim_factors[dim] = ordered

    # 每个 (dim, factor) 的截面 z 列
    z_cols: Dict[str, Dict[str, List[float]]] = {}
    for dim in DIMENSIONS:
        cols: Dict[str, List[float]] = {}
        for f in dim_factors[dim]:
            series = [((s.get("factors") or {}).get(dim) or {}).get(f) for s in stocks]
            cols[f] = zscore_series(series)
        z_cols[dim] = cols

    out: List[ScoredStock] = []
    for i, s in enumerate(stocks):
        code = s["code"]
        factors = (s.get("factors") or {})
        res = ScoredStock(code=code, raw={d: dict((factors.get(d) or {})) for d in DIMENSIONS})
        res.z_factors = {d: {} for d in DIMENSIONS}
        # 缺失因子清单
        for dim in DIMENSIONS:
            for f in dim_factors[dim]:
                if res.raw[dim].get(f) is None:
                    res.na_factors.append(f"{dim}.{f}")

        total = 0.0
        for dim in DIMENSIONS:
            w_dim = float(weights.get(dim, 0.0))
            sub_w = {f: float(w) for f, w in (sub_weights.get(dim) or {}).items()}
            if not sub_w:  # 维度无子权重配置 → 等权
                keys = dim_factors[dim]
                sub_w = {k: 1.0 / len(keys) for k in keys} if keys else {}

            z_dim: Optional[float] = None
            if missing_policy == "drop":
                # 任一子因子缺失 → 该维度 None（不参与综合分）
                dim_vals = [res.raw[dim].get(f) for f in sub_w]
                if any(v is None for v in dim_vals):
                    z_dim = None
                else:
                    num = sum(sub_w[f] * z_cols[dim][f][i] for f in sub_w)
                    den = sum(sub_w.values())
                    z_dim = num / den if den > 0 else 0.0
            else:
                # neutral / neutral_renorm：缺失因子 z=0（z_cols 已把 None→0）
                num = sum(sub_w[f] * z_cols[dim][f][i] for f in sub_w)
                if missing_policy == "neutral_renorm":
                    # 按"可用子因子权重之和"归一化（等价降权缺失因子）
                    avail = sum(
                        sub_w[f] for f in sub_w if res.raw[dim].get(f) is not None
                    )
                    z_dim = num / avail if avail > 0 else 0.0
                else:  # neutral：不重归一化
                    den = sum(sub_w.values())
                    z_dim = num / den if den > 0 else 0.0

            res.z_dims[dim] = z_dim
            score = None if z_dim is None else w_dim * z_dim
            res.scores[dim] = score
            if score is not None:
                total += score
            elif missing_policy == "neutral_renorm":
                # 维度整体缺失（drop 才会 None；此处兜底）：按可用维度权重归一化在下方统一处理
                pass

        # neutral_renorm：若某维度为 None，综合分按"可用维度权重之和"归一化
        if missing_policy == "neutral_renorm":
            avail_w = sum(
                float(weights.get(d, 0.0)) for d in DIMENSIONS if res.scores[d] is not None
            )
            total = total / avail_w if avail_w > 0 else 0.0

        res.total_score = total
        out.append(res)

    # 排序：total_score 降序，code 升序兜底（稳定、可复现）
    out.sort(key=lambda r: (-r.total_score, r.code))
    for i, r in enumerate(out):
        r.rank = i + 1
        r.top_n_selected = i < top_n
    return out


def dimension_means(
    stocks: Sequence[Dict[str, Any]],
) -> Dict[str, Dict[str, Optional[float]]]:
    """每个 (dim, factor) 的截面均值（供报告"四维分解"章节展示）。缺失不参与。"""
    means: Dict[str, Dict[str, Optional[float]]] = {}
    for dim in DIMENSIONS:
        per: Dict[str, Optional[float]] = {}
        seen: List[str] = []
        for s in stocks:
            f = (s.get("factors") or {}).get(dim) or {}
            for k in f:
                if k not in seen:
                    seen.append(k)
        for f in seen:
            vals = [((s.get("factors") or {}).get(dim) or {}).get(f) for s in stocks]
            valid = [v for v in vals if v is not None]
            per[f] = (sum(valid) / len(valid)) if valid else None
        means[dim] = per
    return means
