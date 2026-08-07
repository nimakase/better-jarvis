"""
core/search_augment.py — 联网检索补偿层（DeepSeek 迁移的关键拼图）

背景：`intel/workflow_defs.py` 的 signal_collection 和 `prospecting/workflows.py`
的 `make_llm_generate_fn` 这两处"直连模型的一次性大补全"，此前都依赖
`config.CLAUDE_MODEL` 带 `:online`（OpenRouter 联网插件）——模型自己决定搜什么、
怎么搜。DeepSeek 官方 API 没有这个语法：迁移后这两处会**静默退化**成基于模型
静态训练知识生成，不报错，只是结果可能过期或编造——这是 DeepSeek 迁移里最容易
被忽略、后果却最贵的一环（这两处正好是真实产出潜客名单/市场信号的地方）。

补偿策略：不是让模型自己决定"要不要搜"（那需要工具调用循环，这两处刻意没有，
也不该有——`make_llm_generate_fn` 的注释写得很明白："这一步不需要工具循环"），
而是在生成前【由调用方显式发起搜索】，把搜索结果当作既成事实拼进提示词，模型
只负责基于给定材料做结构化整理。这是 RAG（先检索、再生成）的标准形状，不是
"agent 自主判断"的形状，跟这两处"确定性、不发挥"的既有设计哲学是同一路数，
不是新引入一种风格。

按 core/model_capabilities 判断：如果当前模型本身带 online_search（如仍在
OpenRouter :online 下），直接原样返回 prompt，不做任何改动——对现状零行为
影响，只有迁移到不带联网的模型后才会真正生效。
"""
from __future__ import annotations

import logging

logger = logging.getLogger("jarvis.search_augment")


async def augment_with_search(prompt: str, queries: list[str], *,
                               max_results_per_query: int = 5, label: str = "") -> str:
    """若当前模型没有内置联网，显式搜一遍 queries，把结果拼进 prompt 前面再返回；
    模型本身带联网则原样返回 prompt（零行为变化）。

    queries 支持多条（如 signal_collection 要覆盖多个主题面）。单条查询失败不影响
    其余查询；全部查询都拿不到东西时【原样返回 prompt，不阻断】——让生成步骤至少
    基于模型的既有知识产出，好过因为补偿机制本身失败就交白卷（"能凑合出结果"
    优先于"因为搜索失败而彻底罢工"，跟这两处工作流本身"明确报错而非静默返回空"
    的纪律不冲突：拼不到搜索材料不算错误，生成本身解析不出结果才算错误）。
    """
    import config
    from core import model_capabilities

    caps = model_capabilities.capabilities_of(config.CLAUDE_MODEL)
    if caps.online_search:
        return prompt   # 模型自己能联网，不用补偿，原样返回（零行为变化）

    from connectors import web_search as ws
    blocks = []
    for q in queries:
        q = (q or "").strip()
        if not q:
            continue
        try:
            text = await ws.web_search(q, max_results=max_results_per_query)
        except Exception as e:  # noqa: BLE001
            logger.warning("search_augment 查询失败（%s）：%s：%s", label, q, e)
            continue
        # web_search 内部失败时返回的是 _fallback_note（含"结构化搜索不可用"），
        # 这种"假成功"文本不该被当作真实检索材料拼进提示词。
        if text and "结构化搜索不可用" not in text:
            blocks.append(text)

    if not blocks:
        logger.warning("search_augment（%s）：全部 %d 条查询都没拿到可用结果，"
                       "prompt 原样返回（模型将基于既有知识作答，未必是最新信息）",
                       label, len(queries))
        return prompt

    search_context = "\n\n".join(blocks)
    return (
        "【以下是为你准备的联网检索结果——你本身没有内置联网能力，下面这些材料是"
        "调用方额外搜索补上的，请【只依据这些材料】补充你对近期/实时情况的了解；"
        "材料之外的具体数字/事件/公司名不要凭空编造，材料不够就如实说覆盖不到】\n\n"
        f"{search_context}\n\n---\n\n{prompt}"
    )
