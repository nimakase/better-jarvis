"""
工作流触发工具 run_workflow —— 运行已注册的多步骤工作流（潜客名单等）。

触发纪律（见 controller 的工作流政策）：仅在用户【明确要求运行】或定时任务触发时调；
对有副作用的工作流（confirm=true，会打开浏览器/连 HubSpot），必须先一句话告知将做什么、
得到确认后再调，绝不擅自或意外触发。读取无需工具——工作流目录已注入 system prompt。
"""
from functools import partial

from core.registry import tool as _tool
from core import workflow_registry as wr

tool = partial(_tool, group="workflows")


@tool(
    "run_workflow",
    "运行一个已注册的多步骤工作流（如 prospect_daily 今日潜客名单）。仅在用户明确要求运行时调用。"
    "对 confirm=true 的工作流（会打开有头 Chrome、连接 HubSpot），必须【先】用一句话告诉用户"
    "『我将运行X，会打开浏览器连 HubSpot』并取得确认，再调用本工具。可用工作流见 system prompt 的"
    "『可运行的工作流』。运行结束会返回各步骤状态（含是否降级）。",
    {
        "type": "object",
        "properties": {
            "workflow_id": {"type": "string", "description": "工作流 id，如 prospect_daily"},
        },
        "required": ["workflow_id"],
    },
)
async def run_workflow(workflow_id: str) -> str:
    res = await wr.run(workflow_id)
    run = res.get("run")
    if not run:
        return f"运行失败：{res.get('error', '未知错误')}"
    return f"工作流『{run.get('name', workflow_id)}』运行结束（{run.get('status')}）。\n{run.get('summary', '')}"
