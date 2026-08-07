"""
connectors/capability_watch_tools.py — 模型能力变化检测的可调用入口（任务 #19）

对应 core/capability_watch.py。想要"定时"检查，把这个工具挂一个
core.schedule 定时任务即可（多久查一次由用户决定，这里不预设周期）。
"""
from __future__ import annotations

from functools import partial

from core import effects
from core.registry import tool as _tool

tool = partial(_tool, group="self")


@tool(
    "check_capability_drift",
    "检查主模型的能力（联网/视觉等，见 core/model_capabilities）相比上次检查有没有"
    "变化。若发现新获得了某项能力，会顺带提示哪些工具是为绕过那项能力缺失搭的"
    "临时通路（如没视觉时用的 describe_image），现在可能不再需要绕远路了，建议"
    "复核。首次调用只记录基线，不报变化。想定期自动查，把这个工具挂一个定时任务。",
    {"type": "object", "properties": {}},
    effect=effects.READ_LOCAL,
)
async def check_capability_drift() -> str:
    from core import capability_watch
    result = capability_watch.check_drift()
    return capability_watch.describe_drift(result)
