# -*- coding: utf-8 -*-
"""lake.ingest —— 各表灌入器（全部复用现有客户端，零新抓取逻辑）。

文件级布局（调研报告 §2；v7.0.18 baostock_ingest 随渠道移除）：
- tencent_ingest.py    T2 OHLCV / T3 估值 / T7 指数 —— TencentKlineSource + TencentClient
- sina_ingest.py       T6 股东 / T5.ocf(MANANETR) —— SinaClient（限速+WAF退避）
- em_dividend_ingest.py T4：读 cache/em_dividend_all.csv（零网络，sina.load_local_dividends）
- tushare_ingest.py    T1 stock_basic 主档（v7.0.18 主源）+ T2/T3 每日增量（token .env）
- local_cache_ingest.py 从现有 cache/*.csv bootstrap T2/T5 历史段
- common.py            QuotaGuard 等公共件（v7.0.18 从 baostock_client 迁出）

边界：只 import screener.data.*（只读客户端），绝不反向被主路径 import。
"""
from __future__ import annotations
