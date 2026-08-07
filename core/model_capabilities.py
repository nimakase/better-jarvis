"""
core/model_capabilities.py — 模型能力自知（声明式能力表）

背景：此前唯一的"模型能力自知"是 controller._network_capability_note() 里一条
写死的 if（字符串匹配 config.CLAUDE_MODEL 有没有 ":online"），只回答"能不能联网"
一件事，且判断逻辑硬编码在 system prompt 拼装函数里。

这带来两个问题：① DeepSeek 官方 API 没有 :online 这个语法，迁移后这条判断自动
失效（这是好事，但只是巧合）；② DeepSeek V4 已经在 2026-04 原生支持了视觉，
贾维斯的代码里完全没有感知——不是因为做不到，是因为压根没有地方声明"模型现在
能做什么"，多加一种能力就要多写一条 if，散在各处。

这张表把"模型字符串 → 能力集合"收敛成一处声明，_network_capability_note 和未来
任何"这个模型能不能做 X"的判断都从这里查。这依然需要人工维护——模型能力变化
终究要有人先知道、再更新这张表——但至少把"改哪里"从"满代码搜哪里写了 if"
收敛成"改这一张表"。定时轮询模型目录、自动发现能力变化是姊妹机制（配合
core/telemetry.log_gap 的"能力缺口"记录，另建），本模块只负责声明与查询。
"""
from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class ModelCapabilities:
    online_search: bool = False     # 是否原生/经路由层带联网检索（如 OpenRouter :online）
    vision: bool = False            # 是否支持图片输入
    context_window: int = 0         # 上下文窗口 token 数，0 = 未知
    notes: str = ""


# 未登记的模型 → 保守起见不声称任何能力（fail-safe，跟 self_model.py 的判定精神一致：
# 没写等于没有，不能反过来假设"新模型什么都会"）。
UNKNOWN = ModelCapabilities(notes="未登记的模型，能力未知——保守起见不声称任何能力")

# 已知模型/路由片段的能力声明。key 按"包含匹配"（模型字符串里含这段就命中），
# 命中多条时取最长匹配（更具体的声明优先于笼统的）。
_KNOWN: dict[str, ModelCapabilities] = {
    "deepseek-v4-flash": ModelCapabilities(
        vision=True, context_window=1_000_000,
        notes="DeepSeek 官方 API v4-flash，2026-04 起原生支持视觉，本身无内置联网",
    ),
    "deepseek-v4-pro": ModelCapabilities(
        vision=True, context_window=1_000_000,
        notes="DeepSeek 官方 API v4-pro，同上",
    ),
    "deepseek/deepseek-v4-flash": ModelCapabilities(
        vision=True, context_window=1_000_000,
        notes="经 OpenRouter 转发的 DeepSeek v4-flash；联网与否取决于是否带 :online 后缀",
    ),
    "deepseek/deepseek-v4-pro": ModelCapabilities(
        vision=True, context_window=1_000_000,
        notes="经 OpenRouter 转发的 DeepSeek v4-pro，同上",
    ),
}


def capabilities_of(model: str) -> ModelCapabilities:
    """按模型字符串查能力表。

    :online 后缀单独判断、按位或合并进最终结果——它是 OpenRouter 的路由层插件，
    不属于模型本身的能力，任何模型只要带这个后缀就获得联网能力，跟登记表无关。
    """
    model = model or ""
    matched: tuple[str, ModelCapabilities] | None = None
    for key, caps in _KNOWN.items():
        if key in model and (matched is None or len(key) > len(matched[0])):
            matched = (key, caps)
    base = matched[1] if matched else UNKNOWN

    online = base.online_search or (":online" in model)
    if online != base.online_search:
        base = replace(base, online_search=online)
    return base
