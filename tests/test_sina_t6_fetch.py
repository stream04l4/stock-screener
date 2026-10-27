"""T6 新浪 F10 股东页抓取路径回归（v7.0.18.4，tester DEF-4 根因）。

背景：v7.0.0（060235c）删除 screener.data.rawstore 时漏删 sina.py 里两处
`from . import rawstore` 惰性导入 → 每只 fetch_holders 在解析成功后、写库前
抛 ImportError，整个 T6 抓取路径在 v7.0.0 之后从未成功（10-22 起冻结，
2026-10-27 全市场重抓实锤 5556 只全灭）。此前 t6 相关测试（test_t6_breaker.py）
随 v7.0 删除，无人覆盖该路径。

本测试用真实 F10 页快照（tests/fixtures/sina_f10_holders_min.html，GBK 字节、
3 报告期、工行 601398）monkeypatch session.get，驱动**完整** fetch_holders：
限速/重试 → GBK 解码 → parse_holders_page → 契约监控 → 返回。任何人在
fetch_holders 里重新引入坏导入/坏调用，此测试即失败。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from screener.data import sina as sina_mod  # noqa: E402
from screener.data.sina import SinaClient  # noqa: E402

FIXTURE = (Path(__file__).resolve().parent / "fixtures"
           / "sina_f10_holders_min.html")


def _client_with(monkeypatch, content: bytes) -> SinaClient:
    """构造零网络 SinaClient：session.get 固定返回给定 GBK 字节。"""
    cfg = {"interval_s": 0.0, "timeout_s": 1.0, "max_attempts": 0,
           "cooldown_every_n": 0}   # 0 → 关闭 WAF 长冷却（测试提速）
    client = SinaClient(cfg)
    fake_resp = SimpleNamespace(status_code=200, content=content)
    monkeypatch.setattr(client.session, "get",
                        lambda *a, **k: fake_resp)
    return client


def test_fetch_holders_full_path_parses_periods(monkeypatch):
    """完整 fetch_holders 路径：3 报告期、字段齐全、最新期 2026-06-30。

    断言覆盖 v7.0.0 断点位置（解析后、返回前）——若该行重新引入
    ImportError，本测试在此处失败。
    """
    raw = FIXTURE.read_bytes()
    client = _client_with(monkeypatch, raw)
    periods = client.fetch_holders("601398")
    assert len(periods) == 3, f"fixture 含 3 报告期, 实得 {len(periods)}"
    # 升序返回（parse 契约：end_date 升序）→ 最新期在末位
    assert periods[0]["end_date"] == "2025-12-31"
    latest = periods[-1]
    assert latest["end_date"] == "2026-06-30"
    assert latest["notice_date"] == "2026-08-29"
    h = latest["holders"]
    assert h[0]["holder_rank"] == 1
    assert h[0]["holder_name"] == "汇金公司"
    assert h[0]["circ_ratio_pct"] == 34.793
    assert h[0]["share_nature"] == "国有股"
    assert h[1]["holder_name"] == "财政部"
    # request_count 走通 _get（证明不是短路返回）
    assert client.request_count == 1


def test_fetch_holders_success_resets_breaker(monkeypatch):
    """成功后 _note_success 复位（不触发熔断）：连续取多只仍有数据的股不抛。"""
    raw = FIXTURE.read_bytes()
    client = _client_with(monkeypatch, raw)
    for _ in range(3):
        assert client.fetch_holders("601398")
    # 未熔断（可继续取）
    assert "holders" not in client._breaker_tripped


def test_fetch_holders_legit_empty_shell_returns_empty(monkeypatch):
    """合法空页（表框架完整 + 0 报告期）→ 返回 [] 且**不记失败**（v6.3.4）。"""
    # 空壳：有 CirculateShareholderTable 框架但无任何日期锚点（GBK 字节形态）
    empty = ('<table id="CirculateShareholderTable"><thead>'
             '<tr><th>工商银行(601398) 流通股股东</th></tr></thead>'
             '<tr><td>截止日期</td></tr></table>').encode('gbk')
    client = _client_with(monkeypatch, empty)
    assert client.fetch_holders("601398") == []
    assert "holders" not in client._consec_fail or \
        client._consec_fail.get("holders", 0) == 0


def test_fetch_holders_structure_drift_still_breaks(monkeypatch):
    """结构漂移（容器缺失）→ 仍抛 SinaDataError（熔断语义保留，v6.3.4）。"""
    drift = b'<html><body>no such table here</body></html>'
    client = _client_with(monkeypatch, drift)
    with pytest.raises(sina_mod.SinaDataError):
        client.fetch_holders("601398")
