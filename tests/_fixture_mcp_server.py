#!/usr/bin/env python3
"""tests/test_auto_mcp_discovery.py 用的最小 stdio MCP server 夹具。

不是测试文件本身（文件名故意不匹配 test_*.py，run_all.py 不会把它当测试跑）。
纯本地子进程通信（stdio 管道），不需要出站网络，可在无网络的沙箱里完整跑通
"发现"这一整条链路（连接 → initialize → list_tools），验证
core/mcp_discovery.py 对着真实 MCP 协议实现是否work，而不是只测"能不能读配置"。
"""
from mcp.server.mcpserver import MCPServer

app = MCPServer("jarvis-test-fixture")


@app.tool()
def ping() -> str:
    """连通性探测，返回 pong。"""
    return "pong"


@app.tool()
def echo(text: str) -> str:
    """原样返回传入的文本。"""
    return text


if __name__ == "__main__":
    app.run(transport="stdio")
