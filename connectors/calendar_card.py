"""情报台「近期日程」卡（阶段 6）。

注册一张 monitor 卡，一眼看未来 7 天将到事项（含表内事件 + 派生到期/定时）。
对标设计文件 §7：intel_cards.register_card，与日历抽屉面板互补。
"""
from __future__ import annotations

from core import intel_cards
from core import calendar as _cal

_TAGS = {"rest": "休假", "reminder": "提醒", "expiry": "到期",
         "task": "定时", "key_date": "关键日"}
_TONES = {"expiry": "red", "rest": "green", "reminder": "yellow"}


async def _agenda_card() -> dict:
    items = _cal.agenda(days=7)[:8]
    if not items:
        return {"has_content": False}
    rows = []
    for e in items:
        when = str(e.get("start", ""))[:16].replace("T", " ")
        if e.get("all_day"):
            when = str(e.get("start", ""))[:10] + " 全天"
        row = {"text": f"{when}　{e.get('title', '')}"}
        kind = e.get("kind")
        if kind in _TAGS:
            row["badge"] = _TAGS[kind]
        if kind in _TONES:
            row["tone"] = _TONES[kind]
        rows.append(row)
    return {"has_content": True, "items": rows, "note": "未来 7 天"}


intel_cards.register_card("calendar_upcoming", "近期日程", "monitor", _agenda_card, order=20)
