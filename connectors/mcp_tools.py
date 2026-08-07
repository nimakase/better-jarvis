"""
connectors/mcp_tools.py — MCP 发现机制的模型可调用入口（任务 #6）

对应 core/mcp_discovery.py 的三个使用场景：
  1. mcp_list_servers   —— "有哪些 MCP server 配置了？各自上次发现状态如何？"（只读缓存）
  2. mcp_discover_tools —— "帮我连一下 xxx server，看它到底有哪些工具"（会真的发起连接）
  3. mcp_search_tools   —— "已发现的工具里有没有能直接用的？"（只读缓存，不新连接）

典型用法（造新技能前，替代"凭记忆/凭猜测拼 API"）：先 mcp_list_servers 看有没有
现成配置好的相关 server（如飞书官方 MCP），有就 mcp_discover_tools 拿到权威工具
清单，参照它写技能，而不是自己猜接口。零配置时这几个工具会如实告知"没配"，
不会假装有能力。
"""
from __future__ import annotations

from functools import partial

from core import effects, mcp_discovery
from core.registry import tool as _tool

tool = partial(_tool, group="self")


@tool(
    "mcp_list_servers",
    "查看当前配置了哪些 MCP server，以及各自上次发现的缓存状态（工具数/失败原因）。"
    "只读缓存，不会发起新连接。造新技能前，想知道「这个平台是不是已经有现成 MCP "
    "server」时，先用这个看一眼配置清单。",
    {"type": "object", "properties": {}},
    effect=effects.READ_LOCAL,
)
async def mcp_list_servers() -> str:
    return mcp_discovery.overview()


@tool(
    "mcp_discover_tools",
    "连接一个已配置的 MCP server，拿到它权威声明的工具清单（名称/说明/参数 schema）。"
    "造新技能前先查一下有没有现成能力可以直接复用，比自己猜接口/翻文档更可靠——"
    "server 是它自己声明的，不是拼凑出来的。server 名字从 mcp_list_servers 里找。"
    "会真的发起连接，可能有几秒延迟；结果会缓存，短期内重复调用同一 server 会走缓存"
    "（想强制重连传 refresh=true）。",
    {
        "type": "object",
        "properties": {
            "server": {"type": "string", "description": "server 名字（见 mcp_list_servers）"},
            "refresh": {"type": "boolean", "description": "true=忽略缓存强制重连，默认 false"},
        },
        "required": ["server"],
    },
    effect=effects.READ_EXTERNAL,
    duration="slow",  # 真的会发起连接（子进程/网络），15s 默认值可能不够
)
async def mcp_discover_tools(server: str, refresh: bool = False) -> str:
    result = await mcp_discovery.discover(server, use_cache=not refresh)
    if not result["ok"]:
        return f"❌ 发现失败（server={server}）：{result['error']}"
    tools = result["tools"]
    if not tools:
        return f"『{server}』已连接，但它没有声明任何工具。"
    cached_note = "（缓存）" if result.get("cached") else "（刚连接）"
    lines = [f"『{server}』{cached_note}共 {len(tools)} 个工具："]
    for t in tools:
        desc = (t.get("description") or "").replace("\n", " ")[:100]
        lines.append(f"  - {t['name']}：{desc}")
    lines.append("\n（以上是该 server 权威声明的工具清单，不是贾维斯已经能直接调用的工具——"
                 "要真正接入还需要人工评估是否可信、给出效应等级，这一步只是发现。）")
    return "\n".join(lines)


@tool(
    "mcp_search_tools",
    "在【已经发现过并缓存】的 MCP 工具里做关键词检索，找有没有能满足某个需求的现成"
    "工具。只查缓存，不发起新连接——想覆盖某个 server 得先 mcp_discover_tools 过一次。",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "要找的能力，用自然语言描述"},
        },
        "required": ["query"],
    },
    effect=effects.READ_LOCAL,
)
async def mcp_search_tools(query: str) -> str:
    hits = mcp_discovery.search_cached(query)
    if not hits:
        return "缓存里没找到相关工具（也可能是相关 server 还没 mcp_discover_tools 过）。"
    lines = ["缓存命中："]
    for h in hits:
        lines.append(f"  [{h['server']}] {h['name']}（相关度 {h['score']:.0%}）："
                     f"{h['description'][:80]}")
    return "\n".join(lines)
