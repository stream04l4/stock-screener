# -*- coding: utf-8 -*-
"""v6.2 O3 回归测试：POST /api/strategy/import（加载策略文件）+ PUT 抽公共函数零回归。

纪律（brief 红线）：
- strategy.yaml 是生产配置 → 所有写路径测试用 tmp 副本（monkeypatch CONFIG_PATH），绝不写生产 yaml；
- .bak 机制保留（备份路径由 CONFIG_PATH 现算，tmp 隔离下落 tmp）；
- PUT /api/strategy 对外行为逐字节不变（抽 _apply_strategy_write 后现有测试原样全绿 + 本文件补响应形状断言）。

覆盖：
1) import 合法 yaml → 200 {ok, backup, sections}，写盘生效 + .bak 备份存在；
2) import 非法 yaml（语法错误）→ 400 errors，不写盘；
3) import 非 mapping（list）→ 400 errors；
4) import 缺字段 → 400 errors（复用 PUT 同一套 _validate_strategy）；
5) import 类型错误 → 400 errors；
6) PUT 响应形状逐字节同旧版 {"ok": True, "backup": str}（不含 sections）。

不联网、不碰 BaoStock、不碰生产 config/strategy.yaml。
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402
import yaml  # noqa: E402

import web.app as appmod  # noqa: E402

REAL_CONFIG = PROJECT_ROOT / "config" / "strategy.yaml"


def _setup_tmp_config(tmp_path, monkeypatch) -> Path:
    """把真实 strategy.yaml 复制到 tmp，monkeypatch CONFIG_PATH 指向 tmp（生产零触碰）。"""
    cfg = tmp_path / "config"
    cfg.mkdir()
    target = cfg / "strategy.yaml"
    target.write_text(REAL_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(appmod, "CONFIG_PATH", target)
    return target


def _valid_payload() -> dict:
    """当前生产 yaml 的合法 payload（改一个值确保写盘路径真正执行）。"""
    p = yaml.safe_load(REAL_CONFIG.read_text(encoding="utf-8"))
    p["scoring"]["top_n"] = 51  # 触发真实改动（原 50）
    return p


# ---------------------------------------------------------------------------
# import：合法 → 200 + 写盘 + 备份
# ---------------------------------------------------------------------------

def test_import_valid_yaml_200_writes_and_backs_up(tmp_path, monkeypatch):
    target = _setup_tmp_config(tmp_path, monkeypatch)
    before_hash = target.read_bytes()

    payload = _valid_payload()
    raw = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
    res = appmod.import_strategy(appmod.ImportStrategyRequest(raw=raw))

    # 200 形状：{ok, backup, sections}
    assert res["ok"] is True
    assert isinstance(res["backup"], str) and res["backup"].endswith("strategy.yaml.bak")
    assert "scoring" in res["sections"] and "technical" in res["sections"]

    # 写盘生效（top_n 50→51）
    reloaded = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert reloaded["scoring"]["top_n"] == 51

    # .bak 备份存在且 = 改前内容
    bak = Path(res["backup"])
    assert bak.exists()
    assert bak.read_bytes() == before_hash


def test_import_noop_roundtrip_byte_identical(tmp_path, monkeypatch):
    """import 与当前文件语义相同的 yaml → round-trip 逐字节还原（D-W03 注释保留）。"""
    target = _setup_tmp_config(tmp_path, monkeypatch)
    orig_text = target.read_text(encoding="utf-8")

    raw = orig_text  # 原样导入（no-op）
    res = appmod.import_strategy(appmod.ImportStrategyRequest(raw=raw))
    assert res["ok"] is True
    assert target.read_text(encoding="utf-8") == orig_text, "no-op import 后文件字节变化"


# ---------------------------------------------------------------------------
# import：非法 → 400 errors，不写盘
# ---------------------------------------------------------------------------

def test_import_invalid_yaml_syntax_400(tmp_path, monkeypatch):
    target = _setup_tmp_config(tmp_path, monkeypatch)
    before = target.read_bytes()
    with pytest.raises(appmod.HTTPException) as ei:
        appmod.import_strategy(appmod.ImportStrategyRequest(raw="a: [unclosed\n  bad: :"))
    assert ei.value.status_code == 400
    assert "YAML 解析失败" in ei.value.detail["errors"][0]
    assert target.read_bytes() == before, "非法 yaml 不应写盘"


def test_import_non_mapping_400(tmp_path, monkeypatch):
    target = _setup_tmp_config(tmp_path, monkeypatch)
    before = target.read_bytes()
    with pytest.raises(appmod.HTTPException) as ei:
        appmod.import_strategy(appmod.ImportStrategyRequest(raw="- 1\n- 2\n"))  # list
    assert ei.value.status_code == 400
    assert any("mapping" in e for e in ei.value.detail["errors"])
    assert target.read_bytes() == before


def test_import_missing_field_400(tmp_path, monkeypatch):
    """缺字段 → 复用 PUT 同一套 _validate_strategy 报 400。"""
    target = _setup_tmp_config(tmp_path, monkeypatch)
    before = target.read_bytes()
    p = _valid_payload()
    del p["dividend"]["min_yield_pct"]
    raw = yaml.safe_dump(p, allow_unicode=True, sort_keys=False)
    with pytest.raises(appmod.HTTPException) as ei:
        appmod.import_strategy(appmod.ImportStrategyRequest(raw=raw))
    assert ei.value.status_code == 400
    assert any("dividend.min_yield_pct" in e for e in ei.value.detail["errors"])
    assert target.read_bytes() == before


def test_import_type_error_400(tmp_path, monkeypatch):
    """类型错误（top_n 改字符串）→ 400。"""
    target = _setup_tmp_config(tmp_path, monkeypatch)
    before = target.read_bytes()
    p = _valid_payload()
    p["scoring"]["top_n"] = "fifty"
    raw = yaml.safe_dump(p, allow_unicode=True, sort_keys=False)
    with pytest.raises(appmod.HTTPException) as ei:
        appmod.import_strategy(appmod.ImportStrategyRequest(raw=raw))
    assert ei.value.status_code == 400
    assert any("scoring.top_n" in e for e in ei.value.detail["errors"])
    assert target.read_bytes() == before


# ---------------------------------------------------------------------------
# PUT：抽公共函数后对外行为逐字节不变（响应形状 + 写盘/备份）
# ---------------------------------------------------------------------------

def test_put_response_shape_unchanged(tmp_path, monkeypatch):
    """PUT 响应仍为 {"ok": True, "backup": str}（不含 sections，逐字节同旧版）。"""
    target = _setup_tmp_config(tmp_path, monkeypatch)
    payload = _valid_payload()
    res = appmod.put_strategy(copy.deepcopy(payload))
    assert set(res.keys()) == {"ok", "backup"}
    assert res["ok"] is True
    assert isinstance(res["backup"], str) and res["backup"].endswith("strategy.yaml.bak")
    # 写盘生效 + 备份存在
    reloaded = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert reloaded["scoring"]["top_n"] == 51
    assert Path(res["backup"]).exists()


def test_put_invalid_400_shape(tmp_path, monkeypatch):
    """PUT 非法 payload → 400 {errors}（形状不变）。"""
    target = _setup_tmp_config(tmp_path, monkeypatch)
    before = target.read_bytes()
    p = _valid_payload()
    p["scoring"]["top_n"] = -5  # 超范围
    with pytest.raises(appmod.HTTPException) as ei:
        appmod.put_strategy(p)
    assert ei.value.status_code == 400
    assert "errors" in ei.value.detail
    assert target.read_bytes() == before


# ---------------------------------------------------------------------------
# import 与 PUT 走同一校验函数（行为一致性）
# ---------------------------------------------------------------------------

def test_import_and_put_share_validation(tmp_path, monkeypatch):
    """同一非法 payload：import 与 PUT 都应 400 且报同样的字段错误。"""
    target = _setup_tmp_config(tmp_path, monkeypatch)
    p = _valid_payload()
    del p["hard_filter"]["st_enabled"]

    raw = yaml.safe_dump(p, allow_unicode=True, sort_keys=False)
    with pytest.raises(appmod.HTTPException) as ei_imp:
        appmod.import_strategy(appmod.ImportStrategyRequest(raw=raw))
    with pytest.raises(appmod.HTTPException) as ei_put:
        appmod.put_strategy(copy.deepcopy(p))
    assert ei_imp.value.status_code == ei_put.value.status_code == 400
    assert ei_imp.value.detail["errors"] == ei_put.value.detail["errors"], "import/PUT 校验不一致"
