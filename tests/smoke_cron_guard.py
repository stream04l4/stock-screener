# -*- coding: utf-8 -*-
"""run_cron.sh 全脚本端到端验证（离线，fake baostock + stub screener，不碰 live BaoStock）。

9/7 缺陷的第二处泄漏：旧 run_cron.sh 的交易日守卫依赖 BaoStock。BaoStock 封禁时
login/query_trade_dates 失败 → 非零退出被误读成"非交易日，跳过"（exit 0），静默吞掉
数据源级失败。修复后脚本区分三种结果并正确路由：
  守卫 exit 0 = 交易日     → 继续跑 screener
  守卫 exit 1 = 非交易日    → "非交易日，跳过" + exit 0（不跑 screener）
  守卫 exit 3 = 数据源级失败 → 写 sidecar + "守卫失败" + exit 3（不静默、不跑 screener）

本验证把**真实 run_cron.sh** 复制到隔离目录，仅替换 `cd` 目标与 `.venv/bin/python`：
- stub python 遇到 screener 调用 → 打印 SCREENER_RAN（不真跑全市场筛选）；
- stub python 遇到守卫调用（`python - "$TODAY"`）→ re-exec **真实守卫体**（从脚本提取），
  用 fake baostock（PYTHONPATH 前置遮蔽真实库）驱动 → 产生真实的退出码 + sidecar 落盘。
这样测的是**完整 cron 路径**：真实 bash case 路由 + 真实守卫逻辑（含 sidecar）。零 live BaoStock。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

RUN_CRON = PROJECT_ROOT / "run_cron.sh"


def _banner(t):
    print("\n" + "=" * 70)
    print(t)
    print("=" * 70)


# stub .venv/bin/python：screener→SCREENER_RAN；守卫→re-exec 真实守卫体（fake baostock）
STUB_PY = '''#!/usr/bin/env python3
import os, sys, runpy
argv = sys.argv[1:]
if "screener" in argv:
    print("SCREENER_RAN"); raise SystemExit(0)
if argv and argv[0] == "-":
    # 守卫调用（python - "$TODAY"）→ re-exec 真实守卫体，透传日期参数
    os.execv(sys.executable, [sys.executable, "guard_body.py"] + argv[1:])
if argv and argv[0] == "guard_body.py":
    runpy.run_path("guard_body.py", run_name="__main__")
raise SystemExit(2)  # 未预期的调用形态
'''

# fake baostock：按 FAKE_BS_MODE 模拟 BaoStock 各状态（login 失败 / 查询失败 / 交易日 / 非交易日）
FAKE_BS = '''
import os

class _Rs:
    def __init__(self, code, msg="", rows=None):
        self.error_code = code; self.error_msg = msg
        self._rows = list(rows or []); self._i = 0
    def next(self):
        if self._i < len(self._rows):
            self._i += 1; return True
        return False
    def get_row_data(self):
        return self._rows[self._i - 1]

MODE = os.environ.get("FAKE_BS_MODE", "trading")

def login():
    if MODE == "login_fail":
        return _Rs("10002001", "黑名单用户，请与管理员联系")
    return _Rs("0", "success")

def query_trade_dates(*a, **k):
    if MODE == "trade_err":
        return _Rs("10001011", "黑名单用户，请与管理员联系")
    if MODE == "nontrading":
        return _Rs("0", "ok", [["2026-09-05", "0"]])   # is_trading_day=0
    return _Rs("0", "ok", [["2026-09-07", "1"]])       # trading

def logout():
    return _Rs("0", "success")
'''


def extract_guard_body() -> str:
    text = RUN_CRON.read_text(encoding="utf-8")
    m = re.search(r"<<'PYEOF'[^\n]*\n(.*?)\nPYEOF", text, re.DOTALL)
    assert m, "run_cron.sh 中未找到 PYEOF 守卫块"
    return m.group(1)


def build_isolated(workdir: Path) -> Path:
    """复制真实 run_cron.sh（仅替换 cd 目标），铺设 stub python + fake baostock + 守卫体。"""
    text = RUN_CRON.read_text(encoding="utf-8")
    patched = re.sub(r"cd\s+\S+.*\|\|\s+exit\s+1", f"cd {workdir} || exit 1", text, count=1)
    assert patched != text, "未能替换 run_cron.sh 的 cd 目标"
    script = workdir / "run_cron_isolated.sh"
    script.write_text(patched, encoding="utf-8")
    os.chmod(script, 0o755)

    vpy = workdir / ".venv" / "bin" / "python"
    vpy.parent.mkdir(parents=True, exist_ok=True)
    vpy.write_text(STUB_PY, encoding="utf-8")
    os.chmod(vpy, 0o755)

    fake = workdir / "fakebs"
    fake.mkdir(exist_ok=True)
    (fake / "baostock.py").write_text(FAKE_BS, encoding="utf-8")

    # 真实守卫体（含 sidecar 写入逻辑）—— re-exec 时 cwd=workdir，runstatus 从 PROJECT_ROOT 导入
    (workdir / "guard_body.py").write_text(extract_guard_body(), encoding="utf-8")
    return script


def run_full_script(mode: str, workdir: Path) -> tuple[int, str]:
    env = dict(os.environ)
    env["FAKE_BS_MODE"] = mode
    # fake baostock 前置遮蔽真实库；PROJECT_ROOT 供 `from screener import runstatus`
    env["PYTHONPATH"] = f"{workdir / 'fakebs'}{os.pathsep}{PROJECT_ROOT}"
    r = subprocess.run(
        ["bash", str(workdir / "run_cron_isolated.sh")],
        cwd=str(workdir), env=env, capture_output=True, text=True, timeout=60,
    )
    return r.returncode, (r.stdout + r.stderr)


def main():
    cases = [
        # (mode, 期望脚本退出码, 期望是否跑 screener, 期望是否写 sidecar, 描述)
        ("trading",    0, True,  False, "交易日（数据源正常）→ 继续选股"),
        ("nontrading", 0, False, False, "非交易日（数据源正常）→ 跳过，不误报失败"),
        ("login_fail", 3, False, True,  "BaoStock login 失败 → 显式失败 + sidecar，不静默跳过"),
        ("trade_err",  3, False, True,  "query_trade_dates 失败 → 显式失败 + sidecar，不静默跳过"),
    ]

    with tempfile.TemporaryDirectory() as td:
        workdir = Path(td)
        build_isolated(workdir)

        for mode, exp_rc, exp_screener, exp_sidecar, desc in cases:
            _banner(f"cron 路由：{desc}  (FAKE_BS_MODE={mode})")
            rc, out = run_full_script(mode, workdir)
            screener_ran = "SCREENER_RAN" in out
            sidecars = sorted(p.name for p in workdir.glob("output/run_status_*.json")) \
                if (workdir / "output").exists() else []
            print(f"[脚本退出码] {rc}   (期望 {exp_rc})")
            print(f"[是否触发 screener] {screener_ran}   (期望 {exp_screener})")
            print(f"[sidecar 落盘] {sidecars}   (期望{'有' if exp_sidecar else '无'})")
            for ln in out.splitlines():
                if "SCREENER_RAN" not in ln and ln.strip():
                    print(f"  | {ln}")

            assert rc == exp_rc, f"{mode}: 期望脚本 exit {exp_rc}，实际 {rc}"
            assert screener_ran == exp_screener, \
                f"{mode}: screener 触发={screener_ran}，期望 {exp_screener}"
            if exp_sidecar:
                assert sidecars, f"{mode}: 数据源级失败必须写 sidecar"
                import json
                print("  [sidecar]", json.dumps(
                    json.loads((workdir / "output" / sidecars[0]).read_text()), ensure_ascii=False))
            else:
                assert not sidecars, f"{mode}: 非失败场景不得写 sidecar"
            # 关键：login_fail/trade_err 绝不能被误读成"非交易日跳过"（旧缺陷：exit 0 + 无 screener）
            if mode in ("login_fail", "trade_err"):
                assert rc != 0, f"{mode}: 数据源级失败不得静默 exit 0（9/7 旧缺陷现场）"
            print(f">>> PASS：{desc}")

    _banner("run_cron.sh 全脚本端到端验证通过 ✅")


if __name__ == "__main__":
    main()
