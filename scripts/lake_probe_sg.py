#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lake_probe_sg —— 新加坡中转通道逐源探活（**运行在 SG 服务器上**）。

用法（由 lake/web_api.py 的 /sources/probe 端点驱动，也可手工跑）::

    ssh -o BatchMode=yes oci 'python3 -s -' < scripts/lake_probe_sg.py  sina,tencent,...
    # 或本地： python3 scripts/lake_probe_sg.py            （缺省=全部 7 源）

**设计纪律（2026-11-10 v1.2，Joel 拍板：数据源状态标明本机可达+备用通道可达）**
- **纯 stdlib**：SG（1C1G）无项目 venv，只依赖 python3 标准库。
- **无凭据**：fuyao/tushare 只做**通道可达性**检查（不附 key、不调配额 API）——
  key 留在 EU 端 .env，"通道可达"≠"已鉴权"，语义在 detail 里写明。
- **并行 + 硬超时**：每源一个线程（端点超时 8s），整体墙钟 25s；SG 弱机器上
  不串行拖长 ssh 会话。
- **只读**：不写 SG 磁盘、不动 SG 任何配置。
- 输出 TSV（stdout 逐行，stderr 留空）::

    name<TAB>0|1<TAB>latency_ms<TAB>detail

**端点选择**：与 EU 端探测同端点（通道语义一致），2026-11-10 实测基线：
tencent/sina/fuyao/datacenter-web 从 SG 全通；**push2his 当日出现 SG 应用层限流**
（TCP 通但 0 字节超时）——故逐源独立探测，不假设"东财整体可达"。
"""
import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

# name: (url 或 "tcp:host:port", extra_headers, check)
#   check 语义：
#     tcp      —— TCP connect 成功即可（TDX 行情端口；多 host 依次试）
#     quote    —— http 200 且 body>10B（行情快照真实返回，防空响应页）
#     http200  —— http 200（报表类端点）
#     http_any —— 任何 HTTP 响应=通道可达（401/403/404 都算网络路径通；
#                fuyao 无 key、tushare 无 token 场景）
#     em_rc0   —— 东财 JSON rc==0 且 klines 非空（真实数据，非错误页）
TDX_HOSTS = ["180.153.18.170", "111.229.247.189", "120.76.152.87"]
TDX_PORT = 7709

ENDPOINTS = {
    "sina": ("https://hq.sinajs.cn/list=sh600546",
             {"Referer": "https://finance.sina.com.cn"}, "quote"),
    "tencent": ("https://qt.gtimg.cn/q=sh600546", {}, "quote"),
    "tdx": ("tcp:tdx", {}, "tcp"),
    "adata_f10": ("https://datacenter.eastmoney.com/securities/api/data/get"
                  "?type=RPT_F10_FINANCE_MAINFINADATA&sty=ALL"
                  "&filter=(SECUCODE%3D%22600546.SH%22)&p=1&ps=1"
                  "&source=WEB&client=WEB", {}, "http200"),
    "tushare": ("https://api.tushare.pro/", {}, "http_any"),
    "eastmoney": ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
                  "?secid=1.600546&fields1=f1&fields2=f51,f53&klt=101"
                  "&fqt=1&lmt=1&end=20500101", {}, "em_rc0"),
    "fuyao": ("https://fuyao.aicubes.cn/api/a-share/special-data/"
              "dragon-tiger-list?date=2026-11-09", {}, "http_any"),
}

TIMEOUT_S = 8.0        # 单端点超时
OVERALL_S = 25.0       # 整体墙钟（含线程调度；ssh 侧外层还有自己的硬超时）
USER_AGENT = {"User-Agent": "Mozilla/5.0 (lake-probe)"}


def _probe_tcp():
    for host in TDX_HOSTS:
        try:
            s = socket.create_connection((host, TDX_PORT), timeout=TIMEOUT_S)
            s.close()
            return 1, f"TCP {host}:{TDX_PORT} OK"
        except OSError:
            continue
    return 0, f"TCP 全 host 失败（{len(TDX_HOSTS)} 试）"


def _probe_http(url, headers, check):
    req = urllib.request.Request(url, headers={**USER_AGENT, **headers})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            code, body = r.getcode(), r.read(8192)
    except urllib.error.HTTPError as e:   # 4xx/5xx 也是"通道可达"
        if check == "http_any":
            return 1, f"http={e.code}（通道可达，未鉴权）"
        return 0, f"http={e.code}"
    if check == "http_any":
        return 1, f"http={code}（通道可达，未鉴权）"
    if check == "http200":
        return (1, "http=200 OK") if code == 200 else (0, f"http={code}")
    if check == "quote":
        if code == 200 and len(body) > 10:
            return 1, "quote OK"
        return 0, f"http={code} body={len(body)}B"
    if check == "em_rc0":
        try:
            d = json.loads(body)
        except ValueError:
            return 0, "响应非 JSON（疑限流/错误页）"
        if d.get("rc") == 0 and (d.get("data") or {}).get("klines"):
            return 1, "push2his 日K OK"
        return 0, f"rc={d.get('rc')}（疑 geo 限流/端点漂移）"
    return 0, "unknown check"


def _probe_one(name):
    spec = ENDPOINTS[name]
    url, headers, check = spec
    t0 = time.monotonic()
    try:
        if check == "tcp":
            ok, detail = _probe_tcp()
        else:
            ok, detail = _probe_http(url, headers, check)
    except Exception as exc:  # noqa: BLE001 - 单源异常→FAIL（不拖死整体）
        ok, detail = 0, f"{type(exc).__name__}: {exc}"[:80]
    ms = int((time.monotonic() - t0) * 1000)
    return name, ok, ms, detail


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    names = [n for n in arg.split(",") if n in ENDPOINTS] or list(ENDPOINTS)

    results = {}
    order = {}

    def worker(n):
        order[n] = results[n] = _probe_one(n)

    threads = [threading.Thread(target=worker, args=(n,), daemon=True)
               for n in names]
    for t in threads:
        t.start()
    deadline = time.monotonic() + OVERALL_S
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))

    for n in names:
        r = results.get(n)
        if r is None:   # 整体墙钟到，线程未完成 → 记超时
            print(f"{n}\t0\t{int(OVERALL_S * 1000)}\t整体超时（>{OVERALL_S:.0f}s）")
        else:
            name, ok, ms, detail = r
            # detail 里可能含 \t（URL 异常文本）→ 压成空格，保 TSV 四列
            print(f"{name}\t{ok}\t{ms}\t{detail.replace(chr(9), ' ')}")


if __name__ == "__main__":
    main()
