# -*- coding: utf-8 -*-
"""v6.3-O1：G1 gate 判据口径统一（与 t2_damage_scan 同一实现）+ --tolerated-codes。

核心回归保护：
- "仅恒定比例基准差"两源（af1_b = 2.0 × af1_a，裸水平差恒 50%）→ G1 必须 PASS
  （旧裸水平相对差口径下会 FAIL——本改动要消灭的假阳性形态）；
- "单日除权事件不一致"（某日 af 跳变 30% → 该日两源收益差 ~23% ≥ RET_TOL 2%）→ 必须 FAIL；
- --tolerated-codes：容忍 code FAIL → 门 PASS + 单列证据行；PASS → "已修复转 PASS" 行。

全部离线：fake DuckDB（tmp_path，只建 kline_daily/dividend_events 两表）+
monkeypatch _cache_rebuilt 注入 fake ground truth，零网络、零生产库。
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
sys.path.insert(0, SCRIPTS)  # 两脚本同在 scripts/，直接模块导入（与 gate 脚本自身做法一致）

import lake_source_gates as gates  # noqa: E402
from t2_damage_scan import RET_TOL, worst_daily_ret_diff  # noqa: E402


# ---------------------------------------------------------------------------
# fixtures：fake 两源序列 + fake DuckDB
# ---------------------------------------------------------------------------

def _biz_dates(n=500):
    """n 个简单工作日字符串（YYYY-MM-DD，升序；仅用于构造日期键）。"""
    import datetime as _dt
    d = _dt.date(2024, 1, 1)
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += _dt.timedelta(days=1)
    return out


DATES = _biz_dates(500)   # > 420：窗口取尾部 420 日，早期段非空（辅助行也有数据）


def _base_af1():
    """源 A：af1 = 10 × (1.001)^i 平滑增长序列。"""
    return {d: 10.0 * (1.001 ** i) for i, d in enumerate(DATES)}


A_F1 = _base_af1()
B_RATIO = {d: 2.0 * v for d, v in A_F1.items()}          # 恒定比例基准差（裸水平差恒 50%）

# 单日除权事件不一致：源 B 在 DATES[300] 起 af 跳变 +30%（此后全部 ×1.3），
# 该日 B 收益 +31.3% vs A 收益 +0.1% → Δret ≈ 23% ≥ RET_TOL。
JUMP_I = 300
B_JUMP = {d: (v * 1.3 if i >= JUMP_I else v) for i, (d, v) in enumerate(A_F1.items())}


def _fake_cache_map(code):
    """_cache_rebuilt 替身：sh.A→恒定比例源；sh.B→跳变源；其余→与存储一致。"""
    if code == "sh.A":
        return {"dates": list(B_RATIO), "af1_close": list(B_RATIO.values())}
    if code == "sh.B":
        return {"dates": list(B_JUMP), "af1_close": list(B_JUMP.values())}
    m = A_F1
    return {"dates": list(m), "af1_close": list(m.values())}


@pytest.fixture()
def fake_db(tmp_path, monkeypatch):
    """fake 生产库（tmp）：sh.A/sh.B 各 500 日 kline_daily（close=af1, adj_factor=1.0）。

    - sh.A = 恒定比例基准差源（cache B = 2×A，裸水平差恒 50%、收益差 0）；
    - sh.B = 单日除权事件不一致源（DATES[300] 起 cache ×1.3 → 该日 Δret≈30%）；
    - sh.601688 = 健康股（cache 与存储一致）——g1() 标的集硬编码含它，fake 库必须提供；
    - G1_STOCKS monkeypatch 为 [sh.A, sh.B]（dividend_events 空 → T4 Top2 为空）。
    """
    import duckdb

    db = str(tmp_path / "fake_lake.duckdb")
    con = duckdb.connect(db)
    con.execute("""
        CREATE TABLE kline_daily (ts_code VARCHAR, date DATE, close DOUBLE,
                                  adj_factor DOUBLE, volume BIGINT)
    """)
    con.execute("CREATE TABLE dividend_events (ts_code VARCHAR, ex_date DATE, "
                "ann_date DATE, cash_dps DOUBLE)")
    # g2 需要 index_daily（无 volume=0 行时查 T7 缺口，JOIN 硬编码 sh.601398）：
    # 插入与 kline_daily 重合的近期日期 + sh.601398 数据 → 无缺口 → G2 PASS
    con.execute("CREATE TABLE index_daily (index_code VARCHAR, date DATE)")
    for code in ("sh.A", "sh.B", "sh.601688", "sh.601398"):
        con.executemany(
            "INSERT INTO kline_daily VALUES (?, ?, ?, 1.0, 100)",
            [(code, d, A_F1[d]) for d in DATES],
        )
    con.executemany(
        "INSERT INTO index_daily VALUES ('sh000001', ?)", [(d,) for d in DATES[-5:]]
    )
    con.close()

    monkeypatch.setattr(gates, "_cache_rebuilt", _fake_cache_map)
    monkeypatch.setattr(gates, "G1_STOCKS", ["sh.A", "sh.B"])
    return db


# ---------------------------------------------------------------------------
# g1() 语义测试（直接调函数，验证判据口径）
# ---------------------------------------------------------------------------

def test_g1_constant_ratio_basis_diff_passes(fake_db, monkeypatch):
    """核心回归保护：仅恒定比例基准差（裸水平差恒 50%）→ 必须 PASS。

    旧 G1 裸水平相对差口径下 max|a-b|/max(|a|,|b|) = 50% ≥ 2% → FAIL（假阳性）；
    收益差口径下两源逐日收益完全一致 → max|Δret|=0 < RET_TOL → PASS。
    """
    import duckdb

    monkeypatch.setattr(gates, "G1_STOCKS", ["sh.A"])   # 只验证恒定比例源
    con = duckdb.connect(fake_db, read_only=True)
    out = []
    ok = gates.g1(con, out, set())
    con.close()
    assert ok is True, "\n".join(out)
    text = "\n".join(out)
    # 诊断行必须存在（裸水平差降级为 level_pct，不参与判定）
    assert "诊断 level_pct" in text
    assert "50.0000%" in text  # 恒定比例源的裸水平差恰为 50%——仅展示、不判 FAIL


def test_g1_single_day_event_mismatch_fails(fake_db, monkeypatch):
    """单日除权事件不一致（某日 af 跳变 30%）→ 必须 FAIL。"""
    import duckdb

    monkeypatch.setattr(gates, "G1_STOCKS", ["sh.B"])   # 只验证跳变源
    con = duckdb.connect(fake_db, read_only=True)
    out = []
    ok = gates.g1(con, out, set())
    con.close()
    assert ok is False, "\n".join(out)
    text = "\n".join(out)
    assert "sh.B" in text and "FAIL" in text
    # 最大分歧日应落在跳变日：JUMP_I=300，全历史 500 日 → 距尾部 200 日 < 420，
    # 落在近 420 窗口内 → worst_date = DATES[JUMP_I]
    assert f"max|Δret|" in text and DATES[JUMP_I] in text


def test_g1_tolerated_fail_does_not_block(fake_db):
    """容忍 code FAIL → 门 PASS + 单列证据行（不计入门判定）。"""
    import duckdb

    con = duckdb.connect(fake_db, read_only=True)
    out = []
    ok = gates.g1(con, out, {"sh.B"})   # sh.B 跳变源 FAIL，但被容忍
    con.close()
    assert ok is True, "\n".join(out)
    text = "\n".join(out)
    assert "容忍股 FAIL 确认" in text
    assert "容忍股 FAIL 汇总（不阻塞门）" in text


def test_g1_tolerated_fixed_prints_repaired(fake_db):
    """容忍 code PASS → 打印"已修复转 PASS"（informational，不阻塞）。"""
    import duckdb

    con = duckdb.connect(fake_db, read_only=True)
    out = []
    # sh.A 被容忍且 PASS（验证"已修复转 PASS"行）；sh.B 同列容忍避免其 FAIL 干扰断言
    ok = gates.g1(con, out, {"sh.A", "sh.B"})
    con.close()
    assert ok is True, "\n".join(out)
    text = "\n".join(out)
    assert "已修复转 PASS" in text
    assert "已修复转 PASS 汇总（informational）: ['sh.A']" in text


# ---------------------------------------------------------------------------
# CLI 级测试：--tolerated-codes 文件解析 + 门判定 + 退出码
# ---------------------------------------------------------------------------

def _run_cli(tmp_path, db, tolerated_file):
    out_file = str(tmp_path / "gates_evidence.txt")
    argv = ["--db", db, "--out", out_file]
    if tolerated_file is not None:
        argv += ["--tolerated-codes", str(tolerated_file)]
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = gates.main(argv)
    return rc, buf.getvalue(), open(out_file, encoding="utf-8").read()


def test_cli_tolerated_fail_gate_passes(fake_db, tmp_path):
    """CLI：容忍清单含 sh.B（FAIL）→ 退出码非 3（门不被 G1 阻塞）+ 证据行。"""
    tol = tmp_path / "tolerated.txt"
    tol.write_text("sh.B\n", encoding="utf-8")
    rc, _stdout, report = _run_cli(tmp_path, fake_db, tol)
    assert rc != 3, f"门不应被容忍股 FAIL 阻塞: rc={rc}\n{report}"
    assert "容忍股 FAIL 确认" in report


def test_cli_no_tolerated_fail_blocks(fake_db, tmp_path):
    """CLI：无 --tolerated-codes 时 sh.B FAIL → GATES_BLOCKED（退出码 3）。"""
    rc, _stdout, report = _run_cli(tmp_path, fake_db, None)
    assert rc == 3, f"非容忍股 FAIL 应阻塞门: rc={rc}\n{report}"


def test_cli_tolerated_fixed_informational(fake_db, tmp_path):
    """CLI：容忍清单含 sh.A（PASS）→ "已修复转 PASS" 行，门不因此变化。"""
    tol = tmp_path / "tolerated.txt"
    # sh.B（跳变源 FAIL）同列容忍，避免其阻塞门干扰本断言；sh.A 验证"已修复转 PASS"
    tol.write_text("sh.A,sh.B", encoding="utf-8")   # 逗号分隔、无换行
    rc, _stdout, report = _run_cli(tmp_path, fake_db, tol)
    assert rc != 3, f"容忍股不应阻塞门: rc={rc}\n{report}"
    assert "已修复转 PASS" in report


def test_tolerated_file_missing_exits(fake_db, tmp_path):
    """--tolerated-codes 指向不存在文件 → SystemExit（不静默当空清单）。"""
    with pytest.raises(SystemExit):
        gates.load_tolerated_codes(str(tmp_path / "nope.txt"))


def test_load_tolerated_parsing():
    """逗号/换行混合分隔解析。"""
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
        fh.write("sh.601688, sz.000001\nsh.600519  \n\n")
        p = fh.name
    try:
        assert gates.load_tolerated_codes(p) == {"sh.601688", "sz.000001", "sh.600519"}
    finally:
        os.unlink(p)


# ---------------------------------------------------------------------------
# 同一实现验证（验收标准 #1：import 关系，非复制）
# ---------------------------------------------------------------------------

def test_g1_uses_same_impl_as_scan():
    """G1 主校验与 scan 共用 t2_damage_scan.worst_daily_ret_diff / RET_TOL。"""
    assert gates.worst_daily_ret_diff is worst_daily_ret_diff
    assert gates.TOL is RET_TOL
    import t2_damage_scan as scan_mod

    assert scan_mod._worst_return_diff is worst_daily_ret_diff  # 内部别名同一对象


def test_worst_daily_ret_diff_semantics():
    """函数级语义：恒定比例 → Δret=0；单日跳变 → 捕获跳变日。"""
    w, wd, n = worst_daily_ret_diff(A_F1, B_RATIO, DATES[-420:])
    assert w == 0.0 and n > 400
    w2, wd2, _ = worst_daily_ret_diff(A_F1, B_JUMP, DATES[-420:])
    assert w2 >= RET_TOL and wd2 == DATES[JUMP_I]
