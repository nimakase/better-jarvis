"""
core/llm.py — 模型客户端工厂（单一构建点）

背景（2026-07-22 审计）：AsyncOpenAI(...) 的构建散布在 12 处，超时政策不一致——
controller 在 2026-07-12 就修过「SDK 默认 600s 静默长挂」（加了有界超时），
但修法没传播：self_review / document / doc_vault / sensitivity / prospecting
等 7 处仍在裸奔默认 600s。同一个 bug 只该修一次。

约定：
  - 一律经 get_client(timeout=...) 拿客户端，默认超时与 controller 同源
    （JARVIS_LLM_TIMEOUT，默认 120s）；长任务（采集等）显式传大超时。
  - 新代码不要再直接 AsyncOpenAI(...)；见到就迁过来。
"""
from __future__ import annotations

import os

DEFAULT_TIMEOUT = float(os.environ.get("JARVIS_LLM_TIMEOUT", "120"))


def get_client(timeout: float = DEFAULT_TIMEOUT):
    """构建 OpenRouter 客户端（有界超时，绝不裸奔 SDK 默认 600s）。"""
    import config
    from openai import AsyncOpenAI
    return AsyncOpenAI(
        api_key=config.OPENROUTER_API_KEY,
        base_url=config.OPENROUTER_BASE_URL,
        timeout=timeout,
    )
