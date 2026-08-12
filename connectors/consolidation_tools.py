"""
connectors/consolidation_tools.py — 记忆巩固的触发工具（core/consolidation）

定时跑：用 create_schedule 建一条调用本工具的任务（如每晚一次）即可，无需新调度代码。

2026-08-12 补：过程记忆巩固（run_procedural_consolidation）+ 按需查询（recall_procedure）。
过程记忆不常驻注入 system prompt（渐进式披露——细节按需展开），靠 recall_procedure
在真正卡壳/报错时主动查一下，别人踩过的坑不必自己重踩。
"""
from functools import partial

from core import effects
from core.registry import tool as _tool

tool = partial(_tool, group="self")


@tool(
    "run_consolidation",
    "跑一轮【记忆巩固】：复盘近期对话与监督信号（用户的纠正/放弃/重试），对常驻用户"
    "档案提出新增/确认/修正/过时标记，经机械闸（证据必填、去重、限量、只软删）后写回，"
    "并产出复盘文档。适合定时触发（如每晚）或用户说「整理一下你的记忆」时调用。",
    {"type": "object", "properties": {}},
    effect=effects.WRITE_LOCAL,
)
async def run_consolidation() -> str:
    from core import consolidation
    res = await consolidation.run_consolidation()
    lines = [
        f"记忆巩固完成：模型提 {res['n_ops']} 条操作。",
        f"✅ 执行 {len(res['applied'])} 条、降级为确认 {len(res['demoted'])} 条、"
        f"打回 {len(res['rejected'])} 条。",
        f"复盘：{res['review_path']}",
    ]
    if res["applied"]:
        for o in res["applied"][:5]:
            op = o["op"]
            lines.append(f"  · {o['verdict']}：{(op.get('text') or op.get('reason') or '#' + str(op.get('id')))[:60]}")
    return "\n".join(lines)


@tool(
    "run_procedural_consolidation",
    "跑一轮【过程记忆巩固】：复盘近期 self_review 复盘文档、对话与监督信号，提炼"
    "「遇到某类问题该怎么解决」的可复用经验，经机械闸（依据必填、去重、限量、只软删）"
    "后写回过程记忆库，并产出复盘文档。适合定时触发（如每周）或用户说「把这次踩坑的"
    "经验记一下」时调用。",
    {"type": "object", "properties": {}},
    effect=effects.WRITE_LOCAL,
)
async def run_procedural_consolidation() -> str:
    from core import consolidation
    res = await consolidation.run_procedural_consolidation()
    lines = [
        f"过程记忆巩固完成：模型提 {res['n_ops']} 条操作。",
        f"✅ 执行 {len(res['applied'])} 条、降级为确认 {len(res['demoted'])} 条、"
        f"打回 {len(res['rejected'])} 条。",
        f"复盘：{res['review_path']}",
    ]
    if res["applied"]:
        for o in res["applied"][:5]:
            op = o["op"]
            desc = op.get("problem") or op.get("reason") or ("#" + str(op.get("id")))
            lines.append(f"  · {o['verdict']}：{desc[:60]}")
    return "\n".join(lines)


@tool(
    "recall_procedure",
    "按关键词检索【过程记忆】(解决问题的方法/踩过的坑)——卡壳、报错、不确定该怎么"
    "处理时先查一下，可能已有前人经验可直接照做，避免重新踩坑。只在需要时调用"
    "（渐进式披露：过程记忆不常驻注入，靠这个按需展开完整解法）。",
    {"type": "object", "properties": {
        "query": {"type": "string", "description": "要查的问题/场景关键词"},
    }, "required": ["query"]},
    effect=effects.READ_LOCAL,
)
async def recall_procedure(query: str) -> str:
    from core import procedures
    hits = procedures.recall(query)
    if not hits:
        return f"没查到与「{query}」相关的过程记忆——可能是第一次遇到，正常排查即可。"
    lines = [f"查到 {len(hits)} 条相关过程记忆："]
    for p in hits:
        lines.append(f"\n#{p['id']} 问题：{p['problem']}\n解法：{p['method']}"
                     + (f"\n（依据：{p['evidence']}）" if p.get("evidence") else ""))
    return "\n".join(lines)
