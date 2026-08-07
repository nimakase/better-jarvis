"""
core/mcp_discovery.py — MCP（Model Context Protocol）优先发现机制（任务 #6）

背景：贾维斯自造技能时最容易在"根本不知道某个外部系统提供什么能力"这一步就
开始瞎猜（例：让贾维斯给飞书新增多维表格能力，他不知道飞书有多维表格，也不
知道官方已经把整套能力包装成了 MCP server）。MCP 是这类问题的行业标准解法：
第三方（含平台官方，如 larksuite/lark-openapi-mcp）把自己的能力声明式地暴露成
`tools/list`，贾维斯不需要自己猜文档拼方法名，问一句就是权威答案（server 作者
维护的，不是贾维斯脑补的）。

设计要点（对应用户的澄清问题："这是不是一个通用框架，不需要你给我一个现成例子"）：
  - 这是一个**通用机制**，不依赖任何具体 server 才能建成——零配置时全部函数
    直接返回空，不触发任何连接、不影响启动、对现有行为零改变（跟 core/effects、
    core/model_capabilities 一样的"新增基建默认关闭"纪律）。
  - 配置来源：DATA_DIR/mcp_servers.json，格式对齐 Claude Desktop 的
    mcpServers 惯例（{name: {command,args,env} 走 stdio | {url} 走 http}），
    这样用户以后要接一个新 MCP server，直接照抄网上任何一份现成配置片段即可，
    不用学贾维斯专有格式。
  - discover() 只回答"这个 server 有哪些工具"，**不**自动把发现的工具注册成
    贾维斯可直接调用的工具——那是更大的信任决策（谁来审这个 server 是否可信、
    要不要给它效应等级），留给后续环节（造技能前的能力浏览闸门，任务 #7）。
  - 依赖可选：未装 `mcp` 包（pip install mcp）时优雅报错文案，不拖垮其余功能。

已用一个本地 stdio 测试 server 验证过端到端连接可行（见
tests/test_auto_mcp_discovery.py），不依赖真实第三方 server。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

import config

logger = logging.getLogger("jarvis.mcp_discovery")

CONFIG_PATH = config.DATA_DIR / "mcp_servers.json"

_CACHE_TTL = 3600  # 秒；发现结果按 server 名缓存，避免每次查重都重连
_CACHE: dict[str, dict] = {}


# ── 配置 ──────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    """读取 DATA_DIR/mcp_servers.json；不存在/解析失败都返回 {}（零配置=零影响）。"""
    if not CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        logger.warning("mcp_servers.json 解析失败，按未配置处理：%s", e)
        return {}
    servers = data.get("mcpServers")
    return servers if isinstance(servers, dict) else {}


def configured_servers() -> list[str]:
    return sorted(load_config().keys())


# ── 连接与发现 ────────────────────────────────────────────────────────────────

def _mcp_available() -> tuple[bool, str]:
    try:
        import mcp  # noqa: F401
        return True, ""
    except ImportError:
        return False, "未安装 mcp 依赖包（pip install mcp），无法发现 MCP server 工具。"


def _tool_input_schema(t) -> dict:
    """不同 mcp SDK 版本里 Tool 的入参 schema 字段名不一致（协议线上是驼峰
    inputSchema，但 2.x SDK 的 python 对象属性改成了 input_schema）——两个都试。"""
    return getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) or {}


async def _list_tools_stdio(spec: dict, timeout: float) -> list[dict]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=spec["command"],
        args=list(spec.get("args", [])),
        env=spec.get("env") or None,
        cwd=spec.get("cwd") or None,
    )

    async def _run():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.list_tools()

    # asyncio.timeout() 要 3.11+；沙箱/部分部署环境还在 3.10，统一用
    # wait_for 保证跨版本都能跑。
    result = await asyncio.wait_for(_run(), timeout=timeout)
    return [
        {"name": t.name, "description": t.description or "",
         "input_schema": _tool_input_schema(t)}
        for t in result.tools
    ]


async def _list_tools_http(spec: dict, timeout: float) -> list[dict]:
    # 不同 mcp SDK 版本里 http 客户端的导出名不完全一致，两个都试一下。
    from mcp import ClientSession
    try:
        from mcp.client.streamable_http import streamablehttp_client as http_client
    except ImportError:
        from mcp.client.streamable_http import streamable_http_client as http_client

    url = spec["url"]
    headers = spec.get("headers") or None

    async def _run():
        async with http_client(url, headers=headers) as ctx:
            read, write = ctx[0], ctx[1]
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.list_tools()

    result = await asyncio.wait_for(_run(), timeout=timeout)
    return [
        {"name": t.name, "description": t.description or "",
         "input_schema": _tool_input_schema(t)}
        for t in result.tools
    ]


async def discover(name: str, *, use_cache: bool = True, timeout: float = 20.0) -> dict:
    """发现指定 server 的工具清单。返回：
    {"ok": bool, "server": name, "tools": [...], "error": str|None, "cached": bool}

    单个 server 连接失败不抛异常（返回 ok=False + 可读 error），不拖垮调用方
    批量发现其余 server。
    """
    if use_cache:
        cached = _CACHE.get(name)
        if cached and (time.monotonic() - cached["_ts"]) < _CACHE_TTL:
            out = dict(cached)
            out["cached"] = True
            out.pop("_ts", None)
            return out

    servers = load_config()
    spec = servers.get(name)
    if spec is None:
        return {"ok": False, "server": name, "tools": [],
                "error": f"未在 {CONFIG_PATH} 里配置名为 {name!r} 的 MCP server。",
                "cached": False}

    ok_dep, err_dep = _mcp_available()
    if not ok_dep:
        return {"ok": False, "server": name, "tools": [], "error": err_dep, "cached": False}

    try:
        if "url" in spec:
            tools = await _list_tools_http(spec, timeout)
        elif "command" in spec:
            tools = await _list_tools_stdio(spec, timeout)
        else:
            return {"ok": False, "server": name, "tools": [],
                    "error": "配置项既没有 command（stdio）也没有 url（http），格式不对。",
                    "cached": False}
    except (TimeoutError, asyncio.TimeoutError):
        return {"ok": False, "server": name, "tools": [],
                "error": f"连接/发现超时（>{timeout}s）。", "cached": False}
    except Exception as e:  # noqa: BLE001
        logger.warning("MCP 发现失败：server=%r：%s", name, e)
        return {"ok": False, "server": name, "tools": [], "error": str(e), "cached": False}

    result = {"ok": True, "server": name, "tools": tools, "error": None}
    _CACHE[name] = {**result, "_ts": time.monotonic()}
    out = dict(result)
    out["cached"] = False
    return out


async def discover_all(*, use_cache: bool = True, timeout: float = 20.0) -> dict[str, dict]:
    """批量发现所有已配置 server。零配置 → 直接返回 {}，不导入 mcp 包、不做任何 IO。"""
    names = configured_servers()
    if not names:
        return {}
    results = await asyncio.gather(
        *(discover(n, use_cache=use_cache, timeout=timeout) for n in names)
    )
    return {r["server"]: r for r in results}


def cached_snapshot() -> dict[str, dict]:
    """只读缓存快照，不触发任何新连接（供 capability.gather() 这类高频只读场景用）。"""
    return {k: {kk: vv for kk, vv in v.items() if kk != "_ts"} for k, v in _CACHE.items()}


# ── 检索（只在缓存里找，不新连接）──────────────────────────────────────────────

def search_cached(query: str, top_k: int = 5, min_score: float = 0.15) -> list[dict]:
    """在【已发现过并缓存】的 MCP 工具里做词面检索。返回
    [{server, name, description, score}]。要保证覆盖某个 server，先 discover() 一次。
    """
    from core.capability import _score, _tokens  # 复用同一套确定性词面匹配

    q = _tokens(query)
    scored = []
    for server, result in _CACHE.items():
        if not result.get("ok"):
            continue
        for t in result.get("tools", []):
            target = _tokens(f"{t['name']} {t.get('description', '')}")
            s = _score(q, target)
            if s >= min_score:
                scored.append({"server": server, "name": t["name"],
                                "description": t.get("description", ""),
                                "score": round(s, 3)})
    scored.sort(key=lambda c: c["score"], reverse=True)
    return scored[:top_k]


def overview() -> str:
    """人/模型可读的总览：配置了哪些 server，各自缓存里发现过多少工具。"""
    names = configured_servers()
    if not names:
        return "未配置任何 MCP server（DATA_DIR/mcp_servers.json 不存在或为空）。"
    lines = [f"MCP server 配置（共 {len(names)} 个）", ""]
    for n in names:
        cached = _CACHE.get(n)
        if cached is None:
            lines.append(f"  - {n}：尚未发现过（调用 mcp_discover_tools 触发一次）")
        elif cached.get("ok"):
            lines.append(f"  - {n}：{len(cached.get('tools', []))} 个工具（缓存）")
        else:
            lines.append(f"  - {n}：上次发现失败：{cached.get('error', '?')}")
    return "\n".join(lines)
