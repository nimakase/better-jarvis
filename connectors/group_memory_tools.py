"""
组作用域记忆写入工具：remember_group_fact / list_group_facts

对应 connectors/profile_tools.py 的 remember_fact，但记的是"只在用某个工具组时
才有意义"的业务规则，不是全局都该看到的硬事实。典型场景：跟 HubSpot 打交道时
的报价规则、飞书渠道的能力边界备注——这些塞进全局档案会污染每一轮 system
prompt，塞进这里则只在对应工具组被加载那一刻才可能被看到。

读取暂无需常驻工具：真正"哪个组加载时注入笔记"的挂钩在 core/controller.py
（PROTECTED，未接入前，这里写的笔记不会自动出现在 system prompt 里，但可以先
用 list_group_facts 手动查）。
"""

from functools import partial

from core import group_memory
from core.registry import tool as _tool

tool = partial(_tool, group="self")


@tool(
    "remember_group_fact",
    "把只在使用某个【特定工具组】时才有意义的业务规则/约定记下来（如 HubSpot 报价规则、"
    "飞书能力边界备注），跟全局档案（remember_fact）不同——这类笔记不会污染每一轮对话，"
    "只在对应工具组被用到时才可能被看到。group 传对应的工具组名（如 hubspot/lark/calendar）。"
    "不要用它存跟具体工具组无关的通用信息，那种请用 remember_fact。",
    {
        "type": "object",
        "properties": {
            "group": {"type": "string", "description": "工具组名，如 hubspot、lark、calendar"},
            "text": {"type": "string", "description": "要记住的单条业务规则，简洁陈述"},
        },
        "required": ["group", "text"],
    },
)
async def remember_group_fact(group: str, text: str) -> str:
    return group_memory.add_note(group, text)["message"]


@tool(
    "list_group_facts",
    "查看某个工具组下已经记住的专属笔记。用户问「你记了哪些跟 HubSpot/飞书有关的规则」"
    "或需要确认某组笔记是否存在时用。只读。",
    {
        "type": "object",
        "properties": {
            "group": {"type": "string", "description": "工具组名，如 hubspot、lark、calendar"},
        },
        "required": ["group"],
    },
)
async def list_group_facts(group: str) -> str:
    notes = group_memory.list_notes(group)
    if not notes:
        return f"『{group}』组暂无专属笔记。"
    lines = [f"『{group}』组笔记（{len(notes)} 条）："]
    lines += [f"  #{n['id']} {n['text']}" for n in notes]
    return "\n".join(lines)
