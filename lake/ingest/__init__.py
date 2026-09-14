# -*- coding: utf-8 -*-
"""lake.ingest —— 各表灌入器（全部复用现有客户端，零新抓取逻辑）。

文件级布局（调研报告 §2）：
- baostock_ingest.py   T1/T2(af)/T5 —— BaoStockClient（自带 QuotaGuard）
- tencent_ingest.py    T2 OHLCV / T3 估值 / T7 指数 —— TencentKlineSource + TencentClient
- sina_ingest.py       T6 股东 / T5.ocf(MANANETR) —— SinaClient（限速+WAF退避）
- em_dividend_ingest.py T4：读 cache/em_dividend_all.csv（零网络，sina.load_local_dividends）
- tushare_ingest.py    P2 低频：daily/dividend/daily_basic/adj_factor/index_daily（token .env）
- local_cache_ingest.py 从现有 cache/*.csv bootstrap T2/T5 历史段

边界：只 import screener.data.*（只读客户端），绝不反向被主路径 import。
"""
from __future__ import annotations
