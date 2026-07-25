"""
core/world_state.py — 世界状态总线（感官层的汇聚点 · 拉取式）

感官（sensors/）负责「看」，本模块负责「记住看到的」并以统一口径供出：
  - register_provider(name, fn, ttl_s)：注册一个采集器（返回 dict 的普通函数）
  - snapshot()：全部感官的最新读数（TTL 内用缓存，过期才重采——拉取式，
    无后台循环、无生命周期管理，读时自动新鲜）
  - context_block()：给 system prompt 的紧凑处境块（贾维斯的「此刻感知」）

设计取舍：
  - 拉取 + TTL 缓存，不搞后台采集循环：无启动顺序问题、无泄漏风险、
    首轮付一次采集成本后续走缓存；
  - 任一采集器失败/超预算一律跳过（感知残缺不该影响对话）；
  - 本模块进 system prompt，属提示词注入面 → PROTECTED；
    具体采集器（sensors/）只产出短值，业务性质 → OPEN 可自我迭代。
"""
from __future__ import annotations

import time
from typing import Callable, Optional

# name -> {fn, ttl, value(dict|None), fetched_at}
_PROVIDERS: dict[str, dict] = {}


def register_provider(name: str, fn: Callable[[], dict], ttl_s: float = 60.0) -> None:
    """注册感官采集器。fn 是无参同步函数，返回 {指标: 值}；抛错视为本次无读数。"""
    _PROVIDERS[name] = {"fn": fn, "ttl": ttl_s, "value": None, "fetched_at": 0.0}


def unregister_provider(name: str) -> None:
    _PROVIDERS.pop(name, None)


def _fresh(entry: dict, now: float) -> bool:
    return entry["value"] is not None and (now - entry["fetched_at"]) < entry["ttl"]


def snapshot(force: bool = False) -> dict[str, dict]:
    """全部感官读数 {感官名: {指标: 值}}。TTL 内用缓存；失败的感官跳过。"""
    now = time.monotonic()
    out: dict[str, dict] = {}
    for name, entry in _PROVIDERS.items():
        if not force and _fresh(entry, now):
            out[name] = entry["value"]
            continue
        try:
            val = entry["fn"]()
            if isinstance(val, dict) and val:
                entry["value"], entry["fetched_at"] = val, now
                out[name] = val
        except Exception:
            # 失败：有旧读数就凑合用（标注 stale），没有就跳过
            if entry["value"] is not None:
                out[name] = {**entry["value"], "stale": True}
    return out


def get(name: str) -> Optional[dict]:
    """单个感官的最新读数（走同样的 TTL 逻辑）。"""
    return snapshot().get(name)


def context_block() -> str:
    """紧凑的处境块（注入 system prompt）。无读数时返回空串（零成本降级）。"""
    snap = snapshot()
    if not snap:
        return ""
    parts = []
    for name, val in snap.items():
        kv = "，".join(f"{k}={v}" for k, v in val.items() if k != "stale")
        stale = "（旧读数）" if val.get("stale") else ""
        parts.append(f"{name}: {kv}{stale}")
    return "【当前处境（本机感官实测，供判断该不该打扰/如何应答）】\n" + "\n".join(parts)
