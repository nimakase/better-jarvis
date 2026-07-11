"""
内置日历 —— 模型工具组（calendar 组，阶段 1）

把 core.calendar 的真源能力暴露给模型：建事件 / 读统一时间轴 / 改 / 删。
休假沿用 delivery_rest（底层将于阶段 4 改写为日历 rest 事件），此处不另起炉灶。

读类工具 agenda 任意对话都可能用（“帮我排下周”要先知道已有占用），可考虑进
CORE_TOOL_NAMES；写类工具按需披露。
"""
import json
from functools import partial

from core.registry import tool as _tool
from core import calendar as _cal

tool = partial(_tool, group="calendar")


@tool(
    "calendar_create_event",
    "在内置日历新建一个事件（日历是时间真源）。start 用 ISO 日期或日期时间（如 "
    "2026-06-30 或 2026-06-30T10:00）。重复事件传 rrule（子集：FREQ=DAILY/WEEKLY/MONTHLY/"
    "YEARLY，可加 INTERVAL、BYDAY(含 -1FR/2MO 序号)、BYMONTHDAY、BYMONTH、COUNT 或 UNTIL，"
    "如 'FREQ=WEEKLY;BYDAY=TU' 每周二）。全天事件 all_day=true。notify 传提醒设置 JSON，"
    "如 {\"type\":\"before\",\"minutes\":30} 或 {\"type\":\"at\",\"time\":\"09:00\"}。",
    {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "事件标题"},
            "start": {"type": "string", "description": "开始时间，ISO 日期或日期时间"},
            "end": {"type": "string", "description": "结束时间，可选；区间事件用"},
            "all_day": {"type": "boolean", "description": "是否全天，默认 false"},
            "rrule": {"type": "string", "description": "重复规则（子集），可选"},
            "notify": {"type": "string", "description": "提醒设置 JSON 字符串，可选"},
        },
        "required": ["title", "start"],
    },
)
async def calendar_create_event(title: str, start: str, end: str = "",
                                all_day: bool = False, rrule: str = "",
                                notify: str = "") -> str:
    notify_obj = None
    if notify and notify.strip():
        try:
            notify_obj = json.loads(notify)
        except Exception as e:
            return f"notify 不是合法 JSON：{e}"
    res = _cal.create_event(title, start, end=end or None, all_day=all_day,
                            rrule=rrule or None, notify=notify_obj)
    if not res.get("ok"):
        return f"创建失败：{res['message']}"
    ev = res["event"]
    return f"已创建事件「{ev['title']}」（id={ev['id']}，start={ev['start']}）。"


@tool(
    "calendar_agenda",
    "读内置日历的统一时间轴（表内事件展开重复 + 各来源派生条目，如证件到期/定时任务），"
    "默认未来 7 天。安排日程、回答“我接下来有什么/那天有空吗”前应先调用它了解已有占用。",
    {
        "type": "object",
        "properties": {
            "start": {"type": "string", "description": "起始（ISO），缺省=现在"},
            "end": {"type": "string", "description": "结束（ISO），缺省=start+days"},
            "days": {"type": "integer", "description": "窗口天数，默认 7"},
        },
    },
)
async def calendar_agenda(start: str = "", end: str = "", days: int = 7) -> str:
    items = _cal.agenda(start or None, end or None, days=int(days))
    if not items:
        return f"未来 {days} 天没有日程。"
    lines = []
    for e in items:
        when = str(e.get("start", ""))[:16].replace("T", " ")
        tag = {"rest": "[休假]", "reminder": "[提醒]"}.get(e.get("kind"), "")
        suffix = " (派生)" if e.get("derived") else (f" id={e['id']}" if e.get("id") else "")
        lines.append(f"- {when} {tag}{e.get('title', '')}{suffix}".replace("  ", " "))
    return "近期日程：\n" + "\n".join(lines)


@tool(
    "calendar_update_event",
    "修改内置日历中一个事件。只传要改的字段。改的是整条（含重复系列）；"
    "改单次暂不支持（删后另建一条单次事件即可）。",
    {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "事件 id"},
            "title": {"type": "string"},
            "start": {"type": "string"},
            "end": {"type": "string"},
            "all_day": {"type": "boolean"},
            "rrule": {"type": "string"},
            "notify": {"type": "string", "description": "提醒设置 JSON 字符串"},
        },
        "required": ["id"],
    },
)
async def calendar_update_event(id: str, title: str = None, start: str = None,
                                end: str = None, all_day: bool = None,
                                rrule: str = None, notify: str = None) -> str:
    fields = {}
    if title is not None:
        fields["title"] = title
    if start is not None:
        fields["start"] = start
    if end is not None:
        fields["end"] = end
    if all_day is not None:
        fields["all_day"] = all_day
    if rrule is not None:
        fields["rrule"] = rrule
    if notify is not None:
        try:
            fields["notify"] = json.loads(notify) if notify.strip() else None
        except Exception as e:
            return f"notify 不是合法 JSON：{e}"
    if not fields:
        return "没有提供要修改的字段。"
    res = _cal.update_event(id, **fields)
    return res["message"] if res.get("ok") else f"修改失败：{res['message']}"


@tool(
    "calendar_delete_event",
    "从内置日历删除一个事件（按 id）。重复事件会删除整条系列。",
    {
        "type": "object",
        "properties": {"id": {"type": "string", "description": "事件 id"}},
        "required": ["id"],
    },
)
async def calendar_delete_event(id: str) -> str:
    res = _cal.delete_event(id)
    return res["message"] if res.get("ok") else f"删除失败：{res['message']}"
