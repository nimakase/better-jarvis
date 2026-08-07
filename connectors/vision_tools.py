"""
connectors/vision_tools.py — 通用图片理解工具（对应 core/vision.py，任务 #20）

跟 ingest_credential_image / ingest_document_file（OCR，专攻证件/文档结构化提取）
是互补关系，不是重复：这个工具用于开放式的"这张图里是什么/帮我看看这个"，
不假设图片是证件或文档。
"""
from __future__ import annotations

from functools import partial

from core import effects
from core.registry import tool as _tool

tool = partial(_tool, group="self")


@tool(
    "describe_image",
    "看一张本地图片并回答关于它的问题（开放式图片理解，不是 OCR 结构化提取）。"
    "证件/银行卡/合同这类要结构化字段的，改用 ingest_credential_image / "
    "ingest_document_file；这个工具用于「这张截图/照片里是什么」「帮我看看这张图"
    "表说明了什么」这类开放式问题。若当前主模型不支持视觉，这个工具会自动路由到"
    "配置好的视觉模型（无配置则如实告知看不了，不会假装能看图）。",
    {
        "type": "object",
        "properties": {
            "image_path": {"type": "string", "description": "本地图片文件路径"},
            "question": {"type": "string", "description": "关于这张图片的具体问题；留空则给出通用描述"},
        },
        "required": ["image_path"],
    },
    effect=effects.READ_LOCAL,
    duration="slow",
    capability_workaround="vision",  # 主模型一旦原生带视觉，这条通路多半可以退休
)
async def describe_image(image_path: str, question: str = "") -> str:
    from core import vision
    return await vision.describe(image_path, question)
