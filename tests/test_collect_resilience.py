#!/usr/bin/env python3
"""采集截断韧性 + 最终必答兜底 —— 确定性单测。"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)
        print(f"  ✗ {msg}")
    else:
        print(f"  ✓ {msg}")


# ── 1. 采集：截断输出仍能抢救大部分信号 ──────────────────────────────────────
print("[1] 采集截断韧性")
from intel import workflow_defs as wd  # noqa: E402

# 模拟：模型输出了 3 条完整信号 + 1 条被传输截断的尾巴
TRUNCATED = (
    '[\n'
    '{"signal_type": "price", "scope": "sector", "title": "MLCC 涨价", "direction": "up"},\n'
    '{"signal_type": "supply", "scope": "sector", "title": "晶圆减产", "direction": "down"},\n'
    '{"signal_type": "event", "scope": "company", "title": "某厂停线", "direc'
)

orig = wd._completion_text


async def fake_completion(prompt, retries=1):
    return TRUNCATED

wd._completion_text = fake_completion
try:
    steps = wd._build_signal_collection()
    collect_step = steps[0]
    signals = asyncio.run(collect_step.fn({}))
    check(len(signals) == 2, f"截断输出抢救出完整的 2 条（实际 {len(signals)}），不再整批归零")
    check(signals[0]["title"] == "MLCC 涨价", "抢救内容正确")

    # 空输出 → 明确报错（不静默成功）
    async def empty_completion(prompt, retries=1):
        return "抱歉，我没能检索到内容。"

    wd._completion_text = empty_completion
    try:
        asyncio.run(collect_step.fn({}))
        check(False, "空信号应报错")
    except RuntimeError as e:
        check("未解析出任何信号" in str(e), "空信号明确报错（不静默）")
finally:
    wd._completion_text = orig


# ── 2. _completion_text 本体：流中断不炸、空手才重试 ─────────────────────────
print("[2] 流式累积")


class FakeDelta:
    def __init__(self, content):
        self.content = content


class FakeChoice:
    def __init__(self, content):
        self.delta = FakeDelta(content)


class FakeChunk:
    def __init__(self, content):
        self.choices = [FakeChoice(content)]


class BrokenStream:
    """吐两个分片后模拟传输中断。"""

    def __init__(self):
        self.sent = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        self.sent += 1
        if self.sent == 1:
            return FakeChunk('[{"a": 1}, ')
        if self.sent == 2:
            return FakeChunk('{"b": 2}]')
        raise ConnectionError("传输中断")


class FakeCompletions:
    def __init__(self, outer):
        self.outer = outer

    async def create(self, **kw):
        self.outer.calls += 1
        return BrokenStream()


class FakeClient:
    def __init__(self):
        self.calls = 0
        self.chat = type("C", (), {})()
        self.chat.completions = FakeCompletions(self)


async def _test_stream():
    import openai
    fake = FakeClient()
    orig_cls = openai.AsyncOpenAI
    openai.AsyncOpenAI = lambda **kw: fake
    # workflow_defs 里是 from openai import AsyncOpenAI（函数内 import），patch 模块属性即可
    try:
        text = await wd._completion_text("prompt", retries=1)
        check('{"b": 2}]' in text, "流中断前已累积的内容保留返回")
        check(fake.calls == 1, "有内容就不重试（截断≠失败）")
    finally:
        openai.AsyncOpenAI = orig_cls


asyncio.run(_test_stream())


# ── 3. 最终必答兜底 ───────────────────────────────────────────────────────────
print("[3] 最终必答")
import config  # noqa: E402
if not getattr(config, "OPENROUTER_API_KEY", None):
    config.OPENROUTER_API_KEY = "test-key"
import os  # noqa: E402
for _v in ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy",
           "HTTP_PROXY", "http_proxy"):
    os.environ.pop(_v, None)
from core import controller as ctl_mod  # noqa: E402


class SummaryClient:
    """假 client：返回一段总结文本。"""

    def __init__(self, content):
        self._content = content
        self.chat = type("C", (), {})()

        async def create(**kw):
            msg = type("M", (), {"content": self._content})()
            choice = type("Ch", (), {"message": msg})()
            return type("R", (), {"choices": [choice]})()

        self.chat.completions = type("CC", (), {"create": staticmethod(create)})()


async def _test_fallback():
    out = await ctl_mod._forced_text_summary(
        SummaryClient("工具报了超时错，建议重试。"), "system", [])
    check(out == "工具报了超时错，建议重试。", "白卷时强制补总结")

    class ExplodingClient(SummaryClient):
        def __init__(self):
            super().__init__("")
            self.chat = type("C", (), {})()

            async def create(**kw):
                raise RuntimeError("连模型都挂了")

            self.chat.completions = type("CC", (), {"create": staticmethod(create)})()

    out2 = await ctl_mod._forced_text_summary(ExplodingClient(), "system", [])
    check(out2 == "", "兜底自身失败返回空串（不拖垮回合）")


asyncio.run(_test_fallback())

# ── 收尾 ──────────────────────────────────────────────────────────────────────
print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 项失败")
    sys.exit(1)
print("✅ test_collect_resilience 全部通过")
sys.exit(0)
