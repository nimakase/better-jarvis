"""
主控对话循环 (L1)

使用 OpenRouter API（OpenAI 兼容格式）。
工具调用格式遵循 OpenAI function calling 规范。
"""

import json
import os
import asyncio
import logging
from datetime import datetime
from typing import AsyncGenerator

# 工具调用轮次上限（防跑飞）。默认 30，大工程也够用；可用 env 调。
# 配合下面的「重复无进展」检测：真正的死循环会被快速识别提前停，
# 所以把绝对上限放宽是安全的——不会因为任务大就误伤。
_MAX_TOOL_ROUNDS = int(os.environ.get("JARVIS_MAX_TOOL_ROUNDS", "30"))
# 连续多少轮"调用完全相同的工具+参数"判定为卡循环，提前停。
_TOOL_STALL_LIMIT = int(os.environ.get("JARVIS_TOOL_STALL_LIMIT", "4"))
# 单次模型调用超时（秒）。关键：SDK 默认高达 600s，一次卡住就会静默长挂、
# 灯不黄也没回复。给个有界值，超时即报错而非无限等。
_LLM_TIMEOUT = float(os.environ.get("JARVIS_LLM_TIMEOUT", "120"))
# 历史压缩那次摘要调用用更紧的超时；失败/超时会退化（见 _compress_history），不拖垮整轮。
_COMPRESS_TIMEOUT = float(os.environ.get("JARVIS_COMPRESS_TIMEOUT", "30"))
# 压缩摘要的输入上限（字符），避免历史巨大时把摘要调用拖慢。
_COMPRESS_INPUT_CAP = int(os.environ.get("JARVIS_COMPRESS_INPUT_CAP", "12000"))

from openai import AsyncOpenAI

import time as _time

import config
from core import effects as _effects
from core import profile
from core import group_memory as _group_memory
from core import model_capabilities as _model_capabilities
from core import tool_timeout as _tool_timeout
from core import registry
from core import signals as _signals
from core import telemetry as _telemetry
from core import trust as _trust
from core import reports as _reports
from core import workflow_registry as _workflows
from core import calendar as _calendar
from core.results import ToolResult
# 向后兼容：连接器/技能仍可 `from core.controller import register_tool`
from core.registry import register_tool

# ── System Prompt ─────────────────────────────────────────────────────────────

FIXED_SYSTEM_PROMPT = """你是贾维斯，Ned 的个人 AI 助理。你的职责是处理保险、投资、文档、情报等日常事务。

【人格】
- 简洁直接，不废话，不重复用户说过的话
- 主动关联上下文：如果记忆里有相关信息，自然地带出来
- 高风险领域（医疗、财务）的建议结尾必须加免责声明
- 不确定的事情说不确定，绝不编造数据
- 回答用中文，技术词汇可用英文

【路由规则】
- 能自己答的直接答，不调工具
- 需要实时数据才调对应连接器工具
- 用户透露【长期稳定】的事实或偏好（风险偏好、家庭成员、长期目标、关键日期、固定习惯等）时，调 remember_fact 钉进用户档案；一次性/临时信息不要记
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

【报告政策（重要，避免滥用）】
- 报告是"归档成档、可在线查看/发送"的 PDF 工件，不是更长的回答。生成报告调 generate_report。
- 默认一律用【对话】回答。只有用户明确说"出/生成/做一份…报告 / 日报 / PDF / 导出 / 存档 / 发我一份"这类措辞时才生成报告。
- "怎么样 / 分析一下 / 什么情况 / 帮我看看 / 某赛道某公司近况"等一律对话回答，绝不生成报告。
- 绝不主动提议生成报告（不要在回答末尾问"要不要我帮你生成一份报告"），除非用户自己问起。
- 不明确要哪种报告时，先问用户要哪种、什么范围，不要擅自选型或瞎填参数。
- 周期性报告（如每日市场日报）由定时任务自动产出，日常对话中不要主动去生成。

【工作流政策（多步骤任务，重要）】
- 工作流（如 prospect_daily 今日潜客名单）是有副作用的多步骤任务，用 run_workflow 运行。
- 只在用户【明确要求运行】（如"跑一下潜客 / 生成今日名单"）或定时任务触发时才运行；绝不主动或因为联想而运行。
- 对会打开浏览器/连接 HubSpot 的工作流（标注"有副作用"），必须先用一句话告知"我将运行X，会打开 Chrome 连 HubSpot"，得到用户确认后再调 run_workflow。
- 需求不明确时先问，不要擅自选择工作流或瞎跑。

【免责声明模板】
投资建议结尾加："（以上仅供参考，不构成投资建议，请结合自身情况判断）"
医疗建议结尾加："（以上仅供参考，具体请咨询专业医生）"

【自建工具的读 / 审 / 激活（重要，别再说自己读不了代码）】
- 自建工具的源码就在 skills/<工具名>/tool.py，你【能】读：要看代码、查 bug、讲实现，一律调 read_tool_code(name)，绝不要回复"我读不了 py 文件"。
- 用户要重新看某工具的审查、或说"刚才的审查窗口没了"：调 review_tool(name) 把代码和校验结果重新调出来（网页重弹卡片、飞书重发代码）。
- 用户审查完说"激活 X / 启用 X / 这个能用了"：调 activate_tool(name)，无需回本机网页点按钮，飞书对话里也能激活。
- 工具有 bug 时的正确流程：read_tool_code 看代码 → edit_tool 改 → review_tool 复看 → activate_tool 生效；不要停在"我看不了代码"。

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


# 能力边界与求助纪律（2026-07-25 收敛哲学落地）：把"何时自己上、何时停下交接"
# 从口头共识变成 prompt 里的硬规则。升级路径 = 停下 + 交接给 Ned（他手动开 Claude
# 接手），【不是】自动 spawn。每轮无条件注入（见 _build_system_prompt）。
ESCALATION_POLICY = """【能力边界与求助纪律】
- 日常事务大胆自主、别畏手畏脚：对话、查记忆、读文档、管日程、跑已有工作流、造简单
  自包含的工具——直接做，不必事事请示。
- 但遇到下列情形，【停下、别硬上】，产出一份交接简报交给 Ned（他会开更强的 agent 如
  Claude 接手）：① 复杂代码 / 大重构 / 新架构 / 改动牵动多个模块；② 碰安全边界或需要
  改 PROTECTED 核心文件；③ 机械信号触发：命中卡循环检测、能力索引里已记过这条缺口、
  自建工具子进程冒烟连续失败。
- 交接简报必须含四段：我想达成什么 / 我已试了什么及结果 / 我判断的根因 / 需要更强的
  agent 具体做什么。宁可停早一点，也不要产出"看着成功、其实是错的"的半成品。
- 注意：spawn 子 agent 是我自己拆并行子任务用的（有界、只读扇出），【不是】用来绕过
  "这活我不该独自干"——难活的出口是交接给 Ned，不是 spawn。"""


# 仅在渐进披露开启时追加：告诉模型如何按需加载领域工具。关闭时此段不出现，
# 默认 system prompt 与历史完全一致。
PROGRESSIVE_PROMPT_NOTE = """【工具按需加载】
当前只给你暴露了核心常驻工具和一个 load_tools 元工具。若用户的需求属于某个
领域（如日报推送、潜客信号、报告生成等），而你当前看不到对应工具，就先调用
load_tools(group="领域名") 加载该组；下一轮你即可看到并调用这些工具。load_tools
的说明里列出了所有可用领域及其包含的工具。不要凭空臆造工具名。"""


def _network_capability_note() -> str:
    """据当前模型的能力声明（core/model_capabilities）告诉它能否联网、能否读图。
    模型不会凭空知道自己的运行配置，必须显式说明，否则它会错误地拒绝/假装联网。

    2026-08-07：从"一条写死判断 :online"改为查 core/model_capabilities 的声明式
    能力表——DeepSeek 官方 API 没有 :online 语法，但已原生支持视觉，靠字符串
    匹配单一标记撑不住多种能力，改成一处可维护的表。"""
    caps = _model_capabilities.capabilities_of(config.CLAUDE_MODEL)
    if caps.online_search:
        parts = ["【联网能力】你当前已接入实时联网检索。"
                 "需要最新信息（新闻、行情、近期事件、网页内容）时可以直接作答，"
                 "并尽量注明信息可能的时效；不要声称自己无法上网。"]
    else:
        parts = ["【联网能力】你当前【没有】实时联网能力，只能基于已有知识和被调用工具"
                 "返回的数据作答。涉及最新/实时信息时，明确说明你无法联网核实，不要编造。"]
    if caps.vision:
        parts.append("【视觉能力】你当前的模型支持图片输入，可以直接看用户发来的图片作答。")
    return " ".join(parts)


def _build_system_prompt(active_groups: "set[str] | frozenset[str]" = frozenset()) -> str:
    now = datetime.now().strftime("现在是 %Y年%m月%d日 %H:%M，%A")
    parts = [FIXED_SYSTEM_PROMPT, f"【当前时间】{now}", _network_capability_note(),
             ESCALATION_POLICY]
    if config.PROGRESSIVE_TOOLS:
        parts.append(PROGRESSIVE_PROMPT_NOTE)
    # 常驻用户档案（core memory）：少量长期硬事实，每轮注入，跨会话钉住不淡化
    block = profile.build_block()
    if block:
        parts.append(block)
    # 组作用域记忆（2026-08-07 新增）：只在对应工具组本轮已加载时才附带该组专属
    # 业务笔记（如 HubSpot 报价规则），不常驻、不污染跟该组无关的对话。
    for _g in sorted(active_groups):
        try:
            gb = _group_memory.build_block(_g)
        except Exception:
            gb = ""
        if gb:
            parts.append(gb)
    # 近期日程（内置日历，时间真源）：将到事件 + 休假 + 临近到期 + 今日定时，每轮注入
    cal_block = _calendar.build_block()
    if cal_block:
        parts.append(cal_block)
    # 报告目录：让模型知道有哪些报告类型、何时用、要什么参数（配合上面的报告政策）
    catalog = _reports.catalog_block()
    if catalog:
        parts.append(catalog)
    # 工作流目录：可运行的多步骤任务（配合上面的工作流政策）
    wf_catalog = _workflows.catalog_block()
    if wf_catalog:
        parts.append(wf_catalog)
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


# ── 后台落盘助手（绝不阻塞事件循环）────────────────────────────────────────────

def _safe_save_episode(text: str, tags: list, source: str) -> None:
    """在后台线程里把一段情节落进 L4。任何异常都吞掉——best-effort，不影响对话。"""
    try:
        from core import episodic
        episodic.save(text, tags=tags, source=source)
    except Exception:
        pass


def _fire_and_forget(coro) -> None:
    """发射即忘地跑一个协程：不 await、不阻塞回复；吞掉异常避免 asyncio 告警。
    没有运行中的事件循环时（理论上不会发生）直接忽略。"""
    try:
        task = asyncio.create_task(coro)
        task.add_done_callback(lambda t: t.exception())
    except RuntimeError:
        pass


# ── 对话历史压缩 ──────────────────────────────────────────────────────────────

async def _compress_history(client: AsyncOpenAI, messages: list, persist: bool = False) -> list:
    """把超出软上限的早期消息压缩成摘要。

    persist=True 时（仅真人会话），额外把摘要落进 L4 情节记忆并向量化，
    避免"被压缩掉的早期对话用完即蒸发"。best-effort：任何异常都不影响主循环。
    """
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
    # 限制输入规模，保证摘要调用有界、不被巨大历史拖慢（取最近一段即可）。
    if len(text_to_summarize) > _COMPRESS_INPUT_CAP:
        text_to_summarize = text_to_summarize[-_COMPRESS_INPUT_CAP:]

    # 关键：这次摘要调用在【每轮对话最开头、出任何字之前】发生。一旦它卡住/超时，
    # 整轮对话就静默冻住（灯不黄、无回复）。所以加紧超时 + 失败即优雅退化——
    # 宁可这一轮省略早期历史，也绝不让它挂起主对话。
    try:
        resp = await client.chat.completions.create(
            model=config.CLAUDE_MODEL_LIGHT,
            max_tokens=512,
            timeout=_COMPRESS_TIMEOUT,
            messages=[
                {"role": "system", "content": "你是摘要助手，请简洁摘要对话历史。"},
                {"role": "user", "content": f"请用中文简洁摘要以下对话历史，保留关键决定、事实和用户偏好：\n\n{text_to_summarize}"}
            ]
        )
        summary = resp.choices[0].message.content
    except Exception as e:
        logging.getLogger("jarvis.controller").warning("历史压缩摘要失败，退化省略早期历史：%s", e)
        return [{"role": "user", "content": "[早期历史摘要生成失败，已省略早期对话]"}] + keep

    # L4 情节记忆：把被压缩掉的早期对话摘要落盘并向量化，之后可被 recall 语义召回。
    # 仅真人会话写入（persist=True）；后台/工作流实例不污染情节记忆。
    # 关键：嵌入是同步 CPU/IO（首次还会下载模型），绝不能在事件循环里同步跑，
    # 否则会冻住整个服务。这里【发射即忘 + 丢到后台线程】，永不拖慢/阻塞回复。
    if persist and summary:
        _fire_and_forget(asyncio.to_thread(
            _safe_save_episode, summary, ["auto-compress"], "compress"))

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


async def _forced_text_summary(client, system: str, messages: list) -> str:
    """最终必答保证：工具跑完但模型交了白卷（最终文本为空）时，强制补一次
    纯文本总结——这次调用不带 tools，模型只能说话。失败返回空串（不拖垮回合）。

    背景（2026-07-22 实测）：多轮工具消息后部分模型会以空 content 收尾，
    传输层只能显示「处理完成，无文本输出」。这不该发生——结果都在工具消息里，
    差的只是让模型把它讲出来。"""
    try:
        resp = await client.chat.completions.create(
            model=config.CLAUDE_MODEL,
            max_tokens=config.MAX_TOKENS_RESPONSE,
            messages=[{"role": "system", "content": system}] + messages + [{
                "role": "user",
                "content": "（系统提示：你刚执行了工具但没输出任何文字。"
                           "请基于上面的工具结果，用中文把结论/现状直接总结给用户；"
                           "若工具报了错，说明是什么错、你判断的原因和建议。）",
            }],
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception:
        return ""


async def _execute_tool(name: str, inputs: dict, session: str = "interactive") -> ToolResult:
    handler = registry.get_handler(name)
    if handler is not None:
        # 遥测（core/telemetry）：记录成败与耗时，喂 self_review/能力画像。绝不阻断。
        t0 = _time.perf_counter()
        ok, err = True, ""
        # 强制超时（core/tool_timeout，任务 #14）：没有任何工具该无限期占住主线——
        # 卡住的工具此前会冻住整条对话；耗时分类见 core/tool_timeout.py。
        timeout_s = _tool_timeout.timeout_of(name)
        try:
            if timeout_s is None:
                raw = await _safe_call(handler, **inputs)
            else:
                raw = await asyncio.wait_for(_safe_call(handler, **inputs), timeout=timeout_s)
        except (asyncio.TimeoutError, TimeoutError):
            ok, err = False, "timeout"
            raw = (f"⏱️ 工具 {name} 执行超过 {timeout_s:g} 秒未返回，已放弃等待（本次未获得结果）。"
                   "如果这类操作本来就需要更久，建议改用 spawn_subtask 派发到后台，而不是同步等它。")
        except Exception as e:
            ok, err = False, f"{type(e).__name__}: {e}"
            raw = f"工具 {name} 执行出错：{e}"
        _telemetry.record(name, ok, int((_time.perf_counter() - t0) * 1000), err, session)
        return _normalize_result(raw)

    return ToolResult(text=f"未知工具：{name}")


# 后台/工作流实例（非真人对话）禁用的工具：它们会写【用户个人长期记忆】或留下
# 代码/调度等持久产物。这些只应由真人面对面的对话实例调用，否则后台跑潜客/采集/
# 定时任务时会把它自己的临时任务塞进用户档案、或擅自自建工具/建调度（污染）。
BACKGROUND_BLOCKED_TOOLS = {
    "remember_fact",                                              # 写个人 core memory (L1)
    "save_entity", "remember_episode",                           # 写实体(L2)/情节(L4)记忆
    "create_tool", "edit_tool", "delete_tool", "activate_tool", "update_tool_code",  # 自建/改/删/激活工具
    "create_schedule", "delete_schedule", "pause_schedule", "resume_schedule",  # 改定时任务
}


# ── 主控对话循环 ──────────────────────────────────────────────────────────────

class JarvisController:
    def __init__(self, interactive: bool = True,
                 allowed_tools: "set[str] | None" = None,
                 max_tool_rounds: "int | None" = None,
                 channel: str = "",
                 model: str = ""):
        from core.llm import get_client
        self.client = get_client(timeout=_LLM_TIMEOUT)   # 单一构建点见 core/llm
        # 工具白名单（⑧，spawn 子 agent 用）：None=不启用（历史行为）；
        # 集合=只允许这些工具（暴露与执行双层过滤，fail-safe）。
        self.allowed_tools = allowed_tools
        # 轮次预算（⑨）：None=全局默认 _MAX_TOOL_ROUNDS。
        self.max_tool_rounds = max_tool_rounds
        # 渠道感知（㉒）：传输层设置（web/chat→"web"，lark_bridge→"lark"）；
        # 非交互实例默认 background。见 core/channels。
        self.channel = channel or ("" if interactive else "background")
        # 实例级模型覆盖（子 agent 可跑便宜模型）；空 = 全局 CLAUDE_MODEL。
        self.model = model or config.CLAUDE_MODEL
        self.messages: list[dict] = []
        self.pending_actions: list = []
        # 渐进披露：本会话已激活的领域分组（核心工具始终可见，与此无关）。
        self.active_groups: set[str] = set()
        # interactive=True 是真人面对面的对话实例；后台/工作流/定时跑的设 False，
        # 届时屏蔽 BACKGROUND_BLOCKED_TOOLS（不写用户个人记忆、不自建工具/调度）。
        self.interactive = interactive
        # 不可逆动作确认闸（core/effects）：机械拦截，逻辑全在 effects 模块。
        self.confirm_gate = _effects.ConfirmGate()
        # 污染闸（core/trust）：本回合读过外部不可信内容 → 禁对外动作（防注入）。
        self.taint = _trust.TaintTracker()

    def reset_session(self):
        self.messages = []
        self.pending_actions = []
        self.active_groups = set()
        self.confirm_gate = _effects.ConfirmGate()
        self.taint = _trust.TaintTracker()

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
                    "group": {"type": "string", "description": "要加载的领域分组名，如 credentials / delivery / scheduling"}
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
        blocked = set() if self.interactive else BACKGROUND_BLOCKED_TOOLS

        def _permitted(name: str) -> bool:
            """黑名单（后台）+ 白名单（spawn 子 agent，⑧）双层过滤。"""
            if name in blocked:
                return False
            return self.allowed_tools is None or name in self.allowed_tools

        if not config.PROGRESSIVE_TOOLS:
            return [_to_openai_tool(d) for d in registry.definitions()
                    if _permitted(d["name"])]

        core = set(config.CORE_TOOL_NAMES)
        name_to_group = {n: g for g, names in registry.groups().items() for n in names}
        exposed = [
            _to_openai_tool(d)
            for d in registry.definitions()
            if _permitted(d["name"])
            and (d["name"] in core or name_to_group.get(d["name"]) in self.active_groups)
        ]
        exposed.append(_to_openai_tool(self._build_load_tools_def()))
        return exposed

    async def chat(self, user_message: str) -> AsyncGenerator[dict, None]:
        """流式处理用户消息，内部自动处理工具调用循环。

        产出的是【结构化事件】而非裸字符串，把"对话正文"和"工具进度"分到两条通道，
        避免进度行混进正文（再也不会在历史里残留 ⚙️/工具清单）：
          - {"type": "text", "text": "..."}  模型真正的回复文本分片
          - {"type": "tool", "name": "..."}  调用某工具的进度提示（传输层可低调渲染）
        """
        self.pending_actions = []
        # 确认闸：新用户回合到来——上轮被拦的不可逆调用晋级为「可执行」（一次性），
        # 更早的过期作废。见 core/effects.ConfirmGate。
        self.confirm_gate.new_user_turn()
        # 污染闸：新用户回合污染清零（见 core/trust）。
        self.taint.new_user_turn()
        # 监督信号打标（core/signals）：检测纠正/放弃/重试并落库，攒学习数据。
        # 后台线程 best-effort，绝不阻塞对话（仅真人会话记）。
        if self.interactive:
            prev = next((m.get("content") or "" for m in reversed(self.messages)
                         if m.get("role") == "assistant"), "")
            _fire_and_forget(asyncio.to_thread(_signals.tag, user_message, prev))
        self.messages = await _compress_history(self.client, self.messages, persist=self.interactive)
        self.messages.append({"role": "user", "content": user_message})

        system = _build_system_prompt(self.active_groups)
        # 自我处境注入（⑩㉒）：本机感官读数 + 渠道能力画像。任何失败零影响。
        try:
            from core import channels as _channels
            from core import world_state as _world_state
            extra = [_channels.note(self.channel)]
            ws = _world_state.context_block()
            if ws:
                extra.append(ws)
            system += "\n\n" + "\n\n".join(extra)
        except Exception:
            pass

        tool_rounds = 0
        # 工具调用轮次上限：实例预算（spawn 子 agent，⑨）优先，否则全局默认。
        MAX_TOOL_ROUNDS = self.max_tool_rounds or _MAX_TOOL_ROUNDS
        # 卡循环检测（结果感知）：签名 = (本轮调用, 本轮结果)。只有【调用与结果都
        # 完全重复】才算"原地打转"；结果一变即视为有进展并清零。比只看调用宽容得多，
        # 不会误伤"反复读同一份代码来思考/修 bug"这类正常行为。
        last_round_sig = None
        stall = 0
        while True:
            # 每轮重算暴露的工具集：渐进披露下，上一轮的 load_tools 会在这里生效。
            # 关闭时这等价于一次性的全量列表（仅多一次廉价的 dict 构造）。
            tools = self.get_exposed_tools()

            # 流式调用
            full_text = ""
            # 收集工具调用（流式下需要拼接 delta）
            tool_call_accum: dict[int, dict] = {}  # index → {id, name, arguments}

            stream = await self.client.chat.completions.create(
                model=self.model,
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
                    yield {"type": "text", "text": delta.content}

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
                # 最终必答保证：跑过工具却交白卷 → 强制补一次纯文本总结
                if not full_text.strip() and tool_rounds > 0:
                    fallback = await _forced_text_summary(self.client, system, self.messages)
                    if fallback:
                        full_text = fallback
                        yield {"type": "text", "text": fallback}
                self.messages.append({"role": "assistant", "content": full_text})
                break

            # 本轮调用签名（结果签名在工具执行后与它合并，见循环末尾的卡循环判定）。
            calls_sig = tuple(sorted((tc["name"], tc["arguments"])
                                     for tc in tool_call_accum.values()))

            # 绝对上限：防止意外跑飞。达到后【不丢进度】——工具结果都在会话历史里，
            # 直接回复「继续」即可从断点接着做，而不是前功尽弃。
            tool_rounds += 1
            if tool_rounds > MAX_TOOL_ROUNDS:
                note = (f"这一步比较大，已连续调用工具 {MAX_TOOL_ROUNDS} 轮，先暂停一下"
                        f"（防止意外跑飞）。进度都在——直接回复「继续」我就接着往下做；"
                        f"或者把剩下的部分说得更具体些也行。")
                yield {"type": "text", "text": "\n⏸️ " + note + "\n"}
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
            _msgs_before = len(self.messages)   # 快照，循环末用于取本轮工具结果做卡循环判定
            for tc in tool_call_accum.values():
                try:
                    inputs = json.loads(tc["arguments"]) if tc["arguments"] else {}
                except json.JSONDecodeError:
                    inputs = {}

                # 渐进披露：load_tools 是控制器级元工具（不在注册表里），
                # 仅在开启时拦截；激活领域后下一轮即可见到该组工具。
                if config.PROGRESSIVE_TOOLS and tc["name"] == self.LOAD_TOOLS_NAME:
                    text = self._activate_group(inputs.get("group", ""))
                    # 只发结构化进度事件（不把工具清单塞进正文流）；清单仅进模型可见的 tool 消息。
                    yield {"type": "tool", "name": "加载工具组"}
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": text,
                    })
                    continue

                # 兜底：后台/工作流实例即便模型硬调被屏蔽的工具，也不执行（防污染用户档案等）。
                if not self.interactive and tc["name"] in BACKGROUND_BLOCKED_TOOLS:
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": f"（后台运行环境，已禁用工具 {tc['name']}：不写用户个人记忆/不自建工具或调度。请直接完成本职任务并输出结果。）",
                    })
                    continue

                # 白名单执行层兜底（⑧）：即便模型硬调未授权工具也不执行（fail-safe）。
                if self.allowed_tools is not None and tc["name"] not in self.allowed_tools:
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": (f"（子 agent 权限外的工具 {tc['name']}：未被授权，已拒绝。"
                                    f"请只用授权清单内的工具完成任务，或在结果里说明缺什么能力。）"),
                    })
                    continue

                # 效应闸：不可逆动作必须隔一个用户回合确认（机械拦截，非 prompt 自觉）。
                allowed, block_msg = self.confirm_gate.check(tc["name"], tc["arguments"])
                if not allowed:
                    yield {"type": "tool", "name": f"{tc['name']}（待确认）"}
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": block_msg,
                    })
                    continue

                # 污染闸：本回合读过外部不可信内容 → 禁对外动作（防提示注入）。
                allowed, block_msg = self.taint.check(tc["name"])
                if not allowed:
                    yield {"type": "tool", "name": f"{tc['name']}（污染闸拦截）"}
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": block_msg,
                    })
                    continue

                yield {"type": "tool", "name": tc["name"]}
                result = await _execute_tool(
                    tc["name"], inputs,
                    session="interactive" if self.interactive else "background")
                self.taint.absorb(tc["name"])
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

            # 卡循环判定（结果感知）：本轮 (调用, 结果) 与上一轮完全相同 → 无新信息进来。
            # 结果一变即清零；连续 _TOOL_STALL_LIMIT 轮"调用+结果"都不变才判卡死。
            _results_sig = tuple(
                (m.get("content") or "")[:500]
                for m in self.messages[_msgs_before:] if m.get("role") == "tool")
            round_sig = (calls_sig, _results_sig)
            stall = stall + 1 if round_sig == last_round_sig else 0
            last_round_sig = round_sig
            if stall >= _TOOL_STALL_LIMIT:
                note = ("同一组工具连续多轮返回完全相同的结果、没有新信息进来，先停一下"
                        "（疑似卡循环）。可以换个思路，或把这一步说得更具体些，我再试。")
                yield {"type": "text", "text": "\n⚠️ " + note + "\n"}
                self.messages.append({"role": "assistant", "content": note})
                break

            # 继续循环，让模型消化工具结果
