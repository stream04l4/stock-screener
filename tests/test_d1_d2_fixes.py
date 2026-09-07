# -*- coding: utf-8 -*-
"""v2 round 1 缺陷回归测试（D1 / D2，离线、不联网、不碰 BaoStock）。

D1 [major]：PUT /api/strategy no-op 保存抹掉全部注释（ruamel 默认 width=80 把
   flow-style 长行折行 → round-trip 自检恒失败 → 退回 safe_dump）。
   判据：no-op 写回后文件逐字节不变；真实改一个权重值后 diff 只含该键那一行。
D2 [minor]：SSE done 事件缺 result_date/api（R5 规格要求两字段）。
   判据：_classify_log_line 的 done 分支从任务上下文补全两字段。
"""
from __future__ import annotations

import copy
import hashlib
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import yaml  # noqa: E402

from web.app import (  # noqa: E402
    _classify_log_line,
    _result_day_for,
    _ruamel_yaml,
    _write_strategy_preserving_comments,
)

REAL_CONFIG = PROJECT_ROOT / "config" / "strategy.yaml"


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# D1：no-op round-trip 逐字节还原（注释保留，git diff 空语义）
# ---------------------------------------------------------------------------

def test_d1_ruamel_roundtrip_real_config_byte_identical():
    """当前仓库 config/strategy.yaml 经 _ruamel_yaml() load→dump 必须逐字节还原。

    这是 D1 的根因判据：ruamel 默认 width=80 会把 sub_weights 的 flow-style
    长行折成多行 → 自检 1 恒失败 → safe_dump 丢注释。width=4096 后必须还原。
    """
    orig = REAL_CONFIG.read_text(encoding="utf-8")
    yml = _ruamel_yaml()
    data = yml.load(orig)
    import io

    buf = io.StringIO()
    yml.dump(data, buf)
    assert buf.getvalue() == orig, "round-trip 未逐字节还原（注释/排版丢失）"


def test_d1_noop_write_byte_identical(tmp_path, monkeypatch):
    """no-op 保存（payload == 当前文件内容）→ 文件逐字节不变。

    用真实 strategy.yaml 的副本做 fixture，monkeypatch CONFIG_PATH 指向 tmp，
    避免污染仓库文件。
    """
    import web.app as appmod

    cfg = tmp_path / "config"
    cfg.mkdir()
    target = cfg / "strategy.yaml"
    target.write_text(REAL_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(appmod, "CONFIG_PATH", target)

    payload = yaml.safe_load(target.read_text(encoding="utf-8"))
    before = _sha256(target)
    _write_strategy_preserving_comments(copy.deepcopy(payload))
    after = _sha256(target)
    assert before == after, "no-op 保存后文件字节变化（注释/排版被重排）"


def test_d1_single_weight_change_diff_only_that_line(tmp_path, monkeypatch):
    """真实改一个权重值（dividend 0.30→0.45）→ diff 只含该键那一行。

    其余所有行（含注释、flow-style 长行）零改动——git diff 最小化判据。
    """
    import web.app as appmod

    cfg = tmp_path / "config"
    cfg.mkdir()
    target = cfg / "strategy.yaml"
    orig_text = REAL_CONFIG.read_text(encoding="utf-8")
    target.write_text(orig_text, encoding="utf-8")
    monkeypatch.setattr(appmod, "CONFIG_PATH", target)

    payload = yaml.safe_load(orig_text)
    payload["scoring"]["weights"]["dividend"] = 0.45
    _write_strategy_preserving_comments(payload)

    new_text = target.read_text(encoding="utf-8")
    # 1) 语义正确：改的值已生效，其余字段不变
    reloaded = yaml.safe_load(new_text)
    assert reloaded["scoring"]["weights"]["dividend"] == 0.45
    for section in payload:
        for k, v in payload[section].items():
            assert reloaded[section][k] == v, f"{section}.{k} 意外变化"

    # 2) diff 只含 dividend 权重那一行
    old_lines = orig_text.splitlines()
    new_lines = new_text.splitlines()
    assert len(old_lines) == len(new_lines), "行数变化（不应增删行）"
    changed = [
        (i, a, b)
        for i, (a, b) in enumerate(zip(old_lines, new_lines), 1)
        if a != b
    ]
    assert len(changed) == 1, f"应恰好 1 行变化，实际 {len(changed)}: {changed}"
    idx, old_l, new_l = changed[0]
    assert "dividend: 0.30" in old_l and "dividend: 0.45" in new_l
    # 注释保留：原行尾的中文注释必须跟着新值一起出现
    assert "# 股息" in new_l

    # 3) 注释总数不变
    def _comment_lines(text):
        return sum(1 for ln in text.splitlines() if "#" in ln)

    assert _comment_lines(orig_text) == _comment_lines(new_text), "注释行数变化"


def test_d1_change_then_revert_byte_identical(tmp_path, monkeypatch):
    """改两个权重（dividend 0.30→0.45 + technical 0.25→0.10）→ 再 PUT 恢复原值
    → 文件逐字节还原 HEAD（brief D1 自测判据：git diff HEAD 为空）。

    依赖 _new_scalar_like 沿用旧节点的 ScalarFloat 字面元数据（"0.30" 的尾零）。
    """
    import web.app as appmod

    cfg = tmp_path / "config"
    cfg.mkdir()
    target = cfg / "strategy.yaml"
    orig_text = REAL_CONFIG.read_text(encoding="utf-8")
    target.write_text(orig_text, encoding="utf-8")
    monkeypatch.setattr(appmod, "CONFIG_PATH", target)

    payload = yaml.safe_load(orig_text)
    payload["scoring"]["weights"]["dividend"] = 0.45
    payload["scoring"]["weights"]["technical"] = 0.10
    _write_strategy_preserving_comments(payload)
    changed_text = target.read_text(encoding="utf-8")
    # 中间态：恰好两行变化且值正确
    mid_changed = [
        (a, b) for a, b in zip(orig_text.splitlines(), changed_text.splitlines()) if a != b
    ]
    assert len(mid_changed) == 2
    assert any("dividend:" in b and "0.45" in b for _, b in mid_changed)
    assert any("technical:" in b and "0.1" in b for _, b in mid_changed)

    # 恢复原值（模拟第二次 PUT：GET 当前 json → 改回 0.30/0.25 → 写回）
    restore = yaml.safe_load(changed_text)
    restore["scoring"]["weights"]["dividend"] = 0.30
    restore["scoring"]["weights"]["technical"] = 0.25
    _write_strategy_preserving_comments(restore)
    final_text = target.read_text(encoding="utf-8")
    assert final_text == orig_text, "恢复原值后文件未逐字节还原（git diff HEAD 不为空）"


def test_d1_two_weight_changes_diff_exactly_two_lines(tmp_path, monkeypatch):
    """改两个权重（dividend 0.30→0.45 + technical 0.25→0.10）→ diff 恰好两行。"""
    import web.app as appmod

    cfg = tmp_path / "config"
    cfg.mkdir()
    target = cfg / "strategy.yaml"
    orig_text = REAL_CONFIG.read_text(encoding="utf-8")
    target.write_text(orig_text, encoding="utf-8")
    monkeypatch.setattr(appmod, "CONFIG_PATH", target)

    payload = yaml.safe_load(orig_text)
    payload["scoring"]["weights"]["dividend"] = 0.45
    payload["scoring"]["weights"]["technical"] = 0.10
    _write_strategy_preserving_comments(payload)

    new_text = target.read_text(encoding="utf-8")
    changed = [
        (a, b)
        for a, b in zip(orig_text.splitlines(), new_text.splitlines())
        if a != b
    ]
    assert len(changed) == 2, f"应恰好 2 行变化，实际 {len(changed)}: {changed}"
    # 按值断言（ruamel 会把 plain float 0.10 dump 成 "0.1"，字面量不同但值正确）
    assert any("dividend:" in b and "0.45" in b for _, b in changed)
    assert any("technical:" in b and "0.1" in b for _, b in changed)
    # 行尾注释保留
    assert all("#" in b for _, b in changed)


# ---------------------------------------------------------------------------
# D2：SSE done 事件补全 result_date / api（R5 规格）
# ---------------------------------------------------------------------------

def test_d2_done_event_log_run_day_wins_over_requested_date(tmp_path, monkeypatch):
    """done 行自带「运行日」= 实际回退后的交易日 → 优先于请求日期。

    场景：请求周六 2026-09-05，引擎回退到周五 2026-09-04 完成筛选。
    result_date 必须是实际运行日 2026-09-04（R5 规格语义）。
    tail_lines 模拟 SSE 生成器已读到的日志行（含当前 done 行）。
    """
    import web.app as appmod

    monkeypatch.setattr(appmod, "OUTPUT_DIR", tmp_path / "output")  # 隔离产物兜底
    ev = _classify_log_line(
        "筛选完成 · 运行日 2026-09-04",
        task_date="2026-09-05",
        tail_lines=["===== 启动 =====", "筛选完成 · 运行日 2026-09-04"],
    )
    assert ev == {
        "type": "done",
        "result_date": "2026-09-04",
        "api": "/api/runs/2026-09-04",
    }


def test_d2_done_event_falls_back_to_task_date(tmp_path, monkeypatch):
    """日志行无「运行日」字样 → 退回任务请求日期（产物存在时直接命中）。"""
    import web.app as appmod

    out = tmp_path / "output"
    out.mkdir()
    (out / "result_20260907.csv").write_text("code\n", encoding="utf-8")
    monkeypatch.setattr(appmod, "OUTPUT_DIR", out)
    ev = _classify_log_line("筛选完成", task_date="2026-09-07")
    assert ev == {
        "type": "done",
        "result_date": "2026-09-07",
        "api": "/api/runs/2026-09-07",
    }


def test_d2_done_event_falls_back_to_log_run_day(tmp_path):
    """无任务日期（服务重启后）→ 从日志尾部抓「运行日」补全字段。"""
    log = tmp_path / "web_run_web_x.log"
    log.write_text(
        "===== 启动 =====\n开始筛选\n筛选完成 · 运行日 2026-09-04\n", encoding="utf-8"
    )
    tail = _tail_lines_for_test(log)
    ev = _classify_log_line("筛选完成 · 运行日 2026-09-04", task_date="", tail_lines=tail)
    assert ev["type"] == "done"
    assert ev["result_date"] == "2026-09-04"
    assert ev["api"] == "/api/runs/2026-09-04"


def _tail_lines_for_test(path: Path):
    from web.app import _tail_lines

    return _tail_lines(path, 200)


def test_d2_done_event_keeps_bare_type_when_no_context(tmp_path, monkeypatch):
    """既无任务日期、日志里也抓不到运行日、output/ 无产物 → done 事件仅 type（不崩）。"""
    import web.app as appmod

    empty_out = tmp_path / "output"
    empty_out.mkdir()
    # 隔离 output 目录，避免读到仓库真实 result_*.csv 兜底命中
    monkeypatch.setattr(appmod, "OUTPUT_DIR", empty_out)
    ev = _classify_log_line("筛选完成", task_date="", tail_lines=["随便一行日志"])
    assert ev == {"type": "done"}


def test_d2_result_day_for_latest_csv_fallback_known_date(tmp_path, monkeypatch):
    """有请求日期但直接命中失败（非交易日回退）→ 取 output/ 最新 result_*.csv。

    status 端点语义：任务刚结束，最新文件 = 本任务产物（原有行为保持）。
    """
    import web.app as appmod

    out = tmp_path / "output"
    out.mkdir()
    (out / "result_20260904.csv").write_text("code\n", encoding="utf-8")
    monkeypatch.setattr(appmod, "OUTPUT_DIR", out)
    # 请求周六 2026-09-05（无直接命中）→ 回退到最新产物 20260904
    assert _result_day_for("2026-09-05", None) == "20260904"


def test_d2_result_day_for_no_date_no_fabrication(tmp_path, monkeypatch):
    """无请求日期且日志证据不足 → 返回 None，不拿 output/ 最新文件冒充。

    对 live 运行那是上一次运行的产物，对旧任务重放可能是后来新运行的产物——宁缺毋错。
    """
    import web.app as appmod

    out = tmp_path / "output"
    out.mkdir()
    (out / "result_20260904.csv").write_text("code\n", encoding="utf-8")
    monkeypatch.setattr(appmod, "OUTPUT_DIR", out)
    assert _result_day_for("", ["L4_TopN入选: 50"]) is None
    assert _result_day_for("", None) is None


def test_d2_result_day_for_log_artifact_path_line(tmp_path):
    """日志尾部无「运行日」行但有产物路径行（CSV: .../result_YYYYMMDD.csv）→ 取之。

    CLI 在最终「筛选完成 · 运行日」行之前先打印产物路径，绑定本任务自己的产物。
    """
    lines = [
        "===== 启动 =====",
        "输出: /home/x/output/result_20260904.csv (4923 行)",
        "筛选完成: mode=zscore 入选 50 只",
    ]
    assert _result_day_for("", lines) == "20260904"


def test_d2_result_day_for_accepts_tail_list():
    """_result_day_for 的 log_path 参数兼容 list（SSE 流式端点传已读行）。"""
    day = _result_day_for("", ["x", "筛选完成 · 运行日 2026-09-04"])
    assert day == "20260904"
    # 带横线日期也必须归一化为紧凑 YYYYMMDD（调用方按 [4:6] 切片拼 ISO）
    day2 = _result_day_for("", ["运行日 2026-09-04"])
    assert day2 == "20260904"


def test_d2_result_day_for_path_branch_unchanged(tmp_path):
    """Path 分支（status 端点现有行为）保持不变。"""
    log = tmp_path / "log.txt"
    log.write_text("筛选完成 · 运行日 2026-09-04\n", encoding="utf-8")
    assert _result_day_for("", log) == "20260904"
