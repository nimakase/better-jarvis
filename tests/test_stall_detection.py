#!/usr/bin/env python3
"""卡循环检测（结果感知）—— 确定性单测：同调用+同结果才判卡死，结果变即算进展。

用假流式 client 驱动真实 controller.chat 循环，不联网。
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
# 本测试往真实 registry 注册一个内联探针 handler（模块名 __main__，跟这个脚本自己
# 共享），运行时权限闸（core/grants.check_tool）按模块名查授权表——__main__ 永远批不
# 到、也不该批（批了等于给任何直接执行的脚本开后门）。测试跑在隔离沙盒里不产生真实
# 副作用，不属于这道面向生产对话的闸该管的范围，直接关掉（2026-08-13，见项目记忆
# prospecting-permission-gate-collision.md）。
config.PERMISSION_ENFORCEMENT = False
if not getattr(config, "OPENROUTER_API_KEY", None):
    config.OPENROUTER_API_KEY = "test-key"

from core import registry, controller  # noqa: E402

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)
        print(f"  ✗ {msg}")
    else:
        print(f"  ✓ {msg}")


# ── 注册一个可控结果的探针工具 ───────────────────────────────────────────────
_STATE = {"mode": "same", "n": 0}


async def _loop_probe() -> str:
    _STATE["n"] += 1
    return "固定结果" if _STATE["mode"] == "same" else f"结果 #{_STATE['n']}"

registry.register_spec(registry.ToolSpec(
    name="loop_probe", description="测试探针", input_schema={"type": "object", "properties": {}},
    handler=_loop_probe))


# ── 假流式 client：每轮都调 loop_probe（同名同参），直到设定的停止轮次 ─────────
class _Delta:
    def __init__(self, content=None, tool_calls=None):
        self.content = content; self.tool_calls = tool_calls


class _Choice:
    def __init__(self, delta, finish):
        self.delta = delta; self.finish_reason = finish


class _Chunk:
    def __init__(self, choice):
        self.choices = [choice]


class _TCFn:
    def __init__(self, name, args):
        self.name = name; self.arguments = args


class _TC:
    def __init__(self):
        self.index = 0; self.id = "call_x"; self.function = _TCFn("loop_probe", "{}")


class _FakeClient:
    def __init__(self, stop_after):
        self.stop_after = stop_after
        self.round = 0
        self.chat = type("C", (), {})()
        self.chat.completions = type("CC", (), {"create": self._create})()

    async def _create(self, **kw):
        self.round += 1
        stop = self.round > self.stop_after
        async def _gen():
            if stop:
                yield _Chunk(_Choice(_Delta(content="完成。"), "stop"))
            else:
                yield _Chunk(_Choice(_Delta(tool_calls=[_TC()]), "tool_calls"))
        return _gen()


async def _drive(mode, stop_after):
    _STATE["mode"], _STATE["n"] = mode, 0
    ctl = controller.JarvisController(interactive=False)   # 非交互：跳过监督信号线程
    ctl.client = _FakeClient(stop_after=stop_after)
    texts = []
    async for ev in ctl.chat("go"):
        if ev.get("type") == "text":
            texts.append(ev.get("text", ""))
    return "".join(texts), _STATE["n"]


async def _main():
    # 1) 同调用 + 同结果：应在 stall 上限处被掐停（探针执行次数 ≈ 上限）
    text, calls = await _drive("same", stop_after=50)
    check("卡循环" in text, "同调用+同结果 → 触发卡循环保护并停")
    check(calls <= controller._TOOL_STALL_LIMIT + 1,
          f"卡死时探针只跑了 ~{controller._TOOL_STALL_LIMIT} 次（实际 {calls}），未跑飞")

    # 2) 同调用 + 每轮不同结果：视为有进展，绝不误触；跑满设定轮次正常收尾
    text2, calls2 = await _drive("progress", stop_after=8)
    check("卡循环" not in text2, "同调用但结果每轮不同 → 不误判为卡死（关键修复）")
    check(calls2 == 8, f"有进展时跑满全部 8 轮（实际 {calls2}）")
    check("完成" in text2, "有进展时正常收尾")


asyncio.run(_main())

# 清理注册表
registry.unregister("loop_probe") or registry._SPECS.pop("loop_probe", None)

print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 项失败")
    sys.exit(1)
print("✅ test_stall_detection 全部通过")
sys.exit(0)
