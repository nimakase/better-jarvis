"""
投递服务（Delivery service）—— 跨任务复用的横切层。

职责：
  - 多渠道：feishu / file / email / webpush（发送函数可注册/注入）
  - 闸门：按「轨道暂停态 + 休假区间」决定今天是否投递
  - 分级路由：按严重度把内容送到不同渠道

设计契约见 intel/delivery_and_resilience_spec.md。
新增层，纯标准库；现有 scheduler 的 feishu/file 行为可平移到这里，老路径不受影响。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Callable, Optional

try:
    import config
    _STATE_PATH = config.DATA_DIR / "delivery_state.json"
except Exception:
    _STATE_PATH = Path(__file__).resolve().parent / "delivery_state.json"

# 严重度 → 默认渠道（可被 deliver() 的 routing 参数覆盖）
SEVERITY_CHANNELS = {
    "normal": ["webpush"],            # 日常交付（日报就绪、清单就绪）
    "high":   ["webpush", "feishu"],  # 高级别（登录过期、改版、配置错）
}

ChannelFn = Callable[[str, str], object]  # (title, content) -> 任意


# ────────────────────────── 状态读写 ──────────────────────────

def _default_state() -> dict:
    return {"tracks": {}, "rest_periods": []}


def _load(state_path: str | Path) -> dict:
    p = Path(state_path)
    if not p.exists():
        return _default_state()
    try:
        s = json.loads(p.read_text(encoding="utf-8"))
        s.setdefault("tracks", {})
        s.setdefault("rest_periods", [])
        return s
    except Exception:
        return _default_state()


def _save(state: dict, state_path: str | Path) -> None:
    Path(state_path).write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ────────────────────────── 轨道暂停/恢复 ──────────────────────────

def pause_track(track: str, until: Optional[str] = None, reason: str = "",
                state_path: str | Path = _STATE_PATH) -> dict:
    """暂停某轨道；until=YYYY-MM-DD（含）到期自动恢复，None=无限期。"""
    s = _load(state_path)
    s["tracks"][track] = {"status": "paused", "paused_until": until, "reason": reason}
    _save(s, state_path)
    return s["tracks"][track]


def resume_track(track: str, state_path: str | Path = _STATE_PATH) -> dict:
    s = _load(state_path)
    s["tracks"][track] = {"status": "active", "paused_until": None, "reason": ""}
    _save(s, state_path)
    return s["tracks"][track]


def track_status(track: str, state_path: str | Path = _STATE_PATH) -> dict:
    return _load(state_path)["tracks"].get(track, {"status": "active", "paused_until": None, "reason": ""})


def all_status(state_path: str | Path = _STATE_PATH) -> dict:
    return _load(state_path)


# ────────────────────────── 休假区间 ──────────────────────────

def add_rest_period(start: str, end: str, reason: str = "休假",
                    state_path: str | Path = _STATE_PATH) -> list:
    """登记一段休假（含首尾），期间默认两轨都不投递。

    带 id + confirmed 字段：confirmed 仅供「主动确认」用，不影响闸门（闸门只看 start/end）。
    """
    import uuid
    s = _load(state_path)
    s["rest_periods"].append({"id": uuid.uuid4().hex[:8], "start": start, "end": end,
                              "reason": reason, "confirmed": False})
    _save(s, state_path)
    return s["rest_periods"]


def clear_rest_periods(state_path: str | Path = _STATE_PATH) -> None:
    s = _load(state_path)
    s["rest_periods"] = []
    _save(s, state_path)


# ────────────────────────── 闸门 ──────────────────────────

def is_paused(track: str, as_of: Optional[str] = None,
              state_path: str | Path = _STATE_PATH) -> tuple[bool, str]:
    """今天该轨道是否被闸门拦住。返回 (是否暂停, 原因)。"""
    today = date.fromisoformat(as_of) if as_of else date.today()
    s = _load(state_path)

    # 休假区间（对所有轨道生效）
    for rp in s.get("rest_periods", []):
        try:
            if date.fromisoformat(rp["start"]) <= today <= date.fromisoformat(rp["end"]):
                return True, f"休假中（{rp.get('reason','')}）"
        except Exception:
            continue

    # 单轨暂停
    t = s["tracks"].get(track)
    if t and t.get("status") == "paused":
        until = t.get("paused_until")
        if until is None:
            return True, t.get("reason") or "已暂停"
        try:
            if today <= date.fromisoformat(until):
                return True, t.get("reason") or f"暂停至 {until}"
        except Exception:
            return True, t.get("reason") or "已暂停"
        # 到期：自动恢复
        resume_track(track, state_path)
    return False, ""


# ────────────────────────── 渠道注册 ──────────────────────────

_CHANNELS: dict[str, ChannelFn] = {}


def register_channel(name: str, fn: ChannelFn) -> None:
    """生产侧注册真实发送函数（feishu / webpush / file / email）。"""
    _CHANNELS[name] = fn


# ────────────────────────── 投递主入口 ──────────────────────────

def deliver(track: str, title: str, content: str, severity: str = "normal",
            routing: Optional[dict] = None, channels: Optional[dict] = None,
            as_of: Optional[str] = None, state_path: str | Path = _STATE_PATH) -> dict:
    """过闸门 → 按严重度路由 → 调各渠道发送。

    channels: {name: fn} 覆盖默认注册表（测试可注入 mock）。
    routing:  {severity: [channel...]} 覆盖默认 SEVERITY_CHANNELS。
    """
    paused, reason = is_paused(track, as_of=as_of, state_path=state_path)
    if paused:
        return {"delivered": False, "reason": reason, "track": track}

    table = routing or SEVERITY_CHANNELS
    targets = table.get(severity, ["webpush"])
    ch = channels if channels is not None else _CHANNELS

    sent, missing = {}, []
    for name in targets:
        fn = ch.get(name)
        if fn is None:
            missing.append(name)
            continue
        try:
            sent[name] = {"ok": True, "result": fn(title, content)}
        except Exception as exc:
            sent[name] = {"ok": False, "error": str(exc)}
    return {"delivered": True, "track": track, "severity": severity,
            "sent": sent, "missing_channels": missing}
