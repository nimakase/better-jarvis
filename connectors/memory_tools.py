"""
内置记忆库读写工具（query_memory / write_memory / list_memory）

这些不是外部连接器，而是第一方工具；放在可被 discover_connectors 自动发现的
位置、统一用 @tool 自注册，避免散落在对话引擎（controller）内部。
"""

import json
from datetime import datetime, timezone, timedelta

from core import memory as mem
from core.registry import tool


@tool(
    "query_memory",
    "查询用户记忆库中的某条信息。用于获取用户的偏好、状态、历史决定等已存储的信息。",
    {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "记忆条目的键，如 'insurance_policies'、'risk_preference'"}
        },
        "required": ["key"],
    },
)
async def query_memory(key: str) -> str:
    val = mem.read(key)
    if val is None:
        return f"记忆库中没有找到 key='{key}' 的记录。"
    return json.dumps(val, ensure_ascii=False)


@tool(
    "write_memory",
    "将用户信息写入记忆库，用于保存用户偏好、状态、决定等需要跨会话保留的信息。",
    {
        "type": "object",
        "properties": {
            "key":          {"type": "string",  "description": "记忆键"},
            "value":        {"type": "string",  "description": "要存储的内容（JSON 字符串或纯文本）"},
            "expires_days": {"type": "integer", "description": "有效天数，0 表示永久"},
            "source":       {"type": "string",  "description": "信息来源描述"},
            "sensitive":    {"type": "boolean", "description": "是否加密存储（含个人敏感信息时为 true）"},
        },
        "required": ["key", "value"],
    },
)
async def write_memory(key: str, value: str, expires_days: int = 0, source: str = "", sensitive: bool = False) -> str:
    exp = None
    if expires_days > 0:
        exp = datetime.now(timezone.utc) + timedelta(days=expires_days)
    try:
        parsed = json.loads(value)
    except Exception:
        parsed = value
    mem.write(key, parsed, source=source, expires_at=exp, sensitive=sensitive)
    return f"已记录：{key} = {value}" + (f"（{expires_days}天后过期）" if expires_days else "")


@tool(
    "list_memory",
    "列出记忆库中所有条目，供用户查看或审计。",
    {"type": "object", "properties": {}},
)
async def list_memory() -> str:
    items = mem.list_all()
    if not items:
        return "记忆库为空。"
    return json.dumps(items, ensure_ascii=False, indent=2)
