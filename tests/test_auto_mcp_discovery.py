#!/usr/bin/env python3
"""MCP 优先发现机制（任务 #6）+ web_search 工具注册回归——自动生成，绝不覆盖既有测试文件。

覆盖：
  1. 零配置 = 零影响（未配置任何 server 时全部函数安全返回空）
  2. 端到端真实发现：起一个本地 stdio 测试 server（tests/_fixture_mcp_server.py，
     纯子进程管道通信，不需要出站网络），验证连接/initialize/list_tools 全链路
  3. 缓存行为（同 server 第二次调用走缓存 / refresh 强制重连 / 未连接过时的 search 为空）
  4. connectors/mcp_tools.py 三个模型可调用工具，经 registry 走完整调用路径
  5. core/capability.gather() 能看到已发现的 MCP 工具（只读缓存来源，不新连接）
  6. 回归：connectors/web_search.py 的 @tool("web_search",...) 修复前错误注册到了
     _render_results（签名对不上，运行时会崩）——现在必须注册到真正的 web_search()
"""
import asyncio
import subprocess
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

FIXTURE = str(Path(__file__).resolve().parent / "_fixture_mcp_server.py")


# ── 1. 零配置 = 零影响 ────────────────────────────────────────────────────────
print("[1] 零配置行为")
from core import mcp_discovery as md  # noqa: E402

_orig_config_path = md.CONFIG_PATH


async def _t_zero_config():
    md.CONFIG_PATH = Path("/tmp/_jarvis_test_mcp_nonexistent_dir/mcp_servers.json")
    md._CACHE.clear()
    try:
        check(md.configured_servers() == [], "未配置文件 → configured_servers 为空")
        result = await md.discover_all()
        check(result == {}, "discover_all 零配置 → 直接返回空 dict，不触发任何连接")
        check(md.search_cached("任何东西") == [], "search_cached 无缓存 → 空列表")
        check("未配置" in md.overview(), "overview 如实说明未配置")
    finally:
        md.CONFIG_PATH = _orig_config_path
        md._CACHE.clear()

asyncio.run(_t_zero_config())


# ── 2~3. 端到端真实发现 + 缓存行为 ─────────────────────────────────────────────
if not _HAS_MCP:
    print("[2/3] 跳过（未装 mcp 依赖包，属可选增强，不影响其余功能）")
else:
    print("[2] 端到端真实发现（本地 stdio 测试 server）")

    _tmp_cfg_dir = Path("/tmp/_jarvis_test_mcp_cfg")
    _tmp_cfg_dir.mkdir(parents=True, exist_ok=True)
    _tmp_cfg = _tmp_cfg_dir / "mcp_servers.json"
    _tmp_cfg.write_text(
        '{"mcpServers": {"fixture": {"command": "python3", "args": ["' + FIXTURE + '"]}, '
        '"broken": {"command": "python3", "args": ["-c", "import time; time.sleep(999)"]}}}',
        encoding="utf-8",
    )

    async def _t_discover_e2e():
        md.CONFIG_PATH = _tmp_cfg
        md._CACHE.clear()
        try:
            check(md.configured_servers() == ["broken", "fixture"],
                  "配置文件里的两个 server 都被读到（排序后）")

            r1 = await md.discover("fixture", use_cache=False, timeout=15)
            check(r1["ok"], f"fixture server 发现成功：{r1.get('error')}")
            names = {t["name"] for t in r1["tools"]}
            check(names == {"ping", "echo"}, f"发现到的工具名正确：{names}")
            echo_tool = next(t for t in r1["tools"] if t["name"] == "echo")
            check("text" in echo_tool["input_schema"].get("properties", {}),
                  "echo 工具的 input_schema 权威地带出了 text 参数")
            check(r1["cached"] is False, "首次发现 cached=False")

            r2 = await md.discover("fixture", use_cache=True, timeout=15)
            check(r2["cached"] is True, "第二次调用命中缓存")
            check(r2["tools"] == r1["tools"], "缓存内容与首次发现一致")

            r3 = await md.discover("fixture", use_cache=False, timeout=15)
            check(r3["cached"] is False, "refresh=不用缓存 → 强制重连，cached=False")

            missing = await md.discover("不存在的server")
            check(not missing["ok"] and "未在" in missing["error"],
                  "未配置的 server 名 → 明确报错，不崩")

            hits = md.search_cached("echo 文本")
            check(any(h["name"] == "echo" for h in hits),
                  "search_cached 能在缓存里找到 echo 工具")

            ov = md.overview()
            check("fixture" in ov and "2 个工具" in ov, "overview 反映已发现的工具数")
        finally:
            md.CONFIG_PATH = _orig_config_path
            md._CACHE.clear()

    asyncio.run(_t_discover_e2e())

    print("[3] 超时处理（连了但 server 卡住不回应 initialize）")

    async def _t_timeout():
        md.CONFIG_PATH = _tmp_cfg
        md._CACHE.clear()
        try:
            r = await md.discover("broken", use_cache=False, timeout=2)
            check(not r["ok"], "卡住的 server 最终 ok=False（不会无限挂起）")
            check("超时" in (r["error"] or "") or r["error"], f"给出可读原因：{r['error']}")
        finally:
            md.CONFIG_PATH = _orig_config_path
            md._CACHE.clear()

    asyncio.run(_t_timeout())


# ── 4. connectors/mcp_tools.py 经 registry 走通 ────────────────────────────────
print("[4] mcp_tools 经 registry 注册且可调用")
from core import registry  # noqa: E402
import connectors.mcp_tools as mt  # noqa: E402, F401 (触发 @tool 装饰注册)

for _n in ("mcp_list_servers", "mcp_discover_tools", "mcp_search_tools"):
    check(registry.get_handler(_n) is not None, f"{_n} 已注册进 registry")


async def _t_mcp_tools_smoke():
    md.CONFIG_PATH = _orig_config_path
    md._CACHE.clear()
    out = await mt.mcp_list_servers()
    check(isinstance(out, str) and len(out) > 0, "mcp_list_servers 返回非空文案")
    out2 = await mt.mcp_search_tools("不存在的能力查询")
    check("没找到" in out2 or "没有" in out2 or out2, "mcp_search_tools 无命中时给出可读文案，不崩")

asyncio.run(_t_mcp_tools_smoke())

if _HAS_MCP:
    async def _t_mcp_discover_tool():
        md.CONFIG_PATH = _tmp_cfg
        md._CACHE.clear()
        try:
            out = await mt.mcp_discover_tools("fixture")
            check("echo" in out and "ping" in out, "mcp_discover_tools 工具函数能拿到真实清单")
            out_bad = await mt.mcp_discover_tools("不存在的server")
            check("失败" in out_bad, "mcp_discover_tools 对未配置 server 给出失败文案而非报错崩溃")
        finally:
            md.CONFIG_PATH = _orig_config_path
            md._CACHE.clear()

    asyncio.run(_t_mcp_discover_tool())


# ── 5. capability.gather() 收纳已发现的 MCP 工具 ───────────────────────────────
print("[5] capability.gather() 收纳 MCP 发现结果")
from core import capability  # noqa: E402

if _HAS_MCP:
    async def _t_capability_gather():
        md.CONFIG_PATH = _tmp_cfg
        md._CACHE.clear()
        try:
            await md.discover("fixture", use_cache=False, timeout=15)
            caps = capability.gather()
            mcp_caps = [c for c in caps if c["kind"] == "mcp_tool(未接入)"]
            names = {c["name"] for c in mcp_caps}
            check("fixture.echo" in names and "fixture.ping" in names,
                  "gather() 里出现刚发现的 MCP 工具，命名前缀带 server 名")
        finally:
            md.CONFIG_PATH = _orig_config_path
            md._CACHE.clear()

    asyncio.run(_t_capability_gather())
else:
    caps = capability.gather()
    check(all(c["kind"] != "mcp_tool(未接入)" for c in caps),
          "未装 mcp 依赖包时 gather() 不产生 mcp_tool 项（无缓存自然为空）")


# ── 6. 回归：web_search 必须注册到真正的 web_search()，不是 _render_results ────
print("[6] web_search 注册回归")
import connectors.web_search as ws  # noqa: E402

handler = registry.get_handler("web_search")
check(handler is ws.web_search, "web_search 工具的 registry handler 就是 connectors.web_search.web_search"
      "（此前误把装饰器套在了 _render_results 上，运行时会因签名不对而崩）")
check(handler is not ws._render_results, "确认没有指向内部渲染辅助函数")


async def _t_web_search_callable():
    orig_any = config.ANYSEARCH_API_KEY
    orig_exa = config.EXA_API_KEY
    orig_model = config.CLAUDE_MODEL
    config.ANYSEARCH_API_KEY = ""
    config.EXA_API_KEY = ""
    config.CLAUDE_MODEL = "deepseek-v4-flash"  # 不带 :online，测最朴素的降级路径
    try:
        # 通过 registry 拿到的 handler 直接按工具调用约定的参数形式调用，必须不抛异常
        out = await handler(query="集成电路 分销商")
        check(isinstance(out, str) and len(out) > 0,
              "经 registry.get_handler 拿到的 handler 能被正常调用且返回字符串")
    finally:
        config.ANYSEARCH_API_KEY = orig_any
        config.EXA_API_KEY = orig_exa
        config.CLAUDE_MODEL = orig_model

asyncio.run(_t_web_search_callable())


# ── 收尾 ──────────────────────────────────────────────────────────────────────
print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 项失败")
    sys.exit(1)
print("✅ test_auto_mcp_discovery 全部通过")
sys.exit(0)
