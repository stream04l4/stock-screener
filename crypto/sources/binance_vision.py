# -*- coding: utf-8 -*-
"""binance_vision —— Binance 官方月度快照（data.binance.vision）全量历史源。

参考 quant_iTransformer historical_pipeline/build_main_features.py 的
fetch_binance_data / load_and_concat_months：月度 ZIP + (币,年,月) 沙盒缓存
+ 403/429 退避重试；此处纯 stdlib（urllib/zipfile/csv）重写，无第三方依赖。

URL 规律（现货 1h）：
  https://data.binance.vision/data/spot/monthly/klines/{SYMBOL}/1h/{SYMBOL}-1h-{YYYY}-{MM}.zip
  - 每月一个 ZIP，内含单 CSV（新文件带表头，老文件无表头——两种都处理）
  - 当月/未发布月 → 404；上市前月份 → 404
  - CSV 列：open_time open high low close volume close_time
            quote_volume count taker_buy_volume taker_buy_quote_volume ignore
  - **open_time 单位随文件年代变化**（2025-01 起实测）：
    老文件毫秒（~1.7e12）/ 新文件微秒（~1.7e15）→ 按数量级自动识别归一为秒。
"""
from __future__ import annotations

import csv
import io
import os
import time
import urllib.error
import urllib.request
import zipfile
from typing import Dict, Iterator, List, Optional, Tuple

BASE = "https://data.binance.vision/data/spot/monthly/klines"

# (ts_s, open, high, low, close, volume, quote_volume)
Bar = Tuple[int, float, float, float, float, float, float]


def zip_url(symbol: str, year: int, month: int, timeframe: str = "1h") -> str:
    return f"{BASE}/{symbol}/{timeframe}/{symbol}-{timeframe}-{year}-{month:02d}.zip"


def _http_status(url: str, timeout_s: float = 10) -> int:
    req = urllib.request.Request(url, method="HEAD")
    try:
        return urllib.request.urlopen(req, timeout=timeout_s).status
    except urllib.error.HTTPError as e:
        return e.code


def has_month(symbol: str, year: int, month: int,
              timeout_s: float = 10) -> Optional[bool]:
    """True=有快照 / False=404（上市前或未发布）/ None=其他错误（网络等）。"""
    code = _http_status(zip_url(symbol, year, month), timeout_s)
    if code == 200:
        return True
    if code == 404:
        return False
    return None


def ym_range(from_ym: Tuple[int, int], to_ym: Tuple[int, int]) -> Iterator[Tuple[int, int]]:
    y, m = from_ym
    while (y, m) <= to_ym:
        yield y, m
        m += 1
        if m > 12:
            y, m = y + 1, 1


def find_first_month(symbol: str, from_ym: Tuple[int, int],
                     to_ym: Tuple[int, int],
                     timeout_s: float = 10) -> Optional[Tuple[int, int]]:
    """从 from_ym 向后探测首个有快照的月份（404 → 下月）。"""
    for ym in ym_range(from_ym, to_ym):
        hit = has_month(symbol, ym[0], ym[1], timeout_s)
        if hit is True:
            return ym
        if hit is None:  # 网络错误：稍等重试一次，仍失败则视为该月缺失继续
            time.sleep(1)
            hit = has_month(symbol, ym[0], ym[1], timeout_s)
            if hit is True:
                return ym
    return None


def last_published_month(now: Optional[time.struct_time] = None) -> Tuple[int, int]:
    """最后一个已发布月（Binance 月末才发布当月 → 保守取上一月；
    若上上月也 404 的极端情况由调用方 REST 尾巴兜底）。"""
    t = now or time.localtime()
    y, m = t.tm_year, t.tm_mon
    m -= 1
    if m < 1:
        y, m = y - 1, 12
    return y, m


def fetch_month_csv(symbol: str, year: int, month: int, cache_dir: str,
                    timeout_s: float = 15) -> Optional[str]:
    """下载并解压 {symbol}-{year}-{month} 月度 CSV，返回 CSV 路径；404 → None。

    沙盒缓存（参考 itransformer 核心修复 1：每币每月独立目录）：
    已存在 CSV → 直接复用，永不重复下载。
    退避重试（参考 itransformer 核心修复 2）：403/429 休眠后重试，共 3 次。
    """
    sandbox = os.path.join(cache_dir, "klines", symbol, f"{year}-{month:02d}")
    os.makedirs(sandbox, exist_ok=True)
    existing = [p for p in os.listdir(sandbox) if p.endswith(".csv")]
    if existing:
        return os.path.join(sandbox, sorted(existing)[0])

    url = zip_url(symbol, year, month)
    for attempt in range(3):
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=timeout_s) as r:
                blob = r.read()
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                name = z.namelist()[0]
                with z.open(name) as src, open(os.path.join(sandbox, name), "wb") as dst:
                    dst.write(src.read())
            return os.path.join(sandbox, name)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code in (403, 429):
                time.sleep(2 * (attempt + 1))
                continue
            return None
        except Exception:  # noqa: BLE001 - 网络抖动重试
            time.sleep(2)
    return None


def _norm_ts(ot: int) -> int:
    """open_time 归一为 epoch 秒。单位随文件年代变化（2025-01 起微秒）：
    ~1e9 秒 / ~1e12 毫秒 / ~1e15 微秒——按数量级识别（防御性含秒直传）。"""
    if ot > 10 ** 14:
        return ot // 1_000_000
    if ot > 10 ** 11:
        return ot // 1_000
    return ot


def month_bars(symbol: str, year: int, month: int, cache_dir: str,
               timeout_s: float = 15) -> List[Bar]:
    """取 {symbol} {year}-{month} 全部 1h K 线（下载/缓存 + 解析）。"""
    csv_path = fetch_month_csv(symbol, year, month, cache_dir, timeout_s)
    if not csv_path:
        return []
    bars: List[Bar] = []
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        first = f.readline()
        has_header = first.lower().startswith("open_time")
        f.seek(0)
        reader = csv.reader(f)
        if has_header:
            next(reader, None)
        for row in reader:
            if len(row) < 6:
                continue
            try:
                bars.append((
                    _norm_ts(int(row[0])),
                    float(row[1]), float(row[2]), float(row[3]),
                    float(row[4]), float(row[5]),
                    float(row[7]) if len(row) > 7 else 0.0,
                ))
            except ValueError:
                continue
    return bars


def full_history(symbol: str, from_ym: Tuple[int, int],
                 to_ym: Tuple[int, int], cache_dir: str,
                 workers: int = 4, timeout_s: float = 15
                 ) -> Tuple[Optional[Tuple[int, int]], List[Bar], List[str]]:
    """全量历史：探测首月 → 并发拉取每月 → 按时间排序合并。

    返回 (first_ym, bars, missing_ym)：missing_ym 为探测到但拉取失败的月份
    （调用方可据此决定是否 REST 补漏；正常情况应为空）。
    """
    first = find_first_month(symbol, from_ym, to_ym, timeout_s)
    if first is None:
        return None, [], [f"{y}-{m:02d}" for y, m in ym_range(from_ym, to_ym)]

    months = list(ym_range(first, to_ym))
    from concurrent.futures import ThreadPoolExecutor, as_completed

    results: Dict[Tuple[int, int], List[Bar]] = {}
    missing: List[str] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(month_bars, symbol, y, m, cache_dir, timeout_s): (y, m)
                for y, m in months}
        for fut in as_completed(futs):
            ym = futs[fut]
            try:
                results[ym] = fut.result()
            except Exception:  # noqa: BLE001 - 单月失败记缺失不阻断
                results[ym] = []
    for ym in months:
        if not results.get(ym):
            missing.append(f"{ym[0]}-{ym[1]:02d}")
    bars = [b for ym in months for b in sorted(results.get(ym, []))]
    bars.sort(key=lambda b: b[0])
    return first, bars, missing


if __name__ == "__main__":
    # 手动自检：python -m crypto.sources.binance_vision
    import sys
    sym = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT"
    lp = last_published_month()
    print(f"最后已发布月: {lp[0]}-{lp[1]:02d}")
    t0 = time.time()
    first, bars, missing = full_history(sym, (2017, 8), lp,
                                        "/tmp/vision_selftest", workers=4)
    print(f"{sym}: 首月={first}, {len(bars)} 行, 缺失={missing}, "
          f"{time.time() - t0:.1f}s")
    if bars:
        print("  首行:", bars[0], " 尾行:", bars[-1])
