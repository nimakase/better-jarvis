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
    """构建模型客户端（有界超时，绝不裸奔 SDK 默认 600s）。

    2026-08-07：从"写死 OpenRouter"改为读 config.LLM_API_KEY/LLM_BASE_URL——
    这两个由 config.py 按 JARVIS_LLM_PROVIDER 联动派生（openrouter/deepseek），
    本函数不关心到底是哪家，切供应商不用改这里。DeepSeek 官方 API 与 OpenRouter
    一样是 OpenAI 兼容格式，同一个 AsyncOpenAI 客户端类型就能用。"""
    import config
    from openai import AsyncOpenAI
    return AsyncOpenAI(
        api_key=config.LLM_API_KEY,
        base_url=config.LLM_BASE_URL,
        timeout=timeout,
    )
