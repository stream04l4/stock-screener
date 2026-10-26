# -*- coding: utf-8 -*-
"""screener.data.errors —— 数据源级异常（中性模块，v7.0.18）。

v7.0.18（2026-10-26，Joel 拍板移除 BaoStock）：BaoStock 渠道完全移除后，
``DataSourceError`` 从 ``baostock_client.py``（文件已删除）迁出到本模块——
sina.py / rf.py 等非 BaoStock 数据源仍引用它，落点须与任何单一渠道解耦。
"""
from __future__ import annotations


class DataSourceError(RuntimeError):
    """数据源级失败的基类。

    语义：数据源不可用/返回无效数据，导致本次运行**无法产出可信结果**。
    与"筛选后 0 只入选"（合法空结果）严格区分——数据源级失败必须显式失败
    （非零退出 + sidecar），绝不写成误导性空 result/report。生产路径失败守卫
    （9/7 缺陷修复）按本类型（含子类）路由到失败路径。
    """
