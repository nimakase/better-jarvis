"""
core/model_routing.py — 子 agent 模型路由表（任务 #20）

背景：此前 core/spawn.py 的 _subagent_model() 只有一个全局开关
（env JARVIS_SUBAGENT_MODEL，运行时动态读取，不在导入时缓存——方便调用方/测试
随时改 env 立即生效），所有子 agent 不管干什么活都用同一个模型（留空则跟主模型
一样）。不同活对模型的要求不一样——读图要视觉能力，审代码要强代码能力——
一刀切撑不住。这张表把"活的性质（purpose）"和"该用哪个模型"解耦，新增用途只需
加一行映射，不用改调用方逻辑。

设计刻意保持简单（不做能力自动推断/自动选型这类更复杂的机制）：
  - 按 purpose 查表，映射到对应 env 变量名，【每次调用时动态读取】——不在导入
    时把值缓存进字典（2026-08-08 修正：第一版缓存过，导致运行时改 env 不生效，
    破坏了 core.spawn._subagent_model() 原有的"随时可改 env 立即生效"行为，
    被 tests/test_workflow_dispatch.py 的既有用例抓到）。
  - purpose="default" 对应 JARVIS_SUBAGENT_MODEL，就是原来 _subagent_model()
    的唯一行为，完全等价——没配置任何 env 时零回归。
  - 未知 purpose / 该 purpose 对应 env 未设置，一律退回 default 路由的 env 值，
    不报错、不阻断。
"""
from __future__ import annotations

import os

# purpose -> env 变量名（值本身运行时动态读，不在这里缓存）
_ROUTE_ENV: dict[str, str] = {
    "default": "JARVIS_SUBAGENT_MODEL",
    # 视觉理解（core/vision.py 用；留空则若主模型自带视觉直接复用主模型）
    "vision": "JARVIS_SUBAGENT_MODEL_VISION",
    # 代码审查类子任务（更看重代码能力，非通用对话能力）
    "code_review": "JARVIS_SUBAGENT_MODEL_CODE_REVIEW",
}


def model_for(purpose: str = "default") -> str:
    """返回该用途该用的模型名；空字符串 = 跟主模型一样（不覆盖）。
    未知 purpose / 对应 env 未设置，一律退回 default 路由。动态读 env，不缓存。"""
    env_name = _ROUTE_ENV.get(purpose, _ROUTE_ENV["default"])
    val = os.environ.get(env_name, "")
    if val:
        return val
    return os.environ.get(_ROUTE_ENV["default"], "")


def known_purposes() -> list[str]:
    return sorted(_ROUTE_ENV.keys())
