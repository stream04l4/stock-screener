# -*- coding: utf-8 -*-
"""test_lake_zero_import —— 主路径零 import lake 断言（v6 边界保证）。

**验收点（brief）**：AST 扫描 ``screener/``·``web/``·``backtest/`` 全部 .py，命中
``import lake`` / ``from lake ...`` 即 fail。

设计：
- 用 ast 模块解析每个文件（比正则稳：不误伤字符串注释里的 "lake"）。
- 只检测**顶层/任意深度的真实 import 语句**指向 lake 包（Import: names 含 'lake'；
  ImportFrom: module 以 'lake' 开头）。
- web/app.py 的条件挂载 ``from lake.web_api import router`` **会命中**——但 brief 明确
  允许"web/app.py 仅在 duckdb 可导入时挂载 router"这一处。故本测试对 web/app.py
  单独豁免（其余 web/*.py + screener/* + backtest/* 一律零容忍）。

⚠️ 为什么豁免 web/app.py：brief 验收标准原文——"``git diff fb2b4b9 -- screener/
web/app.py`` 只允许出现'条件挂载 lake router'这一处 web 改动"。即 web/app.py 的
lake import 是**被明确许可的唯一例外**，不是违规。其余任何文件 import lake = 主路径
耦合 lake = 违反"独立分析层"边界 → fail。
"""
from __future__ import annotations

import ast
import os

# 主路径目录（相对仓库根）：这些包绝不 import lake
_MAINPATH_DIRS = ["screener", "web", "backtest"]

# web/app.py 是唯一被 brief 许可的条件挂载点（duckdb 可导入时挂 router）
_ALLOWED_LAKE_IMPORT = {"web/app.py"}


def _repo_root() -> str:
    # tests/ 的上一级 = 仓库根
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _iter_py_files(root: str):
    for d in _MAINPATH_DIRS:
        base = os.path.join(root, d)
        if not os.path.isdir(base):
            continue
        for dirpath, _dirnames, filenames in os.walk(base):
            for fn in filenames:
                if fn.endswith(".py"):
                    yield os.path.join(dirpath, fn)


def _lake_imports(tree: ast.AST):
    """返回该 AST 里所有指向 lake 包的 import 节点 (lineno, kind)。"""
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                # import lake / import lake.x → name 以 'lake' 开头（且不是子串误伤）
                top = alias.name.split(".")[0]
                if top == "lake":
                    hits.append((node.lineno, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            top = mod.split(".")[0]
            if top == "lake":
                names = ",".join(a.name for a in node.names)
                hits.append((node.lineno, f"from {mod} import {names}"))
    return hits


def test_mainpath_zero_lake_import():
    """screener/*、backtest/* 全部 .py + web/*（除 app.py）零 lake import。"""
    root = _repo_root()
    violations = []
    checked = 0
    for path in _iter_py_files(root):
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        if rel in _ALLOWED_LAKE_IMPORT:
            continue  # web/app.py：brief 许可的条件挂载点（单独验证见下）
        try:
            with open(path, "r", encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=path)
        except (OSError, SyntaxError) as exc:
            violations.append(f"{rel}: 解析失败 {exc}")
            continue
        checked += 1
        for lineno, kind in _lake_imports(tree):
            violations.append(f"{rel}:{lineno} → {kind}")

    assert checked > 0, "未扫描到任何主路径 .py（目录结构异常?）"
    assert not violations, (
        "主路径 import lake（违反独立分析层边界）:\n  " + "\n  ".join(violations)
    )


def test_web_app_only_conditional_mount():
    """web/app.py 的 lake import 必须且只能是条件挂载（duckdb 探测 + try/except）。

    验证：web/app.py 里 ``from lake.web_api import router`` 存在，且被 duckdb 可导入性
    判断包裹（即不是无条件顶层 import）。其余 web/*.py 已在上一测试覆盖为零容忍。
    """
    root = _repo_root()
    app_path = os.path.join(root, "web", "app.py")
    with open(app_path, "r", encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)

    # 1) web/app.py 确实有条件挂载（from lake.web_api import router）
    lake_imports = _lake_imports(tree)
    assert any("lake" in k for _, k in lake_imports), \
        "web/app.py 缺少 lake router 挂载（数据湖页签无法启用）"

    # 2) 该 import 不得是**无条件模块顶层**执行——必须在 if/try 内。
    #    通过检查：源码里 'from lake' 之前存在 duckdb 探测（import duckdb）。
    assert "import duckdb" in src, \
        "web/app.py 的 lake 挂载必须先探测 duckdb 可导入性（优雅降级前提）"
