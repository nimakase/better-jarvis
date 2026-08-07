#!/usr/bin/env python3
"""模型能力变化检测（任务 #19）——自动生成，绝不覆盖既有测试文件。

覆盖：
  1. 首次运行 → 只记基线，不报变化
  2. 模型没变 → 再次检查仍不报变化（幂等）
  3. 新获得能力 → gained 正确、命中的 capability_workaround 工具被点名复核
  4. 失去能力 → lost 正确
  5. describe_drift 的文案覆盖三种情形
  6. registry.tool(capability_workaround=...) 正确写入 ToolSpec；
     connectors/vision_tools.describe_image 已声明 capability_workaround="vision"
  7. connectors/capability_watch_tools.check_capability_drift 经 registry 注册且可调用
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="jarvis_capwatch_test_"))
config.DATA_DIR = _TMP  # 隔离状态文件，不污染真实 DATA_DIR

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)
        print(f"  ✗ {msg}")
    else:
        print(f"  ✓ {msg}")


from core import capability_watch as cw  # noqa: E402

_state_file = cw._state_path()


def _reset_state():
    if _state_file.exists():
        _state_file.unlink()


# ── 1. 首次运行 ────────────────────────────────────────────────────────────────
print("[1] 首次运行只记基线")
_reset_state()
orig_model = config.CLAUDE_MODEL
config.CLAUDE_MODEL = "deepseek-chat"  # 非视觉、非联网
r1 = cw.check_drift()
check(r1["first_run"] is True, "首次运行 first_run=True")
check(r1["changed"] is False, "首次运行不报变化")
check(_state_file.exists(), "首次运行后状态文件已落盘")
check("首次检测" in cw.describe_drift(r1), "首次运行的文案如实说明是记基线")


# ── 2. 模型没变 → 幂等 ─────────────────────────────────────────────────────────
print("[2] 模型未变 → 幂等不报变化")
r2 = cw.check_drift()
check(r2["first_run"] is False, "第二次调用 first_run=False")
check(r2["changed"] is False, "模型没变 → changed=False")
check("无变化" in cw.describe_drift(r2), "文案如实说明无变化")


# ── 3. 新获得能力 + workaround 工具点名 ────────────────────────────────────────
print("[3] 新获得能力 → 命中 capability_workaround 工具")
from core import registry  # noqa: E402

registry.register_spec(registry.ToolSpec(
    name="_test_vision_workaround_tool", description="x",
    input_schema={"type": "object", "properties": {}}, handler=lambda: "x",
    capability_workaround="vision",
))
registry.register_spec(registry.ToolSpec(
    name="_test_unrelated_tool", description="x",
    input_schema={"type": "object", "properties": {}}, handler=lambda: "x",
    capability_workaround="",
))

config.CLAUDE_MODEL = "deepseek-v4-flash"  # 内置能力表：vision=True
r3 = cw.check_drift()
check(r3["changed"] is True, "模型换成带视觉的 → changed=True")
check("vision" in r3["gained"], "gained 正确列出 vision")
check(r3["lost"] == [], "没有能力被失去")
tool_names = {w["tool"] for w in r3["workaround_tools_to_review"]}
check("_test_vision_workaround_tool" in tool_names, "capability_workaround='vision' 的工具被点名复核")
check("_test_unrelated_tool" not in tool_names, "无关工具不被误点名")
d3 = cw.describe_drift(r3)
check("新获得能力" in d3 and "vision" in d3, "文案提到新获得的能力")
check("_test_vision_workaround_tool" in d3, "文案里点名建议复核的工具")


# ── 4. 失去能力 ────────────────────────────────────────────────────────────────
print("[4] 失去能力")
config.CLAUDE_MODEL = "deepseek-chat"  # 换回非视觉
r4 = cw.check_drift()
check(r4["changed"] is True, "从带视觉换回不带视觉 → changed=True")
check("vision" in r4["lost"], "lost 正确列出 vision")
check(r4["gained"] == [], "没有新获得任何能力")
check(r4["workaround_tools_to_review"] == [], "失去能力时不触发复核提示（只在新获得时提示）")
d4 = cw.describe_drift(r4)
check("失去能力" in d4 and "vision" in d4, "文案提到失去的能力")


# ── 5. 再次不变 → 幂等 ─────────────────────────────────────────────────────────
print("[5] 变化后再次检查恢复幂等")
r5 = cw.check_drift()
check(r5["changed"] is False, "状态已落盘为最新，紧接着再查不再报变化")

config.CLAUDE_MODEL = orig_model


# ── 6. registry 层字段 ─────────────────────────────────────────────────────────
print("[6] registry capability_workaround 字段")
spec = registry._SPECS.get("_test_vision_workaround_tool")  # noqa: SLF001
check(spec is not None and spec.capability_workaround == "vision", "ToolSpec 正确保留 capability_workaround")

import connectors.vision_tools as vt  # noqa: E402, F401
vspec = registry._SPECS.get("describe_image")  # noqa: SLF001
check(vspec is not None and vspec.capability_workaround == "vision",
      "describe_image 已声明 capability_workaround='vision'（真实生产工具，非测试桩）")


@registry.tool("_test_decorator_workaround", "x", capability_workaround="online_search")
async def _t_deco():
    return "x"

check(registry._SPECS["_test_decorator_workaround"].capability_workaround == "online_search",
      "@tool(capability_workaround=...) 装饰器参数正确写入")


# ── 7. connectors/capability_watch_tools 注册且可调用 ──────────────────────────
print("[7] capability_watch_tools 注册")
import connectors.capability_watch_tools as cwt  # noqa: E402

handler = registry.get_handler("check_capability_drift")
check(handler is cwt.check_capability_drift, "check_capability_drift 已注册且指向正确函数")


async def _t_tool_call():
    out = await handler()
    check(isinstance(out, str) and len(out) > 0, "工具调用返回非空文案")

asyncio.run(_t_tool_call())


# ── 收尾 ──────────────────────────────────────────────────────────────────────
print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 项失败")
    sys.exit(1)
print("✅ test_auto_capability_watch 全部通过")
sys.exit(0)
