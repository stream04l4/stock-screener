# -*- coding: utf-8 -*-
"""v5.2 Phase 1：canonical 统一 Schema 派生层（报告 §3 选项 C）。

设计（TL 拍板 Q2=选项 C / Q4=只增量）：
- ``data/canonical/{field}.csv``，Phase 1 只做三字段：**close / dps / roe**。
- 每条记录带 ``source / fetched_at / data_version`` 三个溯源字段（brief 契约）。
- canonical **从 raw 构建**（不直接读网络）：生产管线先 record_response 落 raw、
  再 append 派生值；本模块提供 :meth:`CanonicalStore.build_from_raw` 供从 raw
  信封重建/补写（回滚重放用）。
- **只增量**（Q4）：自 v5.2 上线日起逐日追加，不做全史回填。现有缓存即事实
  canonical；历史段在报告标注 as_of。
- 幂等：同 (code, date/period) 键已存在 → 不重写（append-only + 去重）。
- 原子写：复用 cache.py 的临时文件 + os.replace 模式。
- **回滚点**：``canonical.enabled=false`` → :func:`canonical_store` 返回 None，
  全部 no-op；删 ``data/canonical``、``data/raw`` 目录 → 主路径行为逐字节不变。

零网络：本模块纯本地 IO。写失败只告警不抛（派生层是旁路，不得炸主路径）。
"""
from __future__ import annotations

import csv
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

log = logging.getLogger("screener.data.canonical")

# Phase 1 三字段（brief 契约）。列结构固定：溯源三字段必须在末尾。
CANONICAL_FIELDS = ("close", "dps", "roe")
TRACE_COLS = ("source", "fetched_at", "data_version")


def _safe(name: str) -> str:
    return "".join(c if (c.isalnum() or c in "._-") else "_" for c in name)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class CanonicalStore:
    """统一 Schema 派生存储。目录: {root}/{field}.csv（哨兵首行 + 表头）。"""

    SENTINEL = "stock-screener-canonical-v1"

    def __init__(self, root: str, data_version: str = "v5.2-p1") -> None:
        self.root = root
        self.data_version = data_version
        os.makedirs(root, exist_ok=True)

    # ---------- 列定义（Phase 1 三字段） ----------
    @staticmethod
    def columns_for(field: str) -> List[str]:
        """各字段 canonical 列。close/dps 按日；roe 按报告期。"""
        if field == "close":
            return ["code", "date", "value"] + list(TRACE_COLS)
        if field == "dps":
            # dps 事件驱动：ex_date（除权除息日）为时间键
            return ["code", "ex_date", "value"] + list(TRACE_COLS)
        if field == "roe":
            # roe 报告期口径：period=YYYYQn（与 BaoStock profit Q4 基准对齐）
            return ["code", "period", "value"] + list(TRACE_COLS)
        raise ValueError(f"未知 canonical 字段: {field}")

    def _path(self, field: str) -> str:
        return os.path.join(self.root, f"{_safe(field)}.csv")

    # ---------- 读 ----------
    def read(self, field: str) -> List[Dict[str, str]]:
        """读回某字段全部记录（dict 列表）；文件不存在/损坏 → []。"""
        path = self._path(field)
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8", newline="") as f:
                reader = csv.reader(f)
                sentinel = next(reader, None)
                if not sentinel or sentinel[0] != self.SENTINEL:
                    return []
                cols = next(reader, None)
                if cols is None:
                    return []
                return [dict(zip(cols, row)) for row in reader if len(row) == len(cols)]
        except (OSError, csv.Error) as exc:
            log.warning("canonical 读取失败 %s: %s", field, exc)
            return []

    def _existing_keys(self, field: str) -> set:
        cols = self.columns_for(field)
        key_cols = [c for c in cols if c not in TRACE_COLS]
        k1, k2 = key_cols[0], key_cols[1]
        return {(r.get(k1, ""), r.get(k2, "")) for r in self.read(field)}

    # ---------- 写（幂等 append） ----------
    def append(
        self,
        field: str,
        code: str,
        when: str,
        value: Any,
        source: str,
        fetched_at: Optional[str] = None,
    ) -> bool:
        """追加一条 canonical 记录。同 (code, when) 已存在 → False（幂等跳过）。

        :param when: close=交易日 date；dps=ex_date；roe=period(YYYYQn)。
        :return: True=写入；False=幂等跳过/失败。
        """
        if field not in CANONICAL_FIELDS:
            raise ValueError(f"未知 canonical 字段: {field}")
        cols = self.columns_for(field)
        key_cols = [c for c in cols if c not in TRACE_COLS]
        if (code, when) in self._existing_keys(field):
            return False
        row = {
            key_cols[0]: code,
            key_cols[1]: when,
            "value": "" if value is None else str(value),
            "source": source,
            "fetched_at": fetched_at or now_iso(),
            "data_version": self.data_version,
        }
        path = self._path(field)
        new_file = not os.path.exists(path)
        existing: List[Dict[str, str]] = [] if new_file else self.read(field)
        tmp = f"{path}.tmp.{os.getpid()}"
        try:
            with open(tmp, "w", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                w.writerow([self.SENTINEL])
                w.writerow(cols)
                for r in existing:
                    w.writerow([r.get(c, "") for c in cols])
                w.writerow([row.get(c, "") for c in cols])
            os.replace(tmp, path)
        except OSError as exc:
            log.warning("canonical 写入失败 %s %s/%s: %s（旁路，不阻塞主路径）",
                        field, code, when, exc)
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            return False
        return True

    # ---------- 从 raw 构建（回滚重放 / 补写） ----------
    def build_from_raw(self, raw_root: str, source_map: Dict[str, str]) -> Dict[str, int]:
        """扫描 raw 信封 → 按 source_map 派生 canonical。返回 {field: 新写条数}。

        :param raw_root: data/raw 根目录。
        :param source_map: {raw_source: canonical_field}，如
            {"tencent": "close", "akshare_em": "dps", "sina": "roe"}。
        解析规则（Phase 1）：
        - tencent snapshot 信封 data=[{code, close, ts,...}] → close（date=ts[:8]→ISO）。
        - akshare_em fhps_detail 信封 data=[{除权除息日, dps_per_share,...}] → dps。
        - sina fin_indicator 信封 data={period: {roe_pct}} → roe。
        """
        counts = {f: 0 for f in CANONICAL_FIELDS}
        if not os.path.isdir(raw_root):
            return counts
        for src in sorted(os.listdir(raw_root)):
            field = source_map.get(src)
            if field is None:
                continue
            sdir = os.path.join(raw_root, src)
            if not os.path.isdir(sdir):
                continue
            for date_s in sorted(os.listdir(sdir)):
                ddir = os.path.join(sdir, date_s)
                if not os.path.isdir(ddir):
                    continue
                for fn in sorted(os.listdir(ddir)):
                    if not fn.endswith(".json"):
                        continue
                    env = self._read_envelope(os.path.join(ddir, fn))
                    if env is None or env.get("status") != "ok":
                        continue
                    fetched_at = str(env.get("fetched_at", ""))
                    for rec in self._derive(field, src, env.get("data")):
                        code, when, value = rec
                        if self.append(field, code, when, value, source=src,
                                       fetched_at=fetched_at or None):
                            counts[field] += 1
        return counts

    @staticmethod
    def _read_envelope(path: str) -> Optional[Dict[str, Any]]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                env = json.load(f)
            return env if isinstance(env, dict) and "data" in env else None
        except (OSError, ValueError):
            return None

    @staticmethod
    def _derive(field: str, src: str, data: Any) -> List[tuple]:
        """raw data → [(code, when, value)]。解析失败 → []（不抛）。"""
        out: List[tuple] = []
        try:
            if field == "close" and isinstance(data, list):
                for it in data:
                    code = str(it.get("code", ""))
                    close = it.get("close")
                    ts = str(it.get("ts", ""))
                    if not code or close in (None, "") or len(ts) < 8:
                        continue
                    d = f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}"
                    out.append((code, d, close))
            elif field == "dps" and isinstance(data, list):
                for it in data:
                    code = str(it.get("code", ""))
                    exd = str(it.get("ex_date", ""))
                    dps = it.get("dps_per_share")
                    if not code or not exd or dps in (None, ""):
                        continue
                    out.append((code, exd, dps))
            elif field == "roe" and isinstance(data, dict):
                for period, v in data.items():
                    roe = v.get("roe_pct") if isinstance(v, dict) else v
                    code = str(v.get("code", "")) if isinstance(v, dict) else ""
                    if not code or roe in (None, ""):
                        continue
                    out.append((code, str(period), roe))
        except Exception:  # noqa: BLE001 — 派生层解析容错（旁路）
            return []
        return out

    def stats(self) -> Dict[str, int]:
        return {f: len(self.read(f)) for f in CANONICAL_FIELDS}


# ===========================================================================
# 惰性单例（钩子用；canonical.enabled=false → None → 全部 no-op）
# ===========================================================================
_STORE: Optional[CanonicalStore] = None
_INITIALIZED = False
_FORCED_ENABLED: Optional[bool] = None


def set_enabled(flag: bool) -> None:
    """main() 显式注入启用开关（尊重 --config 路径；None=回退读仓库 config）。"""
    global _FORCED_ENABLED
    _FORCED_ENABLED = flag


def _project_root() -> str:
    # ⚠️从 screener/data/canonical.py 上溯**三级**才是项目根（同 rawstore._resolve_root）
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def canonical_store(force_init: bool = False) -> Optional[CanonicalStore]:
    """进程级惰性单例。canonical.enabled=false（或配置缺失/故障）→ None。"""
    global _STORE, _INITIALIZED
    if _STORE is not None or (_INITIALIZED and not force_init):
        return _STORE
    _INITIALIZED = True
    try:
        from .. import config as cfgmod
        root = _project_root()
        path = os.path.join(root, "config", "strategy.yaml")
        if _FORCED_ENABLED is None:
            if not os.path.exists(path):
                return None
            cfg = cfgmod.load_config(path)
            c = (cfg.get("canonical") or {})
            enabled = bool(c.get("enabled", True))
        else:
            enabled = _FORCED_ENABLED
            if not enabled:
                return None
            # 强制启用（--config 指定其他 yaml）：dir/version 仍从仓库 config 兜底读取
            cfg = cfgmod.load_config(path) if os.path.exists(path) else {}
            c = (cfg.get("canonical") or {})
        if not enabled:
            return None
        d = str(c.get("dir", "data/canonical"))
        if not os.path.isabs(d):
            d = os.path.join(root, d)
        ver = str(c.get("data_version", "v5.2-p1"))
    except Exception:  # noqa: BLE001 — 配置故障 → 关闭（回滚语义，最安全方向）
        log.warning("canonical: 配置解析失败 → canonical 层关闭（回滚语义）")
        return None
    _STORE = CanonicalStore(d, data_version=ver)
    return _STORE


def set_canonical_store(store: Optional[CanonicalStore]) -> None:
    """测试注入 / 显式关闭（传 None）。"""
    global _STORE, _INITIALIZED
    _STORE = store
    _INITIALIZED = True


def reset_canonical_store() -> None:
    global _STORE, _INITIALIZED
    _STORE = None
    _INITIALIZED = False
