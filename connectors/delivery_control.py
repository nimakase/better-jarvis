"""
投递控制连接器 —— 让你用对话管理日报/潜客清单的推送与休假。

把 core.delivery（投递闸门）与 core.availability（休假偏好/确认）整合暴露为 5 个工具：
  - delivery_status           查两轨状态 + 休假区间 + 默认策略 + 临近待确认
  - delivery_pause            暂停某轨（report=日报 / prospect=潜客清单 / all=两者）
  - delivery_resume           恢复
  - delivery_rest             管理休假区间：add 登记 / clear 清除 / confirm 确认
  - delivery_vacation_default 设休假期间的默认推送策略（pause_both/ask/keep）

契约见 intel/delivery_and_resilience_spec.md。
注：轨道暂停/偏好在 core.delivery 的 delivery_state.json；休假（rest）的真源自阶段 4
起迁至内置日历（kind=rest 事件），delivery/availability 经薄壳委托日历。
本连接器只整合工具入口，不改 core 逻辑。
"""
import logging
from functools import partial

from core.registry import tool as _tool
from core import delivery
from core import availability as av

logger = logging.getLogger("jarvis.delivery")

tool = partial(_tool, group="delivery")

TRACKS = {"report": "日报", "prospect": "潜客清单"}
_BEHAVIOR_ZH = {"pause_both": "两轨都暂停", "ask": "每次问我", "keep": "照常推送"}


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
    "查询日报与潜客清单的推送全景：各轨是否暂停、已登记的休假区间、休假默认策略、"
    "以及临近且尚未确认的休假（含 id，供 delivery_rest confirm 用）。"
    "用户问“现在投递什么状态/还推不推/有没有要确认的休假”时用。",
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
    rp = delivery.list_rest_periods()   # 休假真源已迁至内置日历
    if rp:
        lines.append("休假：" + "；".join(f"{r['start']}~{r['end']}（{r.get('reason', '')}）" for r in rp))
    default = _BEHAVIOR_ZH.get(av.get_vacation_default(), av.get_vacation_default())
    lines.append(f"休假默认策略：{default}")
    pend = av.pending_confirmations()
    if pend:
        lines.append("临近待确认休假：" + "；".join(
            f"{r['start']}~{r['end']}（{r.get('reason', '')}）id={r.get('id')}" for r in pend))
    # ㉔ 回执：最近几次推送到底送达没有（此前发射即忘，无从查证）
    try:
        from core import telemetry
        receipts = telemetry.delivery_receipts(limit=5)
        if receipts:
            lines.append("最近投递回执：")
            for r in receipts:
                ok = "✓已送达" if r["delivered"] else "✗未送达"
                chans = "、".join(
                    f"{k}{'成功' if v.get('ok') else '失败:' + (v.get('error') or '')[:20]}"
                    for k, v in (r.get("channels") or {}).items())
                lines.append(f"  [{ok}] {r['track']} · {r['created_at'][:16]} · {chans}")
    except Exception:
        pass
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
    "恢复某轨道推送。track：report / prospect / all。用户说“恢复日报/接着推”时用。",
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
    "delivery_rest",
    "管理休假区间（期间默认两轨都不推）。action：add=登记新休假（需 start/end）、"
    "clear=清除全部休假登记、confirm=确认某段按默认处理（需 rest_id，来自 delivery_status）。"
    "用户说“我X到Y休假/放假”→add；“取消休假登记”→clear。",
    {
        "type": "object",
        "properties": {
            "action": {"type": "string", "description": "add | clear | confirm"},
            "start": {"type": "string", "description": "开始日期 YYYY-MM-DD（action=add 时必填）"},
            "end": {"type": "string", "description": "结束日期 YYYY-MM-DD 含当天（action=add 时必填）"},
            "reason": {"type": "string", "description": "事由，如 年假（action=add 可选）"},
            "rest_id": {"type": "string", "description": "休假区间 id（action=confirm 时必填）"},
        },
        "required": ["action"],
    },
)
async def delivery_rest(action: str, start: str = "", end: str = "",
                        reason: str = "休假", rest_id: str = "") -> str:
    a = (action or "").strip().lower()
    if a == "add":
        if not start or not end:
            return "登记休假需要 start 和 end 日期（YYYY-MM-DD）。"
        delivery.add_rest_period(start, end, reason)
        return f"已登记休假 {start}~{end}（{reason}），期间日报和潜客清单默认都不推送。"
    if a == "clear":
        delivery.clear_rest_periods()
        return "已清除全部休假登记。"
    if a == "confirm":
        if not rest_id:
            return "确认休假需要 rest_id（见 delivery_status 列出的 id）。"
        ok = av.confirm_rest(rest_id.strip())
        return "已确认，按默认处理。" if ok else f"没找到 id={rest_id} 的休假区间。"
    return "action 只能是 add / clear / confirm。"


@tool(
    "delivery_vacation_default",
    "设置休假期间的默认推送策略：pause_both=日报和潜客都暂停 / ask=每次问我 / keep=照常。"
    "用户说“以后休假就都别推/休假也照常”这类长期偏好时用（不登记具体区间，那用 delivery_rest add）。",
    {
        "type": "object",
        "properties": {"behavior": {"type": "string", "description": "pause_both | ask | keep"}},
        "required": ["behavior"],
    },
)
async def delivery_vacation_default(behavior: str) -> str:
    b = (behavior or "").strip()
    if b not in av.VALID_DEFAULTS:
        return "behavior 只能是 pause_both / ask / keep。"
    av.set_vacation_default(b)
    try:  # 冗余便利副本：写入记忆便于对话自然带出（权威值已由 set_vacation_default 落盘）
        from core import memory as mem
        mem.write("availability_vacation_default", b, source="availability")
    except Exception as e:
        logger.warning("休假默认偏好写入记忆失败（权威值已保存，仅影响对话带出）：%s", e)
    return f"好的，以后休假默认：{_BEHAVIOR_ZH[b]}。"
