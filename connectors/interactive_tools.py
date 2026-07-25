"""
connectors/interactive_tools.py — 让贾维斯主动设计「可交互」的回复

动机：飞书（及网页）支持卡片里放【可点击按钮】和【结构化表单】，但此前贾维斯
只会发纯文本/富文本卡，任何"决策"都得让用户【打字】（如"激活 X""确认发送"）。
本组给模型两把工具，让它按内容自行决定何时把回复升级成可交互形态：

  · send_choice_buttons —— 当你在向用户「征求一个选择/确认/下一步动作」时，
    与其让用户打字，不如给几个按钮。点击 = 把该选项的 intent 当作用户的话重放
    回对话（复用整条文本管线），于是"点按钮"和"用户打了这句话"完全等价。

  · send_input_form —— 当你需要用户提供【多个结构化字段】（如新建日历事件的
    标题/时间/地点）时，发一张表单一次收齐，胜过来回追问。

这两把工具产出 channel-agnostic 的 Action("interactive")，由各渠道自行渲染：
飞书→2.0 回传卡（见 core/lark_cards + lark_bridge），网页→可点卡片，后台→退化文本。

设计准则（写进工具说明，指导模型）：正文里仍写一句自然的引导语，把"选项/表单"
用这两把工具附上；不要在正文里把选项又用文字重复一遍。
"""
from __future__ import annotations

from core.registry import tool as _tool
from core.results import ToolResult, present_options, present_form


@_tool(
    "send_choice_buttons",
    "给用户一组【可点击按钮】来做选择/确认/选下一步，而不是让用户打字。"
    "适用：需要用户在有限选项里选一个（是/否、方案A/B/C、激活/修改/放弃等）。"
    "点击某按钮 = 把它的 intent 当作用户消息重放回对话，所以 intent 要写成"
    "一句你之后能据此行动的清晰指令。用法约定：正文（你本轮的回复文字）里写一句"
    "自然的引导语即可，不要把选项再用文字罗列一遍——选项只放这里。",
    {
        "type": "object",
        "properties": {
            "text": {"type": "string",
                     "description": "卡片正文：向用户说明在选什么（简明一两句）。"},
            "options": {
                "type": "array",
                "description": "2~5 个选项。",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "按钮上的文字（短）"},
                        "intent": {"type": "string",
                                   "description": "点击后当作用户输入重放的话，如"
                                                  "『激活 stock_quote』『确认发送』"},
                        "style": {"type": "string", "enum": ["primary", "danger", "default", "text"],
                                  "description": "按钮样式：主操作 primary、危险 danger、普通 default"},
                    },
                    "required": ["label", "intent"],
                },
            },
            "title": {"type": "string", "description": "卡片标题（可选，默认『🤖 贾维斯』）"},
        },
        "required": ["text", "options"],
    },
    group="interactive",
    effect="write_local",
)
async def send_choice_buttons(text: str, options: list, title: str = "") -> ToolResult:
    opts = []
    for o in (options or []):
        if not isinstance(o, dict):
            continue
        label = str(o.get("label", "")).strip()
        intent = str(o.get("intent", label)).strip()
        if not label or not intent:
            continue
        opts.append({"label": label, "intent": intent, "style": o.get("style", "default")})
    if not opts:
        return ToolResult(text="（未提供有效选项，未发送按钮卡）")
    labels = " / ".join(o["label"] for o in opts)
    return ToolResult(
        text=f"已给用户发出可点击选项：{labels}。等待用户点击（点击等价于用户说出对应指令）。",
        actions=[present_options(text=text, options=opts, title=title or "")],
    )


@_tool(
    "send_input_form",
    "给用户一张【结构化表单】一次收齐多个字段，而不是来回追问。"
    "适用：新建日历事件（标题/时间/地点）、录入某条目多字段等。用户填完点提交，"
    "各字段值会连同 submit_intent 合成一句用户消息回流给你，你再据此执行。"
    "正文写一句引导语即可，不要把字段再用文字罗列。",
    {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "表单正文：说明这张表要收什么。"},
            "fields": {
                "type": "array",
                "description": "要收集的字段。",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "字段标识（英文，提交时作为 key）"},
                        "label": {"type": "string", "description": "字段显示名（中文）"},
                        "type": {"type": "string", "enum": ["input", "textarea", "select"],
                                 "description": "输入类型：单行 input、多行 textarea、下拉 select"},
                        "placeholder": {"type": "string", "description": "占位提示（可选）"},
                        "options": {
                            "type": "array",
                            "description": "type=select 时的下拉选项",
                            "items": {"type": "object", "properties": {
                                "label": {"type": "string"}, "value": {"type": "string"}}},
                        },
                    },
                    "required": ["name", "label"],
                },
            },
            "submit_label": {"type": "string", "description": "提交按钮文字（默认『提交』）"},
            "submit_intent": {"type": "string",
                              "description": "提交后当作用户输入重放的指令前缀，"
                                             "如『新建日历事件』"},
            "title": {"type": "string", "description": "卡片标题（可选）"},
        },
        "required": ["text", "fields"],
    },
    group="interactive",
    effect="write_local",
)
async def send_input_form(text: str, fields: list, submit_label: str = "提交",
                          submit_intent: str = "提交表单", title: str = "") -> ToolResult:
    clean = []
    for f in (fields or []):
        if isinstance(f, dict) and f.get("name") and f.get("label"):
            clean.append(f)
    if not clean:
        return ToolResult(text="（未提供有效字段，未发送表单卡）")
    names = "、".join(f["label"] for f in clean)
    return ToolResult(
        text=f"已给用户发出表单（{names}）。等待用户填写并提交。",
        actions=[present_form(text=text, fields=clean, submit_label=submit_label,
                              submit_intent=submit_intent, title=title or "")],
    )
