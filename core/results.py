"""
工具结构化返回（ToolResult / Action）

替代早期「在 tool 字符串里塞魔法 JSON 标记，再由 main.py 扫描消息历史」的脆弱侧信道。

工具现在可返回 ToolResult(text, actions)：
  - text：进入对话历史、发给云端模型看的纯文本（脱敏 / 确认信息）。
  - actions：交给传输层（main.py）执行的【带外动作】——下载文件、展示待审查
    代码、本机解密揭示证件等。actions 绝不进入对话历史 / 云端。

向后兼容：工具仍可直接返回 str，由 controller 归一化为 ToolResult(text=str)。
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Action:
    type: str             # "code_review" | "file_download" | "credential_reveal"
    payload: dict


@dataclass
class ToolResult:
    text: str
    actions: list = field(default_factory=list)


# ── 便捷构造器（让连接器/元工具少写样板）──────────────────────────────────────

def code_review(name: str, code: str, validation: Any, message: str) -> Action:
    return Action("code_review", {
        "name": name, "code": code, "validation": validation, "message": message,
    })


def file_download(file_path: str, filename: str, size: int) -> Action:
    return Action("file_download", {
        "file_path": file_path, "filename": filename, "size": size,
    })


def credential_reveal(alias: str, fields: list) -> Action:
    return Action("credential_reveal", {"alias": alias, "fields": fields})


def present_options(text: str, options: list, title: str = "") -> Action:
    """给用户一组可点击的选项（channel-agnostic）。

    各渠道各自渲染：飞书→回传按钮卡；网页→可点按钮；后台→退化为纯文本。
    options: [{label, intent, style?}]，intent = 点击后【当作用户消息重放】的话。
    """
    return Action("interactive", {
        "mode": "options", "text": text, "options": options, "title": title,
    })


def present_form(text: str, fields: list, submit_label: str = "提交",
                 submit_intent: str = "提交表单", title: str = "") -> Action:
    """给用户一张结构化输入表单（channel-agnostic）。

    fields: [{name, label, type?(input|textarea|select), options?, placeholder?}]
    提交后各字段值会连同 submit_intent 合成一句用户消息回流对话。
    """
    return Action("interactive", {
        "mode": "form", "text": text, "fields": fields,
        "submit_label": submit_label, "submit_intent": submit_intent, "title": title,
    })
