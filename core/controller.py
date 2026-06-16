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

import config
from core import memory as mem
from core import registry
from core.results import ToolResult
# 向后兼容：连接器/技能仍可 `from core.controller import register_tool`
from core.registry import register_tool

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
- 生成任何文件（PDF、Excel、报告、文档等）后，必须调用 send_file_to_chat 把文件发到对话界面，不要只告知路径

【证件保险箱（高敏感，单独处理）】
- 证件、银行卡、身份证、护照等存放在加密保险箱，与普通记忆库分开。
- 查"我有哪些证件/卡""XX 什么时候到期"等：调 list_credentials（返回已脱敏）。
- 用户要看完整卡号/CVV/证件号：调 reveal_credential（用代号 alias 定位，只取用户问到的字段，如只问卡号就传 fields='card_number'）。真实号码会直接显示在用户网页上，你只会收到脱敏确认——不要试图自己复述或猜测完整号码。
- 用户给出证件照片路径要保存：调 ingest_credential_image（本机识别，不上传云端）。
- 绝不主动把任何完整号码、CVV 写进回复文本；这些只能经 reveal_credential 安全显示。
- 不要建议用户在聊天框直接打出完整卡号/CVV/证件号（会经过云端）；引导他们用网页右上角「保险箱」录入或上传照片。

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


# 仅在渐进披露开启时追加：告诉模型如何按需加载领域工具。关闭时此段不出现，
# 默认 system prompt 与历史完全一致。
PROGRESSIVE_PROMPT_NOTE = """【工具按需加载】
当前只给你暴露了核心常驻工具和一个 load_tools 元工具。若用户的需求属于某个
领域（如日报推送、潜客信号、报告生成等），而你当前看不到对应工具，就先调用
load_tools(group="领域名") 加载该组；下一轮你即可看到并调用这些工具。load_tools
的说明里列出了所有可用领域及其包含的工具。不要凭空臆造工具名。"""


def _build_system_prompt() -> str:
    now = datetime.now().strftime("现在是 %Y年%m月%d日 %H:%M，%A")
    context = mem.build_context_block()
    parts = [FIXED_SYSTEM_PROMPT, f"【当前时间】{now}"]
    if config.PROGRESSIVE_TOOLS:
        parts.append(PROGRESSIVE_PROMPT_NOTE)
    if context:
        parts.append(context)
    return "\n\n".join(parts)


# ── 工具注册表 ────────────────────────────────────────────────────────────────
# 工具的事实来源已统一到 core.registry；本模块只负责格式转换与执行分发。
# register_tool 已在文件顶部从 core.registry 再导出，保持向后兼容。


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


def _normalize_result(raw) -> ToolResult:
    """把 handler 的返回归一化为 ToolResult（旧式 str 自动包装）。"""
    return raw if isinstance(raw, ToolResult) else ToolResult(text=str(raw))


async def _execute_tool(name: str, inputs: dict) -> ToolResult:
    handler = registry.get_handler(name)
    if handler is not None:
        try:
            raw = await _safe_call(handler, **inputs)
        except Exception as e:
            raw = f"工具 {name} 执行出错：{e}"
        return _normalize_result(raw)

    return ToolResult(text=f"未知工具：{name}")


# ── 主控对话循环 ──────────────────────────────────────────────────────────────

class JarvisController:
    def __init__(self):
        self.client = AsyncOpenAI(
            api_key=config.OPENROUTER_API_KEY,
            base_url=config.OPENROUTER_BASE_URL,
        )
        self.messages: list[dict] = []
        self.pending_actions: list = []
        # 渐进披露：本会话已激活的领域分组（核心工具始终可见，与此无关）。
        self.active_groups: set[str] = set()

    def reset_session(self):
        self.messages = []
        self.pending_actions = []
        self.active_groups = set()

    def drain_actions(self) -> list:
        """取出并清空本轮累积的带外动作（供传输层 main.py 处理）。"""
        actions = self.pending_actions
        self.pending_actions = []
        return actions

    def get_all_tools(self) -> list[dict]:
        """返回 OpenAI function calling 格式的全部工具列表（历史行为）。"""
        all_defs = registry.definitions()
        return [_to_openai_tool(d) for d in all_defs]

    # ── 渐进披露（默认关；config.PROGRESSIVE_TOOLS 控制）────────────────────────
    LOAD_TOOLS_NAME = "load_tools"

    def _build_load_tools_def(self) -> dict:
        """动态构造 load_tools 元工具定义：description 里列出尚未加载的领域及其工具，
        让模型据此决定加载哪个组（参照 deferred-tool 模式）。"""
        core = set(config.CORE_TOOL_NAMES)
        lines = []
        for grp, names in registry.groups().items():
            if grp in self.active_groups:
                continue
            remaining = [n for n in names if n not in core]
            if remaining:
                lines.append(f"  {grp}：{', '.join(remaining)}")
        menu = "\n".join(lines) if lines else "  （所有领域已加载）"
        return {
            "name": self.LOAD_TOOLS_NAME,
            "description": (
                "按需加载某个领域的工具。当前只暴露了核心常驻工具；若用户需求属于"
                "下面某个领域，先用本工具加载该组，下一轮即可看到并调用这些工具。\n"
                "可用领域及其工具：\n" + menu
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "group": {"type": "string", "description": "要加载的领域分组名，如 signal_intel / delivery / report"}
                },
                "required": ["group"],
            },
        }

    def _activate_group(self, group: str) -> str:
        """激活一个领域分组，返回给模型看的确认文本。"""
        grps = registry.groups()
        if group not in grps:
            avail = ", ".join(g for g in grps if g not in self.active_groups) or "（无）"
            return f"没有名为 '{group}' 的领域。当前可加载：{avail}"
        self.active_groups.add(group)
        return f"已加载领域 '{group}'，现在可用工具：{', '.join(grps[group])}"

    def get_exposed_tools(self) -> list[dict]:
        """本轮要暴露给模型的工具集。

        - PROGRESSIVE_TOOLS 关（默认）→ 与 get_all_tools() 完全一致，零行为变化。
        - PROGRESSIVE_TOOLS 开 → 核心常驻工具 ∪ 已激活领域的工具，再加 load_tools 元工具。
        注意：这只影响「告知模型有哪些工具」；任何工具的 handler 仍在全量注册表里，
        即便未被暴露，一旦被调用也照常执行——因此该特性纯加法、不破坏任何调用。
        """
        if not config.PROGRESSIVE_TOOLS:
            return self.get_all_tools()

        core = set(config.CORE_TOOL_NAMES)
        name_to_group = {n: g for g, names in registry.groups().items() for n in names}
        exposed = [
            _to_openai_tool(d)
            for d in registry.definitions()
            if d["name"] in core or name_to_group.get(d["name"]) in self.active_groups
        ]
        exposed.append(_to_openai_tool(self._build_load_tools_def()))
        return exposed

    async def chat(self, user_message: str) -> AsyncGenerator[str, None]:
        """流式处理用户消息，内部自动处理工具调用循环。"""
        self.pending_actions = []
        self.messages = await _compress_history(self.client, self.messages)
        self.messages.append({"role": "user", "content": user_message})

        system = _build_system_prompt()

        tool_rounds = 0
        MAX_TOOL_ROUNDS = 12   # 工具调用轮次上限，防止无限调工具不收尾
        while True:
            # 每轮重算暴露的工具集：渐进披露下，上一轮的 load_tools 会在这里生效。
            # 关闭时这等价于一次性的全量列表（仅多一次廉价的 dict 构造）。
            tools = self.get_exposed_tools()

            # 流式调用
            full_text = ""
            # 收集工具调用（流式下需要拼接 delta）
            tool_call_accum: dict[int, dict] = {}  # index → {id, name, arguments}

            stream = await self.client.chat.completions.create(
                model=config.CLAUDE_MODEL,
                max_tokens=config.MAX_TOKENS_RESPONSE,
                messages=[{"role": "system", "content": system}] + self.messages,
                tools=tools,
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

            # 防死循环：工具调用轮次上限。超过则停止，避免无限调工具不收尾。
            tool_rounds += 1
            if tool_rounds > MAX_TOOL_ROUNDS:
                note = (f"已连续调用工具 {MAX_TOOL_ROUNDS} 轮仍未完成，先停下避免死循环。"
                        f"可能是需求太宽或缺合适工具——请把任务拆细些，或换个说法再试。")
                yield "\n⚠️ " + note + "\n"
                self.messages.append({"role": "assistant", "content": note})
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
                try:
                    inputs = json.loads(tc["arguments"]) if tc["arguments"] else {}
                except json.JSONDecodeError:
                    inputs = {}

                # 渐进披露：load_tools 是控制器级元工具（不在注册表里），
                # 仅在开启时拦截；激活领域后下一轮即可见到该组工具。
                if config.PROGRESSIVE_TOOLS and tc["name"] == self.LOAD_TOOLS_NAME:
                    text = self._activate_group(inputs.get("group", ""))
                    yield f"\n🧩 {text}\n"
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": text,
                    })
                    continue

                yield f"\n⚙️ 调用工具：{tc['name']}...\n"
                result = await _execute_tool(tc["name"], inputs)
                self.pending_actions.extend(result.actions)
                # 若模型直接调用了某领域工具（未先 load_tools），把该组一并激活，
                # 保持后续暴露集与实际用到的工具一致。
                if config.PROGRESSIVE_TOOLS:
                    for g, names in registry.groups().items():
                        if tc["name"] in names:
                            self.active_groups.add(g)
                            break
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result.text,
                })

            # 继续循环，让模型消化工具结果
