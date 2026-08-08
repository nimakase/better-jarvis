#!/usr/bin/env python3
"""_compress_history 切点不能落在 tool_calls/tool 序列中间——自动生成，绝不覆盖既有测试文件。

背景：用户在本机把 JARVIS_LLM_PROVIDER 正式切到 deepseek 后，启动没多久就撞上
DeepSeek 官方 API 400 报错："Messages with role 'tool' must be a response to a
preceding message with 'tool_calls'"。根因是 core/controller.py._compress_history
原来用纯位置切片 `messages[-MAX_HISTORY_TURNS:]` 截历史——如果切点恰好落在一轮
"assistant(tool_calls) → tool 响应"序列中间，被切剩的 keep 部分开头会是一条孤零零
的 tool 消息，前面对应的 tool_calls 声明被压缩掉了。OpenRouter/Claude 此前似乎
容忍这种残缺，DeepSeek 官方 API 严格校验直接拒绝整个请求。

修复：切点若落在 tool 消息上，往前挪，直到落在非 tool 消息上——连带把发起那轮
调用的 assistant 消息一并保留，保证每条 tool 消息前面都有它的 tool_calls 声明。

覆盖：
  1. 构造一个"assistant(tool_calls,两个调用) → tool → tool"序列，让 MAX_HISTORY_TURNS
     的切点分别落在第一个 tool 消息、第二个 tool 消息上 —— 两种情况修复后的 keep
     都必须完整带上前面的 assistant(tool_calls) 消息，不出现孤零零的 tool 消息。
  2. 不落在 tool 序列中间时（正常切点），压缩行为与之前一致，不多切东西。
  3. 一般化校验：对修复后返回的整条消息链跑"tool_call_id 配对检查"（模拟
     DeepSeek/OpenAI 官方校验的逻辑），必须全部配对成功。
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


from core import controller as ctl  # noqa: E402


def _validate_tool_chain(messages: list) -> tuple[bool, str]:
    """模拟 OpenAI/DeepSeek 官方对 tool_calls/tool 配对的校验：每条 role=tool
    消息的 tool_call_id 必须出现在【它之前某条 assistant 消息声明的 tool_calls】里，
    且尚未被更早的同 id tool 消息消费过。不合规返回 (False, 原因)。"""
    pending: set[str] = set()
    for m in messages:
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                pending.add(tc["id"])
        elif role == "tool":
            tcid = m.get("tool_call_id")
            if tcid not in pending:
                return False, f"tool 消息（tool_call_id={tcid!r}）前面没有匹配的 tool_calls 声明"
            pending.discard(tcid)
    return True, ""


def _mk_messages() -> list:
    return [
        {"role": "user", "content": "第一条"},
        {"role": "assistant", "content": "回复1"},
        {"role": "user", "content": "第二条，帮我查点东西"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "f1", "arguments": "{}"}},
            {"id": "call_2", "type": "function", "function": {"name": "f2", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": "结果1"},
        {"role": "tool", "tool_call_id": "call_2", "content": "结果2"},
        {"role": "user", "content": "第三条"},
        {"role": "assistant", "content": "回复3"},
        {"role": "user", "content": "第四条"},
        {"role": "assistant", "content": "回复4"},
    ]


class _FakeResp:
    def __init__(self, text):
        self.choices = [type("C", (), {"message": type("M", (), {"content": text})()})()]


class _FakeClient:
    class chat:
        class completions:
            @staticmethod
            async def create(**kwargs):
                return _FakeResp("这是压缩摘要")


# ── 1. 切点落在 tool 消息序列中间 ────────────────────────────────────────────
print("[1] 切点落在 tool_calls/tool 序列中间时不产生孤零零的 tool 消息")


async def _t_boundary_in_tool_run():
    orig = config.MAX_HISTORY_TURNS
    try:
        msgs = _mk_messages()  # 10 条；索引 4/5 是两条 tool 消息
        for max_turns, label in [(6, "切点落在第一条 tool 消息上"),
                                  (5, "切点落在第二条 tool 消息上")]:
            config.MAX_HISTORY_TURNS = max_turns
            result = await ctl._compress_history(_FakeClient(), msgs, persist=False)
            ok, reason = _validate_tool_chain(result[1:])  # result[0] 是摘要消息，跳过
            check(ok, f"{label}（MAX_HISTORY_TURNS={max_turns}）→ 配对校验通过（{reason}）")
            # 更具体地断言：keep 部分（去掉摘要）第一条必须是那条 assistant(tool_calls)，
            # 不能是任何 tool 消息。
            check(result[1].get("role") != "tool",
                  f"{label} → keep 开头不是孤零零的 tool 消息（实际是 {result[1].get('role')!r}）")
    finally:
        config.MAX_HISTORY_TURNS = orig

asyncio.run(_t_boundary_in_tool_run())


# ── 2. 切点不落在 tool 序列中间时，行为与预期一致（不过度保留）───────────────
print("[2] 切点落在普通位置时不多切")


async def _t_boundary_normal():
    orig = config.MAX_HISTORY_TURNS
    try:
        msgs = _mk_messages()
        config.MAX_HISTORY_TURNS = 4   # 切点落在索引 6（普通 user 消息），不是 tool
        result = await ctl._compress_history(_FakeClient(), msgs, persist=False)
        check(result[1]["content"] == "第三条", "切点是普通消息时原样保留，不额外往前挪")
        ok, reason = _validate_tool_chain(result[1:])
        check(ok, f"配对校验依然通过（{reason}）")
    finally:
        config.MAX_HISTORY_TURNS = orig

asyncio.run(_t_boundary_normal())


# ── 3. 消息数不超限时完全不压缩（零行为影响的基线）───────────────────────────
print("[3] 消息数不超限 → 不压缩")


async def _t_no_compress_needed():
    orig = config.MAX_HISTORY_TURNS
    try:
        msgs = _mk_messages()
        config.MAX_HISTORY_TURNS = 100
        result = await ctl._compress_history(_FakeClient(), msgs, persist=False)
        check(result == msgs, "消息数远小于上限 → 原样返回，不触发摘要调用")
    finally:
        config.MAX_HISTORY_TURNS = orig

asyncio.run(_t_no_compress_needed())


# ── 收尾 ──────────────────────────────────────────────────────────────────────
print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 项失败")
    sys.exit(1)
print("✅ test_auto_compress_history_boundary 全部通过")
sys.exit(0)
