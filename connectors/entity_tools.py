"""
实体记忆（L2）工具：save_entity / lookup_entity / search_entities / list_entities

让模型按需精确查询/记录「客户、供应商、料号、报价单」等具体对象的事实——
这些【不常驻】 system prompt，用到才捞，因此必须做成工具由模型主动调。

与 remember_fact 的分工（写在各工具说明里，帮小模型路由）：
  - 关于【用户本人】的稳定偏好/事实      → remember_fact（写 L1 用户档案）
  - 关于【某个具体对象】的事实            → save_entity（写 L2 实体记忆）
  - 一段【经过/结论】的自由文本           → remember_episode（写 L4 情节记忆）

写工具 save_entity 属于「写个人长期记忆」，后台/工作流实例被禁用
（见 controller.BACKGROUND_BLOCKED_TOOLS），避免自动任务污染记忆。
"""
from functools import partial

from core import entities
from core.registry import tool as _tool

tool = partial(_tool, group="memory")


def _fmt(d: dict) -> str:
    parts = [f"{d['kind']}/{d['name']}"]
    if d.get("fields"):
        parts.append("；".join(f"{k}={v}" for k, v in d["fields"].items()))
    if d.get("notes"):
        parts.append(f"备注：{d['notes']}")
    if d.get("tags"):
        parts.append(f"标签：{','.join(d['tags'])}")
    return " | ".join(parts)


@tool(
    "save_entity",
    "记录/更新一个【具体对象】的长期事实（客户、供应商、料号、报价单等）。"
    "fields 传结构化键值（如账期、地区、联系人、单价）。同 kind+name 会合并更新，不覆盖旧字段。"
    "适合跨会话要精确记住的对象事实；关于用户本人的偏好请改用 remember_fact。",
    {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "description": "对象类型，如 client/supplier/part/quote"},
            "name": {"type": "string", "description": "主名/唯一标识，如公司名或料号"},
            "fields": {"type": "object", "description": "结构化字段键值对，如 {\"账期\":\"NET30\",\"地区\":\"德国\"}"},
            "notes": {"type": "string", "description": "自由备注（可选）"},
            "tags": {"type": "array", "items": {"type": "string"}, "description": "标签（可选）"},
        },
        "required": ["kind", "name"],
    },
)
async def save_entity(kind: str, name: str, fields: dict = None,
                      notes: str = "", tags: list = None) -> str:
    return entities.upsert(kind, name, fields=fields, notes=notes, tags=tags)["message"]


@tool(
    "lookup_entity",
    "按类型+名字【精确】取一个对象的全部已知事实。找不到会返回提示。",
    {
        "type": "object",
        "properties": {
            "kind": {"type": "string"},
            "name": {"type": "string"},
        },
        "required": ["kind", "name"],
    },
)
async def lookup_entity(kind: str, name: str) -> str:
    d = entities.get(kind, name)
    return _fmt(d) if d else f"没有记录：{kind}/{name}。"


@tool(
    "search_entities",
    "按关键词【模糊】查对象（匹配名字/备注/字段），可选限定 kind。返回最多 10 条，精确匹配排前。",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "kind": {"type": "string", "description": "可选，限定类型"},
        },
        "required": ["query"],
    },
)
async def search_entities(query: str, kind: str = None) -> str:
    res = entities.find(query, kind=kind, limit=10)
    if not res:
        return "没有匹配的对象。"
    return "\n".join(f"- {_fmt(d)}" for d in res)


@tool(
    "list_entities",
    "列出某一类型下的对象（按最近更新排序）。",
    {
        "type": "object",
        "properties": {
            "kind": {"type": "string"},
        },
        "required": ["kind"],
    },
)
async def list_entities(kind: str) -> str:
    res = entities.list_by_kind(kind)
    if not res:
        return f"没有 {kind} 类型的对象。"
    return "\n".join(f"- {_fmt(d)}" for d in res)
