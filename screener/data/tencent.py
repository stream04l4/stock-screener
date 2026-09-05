# -*- coding: utf-8 -*-
"""腾讯实时行情 qt.gtimg.cn 封装（GBK 转码 + 批量）。

**定位：只做交叉验证 / 实时快照，不进主计算路径**（TL 拍板 #3：股息率分母用
BaoStock 不复权 close af=3）。

接口行为（调研报告 §5，已实测）：
- URL: https://qt.gtimg.cn/q=sh600519,sz000001,... （支持批量逗号分隔）
- **响应 GBK 编码，必须 .decode('gbk')**；无需特殊请求头。
- 每只一行 ``v_sh600519="f0~f1~...";``，字段以 ~ 分隔（共 88 个）。
- 关键字段下标（0-based）：[1]名称 [2]代码 [3]最新价 [30]时间戳
  [32]涨跌幅% [38]换手率% [39]PE(TTM) [44]流通市值(亿) [45]总市值(亿) [46]PB。
"""
from __future__ import annotations

import logging
import re
import time
from typing import Dict, List, Optional

import requests

log = logging.getLogger("screener.data.tencent")

TENCENT_URL = "https://qt.gtimg.cn/q={codes}"

# 字段下标（0-based，调研报告 §5 实测确认）
IDX_NAME = 1
IDX_CODE = 2
IDX_PRICE = 3
IDX_TIMESTAMP = 30
IDX_PCT_CHG = 32
IDX_TURNOVER = 38
IDX_PE_TTM = 39
IDX_FLOAT_MV = 44   # 亿元
IDX_TOTAL_MV = 45   # 亿元
IDX_PB = 46


def bs_code_to_tencent(code: str) -> str:
    """sh.601398 → sh601398（腾讯格式）。"""
    return code.replace(".", "")


class TencentClient:
    """腾讯行情客户端：GBK 转码、批量、失败重试。"""

    def __init__(self, timeout: float = 10.0, max_attempts: int = 3) -> None:
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.session = requests.Session()

    def fetch(self, codes: List[str], batch_size: int = 50) -> Dict[str, Dict[str, Optional[float]]]:
        """批量取行情快照。

        :param codes: BaoStock 格式代码列表（sh.601398）
        :return: {bs_code: {name, price, timestamp, pct_chg, turnover, pe_ttm,
                            float_mv_yi, total_mv_yi, pb}}；未返回的 code 不在结果里。
        """
        out: Dict[str, Dict[str, Optional[float]]] = {}
        for i in range(0, len(codes), batch_size):
            batch = codes[i : i + batch_size]
            t_codes = ",".join(bs_code_to_tencent(c) for c in batch)
            url = TENCENT_URL.format(codes=t_codes)
            text = self._get_text(url)
            if text is None:
                log.warning("腾讯接口批次失败（%d 只），跳过该批", len(batch))
                continue
            parsed = self.parse(text)
            out.update(parsed)
        return out

    def _get_text(self, url: str) -> Optional[str]:
        for attempt in range(1, self.max_attempts + 1):
            try:
                resp = self.session.get(url, timeout=self.timeout)
                if resp.status_code == 200:
                    # GBK 转码封装（核心要求）：接口返回 GBK 字节流
                    return resp.content.decode("gbk", errors="replace")
                log.warning("腾讯接口 HTTP %d (attempt %d)", resp.status_code, attempt)
            except requests.RequestException as exc:
                log.warning("腾讯接口请求失败 (attempt %d): %s", attempt, exc)
            if attempt < self.max_attempts:
                time.sleep(1.0 * attempt)
        return None

    @staticmethod
    def parse(text: str) -> Dict[str, Dict[str, Optional[float]]]:
        """解析腾讯响应文本（已 GBK 转码后的 str）。"""
        out: Dict[str, Dict[str, Optional[float]]] = {}
        for m in re.finditer(r'v_(\w+)="([^"]*)"', text):
            t_code, payload = m.group(1), m.group(2)
            if not payload:
                continue
            f = payload.split("~")
            if len(f) < 47:
                continue
            bs_code = f"{t_code[:2]}.{t_code[2:]}"

            def _f(idx: int) -> Optional[float]:
                try:
                    return float(f[idx])
                except (ValueError, IndexError):
                    return None

            out[bs_code] = {
                "name": f[IDX_NAME],
                "price": _f(IDX_PRICE),
                "timestamp": f[IDX_TIMESTAMP],
                "pct_chg": _f(IDX_PCT_CHG),
                "turnover": _f(IDX_TURNOVER),
                "pe_ttm": _f(IDX_PE_TTM),
                "float_mv_yi": _f(IDX_FLOAT_MV),
                "total_mv_yi": _f(IDX_TOTAL_MV),
                "pb": _f(IDX_PB),
            }
        return out
