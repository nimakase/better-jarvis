"""
作息 / 可用性（Availability）—— 让 jarvis 主动管理"什么时候推、什么时候问"。

在投递闸门（core.delivery）之上加一层"主动确认"：
  - 默认休假行为偏好（pause_both / ask / keep），持久化
  - 休假区间的 confirmed 标志 + pending_confirmations(临近未确认)
    供调度巡检/对话时主动问你："下周休假，日报和潜客都暂停对吗？"

confirmed 不影响闸门——闸门只看 start/end（见 delivery.is_paused）。
偏好同时由 connector 写入 jarvis 记忆，便于对话时自然带出。
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from core import delivery as d

VALID_DEFAULTS = {"pause_both", "ask", "keep"}


# ────────────────────────── 默认休假行为偏好 ──────────────────────────

def set_vacation_default(behavior: str, state_path: str | Path = d._STATE_PATH) -> str:
    s = d._load(state_path)
    s.setdefault("prefs", {})["vacation_default"] = behavior
    d._save(s, state_path)
    return behavior


def get_vacation_default(state_path: str | Path = d._STATE_PATH) -> str:
    return d._load(state_path).get("prefs", {}).get("vacation_default", "pause_both")


# ────────────────────────── 休假确认 ──────────────────────────

# 阶段 4 起，休假真源在内置日历（kind=rest 事件）；以下三函数委托日历。
# confirmed 标志存日历事件 meta，不影响闸门（闸门只看 start/end，见 calendar.active_rest）。

def list_rest(state_path: str | Path = d._STATE_PATH) -> list[dict]:
    from core import calendar as _cal
    return _cal.list_rests()


def confirm_rest(rest_id: str, state_path: str | Path = d._STATE_PATH) -> bool:
    from core import calendar as _cal
    return _cal.confirm_rest(rest_id)


def pending_confirmations(as_of: Optional[str] = None, lookahead_days: int = 3,
                          state_path: str | Path = d._STATE_PATH) -> list[dict]:
    """临近（lookahead_days 内开始）且尚未确认的休假 → jarvis 该主动来问的。"""
    from core import calendar as _cal
    today = date.fromisoformat(as_of) if as_of else date.today()
    out = []
    for rp in _cal.list_rests():
        if rp.get("confirmed"):
            continue
        try:
            start = date.fromisoformat(rp["start"])
        except Exception:
            continue
        if today <= start <= today + timedelta(days=lookahead_days):
            out.append(rp)
    return out
