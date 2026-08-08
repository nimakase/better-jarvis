#!/usr/bin/env python3
"""
scripts/verify_deepseek_switch.py — DeepSeek 官方 API 迁移前的人工验证脚本（任务 #5）

背景：沙箱连不了 api.deepseek.com / api.exa.ai（只有 PyPI 能连），这一步必须在
你本机跑。脚本不改 .env，用环境变量在【本进程内】临时把 provider 切成
deepseek，跑完不影响你现有 openrouter 配置，也不会误触发正式切换。

三项检查：
  1. 基础连通性：普通对话补全能不能拿到回复（先过一遍鉴权/网络）。
  2. 工具调用兼容性：core/controller.py 是靠 stream=True 读 delta.tool_calls
     分片拼起来判断 finish_reason=="tool_calls" 的（见 controller.py 547-622
     行），这里原样复现同一套解析逻辑，真跑一次工具调用，确认 DeepSeek 官方
     API 的流式分片格式跟 OpenRouter 完全兼容——这是切换最容易埋雷、也最不该
     等生产环境才发现的一环。
  3. web_search 的 Exa 后端：真实跑一次检索，确认 EXA_API_KEY 有效、
     connectors/web_search.py 的响应解析正常，不是静默退化成降级文案。

本机运行（仓库根，已装依赖、.env 里 DEEPSEEK_API_KEY / EXA_API_KEY 已填好）：
    python scripts/verify_deepseek_switch.py

三项全过后，再去 .env 把 `# JARVIS_LLM_PROVIDER=deepseek` 那行的注释去掉，
正式切换；切完建议跑一遍 tests/run_all.py 求个心安（这些测试默认走 mock，
不受真实 provider 影响，但过一遍不吃亏）。
"""
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["JARVIS_LLM_PROVIDER"] = "deepseek"  # 仅本进程内生效，不改 .env

import config  # noqa: E402

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)
        print(f"  ✗ {msg}")
    else:
        print(f"  ✓ {msg}")


async def check_basic_chat():
    print("\n[1] 基础连通性（普通对话补全）")
    if not config.DEEPSEEK_API_KEY:
        check(False, "DEEPSEEK_API_KEY 为空——先在 .env 里填上再跑这个脚本")
        return
    from core import llm
    client = llm.get_client(timeout=30)
    try:
        resp = await client.chat.completions.create(
            model=config.CLAUDE_MODEL,
            max_tokens=50,
            messages=[{"role": "user", "content": "用一句话回复：连接测试成功"}],
        )
        text = resp.choices[0].message.content or ""
        check(bool(text.strip()), f"拿到非空回复：{text.strip()[:60]!r}")
    except Exception as e:
        check(False, f"调用异常：{type(e).__name__}: {e}")


async def check_tool_calling():
    print("\n[2] 工具调用兼容性（原样复现 controller.py 的流式解析逻辑）")
    if not config.DEEPSEEK_API_KEY:
        check(False, "DEEPSEEK_API_KEY 为空，跳过")
        return
    from core import llm
    client = llm.get_client(timeout=30)
    tools = [{
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询某个城市的天气",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string", "description": "城市名"}},
                "required": ["city"],
            },
        },
    }]
    try:
        stream = await client.chat.completions.create(
            model=config.CLAUDE_MODEL,
            max_tokens=200,
            messages=[{"role": "user", "content": "北京今天天气怎么样？调用工具查一下。"}],
            tools=tools,
            tool_choice="auto",
            stream=True,
        )
    except Exception as e:
        check(False, f"发起流式调用异常：{type(e).__name__}: {e}")
        return

    tool_call_accum: dict[int, dict] = {}
    finish_reason = None
    full_text = ""
    try:
        async for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta is None:
                continue
            finish_reason = chunk.choices[0].finish_reason or finish_reason
            if delta.content:
                full_text += delta.content
            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tool_call_accum:
                        tool_call_accum[idx] = {"id": "", "name": "", "arguments": ""}
                    if tc_delta.id:
                        tool_call_accum[idx]["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            tool_call_accum[idx]["name"] += tc_delta.function.name
                        if tc_delta.function.arguments:
                            tool_call_accum[idx]["arguments"] += tc_delta.function.arguments
    except Exception as e:
        check(False, f"读流过程中异常：{type(e).__name__}: {e}")
        return

    check(finish_reason == "tool_calls", f"finish_reason 是 'tool_calls'（实际：{finish_reason!r}）")
    check(bool(tool_call_accum), f"拿到至少一个工具调用（实际 {len(tool_call_accum)} 个）")
    for idx, tc in tool_call_accum.items():
        check(bool(tc["id"]), f"第{idx}个工具调用带 id（实际 {tc['id']!r}）")
        check(tc["name"] == "get_weather", f"第{idx}个工具调用名字解析正确（实际 {tc['name']!r}）")
        try:
            args = json.loads(tc["arguments"]) if tc["arguments"] else {}
            check("city" in args, f"第{idx}个工具调用参数能解析出 city（实际 {args}）")
        except json.JSONDecodeError:
            check(False, f"第{idx}个工具调用的 arguments 不是合法 JSON：{tc['arguments']!r}")


async def check_exa_search():
    print("\n[3] web_search 的 Exa 后端")
    if not config.EXA_API_KEY:
        check(False, "EXA_API_KEY 为空，跳过")
        return
    from connectors import web_search as ws
    try:
        text = await ws.web_search("2026年人工智能最新进展", max_results=3)
        check("结构化搜索不可用" not in text, "没有退化到降级文案（说明 Exa 真的调通了）")
        check(len(text.strip()) > 20, "拿到看起来像真实结果的渲染文本")
        print(f"  （前 200 字预览：{text[:200]!r}）")
    except Exception as e:
        check(False, f"调用异常：{type(e).__name__}: {e}")


async def main():
    print(f"provider={config.LLM_PROVIDER}  model={config.CLAUDE_MODEL}  base_url={config.LLM_BASE_URL}")
    await check_basic_chat()
    await check_tool_calling()
    await check_exa_search()
    print()
    if FAILURES:
        print(f"❌ {len(FAILURES)} 项失败——先别去 .env 正式切换，把上面的报错发给贾维斯排查。")
        return 1
    print("✅ 三项全过。可以去 .env 把 `# JARVIS_LLM_PROVIDER=deepseek` 的注释去掉，正式切换。")
    print("   切完建议跑一遍 tests/run_all.py 求个心安（默认走 mock，不受真实 provider 影响）。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
