#!/usr/bin/env python3
"""工具耗时分类标签 + 强制超时机制（任务 #14）——自动生成，绝不覆盖既有测试文件。

覆盖：
  1. core/tool_timeout.timeout_of() 的三层优先级（注册声明 > 内置表 > 15s 默认值）
  2. core/registry.tool() 装饰器的 duration= 参数正确换算成 timeout_s
  3. controller._execute_tool 端到端强制超时：卡住的工具不再冻住整条对话，
     超时会被当一次工具失败记进 telemetry，且给模型一句可读、指向 spawn_subtask 的建议
  4. UNBOUNDED（None）工具不会被腰斩；正常工具不受影响（零行为变化）
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="jarvis_timeout_test_"))
config.MEMORY_DB_PATH = _TMP / "memory.db"

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)
        print(f"  ✗ {msg}")
    else:
        print(f"  ✓ {msg}")


# ── 1. timeout_of() 优先级 ────────────────────────────────────────────────────
print("[1] tool_timeout.timeout_of() 优先级")
from core import tool_timeout as tt  # noqa: E402
from core import registry  # noqa: E402

check(tt.timeout_of("_从没注册过的工具名_") == tt.DEFAULT_TIMEOUT_S,
      f"未知工具 → 默认 {tt.DEFAULT_TIMEOUT_S}s")
check(tt.DEFAULT_TIMEOUT_S == 15.0, "用户已确认的默认同步超时值就是 15s")
check(tt.timeout_of("web_search") == 60.0, "内置表：web_search 标 slow(60s)")
check(tt.timeout_of("run_self_review") is None, "内置表：run_self_review 标 unbounded(None)")

registry.register_spec(registry.ToolSpec(
    name="_test_explicit_timeout", description="x", input_schema={"type": "object", "properties": {}},
    handler=lambda: "x", timeout_s=5,
))
check(tt.timeout_of("_test_explicit_timeout") == 5, "注册声明的 timeout_s 优先于内置表/默认值")

registry.register_spec(registry.ToolSpec(
    name="_test_explicit_unbounded", description="x", input_schema={"type": "object", "properties": {}},
    handler=lambda: "x", timeout_s=None,
))
check(tt.timeout_of("_test_explicit_unbounded") is None, "注册声明 timeout_s=None → 不设超时")


# ── 2. registry.tool() 装饰器 duration= 参数 ──────────────────────────────────
print("[2] @tool(duration=...) 换算")


@registry.tool("_test_duration_slow", "x", duration="slow")
async def _t_slow():
    return "x"


@registry.tool("_test_duration_unbounded", "x", duration="unbounded")
async def _t_unbounded():
    return "x"


@registry.tool("_test_duration_default", "x")
async def _t_default():
    return "x"


check(tt.timeout_of("_test_duration_slow") == 60.0, "duration='slow' → 60s")
check(tt.timeout_of("_test_duration_unbounded") is None, "duration='unbounded' → None")
check(tt.timeout_of("_test_duration_default") == tt.DEFAULT_TIMEOUT_S,
      "未传 duration → 落到默认值（未声明，非显式 unbounded）")


# ── 3~4. controller._execute_tool 端到端 ──────────────────────────────────────
print("[3] controller._execute_tool 强制超时端到端")
from core import controller as ctrl  # noqa: E402
from core.memory import _get_conn  # noqa: E402
from core import telemetry  # noqa: E402

telemetry.init_db()


async def _hangs_forever():
    await asyncio.sleep(999)
    return "永远不该跑到这里"


async def _fast_ok():
    return "秒回"


registry.register_spec(registry.ToolSpec(
    name="_test_hang_short_timeout", description="x", input_schema={"type": "object", "properties": {}},
    handler=_hangs_forever, timeout_s=0.3,
))
registry.register_spec(registry.ToolSpec(
    name="_test_fast_tool", description="x", input_schema={"type": "object", "properties": {}},
    handler=_fast_ok, timeout_s=0.3,
))


def _last_telemetry_row(tool_name: str) -> dict:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM tool_calls WHERE tool = ? ORDER BY id DESC LIMIT 1", (tool_name,)
        ).fetchone()
    return dict(row) if row else {}


async def _t_controller_timeout():
    result = await ctrl._execute_tool("_test_hang_short_timeout", {})
    check("⏱️" in result.text, "超时结果文本带提示符号")
    check("spawn_subtask" in result.text, "超时提示建议改用 spawn_subtask 派发到后台")
    row = _last_telemetry_row("_test_hang_short_timeout")
    check(row.get("ok") == 0, "telemetry 把超时记为失败（ok=0）")
    check(row.get("error") == "timeout", "telemetry 的 error 字段标记为 timeout")

    result2 = await ctrl._execute_tool("_test_fast_tool", {})
    check(result2.text == "秒回", "正常工具（在超时之内完成）不受影响，结果原样返回")
    row2 = _last_telemetry_row("_test_fast_tool")
    check(row2.get("ok") == 1, "正常完成的工具 telemetry 记成功")

asyncio.run(_t_controller_timeout())


print("[4] UNBOUNDED 工具不被腰斩")


async def _slow_but_finishes():
    await asyncio.sleep(0.5)
    return "跑完了"


registry.register_spec(registry.ToolSpec(
    name="_test_unbounded_slow", description="x", input_schema={"type": "object", "properties": {}},
    handler=_slow_but_finishes, timeout_s=None,
))


async def _t_unbounded_not_cut():
    result = await ctrl._execute_tool("_test_unbounded_slow", {})
    check(result.text == "跑完了", "timeout_s=None 的工具即便比默认 15s 档慢也能跑完，不被腰斩")

asyncio.run(_t_unbounded_not_cut())


# ── 收尾 ──────────────────────────────────────────────────────────────────────
print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 项失败")
    sys.exit(1)
print("✅ test_auto_tool_timeout 全部通过")
sys.exit(0)
