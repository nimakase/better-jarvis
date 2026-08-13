#!/usr/bin/env python3
"""工程改动工作结构重设的 controller.py 侧护栏 —— 自动生成，绝不覆盖既有测试文件。

背景：2026-08-09 复盘一次"冷启动分批工作流"开发事故，定位到 core/controller.py
里几个跟"复杂任务怎么落地成代码"相关的结构性缺口，这次一并补上：
  1. 执行前生成完整性校验：工具调用参数 JSON 解析失败（常见于长生成被截断）时，
     此前会静默降级成 {} 继续执行——实测 write_open_file 因此被空内容调用过。
     改为判定该次调用失败、不执行，写一条清晰的 tool 消息提示重试。
  2. content 通道特殊符号过滤：模型退化成把工具调用协议符号（如 DeepSeek 的
     `<｜tool▁calls▁begin｜>`）当普通文字写进 content 时，此前会原样透传给
     用户。改为检测到特征符号即停止透传、清空本轮内容，走"交白卷"兜底同一条路。
  3. 分场景 token 预算：engineering 组激活时用更宽的 max_tokens
     （config.MAX_TOKENS_ENGINEERING），其余对话轮仍用默认值。
  4. 确认模型重设：write_open_file/patch_open_file/append_to_file 从"后台一律
     屏蔽"改成"计划已被交互式会话确认才放行"（借鉴商业 agent"批一次、之后
     自主执行"的模式）；propose/execute/finalize/abandon 仍然一律后台屏蔽。

覆盖：
  1. _LEAKED_TOKEN_RE 命中已知泄漏特征、不误伤正常中英文/代码。
  2. 端到端：content 通道泄漏 → 用户收不到泄漏文字，走兜底总结。
  3. 端到端：工具调用参数 JSON 损坏 → 不执行、报错提示，不是空参数硬跑。
  4. 端到端：active_groups 含 engineering → 用 MAX_TOKENS_ENGINEERING；不含则用默认值。
  5. _plan_write_allowed()：未确认/不存在的 plan_id 拒绝，已确认的放行。
  6. 端到端：后台会话对未确认计划调用 write_open_file → 拒绝且不落盘；
     确认后同样调用 → 放行（通过 stub handler 观察是否真的被执行）。
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
# 本测试往真实 registry 注册内联桩 handler（模块名 __main__，跟这个脚本自己共享），
# 运行时权限闸（core/grants.check_tool）按模块名查授权表——__main__ 永远批不到、也不
# 该批（批了等于给任何直接执行的脚本开后门）。测试跑在隔离沙盒里不产生真实副作用，
# 不属于这道面向生产对话的闸该管的范围，直接关掉（2026-08-13，见项目记忆
# prospecting-permission-gate-collision.md）。
config.PERMISSION_ENFORCEMENT = False
if not getattr(config, "OPENROUTER_API_KEY", None):
    config.OPENROUTER_API_KEY = "test-key"

from core import registry, engineering, controller as ctl_mod  # noqa: E402

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)
        print(f"  ✗ {msg}")
    else:
        print(f"  ✓ {msg}")


# ── 假流式对象（沿用 test_auto_deepseek_empty_reply.py 的既有模式）────────────
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


# ── 1. _LEAKED_TOKEN_RE：命中已知泄漏特征 / 不误伤正常内容 ─────────────────────
print("[1] _LEAKED_TOKEN_RE 特征匹配")
LEAK_SAMPLES = [
    '<｜｜DSML｜｜tool_calls>',
    '<｜｜DSML｜｜invoke name="write_open_file">',
    '<|tool▁calls▁begin|>',
]
for s in LEAK_SAMPLES:
    check(ctl_mod._LEAKED_TOKEN_RE.search(s) is not None, f"命中泄漏特征：{s!r}")

SAFE_SAMPLES = [
    "今天天气不错，你有 <3 件事要处理。",
    "if a < b and b > c: return True",
    "他说 <这是引用> 之类的话",
    "正常回复文字，不含任何特殊符号。",
]
for s in SAFE_SAMPLES:
    check(ctl_mod._LEAKED_TOKEN_RE.search(s) is None, f"不误伤正常内容：{s!r}")


# ── 2. 端到端：content 通道泄漏 → 用户收不到泄漏文字，走兜底 ────────────────────
print("[2] content 泄漏 → 停止透传 + 走兜底")


async def _t_content_leak():
    ctl = ctl_mod.JarvisController(interactive=False)
    ctl.client = _FakeClient(
        stream_rounds=[[
            _Chunk(_Choice(_Delta(content='<｜｜DSML｜｜tool_calls><｜｜DSML｜｜invoke name="x">'), "stop")),
        ]],
        fallback_text="已重新生成的正常总结",
    )
    texts = []
    async for ev in ctl.chat("帮我写个大文件"):
        if ev.get("type") == "text":
            texts.append(ev.get("text", ""))
    full = "".join(texts)
    check("DSML" not in full and "｜" not in full, "泄漏的协议符号没有出现在用户看到的文字里")
    check("已重新生成的正常总结" in full, "改为走兜底总结，不是彻底静音")
    check(ctl.messages[-1]["content"] == full, "写回历史的是兜底文本，不是泄漏出的半成品符号")

asyncio.run(_t_content_leak())


# ── 3. 端到端：工具调用参数 JSON 损坏 → 不执行、报错提示 ────────────────────────
print("[3] 参数解析失败 → 不执行，不当空参数硬跑")

_PROBE_CALLS = {"n": 0}


async def _probe(**kwargs) -> str:
    _PROBE_CALLS["n"] += 1
    return f"探针被调用，参数={kwargs}"

registry.register_spec(registry.ToolSpec(
    name="_test_probe_truncated", description="测试探针",
    input_schema={"type": "object", "properties": {"path": {"type": "string"},
                                                    "content": {"type": "string"}}},
    handler=_probe, origin="skill"))  # origin="skill"：保证下面能被 unregister() 干净注销


async def _t_truncated_args():
    ctl = ctl_mod.JarvisController(interactive=False)
    ctl.client = _FakeClient(stream_rounds=[
        # 参数 JSON 明显没收尾（截断的典型症状）
        [_Chunk(_Choice(_Delta(tool_calls=[
            _TC(0, "call_1", "_test_probe_truncated",
                '{"path": "foo.py", "content": "def f():\\n    return 1\\n')]), "tool_calls"))],
        [_Chunk(_Choice(_Delta(content="收到。"), "stop"))],
    ])
    async for _ in ctl.chat("写个文件"):
        pass
    check(_PROBE_CALLS["n"] == 0, "参数解析失败时探针工具没有被真的执行")
    tool_msgs = [m for m in ctl.messages if m.get("role") == "tool"]
    check(any("参数解析失败" in m.get("content", "") for m in tool_msgs),
          "历史里留下了清晰的解析失败提示，而不是假装成功")

asyncio.run(_t_truncated_args())
check(registry.unregister("_test_probe_truncated"), "测试探针清理干净（含 _ORDER，不留给后续用例）")


# ── 4. 端到端：分场景 token 预算 ────────────────────────────────────────────────
print("[4] engineering 组激活时用更宽的 max_tokens")


async def _t_token_budget():
    ctl = ctl_mod.JarvisController(interactive=True)
    ctl.client = _FakeClient(stream_rounds=[[_Chunk(_Choice(_Delta(content="ok"), "stop"))]])
    async for _ in ctl.chat("随便聊聊"):
        pass
    check(ctl.client.calls[0]["max_tokens"] == config.MAX_TOKENS_RESPONSE,
          "未激活 engineering 组时用默认 max_tokens")

    ctl2 = ctl_mod.JarvisController(interactive=True)
    ctl2.active_groups.add("engineering")
    ctl2.client = _FakeClient(stream_rounds=[[_Chunk(_Choice(_Delta(content="ok"), "stop"))]])
    async for _ in ctl2.chat("帮我改代码"):
        pass
    check(ctl2.client.calls[0]["max_tokens"] == config.MAX_TOKENS_ENGINEERING,
          "激活 engineering 组后用更宽的 MAX_TOKENS_ENGINEERING")
    check(config.MAX_TOKENS_ENGINEERING > config.MAX_TOKENS_RESPONSE,
          "工程预算确实比默认预算更宽（否则分档没有意义）")

asyncio.run(_t_token_budget())


# ── 5. _plan_write_allowed()：未确认/不存在拒绝，已确认放行 ────────────────────
print("[5] _plan_write_allowed 判定")
plan = engineering.propose("护栏测试计划", ["foo.py"])
ok, msg = ctl_mod._plan_write_allowed("write_open_file", {"plan_id": plan.plan_id})
check(not ok and "未确认" in msg, "未确认的 plan_id 拒绝")
ok, msg = ctl_mod._plan_write_allowed("write_open_file", {"plan_id": "不存在的id"})
check(not ok, "不存在的 plan_id 拒绝")
engineering.confirm(plan.plan_id)
ok, msg = ctl_mod._plan_write_allowed("write_open_file", {"plan_id": plan.plan_id})
check(ok, "已确认的 plan_id 放行")
ok, msg = ctl_mod._plan_write_allowed("write_open_file", {})
check(not ok, "缺 plan_id 时拒绝（不当成合法调用）")


# ── 6. 端到端：后台会话按计划确认状态放行/拒绝写工具 ────────────────────────────
print("[6] 后台会话：write_open_file 是否放行取决于 plan 是否已确认")

_WRITE_CALLS = []


async def _stub_write(plan_id: str = "", path: str = "", content: str = "") -> str:
    _WRITE_CALLS.append((plan_id, path))
    return "写入桩函数被调用"


import connectors.engineering_tools as _et  # noqa: E402,F401 触发@tool注册（确保原始spec已登记）
_ORIGINAL_WRITE_SPEC = registry._SPECS.get("write_open_file")
registry.register_spec(registry.ToolSpec(
    name="write_open_file", description="桩替换，仅测试用",
    input_schema={"type": "object", "properties": {
        "plan_id": {"type": "string"}, "path": {"type": "string"}, "content": {"type": "string"}}},
    handler=_stub_write), replace=True)


async def _t_background_plan_gate():
    plan_unconfirmed = engineering.propose("未确认计划", ["foo.py"])
    ctl = ctl_mod.JarvisController(interactive=False)
    ctl.client = _FakeClient(stream_rounds=[
        [_Chunk(_Choice(_Delta(tool_calls=[
            _TC(0, "call_a", "write_open_file",
                f'{{"plan_id": "{plan_unconfirmed.plan_id}", "path": "foo.py", "content": "x=1"}}')]),
            "tool_calls"))],
        [_Chunk(_Choice(_Delta(content="完成。"), "stop"))],
    ])
    async for _ in ctl.chat("后台写文件"):
        pass
    check(len(_WRITE_CALLS) == 0, "未确认计划：后台调用 write_open_file 被拒绝，桩函数没被真的调用")
    tool_msgs = [m for m in ctl.messages if m.get("role") == "tool"]
    check(any("未确认" in m.get("content", "") for m in tool_msgs), "拒绝理由写入了历史")

    plan_confirmed = engineering.propose("已确认计划", ["foo.py"])
    engineering.confirm(plan_confirmed.plan_id)
    ctl2 = ctl_mod.JarvisController(interactive=False)
    ctl2.client = _FakeClient(stream_rounds=[
        [_Chunk(_Choice(_Delta(tool_calls=[
            _TC(0, "call_b", "write_open_file",
                f'{{"plan_id": "{plan_confirmed.plan_id}", "path": "foo.py", "content": "x=1"}}')]),
            "tool_calls"))],
        [_Chunk(_Choice(_Delta(content="完成。"), "stop"))],
    ])
    async for _ in ctl2.chat("后台写文件"):
        pass
    check(len(_WRITE_CALLS) == 1, "已确认计划：后台调用 write_open_file 放行，桩函数被真的调用了")

asyncio.run(_t_background_plan_gate())

if _ORIGINAL_WRITE_SPEC is not None:
    registry.register_spec(_ORIGINAL_WRITE_SPEC, replace=True)  # 还原，不污染后续测试文件


# ── 收尾 ──────────────────────────────────────────────────────────────────────
print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 项失败")
    sys.exit(1)
print("✅ test_auto_engineering_guardrails 全部通过")
sys.exit(0)
