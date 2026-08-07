"""
connectors/insight_tools.py — 通用再分析工具（对应 core/insight.py，任务 #21）

对话中的贾维斯自己产出一份结构化分析结果（如整理出的账户分级摘要、报表统计）
后，用这个工具对它做一次"再分析"——主动发现使用者可能没问但值得注意的点，
而不是把原始数字直接甩给用户完事。
"""
from __future__ import annotations

import json

from functools import partial

from core import effects, insight
from core.registry import tool as _tool

tool = partial(_tool, group="self")


@tool(
    "generate_insights",
    "对一份你刚产出的结构化分析结果（如CRM账户分级摘要、报表统计、任何字段->数值/"
    "分布的汇总）做【再分析】：主动发现使用者可能没问但值得注意的点（某字段缺失/为0、"
    "某个分布过度集中在一类、跟上次相比的显著变化），而不是把原始数字直接甩给用户。"
    "用完把结果自然地融进你给用户的总结里，不要机械罗列成清单。"
    "data_json 是这份结果的 JSON 对象（字段名->数值/字符串/{类别:数量}分布）。"
    "missing_fields/dominance_fields/delta_fields 是你指定要检查哪些字段——"
    "这个工具不会替你猜哪些字段有业务意义，你需要根据实际数据点名。"
    "delta_fields 要生效必须同时给 prev_data_json（上一次的同结构快照，没有就不用传）。"
    "use_model=true 时额外做一次开放式判断（规则覆盖不到的"
    "「这几个数字放一起说明什么」），会多一次模型调用，默认不开。",
    {
        "type": "object",
        "properties": {
            "data_json": {"type": "string", "description": "本次结果的 JSON 对象字符串"},
            "context": {"type": "string", "description": "这份数据的背景说明（给 use_model 用）"},
            "prev_data_json": {"type": "string", "description": "上一次同结构快照的 JSON（可选，配合 delta_fields）"},
            "missing_fields": {"type": "array", "items": {"type": "string"},
                              "description": "要检查是否缺失/为0的字段名列表"},
            "dominance_fields": {"type": "array", "items": {"type": "string"},
                                "description": "要检查占比是否过度集中的分布型字段名列表（值须是{类别:数量}字典）"},
            "delta_fields": {"type": "array", "items": {"type": "string"},
                            "description": "要跟 prev_data_json 比对变化幅度的字段名列表"},
            "use_model": {"type": "boolean", "description": "是否额外做一次开放式模型判断，默认 false"},
        },
        "required": ["data_json"],
    },
    effect=effects.READ_LOCAL,
    duration="slow",  # use_model=true 时会有一次模型调用
)
async def generate_insights(data_json: str, context: str = "", prev_data_json: str = "",
                            missing_fields: "list | None" = None,
                            dominance_fields: "list | None" = None,
                            delta_fields: "list | None" = None,
                            use_model: bool = False) -> str:
    try:
        data = json.loads(data_json)
    except Exception as e:  # noqa: BLE001
        return f"data_json 不是合法 JSON：{e}"
    if not isinstance(data, dict):
        return "data_json 必须是一个 JSON 对象（字段名->值），不是数组/标量。"

    prev_data = None
    if prev_data_json.strip():
        try:
            prev_data = json.loads(prev_data_json)
        except Exception as e:  # noqa: BLE001
            return f"prev_data_json 不是合法 JSON：{e}"

    insights = insight.analyze(
        data, missing_fields=missing_fields, dominance_fields=dominance_fields,
        prev_data=prev_data, delta_fields=delta_fields,
    )
    if use_model:
        insights += await insight.analyze_with_model(data, context=context)

    text = insight.render(insights)
    return text or "再分析没有发现特别值得单独指出的点（数据看起来符合预期）。"
