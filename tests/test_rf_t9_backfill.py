# -*- coding: utf-8 -*-
"""T9 macro_rf 历史回填（v6.3.5）回归。

覆盖（零网络——akshare / TE session 全 monkeypatch；_no_network autouse 双保险）：
1. fetch_rf_history_em：正常行（升序 + 4 位小数 + source=em_local_static）；
   列漂移 → RuntimeError；sanity [0.5,10] 粗滤（>10 与 <0.5 剔除，5.41 保留——
   2007-08 真实峰值不得被 rf_cfg 的 4.0 误杀）；akshare 缺失/抓取失败 → RuntimeError。
2. merge_rf_history_into_csv：只补缺口不覆盖已有行（TE 现值行优先）；幂等（二次 0 新增）；
   无 csv 时全量新增。
3. csv 3 列向后兼容：旧 2 列行 → 写盘补 te_local；fetch_rf_10y 落盘不抹第 3 列
   （已有 em_local_static 行经 TE 重写后 source 标记保留）。
4. load_rf_10y_asof：3 列 csv 下 PIT 语义不变（<=run_day 最近一行，小数）。

纪律锚点：akshare 是 validate extra（辅助/校验源），仅本低频回填路径使用，
不进每日主链路（现值仍走 fetch_rf_10y TE 主源）。
"""
from __future__ import annotations

import sys
import types

import pytest

sys.path.insert(0, ".")

from screener.data import rf as rfmod  # noqa: E402


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    import socket
    monkeypatch.setattr(socket.socket, "connect",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("network disabled in test")))


# ---------------------------------------------------------------------------
# fake akshare
# ---------------------------------------------------------------------------
class _FakeDF:
    def __init__(self, rows, cols):
        self._rows = rows
        self.columns = cols

    def iterrows(self):
        for i, r in enumerate(self._rows):
            yield i, dict(zip(self.columns, r))


def _fake_ak(rows, cols=("日期", "中国国债收益率10年")):
    mod = types.ModuleType("akshare")
    mod.__version__ = "1.18.88"
    mod.bond_zh_us_rate = lambda start_date=None: _FakeDF(rows, cols)
    return mod


# ---------------------------------------------------------------------------
# fetch_rf_history_em
# ---------------------------------------------------------------------------
def test_fetch_history_normal_rows(tmp_path, monkeypatch):
    rows = [
        ("2002-01-04 00:00:00", 3.2096),
        ("2007-08-01 00:00:00", 5.4127),   # 真实历史峰值：sanity 10.0 内，必须保留
        ("2026-09-30 00:00:00", 1.6822),
        ("2026-09-25 00:00:00", None),     # 缺失（东财该日无数据）
    ]
    monkeypatch.setitem(sys.modules, "akshare", _fake_ak(rows))
    out = rfmod.fetch_rf_history_em(start_date="20020101")
    assert out == [
        ["2002-01-04", "3.2096", "em_local_static"],
        ["2007-08-01", "5.4127", "em_local_static"],
        ["2026-09-30", "1.6822", "em_local_static"],
    ], f"升序 + 4 位小数 + source 标记 + 缺失跳过: {out}"


def test_fetch_history_sanity_filters(tmp_path, monkeypatch):
    rows = [
        ("2002-01-04 00:00:00", 3.0),
        ("2002-01-05 00:00:00", 12.0),    # >10.0 → 剔（字段错位/单位错误）
        ("2002-01-06 00:00:00", 0.3),     # <0.5 → 剔
        ("2002-01-07 00:00:00", 5.9),     # 历史高位真实值 → 留
    ]
    monkeypatch.setitem(sys.modules, "akshare", _fake_ak(rows))
    out = rfmod.fetch_rf_history_em()
    assert [r[0] for r in out] == ["2002-01-04", "2002-01-07"]


def test_fetch_history_column_drift_raises(tmp_path, monkeypatch):
    # 列名漂移（接口改版）→ 显式失败，不静默吞成空序列
    monkeypatch.setitem(sys.modules, "akshare",
                        _fake_ak([("2002-01-04 00:00:00", 3.0)], cols=("date", "cn_10y")))
    with pytest.raises(RuntimeError, match="列漂移"):
        rfmod.fetch_rf_history_em()


def test_fetch_history_fetch_error_raises(tmp_path, monkeypatch):
    mod = types.ModuleType("akshare")
    mod.__version__ = "1.18.88"
    mod.bond_zh_us_rate = lambda start_date=None: (_ for _ in ()).throw(
        ConnectionError("boom"))
    monkeypatch.setitem(sys.modules, "akshare", mod)
    with pytest.raises(RuntimeError, match="抓取失败"):
        rfmod.fetch_rf_history_em()


# ---------------------------------------------------------------------------
# merge_rf_history_into_csv
# ---------------------------------------------------------------------------
def _write_rf_csv(path, rows):
    rfmod._atomic_write_rf_csv(path, rows)


def test_merge_only_fills_gaps_and_keeps_existing(tmp_path):
    p = str(tmp_path / "rf_10y_daily.csv")
    # 现有 TE 现值行（09-30 已有 → 回填同 date 不得覆盖）
    _write_rf_csv(p, [["2026-09-30", "1.7000", "te_local"]])
    added = rfmod.merge_rf_history_into_csv(p, [
        ["2026-09-29", "1.6670", "em_local_static"],
        ["2026-09-30", "1.6822", "em_local_static"],   # 已有 date → 跳过
        ["2026-09-28", "1.6790", "em_local_static"],
    ])
    assert added == 2, "只补缺口：09-29/09-28 新增，09-30 保留已有 TE 行"
    rows = rfmod._read_rf_csv(p)
    by_date = {r[0]: r for r in rows}
    assert by_date["2026-09-30"][1] == "1.7000" and by_date["2026-09-30"][2] == "te_local"
    assert by_date["2026-09-29"] == ["2026-09-29", "1.6670", "em_local_static"]
    assert [r[0] for r in rows] == sorted(r[0] for r in rows), "升序保持"


def test_merge_idempotent(tmp_path):
    p = str(tmp_path / "rf_10y_daily.csv")
    hist = [["2002-01-04", "3.2096", "em_local_static"], ["2002-01-05", "3.2100", "em_local_static"]]
    assert rfmod.merge_rf_history_into_csv(p, hist) == 2
    assert rfmod.merge_rf_history_into_csv(p, hist) == 0, "二次合并 0 新增（幂等）"


def test_merge_no_existing_file(tmp_path):
    p = str(tmp_path / "rf_10y_daily.csv")
    hist = [["2002-01-04", "3.2096", "em_local_static"]]
    assert rfmod.merge_rf_history_into_csv(p, hist) == 1
    assert rfmod._read_rf_csv(p) == hist


# ---------------------------------------------------------------------------
# csv 3 列向后兼容
# ---------------------------------------------------------------------------
def test_atomic_write_backfills_missing_source(tmp_path):
    p = str(tmp_path / "rf_10y_daily.csv")
    # 旧 2 列行（v6.3.5 前格式）→ 写盘补 te_local
    rfmod._atomic_write_rf_csv(p, [["2026-09-10", "1.6800"],
                                   ["2026-09-30", "1.7000", "te_local"]])
    rows = rfmod._read_rf_csv(p)
    assert rows == [["2026-09-10", "1.6800", "te_local"],
                    ["2026-09-30", "1.7000", "te_local"]]


class _OKSession:
    def __init__(self, html):
        self._html = html

    def get(self, url, timeout=None, headers=None):
        r = types.SimpleNamespace()
        r.status_code = 200
        r.content = self._html.encode("utf-8")
        return r


_TE_HTML = """
<html><head><script type="application/ld+json">
{"@type":"Dataset","dateModified":"20261001T12:00:00.00Z",
 "description":"The yield on China 10-year eased to 1.69% on October 1, 2026."}
</script></head><body>x</body></html>
"""


def test_fetch_rf_10y_preserves_history_source_col(tmp_path, monkeypatch):
    """TE 日更重写 csv 时不得抹掉历史回填行的 em_local_static 标记。"""
    p = str(tmp_path / "rf_10y_daily.csv")
    _write_rf_csv(p, [
        ["2002-01-04", "3.2096", "em_local_static"],
        ["2026-09-30", "1.7000", "te_local"],
    ])
    rfc = {"url": "https://example.invalid/te", "fallback_pct": 2.0,
           "sanity_pct": [0.5, 4.0]}
    dec, meta = rfmod.fetch_rf_10y(rfc, str(tmp_path), "2026-10-01",
                                   session=_OKSession(_TE_HTML))
    assert meta["source"] == "tradingeconomics" and meta["date"] == "2026-10-01"
    assert abs(dec - 0.0169) < 1e-12
    by_date = {r[0]: r for r in rfmod._read_rf_csv(p)}
    assert by_date["2002-01-04"][2] == "em_local_static", "历史行 source 标记不得被 TE 重写抹掉"
    assert by_date["2026-10-01"][2] == "te_local"
    assert by_date["2026-09-30"][1] == "1.7000" and by_date["2026-09-30"][2] == "te_local", \
        "同日已有 TE 行被新值覆盖（date PK 语义）但 source 保持 te_local"


def test_load_rf_10y_asof_with_3col_csv(tmp_path):
    """3 列 csv 下 PIT 语义不变：<=run_day 最近一行（小数）。"""
    p = str(tmp_path / "rf_10y_daily.csv")
    _write_rf_csv(p, [
        ["2026-09-29", "1.6670", "em_local_static"],
        ["2026-09-30", "1.7000", "te_local"],
        ["2026-10-01", "1.6900", "te_local"],   # >run_day 不可见
    ])
    assert abs(rfmod.load_rf_10y_asof(str(tmp_path), "2026-09-30") - 0.017) < 1e-12
    assert abs(rfmod.load_rf_10y_asof(str(tmp_path), "2026-09-29") - 0.01667) < 1e-12
    assert rfmod.load_rf_10y_asof(str(tmp_path), "2000-01-01") is None
