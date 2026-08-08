#!/usr/bin/env python3
"""DeepSeek V4 空补全静默失败 —— 自动生成，绝不覆盖既有测试文件。

背景：用户在本机把 JARVIS_LLM_PROVIDER 正式切到 deepseek 后，用了一会儿反馈
"贾维斯有时候静默失败——发一句话，指示灯黄一下就绿了，什么都没有；有时候是
说到一半断掉"。查证：DeepSeek V4 官方 GitHub issue track 上有多起同类报告——
(1) 模型有时会直接返回完全空的补全（content/reasoning_content 都是空，
completion_tokens=0），哪怕没调用过任何工具；(2) 思考模式下带 tool_calls 的
assistant 消息回灌历史时若没带上同一轮的 reasoning_content，从下一轮起请求
会被拒（400）。

core/controller.py 原有的"最终必答保证"只在 tool_rounds>0（用过工具）时才
触发兜底总结，覆盖不到"第一轮就交白卷"这种情况；而且兜底总结本身失败时
只会静默返回空串——两层加起来就是用户看到的"什么都没发生"。这次一并修：
  1. 兜底触发条件从"用过工具才补"改成"只要交白卷就补"。
  2. 兜底本身也失败/仍空时，改为给用户一条看得见的提示，而不是彻底沉默。
  3. 流式累积 delta.reasoning_content，回灌 assistant(tool_calls) 消息时带上，
     避免思考模式下第二轮起被拒。

覆盖：
  1. _forced_text_summary 按 used_tools 措辞不同（不误导"没用过工具却提工具"）。
  2. 端到端驱动 controller.chat：第一轮就交白卷（未调用任何工具）→ 自动触发
     兜底总结，兜底成功则把总结文字返回给用户。
  3. 端到端：兜底本身也交白卷 → 不再彻底静音，给出可见提示并写入历史。
  4. 端到端：一轮工具调用带 reasoning_content 分片 → 累积后回灌进
     assistant(tool_calls) 消息，供下一轮请求带回给 DeepSeek。
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for _v in ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy",
           "HTTP_PROXY", "http_proxy"):
    os.environ.pop(_v, None)

import config  # noqa: E402
config.PROGRESSIVE_TOOLS = False
if not getattr(config, "OPENROUTER_API_KEY", None):
    config.OPENROUTER_API_KEY = "test-key"

from core import registry, controller as ctl_mod  # noqa: E402

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)
        print(f"  ✗ {msg}")
    else:
        print(f"  ✓ {msg}")


# ── 假流式/非流式对象（沿用 test_stall_detection.py 的既有模式）───────────────
class _Delta:
    def __init__(self, content=None, tool_calls=None, reasoning_content=None):
        self.content = content
        self.tool_calls = tool_calls
        self.reasoning_content = reasoning_content


class _Choice:
    def __init__(self, delta, finish):
        self.delta = delta
        self.finish_reason = finish


class _Chunk:
    def __init__(self, choice):
        self.choices = [choice]


class _TCFn:
    def __init__(self, name, args):
        self.name = name
        self.arguments = args


class _TC:
    def __init__(self, idx, call_id, name, args):
        self.index = idx
        self.id = call_id
        self.function = _TCFn(name, args)


class _FakeMsg:
    def __init__(self, content):
        self.content = content


class _FakeRespChoice:
    def __init__(self, content):
        self.message = _FakeMsg(content)


class _FakeResp:
    def __init__(self, content):
        self.choices = [_FakeRespChoice(content)]


class _FakeClient:
    """流式主循环调用（kw 里带 stream=True）按轮次吐 stream_rounds[i]；
    非流式调用（_forced_text_summary 用，没有 stream 参数）一律回 fallback_text。"""

    def __init__(self, stream_rounds, fallback_text=""):
        self.stream_rounds = stream_rounds
        self.fallback_text = fallback_text
        self._round = 0
        self.calls: list[dict] = []
        self.chat = type("C", (), {})()
        self.chat.completions = type("CC", (), {"create": self._create})()

    async def _create(self, **kw):
        self.calls.append(kw)
        if kw.get("stream"):
            chunks = self.stream_rounds[self._round]
            self._round += 1

            async def _gen():
                for c in chunks:
                    yield c
            return _gen()
        return _FakeResp(self.fallback_text)


# ── 1. _forced_text_summary 措辞随 used_tools 变化 ───────────────────────────
print("[1] _forced_text_summary 按 used_tools 措辞不同")


async def _t_wording():
    captured = []

    class _EchoClient:
        def __init__(self):
            self.chat = type("C", (), {})()

            async def create(**kw):
                captured.append(kw)
                return _FakeResp("总结文字")

            self.chat.completions = type("CC", (), {"create": staticmethod(create)})()

    out1 = await ctl_mod._forced_text_summary(_EchoClient(), "system", [], used_tools=True)
    nudge1 = captured[-1]["messages"][-1]["content"]
    check(out1 == "总结文字", "used_tools=True 时正常拿到总结")
    check("刚执行了工具" in nudge1, "used_tools=True → 措辞提工具结果")

    out2 = await ctl_mod._forced_text_summary(_EchoClient(), "system", [], used_tools=False)
    nudge2 = captured[-1]["messages"][-1]["content"]
    check(out2 == "总结文字", "used_tools=False 时同样正常拿到总结")
    check("刚执行了工具" not in nudge2 and "没有输出任何文字回复" in nudge2,
          "used_tools=False → 措辞不提工具，只要求直接回答")

asyncio.run(_t_wording())


# ── 2. 端到端：第一轮就交白卷（未调用任何工具）→ 自动补总结 ───────────────────
print("[2] 第一轮空补全（未用工具）→ 自动触发兜底总结")


async def _t_empty_first_round():
    ctl = ctl_mod.JarvisController(interactive=False)
    ctl.client = _FakeClient(
        stream_rounds=[[_Chunk(_Choice(_Delta(content=None), "stop"))]],
        fallback_text="这是补答的总结文字",
    )
    texts = []
    async for ev in ctl.chat("你好"):
        if ev.get("type") == "text":
            texts.append(ev.get("text", ""))
    full = "".join(texts)
    check("这是补答的总结文字" in full, "第一轮空补全也能触发兜底（此前只在用过工具后才触发）")
    check(ctl.client.calls[1].get("stream") is None or not ctl.client.calls[1].get("stream"),
          "兜底调用是非流式的（走 _forced_text_summary）")
    nudge = ctl.client.calls[1]["messages"][-1]["content"]
    check("没有输出任何文字回复" in nudge, "第一轮没用过工具 → 兜底措辞不提'工具'（used_tools=False 生效）")
    check(ctl.messages[-1]["role"] == "assistant" and ctl.messages[-1]["content"] == "这是补答的总结文字",
          "补答的内容正确写回历史（下一轮上下文里看得到）")

asyncio.run(_t_empty_first_round())


# ── 3. 端到端：兜底本身也交白卷 → 给可见提示，不再彻底静音 ────────────────────
print("[3] 兜底也失败时不再彻底静音")


async def _t_fallback_also_empty():
    ctl = ctl_mod.JarvisController(interactive=False)
    ctl.client = _FakeClient(
        stream_rounds=[[_Chunk(_Choice(_Delta(content=None), "stop"))]],
        fallback_text="",   # 兜底也是空——模拟系统性问题（不是偶发）
    )
    texts = []
    async for ev in ctl.chat("你好"):
        if ev.get("type") == "text":
            texts.append(ev.get("text", ""))
    full = "".join(texts)
    check(full.strip() != "", "兜底也失败时，用户依然能看到一条可见提示（不是彻底空白）")
    check("没能生成任何文字回复" in full, "提示措辞明确说明这一轮真的没有输出")
    check(ctl.messages[-1]["content"] == full, "这条提示也写回了历史（不是只在传输层显示、历史里留白）")

asyncio.run(_t_fallback_also_empty())


# ── 4. 端到端：reasoning_content 累积并回灌进 tool_calls 消息 ─────────────────
print("[4] reasoning_content 累积并回灌")

_PROBE_RESULT = {"n": 0}


async def _probe_tool() -> str:
    _PROBE_RESULT["n"] += 1
    return "探针结果"

registry.register_spec(registry.ToolSpec(
    name="_test_probe_reasoning", description="测试探针",
    input_schema={"type": "object", "properties": {}}, handler=_probe_tool))


async def _t_reasoning_roundtrip():
    ctl = ctl_mod.JarvisController(interactive=False)
    ctl.client = _FakeClient(stream_rounds=[
        # 第一轮：先来几段隐藏推理分片，再声明一次工具调用
        [
            _Chunk(_Choice(_Delta(reasoning_content="先想想…"), None)),
            _Chunk(_Choice(_Delta(reasoning_content="应该调用探针工具。"), None)),
            _Chunk(_Choice(_Delta(tool_calls=[
                _TC(0, "call_1", "_test_probe_reasoning", "{}")]), "tool_calls")),
        ],
        # 第二轮：工具结果消化完，正常收尾
        [_Chunk(_Choice(_Delta(content="完成。"), "stop"))],
    ])
    texts = []
    async for ev in ctl.chat("帮我查一下"):
        if ev.get("type") == "text":
            texts.append(ev.get("text", ""))
    check("".join(texts) == "完成。", "工具轮跑完后正常收尾")

    tool_call_msgs = [m for m in ctl.messages if m.get("role") == "assistant" and m.get("tool_calls")]
    check(len(tool_call_msgs) == 1, f"历史里有且只有一条 assistant(tool_calls) 消息（实际 {len(tool_call_msgs)}）")
    check(tool_call_msgs[0].get("reasoning_content") == "先想想…应该调用探针工具。",
          "两段 reasoning_content 分片被正确累积、原样带进了 assistant(tool_calls) 消息")

    # 反例：普通模型（无 reasoning_content 分片）不应该凭空多出这个键
    ctl2 = ctl_mod.JarvisController(interactive=False)
    ctl2.client = _FakeClient(stream_rounds=[
        [_Chunk(_Choice(_Delta(tool_calls=[
            _TC(0, "call_2", "_test_probe_reasoning", "{}")]), "tool_calls"))],
        [_Chunk(_Choice(_Delta(content="完成。"), "stop"))],
    ])
    async for _ in ctl2.chat("再来一次"):
        pass
    tool_call_msgs2 = [m for m in ctl2.messages if m.get("role") == "assistant" and m.get("tool_calls")]
    check("reasoning_content" not in tool_call_msgs2[0],
          "没有 reasoning_content 分片时不多加这个键（其它 provider 零行为影响）")

asyncio.run(_t_reasoning_roundtrip())

registry.unregister("_test_probe_reasoning") or registry._SPECS.pop("_test_probe_reasoning", None)


# ── 收尾 ──────────────────────────────────────────────────────────────────────
print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 项失败")
    sys.exit(1)
print("✅ test_auto_deepseek_empty_reply 全部通过")
sys.exit(0)
