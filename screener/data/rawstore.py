# -*- coding: utf-8 -*-
"""v5.2 Phase 1：append-only 原始响应层（报告 §3 选项 C）。

设计（TL 拍板 Q2=选项 C）：
- 目录结构 ``data/raw/{source}/{date}/*.json``；每次成功的外部调用把**原始响应**
  （零转换，仅包一层元数据信封）落盘一次。raw 层即"审计日志"——历史可追溯从
  v5.2 上线日起（Q4：只增量、不做全史回填）。
- **文件级幂等**：同一 (source, endpoint, date) 的重复写入直接跳过不重写
  （append-only 语义下"当日同接口"视为同一响应批次；同日重跑零额外 IO）。
- **原子写**：复用 cache.py 的临时文件 + os.replace 模式，避免半截文件。
- **回滚点**（brief 硬约束）：``canonical.enabled=false`` 时 :func:`raw_store`
  返回 None → 所有钩子 no-op，主路径行为逐字节不变；删 ``data/raw``、
  ``data/canonical`` 目录后系统回到 v5.1 状态。

零网络：本模块纯本地 IO。写失败只告警不抛（raw 层是旁路审计，不得炸主路径）。
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

log = logging.getLogger("screener.data.rawstore")


def _safe(name: str) -> str:
    """目录/文件名安全化（只留 alnum 与 ._-，其余替换为 _）。"""
    return "".join(c if (c.isalnum() or c in "._-") else "_" for c in name)


def now_iso() -> str:
    """UTC ISO8601 时间戳（fetched_at 字段口径）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class RawStore:
    """append-only 原始响应存储。目录: {root}/{source}/{date}/{endpoint}.{seq}.json"""

    def __init__(self, root: str) -> None:
        self.root = root
        os.makedirs(root, exist_ok=True)
        # 幂等键 (source, endpoint, date) → 已写文件路径（进程内记忆；
        # 跨进程靠磁盘上同 key 文件存在性判断，见 _existing）
        self._written: Dict[tuple, str] = {}
        self.write_count = 0      # 实际落盘次数（统计/测试用）
        self.skip_count = 0       # 幂等跳过次数

    # ---------- 内部 ----------
    def _day_dir(self, source: str, date_s: str) -> str:
        d = os.path.join(self.root, _safe(source), _safe(date_s))
        os.makedirs(d, exist_ok=True)
        return d

    def _existing(self, source: str, endpoint: str, date_s: str) -> Optional[str]:
        """当日同 (source, endpoint) 是否已有落盘文件（幂等判断，跨进程有效）。"""
        key = (source, endpoint, date_s)
        if key in self._written:
            return self._written[key]
        d = os.path.join(self.root, _safe(source), _safe(date_s))
        if os.path.isdir(d):
            prefix = _safe(endpoint) + "."
            for fn in sorted(os.listdir(d)):
                if fn.startswith(prefix) and fn.endswith(".json"):
                    self._written[key] = os.path.join(d, fn)
                    return self._written[key]
        return None

    def _atomic_write_json(self, path: str, obj: Dict[str, Any]) -> None:
        tmp = f"{path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, path)

    # ---------- 公共 API ----------
    def record_json(
        self,
        source: str,
        endpoint: str,
        data: Any,
        date_s: Optional[str] = None,
        rows: Optional[int] = None,
        status: str = "ok",
        meta: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """落盘一条原始响应（JSON）。幂等：当日同 (source, endpoint) 只写一次。

        :param data: 原始响应体（零转换；list/dict/str 均可）。
        :param date_s: 归属交易日 YYYY-MM-DD（默认今天 UTC）。
        :param rows: 行数提示（None=自动推断 list 长度）。
        :return: 写入文件路径；幂等跳过 → 已有文件路径；失败 → None。
        """
        date_s = date_s or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        existing = self._existing(source, endpoint, date_s)
        if existing is not None:
            self.skip_count += 1
            return existing
        if rows is None and isinstance(data, (list, tuple)):
            rows = len(data)
        env = {
            "source": source,
            "endpoint": endpoint,
            "fetched_at": now_iso(),
            "rows": rows,
            "status": status,
            "meta": meta or {},
            "data": data,
        }
        d = self._day_dir(source, date_s)
        path = os.path.join(d, f"{_safe(endpoint)}.0.json")
        try:
            self._atomic_write_json(path, env)
        except OSError as exc:
            log.warning("raw 落盘失败 %s/%s: %s（旁路审计，不阻塞主路径）", source, endpoint, exc)
            return None
        self._written[(source, endpoint, date_s)] = path
        self.write_count += 1
        return path

    def record_jsonl(
        self,
        source: str,
        endpoint: str,
        records: Sequence[Dict[str, Any]],
        date_s: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """落盘一组记录（JSONL 信封：envelope + data=[...]）。用于批量快照。"""
        return self.record_json(
            source, endpoint, list(records), date_s=date_s, rows=len(records),
            status="ok", meta=meta,
        )

    def read_envelope(self, path: str) -> Optional[Dict[str, Any]]:
        """读回信封；损坏/非本程序文件 → None。"""
        try:
            with open(path, "r", encoding="utf-8") as f:
                env = json.load(f)
            if not isinstance(env, dict) or "data" not in env:
                return None
            return env
        except (OSError, ValueError):
            return None

    def list_files(self, source: Optional[str] = None, date_s: Optional[str] = None) -> List[str]:
        """列出已落盘文件（可按 source/date 过滤），升序。目录结构 {source}/{date}/。"""
        out: List[str] = []

        def _scan_date_dir(ddir: str) -> None:
            if not os.path.isdir(ddir):
                return
            for fn in sorted(os.listdir(ddir)):
                if fn.endswith(".json") and ".tmp." not in fn:
                    out.append(os.path.join(ddir, fn))

        if source is not None:
            # 指定 source → 第二层直接是日期目录
            sdir = os.path.join(self.root, _safe(source))
            if date_s is not None:
                _scan_date_dir(os.path.join(sdir, _safe(date_s)))
                return out
            if os.path.isdir(sdir):
                for ds in sorted(os.listdir(sdir)):
                    _scan_date_dir(os.path.join(sdir, ds))
            return out
        # 未指定 source → 遍历 {source}/{date}/
        if not os.path.isdir(self.root):
            return out
        for src in sorted(os.listdir(self.root)):
            sdir = os.path.join(self.root, src)
            if not os.path.isdir(sdir):
                continue
            date_dirs = (
                [d for d in sorted(os.listdir(sdir)) if os.path.isdir(os.path.join(sdir, d))]
                if date_s is None
                else ([_safe(date_s)] if os.path.isdir(os.path.join(sdir, _safe(date_s))) else [])
            )
            for ds in date_dirs:
                _scan_date_dir(os.path.join(sdir, ds))
        return out

    def history_nonempty(self, source: str, endpoint: str, before_date: str) -> bool:
        """EmptyPayloadGuard 用：该源同类接口在 before_date 之前是否曾有非空响应。"""
        for path in self.list_files(source=source):
            ds = os.path.basename(os.path.dirname(path))
            if ds >= before_date:
                continue
            env = self.read_envelope(path)
            if env is None:
                continue
            if str(env.get("endpoint", "")) != endpoint:
                continue
            if (env.get("rows") or 0) > 0:
                return True
        return False

    def stats(self) -> Dict[str, int]:
        n = len(self.list_files())
        return {"files": n, "writes": self.write_count, "skipped": self.skip_count}


# ===========================================================================
# 惰性单例（钩子用；canonical.enabled=false → None → 全部 no-op）
# ===========================================================================
_STORE: Optional[RawStore] = None
_INITIALIZED = False
_FORCED_ENABLED: Optional[bool] = None


def set_enabled(flag: Optional[bool]) -> None:
    """main() 显式注入启用开关（尊重 --config 路径；None=回退读仓库 config）。"""
    global _FORCED_ENABLED
    _FORCED_ENABLED = flag


def _resolve_root() -> str:
    """raw 根目录：<项目根>/data/raw（与 cache_dir 同级的独立目录，回滚=整删）。

    ⚠️从 screener/data/rawstore.py 上溯**三级**才是项目根（screener/data → screener → 根）；
    只上溯两级会落到 screener/ 下（误写 screener/data/raw——源码目录内，git 不忽略）。
    """
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(root, "data", "raw")


def _enabled_from_cfg() -> bool:
    if _FORCED_ENABLED is not None:
        return _FORCED_ENABLED
    try:
        from .. import config as cfgmod
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        path = os.path.join(root, "config", "strategy.yaml")
        if not os.path.exists(path):
            return False
        cfg = cfgmod.load_config(path)
        c = (cfg.get("canonical") or {})
        return bool(c.get("enabled", True))
    except Exception:  # noqa: BLE001 — 配置故障 → 关闭 raw 层（回滚语义，最安全方向）
        log.warning("rawstore: canonical 配置解析失败 → raw 层关闭（回滚语义）")
        return False


def raw_store(force_init: bool = False) -> Optional[RawStore]:
    """进程级惰性单例。canonical.enabled=false（或配置缺失/故障）→ None。"""
    global _STORE, _INITIALIZED
    if _STORE is not None or (_INITIALIZED and not force_init):
        return _STORE
    _INITIALIZED = True
    if not _enabled_from_cfg():
        return None
    _STORE = RawStore(_resolve_root())
    return _STORE


def set_raw_store(store: Optional[RawStore]) -> None:
    """测试注入 / 显式关闭（传 None）。"""
    global _STORE, _INITIALIZED
    _STORE = store
    _INITIALIZED = True


def reset_raw_store() -> None:
    """测试用：清空单例状态（含 _FORCED_ENABLED——恢复"未注入"初始态，防跨测试泄漏）。"""
    global _STORE, _INITIALIZED, _FORCED_ENABLED
    _STORE = None
    _INITIALIZED = False
    _FORCED_ENABLED = None


def record_response(
    source: str,
    endpoint: str,
    data: Any,
    date_s: Optional[str] = None,
    rows: Optional[int] = None,
    status: str = "ok",
    meta: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """钩子入口：raw 层启用则落盘，否则 no-op。任何异常都不外抛（旁路纪律）。"""
    try:
        store = raw_store()
        if store is None:
            return None
        return store.record_json(
            source, endpoint, data, date_s=date_s, rows=rows, status=status, meta=meta
        )
    except Exception as exc:  # noqa: BLE001 — 旁路审计绝不炸主路径
        log.warning("raw 落盘钩子异常 %s/%s: %s", source, endpoint, exc)
        return None


def record_response_failed(
    source: str, endpoint: str, date_s: Optional[str] = None, err: str = ""
) -> None:
    """失败调用登记（status=error，data=null）——健康度成功率分母需要失败样本。

    幂等语义与成功路径相同：当日同 (source, endpoint) 已有文件则不覆盖
    （避免"先成功后失败"把成功留样冲掉；失败计数由 HealthTracker 另行维护）。
    """
    try:
        store = raw_store()
        if store is None:
            return
        store.record_json(
            source, endpoint, None, date_s=date_s, rows=0, status="error",
            meta={"error": err},
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("raw 失败登记钩子异常 %s/%s: %s", source, endpoint, exc)
