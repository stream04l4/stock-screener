# -*- coding: utf-8 -*-
"""martingale.pushplus —— 推送加（PushPlus）微信推送客户端（共享）。

v7.0.13 从 scripts/position_alert.py 抽出（原实现只服务持仓点位提醒；
选股时机提醒 martingale.timing 需要同一通道 → 单一定义点防口径漂移）。

接口：POST http://www.pushplus.plus/send
  {"token": ..., "title": ..., "content": <html>, "template": "html"}
  返回 {"code": 200, ...} 即成功。token 在 .env：PUSHPLUS_TOKEN（禁止入 git/日志）。
"""
from __future__ import annotations

import os
from typing import Optional

PUSHPLUS_SEND_URL = "http://www.pushplus.plus/send"


def load_token(env_path: Optional[str] = None) -> Optional[str]:
    """从 .env 读 PUSHPLUS_TOKEN（手动解析，不依赖 python-dotenv）。

    与 position_alert 原实现逐字一致：先 .env 后环境变量；缺失 → None。
    """
    if env_path is None:
        env_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("PUSHPLUS_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    except OSError:
        pass
    return os.environ.get("PUSHPLUS_TOKEN") or None


def send(token: str, title: str, content: str) -> None:
    """推送一条 html 消息。失败抛 RuntimeError（调用方记痕/重试，勿静默）。"""
    import requests

    resp = requests.post(
        PUSHPLUS_SEND_URL,
        json={"token": token, "title": title, "content": content,
              "template": "html"},
        timeout=15)
    resp.raise_for_status()
    body = resp.json()
    if body.get("code") != 200:
        # 常见：token 无效/过期、账号被封、额度用尽——msg 里带原因，明确报错
        raise RuntimeError(f"pushplus 推送失败: {body.get('msg')} (code={body.get('code')})")
