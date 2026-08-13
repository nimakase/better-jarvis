"""connectors/engineering_tools.py — 计划模式对话入口（PROTECTED，元能力）

暴露"执行一个人已经想清楚、内容已确定的多文件改动"这条通道。跟
`core/tool_builder.py`（造自建技能，只能写 skills/ 沙箱单文件）和
`core/self_review.py`/`self_iteration.py`（自主发现缺陷、生成提案、先红后绿
验证）都不是同一条路——那两条服务于"内容不确定，需要生成/验证"；这条服务于
"用户/Claude 在对话里已经把改动内容想清楚了，贾维斯只需要安全落地"。

设计详见 core/engineering.py 顶部注释；这里只管对话层接线。护栏摘要：
  - `propose_engineering_change` 只登记不写文件，READ_LOCAL，不需要确认；
    可选带 `steps` 把改动先拆成小步骤（引导默认走小改动，不是机械约束）。
  - `execute_engineering_change` 效应等级 WRITE_EXTERNAL，过 core.effects
    的 ConfirmGate——同一个 plan_id 首次调用会被机械拦下（模型把完整计划
    文本复述给用户看），用户确认后模型原样再调一次才真正解锁。**一次确认
    覆盖整个多文件计划**，不是逐文件确认。
  - `patch_open_file`（默认改法）只传旧文本→新文本做精确替换；`append_to_file`
    用于分段构建体量较大的新文件；`write_open_file` 收窄成"新建文件起始内容/
    确实要整体重写"时才用的兜底手段——三者都只能碰"已批准计划"登记过的文件
    路径，且必须是 `self_model` 判定为 OPEN 的路径（PROTECTED 一律拒绝，不
    生成二次审核提案——真要碰 PROTECTED 不是这条通道的职责）。三者写完都会
    对 .py 文件做一次语法自检，有问题立刻在返回文本里提示，不用等 finalize。
  - `finalize_engineering_change` 跑全量测试 gate，绿才提交；红则整个计划
    涉及的文件一次性回滚，不留半成品。
  - `abandon_engineering_change` 供用户中途改主意时用：回滚已写入的部分、
    作废计划。
  - 确认模型（借鉴商业 agent"批一次、之后自主执行、关键节点仍需人在场"的
    模式）：`propose_engineering_change`/`execute_engineering_change`/
    `finalize_engineering_change`/`abandon_engineering_change` 这四个（开
    新范围、确认闸本身、提交、作废）在后台/定时/子agent 实例里一律屏蔽，
    见 core/controller.py 的 BACKGROUND_BLOCKED_TOOLS——这几步依赖"用户看
    得到、能回话"这个前提。`write_open_file`/`patch_open_file`/
    `append_to_file` 改成【计划范围内有条件放行】：只要对应 plan_id 已经
    被交互式会话确认过（confirmed/executing），后台 worker/子agent 就能
    继续对这个已批准的计划落盘，不用每次写文件都拉人在场——批准的是计划
    整体，不是某一次具体的写入动作。`run_repo_test` 只读不写，不受此限制。
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
    "讨论清楚，或走 run_self_review 的自主反思闭环）。改动稍大时【建议】带上 steps，"
    "先把它拆成几个小步骤——这样后面落地时天然就是一步一小段（patch/append），"
    "不用被迫一次性生成一整份大文件。返回渲染好的计划文本（含每个文件是否可写的"
    "预检结果），登记后用 execute_engineering_change(plan_id) 解锁写入。",
    {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "这个计划要做什么，一句话"},
            "files": {"type": "array", "items": {"type": "string"},
                      "description": "计划涉及的文件路径清单（相对仓库根），如 ['core/foo.py', 'tests/test_auto_foo.py']"},
            "rationale": {"type": "string", "description": "为什么要这么改（可选）"},
            "steps": {"type": "array",
                      "description": "可选：把改动拆成几个小步骤，便于后面按步骤小段落地。"
                                     "每项 {description, files}，files 是该步骤涉及的文件子集。",
                      "items": {
                          "type": "object",
                          "properties": {
                              "description": {"type": "string"},
                              "files": {"type": "array", "items": {"type": "string"}},
                          },
                          "required": ["description"],
                      }},
        },
        "required": ["summary", "files"],
    },
    group=GROUP, effect=effects.READ_LOCAL,
)
def propose_engineering_change(summary: str, files: list, rationale: str = "", steps: list = None) -> str:
    plan = engineering.propose(summary, files, rationale, steps)
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
    "把【完整】内容写入一个已批准计划里登记过的文件（原子写入，首次写入前自动做"
    "快照，供计划失败时整体回滚）。这是整篇覆盖的兜底手段，只用于「新建文件的"
    "起始内容」或「确实需要整体重写」——日常小改动请优先用 patch_open_file（改"
    "一小段），新建的大文件请用 append_to_file 分段构建，不要为了改几行就把整个"
    "文件内容重新生成一遍。只能写 propose 时列出的路径，且该路径必须不受保护——"
    "受保护路径会被拒绝，不能绕开。写完会对 .py 文件做一次语法自检，结果附在返回文本里。",
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
    "patch_open_file",
    "对一个已存在的文件做一次精确的旧文本→新文本替换——日常改动的【默认方式】，"
    "只需要生成变化的那一小段，不用把整份文件内容再吐一遍。old_text 必须在当前"
    "文件内容里恰好出现一次（建议带上足够上下文保证唯一），否则会被拒绝。写完"
    "会对 .py 文件做一次语法自检，结果附在返回文本里。",
    {
        "type": "object",
        "properties": {
            "plan_id": {"type": "string"},
            "path": {"type": "string", "description": "相对仓库根的文件路径，须在计划清单内"},
            "old_text": {"type": "string", "description": "要被替换的原文本，须在文件里唯一出现"},
            "new_text": {"type": "string", "description": "替换后的新文本"},
        },
        "required": ["plan_id", "path", "old_text", "new_text"],
    },
    group=GROUP, effect=effects.WRITE_LOCAL,
)
def patch_open_file(plan_id: str, path: str, old_text: str, new_text: str) -> str:
    ok, msg = engineering.patch_file(plan_id, path, old_text, new_text)
    return msg


@tool(
    "append_to_file",
    "向一个文件追加一段内容——用于分片构建体量较大的新文件：不需要一次生成全文，"
    "按段多次调用，每次只吐这一段，自然拼成完整文件。文件不存在时第一次调用等价"
    "于新建，已存在则接着往后加。写完会对 .py 文件做一次语法自检，结果附在返回"
    "文本里（只有全部段落追加完、文件语法完整时才会通过）。",
    {
        "type": "object",
        "properties": {
            "plan_id": {"type": "string"},
            "path": {"type": "string", "description": "相对仓库根的文件路径，须在计划清单内"},
            "content": {"type": "string", "description": "要追加的这一段内容"},
        },
        "required": ["plan_id", "path", "content"],
    },
    group=GROUP, effect=effects.WRITE_LOCAL,
)
def append_to_file(plan_id: str, path: str, content: str) -> str:
    ok, msg = engineering.append_file(plan_id, path, content)
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
    if ok:
        return f"{status}\n{out[-2000:]}"
    # 失败时：不能只靠"失败摘要恰好落在最后2000字符里"这种运气——run_all.py
    # 聚合多个测试文件的输出很容易远超这个窗口，把关键的"谁失败了"埋没在中间。
    # 改为主动从完整输出里挑出失败行（❌/✗ 开头），摘要单独放在最前面，
    # 后面再跟一段尾部原文作为上下文（2026-08-13：修复贾维斯自己被这个盲区
    # 卡住近30轮工具调用才摸清失败清单的问题，见项目记忆）。
    fail_lines = [ln for ln in out.splitlines() if ln.strip().startswith(("❌", "✗"))]
    parts = [status]
    if fail_lines:
        parts.append("── 失败摘要（从完整输出中提取，不依赖截断位置）──")
        parts.append("\n".join(fail_lines[-40:]))
        parts.append("── 完整输出尾部（最后1500字符，供进一步排查）──")
        parts.append(out[-1500:])
    else:
        # 没有匹配到已知失败标记格式（比如脚本直接崩溃/异常退出）——退回原始尾部，
        # 但给足窗口，别再让最后一点线索被截没。
        parts.append(out[-3000:])
    return "\n".join(parts)


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
