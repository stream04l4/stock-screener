# -*- coding: utf-8 -*-
"""v7 回归测试（test_d1_d2_fixes 按新 schema 重写，调研 ①「web 3 改」）。

D1（保留自 v6 的判据，对象换成 v7 slim schema）：PUT /api/strategy 写回必须
   保留注释/排版——no-op 保存逐字节不变；单值变更 diff 只含该行；改后恢复
   逐字节还原。
D2（v7 语义）：SSE 日志分类器 _classify_log_line —— [PROGRESS] 结构化进度 /
   done 标记（"回测完成"）/ error（Traceback、"错误:"）/ 普通 log。
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
    _ruamel_yaml,
    _write_strategy_preserving_comments,
)

REAL_CONFIG = PROJECT_ROOT / "config" / "strategy.yaml"
DONE_MARKERS = ["回测完成"]  # 与 web/app.py trigger_bt_run 的默认标记一致


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _isolate(tmp_path, monkeypatch) -> Path:
    """strategy.yaml 副本隔离到 tmp（不污染仓库文件）。"""
    import web.app as appmod

    cfg = tmp_path / "config"
    cfg.mkdir()
    target = cfg / "strategy.yaml"
    target.write_text(REAL_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(appmod, "CONFIG_PATH", target)
    return target


# ---------------------------------------------------------------------------
# D1：no-op round-trip 逐字节还原（注释保留）
# ---------------------------------------------------------------------------

def test_d1_ruamel_roundtrip_real_config_byte_identical():
    """仓库 config/strategy.yaml 经 _ruamel_yaml() load→dump 必须逐字节还原。"""
    orig = REAL_CONFIG.read_text(encoding="utf-8")
    yml = _ruamel_yaml()
    data = yml.load(orig)
    import io

    buf = io.StringIO()
    yml.dump(data, buf)
    assert buf.getvalue() == orig, "round-trip 未逐字节还原（注释/排版丢失）"


def test_d1_noop_write_byte_identical(tmp_path, monkeypatch):
    """no-op 保存（payload == 当前文件内容）→ 文件逐字节不变。"""
    target = _isolate(tmp_path, monkeypatch)
    payload = yaml.safe_load(target.read_text(encoding="utf-8"))
    before = _sha256(target)
    _write_strategy_preserving_comments(copy.deepcopy(payload))
    after = _sha256(target)
    assert before == after, "no-op 保存后文件字节变化（注释/排版被重排）"


def test_d1_single_value_change_diff_only_that_line(tmp_path, monkeypatch):
    """真实改一个策略值（base_add_amount 10000→11000）→ diff 只含该行。"""
    target = _isolate(tmp_path, monkeypatch)
    orig_text = target.read_text(encoding="utf-8")
    payload = yaml.safe_load(orig_text)
    old = int(payload["strategy"]["base_add_amount"])
    new = old + 1000
    payload["strategy"]["base_add_amount"] = new
    _write_strategy_preserving_comments(payload)

    new_text = target.read_text(encoding="utf-8")
    reloaded = yaml.safe_load(new_text)
    assert int(reloaded["strategy"]["base_add_amount"]) == new
    # 其余字段不变
    for section in payload:
        for k, v in payload[section].items():
            assert reloaded[section][k] == v, f"{section}.{k} 意外变化"

    old_lines = orig_text.splitlines()
    new_lines = new_text.splitlines()
    assert len(old_lines) == len(new_lines), "行数变化（不应增删行）"
    changed = [(i, a, b) for i, (a, b) in enumerate(zip(old_lines, new_lines), 1) if a != b]
    assert len(changed) == 1, f"应恰好 1 行变化，实际 {len(changed)}: {changed}"
    assert "base_add_amount:" in changed[0][1] and "base_add_amount:" in changed[0][2]

    def _comment_lines(text):
        return sum(1 for ln in text.splitlines() if "#" in ln)

    assert _comment_lines(orig_text) == _comment_lines(new_text), "注释行数变化"


def test_d1_change_then_revert_byte_identical(tmp_path, monkeypatch):
    """改两个值 → 再写回原值 → 文件逐字节还原。"""
    target = _isolate(tmp_path, monkeypatch)
    orig_text = target.read_text(encoding="utf-8")
    payload = yaml.safe_load(orig_text)

    old_base = int(payload["strategy"]["base_add_amount"])
    old_growth = float(payload["strategy"]["growth_ratio"])
    payload["strategy"]["base_add_amount"] = old_base + 1000
    payload["strategy"]["growth_ratio"] = round(old_growth + 0.05, 2)
    _write_strategy_preserving_comments(payload)

    restore = yaml.safe_load(target.read_text(encoding="utf-8"))
    restore["strategy"]["base_add_amount"] = old_base
    restore["strategy"]["growth_ratio"] = old_growth
    _write_strategy_preserving_comments(restore)

    final_text = target.read_text(encoding="utf-8")
    assert final_text == orig_text, "恢复原值后文件未逐字节还原"


# ---------------------------------------------------------------------------
# D2：SSE 日志分类器（v7 回测任务语义）
# ---------------------------------------------------------------------------

def test_d2_progress_line_structured():
    ev = _classify_log_line("[PROGRESS] stage=bt done=42 total=101", DONE_MARKERS)
    assert ev == {"type": "progress", "stage": "bt", "done": 42, "total": 101}


def test_d2_done_marker():
    ev = _classify_log_line("回测完成 · 窗口 2016-01-04 ~ 2026-10-21 · 101 只", DONE_MARKERS)
    assert ev == {"type": "done"}


def test_d2_error_lines():
    assert _classify_log_line("Traceback (most recent call last):", DONE_MARKERS) == {"type": "error"}
    assert _classify_log_line("错误: 数据湖被锁（backfill 灌数进行中）", DONE_MARKERS) == {"type": "error"}


def test_d2_plain_log():
    ev = _classify_log_line("  sh.601088 中国神华: 11 买 …", DONE_MARKERS)
    assert ev == {"type": "log", "text": "  sh.601088 中国神华: 11 买 …"}


def test_d2_progress_not_shadowed_by_done_marker():
    """[PROGRESS] 行即便含 done 字样也必须走 progress 分支（结构化优先）。"""
    ev = _classify_log_line("[PROGRESS] stage=st done=1 total=2", ["done"])
    assert ev["type"] == "progress"
