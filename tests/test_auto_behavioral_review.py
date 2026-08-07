#!/usr/bin/env python3
"""自建技能行为级验证闭环（任务 #11）——自动生成，绝不覆盖既有测试文件。

背景：结构验证（静态校验 + 隔离冒烟 import）只保证"能加载"，不保证"逻辑对"。
本轮加的 core.tool_builder._behavioral_review 不执行生成代码本身（未经验证的
代码有真实副作用风险），而是让模型对着代码+原始需求做一次针对性自查，专找
"结构验证抓不到、真跑起来才会错"的逻辑问题——这是行为级验证闭环的第一版。

覆盖：
  1. _behavioral_review：OK/CONCERNS 两种回复的解析、CONCERNS 最多截 3 条、
     非法严重度兜底、格式不对/空回复/调用异常都降级为 verdict=unknown（不阻断）
  2. 按 core.model_routing 的 "code_review" 用途路由模型（任务 #20 的路由表）
  3. _render_behavioral_review：ok/unknown 都不渲染任何内容；concerns 时按
     严重度渲染并带图标
  4. create_tool / edit_tool 的源码里确实接了这一步（连线检查）
"""
import asyncio
import os
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


from core import tool_builder as tb  # noqa: E402
import core.llm as llm_mod  # noqa: E402


class _FakeResp:
    def __init__(self, text):
        class _Choice:
            def __init__(self, t):
                class _Msg:
                    def __init__(self, t):
                        self.content = t
                self.message = _Msg(t)
        self.choices = [_Choice(text)]


def _make_fake_client(text, captured: list):
    class _FakeCompletions:
        async def create(self, **kwargs):
            captured.append(kwargs)
            return _FakeResp(text)

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    return _FakeClient()


# ── 1. _behavioral_review 解析行为 ─────────────────────────────────────────────
print("[1] _behavioral_review 解析")


async def _t_review():
    orig_get_client = llm_mod.get_client

    captured = []
    llm_mod.get_client = lambda *a, **kw: _make_fake_client("OK", captured)
    r_ok = await tb._behavioral_review("t1", "需求", "code")
    check(r_ok == {"verdict": "ok", "concerns": []}, "回复 OK → verdict=ok，无 concerns")

    captured.clear()
    llm_mod.get_client = lambda *a, **kw: _make_fake_client(
        "CONCERNS\nhigh|字段名可能不对\nmedium|没处理空列表\nlow|风格问题\nlow|第4条应被截断",
        captured)
    r_concerns = await tb._behavioral_review("t2", "需求", "code")
    check(r_concerns["verdict"] == "concerns", "回复 CONCERNS → verdict=concerns")
    check(len(r_concerns["concerns"]) == 3, f"最多保留 3 条，实际 {len(r_concerns['concerns'])}")
    check(r_concerns["concerns"][0] == {"severity": "high", "message": "字段名可能不对"},
          "第一条严重度和内容都解析正确")

    captured.clear()
    llm_mod.get_client = lambda *a, **kw: _make_fake_client(
        "CONCERNS\nweird_severity|严重度非法这条", captured)
    r_bad_sev = await tb._behavioral_review("t3", "需求", "code")
    check(r_bad_sev["concerns"][0]["severity"] == "low", "非法严重度兜底为 low（不确定就按最低级处理）")

    captured.clear()
    llm_mod.get_client = lambda *a, **kw: _make_fake_client("这是一段格式完全不对的回复", captured)
    r_malformed = await tb._behavioral_review("t4", "需求", "code")
    check(r_malformed == {"verdict": "unknown", "concerns": []}, "首行既不是 OK 也不是 CONCERNS → unknown")

    captured.clear()
    llm_mod.get_client = lambda *a, **kw: _make_fake_client("", captured)
    r_empty = await tb._behavioral_review("t5", "需求", "code")
    check(r_empty == {"verdict": "unknown", "concerns": []}, "空回复 → unknown")

    def _boom(*a, **kw):
        raise RuntimeError("模拟网络错误")
    llm_mod.get_client = _boom
    r_err = await tb._behavioral_review("t6", "需求", "code")
    check(r_err == {"verdict": "unknown", "concerns": []}, "调用异常 → unknown，不抛异常（不阻断造工具）")

    llm_mod.get_client = orig_get_client

asyncio.run(_t_review())


# ── 2. 模型路由（任务 #20 的路由表）──────────────────────────────────────────
print("[2] 按 code_review 用途路由模型")


async def _t_review_routing():
    orig_get_client = llm_mod.get_client
    orig_env = os.environ.get("JARVIS_SUBAGENT_MODEL_CODE_REVIEW")

    captured = []
    llm_mod.get_client = lambda *a, **kw: _make_fake_client("OK", captured)
    try:
        os.environ.pop("JARVIS_SUBAGENT_MODEL_CODE_REVIEW", None)
        await tb._behavioral_review("t7", "需求", "code")
        check(captured[0]["model"] == config.CLAUDE_MODEL, "未配置 code_review 路由 → 退回主模型")

        captured.clear()
        os.environ["JARVIS_SUBAGENT_MODEL_CODE_REVIEW"] = "some-strong-code-model"
        await tb._behavioral_review("t8", "需求", "code")
        check(captured[0]["model"] == "some-strong-code-model",
              "配置了 code_review 路由 → 用配置的模型，不是主模型")
    finally:
        llm_mod.get_client = orig_get_client
        if orig_env is None:
            os.environ.pop("JARVIS_SUBAGENT_MODEL_CODE_REVIEW", None)
        else:
            os.environ["JARVIS_SUBAGENT_MODEL_CODE_REVIEW"] = orig_env

asyncio.run(_t_review_routing())


# ── 3. _render_behavioral_review ──────────────────────────────────────────────
print("[3] _render_behavioral_review")

check(tb._render_behavioral_review({"verdict": "ok", "concerns": []}) == "",
      "verdict=ok → 不渲染任何内容")
check(tb._render_behavioral_review({"verdict": "unknown", "concerns": []}) == "",
      "verdict=unknown → 不渲染（复查本身失败不该制造'好像有问题'的误导）")

rendered = tb._render_behavioral_review({"verdict": "concerns", "concerns": [
    {"severity": "high", "message": "高危问题"},
    {"severity": "low", "message": "低危问题"},
]})
check("行为级复查" in rendered, "渲染带标题")
check("高危问题" in rendered and "低危问题" in rendered, "两条 concern 都被渲染")
check("🔴" in rendered and "·" in rendered, "不同严重度用不同图标")


# ── 4. create_tool / edit_tool 接线检查 ─────────────────────────────────────────
print("[4] create_tool/edit_tool 接线检查")
import inspect  # noqa: E402

src_create = inspect.getsource(tb.create_tool)
check("_behavioral_review" in src_create and "_render_behavioral_review" in src_create,
      "create_tool 的源码里接了行为级复查")

src_edit = inspect.getsource(tb.edit_tool)
check("_behavioral_review" in src_edit and "_render_behavioral_review" in src_edit,
      "edit_tool 的源码里同样接了行为级复查")


# ── 收尾 ──────────────────────────────────────────────────────────────────────
print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 项失败")
    sys.exit(1)
print("✅ test_auto_behavioral_review 全部通过")
sys.exit(0)
