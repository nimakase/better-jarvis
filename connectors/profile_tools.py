"""
用户档案（core memory）写入工具：remember_fact

让模型把"值得长期记住的稳定事实/偏好"钉进用户档案（core/profile.py），
之后每轮对话都会在 system prompt 里看到。这是替代旧记忆库的正确形态：
小而精、常驻、可由用户在设置面板增删审计。

读取无需工具——档案每轮已注入 system prompt，模型直接可见。
"""

from functools import partial

from core import profile
from core.registry import tool as _tool

tool = partial(_tool, group="profile")


@tool(
    "remember_fact",
    "把用户的【长期稳定】事实或偏好记入用户档案（core memory），之后每轮对话都会自动看到。"
    "适用于跨会话需要长期记住的信息：风险偏好、家庭成员、长期目标、关键日期、固定习惯等。"
    "不要用它存一次性/临时内容（那些靠对话历史即可）。一条只记一个事实，简洁陈述。",
    {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "要长期记住的单条事实，简洁陈述，如『风险偏好：保守』"}
        },
        "required": ["text"],
    },
)
async def remember_fact(text: str) -> str:
    return profile.add_fact(text)["message"]
