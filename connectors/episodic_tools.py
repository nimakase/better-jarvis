"""
情节记忆（L4）工具：recall / remember_episode

recall：带着问题从情节记忆里语义召回过往聊过/发生过的内容。平时不常驻，用到才捞。
remember_episode：模型判断某段经过值得长期记住时主动存（写个人长期记忆，
                  后台/工作流实例被禁用，见 controller.BACKGROUND_BLOCKED_TOOLS）。

长对话压缩产生的「早期对话摘要」由 controller._compress_history 自动写入，
无需模型操心——这补上了原先"摘要用完即弃"的缺口。
"""
from functools import partial

from core import episodic
from core.registry import tool as _tool

tool = partial(_tool, group="memory")


@tool(
    "recall",
    "从【情节记忆】里按语义召回过往聊过/发生过的相关内容（对话摘要、过往结论、来龙去脉等）。"
    "当用户提到'上次''之前''我们聊过'，或你需要历史背景才能答好时使用。返回最相关的几条及其相关度。",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "要回忆的主题/问题"},
            "k": {"type": "integer", "description": "返回条数，默认 5"},
        },
        "required": ["query"],
    },
)
async def recall(query: str, k: int = 5) -> str:
    hits = episodic.recall(query, k=k or 5)
    if not hits:
        return "情节记忆里没有相关内容。"
    lines = []
    for h in hits:
        when = (h.get("created_at") or "")[:10]
        lines.append(f"[{when} · 相关度{h['score']}] {h['text']}")
    return "\n".join(lines)


@tool(
    "remember_episode",
    "把一段值得长期记住的【经过/结论】存入情节记忆（自由文本，之后可被 recall 语义召回）。"
    "适合'本次讨论的结论''某事的来龙去脉'。区分：用户的稳定偏好用 remember_fact，"
    "具体对象的事实用 save_entity。",
    {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "要记住的经过/结论，简洁但信息完整"},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["text"],
    },
)
async def remember_episode(text: str, tags: list = None) -> str:
    return episodic.save(text, tags=tags, source="model")["message"]
