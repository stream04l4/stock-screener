# -*- coding: utf-8 -*-
"""lake.ingest —— 各表灌入器（全部复用现有客户端，零新抓取逻辑）。

文件级布局（调研报告 §2；v7.0.18 baostock_ingest 随渠道移除）：
- tencent_ingest.py    T2 OHLCV / T3 估值 / T7 指数 —— TencentKlineSource + TencentClient
- sina_ingest.py       T6 股东 / T5.ocf(MANANETR) —— SinaClient（限速+WAF退避）
- em_dividend_ingest.py T4：读 cache/em_dividend_all.csv（零网络，sina.load_local_dividends）
- em_batch_ingest.py     T2/T3/adj 全市场批量 + T1 clist 快照 + T4 事件区间
  （2026-11-15 EM 拍板转正；2026-11-26 tushare 弃用后=唯一主通道，tushare_ingest.py 整删）
- local_cache_ingest.py 从现有 cache/*.csv bootstrap T2/T5 历史段
- common.py            QuotaGuard 等公共件（v7.0.18 从 baostock_client 迁出）

边界：只 import screener.data.*（只读客户端），绝不反向被主路径 import。
"""
from __future__ import annotations
