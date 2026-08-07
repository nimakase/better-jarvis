#!/usr/bin/env python3
"""造技能前的 MCP 能力浏览闸门（任务 #7）——自动生成，绝不覆盖既有测试文件。

覆盖：
  1. 零配置 → 仅返回标准指令（标准指令本身必须始终存在，这是"强制"的部分）
  2. 配置了 server 但与需求词面不相关 → 仍只返回标准指令，不误触发连接
  3. 配置了 server 且词面相关 → 自动发现一次，把权威工具清单注入提示词
  4. 异常路径全部优雅降级为标准指令，不抛异常、不阻断造工具
  5. create_tool 的生成提示词组装里确实接了这一步（源码级连线检查）
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)
        print(f"  ✗ {msg}")
    else:
        print(f"  ✓ {msg}")


try:
    import mcp  # noqa: F401
    _HAS_MCP = True
except ImportError:
    _HAS_MCP = False

from core import tool_builder as tb  # noqa: E402
from core import mcp_discovery as md  # noqa: E402

_orig_config_path = md.CONFIG_PATH
FIXTURE = str(Path(__file__).resolve().parent / "_fixture_mcp_server.py")


# ── 1. 零配置 ─────────────────────────────────────────────────────────────────
print("[1] 零配置 → 只有标准指令")


async def _t_zero_config():
    md.CONFIG_PATH = Path("/tmp/_jarvis_test_capgate_nonexistent/mcp_servers.json")
    md._CACHE.clear()
    try:
        out = await tb._gather_mcp_hints("给我做一个查天气的工具")
        check(out == tb._MCP_STANDING_NOTE, "零配置时精确等于标准指令，不多不少")
        check("mcp_list_servers" in out and "mcp_discover_tools" in out,
              "标准指令点名了两个自查工具，模型能照做而不是空谈")
    finally:
        md.CONFIG_PATH = _orig_config_path
        md._CACHE.clear()

asyncio.run(_t_zero_config())


# ── 2. 配置了但不相关 ──────────────────────────────────────────────────────────
print("[2] 配置了 server 但需求词面不相关 → 不误触发")

_tmp_cfg_dir = Path("/tmp/_jarvis_test_capgate_cfg")
_tmp_cfg_dir.mkdir(parents=True, exist_ok=True)
_tmp_cfg = _tmp_cfg_dir / "mcp_servers.json"
_tmp_cfg.write_text(
    '{"mcpServers": {"feishu多维表格": {"command": "python3", "args": ["' + FIXTURE + '"]}}}',
    encoding="utf-8",
)


async def _t_irrelevant():
    md.CONFIG_PATH = _tmp_cfg
    md._CACHE.clear()
    try:
        out = await tb._gather_mcp_hints("给我查一下今天上海的天气")
        check(out == tb._MCP_STANDING_NOTE,
              "词面不相关（天气 vs 飞书多维表格）→ 不发起连接，只有标准指令")
    finally:
        md.CONFIG_PATH = _orig_config_path
        md._CACHE.clear()

asyncio.run(_t_irrelevant())


# ── 3. 配置了且相关 → 自动发现 ─────────────────────────────────────────────────
if not _HAS_MCP:
    print("[3] 跳过（未装 mcp 依赖包）")
else:
    print("[3] 配置了 server 且需求词面相关 → 自动发现一次")

    async def _t_relevant():
        md.CONFIG_PATH = _tmp_cfg
        md._CACHE.clear()
        try:
            out = await tb._gather_mcp_hints("帮我做一个能操作飞书多维表格的工具")
            check("已自动发现" in out, "命中相关 server → 触发自动发现")
            check("ping" in out and "echo" in out, "发现到的权威工具清单被注入（用测试夹具的两个工具名验证）")
            check(tb._MCP_STANDING_NOTE.strip() in out, "标准指令依然附带在末尾，不因为命中就省略")
        finally:
            md.CONFIG_PATH = _orig_config_path
            md._CACHE.clear()

    asyncio.run(_t_relevant())


# ── 4. 异常路径优雅降级 ─────────────────────────────────────────────────────────
print("[4] 异常路径优雅降级")


async def _t_exception_path():
    import core.tool_builder as tb_mod
    orig = None
    try:
        import core.mcp_discovery as md_mod
        orig = md_mod.configured_servers
        def _boom():
            raise RuntimeError("模拟配置读取失败")
        md_mod.configured_servers = _boom
        out = await tb_mod._gather_mcp_hints("随便什么需求")
        check(out == tb_mod._MCP_STANDING_NOTE, "内部异常 → 优雅降级为标准指令，不抛异常")
    finally:
        if orig is not None:
            import core.mcp_discovery as md_mod
            md_mod.configured_servers = orig

asyncio.run(_t_exception_path())


# ── 5. create_tool 确实接了这一步（源码级连线检查）─────────────────────────────
print("[5] create_tool 接线检查")
import inspect  # noqa: E402

src = inspect.getsource(tb.create_tool)
check("_gather_mcp_hints" in src, "create_tool 的源码里调用了 _gather_mcp_hints")
check("mcp_hint" in src, "create_tool 把 mcp_hint 拼进了最终 base 提示词")


# ── 收尾 ──────────────────────────────────────────────────────────────────────
print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 项失败")
    sys.exit(1)
print("✅ test_auto_capability_gate 全部通过")
sys.exit(0)
