"""
core/lark_cards.py — 飞书卡片构建（JSON 2.0）· 纯函数、可脱离 SDK 单测

为什么单独成模块：卡片的「长什么样」与「怎么发」是两件事。发送/流式/回调
握手都在 lark_bridge.py（依赖 lark-oapi）；本模块只做纯粹的 dict 拼装，
不 import 任何 SDK，因此能被单元测试直接断言结构，也便于将来复用到别的渠道。

统一采用飞书【卡片 JSON 2.0】结构（schema=2.0）：
  - body.elements 承载内容；markdown 组件即富文本正文；
  - 按钮用 behaviors=[{type:callback, value:{...}}] 回传交互；
  - value 里统一塞一个 __jarvis_intent__ 字段：点击 = 把这句话当作用户消息
    重新喂回主对话（见 lark_bridge._on_card_action）。这样按钮不需要新的
    控制器分支，直接复用整条文本管线——「点按钮」等价于「用户打了这句话」。

与「让贾维斯自己决定表达」的关系：模型通过 present_choices 工具产出
channel-agnostic 的 Action("interactive")，飞书侧用本模块把它渲染成带按钮/
表单的 2.0 卡片；模型只描述「给用户哪些选项、点了各自意味着什么」，不关心
飞书 JSON 细节。
"""
from __future__ import annotations

import json
from typing import Any, Optional

# 点击按钮/提交表单时回传给自己的意图键：其值是一句「当作用户输入重放」的话。
INTENT_KEY = "__jarvis_intent__"

# 卡片标题（与旧实现保持一致的观感）。
DEFAULT_TITLE = "🤖 贾维斯"

# 飞书卡片正文有长度上限；超长截断，避免整条消息发送失败。
_MAX_MD = 9990

# 表头主题色（飞书内置模板色）。按钮 type 亦复用这几个语义色。
_HEADER_TEMPLATE = "blue"


# ── 内部小工具 ────────────────────────────────────────────────────────────────

def _truncate(content: str) -> str:
    if content and len(content) > _MAX_MD:
        return content[:_MAX_MD] + "\n\n…（内容较长已截断）"
    return content or ""


def _header(title: str) -> dict:
    return {
        "title": {"tag": "plain_text", "content": title or DEFAULT_TITLE},
        "template": _HEADER_TEMPLATE,
    }


def _markdown(content: str, element_id: str = "md") -> dict:
    """富文本正文组件。element_id 供 cardkit 流式增量更新定位（见 lark_bridge）。"""
    return {"tag": "markdown", "content": _truncate(content), "element_id": element_id}


# ── 对外：文本卡（默认回复形态）──────────────────────────────────────────────

def text_card(content: str, title: str = DEFAULT_TITLE, *, streaming: bool = False) -> dict:
    """最常用的一张卡：标题 + 一段富文本正文。

    streaming=True 时打开 2.0 的流式模式，正文组件带稳定 element_id，配合
    cardkit 的增量推送做原生打字机；不开则是普通静态卡（也可被整卡 patch 刷新）。
    """
    config: dict[str, Any] = {"wide_screen_mode": True, "update_multi": True}
    if streaming:
        config["streaming_mode"] = True
        # 打字机节奏：值越大越顺滑但越"慢"。给一组温和默认。
        config["streaming_config"] = {
            "print_frequency_ms": {"default": 30},
            "print_step": {"default": 2},
            "print_strategy": "fast",
        }
    return {
        "schema": "2.0",
        "config": config,
        "header": _header(title),
        "body": {"elements": [_markdown(content or "…")]},
    }


def text_card_json(content: str, title: str = DEFAULT_TITLE, *, streaming: bool = False) -> str:
    """text_card 的 JSON 字符串版（发送接口要 content 是字符串）。"""
    return json.dumps(text_card(content, title, streaming=streaming), ensure_ascii=False)


# ── 对外：按钮 / 交互卡 ────────────────────────────────────────────────────────

# 选项样式 → 飞书按钮 type。缺省 default（灰）。
_BTN_TYPES = {"primary", "danger", "default", "text"}


def _button(label: str, intent: str, style: str = "default",
            extra_value: Optional[dict] = None) -> dict:
    """一个回传按钮：点击后飞书回调，value 里带 __jarvis_intent__=intent。"""
    value: dict[str, Any] = {INTENT_KEY: intent}
    if extra_value:
        value.update(extra_value)
    btype = style if style in _BTN_TYPES else "default"
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": btype,
        "behaviors": [{"type": "callback", "value": value}],
    }


def _button_row(options: list[dict]) -> dict:
    """把若干按钮横向排布（column_set，每列一个按钮，窄屏自动换行）。"""
    columns = []
    for opt in options:
        columns.append({
            "tag": "column",
            "width": "auto",
            "elements": [_button(
                opt.get("label", "确定"),
                opt.get("intent", opt.get("label", "")),
                opt.get("style", "default"),
                opt.get("value"),
            )],
        })
    return {"tag": "column_set", "flex_mode": "flow", "horizontal_spacing": "8px",
            "columns": columns}


def interactive_card(text: str, options: list[dict], *,
                     title: str = DEFAULT_TITLE) -> dict:
    """正文 + 一排回传按钮。

    options: [{label, intent, style?, value?}, ...]
      - label：按钮文字
      - intent：点击后【当作用户消息重放】的那句话（关键）
      - style：primary / danger / default / text（可选）
      - value：附加回传数据（可选，进 value dict）
    """
    elements: list[dict] = [_markdown(text or "请选择：")]
    if options:
        elements.append(_button_row(options))
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": _header(title),
        "body": {"elements": elements},
    }


def interactive_card_json(text: str, options: list[dict], *,
                          title: str = DEFAULT_TITLE) -> str:
    return json.dumps(interactive_card(text, options, title=title), ensure_ascii=False)


# ── 对外：表单卡（结构化输入）──────────────────────────────────────────────────

def _form_field(field: dict) -> dict:
    """把一个字段描述转成 2.0 表单内的输入组件。

    field: {name, label, type?(input|textarea|select), options?(select 用), placeholder?}
    每个组件的 name 会成为提交时 form_value 里的 key。

    注意：飞书 2.0 没有 form_item 标签；label 直接作为 input / select_static 组件
    自身的属性（label + label_position:top），无需再包一层容器。
    """
    name = field["name"]
    label = field.get("label", name)
    ftype = field.get("type", "input")
    placeholder = field.get("placeholder", "")
    label_obj = {"tag": "plain_text", "content": label}

    if ftype == "select":
        opts = [{"text": {"tag": "plain_text", "content": o.get("label", o.get("value", ""))},
                 "value": o.get("value", o.get("label", ""))}
                for o in field.get("options", [])]
        return {
            "tag": "select_static",
            "name": name,
            "label": label_obj,
            "label_position": "top",
            "placeholder": {"tag": "plain_text", "content": placeholder or f"选择{label}"},
            "options": opts,
        }
    # input / textarea 都用 2.0 input 组件（label 直接挂在组件上）
    return {
        "tag": "input",
        "name": name,
        "label": label_obj,
        "label_position": "top",
        "placeholder": {"tag": "plain_text", "content": placeholder or f"请输入{label}"},
    }


def form_card(text: str, fields: list[dict], *,
              submit_label: str = "提交",
              submit_intent: str = "提交表单",
              title: str = DEFAULT_TITLE) -> dict:
    """一张表单卡：正文 + 若干字段 + 提交按钮。

    提交时飞书回调携带 form_value（各字段 name→值）；lark_bridge 会把它连同
    submit_intent 合成一句用户消息（见 _on_card_action）。
    """
    form_elements: list[dict] = [_form_field(f) for f in fields]
    form_elements.append({
        "tag": "button",
        "name": "submit",
        "text": {"tag": "plain_text", "content": submit_label},
        "type": "primary",
        # 2.0：把 form 内按钮标记为提交触发器的字段是 form_action_type=submit
        # （不是 action_type=form_submit——后者会被当成普通回调按钮，飞书判定
        # form 里没有提交按钮而整卡拒收 code=230099，见 scripts/test_lark_form.py
        # 的实测：唯有 form_action_type=submit 被飞书接受）。behaviors 回调保留，
        # 点击时携带 form_value + submit_intent 回流。
        "form_action_type": "submit",
        "behaviors": [{"type": "callback", "value": {INTENT_KEY: submit_intent}}],
    })
    body_elements = [
        _markdown(text or "请填写："),
        {"tag": "form", "name": "jarvis_form", "elements": form_elements},
    ]
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": _header(title),
        "body": {"elements": body_elements},
    }


def form_card_json(text: str, fields: list[dict], **kw) -> str:
    return json.dumps(form_card(text, fields, **kw), ensure_ascii=False)


# ── 回调 → 用户意图 的纯逻辑（可单测；lark_bridge 调用它）──────────────────────

def intent_from_callback(value: Optional[dict], form_value: Optional[dict]) -> str:
    """把一次卡片回调（按钮 value + 可选 form_value）翻译成一句"用户输入"。

    规则：
      - 取 value[__jarvis_intent__] 作为主指令（按钮/提交语义）；
      - 若带 form_value（表单提交），把「字段: 值」附在后面，让模型拿到填写内容。
    返回空串表示这次回调没有可执行意图（调用方应忽略）。
    """
    value = value or {}
    intent = (value.get(INTENT_KEY) or "").strip()
    fv = form_value or {}
    if fv:
        pairs = "；".join(f"{k}={v}" for k, v in fv.items() if v not in (None, ""))
        if pairs:
            intent = (intent + f"（表单内容：{pairs}）") if intent else f"表单提交：{pairs}"
    return intent
