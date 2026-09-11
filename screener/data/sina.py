# -*- coding: utf-8 -*-
"""v5 Round-2 新浪数据源客户端（TL D3'/D6'/D9'；东财停用后的替代取数通道）。

背景（brief_round2）：本机在欧洲，东财 datacenter-web 禁止海外访问 → em.py 接口代码
保留但**禁用**（config datasource.em.enabled=false）。替代源（TL 已亲自实测可达）：

1. **新浪 F10 流通股股东页**（D3' 央国企识别输入）::

    https://vip.stock.finance.sina.com.cn/corp/go.php/vCI_CirculateStockHolder/stockid/{code6}.phtml

   - **GBK 编码 HTML**，必须 decode('gbk')；
   - 页面内嵌多期股东表（``<table id="CirculateShareholderTable">``），每期以
     ``<a name="YYYY-MM-DD"></a>`` 锚点开头（截止日期=报告期），随后"公告日期"行 +
     编号/股东名称/持股数量(股)/占流通股比例(%)/股本性质 数据行；
   - **比东财更好**：直接有 ``股本性质`` 字段（国有股/境外法人股…）→ soe 双规则
     （股本性质==国有股 OR 名称关键词命中，TL D3'）。
   - ⚠️"占流通股比例"是**流通股口径**非总股本 → 只做定性识别、不参与打分。

2. **新浪财务 JSON API**（D6' OCF 取数）::

    https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022
        ?paperCode={code6}&source=gjzb&type=0&page={p}&num={N}

   - 必须带 ``Referer: https://finance.sina.com.cn``（TL 实测）；
   - 返回 result.data.report_list{YYYYMMDD: {publish_date, data:[{item_field,
     item_title, item_value,...}]}}；**经营现金流量净额 = item_field MANANETR**
     （绝对额，元；601398 2025年报实测 1890530000000.0）。累计口径 → **年报行
     (-12-31) 的 MANANETR 即全年 OCF**。⚠️无 capex 字段 → fcf_coverage 口径改
     OCF-based（TL D6'，metrics.fcf_coverage docstring 有说明）。
   - report_list 每页只含 num 个**最新**报告期（report_count 是总数）→ 翻页直到
     找到 publish_date <= run_day 的最近年报。

3. **本地静态分红全表**（D1'，零网络）：``cache/em_dividend_all.csv``（东财封禁前
   落盘，56974 行、1991→2026；dps_pretax 已 /10 每股口径）。Phase 1 视为**静态快照
   （数据截至 2026-09-10）**，不做每日增量（~2 年未分红新股少算 1 年，Phase 2 用
   BaoStock 对账修正）。本模块提供只读读取函数——这是"东财零请求"纪律的**唯一例外**
   （本地文件，非网络）。

限速纪律（D9'）：新浪为非官方接口 → **串行、间隔 >=1s、单请求 timeout 15s、重试 <=2
次**；任何一类接口（F10 / 财务JSON）**连续失败 >=N 只 → 该类因子整体降级 None**
（missing_policy=neutral_renorm 兜底）+ 报告告警，**不得死磕**。

契约监控（brief §剩余工作1）：字段非空率、数值 sanity（OCF>0 比例、持股比例合计
<100%）——统计并 warning 不静默。
"""
from __future__ import annotations

import csv
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests

from .baostock_client import DataSourceError

log = logging.getLogger("screener.data.sina")

# 本地静态分红全表的首行哨兵（与 em.py 落盘时写入的 EM_CACHE_SENTINEL 同值）。
# ⚠️D-EM 纪律：本模块**不 import em.py**（enabled=false 时引擎不得触碰东财客户端）——
# 只按相同文件格式读本地文件（纯 IO，零网络），故在此独立声明哨兵字面量。
_LOCAL_DIV_SENTINEL = "stock-screener-em-cache-v1"

SINA_HOLDERS_URL = (
    "https://vip.stock.finance.sina.com.cn/corp/go.php/"
    "vCI_CirculateStockHolder/stockid/{code6}.phtml"
)
SINA_CF_URL = (
    "https://quotes.sina.cn/cn/api/openapi.php/"
    "CompanyFinanceService.getFinanceReport2022"
    "?paperCode={code6}&source=gjzb&type=0&page={page}&num={num}"
)
SINA_CF_REFERER = "https://finance.sina.com.cn"
# 浏览器指纹 UA（F10/CF 通用）：新浪 WAF(HTTP 456)对无头/裸请求更敏感；带真实浏览器
# UA+Referer 的串行请求实测稳定（TL curl 可达 + 本环境 10 连抓 ok=10）。D9' 限速纪律不变。
SINA_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# 财务 JSON 字段（TL 实测 evidence_r2/sina_cf_api.json）：
# 经营现金流量净额（绝对额，元；累计口径 → 年报行=全年值）。⚠️无 capex 字段（D6'）。
SINA_CF_OCF_FIELD = "MANANETR"

# 本地静态分红全表文件名（东财封禁前落盘；D1'：Phase 1 静态快照，零网络读取）
LOCAL_DIVIDEND_FILE = "em_dividend_all.csv"
# D1'：只统计已实施的分红事件（progress=="实施分配"）——未实施预案/取消/否决行剔除。
PROGRESS_IMPLEMENTED = "实施分配"

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


class SinaDataError(DataSourceError):
    """新浪接口级失败（重试耗尽 / 契约校验不过）。

    继承 DataSourceError：引擎失败守卫按数据源级失败路由（与 BaoStock/EM 同语义）。
    逐只取数场景由调用方 catch 后降级（单只失败→该股因子 None；连续 N 只→整类降级）。
    """


# ---------------------------------------------------------------------------
# 纯解析函数（离线可测；fixture = evidence_r2 真实留样）
# ---------------------------------------------------------------------------

def _clean_date(v: Any) -> str:
    """'YYYY-MM-DD ...'/8位/YYYYMMDD → ISO 日期串（非法/空 → ''）。"""
    if v is None:
        return ""
    s = str(v).strip()
    m = _DATE_RE.search(s)
    if m:
        return m.group(0)
    if re.fullmatch(r"\d{8}", s):
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return ""


def _fnum(v: Any) -> Optional[float]:
    """数值字段 → float（null/空/非法 → None）。"""
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_holders_page(html: str) -> List[Dict[str, Any]]:
    """新浪 F10 流通股股东页（已 GBK→str）→ 全部报告期 + 前十大股东行。

    解析策略（对页面结构漂移稳健）：
    - 按 ``<a name="YYYY-MM-DD"></a>`` **章节锚点**切段（截止日期=报告期；导航区
      用的是 ``<a href="#...">``，name 锚点只在表格内 → 不会误切）；
    - 每段取"公告日期"行 + 数据行（编号/股东名称/持股数量/占流通股比例/股本性质）。

    :return: [{end_date, notice_date, holders:[{holder_name, hold_shares,
             circ_ratio_pct, share_nature}...]}]，按 end_date 升序。
        无有效段 → []（调用方按失败处理）。
    """
    anchors = list(re.finditer(r'<a\s+name="(\d{4}-\d{2}-\d{2})"\s*></a>', html))
    out: List[Dict[str, Any]] = []
    for i, m in enumerate(anchors):
        end_date = m.group(1)
        start = m.end()
        end = anchors[i + 1].start() if i + 1 < len(anchors) else len(html)
        section = html[start:end]

        notice_m = re.search(
            r"公告日期</strong></div>\s*</td>\s*<td[^>]*>([^<]*)</td>", section)
        notice_date = _clean_date(notice_m.group(1)) if notice_m else ""

        holders: List[Dict[str, Any]] = []
        row_re = re.compile(
            r"<tr[^>]*>\s*"
            r"<td><div align=\"center\">(\d+)</div></td>"          # 编号
            r"\s*<td><div align=\"center\">(.*?)</div></td>"       # 股东名称
            r"\s*<td><div align=\"center\">([^<]*)</div></td>"     # 持股数量(股)
            r"\s*<td><div align=\"center\">([^<]*)</div></td>"     # 占流通股比例(%)
            r"\s*<td><div align=\"center\">([^<]*)</div></td>",    # 股本性质
            re.S,
        )
        for rm in row_re.finditer(section):
            holders.append({
                "holder_rank": int(rm.group(1)),
                "holder_name": rm.group(2).strip(),
                "hold_shares": _fnum(rm.group(3)),
                # 流通股口径比例（%）——只做定性识别、不参与打分（TL D3'）
                "circ_ratio_pct": _fnum(rm.group(4)),
                "share_nature": rm.group(5).strip(),
            })
        if holders:
            out.append({
                "end_date": end_date,
                "notice_date": notice_date,
                "holders": holders,
            })
    out.sort(key=lambda x: x["end_date"])
    return out


def pick_holders_asof(
    periods: Sequence[Dict[str, Any]], run_day: str
) -> Optional[Dict[str, Any]]:
    """PIT 选期（TL D3'）：截止日期 <= run_day 的最新报告期。

    公告日期缺失/晚于 run_day 的期不可见（未披露）；全部不可见 → None。
    """
    best: Optional[Dict[str, Any]] = None
    for p in periods:
        if not p.get("holders"):
            continue
        if str(p.get("end_date") or "") > run_day:
            continue
        nd = str(p.get("notice_date") or "")
        if nd and nd > run_day:
            continue  # PIT：运行日尚未公告
        if best is None or p["end_date"] > best["end_date"]:
            best = p
    return best


def parse_cf_report(
    payload: Dict[str, Any], ocf_field: str = SINA_CF_OCF_FIELD
) -> List[Dict[str, Any]]:
    """新浪财务 JSON（getFinanceReport2022 响应体）→ 报告期 OCF 列表。

    :param payload: ``resp.json()`` 整体（result.data.report_list{YYYYMMDD:{...}}）。
    :return: [{report_date('YYYY-MM-DD'), publish_date, ocf(元)}]，按 report_date 降序；
        只含能解析出 OCF 数值的期。结构异常 → []（调用方按失败处理）。
    """
    try:
        data = payload["result"]["data"]
        report_list = data.get("report_list") or {}
    except (KeyError, TypeError):
        return []
    out: List[Dict[str, Any]] = []
    for k, rep in report_list.items():
        rd = _clean_date(k)
        if not rd:
            continue
        ocf = None
        roe_weighted_pct = None  # v5.2：ROEWEIGHTED（百分数,加权）——ROE 交叉校验源（报告 §4）
        for it in (rep.get("data") or []):
            fld = it.get("item_field")
            if fld == ocf_field and ocf is None:
                ocf = _fnum(it.get("item_value"))  # 首个匹配生效（与原 break 语义一致）
            elif fld == "ROEWEIGHTED" and roe_weighted_pct is None:
                roe_weighted_pct = _fnum(it.get("item_value"))
        out.append({
            "report_date": rd,
            "publish_date": _clean_date(rep.get("publish_date")),
            "ocf": ocf,
            "roe_weighted_pct": roe_weighted_pct,  # v5.2 附加字段（旧调用方不读=零回归）
        })
    out.sort(key=lambda x: x["report_date"], reverse=True)
    return out


def latest_annual_ocf(
    reports: Sequence[Dict[str, Any]], run_day: str
) -> Optional[Dict[str, Any]]:
    """最近一个**已披露年报**（-12-31 且 publish_date <= run_day）的 OCF（PIT）。

    :return: {report_date, publish_date, ocf} 或 None（无可见年报 → 调用方降级代理）。
    """
    for r in reports:  # 已按 report_date 降序
        if not str(r.get("report_date") or "").endswith("-12-31"):
            continue
        pd_ = str(r.get("publish_date") or "")
        if pd_ and pd_ > run_day:
            continue  # PIT：运行日尚未披露
        if r.get("ocf") is not None:
            return r
    return None


# ---------------------------------------------------------------------------
# 客户端（串行限速 + 重试 + 连续失败熔断；D9'）
# ---------------------------------------------------------------------------

class SinaClient:
    """新浪非官方接口受控客户端。

    纪律（TL D9'）：串行、间隔 >=1s（全局限速锚点，跨调用生效）、单请求 timeout 15s、
    重试 <=2 次；某类接口（category=holders/cf）**连续失败 >=breaker 只** → 该类
    后续取数立即抛 SinaDataError（引擎侧整体降级 None + 告警，不得死磕）。

    :param sina_cfg: config.sina_cfg(cfg)（interval_s/timeout_s/max_attempts/
        consecutive_fail_breaker/cf_reports_num）。
    """

    def __init__(self, sina_cfg: Dict[str, Any], session: Optional[requests.Session] = None) -> None:
        self.interval_s = float(sina_cfg["interval_s"])       # >=1.0（config 层强制）
        self.timeout_s = float(sina_cfg["timeout_s"])
        self.max_attempts = int(sina_cfg["max_attempts"])     # <=2（config 层强制）
        self.breaker = int(sina_cfg.get("consecutive_fail_breaker", 5))
        self.cf_reports_num = int(sina_cfg.get("cf_reports_num", 20))
        # ---- WAF 滑动窗口限速（Round-2 实测：F10 持续请求 ~10-15 次后触发 HTTP 456，
        #      与东财"服务器繁忙"同类）——周期性长冷却让窗口在饱和前复位。----
        self.cooldown_every_n = int(sina_cfg.get("cooldown_every_n", 15))   # 每 N 次请求
        self.cooldown_s = float(sina_cfg.get("cooldown_s", 20.0))           # 插入长冷却秒数
        self.waf_backoff_s = float(sina_cfg.get("waf_backoff_s", 30.0))     # HTTP 456 专用退避（WAF 需要时间复位）
        self.session = session or requests.Session()
        self.request_count = 0          # 实际发出的 HTTP 请求数（含重试）
        self._last_request_ts = 0.0     # 全局限速锚点（跨 fetch 调用，防密集突发）
        self._req_since_cooldown = 0    # 距上次长冷却的请求计数（滑动窗口复位）
        self._consec_fail: Dict[str, int] = {}   # category → 连续失败计数
        self._breaker_tripped: set = set()       # 已熔断的 category

    def _throttle(self) -> None:
        if self.interval_s > 0:
            elapsed = time.time() - self._last_request_ts
            if elapsed < self.interval_s:
                time.sleep(self.interval_s - elapsed)
        # WAF 滑动窗口：每 cooldown_every_n 次请求插入长冷却（防持续请求触发 456）。
        self._req_since_cooldown += 1
        if self.cooldown_every_n > 0 and self._req_since_cooldown >= self.cooldown_every_n:
            self._req_since_cooldown = 0
            log.info("[SINA] WAF 冷却：每 %d 次请求长休眠 %.0fs（防 HTTP 456 滑动窗口）",
                     self.cooldown_every_n, self.cooldown_s)
            time.sleep(self.cooldown_s)

    def _check_breaker(self, category: str) -> None:
        if category in self._breaker_tripped:
            raise SinaDataError(
                f"新浪 {category} 接口已连续失败 >= {self.breaker} 只并熔断——"
                "该类因子整体降级 None（D9'：不得死磕）")

    def _note_success(self, category: str) -> None:
        self._consec_fail[category] = 0

    def _note_failure(self, category: str) -> None:
        n = self._consec_fail.get(category, 0) + 1
        self._consec_fail[category] = n
        if n >= self.breaker and category not in self._breaker_tripped:
            self._breaker_tripped.add(category)
            log.warning(
                "新浪 %s 接口连续失败 %d 只 → 熔断：该类因子整体降级 None + 报告告警（D9'）",
                category, n)

    def _get(self, url: str, category: str, headers: Optional[Dict[str, Any]] = None) -> requests.Response:
        """单请求（限速 + WAF 冷却 + <=max_attempts 次重试）。失败抛 SinaDataError。"""
        self._check_breaker(category)
        last_err = "unknown"
        for attempt in range(1, self.max_attempts + 2):  # 首次 + max_attempts 次重试
            self._throttle()
            t0 = time.time()
            try:
                resp = self.session.get(url, timeout=self.timeout_s, headers=headers or {})
                self.request_count += 1
                self._last_request_ts = time.time()
                if resp.status_code == 200:
                    log.info("[SINA] %s %.2fs (累计请求 %d)", category,
                             time.time() - t0, self.request_count)
                    return resp
                last_err = f"HTTP {resp.status_code}"
            except requests.RequestException as exc:
                last_err = f"{type(exc).__name__}: {exc}"
            if attempt <= self.max_attempts:
                # HTTP 456 = 新浪 WAF 拦截（滑动窗口饱和）：短退避无效，需长冷却让窗口复位。
                is_waf = "HTTP 456" in last_err
                delay = self.waf_backoff_s if is_waf else (1.0 * attempt)
                log.warning("[SINA] %s 第%d次失败(%s)%s，%.1fs 后重试",
                            category, attempt, last_err,
                            "（WAF 456→长退避）" if is_waf else "", delay)
                time.sleep(delay)
        self._note_failure(category)
        raise SinaDataError(f"新浪 {category} 重试 {self.max_attempts} 次均失败: {last_err}")

    # ---------- F10 股东页（D3'） ----------
    def fetch_holders(self, code6: str) -> List[Dict[str, Any]]:
        """抓单只 F10 流通股股东页 → parse_holders_page 输出（全部报告期）。"""
        self._check_breaker("holders")
        url = SINA_HOLDERS_URL.format(code6=code6)
        resp = self._get(url, "holders")
        try:
            html = resp.content.decode("gbk", errors="replace")
        except (LookupError, UnicodeError) as exc:  # pragma: no cover - gbk 恒可用
            raise SinaDataError(f"新浪 F10 {code6} GBK 解码失败: {exc}") from exc
        periods = parse_holders_page(html)
        # v5.2 Phase 1：新浪 F10 股东页原始响应落 raw（零转换；canonical.enabled=false → no-op）
        from . import rawstore as _raw
        _raw.record_response("sina", f"f10_holders_{code6}", html, meta={"periods": len(periods)})
        if not periods:
            self._note_failure("holders")
            raise SinaDataError(f"新浪 F10 {code6} 未解析到任何股东表（页面结构漂移?）")
        # 契约监控：字段非空率 + 持股比例 sanity（warning 不静默）
        n = len(periods[0]["holders"]) or 1
        name_nn = sum(1 for h in periods[0]["holders"] if h["holder_name"])
        ratio_sum = sum(h["circ_ratio_pct"] or 0.0 for h in periods[0]["holders"])
        if name_nn / n < 0.5:
            log.warning("[SINA] 契约告警: %s F10 股东名称非空率 %.0f%%（字段漂移?）",
                        code6, name_nn / n * 100)
        if ratio_sum >= 100.0:
            # 前十大合计应 <100%（流通股口径；==100 说明解析错位或数据异常）
            log.warning("[SINA] 契约告警: %s F10 前十大持股比例合计 %.2f%% >=100%%（解析漂移?）",
                        code6, ratio_sum)
        self._note_success("holders")
        return periods

    # ---------- 财务 JSON OCF（D6'） ----------
    def fetch_annual_ocf(self, code6: str, run_day: str) -> Optional[Dict[str, Any]]:
        """抓单只财务 JSON（翻页至最近已披露年报）→ latest_annual_ocf。

        每页 num=cf_reports_num 个最新报告期；年报间隔 4 期 → 通常 1-2 页命中。
        :return: {report_date, publish_date, ocf} 或 None（无可见年报/接口失败）。
            ⚠️None 的两种语义由调用方区分：熔断抛异常 vs 真无年报返回 None。
        """
        self._check_breaker("cf")
        headers = {"Referer": SINA_CF_REFERER, "User-Agent": "Mozilla/5.0"}
        reports: List[Dict[str, Any]] = []
        for page in range(1, 4):  # 防御上限 3 页（~12 期 ≈ 3 年，足够）
            url = SINA_CF_URL.format(code6=code6, page=page, num=self.cf_reports_num)
            resp = self._get(url, "cf", headers=headers)
            try:
                payload = resp.json()
            except ValueError as exc:
                raise SinaDataError(f"新浪财务JSON {code6} 非 JSON 响应: {exc}") from exc
            # v5.2 Phase 1：CF JSON 原始响应落 raw（零转换；canonical.enabled=false → no-op）
            from . import rawstore as _raw
            _raw.record_response("sina", f"cf_{code6}_p{page}", payload)
            page_reports = parse_cf_report(payload)
            if not page_reports:
                break
            reports.extend(page_reports)
            ann = latest_annual_ocf(reports, run_day)
            if ann is not None:
                # 契约监控：OCF 数值 sanity（warning 不静默）
                if ann["ocf"] <= 0:
                    log.warning("[SINA] 契约告警: %s 年报 %s OCF=%.0f 非正（银行/亏损? 人工核对）",
                                code6, ann["report_date"], ann["ocf"])
                self._note_success("cf")
                return ann
            if len(page_reports) < self.cf_reports_num:
                break  # 已到最后一页
        self._note_failure("cf")
        raise SinaDataError(f"新浪财务JSON {code6} 未取到可见年报 OCF（{len(reports)} 期）")


# ---------------------------------------------------------------------------
# 本地静态分红全表（D1'：零网络；"东财零请求"纪律的唯一例外=本地文件读取）
# ---------------------------------------------------------------------------

def _read_local_div_csv(path: str) -> Optional[List[List[str]]]:
    """读本地静态分红 CSV（首行哨兵校验 + 表头跳过）。纯 IO，零网络。

    文件格式与 em.py 落盘一致（哨兵/表头/数据）；无哨兵=损坏 → None。
    """
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            reader = csv.reader(f)
            sentinel = next(reader, None)
            if not sentinel or sentinel[0] != _LOCAL_DIV_SENTINEL:
                return None
            next(reader, None)  # 表头行
            return [row for row in reader]
    except (OSError, csv.Error) as exc:
        log.warning("本地分红全表读取失败 %s: %s", path, exc)
        return None


def load_local_dividends(cache_dir: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """读 ``cache/em_dividend_all.csv``（东财封禁前落盘的静态快照，数据截至 2026-09-10）。

    D1'：Phase 1 **不做每日增量**——该表视为静态历史源；~2 年未分红的新股会少算
    1 年连续分红（Phase 2 用 BaoStock 对账修正），报告需注明数据截至日。

    :return: (rows[{code(6位裸码), report_date, plan_notice_date, ex_date,
                dps_pretax(元/股, 入库已/10), progress}], meta)；
        文件缺失/损坏 → 抛 SinaDataError（数据源级失败，引擎守卫路由）。
    """
    path = os.path.join(cache_dir, LOCAL_DIVIDEND_FILE)
    raw_rows = _read_local_div_csv(path)
    if raw_rows is None:
        raise SinaDataError(
            f"本地分红全表缺失或损坏: {path}（D1' 静态源；需人工恢复——"
            "该文件是东财封禁前落盘的完整快照，56974 行）")
    rows: List[Dict[str, Any]] = []
    for r in raw_rows:
        vals = list(r) + [""] * (6 - len(r))
        code, report_date, plan_notice, ex_date, dps_s, progress = vals
        rows.append({
            "code": str(code),
            "report_date": str(report_date),
            "plan_notice_date": str(plan_notice),
            "ex_date": str(ex_date),
            "dps_pretax": _fnum(dps_s),
            "progress": str(progress),
        })
    n_impl = sum(1 for r in rows if r["progress"] == PROGRESS_IMPLEMENTED)
    meta = {
        "source": "local_static",
        "rows": len(rows),
        "implemented_rows": n_impl,
        "as_of": "2026-09-10",  # 落盘日（TL 完整性验证 evidence_r2/div_completeness.py）
    }
    log.info("[SINA] 本地分红全表 %s（%d 行，实施分配 %d 行；静态快照截至 %s，零网络）",
             path, len(rows), n_impl, meta["as_of"])
    return rows, meta
