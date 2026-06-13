"""
主控对话循环 (L1)

使用 OpenRouter API（OpenAI 兼容格式）。
工具调用格式遵循 OpenAI function calling 规范。
"""

import json
import asyncio
from datetime import datetime
from typing import AsyncGenerator, Callable

from openai import AsyncOpenAI

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import config
from core import memory as mem

# ── System Prompt ─────────────────────────────────────────────────────────────

FIXED_SYSTEM_PROMPT = """你是贾维斯，Ned 的个人 AI 助理。你的职责是处理日程、保险、投资、飞书沟通等日常事务。

【人格】
- 简洁直接，不废话，不重复用户说过的话
- 主动关联上下文：如果记忆里有相关信息，自然地带出来
- 高风险领域（医疗、财务）的建议结尾必须加免责声明
- 不确定的事情说不确定，绝不编造数据
- 回答用中文，技术词汇可用英文

【路由规则】
- 能自己答的直接答，不调工具
- 需要实时数据才调对应连接器工具
- 需要写入或更新用户信息时调 write_memory
- 查询已知用户信息时调 query_memory

【动作安全】
- 只读操作直接执行
- 不可逆操作（发消息、删除）必须先告知用户将要做什么，获得确认后再执行

【免责声明模板】
投资建议结尾加："（以上仅供参考，不构成投资建议，请结合自身情况判断）"
医疗建议结尾加："（以上仅供参考，具体请咨询专业医生）"

【项目结构（自建工具时必须遵守）】
- 项目根目录：main.py 所在目录
- 自建技能目录：项目根目录下的 skills/<工具名>/tool.py
- 用户数据目录：通过 config.DATA_DIR 获取（Windows 在 AppData/Roaming/Jarvis，macOS 在 ~/Library/Application Support/Jarvis）
- 禁止硬编码任何绝对路径，一律用 pathlib.Path 和 config 模块
- 所有文件路径操作必须同时兼容 Windows 和 macOS：
  * 使用 pathlib.Path 而非字符串拼接路径
  * 使用 Path.home() 而非 /Users/xxx 或 C:/Users/xxx
  * 文件分隔符用 Path 对象自动处理，不要手写 / 或 \\
"""


def _build_system_prompt() -> str:
    now = datetime.now().strftime("现在是 %Y年%m月%d日 %H:%M，%A")
    context = mem.build_context_block()
    parts = [FIXED_SYSTEM_PROMPT, f"【当前时间】{now}"]
    if context:
        parts.append(context)
    return "\n\n".join(parts)


# ── 工具注册表 ────────────────────────────────────────────────────────────────

_tool_registry: dict[str, Callable] = {}
_tool_definitions: list[dict] = []  # 存 input_schema 格式，发送前转换为 OpenAI 格式


def register_tool(definition: dict, handler: Callable):
    """注册外部工具（连接器调用）。definition 用 input_schema 格式。重复注册自动跳过。"""
    name = definition["name"]
    if any(d["name"] == name for d in _tool_definitions):
        return
    _tool_definitions.append(definition)
    _tool_registry[name] = handler


def _to_openai_tool(defn: dict) -> dict:
    """把 input_schema 格式转换成 OpenAI function calling 格式。"""
    return {
        "type": "function",
        "function": {
            "name": defn["name"],
            "description": defn["description"],
            "parameters": defn.get("input_schema", {"type": "object", "properties": {}}),
        }
    }


# ── 内置工具：记忆读写 ────────────────────────────────────────────────────────

def _builtin_query_memory(key: str) -> str:
    val = mem.read(key)
    if val is None:
        return f"记忆库中没有找到 key='{key}' 的记录。"
    return json.dumps(val, ensure_ascii=False)


def _builtin_write_memory(key: str, value: str, expires_days: int = 0, source: str = "", sensitive: bool = False) -> str:
    from datetime import datetime, timezone, timedelta
    exp = None
    if expires_days > 0:
        exp = datetime.now(timezone.utc) + timedelta(days=expires_days)
    try:
        parsed = json.loads(value)
    except Exception:
        parsed = value
    mem.write(key, parsed, source=source, expires_at=exp, sensitive=sensitive)
    return f"已记录：{key} = {value}" + (f"（{expires_days}天后过期）" if expires_days else "")


def _builtin_list_memory() -> str:
    items = mem.list_all()
    if not items:
        return "记忆库为空。"
    return json.dumps(items, ensure_ascii=False, indent=2)


BUILTIN_TOOL_DEFS = [
    {
        "name": "query_memory",
        "description": "查询用户记忆库中的某条信息。用于获取用户的偏好、状态、历史决定等已存储的信息。",
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "记忆条目的键，如 'insurance_policies'、'risk_preference'"}
            },
            "required": ["key"]
        }
    },
    {
        "name": "write_memory",
        "description": "将用户信息写入记忆库，用于保存用户偏好、状态、决定等需要跨会话保留的信息。",
        "input_schema": {
            "type": "object",
            "properties": {
                "key":          {"type": "string",  "description": "记忆键"},
                "value":        {"type": "string",  "description": "要存储的内容（JSON 字符串或纯文本）"},
                "expires_days": {"type": "integer", "description": "有效天数，0 表示永久"},
                "source":       {"type": "string",  "description": "信息来源描述"},
                "sensitive":    {"type": "boolean", "description": "是否加密存储（含个人敏感信息时为 true）"}
            },
            "required": ["key", "value"]
        }
    },
    {
        "name": "list_memory",
        "description": "列出记忆库中所有条目，供用户查看或审计。",
        "input_schema": {"type": "object", "properties": {}}
    },
]

BUILTIN_HANDLERS = {
    "query_memory": lambda args: _builtin_query_memory(**args),
    "write_memory":  lambda args: _builtin_write_memory(**args),
    "list_memory":   lambda args: _builtin_list_memory(),
}


# ── 对话历史压缩 ──────────────────────────────────────────────────────────────

async def _compress_history(client: AsyncOpenAI, messages: list) -> list:
    """把超出软上限的早期消息压缩成摘要。"""
    total_chars = sum(len(str(m.get("content", ""))) for m in messages)
    estimated_tokens = total_chars // 2
    if len(messages) <= config.MAX_HISTORY_TURNS and estimated_tokens < config.CONTEXT_WINDOW_SOFT_LIMIT:
        return messages

    to_compress = messages[:-config.MAX_HISTORY_TURNS]
    keep = messages[-config.MAX_HISTORY_TURNS:]

    text_to_summarize = "\n".join(
        f"{m['role'].upper()}: {m['content'] if isinstance(m['content'], str) else '[工具交互]'}"
        for m in to_compress
        if isinstance(m.get('content'), str)
    )

    resp = await client.chat.completions.create(
        model=config.CLAUDE_MODEL_LIGHT,
        max_tokens=512,
        messages=[
            {"role": "system", "content": "你是摘要助手，请简洁摘要对话历史。"},
            {"role": "user", "content": f"请用中文简洁摘要以下对话历史，保留关键决定、事实和用户偏好：\n\n{text_to_summarize}"}
        ]
    )
    summary = resp.choices[0].message.content

    return [{"role": "user", "content": f"[早期对话摘要]\n{summary}"}] + keep


# ── 工具执行 ──────────────────────────────────────────────────────────────────

async def _safe_call(handler, *args, **kwargs):
    """
    调用 handler，自动处理三种情况：
      - 普通函数：直接返回
      - async 函数：await
      - 普通函数但返回了 coroutine（lambda 包 async 的情况）：也 await
    """
    result = handler(*args, **kwargs)
    if asyncio.iscoroutine(result):
        result = await result
    return result


async def _execute_tool(name: str, inputs: dict) -> str:
    if name in BUILTIN_HANDLERS:
        try:
            return await _safe_call(BUILTIN_HANDLERS[name], inputs)
        except Exception as e:
            return f"工具执行出错：{e}"

    if name in _tool_registry:
        try:
            return await _safe_call(_tool_registry[name], **inputs)
        except Exception as e:
            return f"工具 {name} 执行出错：{e}"

    return f"未知工具：{name}"


# ── 主控对话循环 ──────────────────────────────────────────────────────────────

class JarvisController:
    def __init__(self):
        self.client = AsyncOpenAI(
            api_key=config.OPENROUTER_API_KEY,
            base_url=config.OPENROUTER_BASE_URL,
        )
        self.messages: list[dict] = []

    def reset_session(self):
        self.messages = []

    def get_all_tools(self) -> list[dict]:
        """返回 OpenAI function calling 格式的工具列表。"""
        all_defs = BUILTIN_TOOL_DEFS + _tool_definitions
        return [_to_openai_tool(d) for d in all_defs]

    async def chat(self, user_message: str) -> AsyncGenerator[str, None]:
        """流式处理用户消息，内部自动处理工具调用循环。"""
        self.messages = await _compress_history(self.client, self.messages)
        self.messages.append({"role": "user", "content": user_message})

        system = _build_system_prompt()
        all_tools = self.get_all_tools()

        while True:
            # 流式调用
            full_text = ""
            # 收集工具调用（流式下需要拼接 delta）
            tool_call_accum: dict[int, dict] = {}  # index → {id, name, arguments}

            stream = await self.client.chat.completions.create(
                model=config.CLAUDE_MODEL,
                max_tokens=config.MAX_TOKENS_RESPONSE,
                messages=[{"role": "system", "content": system}] + self.messages,
                tools=all_tools,
                tool_choice="auto",
                stream=True,
            )

            finish_reason = None

            async for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta is None:
                    continue

                finish_reason = chunk.choices[0].finish_reason or finish_reason

                # 文字内容
                if delta.content:
                    full_text += delta.content
                    yield delta.content

                # 工具调用 delta（OpenAI 流式下分片到达）
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

            # 没有工具调用，正常结束
            if finish_reason != "tool_calls" or not tool_call_accum:
                self.messages.append({"role": "assistant", "content": full_text})
                break

            # 把助手消息（含工具调用声明）存入历史
            tool_calls_for_msg = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": tc["arguments"]}
                }
                for tc in tool_call_accum.values()
            ]
            self.messages.append({
                "role": "assistant",
                "content": full_text or None,
                "tool_calls": tool_calls_for_msg,
            })

            # 执行每个工具，把结果追加为 tool 消息
            for tc in tool_call_accum.values():
                yield f"\n⚙️ 调用工具：{tc['name']}...\n"
                try:
                    inputs = json.loads(tc["arguments"]) if tc["arguments"] else {}
                except json.JSONDecodeError:
                    inputs = {}
                result = await _execute_tool(tc["name"], inputs)
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                })

            # 继续循环，让模型消化工具结果


# 单例
controller = JarvisController()
