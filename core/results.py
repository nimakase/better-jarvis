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
