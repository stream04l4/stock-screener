# -*- coding: utf-8 -*-
"""test_t6_breaker — v6.3.4 回归：T6 股东页"合法空页" vs "结构漂移" 的熔断纪律。

生产事故（10-02）：新浪 F10 股东页对部分股票（实测 601123 马矿等）返回**合法空页**
（HTTP 200 + CirculateShareholderTable 表框架完整 + 0 个报告期锚点 = 新浪侧确无此股
股东数据）。旧 fetch_holders 把"解析出 0 报告期"一律 _note_failure + 抛错 → 连续 5 只
合法空页误触发 holders 熔断 → 其后所有股票（含 600002/000001 等页面有 80~899 行数据、
解析器完全能解析的）在 _check_breaker 入口被直接拒绝，根本没抓取（2913 只"失败"
绝大多数是这么被误伤的）。

修复（v6.3.4）：
- ``_holders_page_empty_shell(html)`` 判别：表框架完整 + 0 锚点 = 合法空页（接口正常，
  新浪无数据）；容器缺失 = 结构漂移（真异常）。
- ``fetch_holders``：合法空页 → 返回 ``[]`` + ``_note_success``（**不记失败、不触发
  熔断**）；结构漂移 → 仍 ``_note_failure`` + 抛 SinaDataError（熔断语义保留）。
- ``run_t6`` worker：``[]`` → 记 no_data + 正常返回 → runner 标 done（不空耗配额）。

本文件全离线（mock _get / mock client，零网络；遵守 _no_network 纪律）。
"""
import pytest

from screener.data import sina as sinamod
from screener.data.sina import SinaClient, SinaDataError, _holders_page_empty_shell

# ===========================================================================
# A. _holders_page_empty_shell 三分支（纯函数，零网络）
# ===========================================================================
# 合法空页：表框架在、0 报告期锚点（601123 马矿生产留样特征）
EMPTY_SHELL_HTML = (
    '<html><head><title>马矿股份(601123)流通股股东_新浪财经</title></head><body>'
    '<table id="CirculateShareholderTable"><tr><th>工商银行(601123)&nbsp;&nbsp;'
    '流通股股东</th></tr></table>'
    '<div id="CirculateShareholderTable">点击近期部分数据内容</div></body></html>'
)
# 结构漂移：连表容器都没有（整页改版/接口异常返回非预期页）
DRIFT_NO_FRAME_HTML = '<html><body><p>访问过于频繁，请稍后重试</p></body></html>'
# 有数据：表框架在 + 报告期锚点（601398 生产留样特征）
HAS_DATA_HTML = (
    '<html><body><table id="CirculateShareholderTable">'
    '<tr><td><a name="2026-06-30"></a><strong>截止日期</strong></td>'
    '<td>2026-06-30</td></tr>'
    '<tr><td><div align="center">1</div></td>'
    '<td><div align="center">汇金公司</div></td>'
    '<td><div align="center">1000000</div></td>'
    '<td><div align="center">1.0</div></td>'
    '<td><div align="center">国有股</div></td></tr>'
    '</table></body></html>'
)
# 锚点在但 0 行可解析（锚点还在但行格式变了）——结构漂移的一种
ANCHOR_NO_ROWS_HTML = (
    '<html><body><table id="CirculateShareholderTable">'
    '<tr><td><a name="2026-06-30"></a></td></tr></table></body></html>'
)


def test_empty_shell_frame_present_zero_anchor():
    """合法空页 = 表框架完整 + 0 报告期锚点（601123 特征）→ True。"""
    assert _holders_page_empty_shell(EMPTY_SHELL_HTML) is True


def test_drift_no_frame():
    """结构漂移 = 连容器标记都没有 → False（按失败处理）。"""
    assert _holders_page_empty_shell(DRIFT_NO_FRAME_HTML) is False


def test_has_data_frame_plus_anchor():
    """有数据（框架 + 锚点）→ False（走正常解析路径）。"""
    assert _holders_page_empty_shell(HAS_DATA_HTML) is False


def test_anchor_but_no_rows_is_drift():
    """锚点在但 0 行可解析（行格式漂移）→ False（不判合法空页，仍按失败/漂移）。"""
    assert _holders_page_empty_shell(ANCHOR_NO_ROWS_HTML) is False


# ===========================================================================
# B. fetch_holders 熔断纪律（mock _get，零网络）
# ===========================================================================
def _mk_client(monkeypatch, pages_by_code):
    """构造 SinaClient + mock _get（按 code6 返回指定 HTML 页）。

    pages_by_code: {code6: html}。_get 返回带 .content(bytes) 的伪 Response。
    限速/WAF 冷却禁用（interval_s=0 / cooldown_every_n=0）避免测试 sleep。
    """
    import types

    cfg = {"interval_s": 0.0, "timeout_s": 5.0, "max_attempts": 1,
           "consecutive_fail_breaker": 3, "cooldown_every_n": 0,
           "cooldown_s": 0.0, "waf_backoff_s": 0.0}
    c = SinaClient(cfg)

    def fake_get(url, category, headers=None):
        code6 = url.rstrip().split("/")[-1].replace(".phtml", "")
        html = pages_by_code[code6]
        return types.SimpleNamespace(content=html.encode("gbk", errors="replace"))

    monkeypatch.setattr(c, "_get", fake_get)
    # rawstore.record_response 置 no-op（零落盘）
    from screener.data import rawstore
    monkeypatch.setattr(rawstore, "record_response", lambda *a, **k: None)
    return c


def test_fetch_holders_empty_shell_returns_list_no_breaker(monkeypatch):
    """合法空页：连续 5 只（>breaker=3）都返回 [] 且**不触发熔断**。

    回归核心——旧行为连续 3 只空页即熔断，第 4 只抛 SinaDataError。现在：
    空页 _note_success 重置计数 → 后续股票照常抓。"""
    pages = {f"{i:06d}": EMPTY_SHELL_HTML for i in range(1, 9)}   # 8 只全空页
    c = _mk_client(monkeypatch, pages)
    for i in range(1, 9):
        assert c.fetch_holders(f"{i:06d}") == [], f"第{i}只空页应返回 []"
    # 8 只空页后 holders 仍未熔断（关键断言：熔断未触发）
    assert "holders" not in c._breaker_tripped, "合法空页不得触发熔断"
    assert c._consec_fail.get("holders", 0) == 0, "空页应重置连续失败计数"


def test_fetch_holders_drift_trips_breaker(monkeypatch):
    """结构漂移（无容器）：连续 >=breaker 只 → 熔断（语义保留 D9'）。

    熔断触发后，后续股票（即便页面有数据）在 _check_breaker 入口抛 SinaDataError。
    """
    # 3 只漂移（无容器）→ 第 3 只触发熔断；第 4 只即便有数据也被挡
    pages = {
        "111111": DRIFT_NO_FRAME_HTML,
        "222222": DRIFT_NO_FRAME_HTML,
        "333333": DRIFT_NO_FRAME_HTML,
        "444444": HAS_DATA_HTML,   # 有数据，但熔断后到不了
    }
    c = _mk_client(monkeypatch, pages)
    for i in ("111111", "222222", "333333"):
        with pytest.raises(SinaDataError):
            c.fetch_holders(i)
    assert "holders" in c._breaker_tripped, "连续漂移应触发熔断"
    with pytest.raises(SinaDataError):
        c.fetch_holders("444444")   # 熔断后：有数据股也被挡（D9' 整类降级）


def test_fetch_holders_empty_shell_then_real_data(monkeypatch):
    """混合场景：空页穿插有数据股，空页不打断连续成功、有数据正常解析。"""
    pages = {
        "601123": EMPTY_SHELL_HTML,   # 空页（新浪无数据）
        "601398": HAS_DATA_HTML,       # 有数据
        "600002": HAS_DATA_HTML,       # 有数据
    }
    c = _mk_client(monkeypatch, pages)
    assert c.fetch_holders("601123") == []
    p = c.fetch_holders("601398")
    assert len(p) >= 1 and p[0]["holders"], "有数据股应正常解析"
    assert c.fetch_holders("600002")[0]["holders"]
    assert "holders" not in c._breaker_tripped, "混合场景不得熔断"


# ===========================================================================
# C. run_t6 worker：合法空页 → 标 done（不空耗配额）
# ===========================================================================
def test_run_t6_empty_page_marks_done(tmp_path, monkeypatch):
    """run_t6：合法空页（client 返回 []）→ worker 正常返回 → runner 标 done。

    回归核心——旧行为空页抛 RuntimeError 不标 done，数千只无数据股每次 full 重抓
    （空耗新浪配额 + 曾误触发熔断）。现在空页标 done 跳过。"""
    import sys
    _p = "/home/ubuntu/stock-screener/scripts"
    if _p not in sys.path:
        sys.path.insert(0, _p)
    import lake_backfill as drv
    from lake.backfill import BackfillRunner, load_progress
    from lake.ingest import sina_ingest

    codes = ["sh.601123", "sh.601398", "sh.600002"]

    # mock client：601123 空页([])，其余有数据
    class _FakeClient:
        def fetch_holders(self, code6):
            if code6 == "601123":
                return []   # 合法空页
            return [{"end_date": "2026-06-30", "notice_date": "2026-08-29",
                     "holders": [{"holder_rank": 1, "holder_name": "X",
                                  "hold_shares": 100, "circ_ratio_pct": 1.0,
                                  "share_nature": "国有股"}]}]

    monkeypatch.setattr(drv, "_t6_client_factory", lambda: _FakeClient())
    # mock load_t6：返回 1（有数据股成功入库）
    monkeypatch.setattr(sina_ingest, "load_t6", lambda con, code, periods, **k: 1)
    # con：run_t6 内 load_t6 已被 mock，con 仅透传（用真库连接供 runner coverage 刷新）
    import duckdb
    db = str(tmp_path / "t6test.duckdb")
    dcon = duckdb.connect(db)
    dcon.execute(
        "CREATE TABLE holders_snapshot (ts_code TEXT, as_of_date TEXT, "
        "holder_rank INT, holder_name TEXT, hold_shares BIGINT, "
        "circ_ratio_pct DOUBLE, share_nature TEXT)")
    dcon.close()

    con = duckdb.connect(db, read_only=False)
    runner = BackfillRunner(db_path=db)
    try:
        stats = drv.run_t6(con, db, codes, runner)
    finally:
        con.close()
    # 3 只全 processed（含 1 只空页）
    assert stats["processed"] == 3, f"3 只应全处理: {stats}"
    assert stats.get("no_data_count") == 1 and "sh.601123" in stats["no_data_codes"]
    # 空页股已标 done（不空耗）——done 键 (holders_snapshot, sh.601123, t6)
    prog = load_progress(runner.progress_path)
    done = [tuple(k) for k in prog["done"]]
    assert ("holders_snapshot", "sh.601123", "t6") in done, \
        f"空页股应标 done: {done}"
    # 有数据股同样 done
    assert ("holders_snapshot", "sh.601398", "t6") in done
    assert ("holders_snapshot", "sh.600002", "t6") in done
    # 无错误
    assert stats["errors"] == [], f"合法空页不应记 error: {stats['errors']}"
