"""
作息连接器 —— 让 jarvis 用对话管理休假偏好，并主动确认临近休假。

  availability_set_default  设默认休假行为（pause_both/ask/keep），并写入记忆
  availability_pending      列出临近、尚未确认的休假 → 主动来问你
  availability_confirm      确认某段休假按默认处理

与 connectors/delivery_control（暂停/恢复/登记休假）配套。
"""
from functools import partial

from core.registry import tool as _tool
from core import availability as av

tool = partial(_tool, group="delivery")

_BEHAVIOR_ZH = {"pause_both": "两轨都暂停", "ask": "每次问我", "keep": "照常推送"}


@tool(
    "availability_set_default",
    "设置休假期间的默认推送行为：pause_both=日报和潜客都暂停 / ask=每次问我 / keep=照常。"
    "用户说“以后休假就都别推/休假也照常”时用。",
    {
        "type": "object",
        "properties": {"behavior": {"type": "string", "description": "pause_both | ask | keep"}},
        "required": ["behavior"],
    },
)
async def availability_set_default(behavior: str) -> str:
    b = behavior.strip()
    if b not in av.VALID_DEFAULTS:
        return "behavior 只能是 pause_both / ask / keep。"
    av.set_vacation_default(b)
    try:  # 写入记忆，便于以后对话自然带出
        from core import memory as mem
        mem.write("availability_vacation_default", b, source="availability")
    except Exception:
        pass
    return f"好的，以后休假默认：{_BEHAVIOR_ZH[b]}。"


@tool(
    "availability_pending",
    "列出临近（几天内开始）且尚未确认的休假区间。jarvis 应据此主动确认推送安排。",
    {
        "type": "object",
        "properties": {"lookahead_days": {"type": "integer", "description": "提前几天，默认 3"}},
        "required": [],
    },
)
async def availability_pending(lookahead_days: int = 3) -> str:
    items = av.pending_confirmations(lookahead_days=lookahead_days)
    if not items:
        return "近期没有待确认的休假。"
    default = _BEHAVIOR_ZH.get(av.get_vacation_default(), av.get_vacation_default())
    lines = [f"临近休假（默认：{default}）："]
    for rp in items:
        lines.append(f"- {rp['start']}~{rp['end']}（{rp.get('reason','')}）id={rp.get('id')}")
    lines.append("要按默认处理吗？确认用 availability_confirm，或改用 delivery_pause/resume 单独设。")
    return "\n".join(lines)


@tool(
    "availability_confirm",
    "确认某段休假按默认推送行为处理（标记 confirmed，不再提醒）。rest_id 来自 availability_pending。",
    {
        "type": "object",
        "properties": {"rest_id": {"type": "string", "description": "休假区间 id"}},
        "required": ["rest_id"],
    },
)
async def availability_confirm(rest_id: str) -> str:
    ok = av.confirm_rest(rest_id.strip())
    return "已确认，按默认处理。" if ok else f"没找到 id={rest_id} 的休假区间。"
