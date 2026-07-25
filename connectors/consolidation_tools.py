"""
connectors/consolidation_tools.py — 记忆巩固的触发工具（core/consolidation）

定时跑：用 create_schedule 建一条调用本工具的任务（如每晚一次）即可，无需新调度代码。
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
