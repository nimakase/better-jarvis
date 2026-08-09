"""connectors/engineering_tools.py — 计划模式对话入口（PROTECTED，元能力）

暴露"执行一个人已经想清楚、内容已确定的多文件改动"这条通道。跟
`core/tool_builder.py`（造自建技能，只能写 skills/ 沙箱单文件）和
`core/self_review.py`/`self_iteration.py`（自主发现缺陷、生成提案、先红后绿
验证）都不是同一条路——那两条服务于"内容不确定，需要生成/验证"；这条服务于
"用户/Claude 在对话里已经把改动内容想清楚了，贾维斯只需要安全落地"。

设计详见 core/engineering.py 顶部注释；这里只管对话层接线。护栏摘要：
  - `propose_engineering_change` 只登记不写文件，READ_LOCAL，不需要确认。
  - `execute_engineering_change` 效应等级 WRITE_EXTERNAL，过 core.effects
    的 ConfirmGate——同一个 plan_id 首次调用会被机械拦下（模型把完整计划
    文本复述给用户看），用户确认后模型原样再调一次才真正解锁。**一次确认
    覆盖整个多文件计划**，不是逐文件确认。
  - `write_open_file` 只能写"已批准计划"登记过的文件路径，且必须是
    `self_model` 判定为 OPEN 的路径（PROTECTED 一律拒绝，不生成二次审核
    提案——真要碰 PROTECTED 不是这条通道的职责）。
  - `finalize_engineering_change` 跑全量测试 gate，绿才提交；红则整个计划
    涉及的文件一次性回滚，不留半成品。
  - `abandon_engineering_change` 供用户中途改主意时用：回滚已写入的部分、
    作废计划。
  - 这五个工具在后台/定时/子agent实例里一律屏蔽（见 core/controller.py 的
    BACKGROUND_BLOCKED_TOOLS）——计划模式的确认闸依赖"用户看得到、能回话"
    这个前提，后台场景没有这个前提；子agent默认白名单本来就只有只读工具，
    这五个 write_external/write_local 级工具天然不在其中，无需额外过滤。
"""
from __future__ import annotations

from core import effects
from core import engineering
from core.registry import tool

GROUP = "engineering"


@tool(
    "propose_engineering_change",
    "登记一个多文件工程改动计划（不写任何文件）。用于「用户已经把改动内容想清楚、"
    "点名了具体文件」的场景——不要用来处理不确定该怎么改的问题（那种情况直接改代码"
    "讨论清楚，或走 run_self_review 的自主反思闭环）。返回渲染好的计划文本（含每个"
    "文件是否可写的预检结果），登记后用 execute_engineering_change(plan_id) 解锁写入。",
    {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "这个计划要做什么，一句话"},
            "files": {"type": "array", "items": {"type": "string"},
                      "description": "计划涉及的文件路径清单（相对仓库根），如 ['core/foo.py', 'tests/test_auto_foo.py']"},
            "rationale": {"type": "string", "description": "为什么要这么改（可选）"},
        },
        "required": ["summary", "files"],
    },
    group=GROUP, effect=effects.READ_LOCAL,
)
def propose_engineering_change(summary: str, files: list, rationale: str = "") -> str:
    plan = engineering.propose(summary, files, rationale)
    return engineering.render_plan(plan)


@tool(
    "execute_engineering_change",
    "解锁一个已登记计划的写入权限。首次调用会被安全闸拦下——把计划完整内容复述给"
    "用户看，等用户明确确认后，用【完全相同的 plan_id】再调一次才真正解锁。解锁后"
    "才能对该计划里的文件调用 write_open_file。",
    {
        "type": "object",
        "properties": {"plan_id": {"type": "string", "description": "propose_engineering_change 返回的计划 ID"}},
        "required": ["plan_id"],
    },
    group=GROUP, effect=effects.WRITE_EXTERNAL,
)
def execute_engineering_change(plan_id: str) -> str:
    plan = engineering.get(plan_id)
    if plan is None:
        return f"计划 {plan_id} 不存在，请先调用 propose_engineering_change 登记。"
    if plan.status == "applied":
        return f"计划 {plan_id} 已经落地过了，不要重复执行；如需再改请开一个新计划。"
    engineering.confirm(plan_id)
    return (f"计划 {plan_id} 已解锁。可以开始对清单里未被标记为「不可写」的文件逐个调用 "
           f"write_open_file(plan_id, path, content)；全部写完、且都过了你自己的检查后，"
           f"调用 finalize_engineering_change(plan_id) 跑全量测试并收尾。")


@tool(
    "write_open_file",
    "把内容写入一个已批准计划里登记过的文件（原子写入，首次写入前自动做快照，"
    "供计划失败时整体回滚）。只能写 propose 时列出的路径，且该路径必须不受保护——"
    "受保护路径会被拒绝，不能绕开。",
    {
        "type": "object",
        "properties": {
            "plan_id": {"type": "string"},
            "path": {"type": "string", "description": "相对仓库根的文件路径，须在计划清单内"},
            "content": {"type": "string", "description": "该文件写入后的【完整】内容"},
        },
        "required": ["plan_id", "path", "content"],
    },
    group=GROUP, effect=effects.WRITE_LOCAL,
)
def write_open_file(plan_id: str, path: str, content: str) -> str:
    ok, msg = engineering.write_file(plan_id, path, content)
    return msg


@tool(
    "run_repo_test",
    "跑一个测试脚本看结果（仅限 tests/run_all.py 或计划里新建的 tests/test_auto_*.py，"
    "不是通用脚本执行器）。可以在 finalize 之前先自查用的。",
    {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "测试脚本路径，默认 tests/run_all.py",
                                 "default": "tests/run_all.py"}},
        "required": [],
    },
    group=GROUP, effect=effects.READ_LOCAL, duration="unbounded",
)
def run_repo_test(path: str = "tests/run_all.py") -> str:
    ok, out = engineering.run_script(path or "tests/run_all.py")
    status = "✅ 通过" if ok else "✖ 未通过"
    return f"{status}\n{out[-2000:]}"


@tool(
    "finalize_engineering_change",
    "收尾一个执行中的计划：跑全量测试 gate（tests/run_all.py），通过才 git 提交并"
    "标记计划完成；不通过则把该计划涉及的全部文件一次性回滚，不留半成品。",
    {
        "type": "object",
        "properties": {
            "plan_id": {"type": "string"},
            "commit": {"type": "boolean", "description": "gate 通过后是否 git 提交，默认 true", "default": True},
        },
        "required": ["plan_id"],
    },
    group=GROUP, effect=effects.WRITE_LOCAL, duration="unbounded",
)
def finalize_engineering_change(plan_id: str, commit: bool = True) -> str:
    ok, msg = engineering.finalize(plan_id, commit=commit)
    return msg


@tool(
    "abandon_engineering_change",
    "放弃一个还没 finalize 的计划：回滚已写入的部分（若有）并作废该计划。"
    "计划一旦已经 finalize 落地提交，无法用这个工具撤销。",
    {
        "type": "object",
        "properties": {"plan_id": {"type": "string"}},
        "required": ["plan_id"],
    },
    group=GROUP, effect=effects.WRITE_LOCAL,
)
def abandon_engineering_change(plan_id: str) -> str:
    ok, msg = engineering.abandon(plan_id)
    return msg
