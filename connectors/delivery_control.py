"""
投递控制连接器 —— 让你用对话管理日报/潜客清单的推送。

把 core.delivery 暴露成 jarvis 工具：
  - delivery_status     查两轨投递状态 + 休假
  - delivery_pause      暂停某轨（report=日报 / prospect=潜客清单 / all=两者）
  - delivery_resume     恢复
  - delivery_set_rest   登记休假区间（期间默认两轨都不推）
  - delivery_clear_rest 清除休假

契约见 intel/delivery_and_resilience_spec.md。
"""
from functools import partial

from core.registry import tool as _tool
from core import delivery

tool = partial(_tool, group="delivery")

TRACKS = {"report": "日报", "prospect": "潜客清单"}


def _resolve(track: str) -> list[str]:
    t = (track or "").strip().lower()
    if t in ("all", "全部", "两轨", "both", ""):
        return ["report", "prospect"]
    if t in ("report", "日报"):
        return ["report"]
    if t in ("prospect", "潜客", "潜客清单", "清单"):
        return ["prospect"]
    return [t]


@tool(
    "delivery_status",
    "查询日报与潜客清单的推送状态（是否暂停、休假区间）。用户问“现在投递什么状态/还推不推”时用。",
    {"type": "object", "properties": {}, "required": []},
)
async def delivery_status() -> str:
    s = delivery.all_status()
    lines = []
    for t, zh in TRACKS.items():
        st = s["tracks"].get(t, {"status": "active"})
        if st.get("status") == "paused":
            until = f"至 {st['paused_until']}" if st.get("paused_until") else "（无限期）"
            lines.append(f"{zh}：已暂停{until} {st.get('reason', '')}".rstrip())
        else:
            lines.append(f"{zh}：正常推送")
    rp = s.get("rest_periods", [])
    if rp:
        lines.append("休假：" + "；".join(f"{r['start']}~{r['end']}（{r.get('reason', '')}）" for r in rp))
    return "\n".join(lines)


@tool(
    "delivery_pause",
    "暂停某轨道的推送。track：report=日报 / prospect=潜客清单 / all=两者。"
    "until=YYYY-MM-DD（到期自动恢复），留空=无限期。用户说“日报停3天/潜客清单先别推”时用，自己算好日期。",
    {
        "type": "object",
        "properties": {
            "track": {"type": "string", "description": "report | prospect | all"},
            "until": {"type": "string", "description": "恢复日期 YYYY-MM-DD，留空=无限期"},
            "reason": {"type": "string", "description": "暂停原因（可选）"},
        },
        "required": ["track"],
    },
)
async def delivery_pause(track: str, until: str = "", reason: str = "") -> str:
    ts = _resolve(track)
    for t in ts:
        delivery.pause_track(t, until or None, reason)
    names = "、".join(TRACKS.get(t, t) for t in ts)
    tail = f"，{until} 到期自动恢复" if until else "（无限期，恢复前一直停）"
    return f"已暂停【{names}】推送{tail}。"


@tool(
    "delivery_resume",
    "恢复某轨道推送。track：report / prospect / all。",
    {
        "type": "object",
        "properties": {"track": {"type": "string", "description": "report | prospect | all"}},
        "required": ["track"],
    },
)
async def delivery_resume(track: str) -> str:
    ts = _resolve(track)
    for t in ts:
        delivery.resume_track(t)
    return f"已恢复【{'、'.join(TRACKS.get(t, t) for t in ts)}】推送。"


@tool(
    "delivery_set_rest",
    "登记一段休假，期间默认日报和潜客清单都不推送。用户说“我X到Y休假/放假”时用。",
    {
        "type": "object",
        "properties": {
            "start": {"type": "string", "description": "开始日期 YYYY-MM-DD"},
            "end": {"type": "string", "description": "结束日期 YYYY-MM-DD（含当天）"},
            "reason": {"type": "string", "description": "事由，如 年假"},
        },
        "required": ["start", "end"],
    },
)
async def delivery_set_rest(start: str, end: str, reason: str = "休假") -> str:
    delivery.add_rest_period(start, end, reason)
    return f"已登记休假 {start}~{end}（{reason}），期间日报和潜客清单默认都不推送。"


@tool(
    "delivery_clear_rest",
    "清除所有已登记的休假区间。",
    {"type": "object", "properties": {}, "required": []},
)
async def delivery_clear_rest() -> str:
    delivery.clear_rest_periods()
    return "已清除全部休假登记。"
