"""
connectors/spawn_tools.py — 子 agent 的对话入口（core/spawn）

主线程（对话中的贾维斯）用这两个工具把重活派给隔离的子 agent，自己保持轻快。

安全设计：这里【故意不暴露】extra_tools 参数——对话中的模型只能派出
默认只读权限的子 agent；给子 agent 升权（如允许写日历）只能由人在代码/
配置层显式做。模型不能给自己的分身加权。
"""
from functools import partial

from core import effects
from core.registry import tool as _tool
from core.spawn import spawn, spawn_many

tool = partial(_tool, group="self")


@tool(
    "spawn_subtask",
    "把一个需要多轮查询/阅读的重活派给隔离的子 agent 后台完成，只拿回结构化结论"
    "（不占用当前对话的上下文）。适合：调研某公司/主题、通读长文档后总结、跨多个"
    "信息源核对事实。子 agent 只有只读权限（查记忆/文档/联网读），不能写入或对外"
    "操作。同步等待结果，耗时约几十秒到几分钟——派发前告知用户一声。",
    {
        "type": "object",
        "properties": {
            "task":  {"type": "string", "description": "任务描述，自包含（子 agent 看不到本对话）"},
            "label": {"type": "string", "description": "任务短标签，如 B司调研"},
        },
        "required": ["task"],
    },
    effect=effects.READ_EXTERNAL,
)
async def spawn_subtask(task: str, label: str = "") -> str:
    res = await spawn(task, label=label)
    return res.brief()


@tool(
    "spawn_fanout",
    "并发派出多个只读子 agent（扇出调研）：同类任务批量做，如「分别调研这 5 家公司」。"
    "tasks 传任务描述数组，每个子任务隔离运行，汇总各自结论返回。数量建议 ≤5。",
    {
        "type": "object",
        "properties": {
            "tasks": {"type": "array", "items": {"type": "string"},
                      "description": "任务描述列表，每条自包含"},
        },
        "required": ["tasks"],
    },
    effect=effects.READ_EXTERNAL,
)
async def spawn_fanout(tasks: list) -> str:
    if not tasks:
        return "任务列表为空。"
    if len(tasks) > 5:
        return f"一次最多 5 个子任务（收到 {len(tasks)} 个），请分批。"
    results = await spawn_many([{"task": t, "label": f"扇出{i+1}"}
                                for i, t in enumerate(tasks)])
    return "\n\n".join(r.brief() for r in results)
