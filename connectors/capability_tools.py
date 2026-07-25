"""
connectors/capability_tools.py — 能力索引的对话入口（core/capability）

  - search_capability：按意图找已有能力（造工具前查重、回答「你能不能做 X」）
  - capability_overview：全量能力总览（回答「你能干什么」）
"""
from functools import partial

from core import capability
from core import effects
from core.registry import tool as _tool

tool = partial(_tool, group="self")


@tool(
    "search_capability",
    "按意图检索贾维斯【已有】的能力（工具/技能/工作流/报告类型的统一索引）。"
    "两个场景必用：① 用户问「你能不能做 X / 有没有办法 Y」时先查这里再回答，"
    "不要凭记忆说做不到；② 想造新工具之前先查这里，若已有相近能力则复用/扩展，"
    "不要重复造轮子。只读。",
    {
        "type": "object",
        "properties": {
            "intent": {"type": "string", "description": "要做的事，用一句话描述"},
        },
        "required": ["intent"],
    },
    effect=effects.READ_LOCAL,
)
async def search_capability(intent: str) -> str:
    hits = capability.search(intent, top_k=5)
    if not hits:
        from core import telemetry
        telemetry.log_gap(intent, context="search_capability 未命中")
        return ("没有找到相近的已有能力（已记入能力缺口日志）。"
                "若确实需要，可考虑组合现有工具，或用 create_tool 新建。")
    lines = ["找到这些相近的已有能力（优先复用/扩展，其次组合，最后才新建）："]
    for h in hits:
        lines.append(f"  [{h['kind']}] {h['name']}（相关度 {h['score']:.0%}）："
                     f"{(h['description'] or '')[:100]}")
    return "\n".join(lines)


@tool(
    "capability_overview",
    "贾维斯全量能力总览：所有已注册工具、技能（含草稿/试用）、工作流、报告类型的"
    "分类清单。用户问「你都能干什么 / 你有哪些工具」时用。只读。",
    {"type": "object", "properties": {}},
    effect=effects.READ_LOCAL,
)
async def capability_overview() -> str:
    return capability.overview()
