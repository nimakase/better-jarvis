#!/usr/bin/env python3
"""统一 trace_id 贯穿 telemetry/workflow/self_iteration（任务 #17）——自动生成，绝不覆盖既有测试文件。

覆盖：
  1. core/trace：new_id 格式、scope 的设置/嵌套/自动恢复
  2. core/telemetry：init_db 幂等迁移出 trace_id 列；record() 自动读 scope 里的
     trace_id（未显式传参时）；显式传参优先于 scope；calls_by_trace 正确过滤
  3. core/workflow.run_workflow：未嵌套时自建 trace 且步骤内的工具调用能用
     该 trace_id 查到；已在某个 trace 里时复用外层 trace（不重新铸）；
     显式传 trace_id 时以它为准；跑完后外层 trace 环境不被污染
  4. core/self_review.run_cycle：跑的时候 core.trace.get() 确实处在一个
     "review_" 开头的 trace 里（用 model_fn 回调里断言，不依赖真跑出提案）
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="jarvis_trace_test_"))
config.MEMORY_DB_PATH = _TMP / "memory.db"

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)
        print(f"  ✗ {msg}")
    else:
        print(f"  ✓ {msg}")


# ── 1. core/trace ──────────────────────────────────────────────────────────────
print("[1] core/trace")
from core import trace  # noqa: E402

check(trace.get() == "", "顶层未 scope 时 get() 为空串")

id1 = trace.new_id("wf")
id2 = trace.new_id("wf")
check(id1.startswith("wf_") and id1 != id2, "new_id 带前缀且每次不同")
check(trace.new_id() and "_" in trace.new_id(), "不传前缀也能生成合法 id")

with trace.scope(id1):
    check(trace.get() == id1, "scope 内 get() 返回设置的值")
    with trace.scope(id2):
        check(trace.get() == id2, "嵌套 scope 内层生效")
    check(trace.get() == id1, "退出内层 scope 后恢复外层值")
check(trace.get() == "", "退出最外层 scope 后恢复为空串")


# ── 2. core/telemetry ────────────────────────────────────────────────────────
print("[2] core/telemetry 落盘 + 查询")
from core import telemetry  # noqa: E402

telemetry.init_db()
telemetry.init_db()  # 幂等迁移：跑两次不报错

from core.memory import _get_conn  # noqa: E402

with _get_conn() as conn:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(tool_calls)")}
check("trace_id" in cols, "tool_calls 表已迁移出 trace_id 列")

# 未 scope 时 → 落空串
telemetry.record("_test_tool_no_trace", True, 10)
with _get_conn() as conn:
    row = conn.execute("SELECT trace_id FROM tool_calls WHERE tool=? ORDER BY id DESC LIMIT 1",
                       ("_test_tool_no_trace",)).fetchone()
check(row["trace_id"] == "", "未 scope 时落盘 trace_id 为空串（零行为影响）")

# scope 内 → 自动带上
tid = trace.new_id("test")
with trace.scope(tid):
    telemetry.record("_test_tool_in_trace", True, 20)
    telemetry.record("_test_tool_in_trace_2", False, 30, error="出错了")
rows = telemetry.calls_by_trace(tid)
check(len(rows) == 2, "calls_by_trace 拿到这个 trace 下的两条记录")
check({r["tool"] for r in rows} == {"_test_tool_in_trace", "_test_tool_in_trace_2"},
      "两条记录的工具名都对")

# 显式传参优先于 scope
with trace.scope(tid):
    telemetry.record("_test_tool_explicit", True, 5, trace_id="explicit_override")
with _get_conn() as conn:
    row2 = conn.execute("SELECT trace_id FROM tool_calls WHERE tool=?",
                        ("_test_tool_explicit",)).fetchone()
check(row2["trace_id"] == "explicit_override", "显式传参优先于当前 scope")


# ── 3. core/workflow.run_workflow ────────────────────────────────────────────
print("[3] core/workflow.run_workflow")
from core import workflow as wf  # noqa: E402


async def _t_workflow():
    seen_trace_in_step = {}

    def step_fn(ctx):
        seen_trace_in_step["value"] = trace.get()
        telemetry.record("_test_wf_step_tool", True, 1)
        return {"ok": True}

    # 3a. 不嵌套 → 自建 trace，且步骤内工具调用能查到
    check(trace.get() == "", "跑之前顶层无 trace（前置条件）")
    run = await wf.run_workflow("_test_wf", [wf.Step("s1", step_fn)])
    check(run.trace_id.startswith("wf__test_wf_"), f"run.trace_id 自动铸出，前缀正确：{run.trace_id}")
    check(seen_trace_in_step["value"] == run.trace_id, "步骤函数执行期间 trace.get() 就是这个 run 的 trace_id")
    check(trace.get() == "", "跑完后顶层 trace 环境未被污染（自动恢复）")
    wf_rows = telemetry.calls_by_trace(run.trace_id)
    check(any(r["tool"] == "_test_wf_step_tool" for r in wf_rows),
          "telemetry.calls_by_trace(run.trace_id) 能查到步骤里发生的工具调用")

    # 3b. 已在某个 trace 里跑 workflow → 复用外层 trace，不重新铸
    outer_tid = trace.new_id("outer")
    with trace.scope(outer_tid):
        run2 = await wf.run_workflow("_test_wf_nested", [wf.Step("s1", step_fn)])
    check(run2.trace_id == outer_tid, "嵌套在已有 trace 里时复用外层 trace，不重新铸")

    # 3c. 显式传 trace_id → 以它为准
    run3 = await wf.run_workflow("_test_wf_explicit", [wf.Step("s1", step_fn)],
                                 trace_id="my_explicit_trace")
    check(run3.trace_id == "my_explicit_trace", "显式传 trace_id 时以它为准")

asyncio.run(_t_workflow())


# ── 4. core/self_review.run_cycle ────────────────────────────────────────────
print("[4] core/self_review.run_cycle")
from core import self_review as sr  # noqa: E402
from core.self_iteration import SelfIterator  # noqa: E402

_REPO = Path(tempfile.mkdtemp(prefix="jarvis_trace_review_repo_"))
(_REPO / "tests").mkdir()
(_REPO / "tests" / "run_all.py").write_text("import sys\nsys.exit(0)\n")


async def _t_run_cycle():
    seen = {}

    async def model_fn(prompt: str) -> str:
        seen["trace_during_model_call"] = trace.get()
        return "NONE"   # 无提案，最快路径跑完一轮

    check(trace.get() == "", "run_cycle 之前顶层无 trace（前置条件）")
    it = SelfIterator(repo=_REPO)
    result = await sr.run_cycle(model_fn, iterator=it, focus_source={}, module_map="")
    check(seen.get("trace_during_model_call", "").startswith("review_"),
          f"run_cycle 执行期间处在一个 review_ 开头的 trace 里：{seen.get('trace_during_model_call')!r}")
    check(trace.get() == "", "run_cycle 跑完后顶层 trace 环境未被污染")
    check(isinstance(result, dict) and "n_proposals" in result, "run_cycle 仍正常返回结果字典（零回归）")

asyncio.run(_t_run_cycle())


# ── 收尾 ──────────────────────────────────────────────────────────────────────
print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 项失败")
    sys.exit(1)
print("✅ test_auto_trace_id 全部通过")
sys.exit(0)
