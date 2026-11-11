# -*- coding: utf-8 -*-
"""crypto.positions —— P2：我的持仓（预留代码落点）。

规划：crypto_positions 表（models.py 加 DDL）+ /api/positions 实体端点
（web/app.py）+ 持仓页（web/frontend/src/tabs/PositionsTab.vue）+ 可选
PushPlus 告警（state 落 data/crypto/alerts/）。
参照股票侧：web/app.py /api/my_positions + MyPositionsTab.vue 的口径
（手动登记、成本/数量、盈亏展示）。启动前与 Joel 确认账本归属（分账本 vs
合一本账，设计文档 §9）。
"""
