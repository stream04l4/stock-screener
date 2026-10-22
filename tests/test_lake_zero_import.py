# -*- coding: utf-8 -*-
"""test_lake_zero_import —— v7 包边界断言（方向随 v7 架构反转）。

v6 语义：主路径（screener/web/backtest）不得 import lake。
v7 语义（调研 ①「边界 4」）：策略核心替换后多因子包已删，边界变为——

1. **lake/ 不得 import martingale**：数据湖是独立层，martingale 只通过 duckdb
   文件读 lake，不 import lake 包对象，更不得反向依赖策略包。
2. **martingale/ 不得 import lake**：新策略包自带 data.py（read_only duckdb
   连接），与 lake 包零 import 耦合（两包平行，文件级解耦）。
3. **web/app.py 允许条件挂载 lake router**（duckdb 探测 + try/except），
   其余 web/*.py 不得 import lake。

设计：AST 扫描（不误伤字符串/注释里的包名）。
"""
from __future__ import annotations

import ast
import os


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _iter_py_files(root: str, dirs):
    for d in dirs:
        base = os.path.join(root, d)
        if not os.path.isdir(base):
            continue
        for dirpath, _dirnames, filenames in os.walk(base):
            for fn in filenames:
                if fn.endswith(".py"):
                    yield os.path.join(dirpath, fn)


def _imports_of(tree: ast.AST, pkg: str):
    """返回 AST 中指向 pkg（或其子模块）的所有 import (lineno, kind)。"""
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == pkg:
                    hits.append((node.lineno, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod.split(".")[0] == pkg:
                names = ",".join(a.name for a in node.names)
                hits.append((node.lineno, f"from {mod} import {names}"))
    return hits


def _scan(root: str, dirs, pkg: str, allow=()):
    violations, checked = [], 0
    for path in _iter_py_files(root, dirs):
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        if rel in allow:
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=path)
        except (OSError, SyntaxError) as exc:
            violations.append(f"{rel}: 解析失败 {exc}")
            continue
        checked += 1
        for lineno, kind in _imports_of(tree, pkg):
            violations.append(f"{rel}:{lineno} → {kind}")
    return checked, violations


def test_lake_never_imports_martingale():
    """lake/ 全部 .py 零 martingale import（数据湖独立层边界）。"""
    root = _repo_root()
    checked, violations = _scan(root, ["lake"], "martingale")
    assert checked > 0, "未扫描到任何 lake .py（目录结构异常?）"
    assert not violations, (
        "lake import martingale（违反数据湖独立层边界）:\n  " + "\n  ".join(violations)
    )


def test_martingale_never_imports_lake():
    """martingale/ 全部 .py 零 lake import（新策略包自带 data.py，文件级解耦）。"""
    root = _repo_root()
    checked, violations = _scan(root, ["martingale"], "lake")
    assert checked > 0, "未扫描到任何 martingale .py（目录结构异常?）"
    assert not violations, (
        "martingale import lake（两包应平行、零 import 耦合）:\n  " + "\n  ".join(violations)
    )


def test_web_only_app_mounts_lake():
    """web/ 除 app.py 外零 lake import；app.py 的挂载必须 duckdb 探测在前。"""
    root = _repo_root()
    checked, violations = _scan(root, ["web"], "lake", allow={"web/app.py"})
    assert checked > 0, "未扫描到任何 web .py（目录结构异常?）"
    assert not violations, (
        "web import lake（仅 web/app.py 条件挂载被许可）:\n  " + "\n  ".join(violations)
    )

    app_path = os.path.join(root, "web", "app.py")
    with open(app_path, "r", encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
    assert any("lake" in k for _, k in _imports_of(tree, "lake")), \
        "web/app.py 缺少 lake router 挂载（数据湖页签无法启用）"
    assert "import duckdb" in src, \
        "web/app.py 的 lake 挂载必须先探测 duckdb 可导入性（优雅降级前提）"
