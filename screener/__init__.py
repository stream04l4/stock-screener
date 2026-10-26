# -*- coding: utf-8 -*-
"""screener —— v7.0 数据客户端库（退化为 lake/ingest 与 lake_backfill 的只读复用层）。

v6.x 的多因子选股/回测管线已整体移除（v7.0 唯一策略 = martingale 包：
深回撤金字塔加仓）。本包只保留：
- config.py     load_config / sina_cfg / rf_cfg（lake_backfill 消费）
- data/         tencent / sina / rf / errors（数据获取客户端 + 中性异常；
                v7.0.18 baostock_client 随渠道移除）

策略逻辑一律在 ``martingale`` 包；数据湖在 ``lake`` 包。三者互不越界
（lake 不得 import martingale；martingale 只读 duckdb 湖，不 import lake）。
"""

__version__ = "7.0.0"
