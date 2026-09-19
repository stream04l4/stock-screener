# -*- coding: utf-8 -*-
"""v6.2.1 S1 回归测试：策略库端点（本机命名保存 + 下拉加载 + 删除）。

纪律（brief 红线）：
- strategy.yaml 是生产配置 → 所有写路径测试用 tmp 副本（monkeypatch CONFIG_PATH），绝不写生产 yaml；
- data/strategies/ 同理 monkeypatch STRATEGIES_DIR 到 tmp（.gitignore 不入库，生产目录零触碰）；
- 加载端点复用 PUT 同一套 _apply_strategy_write（校验→.bak→写盘，行为逐字节同 PUT/import）。

覆盖：
1) POST /api/strategies 合法名 → 201 {name, path, saved_at} + 文件落盘 = 当前 config 原文；
2) name 缺省/空串/纯空白 → 默认名 策略_YYYYMMDD_HHMM（正则断言）；
3) 中文名（CJK）→ 正常保存（Linux UTF-8 文件名）；
4) sanitize：路径分隔符 / \\ → _、.. 剔除、空格→下划线、非法字符剔除；
5) 重名 → 409 {error:"strategy_exists"}，原文件不被覆盖；
6) GET /api/strategies → [{name, saved_at, size_bytes}] 按 saved_at 降序；目录不存在 → []；
7) DELETE 存在 → 204 + 文件消失；不存在 → 404；
8) POST load 合法快照 → 200 {ok, backup} + config 恢复为快照内容 + .bak = 加载前内容；
9) load 非法 yaml 快照 → 400，config 零改动；load 缺字段快照 → 400（复用 PUT 校验）；
10) load/delete 不存在的 name → 404；
11) _sanitize_strategy_name 单元边界。

不联网、不碰 BaoStock、不碰生产 config/strategy.yaml 与 data/strategies/。
"""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402
import yaml  # noqa: E402

import web.app as appmod  # noqa: E402

REAL_CONFIG = PROJECT_ROOT / "config" / "strategy.yaml"


def _setup(tmp_path, monkeypatch) -> tuple[Path, Path]:
    """tmp config + tmp strategies dir（monkeypatch CONFIG_PATH / STRATEGIES_DIR，生产零触碰）。"""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True)
    target = cfg_dir / "strategy.yaml"
    target.write_text(REAL_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    strat_dir = tmp_path / "strategies"
    monkeypatch.setattr(appmod, "CONFIG_PATH", target)
    monkeypatch.setattr(appmod, "STRATEGIES_DIR", strat_dir)
    return target, strat_dir


def _payload_top_n(n: int) -> dict:
    """当前生产 yaml 改 scoring.top_n（触发真实改动）。"""
    p = yaml.safe_load(REAL_CONFIG.read_text(encoding="utf-8"))
    p["scoring"]["top_n"] = n
    return p


# ---------------------------------------------------------------------------
# _sanitize_strategy_name 单元边界
# ---------------------------------------------------------------------------

def test_sanitize_basic_rules():
    s = appmod._sanitize_strategy_name
    assert s("我的策略 v1") == "我的策略_v1"          # CJK + 空格→下划线 + 字母数字保留
    assert s("a/b\\c") == "a_b_c"                    # 路径分隔符 → _
    assert s("..") == ""                             # .. 剔除后为空
    assert s("a..b") == "ab"                         # 中间 .. 剔除
    assert s("  策略名  ") == "策略名"                # 首尾空白去除
    assert s("bad*name?!.") == "badname"             # 非法字符剔除（保留字母数字-_）
    assert s("a-b_c9") == "a-b_c9"                   # -_ 与数字原样
    assert s("") == ""                               # 空 → 空（调用方落默认名）


# ---------------------------------------------------------------------------
# POST /api/strategies：命名保存
# ---------------------------------------------------------------------------

def test_save_named_201_writes_snapshot(tmp_path, monkeypatch):
    target, strat_dir = _setup(tmp_path, monkeypatch)
    orig_bytes = target.read_bytes()
    res = appmod.save_strategy(appmod.SaveStrategyRequest(name="高股息变体"))
    assert set(res.keys()) == {"name", "path", "saved_at"}
    assert res["name"] == "高股息变体"               # 中文名原样（UTF-8 文件名）
    p = Path(res["path"])
    assert p == strat_dir / "高股息变体.yaml"
    assert p.exists()
    assert p.read_bytes() == orig_bytes, "快照必须 = 当前 config/strategy.yaml 原文（保留注释排版）"


def test_save_default_name_when_missing_or_blank(tmp_path, monkeypatch):
    """name 缺省/None/空串/纯空白 → 默认名 策略_YYYYMMDD_HHMM（各自独立 tmp，各为首存必成功）。"""
    for i, req in enumerate((appmod.SaveStrategyRequest(), appmod.SaveStrategyRequest(name=None),
                             appmod.SaveStrategyRequest(name=""), appmod.SaveStrategyRequest(name="   "))):
        target, strat_dir = _setup(tmp_path / f"d{i}", monkeypatch)
        res = appmod.save_strategy(req)
        # 默认名 策略_YYYYMMDD_HHMM（本地时间，分钟粒度）
        assert re.fullmatch(r"策略_\d{8}_\d{4}", res["name"]), res["name"]
        assert (strat_dir / f"{res['name']}.yaml").exists()


def test_save_default_name_same_minute_collision_409(tmp_path, monkeypatch):
    """默认名分钟粒度：同分钟内二次缺省保存 → 撞名 409 strategy_exists（契约语义，前端 toast 提示换名）。"""
    target, strat_dir = _setup(tmp_path, monkeypatch)
    first = appmod.save_strategy(appmod.SaveStrategyRequest())
    with pytest.raises(appmod.HTTPException) as ei:
        appmod.save_strategy(appmod.SaveStrategyRequest())
    assert ei.value.status_code == 409
    assert ei.value.detail["error"] == "strategy_exists"
    # 首存文件不受影响
    assert (strat_dir / f"{first['name']}.yaml").exists()


def test_save_duplicate_409_strategy_exists(tmp_path, monkeypatch):
    target, strat_dir = _setup(tmp_path, monkeypatch)
    appmod.save_strategy(appmod.SaveStrategyRequest(name="dup"))
    before = (strat_dir / "dup.yaml").read_bytes()
    with pytest.raises(appmod.HTTPException) as ei:
        appmod.save_strategy(appmod.SaveStrategyRequest(name="dup"))
    assert ei.value.status_code == 409
    assert ei.value.detail["error"] == "strategy_exists"   # brief 契约标签
    assert (strat_dir / "dup.yaml").read_bytes() == before, "重名不得覆盖原快照"


# ---------------------------------------------------------------------------
# GET /api/strategies：列表（saved_at 降序；目录不存在 → []）
# ---------------------------------------------------------------------------

def test_list_empty_when_dir_missing(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)  # STRATEGIES_DIR 指向不存在的 tmp 目录
    assert appmod.list_strategies() == []


def test_list_shape_and_desc_order(tmp_path, monkeypatch):
    target, strat_dir = _setup(tmp_path, monkeypatch)
    strat_dir.mkdir()
    a = strat_dir / "aaa.yaml"
    b = strat_dir / "bbb.yaml"
    a.write_text("a: 1\n", encoding="utf-8")
    b.write_text("b: 2\n", encoding="utf-8")
    t_old, t_new = time.time() - 3600, time.time()
    os.utime(a, (t_old, t_old))
    os.utime(b, (t_new, t_new))
    out = appmod.list_strategies()
    assert [x["name"] for x in out] == ["bbb", "aaa"], "必须按 saved_at 降序"
    assert set(out[0].keys()) == {"name", "saved_at", "size_bytes"}
    assert out[1]["size_bytes"] == a.stat().st_size
    # 非 .yaml 文件不进列表
    (strat_dir / "notes.txt").write_text("x", encoding="utf-8")
    assert [x["name"] for x in appmod.list_strategies()] == ["bbb", "aaa"]


# ---------------------------------------------------------------------------
# DELETE /api/strategies/{name}
# ---------------------------------------------------------------------------

def test_delete_existing_204(tmp_path, monkeypatch):
    target, strat_dir = _setup(tmp_path, monkeypatch)
    appmod.save_strategy(appmod.SaveStrategyRequest(name="todelete"))
    assert (strat_dir / "todelete.yaml").exists()
    resp = appmod.delete_strategy("todelete")
    assert resp.status_code == 204
    assert not (strat_dir / "todelete.yaml").exists()


def test_delete_missing_404(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    with pytest.raises(appmod.HTTPException) as ei:
        appmod.delete_strategy("nope")
    assert ei.value.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/strategies/{name}/load：复用 PUT 同一套校验 + .bak + 写盘
# ---------------------------------------------------------------------------

def test_load_valid_restores_config_and_backs_up(tmp_path, monkeypatch):
    target, strat_dir = _setup(tmp_path, monkeypatch)
    # 1) 快照 top_n=77（改 config → 保存）
    p = _payload_top_n(77)
    target.write_text(yaml.safe_dump(p, allow_unicode=True, sort_keys=False), encoding="utf-8")
    appmod.save_strategy(appmod.SaveStrategyRequest(name="snap77"))
    # 2) 再把 config 改成 top_n=51（模拟后续编辑）
    p2 = _payload_top_n(51)
    target.write_text(yaml.safe_dump(p2, allow_unicode=True, sort_keys=False), encoding="utf-8")
    before_load = target.read_bytes()
    # 3) 加载快照 → config 恢复为 top_n=77，.bak = 加载前内容
    res = appmod.load_strategy("snap77")
    assert res["ok"] is True
    assert isinstance(res["backup"], str) and res["backup"].endswith("strategy.yaml.bak")
    reloaded = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert reloaded["scoring"]["top_n"] == 77
    assert Path(res["backup"]).read_bytes() == before_load


def test_load_invalid_yaml_400_no_write(tmp_path, monkeypatch):
    target, strat_dir = _setup(tmp_path, monkeypatch)
    strat_dir.mkdir()
    (strat_dir / "broken.yaml").write_text("a: [unclosed\n  bad: :", encoding="utf-8")
    before = target.read_bytes()
    with pytest.raises(appmod.HTTPException) as ei:
        appmod.load_strategy("broken")
    assert ei.value.status_code == 400
    assert "YAML 解析失败" in ei.value.detail["errors"][0]
    assert target.read_bytes() == before, "非法快照不得写盘"


def test_load_missing_field_400_shares_put_validation(tmp_path, monkeypatch):
    """缺字段快照 → 复用 PUT 同一套 _validate_strategy（与 import/PUT 同错误）。"""
    target, strat_dir = _setup(tmp_path, monkeypatch)
    p = _payload_top_n(51)
    del p["dividend"]["min_yield_pct"]
    strat_dir.mkdir()
    (strat_dir / "incomplete.yaml").write_text(
        yaml.safe_dump(p, allow_unicode=True, sort_keys=False), encoding="utf-8")
    before = target.read_bytes()
    with pytest.raises(appmod.HTTPException) as ei:
        appmod.load_strategy("incomplete")
    assert ei.value.status_code == 400
    assert any("dividend.min_yield_pct" in e for e in ei.value.detail["errors"])
    assert target.read_bytes() == before


def test_load_missing_snapshot_404(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    with pytest.raises(appmod.HTTPException) as ei:
        appmod.load_strategy("ghost")
    assert ei.value.status_code == 404


# ---------------------------------------------------------------------------
# load 与 PUT/import 走同一校验函数（行为一致性）
# ---------------------------------------------------------------------------

def test_load_and_put_share_validation(tmp_path, monkeypatch):
    """同一非法内容：load（经快照文件）与 PUT 都 400 且报同样的字段错误。"""
    target, strat_dir = _setup(tmp_path, monkeypatch)
    p = _payload_top_n(51)
    del p["hard_filter"]["st_enabled"]
    strat_dir.mkdir()
    (strat_dir / "bad.yaml").write_text(
        yaml.safe_dump(p, allow_unicode=True, sort_keys=False), encoding="utf-8")
    with pytest.raises(appmod.HTTPException) as ei_load:
        appmod.load_strategy("bad")
    with pytest.raises(appmod.HTTPException) as ei_put:
        appmod.put_strategy(yaml.safe_load(yaml.safe_dump(p, allow_unicode=True, sort_keys=False)))
    assert ei_load.value.status_code == ei_put.value.status_code == 400
    assert ei_load.value.detail["errors"] == ei_put.value.detail["errors"], "load/PUT 校验不一致"
