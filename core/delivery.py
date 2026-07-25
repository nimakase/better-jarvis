"""
投递服务（Delivery service）—— 跨任务复用的横切层。

职责：
  - 多渠道：file / email / webpush（发送函数可注册/注入）
  - 闸门：按「轨道暂停态 + 休假区间」决定今天是否投递
  - 分级路由：按严重度把内容送到不同渠道

设计契约见 intel/delivery_and_resilience_spec.md。
新增层，纯标准库；现有 scheduler 的 file 行为可平移到这里，老路径不受影响。
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

# 严重度 → 默认渠道（可被 deliver() 的 routing 参数覆盖）。
# lark 未注册时（没配凭据）只是记进 missing_channels，不报错——webpush 照发。
SEVERITY_CHANNELS = {
    "normal": ["webpush", "lark"],  # 日常交付（日报就绪、清单就绪）
    "high":   ["webpush", "lark"],  # 高级别（登录过期、改版、配置错）
}

# 渠道函数两种形态并存：
#   老式 (title, content)                    —— 只发文字（webpush）
#   新式 (title, content, attachments=None)  —— 还能带文件（lark）
# deliver() 按签名自动适配，老渠道零改动。
ChannelFn = Callable[..., object]


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

# 阶段 4 起，休假的真源迁到内置日历（kind=rest 事件）；以下函数改为薄壳委托日历，
# 签名不变（state_path 保留兼容，对休假已无意义）。delivery_state.json 的 rest_periods
# 仅作迁移前的历史载体，迁移后置空（见 migrate_rest_to_calendar）。

def add_rest_period(start: str, end: str, reason: str = "休假",
                    state_path: str | Path = _STATE_PATH) -> list:
    """登记一段休假（含首尾），期间默认两轨都不投递。底层＝日历 rest 事件。"""
    from core import calendar as _cal
    _cal.add_rest(start, end, reason)
    return _cal.list_rests()


def clear_rest_periods(state_path: str | Path = _STATE_PATH) -> None:
    from core import calendar as _cal
    _cal.clear_rests()


def list_rest_periods(state_path: str | Path = _STATE_PATH) -> list:
    """列出休假（来自日历），形状对齐旧 rest_periods。"""
    from core import calendar as _cal
    return _cal.list_rests()


def migrate_rest_to_calendar(state_path: str | Path = _STATE_PATH) -> int:
    """一次性迁移：把 delivery_state.json 的旧 rest_periods 搬成日历 rest 事件，
    随后置空旧字段（幂等：迁移后源为空，再调用为 no-op）。返回迁移条数。"""
    from core import calendar as _cal
    s = _load(state_path)
    old = s.get("rest_periods") or []
    if not old:
        return 0
    existing = {(r["start"], r["end"]) for r in _cal.list_rests()}
    moved = 0
    for rp in old:
        key = (str(rp.get("start"))[:10], str(rp.get("end"))[:10])
        if key in existing:
            continue  # 去重，防重复迁移
        ev = _cal.add_rest(rp.get("start"), rp.get("end"), rp.get("reason", "休假"))
        if rp.get("confirmed") and ev.get("event"):
            _cal.confirm_rest(ev["event"]["id"])
        moved += 1
    s["rest_periods"] = []  # 置空旧字段，真源已转移到日历
    _save(s, state_path)
    return moved


# ────────────────────────── 闸门 ──────────────────────────

def is_paused(track: str, as_of: Optional[str] = None,
              state_path: str | Path = _STATE_PATH) -> tuple[bool, str]:
    """今天该轨道是否被闸门拦住。返回 (是否暂停, 原因)。"""
    today = date.fromisoformat(as_of) if as_of else date.today()
    s = _load(state_path)

    # 休假区间（对所有轨道生效）——真源在内置日历（kind=rest 事件）
    from core import calendar as _cal
    rp = _cal.active_rest(today.isoformat())
    if rp:
        return True, f"休假中（{rp.get('reason', '')}）"

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
    """生产侧注册真实发送函数（webpush / file / email）。"""
    _CHANNELS[name] = fn


# ────────────────────────── 投递主入口 ──────────────────────────

def _accepts_attachments(fn) -> bool:
    """渠道函数是否接收第三个 attachments 参数（新式渠道）。判不出来按老式算。"""
    import inspect
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    if "attachments" in params:
        return True
    positional = [p for p in params.values()
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    return len(positional) >= 3


def deliver(track: str, title: str, content: str, severity: str = "normal",
            routing: Optional[dict] = None, channels: Optional[dict] = None,
            attachments: Optional[list] = None,
            as_of: Optional[str] = None, state_path: str | Path = _STATE_PATH) -> dict:
    """过闸门 → 按严重度路由 → 调各渠道发送。

    channels:    {name: fn} 覆盖默认注册表（测试可注入 mock）。
    routing:     {severity: [channel...]} 覆盖默认 SEVERITY_CHANNELS。
    attachments: 产出文件路径列表（如潜客 xlsx）。只有新式渠道（签名带
                 attachments，如飞书）会收到并真实发文件；老式渠道（webpush）
                 只发文字，附件对它们不可见——定时任务的文件因此能主动到飞书。
    """
    paused, reason = is_paused(track, as_of=as_of, state_path=state_path)
    if paused:
        _record_receipt(track, title, False, {"_paused": {"ok": False, "error": reason}})
        return {"delivered": False, "reason": reason, "track": track}

    table = routing or SEVERITY_CHANNELS
    targets = table.get(severity, ["webpush"])
    ch = channels if channels is not None else _CHANNELS

    sent, missing = {}, []
    for name in targets:
        fn = ch.get(name)
        if fn is None:
            missing.append(name)
            sent[name] = {"ok": False, "error": "渠道未注册"}
            continue
        try:
            if attachments and _accepts_attachments(fn):
                result = fn(title, content, attachments)
            else:
                result = fn(title, content)
            sent[name] = {"ok": True, "result": result}
        except Exception as exc:
            sent[name] = {"ok": False, "error": str(exc)}
    # ㉔ 诚实的 delivered：至少一个渠道真的发出去了才算送达（此前恒 True，
    # 掩盖了「渠道全没注册/全失败」的静默失败）。回执落盘，可查、可被健康感官发现。
    delivered = any(v.get("ok") for v in sent.values())
    _record_receipt(track, title, delivered, sent)
    return {"delivered": delivered, "track": track, "severity": severity,
            "sent": sent, "missing_channels": missing}


def _record_receipt(track: str, title: str, delivered: bool, channels: dict) -> None:
    try:
        from core import telemetry
        telemetry.record_delivery(track, title, delivered, channels)
    except Exception:
        pass
