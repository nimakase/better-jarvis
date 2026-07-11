"""
connectors/self_review_tools.py — 反思闭环的触发工具（阶段 3b · 受保护）

把「跑一轮自我迭代反思」暴露成一个第一方工具，便于手动触发；
定时则由 scheduler 创建一条调用本工具的任务实现（无需另写调度代码）。

跑前告知纪律：本工具有副作用（可能自动修改周边代码），属"有副作用工作流"，
模型应在用户显式索取或定时触发时才调用，并先告知。
"""
from functools import partial

from core.registry import tool as _tool

tool = partial(_tool, group="self")


@tool(
    "run_self_review",
    "跑一轮【自我迭代反思】：读上轮复盘 → 让模型对周边(OPEN)代码提小步优化 → "
    "合格(先红后绿+全量测试通过)的自动落地并可回滚，受保护(核心)的只生成提案送人工审。"
    "有副作用（可能自动改周边代码），仅在用户显式要求或定时触发时调用，调用前先告知。",
    {"type": "object", "properties": {}},
)
async def run_self_review() -> str:
    from core import self_review
    res = await self_review.run_cycle(self_review._default_model_fn)
    lines = [
        f"自我迭代反思完成：本轮 {res['n_proposals']} 条提案。",
        f"✅ 已落地({len(res['applied'])})：" + ("、".join(res["applied"]) or "无"),
        f"🔒 送人工({len(res['needs_human'])})：" + ("、".join(res["needs_human"]) or "无"),
        f"· 非必要仅记录({len(res['deferred'])})、⏸ 冷却跳过({len(res['skipped'])})。",
        f"↩︎ 打回({len(res['rejected'])})、✖ 失败({len(res['failed'])})。",
        f"复盘已记录：{res['summary_path']}",
    ]
    if res["needs_human"]:
        lines.append("（受保护提案需你在审查面板确认 diff + 影响 + 动机后才会生效。）")
    return "\n".join(lines)
